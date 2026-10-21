# -*- coding: utf-8 -*-
"""P5.1-4 守门端 inbox 测试（2026-09-02，assistant-c）。

覆盖：未读消息 → 摘要块；标记已读 → 空；自证 actor 用 zzz- 前缀防与真数据重合。
"""

import sqlite3
import sys
import time
import tempfile
import os

import pytest
import axis_predicates

from memory.inbox import unread_summary

pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用")


def _fresh_db() -> str:
    db = tempfile.mktemp(suffix='.db')
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE chunks (id TEXT PRIMARY KEY, state TEXT)')
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(c)
    c.execute("INSERT INTO messages (id, from_agent, to_agent, content, ts, read, kind) VALUES (?,?,?,?,?,0,'chat')", ('m1','zzz-src','zzz-dst','hello',time.time()))
    c.commit(); c.close()
    return db


def test_unread_summary_present():
    """守卫：未读消息 → 返回含「信箱未读」的摘要块。"""
    db = _fresh_db()
    s = unread_summary('zzz-dst', db)
    assert '信箱未读' in s and 'hello' in s, s
    os.remove(db)


def test_unread_summary_empty_after_read():
    """守卫：全部已读 → 返回空串。"""
    db = _fresh_db()
    c = sqlite3.connect(db)
    c.execute('UPDATE messages SET read=1')
    c.commit(); c.close()
    assert unread_summary('zzz-dst', db) == ""
    os.remove(db)


def test_zzz_actor_prefix():
    """守卫：自证 actor 用 zzz- 前缀（防与真数据重合）。"""
    db = _fresh_db()
    assert unread_summary('zzz-dst', db)  # 能查到，证明 zzz- 自证值生效
    assert unread_summary('alice', db) == ""  # 无关 actor 查不到（不串读）
    os.remove(db)


def test_p1j_inbox_precise_read(tmp_db, cli_env):
    """P1-J / R-14：inbox 只标记本次 SELECT 命中的 id——跨 room/kind 不误刷。"""
    import subprocess as _sub
    import json as _json
    import sys as _sys
    import conftest as _cf

    c = sqlite3.connect(str(tmp_db))
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(c)   # canonical 快照可能缺 room_id 等列，幂等补列
    c.execute("DELETE FROM messages")
    t0 = time.time()
    c.execute(
        "INSERT INTO messages (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind) VALUES (?,?,?,?,?,0,?,?,?)",
        ("zzz-a1", "zzz-src", "zzz-dst", "room-a msg", t0, "room-a", "", "msg"),
    )
    c.execute(
        "INSERT INTO messages (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind) VALUES (?,?,?,?,?,0,?,?,?)",
        ("zzz-b1", "zzz-src", "zzz-dst", "room-b msg", t0 + 1, "room-b", "", "task"),
    )
    c.commit()
    c.close()

    env = dict(cli_env)
    env["XTMEM_DB_PATH"] = str(tmp_db)
    env.pop("XTMEM_ACTOR", None)
    p = _sub.run([_sys.executable, str(_cf.CLI), "msg", "inbox", "--as", "zzz-dst",
                  "--room", "room-a"], capture_output=True, text=True, encoding="utf-8",
                 timeout=60, env=env)
    out = _json.loads(p.stdout.strip() or "{}")
    assert out.get("ok") is True, p.stdout[:300] + p.stderr[:300]

    c = sqlite3.connect(str(tmp_db))
    reads = {r[0]: r[1] for r in c.execute("SELECT id, read FROM messages WHERE to_agent='zzz-dst'").fetchall()}
    c.close()
    assert reads.get("zzz-a1") == 1, reads   # room-a 命中 → 已读
    assert reads.get("zzz-b1") == 0, reads   # room-b 未命中 → 保持未读


