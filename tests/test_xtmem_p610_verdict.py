# -*- coding: utf-8 -*-
"""test_xtmem_p610_verdict.py — P6.10 verdict 收口守卫（v2.1.2 波2，2026-09-05，assistant-c）。

三面：verdict_of 共享函数映射 / health verdict 分布 / CLI-provider 口径一致（同函数）。
环境纪律：tmp_db 副本，不碰生产。
"""
import json
import os
import sqlite3
import sys as _sys
import time

import pytest

from conftest import PLUGIN_DIR

_sys.path.insert(0, str(PLUGIN_DIR))

from provider import verdict_of
from memory.open_db import open_db


def test_verdict_map():
    assert verdict_of("refined-first", True) == "高置信（refined 首跳命中）"
    assert verdict_of("raw-fallback", True) == "兜底（raw 回落）"
    assert verdict_of("unknown", False) == "空结果"
    assert verdict_of("unknown", True) == "未知"


def test_health_verdict_dist(tmp_db, monkeypatch):
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    monkeypatch.setenv("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", "1")
    con = open_db(str(tmp_db))
    try:
        for v in ("高置信（refined 首跳命中）", "高置信（refined 首跳命中）", "兜底（raw 回落）"):
            con.execute(
                "INSERT INTO xtmem_events (ts, kind, agent, namespace, meta) VALUES (?, 'recall', 'dsh', 'dsh', ?)",
                (time.time(), json.dumps({"verdict": v}, ensure_ascii=False)),
            )
        con.commit()
        os.environ["XTMEM_ACTOR"] = "t"
        import memory.api_server as ap
        from memory.vector_store import VectorStore
        vs = VectorStore(db_path=str(tmp_db))
        # 2026-09-06 token 重签后请求级鉴权强制生效——测试显式豁免（allow_noauth 为测试注入口）
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
        from fastapi.testclient import TestClient
        j = TestClient(app).get("/api/memory/health").json()
        dist = j.get("verdict_dist_24h")
        assert dist and dist.get("高置信（refined 首跳命中）") == 2
        assert dist.get("兜底（raw 回落）") == 1
        vs.close()
    finally:
        con.close()