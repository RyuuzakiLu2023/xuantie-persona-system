# -*- coding: utf-8 -*-
"""P5.1-5 群聊调度器测试（2026-09-02，小D）。

守卫：① invite_only 默认不点名零发言；② sequential 一轮邀请数=活跃成员数且每个带 wake_prompt；
     ③ 超时后 pending_speakers 出队 + 轮次推进；④ 房间名 zzz-test- 前缀。
"""

import json
import sqlite3
import sys
import tempfile
import os

import pytest
import axis_predicates

from memory.chat_scheduler import (
    CHAT_MODES, InviteOnlyMode, SequentialMode, NaturalRandomMode,
    run_round, resolve_invite_prompt, DEFAULT_INVITE_PROMPT,
)

pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用")


def _fresh_db() -> tuple[str, sqlite3.Connection]:
    db = tempfile.mktemp(suffix='.db')
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE chunks (id TEXT PRIMARY KEY, state TEXT)')
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(c)
    c.execute("INSERT INTO rooms (id, name, members, mode, invite_prompt, current_round, pending_speakers, ts) VALUES (?,?,?,?,?,?,?,?)",
              ('zzz-test-room', 't', json.dumps(['a','b']), 'invite_only', DEFAULT_INVITE_PROMPT, 0, '[]', 1.0))
    c.commit()
    return db, c


def test_invite_only_no_mention_empty():
    """守卫①：invite_only 不点名单成员零发言。"""
    m = CHAT_MODES['invite_only']
    assert m.determine_speakers(['a','b'], [], {'mode':'invite_only'}, '你好') == []
    assert m.determine_speakers(['a','b'], [], {'mode':'invite_only'}, '@a 请说') == ['a']


def test_sequential_all_members_with_wake_prompt():
    """守卫②：sequential 一轮=全员，且每个邀请都带 wake_prompt（invite 模板替换 agent）。"""
    db, c = _fresh_db()
    calls = []
    def fake_a2a(agent, text, context_id=''):
        calls.append((agent, text))
        return ('reply-' + agent, 'ctx-' + agent, 'completed')
    res = run_round(c, 'zzz-test-room', ['a','b'], 'sequential', '', fake_a2a, room_row=None)
    assert set(res['speakers']) == {'a','b'}, res
    assert len(res['results']) == 2
    # 每个邀请消息都含被邀请 agent（wake_prompt 模板替换生效）
    for agent, text in calls:
        assert '{{agent}}' not in text, text
        assert agent in text, text
    c.close(); os.remove(db)


def test_timeout_dequeue_and_round_advance():
    """守卫③：超时成员出队（不阻塞本轮）+ 邀请留言 + 轮次推进。"""
    db, c = _fresh_db()
    def failing_a2a(agent, text, context_id=''):
        raise TimeoutError('agent down')
    res = run_round(c, 'zzz-test-room', ['a','b'], 'sequential', '', failing_a2a)
    assert all(not r['ok'] for r in res['results']), res
    # 每个超时成员都留了 kind=chat 邀请消息（留言兜底）
    n = c.execute("SELECT COUNT(*) FROM messages WHERE room_id='zzz-test-room' AND kind='chat'").fetchone()[0]
    assert n == len(res['speakers']), n
    # 轮次推进 + pending 清空
    room = c.execute("SELECT current_round, pending_speakers FROM rooms WHERE id='zzz-test-room'").fetchone()
    assert room['current_round'] == 1, room['current_round']
    assert room['pending_speakers'] == '[]', room['pending_speakers']
    c.close(); os.remove(db)


def test_zzz_test_room_prefix():
    """守卫④：房间名 zzz-test- 前缀（防与真数据重合）。"""
    db, c = _fresh_db()
    assert c.execute("SELECT id FROM rooms").fetchone()['id'].startswith('zzz-test-')
    c.close(); os.remove(db)


def test_msg_invite_event_on_success():
    """守卫⑤（P5.1-2b 尾巴②）：成功邀请写 msg_invite 事件（调度活性留痕）。"""
    db, c = _fresh_db()
    events = []
    def ev(kind, agent, meta=None):
        events.append((kind, agent, meta or {}))
    def fake_a2a(agent, text, context_id=''):
        return ('reply-' + agent, 'ctx', 'completed')
    res = run_round(c, 'zzz-test-room', ['a', 'b'], 'sequential', '', fake_a2a, write_event=ev)
    assert len(res['speakers']) == 2
    kinds = [e[0] for e in events]
    assert kinds.count('msg_invite') == 2, events
    assert all(e[1] in ('a', 'b') for e in events)
    c.close()
    os.remove(db)
