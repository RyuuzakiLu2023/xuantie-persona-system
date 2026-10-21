# -*- coding: utf-8 -*-
"""R-02 / R-03 验收回归（2026-09-03，assistant-c）。

覆盖：
- 角色 token 六身份矩阵：admin/4 角色/匿名 × shared/private/shared_with 的
  /api/cockpit/state recent 与 /api/room/{id}/context 内容可见性（shared_with 越权读已修）；
- /api/msg/send 的 from == token viewer（角色视角发信，防请求体伪造）；
- CLI：XTMEM_ACTOR=dsh 时 --from zcode → 拒绝；
- rest-token issue 自助签发门禁（只能签自己 / 管理员可签 admin）；
- 无 token 且无 NOAUTH 调试 flag → create_app 拒绝启动。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import axis_predicates
import conftest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用")

CLI = conftest.CLI

TOK = {
    "tok-admin": {"viewer": "admin", "admin": True},
    "tok-dsh": {"viewer": "dsh", "admin": False},
    "tok-x": {"viewer": "personal", "admin": False},
    "tok-wb": {"viewer": "workbuddy", "admin": False},
    "tok-z": {"viewer": "zcode", "admin": False},
}


@pytest.fixture()
def acl_env(tmp_db):
    prior = {k: os.environ.get(k) for k in ("XTMEM_DB_PATH", "XTMEM_REST_TOKENS",
                                            "XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", "XTMEM_ACTOR", "XTMEM_ADMIN")}
    os.environ["XTMEM_DB_PATH"] = str(tmp_db)
    os.environ["XTMEM_REST_TOKENS"] = json.dumps(TOK)
    os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)
    os.environ.pop("XTMEM_ACTOR", None)
    os.environ.pop("XTMEM_ADMIN", None)
    import memory.schema as schema
    con = sqlite3.connect(str(tmp_db))
    schema.ensure_xtmem_schema(con)
    con.commit()
    con.close()
    try:
        yield tmp_db
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seed_msg(db, from_agent, to_agent, content, vis="shared", sw=None, room="zzz-r1", topic="t-1", kind="msg"):
    con = sqlite3.connect(str(db))
    mid = "msg-r02-" + content.encode("utf-8").hex()[:8]
    con.execute(
        "INSERT OR IGNORE INTO rooms (id, name, members, mode, current_round, pending_speakers, ts)"
        " VALUES (?,?,?,?,?,?,?)",
        (room, room, "[]", "invite_only", 0, "[]", 1999999999.0),
    )
    con.execute(
        "INSERT OR REPLACE INTO messages"
        " (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind, state, visibility, shared_with)"
        " VALUES (?,?,?,?,?,0,?,?,?, '', ?, ?)",
        (mid, from_agent, to_agent, content, 1999999999.0, room, topic, kind,
         vis, json.dumps(sw or [], ensure_ascii=False)),
    )
    con.commit()
    con.close()
    return mid


def _app_client():
    import memory.api_server as ap
    return TestClient(ap.create_app())


def test_r02_role_matrix_recent_context(acl_env, tmp_db):
    _seed_msg(tmp_db, "dsh", "personal", "公开-大家好", "shared", room="zzz-r1")
    _seed_msg(tmp_db, "dsh", "workbuddy", "私密-给wb", "shared_with", sw=["workbuddy"], room="zzz-r1")
    _seed_msg(tmp_db, "dsh", "dsh", "私有-给自己", "private", room="zzz-r1")
    _seed_msg(tmp_db, "workbuddy", "dsh", "wb公开", "shared", room="zzz-r1")

    c = _app_client()
    assert c.get("/api/cockpit/state").status_code == 401
    r_admin = c.get("/api/cockpit/state", headers={"X-XTMEM-Token": "tok-admin"}).json()
    texts_admin = [m["content"] for m in r_admin["recent"]]
    assert "公开-大家好" in texts_admin and "wb公开" in texts_admin
    # R-02 v2：admin 必须看到 private 正文（不再是 content=null + redacted）
    assert "私有-给自己" in texts_admin, texts_admin
    r_dsh = c.get("/api/cockpit/state", headers={"X-XTMEM-Token": "tok-dsh"}).json()
    contents_dsh = [m["content"] for m in r_dsh["recent"]]
    assert "公开-大家好" in contents_dsh
    assert "私密-给wb" in contents_dsh
    assert "wb公开" in contents_dsh
    r_wb = c.get("/api/cockpit/state", headers={"X-XTMEM-Token": "tok-wb"}).json()
    contents_wb = [m["content"] for m in r_wb["recent"]]
    assert "私密-给wb" in contents_wb
    assert "公开-大家好" in contents_wb
    assert all(("私有-给自己" not in str(m.get("content"))) for m in r_wb["recent"])
    ctx_dsh = c.get("/api/room/zzz-r1/context", headers={"X-XTMEM-Token": "tok-dsh"},
                    params={"topic_id": "t-1", "n": 50}).json()
    assert "私密-给wb" in [m["content"] for m in ctx_dsh["messages"]]
    ctx_x = c.get("/api/room/zzz-r1/context", headers={"X-XTMEM-Token": "tok-x"},
                  params={"topic_id": "t-1", "n": 50}).json()
    for m in ctx_x["messages"]:
        assert "私密-给wb" not in m.get("content", "") and "私有-给自己" not in m.get("content", "")


def test_r02_msg_send_from_must_equal_viewer(acl_env, tmp_db):
    c = _app_client()
    ok = c.post("/api/msg/send", headers={"X-XTMEM-Token": "tok-dsh"}, json={
        "room_id": "zzz-r1", "to": ["workbuddy"], "kind": "msg",
        "content": "dsh视角发信", "visibility": "shared"})
    assert ok.status_code == 200, ok.text[:200]
    con = sqlite3.connect(str(tmp_db))
    row = con.execute("SELECT from_agent FROM messages WHERE id=?", (ok.json()["message"]["id"],)).fetchone()
    con.close()
    assert row and row[0] == "dsh", row
    w_ok = c.post("/api/widget", headers={"X-XTMEM-Token": "tok-dsh"},
                  json={"from": "dsh", "kind": "note", "title": "zzz-w", "body": "b", "visibility": "shared"})
    assert w_ok.status_code == 200, w_ok.text[:200]
    w_bad = c.post("/api/widget", headers={"X-XTMEM-Token": "tok-dsh"},
                   json={"from": "xtmem", "kind": "note", "title": "zzz-w2", "body": "b", "visibility": "shared"})
    assert w_bad.status_code == 403


def test_r03_cli_msg_from_forgery_rejected(acl_env, tmp_db):
    env = {**os.environ, "XTMEM_DB_PATH": str(tmp_db), "PYTHONUTF8": "1",
           "XTMEM_ACTOR": "dsh"}
    p = subprocess.run([sys.executable, str(CLI), "msg", "send", "--from", "zcode",
                        "--to", "dsh", "--text", "zzz 伪造"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=env)
    out = json.loads(p.stdout.strip() or "{}")
    assert out.get("ok") is False and "伪造" in out.get("error", ""), out


def test_r02_rest_token_issue_gates(tmp_path):
    auth_file = tmp_path / "xtmem_rest_auth.json"

    def run(argv, actor=None, admin=None):
        env = {**os.environ, "XTMEM_REST_AUTH_FILE": str(auth_file), "PYTHONUTF8": "1"}
        env.pop("XTMEM_ACTOR", None)
        env.pop("XTMEM_ADMIN", None)
        env.pop("XTMEM_ADMIN_TOKEN", None)
        if actor:
            env["XTMEM_ACTOR"] = actor
        if admin is not None:
            env["XTMEM_ADMIN"] = admin
        p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env=env)
        try:
            return json.loads(p.stdout.strip() or "{}")
        except Exception:
            return {"ok": False, "error": (p.stdout or p.stderr or "")[:200]}

    assert run(["rest-token", "issue", "--role", "admin"]).get("ok") is False
    r = run(["rest-token", "issue", "--role", "dsh"], actor="dsh")
    assert r.get("ok") is True and r.get("token"), r
    r2 = run(["rest-token", "issue", "--role", "workbuddy"], actor="dsh")
    assert r2.get("ok") is False, "角色只能签自己"
    r3 = run(["rest-token", "issue", "--role", "admin"], admin="1")
    assert r3.get("ok") is True, r3
    store = json.loads(auth_file.read_text(encoding="utf-8"))
    assert store[r["token"]]["viewer"] == "dsh" and store[r3["token"]]["viewer"] == "admin"


def test_r02_noauth_startup_refused(tmp_db):
    import memory.api_server as ap
    prior = {k: os.environ.get(k) for k in ("XTMEM_REST_TOKENS", "XTMEM_REST_ALLOW_LOOPBACK_NOAUTH",
                                            "XTMEM_REST_AUTH_FILE")}
    os.environ.pop("XTMEM_REST_TOKENS", None)
    os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)
    os.environ["XTMEM_REST_AUTH_FILE"] = str(Path(tmp_db).parent / "no-such-auth.json")
    try:
        refused = False
        try:
            ap.create_app(host="127.0.0.1")
        except RuntimeError:
            refused = True
        assert refused, "无 token 且无 NOAUTH 应拒绝启动"
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
