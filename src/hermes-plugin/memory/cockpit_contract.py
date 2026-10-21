# -*- coding: utf-8 -*-
"""memory/cockpit_contract.py — 照渊台契约组装层（P5.1-2b 契约缝合，2026-09-02，小D）

把 XTMEM 真库（chunks/messages/rooms/widgets/entities/tag_edges/relations/…）组装成
照渊台前端契约定死的八端点形状。契约唯一出处：
    <WORKBENCH>\\src\\api\\contract.ts + endpoints.ts（字段级）
mock（src/mock/engine.ts）是行为基准；fixtures/*.json 是返回样本。
本模块是纯函数层：入参 sqlite3.Connection（读或写）+ 探活 dict → 出参契约形状 dict，
api_server 只做薄路由。改字段名/形状前必须对照 endpoints.ts，禁止自造形状。

设计裁定（Q1-Q6 后端拍板）见 <PROJECT_ROOT>\\new\\XTMEM-驾驶舱-照渊台契约缝合-设计-v1.md。
"""
from __future__ import annotations

import hashlib
import json
import os
import math
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

# ── 三域翻译（照渊台 AgentId / 库 namespace / A2A peer）──────────
# ⚠️ "今日"窗口按北京时间(UTC+8)划日界（2026-09-05 统一）：此前 time.time()%86400 是 UTC 日界，
# 北京每天 00:00–07:59 的事件会被漏出"今日"统计（evicted_today/done_today 少算半天）。
_BJ_OFFSET_S = 8 * 3600
# 展示角色由使用者配置；未配置不探测任何私人服务端口。
_PUBLIC_AGENTS = json.loads(os.environ.get("XTMEM_AGENTS_JSON", "[]"))
CONTRACT_AGENT_IDS = [str(a["id"]) for a in _PUBLIC_AGENTS]
NS_OF = {str(a["id"]): str(a.get("namespace", a["id"])) for a in _PUBLIC_AGENTS}
ID_OF_NS = {value: key for key, value in NS_OF.items()}
A2A_OF = {str(a["id"]): str(a.get("peer", a["id"])) for a in _PUBLIC_AGENTS}
PROBE_PORTS = {str(a["id"]): int(a["port"]) for a in _PUBLIC_AGENTS if a.get("port")}



# 灯静态源：epithet/presence/channel/wake_p50_ms/clock_rate 抄照渊台 fixtures 预设
# （wake_p50 是实测中位数——fixtures 生成器注释口径）。lamp/can_answer_now 是动态位。
AGENT_META = [{"name": str(a.get("name", a["id"])), "presence": "session", "channel": "configured", "wake_p50_ms": 0, "clock_rate": 1.0, "can_answer_now_base": False, "epithet": "", **a} for a in _PUBLIC_AGENTS]

# 灯光名（照渊台契约 AGENT_META 派生；前端 NAMES 一致）：createRoom 房间命名用
AGENT_NAME = {m["id"]: m["name"] for m in AGENT_META}

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
    # P5.1-2c 主链：发帖即上工——A2A 触发链路把帖子状态写成链上位置，驾驶舱能看到走链
    "waking": "waking",       # 对端被唤醒中
    "streaming": "streaming", # 对端边干边回传
    "replied": "replied",     # 对端已回
    "timeout": "timeout",     # 叫不醒降级留帖
}

# 谁看见（audience）：照 FATE 表（见前端 fates.ts），只按 kind 推
_PERSIST_KINDS = frozenset({"task", "decision"})
_NOTIFY_KINDS = frozenset({"task"})
_NEEDS_ACK_KINDS = frozenset({"task"})
_PAIR_AUDIENCE_KINDS = frozenset({"msg", "task"})

# 未知 from/to 的归一：bus 自定义 actor 原样透传（运行期容忍，前端对未知 id 显示 id 原文）
_SENDER_ALIAS = {}

# 实体 kind 启发（实体表无 kind 列；canon 名单 → agent/person/project，其余 term）
_AGENT_CANONS = frozenset(CONTRACT_AGENT_IDS)
_PERSON_CANONS = frozenset()
_PROJECT_CANONS = frozenset({"XTMEM"})

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

# ── R-02（2026-09-03）角色级可见性 ─────────────────────────
ADMIN_VIEWER = "__admin__"


def _is_admin(viewer):
    """管理员判定。

    ★M03（2026-10-13）收口：**空 viewer 不再算管理员**（原实现 `not viewer` 即 True）。

    原行为与实测后果（探针 <TEMP>\\DSH\\m03-probe8.py）：
        _is_admin(None) = True ，_is_admin("") = True
        chunk_visible("assistant", "private", "")  → True
        而 canonical(B00/§6.1) visibility_ok("private", "assistant", "") → False
      ⇒ **同一个问题两套答案**，违反 M03 专项验收「所有读入口同权限」。
      更实际的风险：REST token 表允许 `{"viewer": "", "admin": false}` 这种配置
      （见 api_server._require_auth：`viewer = str(role.get("viewer") or "")`），
      配上本函数就把一个**非管理员 token 顶成了全可见视角**——是提权，不只是口径分歧。

    为什么是"None 也一并收紧"而不是只收紧 ""：
      本文件所有对外函数的 viewer 形参**缺省值都是 ADMIN_VIEWER**（='__admin__'），
      所以"调用方没传"这件事会走缺省值、根本不会产生 None。
      None 只可能来自"显式传了 None"，那属于"明确说了不知道身份"——按 fail-closed 处理才对。
      ⇒ 收掉 None 不会误伤任何"省略参数"的内部调用点。

    ⚠️【不】把 `not viewer` 写回来。要管理员视角请显式传 '__admin__'；
       要内部全可见请显式传 ADMIN_VIEWER。让它出现在调用点上，review 才看得见。

    保留的兼容面：ADMIN_VIEWER('__admin__') / 'admin' / 'owner' 三个显式值仍判管理员
    （R-02 验收修正：admin/owner token 在 API 层已被归一成 __admin__，此处兜历史调用）。
    """
    return viewer in (ADMIN_VIEWER, "admin", "owner")


def msg_visible(row, viewer):
    """messages 一行对 viewer 是否可见（R-02：shared_with 越权读已实机复现，收口）。

    shared → 全可见；private → 仅 owner(from_agent)==viewer；
    shared_with → owner 本人或名单内可见；未知 visibility → fail-closed 不可见。

    ⚠️ messages 表与 chunks 表是**两套不同的授权模型**（前者 owner=人/agent 名，
       后者 owner=namespace），所以本函数**不能**直接转发 chunks 的 visibility_ok。
       但"空 viewer 一律不可见"这条是两者共有的 fail-closed 底线（M03 已对齐）。
    """
    if _is_admin(viewer):
        return True
    if not viewer:
        # ★M03：空/None viewer 直接不可见（与 chunks 侧 visibility_ok 同款 fail-closed）
        return False
    vis = str(row.get("visibility") or "shared")
    owner = str(row.get("from_agent") or "")
    if vis == "shared":
        return True
    if vis == "private":
        return owner == viewer
    if vis == "shared_with":
        allowed = [str(x).strip() for x in _loads_list(row.get("shared_with"))]
        return owner == viewer or viewer in allowed
    return False


def chunk_visible(namespace, vis, viewer):
    """chunks 一行对 viewer 是否可见。

    ★M03（2026-10-13）：**改为直接转发全库唯一实现 memory.visibility.visibility_ok**，
    不再自己重写一份（原实现是第二份判定，与 canonical 在空 viewer 处分歧）。

    为什么要转发而不是"抄得一样"：抄一遍就等于留了一个"以后两边会再次漂移"的口子——
    M03 的病根（min_visibility 与 visibility_ok 两套口径并存）就是这么来的。
    转发之后，本函数与 CLI/provider/引擎的读权限**在实现层就是同一个函数**，
    将来 canonical 收紧它自动跟着收紧。
    """
    from memory.visibility import visibility_ok
    if _is_admin(viewer):
        return True
    return bool(visibility_ok(vis or "", namespace or "", viewer or ""))



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


def to_message(row: Dict[str, Any], viewer: Optional[str] = None) -> Dict[str, Any]:
    """messages 一行 → 契约 Message。私密：非管理员 content=null + redacted；
    管理员（_is_admin，含 None 历史兜底 / __admin__ / admin / owner）可见正文。"""
    _raw_vis = str(row.get("visibility") or "")
    vis = _raw_vis if _raw_vis in ("shared", "private", "shared_with") else "private"
    sealed = (vis == "private") and not _is_admin(viewer)
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
        "visibility": vis,
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
    # 🔴【2026-09-14 改道（顶模裁定二-1）】演化统计从 memory_evolution 边表切到 chunks 指针。
    #   原口径读边表（含 98.7% 无依据的 680 条），展示层报的数字会虚高；指针是既成退役事实。
    #   口径：supersedes = 有 superseded_by 的条数（真被接替的）；无依据边不再计入任何一档。
    #   ⚠️ 保留 by_rel 三键结构（前端契约），disagrees_with 恒 0（该语义只在废弃边表里存在）。
    try:
        _ptr_rows = conn.execute(
            "SELECT retire_reason, COUNT(*) c FROM chunks "
            "WHERE superseded_by IS NOT NULL AND superseded_by!='' GROUP BY retire_reason").fetchall()
    except Exception:
        _ptr_rows = []
    evo_total = 0
    by_rel = {"supersedes": 0, "disagrees_with": 0, "related": 0}
    for r in _ptr_rows:
        n = r[1]
        evo_total += n
        by_rel["supersedes"] += n  # 指针链全部是"被接替"语义
    try:
        relations = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    except Exception:
        relations = 0
    day0 = time.time() - ((time.time() + _BJ_OFFSET_S) % 86400)  # 北京日界（0点）折算 epoch
    try:
        evicted = conn.execute(
            "SELECT COUNT(*) FROM xtmem_events WHERE kind='forget' AND ts>=?", (day0,)).fetchone()[0]
    except Exception:
        evicted = 0
    last_w = conn.execute("SELECT MAX(ts) FROM xtmem_events WHERE kind='write'").fetchone()[0]
    # refined_hit_rate：诚实近似——用「被显式引用的记忆里精炼层占比」（memory_used 事件）
    # 反映精炼层被实际使用的比例；无引用时 0。检索命中 tier 埋点后续单独接。
    try:
        used_rows = conn.execute(
            "SELECT COALESCE(ch.tier,'raw') tier, COUNT(*) c FROM xtmem_events e "
            "LEFT JOIN chunks ch ON ch.id = e.target_chunk "
            "WHERE e.kind='memory_used' AND e.target_chunk IS NOT NULL GROUP BY tier").fetchall()
        _tot = sum(int(r["c"] or 0) for r in used_rows)
        _ref = sum(int(r["c"] or 0) for r in used_rows if str(r["tier"]) == "refined")
        refined_hit_rate = round(_ref / _tot, 4) if _tot else 0.0
    except Exception:
        refined_hit_rate = 0.0
    return {
        "total": total, "raw": raw, "refined": refined, "domains": domains, "clocks": clocks,
        "evo_edges": evo_total, "evo_by_rel": by_rel, "relations": relations,
        # 蒸馏无独立事件（引擎蒸馏走 write 链）→ 以最近 write 时刻近似，注释如实
        "last_distill": _iso(last_w) or _now_iso(),
        "evicted_today": evicted,
        "refined_hit_rate": refined_hit_rate,
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


def widgets_of(conn, limit: int = 8, viewer: Optional[str] = ADMIN_VIEWER) -> List[Dict[str, Any]]:
    rows = _rows(conn,
                 "SELECT * FROM widgets WHERE handled=0 ORDER BY ts DESC LIMIT ?", (limit,))
    return [widget_of(r) for r in rows if msg_visible(r, viewer)]


def task_counts_of(conn) -> Dict[str, int]:
    open_n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state IN ('pending','')").fetchone()[0]
    waiting = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state=''").fetchone()[0]
    day0 = time.time() - ((time.time() + _BJ_OFFSET_S) % 86400)  # 北京日界（0点）折算 epoch
    done = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='task' AND state IN ('ack','done') AND ts>=?",
        (day0,)).fetchone()[0]
    return {"open": open_n, "waiting_ack": waiting, "done_today": done}


def _feishu_mirror_repair(recent_rows: list) -> list:
    """飞书镜像回信修正（2026-09-07）：dm:* 房间里，若某 waking/timeout 帖之后出现了
    同房间的 [飞书镜像] 副本，则该帖在展示层视为已回复（replied）。
    背景：dm:assistant 走身份桥+飞书回复后，A2A watchdog 常常先判 timeout，
    但玄铁的飞书回复随后由 cockpit_mirror 回流——实际有回复，不该亮 timeout。
    只改展示层（recent/to_message 的入参），不改库。"""
    try:
        # 倒序扫描：镜像帖出现后，其后（时间上更早的）同房间差帖全部视为已回复
        mirror_rooms = set()
        fixed = []
        for row in recent_rows:  # recent_rows 按 ts DESC 排列
            content = str(row.get("content") or "")
            room = str(row.get("room_id") or "")
            if content.startswith("[飞书镜像]") and room:
                mirror_rooms.add(room)
                row = dict(row)
                row["state"] = "replied"
            elif room in mirror_rooms and str(row.get("state") or "") in ("waking", "timeout"):
                row = dict(row)
                row["state"] = "replied"
            fixed.append(row)
        return fixed
    except Exception:
        return recent_rows


def cockpit_state(conn, probes: Optional[Dict[str, bool]] = None,
                 viewer: Optional[str] = ADMIN_VIEWER) -> Dict[str, Any]:
    unread_total = conn.execute("SELECT COUNT(*) FROM messages WHERE read=0").fetchone()[0]
    recent_rows = _rows(conn, "SELECT * FROM messages ORDER BY ts DESC LIMIT 64")
    recent_rows = [r for r in recent_rows if msg_visible(r, viewer)][:24]
    recent_rows = _feishu_mirror_repair(recent_rows)
    return {
        "ok": True,
        "now": _now_iso(),
        "liveness": _liveness_of(conn),
        "agents": agent_rows(conn, probes),
        "vitals": vitals_of(conn),
        "unread_total": unread_total,
        "task_counts": task_counts_of(conn),
        "recent": [to_message(r, viewer) for r in recent_rows],
        "widgets": widgets_of(conn, viewer=viewer),
    }


# ── 房间 / 上下文 ─────────────────────────────────────────

def _topic_title(conn, room_id, topic_id, viewer: Optional[str] = ADMIN_VIEWER):
    """话题标题取「对 viewer 可见」的首条消息；无可见首帖回退话题 id（R-02：防私密首帖泄漏）。"""
    rows = _rows(conn,
        "SELECT content, from_agent, visibility, shared_with FROM messages "
        "WHERE room_id=? AND topic_id=? ORDER BY ts ASC LIMIT 8",
        (room_id, topic_id))
    title_src = ""
    for rr in rows:
        if msg_visible(rr, viewer):
            title_src = str(rr["content"] or "")
            break
    if not title_src:
        return topic_id
    t = title_src.strip().replace("\n", " ")
    # 标题太短/无意义（如测试占位 "1."）→ 回退话题 id，避免列表显示垃圾标题
    if len(t) <= 2 or not re.search("[一-鿿A-Za-z]", t):
        return "话题 " + str(topic_id)
    return (t[:24] + "…") if len(t) > 24 else t


def room_of(conn, row: Dict[str, Any], viewer: Optional[str] = ADMIN_VIEWER) -> Dict[str, Any]:
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
            "title": _topic_title(conn, room_id, t["topic_id"], viewer),
            "msg_count": t["c"], "private_count": int(t["pc"] or 0),
            "last_at": _iso(t["last"]),
        })
    # 空话题兜底：房间尚无任何消息时给一条首话题（真后端话题来自消息 topic_id 聚合；
    # mock 有首话题，真后端新房 topics=[] 会让前端"无话题可切"，此处兜底为一条）
    if not topics:
        topics = [{
            "id": "t-1",
            "title": str(row.get("name") or room_id) + " · 首话题",
            "msg_count": 0, "private_count": 0,
            "last_at": _iso(row.get("ts")),
        }]
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


def rooms_of(conn, viewer: Optional[str] = ADMIN_VIEWER) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM rooms ORDER BY ts DESC")
    return {"ok": True, "rooms": [room_of(conn, r, viewer) for r in rows]}


def room_context_of(conn, room_id: str, topic_id: Optional[str], n: int = 40,
                   viewer: Optional[str] = ADMIN_VIEWER) -> Dict[str, Any]:
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
    rows = [r for r in rows if msg_visible(r, viewer)]
    return {"ok": True, "room": room_of(conn, row, viewer), "topic_id": topic_id, "n": min(max(int(n), 1), 100),
            "messages": [to_message(r, viewer) for r in rows]}


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


def _memory_window(conn, viewer: Optional[str] = ADMIN_VIEWER) -> List[Dict[str, Any]]:
    """四灯 ns + active 的轻列候选 → 按(heat, updated_at)排序取窗口（Q1：500）。

    R-02：候选先按可见性过滤（shared / 本人 private / shared-with 名单），
    越权记忆不进入后续任何聚合与摘录。
    """
    ns = list(NS_OF.values())
    marks = ",".join("?" * len(ns))
    cand = _rows(conn,
                 "SELECT id, namespace, tier, importance, timestamp, updated_at, state, superseded_by, visibility "
                 "FROM chunks WHERE namespace IN (" + marks + ") AND state='active' "
                 "ORDER BY updated_at DESC LIMIT 800",
                 tuple(ns))
    if not _is_admin(viewer):
        cand = [c for c in cand if chunk_visible(c.get("namespace"), c.get("visibility"), viewer)]
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


def graph_of(conn, viewer: Optional[str] = ADMIN_VIEWER) -> Dict[str, Any]:
    win = _memory_window(conn, viewer)
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
        if not _is_admin(viewer) and not chunk_visible(c.get("namespace"), vis, viewer):
            continue  # 角色视角：越权记忆整条不出现（不留节点/不留摘录）
        sealed = (not _is_admin(viewer)) and not chunk_visible(c.get("namespace"), vis, viewer)
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
    # P1-9（2026-09-06 外部审计整改总单-4）：按 viewer 过滤——私有记忆贡献的实体不得
    # 出现在他人图（照 600-602 chunk_visible 范式）。实体可见 = 至少有一个 viewer 可见的
    # chunk 贡献它（chunk_entities→chunks 联查重算可见计数；管理员全局）。
    # ⚠️ 局限备注：count 重算走 LIMIT 前聚合，超大实体表性能可回看；tag_edges.cnt 无法
    # 逐边回源（表无 chunk 关联列），边侧用"两端实体可见"必要条件过滤（重构另立单）。
    if _is_admin(viewer):
        ents = _rows(conn,
                     "SELECT canon, count, last_seen FROM entities WHERE count>0 "
                     "ORDER BY count DESC LIMIT ?", (GRAPH_ENTITY_LIMIT,))
    else:
        # chunk_visible 是 Python 函数不能进 SQL——先拉贡献关系，Python 侧筛可见 chunk 集，
        # 再按可见集重算实体 count（私有记忆专属实体整体消失，连边随之消失）。
        contrib = _rows(conn,
                        "SELECT ce.chunk_id, ce.canon, c.namespace, c.visibility, c.state "
                        "FROM chunk_entities ce JOIN chunks c ON c.id = ce.chunk_id "
                        "WHERE ce.canon IN (SELECT canon FROM entities WHERE count>0)")
        _vis_chunks = {}
        for row in contrib:
            if row["state"] != "active":
                continue
            if not chunk_visible(row["namespace"], row["visibility"], viewer):
                continue
            _vis_chunks.setdefault(row["canon"], set()).add(row["chunk_id"])
        _cnt_rows = [
            {"canon": k, "count": len(v), "last_seen": None}
            for k, v in _vis_chunks.items()
        ]
        _cnt_rows.sort(key=lambda x: x["count"], reverse=True)
        ents = _cnt_rows[:GRAPH_ENTITY_LIMIT]
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
    # P1-9：边可见 = 两端实体都在 viewer 可见集（ent_id 已按 viewer 过滤——不可见端直接失配）
    for r in _rows(conn, "SELECT a, b, cnt FROM tag_edges ORDER BY cnt DESC"):
        if len(edges) >= budget:
            break
        ia, ib = ent_id.get(r["a"]), ent_id.get(r["b"])
        if not ia or not ib:
            continue
        w = float(r["cnt"] or 1)
        idf = min([x.get("idf", 0.5) for x in entities if x["id"] in (ia, ib)] or [0.5])
        add_edge("g-co-" + _hash6(r["a"] + r["b"]), ia, ib, "co", w, idf)

    # evo 边（窗口内 chunk↔chunk：只认 chunks.superseded_by 指针）
    # 🔴【2026-09-14 改道（顶模裁定二-1）】原第二段读 memory_evolution 边表已【删除】——
    #   边表 689 条 supersedes 中 98.7% 无正文依据，继续展示给人看 = 把"记错"摆到展示层。
    #   指针段（第一段）实测 100% 闭环，单独就够。要恢复边表展示须先证明其依据率（现 1.3%）。
    if win_ids:
        for r in conn.execute(
                "SELECT id, superseded_by FROM chunks WHERE id IN ("
                + ",".join("?" * len(win_ids)) + ") AND superseded_by!=''", tuple(win_ids)):
            tgt, src = r[0], r[1]
            if src in win_ids:
                add_edge("g-evo-" + _hash6(src + tgt), src, tgt, "supersedes", 1.0)

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