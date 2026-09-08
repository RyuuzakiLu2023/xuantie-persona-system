# -*- coding: utf-8 -*-
"""memory/cockpit_contract.py — 照渊台契约组装层（P5.1-2b 契约缝合，2026-09-02，小D）

把 XTMEM 真库（chunks/messages/rooms/widgets/entities/tag_edges/relations/…）组装成
照渊台前端契约定死的八端点形状。契约唯一出处：
    <COCKPIT_ROOT>\\src\\api\\contract.ts + endpoints.ts（字段级）
mock（src/mock/engine.ts）是行为基准；fixtures/*.json 是返回样本。
本模块是纯函数层：入参 sqlite3.Connection（读或写）+ 探活 dict → 出参契约形状 dict，
api_server 只做薄路由。改字段名/形状前必须对照 endpoints.ts，禁止自造形状。

设计裁定（Q1-Q6 后端拍板）见 <XT_ROOT>\\new\\XTMEM-驾驶舱-照渊台契约缝合-设计-v1.md。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

# ── 三域翻译（照渊台 AgentId / 库 namespace / A2A peer）──────────
CONTRACT_AGENT_IDS = ["xuantie", "dsh", "workbuddy", "zcode"]
NS_OF = {"xuantie": "xuantie_lark", "dsh": "dsh", "workbuddy": "workbuddy", "zcode": "zcode"}
ID_OF_NS = {"xuantie_lark": "xuantie", "dsh": "dsh", "workbuddy": "workbuddy", "zcode": "zcode"}
# A2A peer：契约 id 'dsh' 在 a2a_client 里注册为 'xiaod'（9901）；其余同名。
A2A_OF = {"xuantie": "xuantie", "dsh": "xiaod", "workbuddy": "workbuddy", "zcode": "zcode"}
# 探活端口（127.0.0.1）。zcode 是会话内 MCP，无常驻端口 → 不探活。
PROBE_PORTS = {"xuantie": 9900, "dsh": 28000, "workbuddy": 5778}

# 灯静态源：epithet/presence/channel/wake_p50_ms/clock_rate 抄照渊台 fixtures 预设
# （wake_p50 是实测中位数——fixtures 生成器注释口径）。lamp/can_answer_now 是动态位。
AGENT_META = [
    {"id": "xuantie", "name": "玄铁", "epithet": "常明 · 铁砧上的火", "presence": "resident",
     "channel": "A2A 入站 · 常驻进程 9900", "wake_p50_ms": 0, "clock_rate": 1.0,
     "can_answer_now_base": True},
    {"id": "dsh", "name": "小D", "epithet": "引火 · 鱼塘的水影", "presence": "on_demand",
     "channel": "headless 拉起 · 端口 28000", "wake_p50_ms": 4542, "clock_rate": 0.72,
     "can_answer_now_base": True},
    {"id": "workbuddy", "name": "江北", "epithet": "檐下 · 茶馆的逗哏", "presence": "desktop",
     "channel": "桌面端 automations 表驱动", "wake_p50_ms": 15200, "clock_rate": 1.35,
     "can_answer_now_base": False},
    {"id": "zcode", "name": "Z指导", "epithet": "行灯 · 命令行里的发布会", "presence": "session",
     "channel": "会话内 MCP · 关了只能留帖", "wake_p50_ms": 30000, "clock_rate": 1.0,
     "can_answer_now_base": False},
]

# 发言模式：库/调度器值 ↔ 照渊台契约 SpeakMode
MODE_OF_DB = {"invite_only": "inviteonly", "natural_random": "naturerandom", "sequential": "sequential"}
MODE_OF_CONTRACT = {v: k for k, v in MODE_OF_DB.items()}

# 消息 state：库值（task 域）→ 契约 MsgState（真后端无命途推演，语义如实简化）
_STATE_OF_DB = {
    "": "delivered",          # 非 task 已入库送达；task 空态 = 待回执（delivered）
    "pending": "queued",
    "ack": "acked",
    "done": "acked",
    "expired": "timeout",
}

# 谁看见（audience）：照 FATE 表（见前端 fates.ts），只按 kind 推
_PERSIST_KINDS = frozenset({"task", "decision"})
_NOTIFY_KINDS = frozenset({"task"})
_NEEDS_ACK_KINDS = frozenset({"task"})
_PAIR_AUDIENCE_KINDS = frozenset({"msg", "task"})

# 未知 from/to 的归一：bus 自定义 actor 原样透传（运行期容忍，前端对未知 id 显示 id 原文）
_SENDER_ALIAS = {"scheduler": "ryuuzaki"}  # 调度器邀请以库主名义发起（产品语义）

# 实体 kind 启发（实体表无 kind 列；canon 名单 → agent/person/project，其余 term）
_AGENT_CANONS = frozenset({"玄铁", "小D", "江北", "Z老师", "xuantie_lark", "workbuddy", "zcode",
                           "dsh", "XuanTie", "DSH", "阿北", "zhanglaoshi", "Z 老师"})
_PERSON_CANONS = frozenset({"龙崎", "lq", "Ryuuzaki"})
_PROJECT_CANONS = frozenset({"XTMEM", "xtmem", "tuan-game", "tuan_game", "XT-Forge",
                             "Hermes", "hermes", "赫尔墨斯", "AGENTS.md"})

WIDGET_KINDS = frozenset({"note", "metric", "ask", "dream"})
GRAPH_MEMORY_LIMIT = 500    # Q1 拍板：窗口 400-600 → 取 500
GRAPH_EDGE_BUDGET = 1000
GRAPH_ENTITY_LIMIT = 60
EXCERPT_LEN = 80


# ── 基础工具 ──────────────────────────────────────────────

def _iso(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(float(ts)))
    except Exception:
        return None


def _now_iso():
    return _iso(time.time())


def _hash6(s):
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()[:6]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _contract_id(name):
    """roles id（ns）→ 契约 AgentId；未知原样返回（运行期容忍）。"""
    if name in ID_OF_NS:
        return ID_OF_NS[name]
    return _SENDER_ALIAS.get(str(name), str(name))


def _ns_of(name):
    return NS_OF.get(str(name), str(name))


def _loads_list(v):
    """宽容 JSON 列表解析：list 直返；JSON 数组串；double-encode 串再解一层（rooms.members 历史坑）。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    s = str(v).strip()
    if not s:
        return []
    try:
        out = json.loads(s)
        if isinstance(out, list):
            return out
        if isinstance(out, str):
            out2 = json.loads(out)
            if isinstance(out2, list):
                return out2
    except Exception:
        pass
    return [x.strip() for x in s.split(",") if x.strip()]


# ── 命运位 / 帖形状 ───────────────────────────────────────

def fate_of(kind: str) -> Dict[str, Any]:
    kind = kind or "msg"
    return {
        "persist": kind in _PERSIST_KINDS,
        "notify": kind in _NOTIFY_KINDS,
        "needs_ack": kind in _NEEDS_ACK_KINDS,
        "audience": "pair" if kind in _PAIR_AUDIENCE_KINDS else "room",
    }


def state_of(row: Dict[str, Any]) -> str:
    db_state = str(row.get("state") or "")
    if db_state in _STATE_OF_DB:
        return _STATE_OF_DB[db_state]
    return "failed"


def to_message(row: Dict[str, Any]) -> Dict[str, Any]:
    """messages 一行 → 契约 Message。私密：content=null + redacted（正文零下发硬契约）。"""
    vis = str(row.get("visibility") or "shared")
    sealed = vis == "private"
    fate = fate_of(row.get("kind"))
    shared_with = row.get("shared_with") or ""
    try:
        sw = json.loads(shared_with) if shared_with else []
    except Exception:
        sw = []
    created = _iso(row.get("ts"))
    return {
        "id": str(row.get("id") or ""),
        "room_id": str(row.get("room_id") or ""),
        "topic_id": str(row.get("topic_id") or "") or None,
        "kind": "system" if str(row.get("kind") or "msg") == "notice" else str(row.get("kind") or "msg"),
        "state": state_of(row),
        "from": _contract_id(row.get("from_agent")),
        "to": [_contract_id(row.get("to_agent"))] if (row.get("to_agent") or "") else [],
        "content": None if sealed else str(row.get("content") or ""),
        **({"redacted": True} if sealed else {}),
        "visibility": vis if vis in ("shared", "private", "shared_with") else "shared",
        **({"shared_with": sw} if vis == "shared_with" else {}),
        "persist": fate["persist"],
        "notify": fate["notify"],
        "needs_ack": fate["needs_ack"],
        "audience": fate["audience"],
        "created_at": created,
        "woke_at": _iso(row.get("woke_at")) if row.get("woke_at") else None,
        "reply_to": str(row.get("reply_to") or "") or None,
        "deadline_s": row.get("deadline_s"),
        "chunk_id": str(row.get("chunk_id") or "") or None,
    }


# ── 灯 / 体征 / state ─────────────────────────────────────

def agent_rows(conn, probes: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
    """四盏灯。probes = {agent_id: reachable}，None 表示全部按 can_answer_now_base（测试用）。"""
    probes = probes or {}
    out = []
    for meta in AGENT_META:
        aid = meta["id"]
        ns = NS_OF[aid]
        if aid in probes:
            reachable = probes[aid]
        elif aid == "zcode":
            reachable = False
        else:
            reachable = meta["can_answer_now_base"]
        # zcode 是行灯（会话关了只能留帖）：lamp 恒 asleep（只剩余烬），不随探活变 unreachable
        lamp = "asleep" if aid == "zcode" else ("unreachable" if not reachable else "online")
        mem = conn.execute("SELECT COUNT(*) FROM chunks WHERE namespace=?", (ns,)).fetchone()[0]
        priv = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE namespace=? AND visibility='private'", (ns,)).fetchone()[0]
        unread = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_agent=? AND read=0", (ns,)).fetchone()[0]
        open_tasks = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_agent=? AND kind='task' AND state IN ('pending','')",
            (ns,)).fetchone()[0]
        last = conn.execute(
            "SELECT MAX(ts) FROM (SELECT MAX(ts) ts FROM messages WHERE from_agent=? OR to_agent=?"
            " UNION ALL SELECT MAX(updated_at) ts FROM chunks WHERE namespace=?)",
            (ns, ns, ns)).fetchone()[0]
        out.append({
            "id": aid, "name": meta["name"], "epithet": meta["epithet"],
            "presence": meta["presence"], "lamp": lamp, "channel": meta["channel"],
            "wake_p50_ms": meta["wake_p50_ms"],
            "can_answer_now": reachable and (aid != "zcode"),
            "unread": unread, "open_tasks": open_tasks,
            "memories": mem, "private_count": priv,
            "clock_rate": meta["clock_rate"], "last_seen": _iso(last) or _now_iso(),
        })
    return out


def vitals_of(conn) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    raw = conn.execute("SELECT COUNT(*) FROM chunks WHERE tier IS NULL OR tier='raw'").fetchone()[0]
    refined = conn.execute("SELECT COUNT(*) FROM chunks WHERE tier='refined'").fetchone()[0]
    domains = conn.execute(
        "SELECT COUNT(DISTINCT namespace) FROM chunks WHERE namespace IS NOT NULL AND namespace!='hermes-webui'"
    ).fetchone()[0]
    try:
        clocks = max(5, conn.execute("SELECT COUNT(DISTINCT viewer_agent) FROM chunk_utility").fetchone()[0])
    except Exception:
        clocks = 5
    try:
        evo_rows = conn.execute(
            "SELECT relation, COUNT(*) c FROM memory_evolution GROUP BY relation").fetchall()
    except Exception:
        evo_rows = []
    evo_total = 0
    by_rel = {"supersedes": 0, "disagrees_with": 0, "related": 0}
    for r in evo_rows:
        rel = str(r[0] or "")
        n = r[1]
        evo_total += n
        if rel == "supersedes":
            by_rel["supersedes"] += n
        elif "disagrees" in rel:
            by_rel["disagrees_with"] += n
        else:
            by_rel["related"] += n
    try:
        relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    except Exception:
        relations = 0
    day0 = time.time() - (time.time() % 86400)
    try:
        evicted = conn.execute(
            "SELECT COUNT(*) FROM xtmem_events WHERE kind='forget' AND ts>=?", (day0,)).fetchone()[0]
    except Exception:
        evicted = 0
    last_w = conn.execute("SELECT MAX(ts) FROM xtmem_events WHERE kind='write'").fetchone()[0]
    return {
        "total": total, "raw": raw, "refined": refined, "domains": domains, "clocks": clocks,
        "evo_edges": evo_total, "evo_by_rel": by_rel, "relations": relations,
        # 蒸馏无独立事件（引擎蒸馏走 write 链）→ 以最近 write 时刻近似，注释如实
        "last_distill": _iso(last_w) or _now_iso(),
        "evicted_today": evicted,
        "refined_hit_rate": 0.0,  # TODO：等 recall 命中统计埋点后接真值
    }


def _liveness_of(conn) -> float:
    """Q4 定义：有帖在途（task pending/待回执）≥0.6 顶到 0.95；无在途按近 15 分钟帖事件指数衰减。"""
    in_chain = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state IN ('pending','')").fetchone()[0]
    if in_chain > 0:
        return round(_clamp(0.6 + 0.15 * in_chain, 0, 0.95), 3)
    last = conn.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
    if not last:
        return 0.05
    mins = max(0.0, (time.time() - last) / 60.0)
    return round(max(0.05, 0.5 * math.exp(-mins / 15.0)), 3)


def widget_of(row: Dict[str, Any]) -> Dict[str, Any]:
    sealed = str(row.get("visibility") or "shared") == "private"
    try:
        actions = json.loads(row.get("actions") or "[]")
    except Exception:
        actions = []
    return {
        "id": str(row.get("id") or ""),
        "from": _contract_id(row.get("from_agent")),
        "kind": str(row.get("kind") or "note"),
        "title": str(row.get("title") or ""),
        "body": None if sealed else str(row.get("body") or ""),
        **({"redacted": True} if sealed else {}),
        "visibility": str(row.get("visibility") or "shared"),
        "created_at": _iso(row.get("ts")),
        **({"actions": actions} if actions else {}),
    }


def widgets_of(conn, limit: int = 8) -> List[Dict[str, Any]]:
    rows = _rows(conn,
                 "SELECT * FROM widgets WHERE handled=0 ORDER BY ts DESC LIMIT ?", (limit,))
    return [widget_of(r) for r in rows]


def task_counts_of(conn) -> Dict[str, int]:
    open_n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state IN ('pending','')").fetchone()[0]
    waiting = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state=''").fetchone()[0]
    day0 = time.time() - (time.time() % 86400)
    done = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state IN ('ack','done') AND ts>=?",
        (day0,)).fetchone()[0]
    return {"open": open_n, "waiting_ack": waiting, "done_today": done}


def cockpit_state(conn, probes: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    unread_total = conn.execute("SELECT COUNT(*) FROM messages WHERE read=0").fetchone()[0]
    recent_rows = _rows(conn, "SELECT * FROM messages ORDER BY ts DESC LIMIT 24")
    return {
        "ok": True,
        "now": _now_iso(),
        "liveness": _liveness_of(conn),
        "agents": agent_rows(conn, probes),
        "vitals": vitals_of(conn),
        "unread_total": unread_total,
        "task_counts": task_counts_of(conn),
        "recent": [to_message(r) for r in recent_rows],
        "widgets": widgets_of(conn),
    }


# ── 房间 / 上下文 ─────────────────────────────────────────

def _topic_title(conn, room_id, topic_id):
    r = conn.execute(
        "SELECT content FROM messages WHERE room_id=? AND topic_id=? AND ts="
        "(SELECT MIN(ts) FROM messages WHERE room_id=? AND topic_id=?) LIMIT 1",
        (room_id, topic_id, room_id, topic_id)).fetchone()
    if not r:
        return topic_id
    t = str(r[0] or "").strip().replace("\n", " ")
    return (t[:24] + "…") if len(t) > 24 else t


def room_of(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    members = [_contract_id(m) for m in _loads_list(row.get("members"))]
    mode_db = str(row.get("mode") or "invite_only")
    pending = [_contract_id(m) for m in _loads_list(row.get("pending_speakers"))]
    round_n = int(row.get("current_round") or 0)
    room_id = str(row.get("id") or "")
    if pending:
        round_state = "running"
    elif round_n == 0:
        round_state = "idle"
    else:
        round_state = "waiting"
    unread = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE room_id=? AND read=0", (room_id,)).fetchone()[0]
    topics = []
    for t in _rows(conn,
                   "SELECT topic_id, COUNT(*) c, "
                   "SUM(CASE WHEN visibility='private' THEN 1 ELSE 0 END) pc, MAX(ts) last "
                   "FROM messages WHERE room_id=? AND topic_id!='' GROUP BY topic_id ORDER BY last DESC",
                   (room_id,)):
        topics.append({
            "id": t["topic_id"],
            "title": _topic_title(conn, room_id, t["topic_id"]),
            "msg_count": t["c"], "private_count": int(t["pc"] or 0),
            "last_at": _iso(t["last"]),
        })
    return {
        "id": room_id,
        "type": "dm" if room_id.startswith("dm:") else "group",
        "title": str(row.get("name") or room_id),
        "members": members,
        "speak_mode": MODE_OF_DB.get(mode_db, "inviteonly"),
        "pending_speakers": pending,
        "round": round_n,
        "round_state": round_state,
        "unread": unread,
        "last_at": _iso(row.get("ts")),
        "topics": topics,
    }


def rooms_of(conn) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM rooms ORDER BY ts DESC")
    return {"ok": True, "rooms": [room_of(conn, r) for r in rows]}


def room_context_of(conn, room_id: str, topic_id: Optional[str], n: int = 40) -> Dict[str, Any]:
    row = _row(conn, "SELECT * FROM rooms WHERE id=?", (room_id,))
    if not row:
        return {"ok": False, "code": "bad_shape", "message": "没有这间房：" + str(room_id)}
    sql = "SELECT * FROM messages WHERE room_id=?"
    params = [room_id]
    if topic_id:
        sql += " AND topic_id=?"
        params.append(topic_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(min(max(int(n), 1), 100))
    rows = _rows(conn, sql, tuple(params))
    rows.reverse()
    return {"ok": True, "room": room_of(conn, row), "topic_id": topic_id, "n": min(max(int(n), 1), 100),
            "messages": [to_message(r) for r in rows]}


# ── 星渊图 ────────────────────────────────────────────────

def _entity_kind(canon: str) -> str:
    c = str(canon)
    if c in _AGENT_CANONS:
        return "agent"
    if c in _PERSON_CANONS:
        return "person"
    if c in _PROJECT_CANONS:
        return "project"
    return "term"


def _memory_window(conn) -> List[Dict[str, Any]]:
    """四灯 ns + active 的轻列候选 → 按(heat, updated_at)排序取窗口（Q1：500）。"""
    ns = list(NS_OF.values())
    marks = ",".join("?" * len(ns))
    cand = _rows(conn,
                 "SELECT id, namespace, tier, importance, timestamp, updated_at, state, superseded_by "
                 "FROM chunks WHERE namespace IN (" + marks + ") AND state='active' "
                 "ORDER BY updated_at DESC LIMIT 800",
                 tuple(ns))
    if not cand:
        return []
    ids = [c["id"] for c in cand]
    heat_map = {}
    try:
        for r in conn.execute(
                "SELECT chunk_id, utility_score, access_count FROM chunk_utility WHERE chunk_id IN ("
                + ",".join("?" * len(ids)) + ")", tuple(ids)):
            u = r[1]
            if u is None:
                u = 0.3 + (r[2] or 0) * 0.02
            heat_map[r[0]] = round(_clamp(float(u), 0, 1), 3)
    except Exception:
        pass
    half = 720 * 3600.0
    now = time.time()
    for c in cand:
        c["heat"] = heat_map.get(c["id"], 0.2)
        elapsed = max(0.0, now - (c.get("updated_at") or c.get("timestamp") or now))
        c["life"] = round(max(0.0, min(1.0, 0.5 ** (elapsed / half))), 3)
    cand.sort(key=lambda c: (-c["heat"], -c["life"], -(c.get("updated_at") or 0)))
    return cand[:GRAPH_MEMORY_LIMIT]


def graph_of(conn) -> Dict[str, Any]:
    win = _memory_window(conn)
    win_ids = [c["id"] for c in win]
    ns = list(NS_OF.values())
    marks = ",".join("?" * len(ns))

    # memories（第三趟取 excerpt）
    excerpt_map = {}
    if win_ids:
        for r in conn.execute(
                "SELECT id, substr(content,1,?), visibility FROM chunks WHERE id IN ("
                + ",".join("?" * len(win_ids)) + ")", (EXCERPT_LEN, *win_ids)):
            excerpt_map[r[0]] = (r[1], r[2])
    memories = []
    for c in win:
        ex, vis = excerpt_map.get(c["id"], ("", "shared"))
        sealed = vis == "private"
        memories.append({
            "id": c["id"], "type": "node_memory",
            "tier": "refined" if c.get("tier") == "refined" else "raw",
            "owner": _contract_id(c.get("namespace")),
            "visibility": vis if vis in ("shared", "private") else "shared",
            "excerpt": None if sealed else (str(ex or "")[:EXCERPT_LEN]),
            **({"redacted": True} if sealed else {}),
            "importance": round(float(c.get("importance") or 0.5), 3),
            "heat": c.get("heat", 0.2), "life": c.get("life", 1.0),
            **({"retired": True} if (c.get("state") in ("superseded", "archived")) else {}),
            "created_at": _iso(c.get("timestamp")),
        })

    # entities（四灯域 canon + 别名 + mentions + idf）
    ents = _rows(conn,
                 "SELECT canon, count, last_seen FROM entities WHERE count>0 "
                 "ORDER BY count DESC LIMIT ?", (GRAPH_ENTITY_LIMIT,))
    aliases = {}
    if ents:
        canon_list = [e["canon"] for e in ents]
        for r in conn.execute(
                "SELECT canon, alias FROM entity_aliases WHERE canon IN ("
                + ",".join("?" * len(canon_list)) + ")", tuple(canon_list)):
            aliases.setdefault(r[0], []).append(r[1])
    max_count = max([int(e["count"] or 1) for e in ents] or [1])
    entities = []
    for e in ents:
        canon = e["canon"]
        cnt = int(e["count"] or 0)
        entities.append({
            "id": "e-" + _hash6(canon), "type": "node_entity", "label": canon,
            "aliases": [a for a in aliases.get(canon, []) if a != canon],
            "mentions": cnt, "kind": _entity_kind(canon),
            "idf": round(_clamp(1.0 - (cnt / float(max_count)), 0.05, 1.0), 2),
        })
    ent_id = {e["label"]: e["id"] for e in entities}

    edges: Dict[str, Dict[str, Any]] = {}
    budget = GRAPH_EDGE_BUDGET

    def add_edge(eid, src, tgt, rel, weight, idf=None):
        if len(edges) >= budget:
            return
        edges[eid] = {"id": eid, "source": src, "target": tgt, "rel": rel, "weight": weight,
                      **({"idf": idf} if idf is not None else {})}

    # co 边（entity↔entity，tag_edges）
    for r in _rows(conn, "SELECT a, b, cnt FROM tag_edges ORDER BY cnt DESC"):
        if len(edges) >= budget:
            break
        ia, ib = ent_id.get(r["a"]), ent_id.get(r["b"])
        if not ia or not ib:
            continue
        w = float(r["cnt"] or 1)
        idf = min([x.get("idf", 0.5) for x in entities if x["id"] in (ia, ib)] or [0.5])
        add_edge("g-co-" + _hash6(r["a"] + r["b"]), ia, ib, "co", w, idf)

    # evo 边（窗口内 chunk↔chunk：superseded_by 语义确定 + memory_evolution 双在窗口）
    if win_ids:
        for r in conn.execute(
                "SELECT id, superseded_by FROM chunks WHERE id IN ("
                + ",".join("?" * len(win_ids)) + ") AND superseded_by!=''", tuple(win_ids)):
            tgt, src = r[0], r[1]
            if src in win_ids:
                add_edge("g-evo-" + _hash6(src + tgt), src, tgt, "supersedes", 1.0)
        for r in conn.execute(
                "SELECT e.source_id, e.target_id, e.relation FROM memory_evolution e "
                "JOIN chunks s ON s.id = e.source_id "
                "JOIN chunks t ON t.id = e.target_id "
                "WHERE s.id IN (" + ",".join("?" * len(win_ids)) + ") LIMIT 400",
                tuple(win_ids)):
            rel = str(r[2] or "")
            rel_out = "supersedes" if rel == "supersedes" else ("disagrees_with" if "disagrees" in rel else "related")
            add_edge("g-evo2-" + _hash6(r[0] + r[1] + rel_out), r[0], r[1], rel_out, 1.0)

    # mentions 边（chunk_entities 窗口内，配额内按 canon 优先）
    if win_ids:
        for r in conn.execute(
                "SELECT chunk_id, canon FROM chunk_entities WHERE chunk_id IN ("
                + ",".join("?" * len(win_ids)) + ") LIMIT 3000", tuple(win_ids)):
            if len(edges) >= budget:
                break
            eid = ent_id.get(r[1])
            if not eid:
                continue
            add_edge("g-m-" + _hash6(r[0] + r[1]), r[0], eid, "mentions", 1.0)

    # bonds（relations 六行 → 契约 Bond）
    bonds = []
    for r in _rows(conn, "SELECT * FROM relations"):
        a, b = _contract_id(r.get("agent_a")), _contract_id(r.get("agent_b"))
        if a not in CONTRACT_AGENT_IDS or b not in CONTRACT_AGENT_IDS:
            continue
        bonds.append({
            "a": a, "b": b,
            "affinity": round(float(r.get("sentiment") or 0), 3),
            "trust": round(float(r.get("trust") or 0.5), 3),
            "favor_debt": 0.0,  # debt 列存 JSON 无数值语义；favor 演化接 relations 后补
            "dm_count": 0,      # TODO：按 dm 房 messages 计数（rooms 播种后接）
            "dreams": 0,
            "last_dm": _iso(r.get("last_interaction")),
        })
    return {"ok": True, "entities": entities, "memories": memories,
            "edges": list(edges.values()), "bonds": bonds}