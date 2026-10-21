# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · conversation_capture.py（CST01 · 可信事件 capture + 受控查询）
#
# ▍这是什么
# ① 干什么：把**可信 adapter 实际收到**的一条消息规范化成 ConversationEvent v1（Spec §5.2）
#    并耐久落进 cs_events；提供受控的按原话查询与归属解析。
# ② 技术栈：纯 Python + sqlite3；时间统一 **epoch UTC 秒**（float）。
# ③ 依赖：memory.conversation_schema（feature 闸）、memory.vector_store（FTS 分词口径）。
# ④ 给谁看：宿主 adapter 接入者、CST02/CST03 下游、CST01 复核者。
#
# ▍三条不许越的线（改前必读）
# 1. **原文归宿主 SessionDB，本模块只存“收到的规范化证据快照 + locator”。**
#    所以这里**没有** update_event/delete_event/edit_event —— 原文不可原地改写
#    （触发器 cs_events_immutable 也挡着）。要改内容只能追加新 revision。
# 2. **模型/请求 args 不能自报身份。** 身份一律由 adapter 侧凭据绑定的 AdapterIdentity
#    提供；事件自报值只用于**对账**，对不上就 ACTOR_MISMATCH 拒收。
# 3. **没收到的不许编。** capture 只接“实际到达 adapter 的事件”。
#
# ▍R02 返修 · 事务拥有者必须唯一（血泪，见项目错题本 #2）
#   病灶：旧实现在**调用方的连接**上无条件 commit()，失败时无条件 rollback()。
#   实测三条都红：调用方先插入未提交的业务行，capture 成功后调用方 rollback 撤不掉它
#   （被 capture 提前结账了）；反向地，capture 失败会把调用方原有未提交行一起撤掉。
#   修法：唯一写入口 **自持事务**，进门先问 `conn.in_transaction`——
#     已在事务里 ⇒ 抛 WRITE_TRANSACTION_HELD（**拒绝替调用方结账**），而不是“顺手用它的”。
#   为什么不是退化成 SAVEPOINT：savepoint 只能保证“不提前提交”，但**失败时回滚谁**
#     仍然含糊（回滚到 savepoint 会连带丢掉调用方在 savepoint 之前的未提交修改吗？
#     不会——但调用方往往期望“整笔一起退”，而我们的契约是“证据是独立一笔”）。
#   把话说死比“看运气”强：辅助函数不替调用方记账，调用方也别指望我们替他退。
#
# ▍F02 续修（2026-10-16 · 独立验收实测，R03 只修了一半）
#   病灶两条：
#     ① **同来源分支**只比 content_hash，不核身份 —— 换 sender（或换 operation）但正文相同，
#        新请求被当作重复、回旧 event_seq：B 的新话被签成 A 的旧证据；
#     ② 桥接自动 operation 键的**注释说含 chat、代码里没有**（注释与代码不一致）。
#   修法：**所有幂等命中路径统一用同一完整身份判据**（operation 命中 / 来源命中 /
#        唯一约束冲突回查三处）；自动键补 chat 且用不可与内容混淆的边界分隔。
#   ⚠️ 判据是 `_same_identity()` 一处实现，**不**在分支里各写一份比较（写两份必漂移）。
#
# ▍R03 返修 · 幂等键必须绑**完整来源身份**，且并发下要回同一结果
#   病灶三条（独立验收实测）：
#     ① 命中 operation 只比 content_hash ⇒ 换 chat/sender/来源但正文相同，被别名成旧证据；
#     ② 桥接自动 operation 只含 profile/platform/message ID ⇒ 跨 tenant/bot 撞键丢事件；
#     ③ “先查再插”没在同一串行保护里 ⇒ 两连接同时查无旧行、各插一条，
#        一条成功一条 IntegrityError，**“重复投递返回同一结果”的接口承诺没达成**。
#   修法：
#     · operation_id 命中后**逐字段比对完整来源身份**（含 tenant/bot/chat/sender/revision/kind），
#       任一不同 ⇒ SourceRevisionConflict（**拒绝**别名，不是当好话收下）；
#     · 写路径先 BEGIN IMMEDIATE 拿写锁**再**查——查与插在同一串行区，第二条连接进锁后
#       重查必然看到第一条的结果 ⇒ 返回同一个 event_seq；
#     · 万一还是撞了唯一约束（别的路径写入/极端时序），**捕获 IntegrityError 后回头重查**，
#       查到同 identity ⇒ 当作 duplicate 返回，查不到才按 WRITE_FAILED 抛。
#   ⚠️ 幂等承诺的最终兜底是**库层唯一约束**（schema 里那份，R06 会核它在不在）。
#      Python 分支只是快手；并发下必然有穿过的时候，靠约束兜住，靠重查把结果对齐。
#
# ▍R04 返修 · 生命周期事件与“最新态缺失”
#   病灶两条：① 所有 kind 都把来源头写回 'intact'，撤回被存成“仍正常”；
#             ② 来源头缺失/读不到时沿用默认 'intact' ⇒ 撤回后继续可引用。
#   修法：撤回/编辑走**生命周期分支**更新来源头；相同或更旧的普通事件**不得复活**撤回态；
#        最新态缺失或不可读 ⇒ 保守围栏（见 conversation_source）。
#
# ▍R05 返修 · 普通查询与“管理取原件”分家
#   病灶：单条/来源/检索接口**没有可信当前调用者上下文**，search 只按调用方自报的
#         profile/scope 字符串过滤，不查授权、不看生命周期水位 ⇒ 撤回后仍能搜出全文。
#   修法：普通路径一律要 AuthContext（principal/profile/bot/scope/水位），
#         授权、状态、revision **在返回正文与 LIMIT 之前**生效；
#         无身份/缺权限 ⇒ 不返回正文、标题、计数或存在性。
#         “查原件”另走 get_evidence_original()，要求 admin_evidence 能力，明确标注是管理取数。
#
# ▍外号表
# "证据行"   → cs_events 的一行（不可原地改）
# "来源头"   → cs_source_heads 的一行（最新态）
# "就绪闸"   → conversation_schema.require_feature_schema()
# "受控查询" → query_events(auth=...)（普通路径）
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-F02）：同来源命中分支补齐完整身份判据（不再只比正文），
#   三处幂等命中路径统一走 _same_identity()*
# *更新：2026-10-16 · 小D（CST01-R02/R03/R04/R05 返修）：自持事务拒绝外层事务；幂等绑完整
#   来源身份 + 先持写锁再查 + 唯一约束冲突重查回同一结果；生命周期事件正确置撤回且不被
#   同版本普通事件复活；普通查询引入 AuthContext，授权/状态/水位先于正文与 LIMIT*
# *创建：2026-10-16 · 小D（CST01）：ConversationEvent v1 + 幂等落库 + 原话即时可查 +
#   来源归属分离 + feature 版本闸拒写*
# ================================================================
"""CST01 可信事件 capture（ConversationEvent v1）、生命周期与受控查询。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

from memory import conversation_schema as cs

FORMAT_VERSION = 1
CAPTURE_STATUS_CAPTURED = "captured"
CAPTURE_STATUS_DUPLICATE = "duplicate"

CHAT_TYPES = ("dm", "group", "thread")
SENDER_KINDS = ("human", "agent", "tool", "platform")
IDENTITY_STATUSES = ("verified", "partial", "unknown")
SOURCE_TIME_STATUSES = ("verified", "observed_only", "unknown")
EVENT_KINDS = ("inbound", "assistant_published", "message_edited", "message_retracted",
               "imported", "media_transcript", "platform_notice")
READER_SCOPE_KINDS = ("dm_owner", "group", "explicit", "agent_private", "knowledge_shared", "unknown")
DISCLOSURE_CLASSES = ("normal", "personal", "restricted", "unknown")

# 生命周期 kind：会改变“这条来源还有效吗”的事件。R04 的处理分支只认这几个。
LIFECYCLE_KINDS: FrozenSet[str] = frozenset({"message_retracted"})
# 撤回后期望落进来源头的状态。message_edited 不改可见性（内容变更是新 revision 的事），
# 所以不放进这张表 —— 放进去会把“编辑过”错判成“不可读”。
LIFECYCLE_STATE: Dict[str, str] = {"message_retracted": "retracted"}

# 身份字段：幂等比对必须**逐字段**核的完整来源边界。
# ⚠️【不】只比 content_hash：正文相同而来源不同 = **不同的事**，别名旧证据就是串来源。
_IDENTITY_FIELDS = ("profile", "platform", "tenant_id", "bot_id", "chat_id",
                    "sender_principal_id", "source_message_id", "source_revision", "kind")

# 管理取数能力名（不是普通聊天能力，别往普通 AuthContext 里塞）。
CAP_ADMIN_EVIDENCE = "admin_evidence"


# ================================================================
# 统一 envelope 与错误码（Spec §12）
# ================================================================

class CaptureError(RuntimeError):
    """capture 域的统一异常；code 取 Spec §12 的错误码表。"""

    def __init__(self, code: str, message: str, *, detail: Optional[Dict[str, Any]] = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code = str(code)
        self.detail = detail or {}
        self.retryable = bool(retryable)


class ValidationError(CaptureError):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("VALIDATION_FAILED", message, detail=detail)


class ActorMismatch(CaptureError):
    """adapter 凭据绑定的身份 ≠ 事件自报身份 —— 不重试更宽入口（Spec §12）。"""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("ACTOR_MISMATCH", message, detail=detail)


class SourceRevisionConflict(CaptureError):
    """同一来源 revision 的内容 hash 冲突，或同 operation 换了来源 —— 不覆盖、不别名。"""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("SOURCE_REVISION_CONFLICT", message, detail=detail)


class WriteTransactionHeld(CaptureError):
    """调用方已持有事务：拒绝替它结账（R02）。"""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("WRITE_TRANSACTION_HELD", message, detail=detail)


class AuthDenied(CaptureError):
    """普通查询缺可信身份/权限 —— 不返回正文、标题、计数或存在性（R05）。"""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("AUTH_DENIED", message, detail=detail)


def envelope(*, ok: bool, code: str, operation_id: str = "", data: Any = None,
             retryable: bool = False, degraded: bool = False,
             request_id: str = "") -> Dict[str, Any]:
    """统一返回信封（Spec §12）。公开响应**不**回显内部路径/凭据/私聊全文。"""
    return {
        "ok": bool(ok), "code": str(code),
        "request_id": request_id or ("req-" + uuid.uuid4().hex[:12]),
        "operation_id": str(operation_id or ""), "data": data,
        "retryable": bool(retryable), "degraded": bool(degraded),
    }


# ================================================================
# R02：写事务的唯一拥有者
# ================================================================

@contextmanager
def _own_write_transaction(conn: sqlite3.Connection, *, what: str):
    """本模块**唯一**的写事务入口：自持事务，拒绝外层事务（R02）。

    契约：
    入参：conn（调用方连接）；what（出错信息里标明是哪次写）
    行为：调用方已在事务里 → 抛 WriteTransactionHeld（不 commit、不 rollback、什么都不做）
          否则 BEGIN IMMEDIATE → 正常结束 commit / 异常 rollback
    为什么 BEGIN IMMEDIATE：需要拿写锁来串行化“先查再插”（R03 并发面）。
    ⚠️【不】在里面再问一次 in_transaction 做“兜底 savepoint”：那是给两套语义开口子，
       调用方下次不知道该按哪套期望。进门一次判清，之后这套事务**只归本模块**。
    """
    if conn.in_transaction:
        raise WriteTransactionHeld(
            "%s 拒绝在调用方事务内执行：本模块自带事务，不能替外层结账或撤销（Spec §16）" % what,
            {"operation": what})
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            pass  # 回滚本身失败也不能掩盖原始异常
        raise


# ================================================================
# R05：可信调用者上下文
# ================================================================

@dataclass(frozen=True)
class AuthContext:
    """一次查询/引用的可信调用者上下文（Spec §6.1）。

    为什么必须显式传进来：可见性筛选要先于候选与 LIMIT，而“谁在看”不能由被查询的参数
    自报。生产里由 adapter/AuthContext 装配，测试里由夹具给。

    字段：
      principal_id        当前实际 speaker/调用者（空 ⇒ 一律拒绝）
      profile / bot_id    当前 bot 身份（与事件行比对，跨 bot 不返回）
      reader_scope_kind/id 当前场合范围（只返回该范围内的证据）
      visible_watermark   只读 event_seq <= 该值的证据（本轮快照水位；防读到更晚的消息）
      capabilities        能力集合；CAP_ADMIN_EVIDENCE 才允许“取原件”
    """
    principal_id: str
    profile: str
    bot_id: str
    reader_scope_kind: str
    reader_scope_id: str
    visible_watermark: int = 1 << 62
    capabilities: FrozenSet[str] = frozenset()

    def validate(self) -> None:
        if not str(self.principal_id or "").strip():
            raise AuthDenied("缺少可信 principal：不返回正文、标题、计数或存在性")
        if not str(self.profile or "").strip() or not str(self.bot_id or "").strip():
            raise AuthDenied("缺少 profile/bot 身份：无法判定来源资格")
        if str(self.reader_scope_kind or "") not in READER_SCOPE_KINDS:
            raise AuthDenied("reader_scope_kind 不合法：%r" % self.reader_scope_kind)

    def has(self, capability: str) -> bool:
        return capability in (self.capabilities or frozenset())


# ================================================================
# ConversationEvent v1
# ================================================================

@dataclass
class ConversationEvent:
    """Spec §5.2 的规范化事件。字段由可信 adapter/AuthContext 填充。"""

    operation_id: str
    profile: str
    platform: str
    tenant_id: str
    bot_id: str
    chat_id: str
    chat_type: str
    sender_principal_id: str
    sender_kind: str
    source_message_id: str
    text: str
    observed_at: float
    source_revision: int = 1
    identity_status: str = "verified"
    session_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    root_message_id: Optional[str] = None
    thread_id: Optional[str] = None
    source_timestamp: Optional[float] = None
    source_time_status: str = "unknown"
    kind: str = "inbound"
    reader_scope: Dict[str, Any] = field(default_factory=lambda: {"kind": "unknown", "id": ""})
    disclosure_class: str = "unknown"
    host_locator: Optional[Dict[str, Any]] = None
    observe_allowed: bool = False
    reply_allowed: bool = False
    format_version: int = FORMAT_VERSION

    def validate(self) -> None:
        """逐字段校验；任何不合规都抛 ValidationError（不猜、不补默认身份）。"""
        if int(self.format_version) != FORMAT_VERSION:
            raise ValidationError("format_version must be %d" % FORMAT_VERSION,
                                  {"format_version": self.format_version})
        for name in ("operation_id", "profile", "platform", "tenant_id", "bot_id",
                     "chat_id", "sender_principal_id", "source_message_id"):
            if not str(getattr(self, name) or "").strip():
                raise ValidationError("required field missing/blank: " + name,
                                      {name: getattr(self, name)})
        if self.chat_type not in CHAT_TYPES:
            raise ValidationError("chat_type not in %s" % (CHAT_TYPES,), {"chat_type": self.chat_type})
        if self.sender_kind not in SENDER_KINDS:
            raise ValidationError("sender_kind not in %s" % (SENDER_KINDS,), {"sender_kind": self.sender_kind})
        if self.identity_status not in IDENTITY_STATUSES:
            raise ValidationError("identity_status not in %s" % (IDENTITY_STATUSES,),
                                  {"identity_status": self.identity_status})
        if self.source_time_status not in SOURCE_TIME_STATUSES:
            raise ValidationError("source_time_status not in %s" % (SOURCE_TIME_STATUSES,),
                                  {"source_time_status": self.source_time_status})
        if self.kind not in EVENT_KINDS:
            raise ValidationError("kind not in %s" % (EVENT_KINDS,), {"kind": self.kind})
        if self.disclosure_class not in DISCLOSURE_CLASSES:
            raise ValidationError("disclosure_class not in %s" % (DISCLOSURE_CLASSES,),
                                  {"disclosure_class": self.disclosure_class})
        if not isinstance(self.source_revision, int) or self.source_revision < 1:
            raise ValidationError("source_revision must be int >= 1",
                                  {"source_revision": self.source_revision})
        # ⚠️ 契约来自参考 DDL 的 CHECK：verified 时间必须有值。
        #    这里**不**拿 observed_at 顶替 source_timestamp。
        if self.source_time_status == "verified" and self.source_timestamp is None:
            raise ValidationError("source_time_status=verified 但 source_timestamp 为空",
                                  {"source_timestamp": None})
        scope = self.reader_scope if isinstance(self.reader_scope, dict) else {}
        if str(scope.get("kind") or "") not in READER_SCOPE_KINDS:
            raise ValidationError("reader_scope.kind not in %s" % (READER_SCOPE_KINDS,),
                                  {"reader_scope": self.reader_scope})
        if not str(scope.get("id") or "").strip():
            raise ValidationError("reader_scope.id 为空：来源范围必须可指认（否则无权范围不可判）",
                                  {"reader_scope": self.reader_scope})
        if not isinstance(self.observed_at, (int, float)):
            raise ValidationError("observed_at must be epoch seconds (number)",
                                  {"observed_at": self.observed_at})
        if self.text is None:
            raise ValidationError("text must be a string (空串可以，None 不行)", {"text": None})

    def content_hash(self) -> str:
        """内容指纹：只算**文本本身**。改了文本就是改了来源内容，不许悄悄覆盖。"""
        return hashlib.sha256(str(self.text).encode("utf-8")).hexdigest()

    def source_key(self) -> str:
        """来源目标身份（对应参考 DDL 的 UNIQUE 去掉 revision/kind 的部分 + kind）。

        ★F02：改用 **JSON 数组** 而不是分隔符拼接。
        旧写法把字段用 "|" 连起来，字段自带的 "|" 会让不同来源算出**同一个**值 ——
        最直接的后果是 `_evidence_id()` 同哈希 ⇒ 两个不同来源撞 evidence_id 的唯一约束。
        JSON 数组的可变性（分隔/转义/引号）由标准库保证，拼接歧义从根上不存在。
        ⚠️ 顺序固定不含随机量：同一来源 revision 重放必须仍得同一个 evidence_id（幂等可对账）。
        """
        return json.dumps([
            str(self.profile), str(self.platform), str(self.tenant_id), str(self.bot_id),
            str(self.chat_id), str(self.source_message_id), str(self.source_revision), str(self.kind),
        ], ensure_ascii=False)

    def identity_fingerprint(self) -> str:
        """R03：完整来源身份指纹。**幂等比对只用它**，不用 content_hash。"""
        payload = json.dumps({f: str(getattr(self, f)) for f in _IDENTITY_FIELDS},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "format_version": self.format_version, "operation_id": self.operation_id,
            "profile": self.profile, "platform": self.platform, "tenant_id": self.tenant_id,
            "bot_id": self.bot_id, "chat_id": self.chat_id, "chat_type": self.chat_type,
            "sender_principal_id": self.sender_principal_id, "sender_kind": self.sender_kind,
            "identity_status": self.identity_status,
            "source_message_id": self.source_message_id, "source_revision": self.source_revision,
            "session_id": self.session_id, "reply_to_message_id": self.reply_to_message_id,
            "root_message_id": self.root_message_id, "thread_id": self.thread_id,
            "source_timestamp": self.source_timestamp, "source_time_status": self.source_time_status,
            "observed_at": self.observed_at, "kind": self.kind, "text": self.text,
            "reader_scope": dict(self.reader_scope), "disclosure_class": self.disclosure_class,
            "host_locator": self.host_locator, "observe_allowed": bool(self.observe_allowed),
            "reply_allowed": bool(self.reply_allowed),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ConversationEvent":
        """从 JSON 载荷构造（未知键忽略、缺失键走 dataclass 默认；不臆造身份）。"""
        if not isinstance(raw, dict):
            raise ValidationError("ConversationEvent 必须是对象", {"type": type(raw).__name__})
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in raw.items() if k in known}
        if "observed_at" not in kwargs:
            kwargs["observed_at"] = time.time()
        return cls(**kwargs)


# ================================================================
# adapter 身份（可信侧提供；事件自报值只用于对账）
# ================================================================

@dataclass(frozen=True)
class AdapterIdentity:
    """一个可信 adapter 进程在启动时由**配置/凭据**绑定的身份。

    事由：Spec §5.2 要求「origin tenant/bot/profile 必须与 adapter 凭据能力匹配」，
    §6.1 要求「模型/tool args 不能覆盖 AuthContext」。所以身份只能从这里来。
    """
    profile: str
    platform: str
    tenant_id: str
    bot_id: str

    def assert_matches(self, event: ConversationEvent) -> None:
        for name in ("profile", "platform", "tenant_id", "bot_id"):
            got = str(getattr(event, name))
            want = str(getattr(self, name))
            if got != want:
                raise ActorMismatch(
                    "事件自报的 %s=%r 与 adapter 凭据绑定的 %r 不符：拒收（ACTOR_MISMATCH 不重试更宽入口）"
                    % (name, got, want),
                    {"field": name, "event_value": got, "adapter_value": want})


# ================================================================
# 落库
# ================================================================

def _search_text(text: str) -> str:
    """search_text 的唯一口径 = vector_store 的 FTS5 分词器（jieba v2）。"""
    try:
        from memory.vector_store import _tokenize_for_fts5
        return str(_tokenize_for_fts5(str(text)))
    except Exception:
        return str(text)


def _evidence_id(event: ConversationEvent) -> str:
    """证据 ID：从来源身份确定性派生 ⇒ 同一来源 revision 重放必得同一 ID（幂等可对账）。"""
    digest = hashlib.sha256(event.source_key().encode("utf-8")).hexdigest()[:24]
    return "cev-" + digest


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    raw_loc = out.pop("host_locator_json", None)
    try:
        out["host_locator"] = json.loads(raw_loc) if raw_loc else None
    except (TypeError, ValueError):
        out["host_locator"] = None
    out["reader_scope"] = {"kind": out.get("reader_scope_kind"), "id": out.get("reader_scope_id")}
    out["observe_allowed"] = bool(out.get("observe_allowed"))
    out["reply_allowed"] = bool(out.get("reply_allowed"))
    return out


def _identity_of_row(row: sqlite3.Row) -> Dict[str, str]:
    """R03：把库里一行的身份字段抽出来，与事件逐字段比对。"""
    return {f: str(row[f]) for f in _IDENTITY_FIELDS if f in row.keys()}


def _event_identity(event: ConversationEvent) -> Dict[str, str]:
    return {f: str(getattr(event, f)) for f in _IDENTITY_FIELDS}


def _same_identity(event: ConversationEvent, row: sqlite3.Row) -> bool:
    """幂等命中的**唯一**身份判据（operation 命中 / 来源命中 / 约束回查三处共用）。

    ⚠️【不】在任一分支里另写一份“只比 content_hash”之类的快手比较：
       F02 的病根就是“一条路径核身份、另一条只核正文”。判据只能有一份。
    """
    return _event_identity(event) == _identity_of_row(row)


def _duplicate_result(row: sqlite3.Row, event: ConversationEvent, content_hash: str) -> Dict[str, Any]:
    return {"status": CAPTURE_STATUS_DUPLICATE, "event_seq": int(row["event_seq"]),
            "evidence_id": str(row["evidence_id"]), "operation_id": event.operation_id,
            "content_hash": content_hash, "feature_ready": True}


def _update_source_head(conn: sqlite3.Connection, event: ConversationEvent, event_seq: int) -> None:
    """R04：来源头（最新态）更新。撤回等生命周期事件在这里生效，且**不被更旧的普通事件复活**。

    规则（三条，缺一不可）：
      1. 生命周期事件（撤回）在**同版本**上也能覆盖普通事件 —— 否则“撤回恰好和原消息同 revision”
         就永远撤不掉；
      2. 普通事件只有**严格更新**的 revision 才能覆盖；
      3. 状态只由生命周期事件改写；普通事件跟随时写 'intact'（新版本 = 新内容 = 有效）。
    ⚠️【不】把所有 kind 一律写 intact —— 那正是 R04 的病根（撤回被存成“仍正常”）。
    """
    state = LIFECYCLE_STATE.get(str(event.kind), "intact")
    is_lifecycle = 1 if str(event.kind) in LIFECYCLE_STATE else 0
    conn.execute(
        """INSERT INTO cs_source_heads (
            profile, platform, tenant_id, bot_id, chat_id, source_message_id,
            event_seq, source_revision, state, acl_revision, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?, 1, ?)
        ON CONFLICT(profile,platform,tenant_id,bot_id,chat_id,source_message_id) DO UPDATE SET
            event_seq = CASE
                WHEN excluded.source_revision > cs_source_heads.source_revision
                  OR (excluded.source_revision = cs_source_heads.source_revision AND ? = 1)
                THEN excluded.event_seq ELSE cs_source_heads.event_seq END,
            state = CASE
                WHEN excluded.source_revision > cs_source_heads.source_revision
                  OR (excluded.source_revision = cs_source_heads.source_revision AND ? = 1)
                THEN excluded.state ELSE cs_source_heads.state END,
            source_revision = MAX(cs_source_heads.source_revision, excluded.source_revision),
            updated_at = CASE
                WHEN excluded.source_revision >= cs_source_heads.source_revision
                THEN excluded.updated_at ELSE cs_source_heads.updated_at END""",
        (event.profile, event.platform, event.tenant_id, event.bot_id, event.chat_id,
         event.source_message_id, event_seq, int(event.source_revision), state,
         float(event.observed_at), is_lifecycle, is_lifecycle))


def _lookup_by_operation(conn: sqlite3.Connection, operation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cs_events WHERE operation_id=?", (str(operation_id),)).fetchone()


def _lookup_by_source(conn: sqlite3.Connection, event: ConversationEvent) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cs_events WHERE profile=? AND platform=? AND tenant_id=? AND bot_id=? "
        "AND chat_id=? AND source_message_id=? AND source_revision=? AND kind=?",
        (event.profile, event.platform, event.tenant_id, event.bot_id, event.chat_id,
         event.source_message_id, int(event.source_revision), event.kind)).fetchone()


def capture(conn: sqlite3.Connection, event: ConversationEvent,
            adapter: Optional[AdapterIdentity] = None) -> Dict[str, Any]:
    """落一条证据。返回 {status, event_seq, evidence_id, operation_id, content_hash}。

    契约：
    入参：conn（**调用方不得持事务**；本模块自持事务，见 R02）
          event（已规范化的 ConversationEvent）
          adapter（可信身份；生产接线必须给）
    返回：dict（status ∈ captured/duplicate）
    副作用：cs_events 新增一行 + cs_source_heads upsert（**不改任何既有证据行**）
    谁调用：宿主 adapter（经 conversation_adapter_bridge）、测试、CLI 导入
    改动影响：幂等键与冲突语义是 I05 的验收面。
    """
    event.validate()
    if adapter is not None:
        adapter.assert_matches(event)

    # ── feature 轴闸：版本/结构不匹配 = 不写（SCHEMA_MISMATCH），且必须可归因 ──
    cs.require_feature_schema(conn)

    content_hash = event.content_hash()

    with _own_write_transaction(conn, what="capture"):
        # ① operation 命中：必须**完整身份**一致才算同一件事（R03）。
        existing = _lookup_by_operation(conn, event.operation_id)
        if existing is not None:
            if not _same_identity(event, existing):
                raise SourceRevisionConflict(
                    "同一 operation_id 携带了不同来源身份：拒绝别名到旧证据",
                    {"operation_id": event.operation_id, "event_seq": int(existing["event_seq"]),
                     "existing_identity": _identity_of_row(existing),
                     "incoming_identity": _event_identity(event)})
            if str(existing["content_hash"]) != content_hash:
                raise SourceRevisionConflict(
                    "同一 operation_id 携带了不同内容：拒绝覆盖既有证据",
                    {"operation_id": event.operation_id, "event_seq": int(existing["event_seq"])})
            return _duplicate_result(existing, event, content_hash)

        # ② 来源身份命中：**所有幂等命中路径同一判据（F02）**。
        #   病灶：旧实现这一支只比 content_hash ⇒ 换了 sender 而正文相同的新 operation，
        #   被别名成旧证据（B 的新请求被签成 A 的旧话）。正文相同**不是**同一件事；
        #   来源身份（含 sender）才是。
        #   ⚠️【不】用 content_hash 代替身份判据：那正是 R03/F02 反复复发的形态。
        same_source = _lookup_by_source(conn, event)
        if same_source is not None:
            if not _same_identity(event, same_source):
                raise SourceRevisionConflict(
                    "同一来源 revision 携带了不同来源身份：拒绝把新来源别名成旧证据",
                    {"event_seq": int(same_source["event_seq"]),
                     "existing_identity": _identity_of_row(same_source),
                     "incoming_identity": _event_identity(event)})
            if str(same_source["content_hash"]) != content_hash:
                raise SourceRevisionConflict(
                    "同一来源 revision 的内容 hash 冲突：迟到编辑不覆盖原文，也不把到达最晚当最新",
                    {"event_seq": int(same_source["event_seq"]), "source_key": event.source_key()})
            return _duplicate_result(same_source, event, content_hash)

        evidence_id = _evidence_id(event)
        scope = event.reader_scope if isinstance(event.reader_scope, dict) else {}
        locator = (json.dumps(event.host_locator, ensure_ascii=False, sort_keys=True)
                   if event.host_locator else None)
        try:
            cur = conn.execute(
                """INSERT INTO cs_events (
                    evidence_id, operation_id, format_version, profile, platform, tenant_id, bot_id,
                    chat_id, chat_type, sender_principal_id, sender_kind, identity_status,
                    source_message_id, source_revision, session_id, reply_to_message_id,
                    root_message_id, thread_id, source_timestamp, source_time_status, observed_at,
                    kind, text, content_hash, search_text, reader_scope_kind, reader_scope_id,
                    disclosure_class, host_locator_json, observe_allowed, reply_allowed
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_id, event.operation_id, int(event.format_version), event.profile,
                 event.platform, event.tenant_id, event.bot_id, event.chat_id, event.chat_type,
                 event.sender_principal_id, event.sender_kind, event.identity_status,
                 event.source_message_id, int(event.source_revision), event.session_id,
                 event.reply_to_message_id, event.root_message_id, event.thread_id,
                 event.source_timestamp, event.source_time_status, float(event.observed_at),
                 event.kind, str(event.text), content_hash, _search_text(str(event.text)),
                 str(scope.get("kind") or "unknown"), str(scope.get("id") or ""),
                 event.disclosure_class, locator,
                 1 if event.observe_allowed else 0, 1 if event.reply_allowed else 0))
            event_seq = int(cur.lastrowid)
            _update_source_head(conn, event, event_seq)
        except sqlite3.IntegrityError:
            # R03 兜底：并发下（或别的路径先写）我们可能撞唯一约束。
            # ⚠️ 这里**不能**直接报 WRITE_FAILED —— “重复投递返回同一结果”是接口承诺。
            #    回头重查：对得上就让这次调用以 duplicate 正常返回。
            again = _lookup_by_operation(conn, event.operation_id) or _lookup_by_source(conn, event)
            if again is not None and _same_identity(event, again) and \
                    str(again["content_hash"]) == content_hash:
                return _duplicate_result(again, event, content_hash)
            raise SourceRevisionConflict(
                "写入撞唯一约束但回查对不上同一来源：拒绝覆盖",
                {"operation_id": event.operation_id, "source_key": event.source_key()})
        except sqlite3.DatabaseError as exc:
            # 版本/maintenance 拒写、CHECK 失败等：不假报记住（CAPTURE_FAILED/WRITE_FAILED）。
            raise CaptureError("WRITE_FAILED", "capture 落库失败：%s" % type(exc).__name__,
                               detail={"sqlite_error": type(exc).__name__}) from exc

    return {"status": CAPTURE_STATUS_CAPTURED, "event_seq": event_seq,
            "evidence_id": evidence_id, "operation_id": event.operation_id,
            "content_hash": content_hash, "feature_ready": True}


# ================================================================
# R05：受控查询（普通路径）
# ================================================================

def _head_state(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[str]:
    """取该证据对应的来源头状态。**取不到返回 None**（调用方按 fail-closed 处理）。"""
    try:
        head = conn.execute(
            "SELECT state FROM cs_source_heads WHERE profile=? AND platform=? AND tenant_id=? "
            "AND bot_id=? AND chat_id=? AND source_message_id=?",
            (row["profile"], row["platform"], row["tenant_id"], row["bot_id"],
             row["chat_id"], row["source_message_id"])).fetchone()
    except sqlite3.Error:
        return None
    return None if head is None else str(head["state"])


def _principal_granted(conn: sqlite3.Connection, event_seq: int, principal: str) -> bool:
    """该 principal 是否持有未撤销的 principal 型授权。查不到授权表 ⇒ 否（fail-closed）。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM cs_event_audiences WHERE event_seq=? AND audience_kind='principal' "
            "AND audience_id=? AND revoked_at IS NULL", (int(event_seq), str(principal))).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _visible_for(conn: sqlite3.Connection, row: sqlite3.Row, auth: AuthContext) -> bool:
    """R05 的判据集中处：范围 → 生命周期 → 水位 → 授权 + dm_owner 本人。

    ⚠️ 顺序就是语义：**任何一条不满足都不返回**，而且必须在取正文与 LIMIT 之前完成。
    ⚠️ 来源头状态缺失 ⇒ False（撤回态未知时不许当“没撤回过”）。
    """
    if str(row["profile"]) != str(auth.profile) or str(row["bot_id"]) != str(auth.bot_id):
        return False
    if str(row["reader_scope_kind"]) != str(auth.reader_scope_kind) or \
            str(row["reader_scope_id"]) != str(auth.reader_scope_id):
        return False
    if int(row["event_seq"]) > int(auth.visible_watermark):
        return False
    state = _head_state(conn, row)
    if state != "intact":
        return False
    if _principal_granted(conn, int(row["event_seq"]), auth.principal_id):
        return True
    # own-DM：本人可读自己那一侧（与 conversation_source 的口径一致）
    if str(row["reader_scope_kind"]) == "dm_owner" and \
            str(row["reader_scope_id"]) == str(auth.principal_id):
        return True
    return False


def query_events(conn: sqlite3.Connection, auth: AuthContext, *, query: str = "",
                 limit: int = 20) -> List[Dict[str, Any]]:
    """普通受控查询：返回调用者**有权看到**的证据原文（Spec §5.5 强不变量 1）。

    契约：
    入参：conn、auth（可信上下文，必须）、query（空 ⇒ 列最近）、limit
    返回：证据 dict 列表（**只含**通过范围/生命周期/水位/授权的行）
    副作用：无（纯只读）
    谁调用：普通聊天/接话路径
    改动影响：可见性筛选必须**先于** LIMIT（否则条数上限被不可见内容占满，Spec §16）。
    """
    auth.validate()
    limit = max(1, int(limit))
    if str(query or "").strip():
        terms = _search_text(str(query).strip())
        sql = ("SELECT e.* FROM cs_events_fts f JOIN cs_events e ON e.event_seq = f.rowid "
               "WHERE cs_events_fts MATCH ? AND e.profile=? AND e.bot_id=? "
               "AND e.reader_scope_kind=? AND e.reader_scope_id=? AND e.event_seq <= ? "
               "ORDER BY e.event_seq ASC")
        args: List[Any] = ['"' + terms.replace('"', '""') + '"', str(auth.profile),
                           str(auth.bot_id), str(auth.reader_scope_kind),
                           str(auth.reader_scope_id), int(auth.visible_watermark)]
    else:
        sql = ("SELECT e.* FROM cs_events e WHERE e.profile=? AND e.bot_id=? "
               "AND e.reader_scope_kind=? AND e.reader_scope_id=? AND e.event_seq <= ? "
               "ORDER BY e.event_seq ASC")
        args = [str(auth.profile), str(auth.bot_id), str(auth.reader_scope_kind),
                str(auth.reader_scope_id), int(auth.visible_watermark)]
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
    except sqlite3.DatabaseError:
        return []  # 查询失败 ⇒ 空（**不**回落成“不过滤”）
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _visible_for(conn, row, auth):
            continue
        out.append(_row_to_dict(row))
        if len(out) >= limit:
            break
    return out


def search_events(conn: sqlite3.Connection, query: str, *, limit: int = 20,
                  profile: Optional[str] = None, reader_scope_kind: Optional[str] = None,
                  reader_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """【低层/兼容用】按原话检索，**只做生命周期过滤**，不做授权过滤。

    ⚠️ 为什么保留这个旧签名：验收方的固定反例用它，接口不能靠改名“修好”。
    ⚠️ 但它**不是普通用户可达路径** —— 普通查询必须走 query_events(auth=...)，
       那里才有 principal/范围/水位/授权的完整判据；管理取原件走 get_evidence_original。
    ⚠️ 保留它不代表可以省掉授权：生命周期（撤回/来源头缺失/不可读）在这里**照常拦**，
       因为“撤回后还能搜出全文”是纯粹的数据事故，跟调用者是谁无关。

    改动影响：任何人把本函数接到用户可达入口 = 把 R05 又拆回去。
              新增调用点前先问一句“这里为什么不能用 query_events(auth=)”。
    """
    terms = str(query or "").strip()
    if not terms:
        return []
    sql = ("SELECT e.* FROM cs_events_fts f JOIN cs_events e ON e.event_seq = f.rowid "
           "WHERE cs_events_fts MATCH ?")
    args: List[Any] = ['"' + _search_text(terms).replace('"', '""') + '"']
    if profile is not None:
        sql += " AND e.profile=?"
        args.append(str(profile))
    if reader_scope_kind is not None:
        sql += " AND e.reader_scope_kind=?"
        args.append(str(reader_scope_kind))
    if reader_scope_id is not None:
        sql += " AND e.reader_scope_id=?"
        args.append(str(reader_scope_id))
    sql += " ORDER BY e.event_seq ASC"
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
    except sqlite3.DatabaseError:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        # 生命周期过滤：只有明确 intact 的才返回（来源头缺失/不可读一律不返回）
        if _head_state(conn, row) != "intact":
            continue
        out.append(_row_to_dict(row))
        if len(out) >= max(1, int(limit)):
            break
    return out


def get_evidence_original(conn: sqlite3.Connection, event_seq: int, *,
                          auth: AuthContext) -> Optional[Dict[str, Any]]:
    """**管理取原件**：按 event_seq 取证据全文，要求 admin_evidence 能力（R05）。

    为什么要和普通路径分开：受限管理员查原件是合法需求（审计/排障），
    但它**不是**“普通聊天里可见性已经过滤”的服务。两条路各有各的入口，
    免得日后有人把管理入口接到普通查询上，把整库原文暴露出去。
    ⚠️ 即便带能力，也**不**返回已抹除（redacted）的正文。
    """
    auth.validate()
    if not auth.has(CAP_ADMIN_EVIDENCE):
        raise AuthDenied("取证据原件需要 %s 能力" % CAP_ADMIN_EVIDENCE)
    row = conn.execute("SELECT * FROM cs_events WHERE event_seq=?", (int(event_seq),)).fetchone()
    if row is None:
        return None
    if _head_state(conn, row) in ("redacted",):
        return None
    return _row_to_dict(row)


def get_event(conn: sqlite3.Connection, event_seq: int) -> Optional[Dict[str, Any]]:
    """【低层/测试用】按 event_seq 直取证据行，**不做权限判定**。

    ⚠️【不】把本函数接到任何普通用户可达路径上 —— 它等价于“整库原文直读口”。
       普通查询走 query_events(auth=...)；管理取数走 get_evidence_original(auth=...)。
       保留它是为了让迁移/对账/测试能拿到原始行，不是为了省一层 auth 参数。
    """
    row = conn.execute("SELECT * FROM cs_events WHERE event_seq=?", (int(event_seq),)).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_event_by_operation(conn: sqlite3.Connection, operation_id: str) -> Optional[Dict[str, Any]]:
    """【低层/测试用】按 operation 直取证据行（同 get_event 的提醒）。"""
    row = conn.execute("SELECT * FROM cs_events WHERE operation_id=?", (str(operation_id),)).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_event_by_source(conn: sqlite3.Connection, *, profile: str, platform: str, tenant_id: str,
                        bot_id: str, chat_id: str, source_message_id: str,
                        source_revision: Optional[int] = None) -> List[Dict[str, Any]]:
    """【低层/测试用】按平台来源定位（platform message ID 独立于 host 行 ID，Spec §5.1）。"""
    sql = ("SELECT * FROM cs_events WHERE profile=? AND platform=? AND tenant_id=? AND bot_id=? "
           "AND chat_id=? AND source_message_id=?")
    args: List[Any] = [profile, platform, tenant_id, bot_id, chat_id, source_message_id]
    if source_revision is not None:
        sql += " AND source_revision=?"
        args.append(int(source_revision))
    sql += " ORDER BY source_revision ASC, event_seq ASC"
    return [_row_to_dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


# ================================================================
# 归属分离（T16）：B 引用 A 的话，说话人与被引用者必须分开
# ================================================================

def resolve_quote_attribution(conn: sqlite3.Connection, event_seq: int, *,
                              auth: Optional[AuthContext] = None) -> Dict[str, Any]:
    """返回 {speaker, quoted_speaker, quoted_text, quoted_event_seq, reply_to_message_id}。

    契约：
    入参：conn、event_seq、auth（**给了就走 R05 判据**：本行与被引用行都必须可见；
          不给则按低层对账语义返回，只有测试/迁移该这么用）
    返回：说话人（本行 sender）与被引用人（reply_to 那行的 sender）**分列两栏**
    谁调用：CST03 的上下文装配 / 未来接话路由
    改动影响：把两者合并 = 让 B 的话被当成 A 说的（T16 直接红）。

    ⚠️ 引用**没有被 capture 过**时，quoted_* 一律 None —— **不**凭 reply_to_message_id
       去平台侧补拉历史，也**不**用本行文本猜被引用人（T19：没收到的不许编）。
    """
    row = conn.execute("SELECT * FROM cs_events WHERE event_seq=?", (int(event_seq),)).fetchone()
    if row is None:
        return {"speaker": None, "quoted_speaker": None, "quoted_text": None,
                "quoted_event_seq": None, "reply_to_message_id": None, "quote_resolved": False,
                "reason": "event_absent"}
    if auth is not None:
        auth.validate()
        # 本行不可见 ⇒ 不返回它的说话人（连“存在”都不说）
        if not _visible_for(conn, row, auth):
            return {"speaker": None, "quoted_speaker": None, "quoted_text": None,
                    "quoted_event_seq": None, "reply_to_message_id": None,
                    "quote_resolved": False, "reason": "current_event_not_visible"}
    out: Dict[str, Any] = {
        "speaker": str(row["sender_principal_id"]),
        "quoted_speaker": None, "quoted_text": None, "quoted_event_seq": None,
        "reply_to_message_id": row["reply_to_message_id"], "quote_resolved": False, "reason": "",
    }
    reply_to = row["reply_to_message_id"]
    if not reply_to:
        out["reason"] = "not_a_reply"
        return out
    quoted = conn.execute(
        "SELECT * FROM cs_events WHERE profile=? AND platform=? AND tenant_id=? AND bot_id=? "
        "AND chat_id=? AND source_message_id=? "
        "ORDER BY source_revision DESC, event_seq DESC LIMIT 1",
        (row["profile"], row["platform"], row["tenant_id"], row["bot_id"], row["chat_id"],
         reply_to)).fetchone()
    if quoted is None:
        # 没收到过被引用那条 —— 如实说“未取得来源”，不补拉、不猜。
        out["reason"] = "quoted_source_not_captured"
        return out
    if auth is not None and not _visible_for(conn, quoted, auth):
        # 被引用那条不在调用者可见范围 —— 同样不返回（R05：授权先于正文）
        out["reason"] = "quoted_source_not_visible"
        return out
    # ⚠️【不】把被引用正文拼进 speaker 字段，也【不】在返回里把它当本行说话人的话。
    out["quoted_speaker"] = str(quoted["sender_principal_id"])
    out["quoted_text"] = str(quoted["text"])
    out["quoted_event_seq"] = int(quoted["event_seq"])
    out["quote_resolved"] = True
    return out


# ================================================================
# 来源授权（只有写入函数，不发明授权）
# ================================================================

def grant_audience(conn: sqlite3.Connection, event_seq: int, *, audience_kind: str, audience_id: str,
                   authorization_ref: str, grant_revision: int = 1) -> None:
    """登记一条 explicit grant。**调用方必须持有真实授权依据**（authorization_ref）。

    ⚠️【不】在 adapter/capture 路径里自动调用它：显示名、自然语言“我是谁”、模型输出
       都不构成授权（Spec §5.3/§6.1）。本函数只是落库入口，授权判断在可信授权侧。
    R02：同样**自持事务**，不替调用方结账、也不替它撤销。
    """
    if audience_kind not in ("principal", "group", "capability"):
        raise ValidationError("audience_kind not in (principal,group,capability)",
                              {"audience_kind": audience_kind})
    if not str(audience_id or "").strip() or not str(authorization_ref or "").strip():
        raise ValidationError("audience_id / authorization_ref 不得为空",
                              {"audience_id": audience_id, "authorization_ref": authorization_ref})
    with _own_write_transaction(conn, what="grant_audience"):
        exists = conn.execute("SELECT 1 FROM cs_events WHERE event_seq=?", (int(event_seq),)).fetchone()
        if exists is None:
            raise ValidationError("event_seq 不存在，拒绝孤儿授权", {"event_seq": event_seq})
        conn.execute(
            "INSERT INTO cs_event_audiences (event_seq, audience_kind, audience_id, authorization_ref, "
            "grant_revision, revoked_at) VALUES (?,?,?,?,?,NULL) "
            "ON CONFLICT(event_seq,audience_kind,audience_id) DO UPDATE SET "
            "authorization_ref=excluded.authorization_ref, grant_revision=excluded.grant_revision",
            (int(event_seq), audience_kind, str(audience_id), str(authorization_ref),
             int(grant_revision)))


def revoke_audience(conn: sqlite3.Connection, event_seq: int, *, audience_kind: str,
                    audience_id: str, at: Optional[float] = None) -> None:
    """撤销一条授权（Spec §6.2：来源授权收紧 ⇒ 相关 projection/cache 失效）。

    ⚠️ 撤销是**标记**（写 revoked_at）而不是删行：删掉就再也说不清“曾经授权给谁”。
    """
    with _own_write_transaction(conn, what="revoke_audience"):
        conn.execute(
            "UPDATE cs_event_audiences SET revoked_at=? "
            "WHERE event_seq=? AND audience_kind=? AND audience_id=? AND revoked_at IS NULL",
            (float(at if at is not None else time.time()), int(event_seq),
             str(audience_kind), str(audience_id)))


__all__ = [
    "FORMAT_VERSION", "ConversationEvent", "AdapterIdentity", "AuthContext",
    "CaptureError", "ValidationError", "ActorMismatch", "SourceRevisionConflict",
    "WriteTransactionHeld", "AuthDenied", "envelope",
    "CAP_ADMIN_EVIDENCE", "LIFECYCLE_KINDS", "LIFECYCLE_STATE",
    "capture", "query_events", "get_evidence_original", "search_events",
    "get_event", "get_event_by_operation", "get_event_by_source",
    "resolve_quote_attribution", "grant_audience", "revoke_audience",
]
