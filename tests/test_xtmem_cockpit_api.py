# -*- coding: utf-8 -*-
"""P5.1-2b 契约缝合守卫测试（2026-09-02，assistant-c）。

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


@pytest.fixture(autouse=True)
def _block_real_a2a_outbound(monkeypatch):
    """测试隔离（2026-09-03）：拦掉 A2A 网络出口 _post。

    P5.1-2c 的 msg_send/schedule 会在 worker 线程真实外发到 9900/9901 —— 曾把用例文本
    'zzz 秘密内容' 刷进assistant-c 会话（探针事故）。拦在 _post 层：resolve_peer/unknown-peer
    抛错语义原样保留（fallback 用例不受影响）；要断言外发行为的用例自行 monkeypatch a2a_send。
    """
    import memory.a2a_client as a2a_mod

    def _fake_post(base, method, message, context_id=""):
        ctx = context_id or "ctx-test-noop"
        return {"jsonrpc": "2.0", "id": "task-test-noop",
                "result": {"message": {"role": "agent",
                          "parts": [{"text": "[pending] test-isolation: no real A2A send", "mediaType": "text/plain"}],
                          "messageId": "test-msg", "contextId": ctx}}}

    monkeypatch.setattr(a2a_mod, "_post", _fake_post)


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
    """TestClient：cli_env 已注入 XTMEM_DB_PATH=tmp_db（子进程与同进程都指向它）。

    R-02（2026-09-03）：无角色 token 时 create_app 拒绝启动，测试统一走
    XTMEM_REST_ALLOW_LOOPBACK_NOAUTH=1 调试模式（viewer=__admin__）。
    """
    import os as _os
    from pathlib import Path as _Path
    import memory.api_server as ap
    # 真库旁已有 xtmem_rest_auth.json（生产 token），必须把读取路径指到不存在的
    # 临时文件，否则所有无头请求都会按 401 处理。NOAUTH flag 仅供本次 TestClient。
    _prior_auth_file = _os.environ.get("XTMEM_REST_AUTH_FILE")
    _os.environ["XTMEM_REST_AUTH_FILE"] = str(_Path(cli_env["XTMEM_DB_PATH"]).parent / "client-no-auth.json")
    _os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    try:
        app = ap.create_app(allow_noauth=True)
        yield TestClient(app)
    finally:
        _os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)
        if _prior_auth_file is None:
            _os.environ.pop("XTMEM_REST_AUTH_FILE", None)
        else:
            _os.environ["XTMEM_REST_AUTH_FILE"] = _prior_auth_file


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
    assert ids == ["xtmem", "dsh", "workbuddy", "zcode"]
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
    """私密正文边界（R-02 v2）：管理员(noauth=__admin__)可见 private 正文；
    普通角色 token 看不到他人 private 行/正文（越权封口在 r02r03 矩阵覆盖）。"""
    r = client.post("/api/msg/send", json={
        "room_id": "zzz-room", "topic_id": None, "to": ["dsh"],
        "kind": "task", "content": "zzz 秘密内容", "visibility": "private",
    })
    assert r.status_code == 200, r.text[:300]
    m = r.json().get("message", {})
    assert m.get("content") == "zzz 秘密内容", m     # admin 可见正文
    assert "redacted" not in m
    assert m.get("visibility") == "private"
    # 角色视角（dsh token）：他人 private 消息行不出现
    import json as _json
    import os as _os
    prior = _os.environ.get("XTMEM_REST_TOKENS")
    _os.environ["XTMEM_REST_TOKENS"] = _json.dumps({"tok-dsh": {"viewer": "dsh", "admin": False}})
    try:
        st = client.get("/api/cockpit/state", headers={"X-XTMEM-Token": "tok-dsh"}).json()
        leaked = [x for x in st.get("recent", []) if x.get("content") == "zzz 秘密内容"]
        assert not leaked, "普通角色不应看到他人 private 正文/行"
    finally:
        if prior is None:
            _os.environ.pop("XTMEM_REST_TOKENS", None)
        else:
            _os.environ["XTMEM_REST_TOKENS"] = prior


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
                "--members", '["xtmem","dsh"]', "--mode", "invite_only").get("ok")
    # 直接落两条消息：chat（共享）+ decision（私密），同话题——验证聚合与 kind 不过滤
    _cli(db, "msg", "send", "--from", "local", "--to", "dsh", "--text", "zzz 话题首条消息内容",
         "--kind", "chat", "--room", "zzz-room-x", "--topic", "zzz-t1", "--visibility", "shared")
    _cli(db, "msg", "send", "--from", "local", "--to", "dsh", "--text", "zzz 私密凭帖",
         "--kind", "decision", "--room", "zzz-room-x", "--topic", "zzz-t1", "--visibility", "private")

    r = client.get("/api/rooms")
    assert r.status_code == 200
    rooms = [x for x in r.json().get("rooms", []) if x["id"] == "zzz-room-x"]
    assert rooms, "zzz-room-x 应出现在 rooms"
    room = rooms[0]
    assert room["type"] == "group"
    assert room["speak_mode"] == "inviteonly"
    assert room["round_state"] == "idle"
    assert room["members"] == ["xtmem", "dsh"]
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
    assert priv and priv[0]["content"] == "zzz 私密凭帖", priv   # R-02 v2：admin 可见正文
    assert "redacted" not in priv[0]


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
        "from": "xtmem", "kind": "ask", "title": "zzz 请拍板",
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
    sent = _cli(db, "msg", "send", "--from", "personal", "--to", "local",
                "--text", "zzz 请库主拍板", "--kind", "task", "--visibility", "shared")
    assert sent.get("ok"), sent
    mid = sent["id"]
    a = client.post("/api/task/ack", json={"message_id": mid, "note": "照办"})
    assert a.status_code == 200, a.text[:300]
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT from_agent, to_agent, kind, reply_to FROM messages "
        "WHERE reply_to=? AND from_agent='local'", (mid,)).fetchone()
    con.close()
    assert row, "回执通知消息未落库"
    assert row[1] == "personal" and row[2] == "msg"


# ── 7. createRoom 新建房间（POST /api/rooms）──────────────

def test_create_room_dm_idempotent(db, client):
    """新建对席房：id=dm:{to} + 幂等（同 id 复用）+ 空话题兜底一条。"""
    # 测试隔离：db fixture 是 canonical 快照副本，里面可能已有 dm:xtmem 及历史消息，
    # 幂等断言 topics[0].msg_count == 0 会被存量数据干扰。先清空 rooms/messages 从零测。
    con = sqlite3.connect(db)
    con.execute("DELETE FROM rooms")
    con.execute("DELETE FROM messages")
    con.commit()
    con.close()
    r = client.post("/api/rooms", json={"type": "dm", "to": "xtmem", "topic": "zzz 话题"})
    assert r.status_code == 200, r.text[:300]
    room = r.json().get("room", {})
    assert room.get("id") == "dm:xtmem"
    assert room.get("type") == "dm"
    assert room.get("members") == ["local", "xtmem"]
    assert room.get("round_state") == "idle"
    assert room.get("topics") and room["topics"][0]["msg_count"] == 0
    r2 = client.post("/api/rooms", json={"type": "dm", "to": "xtmem"})
    assert r2.status_code == 200 and r2.json()["room"]["id"] == "dm:xtmem"
    rooms = client.get("/api/rooms").json().get("rooms", [])
    assert sum(1 for x in rooms if x["id"] == "dm:xtmem") == 1, "同 id 房只应有一间"


def test_create_room_group(db, client):
    """新建合席房：gr-* + members 自动补库主。"""
    r = client.post("/api/rooms", json={"type": "group", "members": ["dsh", "workbuddy"],
                                        "speak_mode": "sequential"})
    assert r.status_code == 200, r.text[:300]
    room = r.json().get("room", {})
    assert room.get("type") == "group"
    assert set(room.get("members", [])) == {"local", "dsh", "workbuddy"}
    assert room.get("id", "").startswith("gr-")


# ── 8. 发帖即上工（P5.1-2c）───────────────

def test_msg_send_fires_a2a_fallback(db, client):
    """notify 型消息落库后异步触发对端 A2A；对端不可达 → msg_timeout 降级事件（不真外发）。

    workbuddy 无 A2A peer（resolve_peer 空）→ a2a_call 立即抛 → 后台线程写 msg_timeout。
    """
    r = client.post("/api/msg/send", json={
        "room_id": "zzz-room", "to": ["workbuddy"], "kind": "chat",
        "content": "zzz 快去干活", "visibility": "shared"})
    assert r.status_code == 200, r.text[:300]
    mid = r.json()["message"]["id"]
    import time
    time.sleep(1.2)  # 等后台线程降级
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind='msg_timeout' AND agent='workbuddy'").fetchone()[0]
    con.close()
    assert n >= 1, "对端不可达应落 msg_timeout 降级事件"


def test_msg_send_a2a_success_writes_reply(db, client, monkeypatch):
    """成功路径：monkeypatch a2a_send 返回假回复 → 后台线程把回复写回 reply_to 链（不真外发）。"""
    import memory.a2a_client as a2a_mod
    monkeypatch.setattr(a2a_mod, "a2a_send", lambda agent, text, ctx="": ("fake reply", "ctx", "completed", "task-x"))
    r = client.post("/api/msg/send", json={
        "room_id": "zzz-room", "to": ["dsh"], "kind": "chat",
        "content": "zzz hi", "visibility": "shared"})
    assert r.status_code == 200, r.text[:300]
    mid = r.json()["message"]["id"]
    import time
    time.sleep(1.2)
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT from_agent, to_agent, content, reply_to FROM messages "
        "WHERE reply_to=? AND from_agent='dsh'", (mid,)).fetchone()
    con.close()
    assert row, "对端回复未写回 reply_to 链"
    assert row[1] == "local" and row[3] == mid and row[2] == "fake reply"


def test_msg_send_state_machine(db, client, monkeypatch):
    """走链状态机：成功 → 原消息 state=replied；对端不可达 → state=timeout。"""
    import memory.a2a_client as a2a_mod
    import time

    def _fake(agent, text, ctx=""):
        if agent == "xiaod":
            return ("fake reply", "ctx", "completed", "task-x")
        raise ValueError("unknown peer: " + str(agent))
    monkeypatch.setattr(a2a_mod, "a2a_send", _fake)

    # 成功：dsh → xiaod
    r1 = client.post("/api/msg/send", json={"room_id": "zzz1", "to": ["dsh"], "kind": "chat",
                                            "content": "zzz a", "visibility": "shared"})
    mid1 = r1.json()["message"]["id"]
    # 降级：workbuddy（无 peer）
    r2 = client.post("/api/msg/send", json={"room_id": "zzz2", "to": ["workbuddy"], "kind": "chat",
                                            "content": "zzz b", "visibility": "shared"})
    mid2 = r2.json()["message"]["id"]
    time.sleep(1.4)
    con = sqlite3.connect(db)
    s1 = con.execute("SELECT state FROM messages WHERE id=?", (mid1,)).fetchone()[0]
    s2 = con.execute("SELECT state FROM messages WHERE id=?", (mid2,)).fetchone()[0]
    con.close()
    assert s1 == "replied", "对端回应当置 replied，实际 " + str(s1)
    assert s2 == "timeout", "对端不可达当置 timeout，实际 " + str(s2)


def test_msg_send_ack_text_not_written_as_reply(db, client, monkeypatch):
    """A2A 返回『入站确认』文本（如 [pending] A2A 入站已注入assistant-c会话）时：
    不回写为对端回复（防污染），改发 system 风讯提示，原消息保持 waking。"""
    import memory.a2a_client as a2a_mod
    import time
    monkeypatch.setattr(a2a_mod, "a2a_send", lambda agent, text, ctx="": ("[pending] A2A 入站已注入assistant-c会话", "ctx", "", ""))
    r = client.post("/api/msg/send", json={"room_id": "zzz3", "to": ["dsh"], "kind": "chat",
                                            "content": "zzz 请回", "visibility": "shared"})
    mid = r.json()["message"]["id"]
    time.sleep(1.3)
    con = sqlite3.connect(db)
    # 不应有 from_agent='dsh' 的对端回复
    reply = con.execute("SELECT COUNT(*) FROM messages WHERE reply_to=? AND from_agent='dsh'", (mid,)).fetchone()[0]
    # 应有一条 system 风讯（kind=notice，映射 system）
    notice = con.execute("SELECT COUNT(*) FROM messages WHERE reply_to=? AND kind='notice' AND from_agent='local'", (mid,)).fetchone()[0]
    state = con.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()[0]
    con.close()
    assert reply == 0, "确认文本不应写回为对端回复"
    assert notice >= 1, "应发 system 风讯提示已唤醒"
    assert state == "waking", "对端未真回，消息应保持 waking，实际 " + str(state)
