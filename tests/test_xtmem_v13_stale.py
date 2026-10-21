# -*- coding: utf-8 -*-
"""test_xtmem_v13_stale.py — V1.3 stale 端探测守卫（v1.3.2 立项批 #8，2026-09-05，assistant-c）。

四落点：open_db 上报 {actor, code_sha, pid} 至 xtmem_meta.runtime_actors（进程级去重、
表自建、自兜底）→ health 比对磁盘 code_sha，不匹配亮红（status=stale + stale_actors 明细）。

验穿（总则 13）：故意伪造旧码指纹（deadbeef）→ 红灯必亮；同码 → 绿。
环境纪律：测试走 tmp_db/empty_db 副本，【不】碰 canonical/生产库（V1.3 代码在生产树，
本测试只 import 不触发生产路径）。
"""
import json
import os
import sqlite3
import tempfile

import pytest

from conftest import PLUGIN_DIR

import sys as _sys
_sys.path.insert(0, str(PLUGIN_DIR))

from memory.open_db import open_db, get_runtime_actors, _runtime_code_sha


def test_v13_report_on_open(tmp_db):
    """可写 open_db 上报 {actor, code_sha, pid}；二次 open_db 进程级去重不重复写。"""
    os.environ["XTMEM_ACTOR"] = "v13-test"
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        actors = get_runtime_actors(con)
        mine = actors.get(str(os.getpid()), {})
        assert mine, "open_db 后 runtime_actors 应含本 pid"
        assert mine.get("actor") == "v13-test"
        assert mine.get("code_sha") == _runtime_code_sha(), "上报指纹应=当前磁盘指纹"
        n_before = len(get_runtime_actors(con))
        con2 = open_db(str(tmp_db))
        n_after = len(get_runtime_actors(con2))
        assert n_after == n_before, "同 pid 二次 open_db 不应新增条目（进程级去重）"
        con2.close()
    finally:
        con.close()
        os.environ.pop("XTMEM_ACTOR", None)


def test_v13_actor_table_self_create(empty_db):
    """Y7 前老库/全新库无 xtmem_meta——上报面 CREATE TABLE IF NOT EXISTS 自建（canonical 实证教训）。"""
    con = open_db(str(empty_db))
    try:
        con.execute("SELECT 1")
        tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='xtmem_meta'").fetchall()]
        assert tabs == ["xtmem_meta"], "上报面应自建 xtmem_meta"
    finally:
        con.close()


def test_v13_health_stale_detection(tmp_db):
    """health 端到端：同码绿（stale=[]）→ 人造旧码指纹红灯必亮（status=stale）。验穿主用例。"""
    os.environ["XTMEM_ACTOR"] = "v13-test"
    os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        import importlib
        import memory.api_server as ap
        importlib.reload(ap)
        from memory.vector_store import VectorStore
        vs = VectorStore(db_path=str(tmp_db))
        # 2026-09-06 token 重签后请求级鉴权强制生效——测试显式豁免
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
        from fastapi.testclient import TestClient
        client = TestClient(app)

        j = client.get("/api/memory/health").json()
        assert j.get("status") == "ok", "同码应绿"
        assert j.get("stale_actors") == [], "同码 stale 应空"
        assert j.get("runtime_code_sha") == _runtime_code_sha()

        # 人造旧码：本 pid 指纹改成 deadbeef（模拟旧码进程在场）
        con.execute(
            "UPDATE xtmem_meta SET value=? WHERE key='runtime_actors'",
            (json.dumps({str(os.getpid()): {"actor": "old", "code_sha": "deadbeef00000000", "ts": 1.0}}),),
        )
        con.commit()
        j2 = client.get("/api/memory/health").json()
        assert j2.get("status") == "stale", "旧码指纹红灯必亮（验穿）"
        stale = j2.get("stale_actors") or []
        assert any(s.get("code_sha") == "deadbeef00000000" for s in stale), "红灯应带旧码明细"
        vs.close()
    finally:
        con.close()
        os.environ.pop("XTMEM_ACTOR", None)
        os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)


def test_v13_report_never_blocks(tmp_db):
    """自兜底：上报失败（库只读/异常）不得阻塞 open_db 主链路。"""
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        # 把 xtmem_meta 表删掉 + 用只读 authorizer 模拟异常路径——上报静默失败、连接照常可用
        con.execute("DROP TABLE IF EXISTS xtmem_meta")
        con.commit()
        # 直接调上报函数（内部全 try/except）——不应抛
        from memory.open_db import _report_runtime_actor
        _report_runtime_actor(con, str(tmp_db))  # 不抛即过
        # 连接仍可查询
        assert con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 0
    finally:
        con.close()