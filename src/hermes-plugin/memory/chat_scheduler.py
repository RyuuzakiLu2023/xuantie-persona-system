# -*- coding: utf-8 -*-
"""memory/chat_scheduler.py — P5.1-5 群聊调度器（2026-09-02，小D）。

架构照 VCP 借：BaseChatMode + determineSpeakers(成员, 历史, 房间, 用户消息) 单方法契约 + CHAT_MODES 映射。
三模式：invite_only（默认，不点名零发言）/ sequential（轮序邀全）/ natural_random（五级降级链）。

执行模型 v2.2 A2A 化：run_round 接受注入的 a2a_call(agent, text, context_id?) -> (reply, ctx, state)，
调度器不自己实现通道；超时降级写 kind=chat 邀请消息（inject=1）+ xtmem_events(msg_timeout) + pending_speakers 出队。

轮次状态机：rooms.current_round + pending_speakers，按端超时进 xtmem_tuning.json（P3.1-a 现成机制）。
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Callable, Dict, List, Optional

DEFAULT_INVITE_PROMPT = "现在轮到你{{agent}}发言了。系统已为房间添加发言标记，正常聊天即可。"

# 按端默认超时（秒）；可由 xtmem_tuning.json 的 cockpit_timeouts 覆盖
DEFAULT_TIMEOUTS = {"xuantie_lark": 90, "dsh": 60, "workbuddy": 120, "zcode": 0}


def _resolve_timeouts(overrides=None):
    """P5.1-2b 尾巴③（2026-09-02）：超时旋钮外置——xtmem_tuning.json cockpit_timeouts
    覆盖默认（键=端 ns 名）。tuning 缺失/损坏 → 纯默认兜底（删 json 行为与改前一致）。"""
    if overrides:
        return overrides
    out = dict(DEFAULT_TIMEOUTS)
    try:
        from memory.tuning import load_tuning
        cfg = (load_tuning().get("cockpit_timeouts") or {})
        for k, v in cfg.items():
            try:
                out[k] = int(v)
            except Exception:
                pass
    except Exception:
        pass
    return out

ROLE_TAGS = {"natural": 0.2, "strict": 0.85}


class BaseChatMode:
    """VCP determineSpeakers 抽象：返回本轮应发言的成员列表。"""
    name = "base"

    def determine_speakers(self, members, history, room, user_message):
        raise NotImplementedError


def _mentions(text, members):
    """返回被 @ 直接点名的活跃成员。"""
    text = text or ""
    hits = []
    for m in members:
        for token in ("@" + m, "@" + m.replace("_", " "), "@" + m.replace("_", ""), "@" + m.lower()):
            if token in text.lower():
                hits.append(m)
                break
    return hits


class InviteOnlyMode(BaseChatMode):
    """默认模式：不点名返回空，@成员才发言。"""
    name = "invite_only"

    def determine_speakers(self, members, history, room, user_message):
        return _mentions(user_message, members)


class SequentialMode(BaseChatMode):
    """轮序：直接返回全部活跃成员（不看历史）。"""
    name = "sequential"

    def determine_speakers(self, members, history, room, user_message):
        return list(members)


class NaturalRandomMode(BaseChatMode):
    """naturerandom：五级降级链（@提及 → tag → @所有人 → 概率 0.15 → 均匀随机兜底）。"""
    name = "natural_random"

    def __init__(self, rng=None):
        self._rng = rng or random

    def determine_speakers(self, members, history, room, user_message):
        members = list(members)
        if not members:
            return []
        # ① @提及直接命中
        mentioned = _mentions(user_message, members)
        if mentioned:
            return mentioned
        # ② 标签匹配 natural/strict
        tags = (room or {}).get("tags") or {}
        strict = tags.get("strict") or []
        natural = tags.get("natural") or []
        picked = []
        for m in members:
            if m in strict and self._rng.random() < ROLE_TAGS["strict"]:
                picked.append(m)
            elif m in natural:
                # natural：上轮自己说过 0.75，否则 0.2
                spoke_last = any(

                    (h.get("from_agent") == m and h.get("to_agent") in ("", "all", "group"))

                    for h in history[:8]

                )
                p = 0.75 if spoke_last else 0.2
                if self._rng.random() < p:
                    picked.append(m)
        if picked:
            return picked
        # ③ @所有人
        um = (user_message or "").lower()
        if "@all" in um or "@所有人" in um or "[@all]" in um:
            return members
        # ④ 概率触发 base 0.15
        if self._rng.random() < 0.15:
            return [self._rng.choice(members)]
        # ⑤ 兜底均匀随机
        return [self._rng.choice(members)]


CHAT_MODES: Dict[str, BaseChatMode] = {
    "invite_only": InviteOnlyMode(),
    "sequential": SequentialMode(),
    "natural_random": NaturalRandomMode(),
}


def resolve_invite_prompt(room, agent):
    """invitePrompt 模板替换：rooms.invite_prompt 可覆盖；默认用 DEFAULT_INVITE_PROMPT。"""
    template = (room or {}).get("invite_prompt") or DEFAULT_INVITE_PROMPT
    return template.replace("{{agent}}", agent)


def run_round(
    conn, room_id, members, mode_name, user_message, a2a_call, timeouts=None, write_event=None, room_row=None,
):
    """执行一轮群聊调度。

    a2a_call(agent, text, context_id?) -> (reply_text, ctx, state)；超时/异常 -> 抛异常走降级。
    轮次状态机：房间 pending_speakers 有值则当作本轮队列（逐一出队），否则由 determine_speakers 现算；
    每个成员处理后出队（超时不阻塞本轮），current_round+1，pending_speakers 清空。
    返回 {round, speakers, results:[{agent, ok, reply}]}。
    """
    timeouts = _resolve_timeouts(timeouts)
    mode = CHAT_MODES.get(mode_name) or CHAT_MODES["invite_only"]
    history = _recent_history(conn, room_id, limit=12)
    room = room_row or _get_room(conn, room_id)
    pending = _parse_pending(room)
    if pending:
        speakers = pending
    else:
        speakers = mode.determine_speakers(list(members), history, room or {}, user_message or "")
    results = []
    for agent in speakers:
        invite = resolve_invite_prompt(room, agent)
        try:
            reply, ctx, state = a2a_call(agent, invite, "")
            if write_event:
                write_event("msg_invite", agent, meta={"room": room_id, "state": state, "ok": True})
            results.append({"agent": agent, "ok": True, "reply": reply, "ctx": ctx, "state": state})
        except Exception as e:
            # 超时/不可达 → 留言兜底 + 事件；该成员出队不阻塞本轮
            _write_invite_message(conn, room_id, agent, invite)
            if write_event:
                write_event("msg_timeout", agent, meta={"room": room_id, "reason": str(e)})
            results.append({"agent": agent, "ok": False, "error": str(e)})
    _advance_round(conn, room_id)
    return {"round": _get_room(conn, room_id).get("current_round", 0), "speakers": speakers, "results": results}


def _parse_pending(room):
    raw = (room or {}).get("pending_speakers") or "[]"
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return list(raw)


def _recent_history(conn, room_id, limit=12):
    try:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, content, ts FROM messages WHERE room_id=? ORDER BY ts DESC LIMIT ?",
            (room_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_room(conn, room_id):
    try:
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def _write_invite_message(conn, room_id, agent, text):
    try:
        mid = "msg-" + str(int(time.time() * 1000)) + "-" + str(len(text) % 1000)
        conn.execute(
            "INSERT OR REPLACE INTO messages (id, from_agent, to_agent, content, ts, read, room_id, kind, persist, inject)"
            " VALUES (?,?,?,?,?,0,?,'chat',0,1)",
            (mid, "scheduler", agent, text, time.time(), room_id),
        )
        conn.commit()
    except Exception:
        pass


def _advance_round(conn, room_id):
    """本轮结束：轮次+1，pending_speakers 清空（全部出队，超时不阻塞）。"""
    try:
        conn.execute("UPDATE rooms SET current_round=current_round+1, pending_speakers='[]', ts=? WHERE id=?", (time.time(), room_id))
        conn.commit()
    except Exception:
        pass
