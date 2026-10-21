# -*- coding: utf-8 -*-
"""test_xtmem_v17_edge.py — V1.7 api_server 钉边守卫（v1.3.2 工单批，2026-09-05，assistant-c）。

两件：包体上限 1MB → 413；per-IP 滑动窗 120 次/分 → 429。鉴权半条按工单不做。
环境纪律：tmp_db 副本，不碰生产。
"""
import os
import sys as _sys

import pytest

from conftest import PLUGIN_DIR

_sys.path.insert(0, str(PLUGIN_DIR))

from memory.open_db import open_db
import memory.api_server as ap
from memory.vector_store import VectorStore


@pytest.fixture()
def _client(tmp_db):
    os.environ["XTMEM_ACTOR"] = "v17-test"
    os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    # 2026-09-06 token 重签后：auth 文件非空 → 请求级鉴权强制生效，env noauth 只管启动闸。
    # 测试必须走 create_app(allow_noauth=True) 显式豁免。
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        vs = VectorStore(db_path=str(tmp_db))
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
        from fastapi.testclient import TestClient
        yield TestClient(app)
        vs.close()
    finally:
        con.close()
        os.environ.pop("XTMEM_ACTOR", None)
        os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)


def test_v17_small_request_passes(_client):
    r = _client.get("/api/memory/health")
    assert r.status_code == 200


def test_v17_body_limit_413(_client):
    big = {"text": "x" * (1024 * 1024 + 100)}
    r = _client.post("/api/rooms", json=big)
    assert r.status_code == 413
    assert r.json()["code"] == "body_too_large"


def test_v17_rate_limit_429(_client):
    hit_429 = False
    for _ in range(130):
        if _client.get("/api/memory/health").status_code == 429:
            hit_429 = True
            break
    assert hit_429, "rate limit never triggered"


def test_v17_non_loopback_binding_rejected(tmp_db):
    """V1.7 钉边：bind 127.0.0.1 显式钉死，任何非回环绑定一律拒绝。"""
    os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    vs = VectorStore(db_path=str(tmp_db))
    try:
        refused = False
        try:
            ap.create_app(vector_store=vs, host="0.0.0.0")
        except RuntimeError as e:
            assert "refuses non-loopback" in str(e)
            refused = True
        assert refused, "非回环 host=0.0.0.0 必须被拒"

        refused_ext = False
        try:
            ap.create_app(vector_store=vs, host="192.168.1.100")
        except RuntimeError as e:
            assert "refuses non-loopback" in str(e)
            refused_ext = True
        assert refused_ext, "外网 host=192.168.1.100 必须被拒"
    finally:
        vs.close()
        os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)


def test_v17_non_loopback_client_rejected(tmp_db):
    """V1.7 钉边验穿：从非回环地址连接必须被拒（403 forbidden_non_loopback）。"""
    os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    vs = VectorStore(db_path=str(tmp_db))
    try:
        # 本测试验 edge-guard（非回环 403 / 回环放行），不是鉴权——allow_noauth=True 摘掉鉴权干扰
        app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
        from fastapi.testclient import TestClient
        client = TestClient(app, client=("192.168.1.200", 54321))
        r = client.get("/api/memory/health")
        assert r.status_code == 403
        assert r.json()["code"] == "forbidden_non_loopback"

        # 回环 IP 允许通过
        client_loop = TestClient(app, client=("127.0.0.1", 54321))
        r_ok = client_loop.get("/api/memory/health")
        assert r_ok.status_code == 200
    finally:
        vs.close()
        os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)