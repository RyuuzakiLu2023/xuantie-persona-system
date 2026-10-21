# -*- coding: utf-8 -*-
"""test_xtmem_noauth_strip.py — NOAUTH 摘牌守卫（REST 鉴权半条收口，2026-09-06，assistant-c）。

守卫清单（maintainer 工单三件-③）：
  ① test_no_token_rejected_failclosed：无 env 无 token 请求 → 401（fail-closed 实弹）；
  ② test_real_token_accepted：真 token（auth 文件内存读取，不回显明文）→ 200；
  ③ test_forged_token_rejected：伪造 token → 401；
  ④ test_keepalive_no_noauth_env：keepalive 源码不再设 NOAUTH env（摘牌持久性——防回填）。
⚠️ R7：临时库 + 随机高端口（uvicorn 不监听的测试用 TestClient 亦可）——【禁】写生产。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import sys as _sys

import pytest

from conftest import PLUGIN_DIR

_sys.path.insert(0, str(PLUGIN_DIR))

from memory.open_db import open_db
import memory.api_server as ap
from memory.vector_store import VectorStore

REAL_AUTH = os.path.join(os.path.dirname(PLUGIN_DIR), "xtmem_rest_auth.json")
# PLUGIN_DIR=.../plugins/xtmem → auth 文件在其上级 mimox-data/ 下
REAL_AUTH = _REPO / "xtmem_rest_auth.json"


def _setup_app(tmp_db, monkeypatch, auth_env=None):
    """临时库 app：auth 环境按用例注入。"""
    monkeypatch.setenv("XTMEM_ACTOR", "t")
    monkeypatch.delenv("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", raising=False)
    monkeypatch.delenv("XTMEM_REST_TOKENS", raising=False)
    monkeypatch.delenv("XTMEM_REST_TOKEN", raising=False)
    con = open_db(str(tmp_db))
    con.execute("SELECT 1")
    vs = VectorStore(db_path=str(tmp_db))
    kw = {"vector_store": vs, "host": "127.0.0.1", "allow_noauth": None}
    if auth_env:
        for k, v in auth_env.items():
            monkeypatch.setenv(k, v)
    app = ap.create_app(**kw)
    from fastapi.testclient import TestClient
    return TestClient(app), vs, con


def test_no_token_rejected_failclosed(tmp_db, monkeypatch):
    """无 env 无 token：auth 文件指向不存在路径（=无 token 配置）→ 无 noauth env → 启动即拒。"""
    monkeypatch.setenv("XTMEM_REST_AUTH_FILE", str(tmp_db.parent / "no-auth-here.json"))
    with pytest.raises(RuntimeError) as ei:
        _setup_app(tmp_db, monkeypatch)
    assert "拒绝无 token 启动" in str(ei.value) or "fail" in str(ei.value).lower()


def test_real_token_accepted(tmp_db, monkeypatch):
    """真 token（auth 文件真配置）→ 200；token 明文不落断言消息。"""
    assert os.path.exists(REAL_AUTH), "生产 auth 文件应存在（dsh/zcode 已签）"
    auth = json.load(open(REAL_AUTH, encoding="utf-8"))
    tokens = auth if isinstance(auth, dict) else auth.get("tokens", {})
    assert tokens, "auth 文件应有 token"
    # 取任一真 token（内存用，不打印）
    real_tok = next(iter(tokens))
    client, vs, con = _setup_app(tmp_db, monkeypatch,
                                 {"XTMEM_REST_AUTH_FILE": REAL_AUTH})
    try:
        r = client.get("/api/memory/health", headers={"X-XTMEM-Token": real_tok})
        assert r.status_code == 200, "真 token 应通（status=%d）" % r.status_code
    finally:
        vs.close()
        con.close()


def test_forged_token_rejected(tmp_db, monkeypatch):
    """伪造 token → 401。"""
    client, vs, con = _setup_app(tmp_db, monkeypatch,
                                 {"XTMEM_REST_AUTH_FILE": REAL_AUTH})
    try:
        r = client.get("/api/memory/health", headers={"X-XTMEM-Token": "forged-" + "x" * 32})
        assert r.status_code == 401, r.status_code
        # 无头也拒
        r2 = client.get("/api/memory/health")
        assert r2.status_code == 401, r2.status_code
    finally:
        vs.close()
        con.close()


def test_keepalive_no_noauth_env():
    """keepalive 源码不再设 NOAUTH env（摘牌持久性守卫——防回填）。"""
    src = open(_REPO / "scripts"/ "xtmem_api_keepalive.py", encoding="utf-8").read()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue  # 注释允许提名字（带日期原因）
        assert "XTMEM_REST_ALLOW_LOOPBACK_NOAUTH" not in s, \
            "keepalive 代码行不得再设 NOAUTH env: " + s[:80]
