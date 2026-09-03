# -*- coding: utf-8 -*-
"""P5.1-4 守门端 inbox 测试（2026-09-02，小D）。

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

