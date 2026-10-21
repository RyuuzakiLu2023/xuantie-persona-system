# -*- coding: utf-8 -*-
"""memory/inbox.py — P5.1-4 守门端未读信箱摘要（2026-09-02，小D）。

提供 unread_summary(actor, db_path) -> str，供 hermes provider / zcode hook / DSH 工具等注入点共用。
零依赖，纯 sqlite3 只读。
"""
from __future__ import annotations

import sqlite3
import time


def unread_summary(actor: str, db_path: str, max_age_days: int = 7) -> str:
    """返回 actor 的未读消息摘要（最近 7 天，最多 10 条）。无未读返回空字符串。

    P5.1-4 守门端（2026-09-02）：每端注入点加「若有未读 messages 先处理」——
    兜底覆盖 A2A 没配 / 对端休息 / 消息在信箱里等人的场景。
    """
    if not actor or not db_path:
        return ""
    try:
        from memory.open_db import open_db
        conn = open_db(db_path, ro=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, from_agent, content, ts, room_id, kind FROM messages"
                " WHERE to_agent=? AND read=0 AND ts > ? ORDER BY ts DESC LIMIT 10",
                (actor, time.time() - max_age_days * 86400),
            ).fetchall()
            if not rows:
                return ""
            lines = []
            for r in rows:
                d = dict(r)
                room = f" (房间 {d['room_id']})" if d.get("room_id") else ""
                kind = f"[{d.get('kind','msg')}]"
                text = (d.get("content") or "")[:80]
                lines.append(f"  - {kind} {d['from_agent']}: {text}{room}")
            return "【信箱未读 · 守门端兜底】有未读留言，优先处理：\n" + "\n".join(lines)
        finally:
            conn.close()
    except Exception:
        return ""

