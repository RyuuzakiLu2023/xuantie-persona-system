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

# 议事厅点名模板（2026-09-08 重写）。旧版照抄 VCP 的「系统已为房间添加发言标记」——
# 那是 VCP 的文本标记法，我方按修订2 走角色层（正文零标记），那句话既误导又无用；
# 且没要求「以本人身份直接发言」，导致对端把点名当汇报、复述指令。
# 占位符：{{agent}}=被点名端，{{room}}=房间名。
DEFAULT_INVITE_PROMPT = (
    "【议事厅点名 · {{room}}】现在轮到你（{{agent}}）发言。"
    "请以你自己的身份，直接对房间里当前的话题说一句你的话——"
    "你的回复会原样贴进房间给库主看。"
    "不要复述本条点名指令，不要转述别人说了什么，不要汇报系统状态；"
    "没话可说就回一句「本轮无话」。"
)

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
    room_name = str((room or {}).get("name") or (room or {}).get("id") or "")
    return template.replace("{{agent}}", agent).replace("{{room}}", room_name)


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
        # 邀请先落库拿 id，并用它当 context_id（2026-09-08 修）：
        # 原先传 "" → a2a_client 自造 ctx-xxx，对端异步回帖的 reply_to 指向不存在的帖，
        # cmd_msg 的「--reply-to 自动归房」失效，群聊回帖会掉进无房孤儿帖。
        invite_mid = _write_invite_message(conn, room_id, agent, invite)
        try:
            reply, ctx, state = a2a_call(agent, invite, invite_mid or "")
            if write_event:
                write_event("msg_invite", agent, meta={"room": room_id, "state": state, "ok": True})
            # 群聊回帖落库（2026-09-08 修）：此前 reply 只进返回值、从不入库，
            # 导致议事厅跑完一轮房间时间线是空的。kind=chat → persist=0 / inject=1（二维闸）。
            # 对端只回确认文本（[pending]/ack 形）时不落，避免噪声帖。
            if reply and not _looks_like_ack(reply):
                _write_chat_message(conn, room_id, agent, reply)
            results.append({"agent": agent, "ok": True, "reply": reply, "ctx": ctx, "state": state})
        except Exception as e:
            # 超时/不可达 → 事件 + 降级标注；邀请帖已在 try 前落库，不重复写
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


# 对端「只确认没真答」的文本特征（2026-09-08 收紧：短真回复不是噪声，不能按长度吞）
_ACK_PREFIXES = ("[pending]",)
_ACK_EXACT = {"收到", "已收到", "好的", "行", "嗯", "ok", "okay", "ack", "已注入", "已唤醒", "收到！"}
_ACK_HINTS = ("a2a 入站已注入", "真回复异步产生中", "task_state_submitted", "task_state_working")


def _looks_like_ack(text):
    t = str(text or "").strip()
    if not t:
        return True
    low = t.lower().rstrip("。！!～~. ")
    if any(low.startswith(p) for p in _ACK_PREFIXES):
        return True
    if low in _ACK_EXACT:
        return True
    return any(h in low for h in _ACK_HINTS) and len(low) < 200


def _write_chat_message(conn, room_id, agent, text):
    """群聊发言落库：from=该端 → to=ryuuzaki，kind=chat（persist=0 / inject=1）。"""
    try:
        mid = "msg-" + str(int(time.time() * 1000)) + "-" + str(len(text) % 1000)
        conn.execute(
            "INSERT OR REPLACE INTO messages (id, from_agent, to_agent, content, ts, read, room_id, kind, persist, inject)"
            " VALUES (?,?,?,?,?,0,?,'chat',0,1)",
            (mid, agent, "ryuuzaki", str(text)[:4000], time.time(), room_id),
        )
        conn.commit()
        return mid
    except Exception:
        return ""


def _write_invite_message(conn, room_id, agent, text):
    try:
        mid = "msg-" + str(int(time.time() * 1000)) + "-" + str(len(text) % 1000)
        conn.execute(
            "INSERT OR REPLACE INTO messages (id, from_agent, to_agent, content, ts, read, room_id, kind, persist, inject)"
            " VALUES (?,?,?,?,?,0,?,'chat',0,1)",
            (mid, "scheduler", agent, text, time.time(), room_id),
        )
        conn.commit()
        return mid
    except Exception:
        return ""


def _advance_round(conn, room_id):
    """本轮结束：轮次+1，pending_speakers 清空（全部出队，超时不阻塞）。"""
    try:
        conn.execute("UPDATE rooms SET current_round=current_round+1, pending_speakers='[]', ts=? WHERE id=?", (time.time(), room_id))
        conn.commit()
    except Exception:
        pass
