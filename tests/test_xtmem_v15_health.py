# -*- coding: utf-8 -*-
"""test_xtmem_v15_health.py — V1.5 熔断记账守卫（修复单版，2026-09-05，assistant-c）。

错题本 #27：旧版记账面 _probe_ok/fail_count 在 9900 网关进程内存累加，20000 独立进程
读不到 → 黄灯结构性永灭；且旧守卫【禁】同进程注入真 provider 对象造绿灯——上一版就是
这么假绿到验收的。

修复后判据（DoD）：
  ① 记账落 DB：probe 成败直写 xtmem_events（kind='probe-ok'/'probe-fail'），
     health 在 20000 独立进程读 DB 事件算 fail_rate（24h 窗口）；
  ② 三态守卫【按 start_server 同款链路起——不传 provider】（=生产形态）：
     - 临时库直写 probe-fail×4 + probe-ok×1 → total=5 且 fail_rate=0.8>0.5 → degraded + fail_rate 数；
     - 全 probe-ok（≥5）→ 不亮（status=ok）；
     - 无事件 → degraded 不亮 / fail_rate=None（兼容旧响应）；
  ③ 第二触发道：最近【连续】probe-fail（无 ok 打断）≥3 → degraded；
  ④ stale 红（V1.3）优先于 degraded 黄（红盖黄）。
环境纪律：tmp_db 副本，不碰生产。
"""
import json
import os
import sqlite3
import time

import pytest

from memory.open_db import open_db
import memory.api_server as ap
from memory.vector_store import VectorStore


def _put_probe_events(db, fails=0, oks=0, now=None, kinds_seq=None):
    """临时库直写 probe 事件（模拟 9900 provider 记账落 DB）。

    kinds_seq 给连续失败段测试用：如 ['fail','fail','fail','ok','fail']。
    """
    now = now or time.time()
    con = sqlite3.connect(str(db))
    seq = kinds_seq if kinds_seq is not None else (["fail"] * fails + ["ok"] * oks)
    for k in seq:
        kind = "probe-fail" if k == "fail" else "probe-ok"
        con.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, meta) VALUES (?, ?, 'v15t', 'dsh', ?)",
            (now, kind, json.dumps({"actor": "dsh", "err": "", "qlen": 0, "qsha": "abc12345"}, ensure_ascii=False)),
        )
    con.commit()
    con.close()


@pytest.fixture()
def _client(tmp_db):
    """TestClient【不传 provider】（= start_server 生产形态：create_app(provider=) 收了但
    start_server 不接不传 → provider 恒 None）。错题本 #27 防假绿——同进程注入真 provider
    造绿灯的旧路【禁】。"""
    os.environ["XTMEM_ACTOR"] = "v15-test"
    os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    # 2026-09-06 token 重签后：auth 文件非空 → 请求级鉴权强制生效，env noauth 只管启动闸。
    # 测试必须走 create_app(allow_noauth=True) 显式豁免（该参数就是为测试注入设计的）。
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        vs = VectorStore(db_path=str(tmp_db))
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)  # 无 provider
        from fastapi.testclient import TestClient
        yield TestClient(app), con
        vs.close()
    finally:
        con.close()
        os.environ.pop("XTMEM_ACTOR", None)
        os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)


def test_v15_db_events_high_fail_rate_degraded(tmp_db, _client):
    """DB 事件直写 probe-fail×4 + probe-ok×1 → total=5 fail_rate=0.8>0.5 → degraded。"""
    client, con = _client
    _put_probe_events(tmp_db, fails=4, oks=1)
    j = client.get("/api/memory/health").json()
    assert j["status"] == "degraded", j
    assert j["degraded_reason"] == "probe fail_rate>0.5", j
    assert j["probe"]["ok"] == 1 and j["probe"]["fail"] == 4, j["probe"]
    assert j["probe"]["fail_rate"] == 0.8, j["probe"]


def test_v15_db_events_all_ok_not_degraded(tmp_db, _client):
    """全 probe-ok（≥5）→ 不亮（status=ok，fail_rate=0.0）。"""
    client, con = _client
    _put_probe_events(tmp_db, fails=0, oks=6)
    j = client.get("/api/memory/health").json()
    assert j["status"] == "ok", j
    assert j["probe"]["ok"] == 6 and j["probe"]["fail"] == 0, j["probe"]
    assert j["probe"]["fail_rate"] == 0.0, j["probe"]


def test_v15_db_events_no_events_none(tmp_db, _client):
    """无事件 → degraded 不亮 / fail_rate=None / probe 全 0（兼容旧响应）。"""
    client, con = _client
    j = client.get("/api/memory/health").json()
    assert j["status"] == "ok", j
    assert j["probe"]["ok"] == 0 and j["probe"]["fail"] == 0, j["probe"]
    assert j["probe"]["fail_rate"] is None, j["probe"]
    assert j.get("consecutive_fail") == 0, j


def test_v15_db_events_consecutive_fail_degraded(tmp_db, _client):
    """第二触发道：最近【连续】probe-fail（无 ok 打断）≥3 → degraded（即使 total<5）。"""
    client, con = _client
    _put_probe_events(tmp_db, kinds_seq=["fail", "fail", "fail"])
    j = client.get("/api/memory/health").json()
    assert j["status"] == "degraded", j
    assert j["degraded_reason"] == "consecutive_fail>=3", j
    assert j.get("consecutive_fail") == 3, j


def test_v15_db_events_ok_interrupts_streak(tmp_db, _client):
    """ok 打断连续失败段：fail×3+ok+fail×2 → 连续段=2（<3 不因连续触发）。"""
    client, con = _client
    _put_probe_events(tmp_db, kinds_seq=["fail", "fail", "fail", "ok", "fail", "fail"])
    j = client.get("/api/memory/health").json()
    # total=6, fail=5 → fail_rate=0.833>0.5 仍 degraded（第一道触发）
    assert j["status"] == "degraded", j
    assert j["probe"]["fail_rate"] == pytest.approx(5 / 6, 3), j["probe"]
    # 连续段被 ok 打断 → 末尾连续 fail=2
    assert j.get("consecutive_fail") == 2, j


def test_v15_stale_takes_precedence(tmp_db, _client):
    """红盖黄：stale 与 degraded 同场时 status=stale。"""
    client, con = _client
    _put_probe_events(tmp_db, fails=6, oks=0)  # 触发 degraded 面
    # pid-alive 新语义（2026-09-05 误报面收口）：死 pid 不标红——假旧码条目必须用【活 pid】
    # （本测试进程）才能触发 stale；红盖黄判据不变
    con.execute(
        "UPDATE xtmem_meta SET value=? WHERE key='runtime_actors'",
        (json.dumps({str(os.getpid()): {"actor": "old", "code_sha": "dead", "ts": 1.0}}),),
    )
    con.commit()
    j = client.get("/api/memory/health").json()
    assert j["status"] == "stale"
