# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · conversation_source.py（CST01 · 会话证据 → 受控来源引用）
#
# ▍这是什么
# ① 干什么：把一条 cs_events 证据翻译成 **读者集合 + 版本指纹**（SourceSnapshot），
#    并在 SourceResolver 上注册 kind='conversation_event' 的 provider。
# ② 技术栈：纯 Python + sqlite3；只读。
# ③ 依赖：memory.source_resolver（注册点）、memory.visibility（读者集哨兵）。
# ④ 给谁看：CST02/CST03 上下文装配者、M03 派生权限维护者。
#
# ▍为什么必须走 register_provider 而不是改 source_resolver 的 if-else
#   source_resolver.py 铁律 4 写死了：「新增来源类型（event / media）必须走 register_provider()，
#   【不】在本文件里 if-else 加分支」。本模块就是那个扩展点，一行都不用改它。
#
# ▍R01 返修 · 注册要接**真实入口**，不能只靠测试手工调
#   病灶：旧实现只有 `register()`，生产里没人调；真实 SourceResolver 初始只注册 chunk
#   （source_resolver.py 的 __init__），vector_store/context_builder 拿到的解析器上
#   **没有** conversation_event provider ⇒ “有权查询”在真实路径上根本走不到。
#   修法：提供 `ensure_registered(resolver)`（幂等）+`register_for_connection(conn)`
#   （走 get_resolver(conn) 的**按库分槽**单例，与引擎实际使用的解析器是同一个对象），
#   并暴露 `install()` 供 XTMEM 启动路径调用。
#   ⚠️【不】去改 source_resolver 的 __init__ 直接 import 本模块：那会造出
#      source_resolver ← conversation_source ← source_resolver 的循环导入。
#      扩展点就是 register_provider，从**外面**注册才是它设计的用法。
#
# ▍F01 续修（2026-10-16 · 独立验收实测，比 R01 更狠的地方）
#   R01 只做到“提供注册入口”，但 install() 只挂**默认槽**；引擎用的是按库槽。
#   实测：install() → installed=True，get_resolver(conn).resolve() 仍 present=False。
#   修法两条（缺一不可）：
#     ① install() 把**已存在**的全部按库槽补齐（source_resolver.iter_resolvers()）；
#     ② 接线层每次拿到引擎连接就调 register_for_connection(conn)（幂等），
#        覆盖“槽是后来才建的”这一情形。
#   ⚠️【不】指望“启动时装一次就够”：引擎会重连、会开第二个库，槽是动态建的。
#
# ▍R04 返修 · 最新状态缺失/不可读 ⇒ 保守拒读（fail-closed）
#   病灶两条：① 来源头缺失时沿用默认 'intact' ⇒ 撤回后继续可引用；
#             ② 读来源头抛异常（如 authorizer 拒读）时同样沿用默认 ⇒ 同上。
#   修法：默认值从 'intact' 改成 None；**只有明确读到 'intact' 才算有效**。
#   为什么默认必须是“最坏”而不是“最好”：这一层的读者集合直接决定派生记忆能被谁读。
#   默认放宽 = 一次读失败就能把已撤回的话重新发出去；默认收紧 = 最多让正常来源暂时
#   读不到，而“读不到”用户看得见、也查得出（version 指纹里带着 state）。
#
# ▍读者集合怎么算（本单最重要的取舍，改前必读）
#   cs_events 只有 reader_scope（kind/id）**没有成员表** —— 群的成员名单不在我们的库里。
#   所以本模块**不猜成员**，只按下面两条落：
#     · 已核验且未撤销的 principal grant（唯一硬证据）
#     · dm_owner：再加上 scope_id 那个人自己（own-DM 归本人）
#   ⚠️【不】把 group 直接当 ALL_READERS，也【不】按 scope 类型另行放宽：
#      群/能力面的成员名单我们**没有**，任何“按类型放宽”都是凭空授权。
#   ⚠️ 代价如实登记：没有登记 grant 时，群证据的读者集合是空的（不可被派生引用）。
#
# ▍版本指纹（T17 用）
#   = event_seq | source_revision | 最新态 | acl_revision | head_event_seq | 内容哈希 | 授权集合指纹
#   任一项变了就说明“旧计划不能再用”（撤回/编辑/权限收紧）。
#
# ▍外号表
# "证据来源"   → SourceRef(kind='conversation_event', id=<event_seq>)
# "来源头"     → cs_source_heads 的那一行（带 state/acl_revision）
# "安装"       → install()（把 provider 挂到进程内实际解析器上）
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-R01/R04 返修）：新增 install()/ensure_registered()/
#   register_for_connection()（接到真实解析器实例）；来源头默认值由 'intact' 改为“无”,
#   缺失或不可读一律 fail-closed*
# *创建：2026-10-16 · 小D（CST01）：conversation_event provider（读者集 + 版本指纹），
#   经 register_provider 扩展，不改 source_resolver*
# ================================================================
"""CST01 会话证据的来源解析 provider（受控来源引用）。"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Optional, Tuple

from memory.source_resolver import (
    SourceRef, SourceSnapshot, get_resolver, iter_resolvers, register_default_provider,
)
from memory.visibility import CONFINED, ReaderSet

PROVIDER_KIND = "conversation_event"

# 哪些 reader_scope_kind 允许“本人可读”。其余一律只认显式 grant。
_OWNER_READABLE_SCOPES = ("dm_owner",)


def _granted_readers(conn: sqlite3.Connection, event_seq: int) -> Tuple[ReaderSet, str]:
    """已核验且未撤销的 grant 主体集合 + 指纹。

    ⚠️ 只认 revoked_at IS NULL；撤销过的授权**不**从集合里“忘掉”而是明确排除。
    ⚠️ 授权表不可读 ⇒ 空集合（不是“没限制”）。
    """
    try:
        rows = conn.execute(
            "SELECT audience_kind, audience_id, grant_revision FROM cs_event_audiences "
            "WHERE event_seq=? AND revoked_at IS NULL ORDER BY audience_kind, audience_id",
            (int(event_seq),)).fetchall()
    except sqlite3.Error:
        return CONFINED, "grants_unavailable"
    principals = set()
    for row in rows:
        # 只有 principal 型授权能映射到读者身份；group/capability 是能力面，
        # 在没有成员表的情况下**不能**被展开成具体读者（展开 = 编成员名单）。
        if str(row["audience_kind"]) == "principal":
            pid = str(row["audience_id"] or "")
            if pid:
                principals.add(pid)
    fingerprint = "|".join(sorted(
        "%s:%s:%s" % (r["audience_kind"], r["audience_id"], r["grant_revision"]) for r in rows))
    return frozenset(principals), fingerprint


def resolve_conversation_event(conn: Optional[sqlite3.Connection], ref: SourceRef) -> SourceSnapshot:
    """SourceRef(kind='conversation_event') → (读者集合, 版本)。

    契约：
    入参：conn（sqlite3 连接，由 SourceResolver 传入）
          ref（id 是 cs_events.event_seq 的字符串形式）
    返回：SourceSnapshot（查不到 / 连接缺失 / 最新态不可读 一律 present=False + CONFINED）
    副作用：无（纯只读）
    谁调用：SourceResolver.resolve()（注册后自动）
    改动影响：读者集合放宽 = 越权（派生记忆拿到不该拿的来源）；收紧 = 来源不可用。
    """
    if conn is None:
        return SourceSnapshot(ref=ref, present=False, readers=CONFINED, revision="", state="")

    try:
        seq = int(str(ref.id))
    except (TypeError, ValueError):
        return SourceSnapshot(ref=ref, present=False, readers=CONFINED, revision="", state="")

    try:
        row = conn.execute(
            "SELECT event_seq, profile, platform, tenant_id, bot_id, chat_id, source_message_id, "
            "reader_scope_kind, reader_scope_id, content_hash, source_revision "
            "FROM cs_events WHERE event_seq=?", (seq,)).fetchone()
    except sqlite3.Error:
        # 查询失败 ≠ 来源共享。保守围栏（source_resolver 铁律 3 同款）。
        return SourceSnapshot(ref=ref, present=False, readers=CONFINED, revision="", state="")

    if row is None:
        return SourceSnapshot(ref=ref, present=False, readers=CONFINED, revision="", state="")

    scope_kind = str(row["reader_scope_kind"] or "")
    scope_id = str(row["reader_scope_id"] or "")
    granted, grant_fp = _granted_readers(conn, seq)

    # ── 来源头（最新态）：R04 的核心改动 ────────────────────────────────
    # ⚠️ 默认是 None（“不知道”），**不是** 'intact'（“正常”）。
    #    旧实现默认 intact，于是“来源头行被删了”和“读来源头被拒”两种情形都
    #    被静默当成“这条还正常”，撤回白撤。
    head_state: Optional[str] = None
    head_acl = 1
    head_seq = seq
    head_read_failed = False
    try:
        head = conn.execute(
            "SELECT state, acl_revision, event_seq FROM cs_source_heads "
            "WHERE profile=? AND platform=? AND tenant_id=? AND bot_id=? AND chat_id=? "
            "AND source_message_id=?",
            (row["profile"], row["platform"], row["tenant_id"], row["bot_id"],
             row["chat_id"], row["source_message_id"])).fetchone()
        if head is not None:
            head_state = str(head["state"])
            head_acl = int(head["acl_revision"])
            head_seq = int(head["event_seq"])
    except sqlite3.Error:
        head_read_failed = True

    # 读者集合只有两条来源：**已核验 grant**（唯一硬证据）+ own-DM 的本人。
    readers: ReaderSet
    if scope_kind in _OWNER_READABLE_SCOPES and scope_id:
        readers = frozenset(set(granted) | {scope_id})
    else:
        readers = frozenset(granted)

    # 生命周期围栏：只有**明确读到** intact 才放行。
    if head_state is None:
        readers = CONFINED
    elif head_state in ("retracted", "redacted", "revision_uncertain"):
        readers = CONFINED

    revision = "|".join([
        "seq=%d" % int(row["event_seq"]),
        "rev=%d" % int(row["source_revision"]),
        "state=%s" % (head_state if head_state is not None else "UNKNOWN"),
        "acl=%d" % head_acl,
        "head=%d" % head_seq,
        "hash=%s" % str(row["content_hash"] or "")[:16],
        "grants=" + hashlib.sha256(grant_fp.encode("utf-8")).hexdigest()[:16],
        "head_err=%d" % (1 if head_read_failed else 0),
    ])

    return SourceSnapshot(
        ref=ref, present=True, readers=readers, revision=revision,
        state=(head_state or "unknown"), namespace=str(row["profile"] or ""),
        visibility=scope_kind,
    )


def ensure_registered(resolver: Any) -> Any:
    """幂等注册到**给定**解析器实例（重复调用无害；换库换实例时各注册各的）。

    ⚠️ 只挂这一个实例 —— “以后新建的槽也要有”由 install() 的
       register_default_provider() 负责，别指望本函数顺手包办。
    """
    if resolver is None:
        return resolver
    providers = getattr(resolver, "_providers", None)
    if isinstance(providers, dict) and providers.get(PROVIDER_KIND) is resolve_conversation_event:
        return resolver
    resolver.register_provider(PROVIDER_KIND, resolve_conversation_event)
    return resolver


def register_for_connection(conn: sqlite3.Connection) -> Any:
    """把 provider 注册到**该连接所属库**的解析器单例上（R01）。

    为什么用 get_resolver(conn)：source_resolver 按库路径分槽，引擎（vector_store /
    context_builder）用的就是同一个 `get_resolver(conn)` 实例 ⇒ 在这里注册
    = 真实引擎查询路径立刻能看到 conversation_event 来源。
    """
    return ensure_registered(get_resolver(conn))


def register(resolver: Any = None) -> Any:
    """注册到给定解析器；不传则注册到进程默认解析器（无连接的兜底实例）。"""
    return ensure_registered(resolver if resolver is not None else get_resolver())


def install() -> dict:
    """XTMEM 启动路径调用入口（R01 / F01 续修）。

    契约：
    入参：无
    返回：{"installed": bool, "kind": str, "note": str, "slots": dict}
    副作用：把 provider 挂到 ①进程默认解析器 ②**当前已经存在的全部按库解析器实例**上；
            此后新建的按库实例在 register_for_connection() 补挂。
    谁调用：XTMEM 引擎/插件启动（启动装配见 conversation_bootstrap.wire_capture()）
    改动影响：不装 = 真实路径上 conversation_event 来源“查不到”，派生记忆无法引用原话。

    ⚠️ 为什么不能只挂默认槽（F01 独立验收实测的假就绪）：
       旧实现只挂 get_resolver()（无连接默认实例），而引擎实际用的是 get_resolver(conn)
       的**按库槽** —— 两者是不同对象。实测：install() 返回 installed=True，但
       get_resolver(conn).resolve(...) 仍然 present=False。
       ⇒ 现在把**已存在的**按库槽一并补齐（iter_resolvers()），并由接线层在每次拿到
         引擎连接时调 register_for_connection()，覆盖“槽是后来才建的”这一情形。

    ⚠️ 本函数**不抛异常**：启动期注册失败不该让整个插件起不来（旁路能力）。
       失败只如实回报 installed=False，由 doctor/日志去发现。
       返回值里的 slots 明细必须如实：installed=True 只代表“默认槽挂上了”。
    """
    try:
        # ★F01 的关键一步：登记为**进程级默认** provider。
        #   这一下同时做成两件事：①把已存在的槽（含引擎正拿着的按库实例）立刻补挂；
        #   ②此后**新建**的每个 resolver 在构造时就带上本类型 —— 堵死“注册早于建槽”。
        bound = register_default_provider(PROVIDER_KIND, resolve_conversation_event)
        return {"installed": True, "kind": PROVIDER_KIND,
                "note": "已登记为进程级默认 provider；已存在的 %d 个解析器实例已补挂" % bound,
                "slots": {"existing_bound": int(bound), "failed": []}}
    except Exception as exc:  # noqa: BLE001 —— 启动期旁路注册失败不拖垮插件
        return {"installed": False, "kind": PROVIDER_KIND,
                "note": "注册失败：%s: %s" % (type(exc).__name__, exc),
                "slots": {"existing_bound": 0, "failed": []}}


__all__ = ["PROVIDER_KIND", "resolve_conversation_event", "register",
           "ensure_registered", "register_for_connection", "install"]
