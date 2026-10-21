# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_466d_fts5_health_guard.py · 466d 件②守卫
#
#   ▍这是什么
#   466d 零碎收尾二合一（XTMEM-派单工单-零碎收尾二合一-摘标与health联动-20260913）
#   件②守卫：api_server.memory_health() 的 FTS5 降级留痕联动段
#   （FTS5 单回执 §八 diff，2026-09-13 落地五树）。
#   病：FTS5 腿带伤时（fts5_fallback 事件）health 仍显示正常——观测面说谎。
#
#   ▍守卫内容
#   1. 造 fts5_fallback 事件 → health 应答含 fts5.fallback_24h>0 且 status 降
#      degraded + degraded_reason（工单 DoD 原文）；
#   2. 无事件 → fts5.ok=True、status 不降（防误伤）；
#   3. 24h 窗外旧事件 → 不计数（_win 口径）；
#   4. 红黄不叠加：stale（红）在场时 fts5 事件不再降黄（红盖黄，probe 段口径）；
#   5. 五树静态同步：联动段标记串在 5 份 api_server.py 全在场（防镜像分叉）。
#
#   ▍修改铁律
#   1. MASTER 直载运行时 api_server（错题本 M1：必须打中真实读取路径）；
#      红演示可用 XTMEM_466D_MASTER 指向 cbak 临时树换靶。
#   2. 断言只用 tempfile 夹具库，严禁连生产库。
#   3. 三镜像同步（xtmem-project/tests ↔ XTMEM/tests ↔ XTMEM-public/tests），
#      sha 不等即未交付。
#
#   ▍更新记录
#   *更新：2026-09-13 · 创建（maintainer 代执 466d 件②）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import sys
import tempfile
import time

import pytest

MASTER_PARENT = os.environ.get("XTMEM_466D_MASTER", _REPO / "src" / "hermes-plugin")
if MASTER_PARENT not in sys.path:
    sys.path.insert(0, MASTER_PARENT)

import memory.api_server as ap  # noqa: E402
import memory.vector_store as VS  # noqa: E402

# 五树静态同步清单（件② 落点；联动段标记串任何一份缺席=镜像分叉）
FIVE_TREES = [
    _REPO / "src" / "hermes-plugin" / "memory" / "api_server.py",
    _REPO / "xtmem-project"/ "src"/ "hermes-plugin"/ "memory"/ "api_server.py",
    _REPO / "XTMEM"/ "src"/ "hermes-plugin"/ "memory"/ "api_server.py",
    _REPO / "xtmem-project"/ "src"/ "hermes-plugin"/ "memory"/ "api_server.py",
    _REPO / "XTMEM-public"/ "src"/ "hermes-plugin"/ "memory"/ "api_server.py",
]
MARKER = "# FTS5 降级留痕"
REASON = "fts5_fallback events>0 (24h)"


def _mk_client(db_path):
    """临时库起 TestClient（vector_store 注入临时库；不传 provider=v15 生产形态纪律）。
    返回 (client, vs)——调用方断言完必须 vs.close()，否则 Windows 下连接不撒手，
    TemporaryDirectory 清理必炸 WinError 32（466c 守卫同款教训）。"""
    vs = VS.VectorStore(db_path)
    try:
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
    except TypeError:
        app = ap.create_app(vector_store=vs, host="127.0.0.1")  # 旧基线无 allow_noauth 形态
    from fastapi.testclient import TestClient
    return TestClient(app), vs


def _put_fts5_event(db_path, ts):
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, meta) VALUES (?, 'fts5_fallback', 't466d', 't', ?)",
            (ts, json.dumps({"note": "guard"}, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


def test_fts5_fallback_event_degrades_health():
    """造 fts5_fallback 事件 → fallback_24h>0 且 status 降 degraded（工单 DoD 原文）"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "g.db")
        client, vs = _mk_client(db)  # 先建库（_init_db 建表），后插事件——次序勿反
        try:
            _put_fts5_event(db, time.time())
            j = client.get("/api/memory/health").json()
            assert j["fts5"]["fallback_24h"] >= 1, j
            assert j["fts5"]["ok"] is False, j
            assert j["status"] == "degraded", j
            assert j["degraded_reason"] == REASON, j
        finally:
            vs.close()


def test_no_event_no_degrade():
    """无事件 → fts5.ok=True、status 不降（防误伤）"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "g.db")
        client, vs = _mk_client(db)
        try:
            j = client.get("/api/memory/health").json()
            assert j["fts5"]["fallback_24h"] == 0, j
            assert j["fts5"]["ok"] is True, j
            assert j["status"] == "ok", j
        finally:
            vs.close()


def test_old_event_outside_24h_window_ignored():
    """24h 窗外旧事件不计数（_win 口径）"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "g.db")
        client, vs = _mk_client(db)  # 先建库后插事件
        try:
            _put_fts5_event(db, time.time() - 90000)  # >24h
            j = client.get("/api/memory/health").json()
            assert j["fts5"]["fallback_24h"] == 0, j
            assert j["status"] == "ok", j
        finally:
            vs.close()


def test_stale_red_takes_precedence_over_fts5_yellow():
    """红黄不叠加：stale（红）在场时 fts5 事件【不】改写 status（红盖黄，probe 段口径）"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "g.db")
        client, vs = _mk_client(db)  # 先建库后插事件/改 meta
        try:
            _put_fts5_event(db, time.time())
            con = sqlite3.connect(db)
            try:
                # 假旧码条目须用【活 pid】才能标红（09-05 误报面收口：死 pid 不标红）
                con.execute(
                    "UPDATE xtmem_meta SET value=? WHERE key='runtime_actors'",
                    (json.dumps({str(os.getpid()): {"actor": "old", "code_sha": "dead", "ts": 1.0}}),),
                )
                con.commit()
            finally:
                con.close()
            j = client.get("/api/memory/health").json()
            assert j["status"] == "stale", j
            assert j["fts5"]["fallback_24h"] >= 1, j  # fts5 口径照报，但【不】夺 status
        finally:
            vs.close()


def test_fts5_block_synced_across_five_trees():
    """五树静态同步：联动段标记串全在场（防镜像分叉；换靶跑红时【不】跳过——静态检查恒打五真身）"""
    for f in FIVE_TREES:
        src = open(f, encoding="utf-8").read()
        assert MARKER in src, f"{f} 缺联动段标记（镜像分叉）"
        assert REASON in src, f"{f} 缺 degraded_reason 字面量（镜像分叉）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
