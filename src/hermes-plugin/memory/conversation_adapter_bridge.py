# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · conversation_adapter_bridge.py（CST01 · 宿主 adapter → 证据 的接线层）
#
# ▍这是什么
# ① 干什么：把宿主 adapter 的入站消息翻成 ConversationEvent v1，再交给
#    memory.conversation_capture 落库；并负责把宿主 SessionDB 的**可回查地址**
#    （host locator）一并记下。
# ② 技术栈：纯 Python；**不在模块级 import 宿主**（宿主不在 sys.path 时也要能 import）。
# ③ 依赖：memory.conversation_capture（唯一写入方）。
# ④ 给谁看：宿主接线者（isolated_host_feishu.py 是它的真实调用示范）。
#
# ▍R01 返修 · 三条“不能糊”的点
#   ① **原话 ≠ 加工后的 text。** 宿主 _process_inbound_message 里的 text 已经去过边缘
#      @点名、并且可能被塞进 `[提到你]` 之类的提示（adapter.py:3334–3347）。
#      把它当“逐字原话”存下来 = 存了一份**加工品**还标着原文。所以本模块要求
#      调用方**分别**给出 raw_text（平台原始正文）与 processed_text（宿主加工后），
#      event.text 只收 raw_text；processed_text 只以指纹形式进 locator 备查。
#   ② **locator 必须是真的能回查的地址**，不是 `raw_message_present=True` 这种“它在某处”。
#      宿主原件在 SessionDB（session_id + 消息行），所以 locator 至少要能指过去；
#      拿不到就如实标 locator_kind="unknown" 并写明原因，不编造。
#   ③ **opreation_id 必须含完整来源边界**（tenant/bot/chat/message/revision），否则跨
#      tenant/bot 的同名消息 ID 会撞键、把不同来源的两条消息当成同一次操作（R03 实测）。
#
# ▍接线铁律（改前必读）
# 1. **best-effort，绝不阻塞/绝不中断消息流。** 任何异常只 log，然后 return None。
# 2. **身份来自 adapter 配置，不来自事件。** 不读 user_name 当 principal。
# 3. **不扩大群监听。** 只翻译“adapter 已经收到的那一条”，不额外订阅、不补拉历史。
#
# ▍外号表
# "host event" → 宿主 gateway.platforms.base.MessageEvent
# "raw_text"   → 平台原始正文（未经点名剥离/提示注入）
# "接线点"     → 宿主 feishu adapter 的 _process_inbound_message 里 dispatch 之前
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-R01/R03）：raw_text 与 processed_text 分开；
#   locator 必须给可回查地址（拿不到就标 unknown）；operation_id 补全来源边界*
# *创建：2026-10-16 · 小D（CST01）：宿主 MessageEvent → ConversationEvent 的只读翻译*
# ================================================================
"""CST01 宿主 adapter 接线层（只读翻译 + best-effort capture）。"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from memory import conversation_capture as cc

logger = logging.getLogger("xtmem.conversation_bridge")

# 宿主 chat_type 取值 → ConversationEvent.chat_type（参考 DDL 的 CHECK 只认这三个）。
_CHAT_TYPE_MAP = {
    "p2p": "dm",
    "dm": "dm",
    "private": "dm",
    "group": "group",
    "topic": "thread",
    "thread": "thread",
}

LOCATOR_KIND_SESSION_DB = "session_db_message_row"
LOCATOR_KIND_UNKNOWN = "unknown"

# operation_id 的字段分隔符（F02）：外部字段一律先百分号编码，编码后不含它。
OP_FIELD_SEP = "~"


def _op_field(value: Any) -> str:
    """operation_id 的字段编码（F02）：百分号编码，杜绝拼接歧义。

    为什么不能直接拼：外部字段（chat_id / message_id / profile）**可能自带分隔符**。
    若用冒号拼，chat="a:b" + msg="c" 与 chat="a" + msg="b:c" 会得到同一个键
    ⇒ 两个不同来源被当作同一次操作（丢事件 / 串来源）。

    做法：按 UTF-8 百分号编码（safe="" 会把 separators 与 % 一并编掉），再用
    OP_FIELD_SEP 连接。于是每个字段编码后**不含**分隔符 ⇒ 拼接无歧义、可逆、纯函数。
    用 quote 而不是 quote_plus：空格编成 %20，避免再引入一层 "+" 歧义。
    """
    return quote(str(value), safe="")


def map_chat_type(host_chat_type: Any) -> str:
    """宿主 chat_type → dm/group/thread。**认不出就是 unknown，不猜成 group**。

    ⚠️【不】把认不出的值当 group 处理：把私聊错标成群 = 把 owner DM 的内容摆到群范围下，
       正是 Spec §6.2 要防的披露事故。
    """
    key = str(host_chat_type or "").strip().lower()
    return _CHAT_TYPE_MAP.get(key, "unknown")


def _reader_scope(chat_type: str, chat_id: str, sender_principal: str) -> Dict[str, str]:
    """来源范围（可见范围）：群/线程 → group/chat_id；私聊 → dm_owner/发言人；其余 → unknown。

    ⚠️ 私聊的 scope id 用**发言人**而不是 bot：own-DM 的阅读权归那个人（Spec §6.1）。
    """
    if chat_type in ("group", "thread"):
        # 线程仍是群内可见：范围挂在群上（thread 只是话题细分，不缩小可见性）。
        return {"kind": "group", "id": str(chat_id)}
    if chat_type == "dm":
        return {"kind": "dm_owner", "id": str(sender_principal)}
    return {"kind": "unknown", "id": str(chat_id)}


def build_locator(*, session_id: Optional[str], host_message_row_id: Any = None,
                  raw_text: Optional[str] = None, processed_text: Optional[str] = None,
                  reason: str = "") -> Dict[str, Any]:
    """构造 host locator（可回查宿主原件的地址）。

    ⚠️【不】用 `{"raw_message_present": True}` 充当 locator：那只说明“内存里当时有个对象”，
       事后**回查不到**任何东西 —— 独立验收明确把这一条列为 R01 的缺口。

    取数口径（Spec §4.3）：原件在宿主 SessionDB，XTMEM 只留**引用**。
    所以 locator 里放 session_id + 行标识；两者都拿不到时标 unknown 并写明原因，
    由调用方/部署侧决定是否补——**不臆造**一个看起来像地址的东西。
    """
    loc: Dict[str, Any] = {"locator_kind": LOCATOR_KIND_UNKNOWN, "reason": reason or ""}
    if session_id:
        loc["session_id"] = str(session_id)
    if host_message_row_id is not None:
        try:
            loc["host_message_row_id"] = int(host_message_row_id)
        except (TypeError, ValueError):
            loc["host_message_row_id"] = str(host_message_row_id)
    if session_id and host_message_row_id is not None:
        loc["locator_kind"] = LOCATOR_KIND_SESSION_DB
    elif not session_id:
        loc["reason"] = loc["reason"] or "host_session_id_unknown"
    else:
        loc["reason"] = loc["reason"] or "host_message_row_id_unknown"
    # 加工痕迹只留**指纹**，不落正文：正文该在宿主那边，XTMEM 不复制第二份可编辑的话。
    if raw_text is not None:
        loc["raw_text_sha256"] = hashlib.sha256(str(raw_text).encode("utf-8")).hexdigest()[:16]
    if processed_text is not None:
        loc["processed_text_sha256"] = hashlib.sha256(
            str(processed_text).encode("utf-8")).hexdigest()[:16]
        loc["text_was_processed"] = str(raw_text) != str(processed_text)
    return loc


def build_event(*, adapter: cc.AdapterIdentity, raw_text: Optional[str] = None, chat_id: str,
                chat_type: str,
                sender_principal: str, message_id: str, source_revision: int = 1,
                text: Optional[str] = None, host_event: Any = None,
                session_id: Optional[str] = None,
                host_message_row_id: Any = None, processed_text: Optional[str] = None,
                source_timestamp: Optional[float] = None, reply_to_message_id: Optional[str] = None,
                root_message_id: Optional[str] = None, thread_id: Optional[str] = None,
                sender_kind: str = "human", identity_status: str = "verified",
                operation_id: Optional[str] = None, observe_allowed: bool = False,
                reply_allowed: bool = False, locator: Optional[Dict[str, Any]] = None,
                kind: str = "inbound", source_time_status: Optional[str] = None) -> cc.ConversationEvent:
    """把宿主一条入站消息翻成 ConversationEvent v1。

    契约：
    入参：**全部显式传入**（不从 host_event 猜身份、不把加工后的 text 当原话）
          raw_text：平台原始正文（事件的 text 只收它）
          processed_text：宿主加工后的正文（只留指纹进 locator）
    返回：ConversationEvent（尚未落库）
    副作用：无
    谁调用：宿主 adapter 接线点（isolated_host_feishu.py）/ 测试
    改动影响：operation_id 构造规则一改，重复投递的幂等就破 —— 见下方注释。
    """
    # ⚠️ text= 是**旧签名的兼容别名**（验收方固定反例在用，改不了）。
    #    新代码一律用 raw_text，语义更明确：事件的 text 只收平台原文。
    if raw_text is None:
        raw_text = text
    if raw_text is None:
        raise cc.ValidationError("build_event 需要 raw_text（平台原始正文）")
    ctype = map_chat_type(chat_type)
    if ctype == "unknown":
        # 认不出就按最保守范围落：unknown scope。**不**沿用到群范围。
        scope = {"kind": "unknown", "id": str(chat_id)}
    else:
        scope = _reader_scope(ctype, chat_id, sender_principal)

    # kind 先定下来：它既是事件字段，也是 operation 键的一部分（F02）。
    kind_base = str(kind or "inbound")

    # ★F02：operation_id 必须含**完整来源边界**
    #   （profile/platform/tenant/bot/chat/message/revision/kind）
    # 病灶（独立验收实测）三条：
    #   ① 旧写法只含 profile/platform/message ID ⇒ 两个 tenant 下同名消息 ID 撞成
    #      同一 operation，第二条被当“重复”丢掉（跨 tenant 丢事件）；
    #   ② R03 补了 tenant/bot 但**漏了 chat**——而本段注释一直声称含 chat：
    #      **注释与代码不一致**，两个不同群里同名 message ID 仍撞键；
    #   ③ 分隔符只用冒号：由外部字段自带的冒号会让
    #      `a:b` + `c` 与 `a` + `b:c` 拼出同一个键（**拼接歧义**）。
    # ⇒ 修法：字段顺序固定 + 一律走 _op_field()（URL 百分号编码 + 波浪号分隔），
    #    保证“不同来源边界 ⇒ 不同键”且可逆；kind 也进键（同消息的撤回/编辑不是同一次操作）。
    #   ⚠️【不】把 time.time() 掺进来：那会让重放的“同一次投递”拿到新 operation，
    #      I05（重复重放只一次效果）当场失效。
    if not operation_id:
        operation_id = "capture:" + OP_FIELD_SEP.join([
            _op_field(adapter.profile), _op_field(adapter.platform),
            _op_field(adapter.tenant_id), _op_field(adapter.bot_id),
            _op_field(chat_id), _op_field(message_id),
            "r%d" % int(source_revision), _op_field(kind_base),
        ])

    if thread_id is None:
        thread_id = root_message_id
    if processed_text is None:
        processed_text = raw_text

    # 时间：平台给了就标 verified；没给就退 observed_only，
    # ⚠️【不】拿本机时间冒充平台时间。
    # source_time_status 显式传了就尊重（撤回/编辑这类生命周期事件常常没有平台时间戳）。
    time_status = str(source_time_status or (
        "verified" if source_timestamp is not None else "observed_only"))

    if locator is None:
        locator = build_locator(session_id=session_id, host_message_row_id=host_message_row_id,
                                raw_text=raw_text, processed_text=processed_text)

    return cc.ConversationEvent(
        operation_id=str(operation_id),
        profile=adapter.profile, platform=adapter.platform,
        tenant_id=adapter.tenant_id, bot_id=adapter.bot_id,
        chat_id=str(chat_id), chat_type=ctype,
        sender_principal_id=str(sender_principal), sender_kind=str(sender_kind),
        source_message_id=str(message_id), source_revision=int(source_revision),
        identity_status=str(identity_status), session_id=session_id,
        reply_to_message_id=reply_to_message_id, root_message_id=root_message_id,
        thread_id=thread_id, source_timestamp=source_timestamp,
        source_time_status=time_status, kind=kind_base, text=str(raw_text or ""),
        reader_scope=scope, disclosure_class="normal", host_locator=locator,
        observed_at=time.time(), observe_allowed=bool(observe_allowed),
        reply_allowed=bool(reply_allowed))


def capture_host_event(conn, *, adapter: cc.AdapterIdentity, **kwargs) -> Optional[Dict[str, Any]]:
    """接线点调用的**唯一**入口：best-effort 落库，任何失败都只 log（不打断消息流）。

    契约：
    入参：conn（公共写连接，调用方持有；可以是 None —— 表示本机没接记忆库，直接跳过）
          adapter + 与 build_event 同名的 kwargs
    返回：capture 结果 dict，或 None（跳过/失败）
    副作用：落一条证据（成功时）
    谁调用：宿主 adapter 的接线点
    改动影响：把它改成抛异常 = 让证据写入能拖垮用户消息处理；本函数**必须**吞异常。
    """
    if conn is None:
        return None
    try:
        event = build_event(adapter=adapter, **kwargs)
        return cc.capture(conn, event, adapter=adapter)
    except cc.WriteTransactionHeld as exc:
        # 复用中的连接恰好开着事务：这不是证据写失败，是“现在不能写”。
        # ⚠️ 这里**不**重连重试：重连会把消息处理拖慢，而这条证据本就 best-effort。
        logger.warning("conversation capture skipped (transaction held): %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 —— 旁路证据不许打断主链路（见文件头铁律 1）
        logger.warning("conversation capture skipped (%s): %s", type(exc).__name__, exc)
        return None


__all__ = ["map_chat_type", "build_locator", "build_event", "capture_host_event",
           "LOCATOR_KIND_SESSION_DB", "LOCATOR_KIND_UNKNOWN", "OP_FIELD_SEP"]
