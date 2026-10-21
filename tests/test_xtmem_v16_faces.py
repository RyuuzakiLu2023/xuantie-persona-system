# -*- coding: utf-8 -*-
"""test_xtmem_v16_faces.py — V1.6 图从码生守卫（v1.3.2 工单批，2026-09-05，assistant-c）。

三面：graph_stats 带 jobs/schema/connect（实时读真值面，变更必变图）；
runtime_actors 收敛（每 (actor, code_sha) 最新 3 条，CLI 高频调用不膨胀）。
环境纪律：tmp_db 副本 + XTMEM_DB_PATH 显式注入（conftest 教训），不碰生产。
"""
import json
import sqlite3
import sys as _sys
import time

import pytest

from conftest import PLUGIN_DIR

_sys.path.insert(0, str(PLUGIN_DIR))

from memory.graph import graph_stats
from memory.open_db import open_db, _report_runtime_actor


@pytest.fixture()
def _env_db(tmp_db, monkeypatch):
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    return tmp_db


def _ensure_min_schema(db):
    """faces/graph_stats 主查询需要的最小表：chunk_entities/tag_edges 不在则建空表。"""
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS chunk_entities (chunk_id TEXT, canon TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS tag_edges (a TEXT, b TEXT, cnt INTEGER, updated_at REAL)")
        con.commit()
    finally:
        con.close()


def test_v16_stats_has_three_faces(_env_db):
    _ensure_min_schema(_env_db)
    out = graph_stats(str(_env_db))
    assert out["ok"] is True
    for face in ("jobs", "schema", "connect"):
        assert face in out, f"missing face {face}"
        assert isinstance(out[face], dict)
    assert "user_version" in out["schema"]


def test_v16_faces_reflect_changes(_env_db):
    """变更必变图：schema 面 user_version 实时（人为升版 → stats 反映）。"""
    _ensure_min_schema(_env_db)
    out1 = graph_stats(str(_env_db))
    con = sqlite3.connect(str(_env_db))
    try:
        con.execute("PRAGMA user_version = 9")
        con.commit()
    finally:
        con.close()
    out2 = graph_stats(str(_env_db))
    assert out2["schema"]["user_version"] == 9
    assert out1["schema"]["user_version"] != 9


def test_v16_actor_convergence(_env_db):
    """收敛：>24 条时每 (actor, code_sha) 只留最新 3 条。"""
    con = open_db(str(_env_db))
    try:
        data = {}
        base = time.time() - 7200
        for i in range(30):
            data[str(10000 + i)] = {"actor": "g", "code_sha": "a" * 64, "ts": base + i}
        con.execute(
            "INSERT INTO xtmem_meta (key, value) VALUES ('runtime_actors', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(data),),
        )
        con.commit()
        import memory.open_db as _odb
        _odb._RUNTIME_REPORTED_PIDS.clear()  # 绕进程级去重（生产语义：每 CLI 新进程天然绕过）
        _report_runtime_actor(con, str(_env_db))
        row = con.execute("SELECT value FROM xtmem_meta WHERE key='runtime_actors'").fetchone()
        after = json.loads(row[0])
        assert len(after) <= 4, f"converge failed: {len(after)}"
    finally:
        con.close()