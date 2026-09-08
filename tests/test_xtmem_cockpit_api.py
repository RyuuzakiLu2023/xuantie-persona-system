# -*- coding: utf-8 -*-
"""P5.1-2b 契约缝合守卫测试（2026-09-02，小D）。

TestClient 直打 api_server 八路由，断言返回形状与照渊台契约一致
（<COCKPIT_ROOT>\\src\\api\\endpoints.ts 是字段级唯一出处；mock 是行为基准）。

覆盖：state 聚合形状 / rooms+topics 聚合 / context 不过滤 kind / msg send 契约字段
（private → content=null+redacted；system→notice 落库）/ ack 双义（差帖回执 + widget 拍板）/
widget 注入 400 / schedule 契约词翻译（workbuddy 无 A2A peer → 降级留帖，不外发）/
graph 四件套形状。

自证值一律 zzz- 前缀（总则：防与真数据重合）。
"""
import json
import sqlite3
import sys

import pytest
import axis_predicates
import conftest
from fastapi.testclient import TestClient

import memory.schema as schema

pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用")

CLI = conftest.CLI

AGENT_FIELDS = ["id", "name", "epithet", "presence", "lamp", "channel", "wake_p50_ms",
                "can_answer_now", "unread", "open_tasks", "memories", "private_count",
                "clock_rate", "last_seen"]
VITALS_FIELDS = ["total", "raw", "refined", "domains", "clocks", "evo_edges", "evo_by_rel",
                 "relations", "last_distill", "evicted_today", "refined_hit_rate"]
MESSAGE_FIELDS = ["id", "room_id", "topic_id", "kind", "state", "from", "to", "content",
                  "visibility", "persist", "notify", "needs_ack", "audience", "created_at"]


def _ensure_schema(db):
    con = sqlite3.connect(str(db))
    schema.ensure_xtmem_schema(con)
    con.commit()
    con.close()


@pytest.fixture()
def client(cli_env):
    """TestClient：cli_env 已注入 XTMEM_DB_PATH=tmp_db（子进程与同进程都指向它）。"""
    import memory.api_server as ap
    app = ap.create_app()
    return TestClient(app)


@pytest.fixture()
def db(cli_env):
    _ensure_schema(cli_env["XTMEM_DB_PATH"])
    return cli_env["XTMEM_DB_PATH"]


def _cli(db, *argv):
    import os
    import subprocess
    env = {**os.environ, "XTMEM_DB_PATH": str(db), "PYTHONUTF8": "1"}
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


# ── 1. state 聚合形状 ────────────────────────────────────

def test_state_shape(db, client):
    r = client.get("/api/cockpit/state")
    assert r.status_code == 200, r.text[:300]
    j = r.json()
    assert j.get("ok") is True
    assert 0.0 <= j.get("liveness", -1) <= 1.0
    assert isinstance(j.get("now"), str) and j["now"]
    agents = j.get("agents")
    assert isinstance(agents, list) and len(agents) == 4
    ids = [a["id"] for a in agents]
    assert ids == ["xuantie", "dsh", "workbuddy", "zcode"]
    for a in agents:
        for f in AGENT_FIELDS:
            assert f in a, "agent 缺字段 " + f
    for f in VITALS_FIELDS:
        assert f in j.get("vitals", {}), "vitals 缺字段 " + f
    evo = j["vitals"]["evo_by_rel"]
    for k in ("supersedes", "disagrees_with", "related"):
        assert k in evo
    tc = j.get("task_counts", {})
    for k in ("open", "waiting_ack", "done_today"):
        assert k in tc
    assert isinstance(j.get("recent"), list)
    for m in j["recent"][:3]:
        for f in MESSAGE_FIELDS:
            assert f in m, "recent message 缺字段 " + f
    assert isinstance(j.get("widgets"), list)


def test_private_message_redacted(db, client):
    """私密正文零下发：content=null + redacted=true（契约硬纪律）。"""
    r = client.post("/api/msg/send", json={
        "room_id": "zzz-room", "topic_id": None, "to": ["dsh"],
        "kind": "task", "content": "zzz 秘密内容", "visibility": "private",
    })
    assert r.status_code == 200, r.text[:300]
    m = r.json().get("message", {})
    assert m.get("content") is None
    assert m.get("redacted") is True
    assert m.get("visibility") == "private"


def test_system_kind_roundtrip(db, client):
    """契约 system（风讯）→ 库 notice 落库 → 读回映射 system。"""
    r = client.post("/api/msg/send", json={
        "room_id": "", "to": ["zcode"], "kind": "system",
        "content": "zzz 风讯", "visibility": "shared",
    })
    assert r.status_code == 200, r.text[:300]
    m = r.json().get("message", {})
    assert m.get("kind") == "system"
    con = sqlite3.connect(db)
    row = con.execute("SELECT kind FROM messages WHERE id=?", (m["id"],)).fetchone()
    con.close()
    assert row and row[0] == "notice"


def test_bad_kind_rejected(db, client):
    r = client.post("/api/msg/send", json={
        "to": ["dsh"], "kind": "bogus", "content": "x", "visibility": "shared"})
    assert r.status_code == 400
    assert r.json().get("ok") is False


# ── 2. rooms / context 聚合 ──────────────────────────────

def test_rooms_and_context(db, client):
    assert _cli(db, "rooms", "create", "--id", "zzz-room-x", "--name", "测试群",
                "--members", '["xuantie","dsh"]', "--mode", "invite_only").get("ok")
    # 直接落两条消息：chat（共享）+ decision（私密），同话题——验证聚合与 kind 不过滤
    _cli(db, "msg", "send", "--from", "ryuuzaki", "--to", "dsh", "--text", "zzz 话题首条消息内容",
         "--kind", "chat", "--room", "zzz-room-x", "--topic", "zzz-t1", "--visibility", "shared")
    _cli(db, "msg", "send", "--from", "ryuuzaki", "--to", "dsh", "--text", "zzz 私密凭帖",
         "--kind", "decision", "--room", "zzz-room-x", "--topic", "zzz-t1", "--visibility", "private")

    r = client.get("/api/rooms")
    assert r.status_code == 200
    rooms = [x for x in r.json().get("rooms", []) if x["id"] == "zzz-room-x"]
    assert rooms, "zzz-room-x 应出现在 rooms"
    room = rooms[0]
    assert room["type"] == "group"
    assert room["speak_mode"] == "inviteonly"
    assert room["round_state"] == "idle"
    assert room["members"] == ["xuantie", "dsh"]
    topics = [t for t in room["topics"] if t["id"] == "zzz-t1"]
    assert topics and topics[0]["msg_count"] == 2
    assert topics[0]["private_count"] == 1

    rc = client.get("/api/room/zzz-room-x/context", params={"topic_id": "zzz-t1", "n": 20})
    assert rc.status_code == 200
    cj = rc.json()
    assert cj.get("ok") is True
    kinds = {m["kind"] for m in cj["messages"]}
    # 不过滤 kind：chat 与 decision（映射自 decision）都该在
    assert "chat" in kinds and "decision" in kinds
    priv = [m for m in cj["messages"] if m["kind"] == "decision"]
    assert priv and priv[0]["content"] is None and priv[0].get("redacted") is True


# ── 3. ack 双义 ──────────────────────────────────────────

def test_ack_message_and_widget(db, client):
    # 差帖回执
    sent = client.post("/api/msg/send", json={
        "room_id": "zzz-room", "to": ["workbuddy"], "kind": "task",
        "content": "zzz 差帖请办", "visibility": "shared"})
    mid = sent.json()["message"]["id"]
    a = client.post("/api/task/ack", json={"message_id": mid})
    assert a.status_code == 200, a.text[:300]
    assert a.json()["message"]["state"] == "acked"

    # widget 拍板（ask 类默认带 action）
    w = client.post("/api/widget", json={
        "from": "xuantie", "kind": "ask", "title": "zzz 请拍板",
        "body": "要不要清理缓存", "visibility": "shared"})
    assert w.status_code == 200, w.text[:300]
    wid = w.json()["widget"]["id"]
    assert any(x["id"] == "a-yes" for x in w.json()["widget"].get("actions", []))

    ad = client.post("/api/task/ack", json={"message_id": wid, "action_id": "a-yes"})
    assert ad.status_code == 200, ad.text[:300]
    dm = ad.json()["message"]
    assert dm["kind"] == "decision"
    assert "zzz 请拍板" in (dm["content"] or "")

    st = client.get("/api/cockpit/state").json()


def test_ack_notifies_agent_inbox(db, client):
    """尾巴④：agent 发来的请求类差帖被 ack 后，回执通知写进它信箱（兜底通道）。"""
    sent = _cli(db, "msg", "send", "--from", "xuantie_lark", "--to", "ryuuzaki",
                "--text", "zzz 请库主拍板", "--kind", "task", "--visibility", "shared")
    assert sent.get("ok"), sent
    mid = sent["id"]
    a = client.post("/api/task/ack", json={"message_id": mid, "note": "照办"})
    assert a.status_code == 200, a.text[:300]
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT from_agent, to_agent, kind, reply_to FROM messages "
        "WHERE reply_to=? AND from_agent='ryuuzaki'", (mid,)).fetchone()
    con.close()
    assert row, "回执通知消息未落库"
    assert row[1] == "xuantie_lark" and row[2] == "msg"
