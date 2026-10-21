# ================================================================
# XTMEM · source_resolver.py（来源权限解析 · M03 派生权限核心）
#
# ▍这是什么
# ① 干什么：把「一条来源的记忆」翻译成**实际读者集合**，并对多条来源求【交集】；
#    再把交集投影回 schema 能表达的一个 visibility 字串。是 M03 的唯一权威。
# ② 技术栈：纯 Python，只依赖 memory.visibility（无环，模块级 import 安全）。
# ③ 依赖（内嵌了什么）：不嵌数据；chunk 来源靠调用方注入的 sqlite3 连接读取。
# ④ 给谁看：memory/vector_store.py（写时闸 + 收紧传播）、memory/context_builder.py（存派生）、
#    以及未来 CST01（event 来源）/UX02（media 来源）的接入口。
#
# ▍核心概念：为什么"最严可见性字串"是错的（M03 的病根）
# visibility 是【namespace 相对】语义：
#     shared            → 所有人
#     private           → 仅 chunk_namespace 这个域
#     shared-with:a,b   → chunk_namespace 域 + 名单 a,b
# 于是「private」这个字串本身**不带域名**。同一个 'private'：
#     挂在 ns=dsh 上   ⇒ 读者 {dsh}
#     挂在 ns=zcode 上 ⇒ 读者 {zcode}
# ⇒ 旧 min_visibility 求的是「最严的**字串**」，而 Spec §9.3 要的是「读者**集合**的交集」。
#   跨 owner 派生时两者不等价：源读 {dsh}，算出 'private'，派生落到 assistant ⇒ 读者
#   变成 {assistant} —— 实际读者集合**扩大了**（I09 缺陷面，实机复现见 _evidence）。
# 本模块把「读者集合」提成一等公民，字串只是它的一种编码。
#
# ▍表达能力的硬边界（⚠️ 改前必读）
# schema 只认三种字串，能表达的读者集合形如：
#     'shared'          → ALL
#     'private'         → {ns}
#     'shared-with:L'   → {ns} ∪ L
# 共同点：**namespace 必须属于读者集合**。所以「多个域的交集 = 空集」或
# 「交集里的域 ≠ 拟落的域」这两种情形**编码不出来**。
#
# Spec §9.3 对这两种情形给了两个选项：「拒绝生成」或「保留受限归属」。
# 本模块按**来源可解析性**分流（决策只在 derived_visibility_for_write 一处做，铁律 5）：
#   · 来源全部可解析（交集是真交集）→ **拒绝生成**：
#       交集不含拟落域时若围栏成 'private'，等于把阅读权**凭空授予**拟落域——
#       那正是 I09 的复发形态。拒绝才是 fail-closed。
#   · 有来源查不到（交集根本算不出来）→ **保留受限归属**（围栏到 private）：
#       此时拒绝生成会把"引用了已删源的遗留行 + WAL 重放"整批打死（数据丢失），
#       而 M01 的血缘保真契约要求这类行仍能落库。围栏到 private 与旧行为等价或更严，
#       不引入任何新的放行面。
# 【不】许把「来源可解析但交集编不出来」也悄悄放宽成 'private'。
#
# ▍修改铁律
# 1. 【不】在别处再写一份读者集合换算：reader_set_of() 在 memory/visibility.py，全库唯一实现。
#    （血泪：当年 visibility.py 的注释就写着「全库唯一实现」，结果 min_visibility 另立了
#      一套「最严字串」口径，两套口径并存三年——M03 就是来收这个摊子的。）
# 2. 【不】把 ALL 当空集：ALL ∩ X = X，EMPTY ∩ X = EMPTY。写反了会静默放行全库。
# 3. 【不】让 resolve 失败变成"按 shared 处理"：查不到来源 = 读者集围栏到 EMPTY（保守）。
#    Spec §9.3「空/缺失来源保守处理」。
# 4. 新增来源类型（event / media）必须走 register_provider()，【不】在本文件里 if-else 加分支。
# 5. ★【不】让写路径自己判"拒绝还是围栏"：两个写入口（context_builder.store 与
#    vector_store 的写侧闸）必须问同一个 derived_visibility_for_write()。
#    M01 契约 §5 要求 WAL 与 DB 的 visibility 同源——两边各判一次就是"两套口径"，
#    正是 M03 要根治的病。改这条等于把病根请回来。
#
# ▍外号表
# "读者集" → reader set（能读到该条的身份集合）
# "围栏"   → CONFINED（只有管理员能读；查不到来源时的保守值）
# "交集"   → intersection_of() 的结果，派生读者集合的唯一合法上界
# "严格档" → derived_visibility_for_write 的 strict 分支（来源齐全 → 编不出就拒）
# "保守档" → 同函数的 conservative 分支（来源缺失 → 保留受限归属）
#
# ▍已知问题
# ⚠️ TODO（2026-10-13）：conversation event / media 来源表尚未建成（CST01/UX02），
#    当前只注册了 chunk provider；其余类型 resolve 一律返回「不可用」并按保守处理。
#
# ▍更新记录
# *更新：2026-10-13 · 创建（小D，M03）：读者集合为一等公民 + 多源交集 + 收紧传播 +
#   受众投影 + 写路径唯一决策函数（严格/保守双档）*
# ================================================================
"""来源权限解析：把来源展开成实际读者集合，对多源求交集，再投影回合法 visibility 字串。

契约（§9.3 落地）：
入参：refs（来源引用序列）/ namespace（拟落的域）
返回：ResolveResult(readers, visibility, refusals, snapshots)
副作用：只读（除非调用方显式调用 tighten_source）
谁调用：memory/vector_store.py、memory/context_builder.py
改动影响：reader_set_of() 的语义是权限上界，改窄会误伤正常读取，改宽就是越权，改前跑
         tests/test_m03_derived_permissions.py 全套。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

# ⚠️ 本模块此前【没有】logger。R2/R1 返修新增的告警需要它——顺手补上，
#    别用 print（引擎进程的 stdout 不一定有人看，日志走 logging 才进得到巡检口径）。
logger = logging.getLogger("xtmem.source_resolver")

# ★M03 铁律 1 的落地：读者集代数的**唯一实现**在 memory/visibility.py（那里是可见性语义的
#   老巢，所有端都 import 它）。本模块只做"来源解析"这一层，【不】重抄一份代数——
#   重抄一份正是 M03 要根治的病根（判定与换算两套口径并存）。
from memory.visibility import (
    ALL_READERS, CONFINED, QUARANTINE_MARK, QUARANTINE_OWNER, ReaderSet,
    best_visibility_for, entry_visibility_ok, intersect, is_quarantined, is_subset,
    reader_set_of, readers_from_visibility,
)

__all__ = [
    "ALL_READERS", "CONFINED", "ReaderSet",
    "reader_set_of", "readers_from_visibility", "intersect", "is_subset", "best_visibility_for",
    "SourceRef", "SourceSnapshot", "ResolveResult",
    "SourceResolver", "get_resolver", "iter_resolvers", "register_default_provider",
]


# ── 进程级默认 provider（F01，2026-10-16）─────────────────────────────────
# 含义：**此后新建的每一个 SourceResolver** 都自动带上这些来源类型。
# 谁登记：旁路来源模块自己在 install()/wire 时调 register_default_provider()。
# 为什么必须有这一层：按库 resolver 是**动态**创建的（引擎重连、开第二个库），
#   “启动时给已存在的槽补挂一次”覆盖不了之后新建的槽 ⇒ 会重现 F01 的假就绪
#   （install 报 True、真实查询 present=False）。
# ⚠️【不】把它当“全局放行开关”：它只决定**注册哪些来源类型**，不放宽任何权限；
#    每种来源的读者集合仍由各自的 provider 按 fail-closed 算。
_DEFAULT_PROVIDERS: Dict[str, Callable[[Optional[sqlite3.Connection], SourceRef], SourceSnapshot]] = {}


# ================================================================
# 读者集合的代数 —— 实现落在 memory/visibility.py
#
# ⚠️ 这里**只做再导出**，不写第二份。当年 min_visibility 另立"最严字串"口径，
#    与 visibility_ok 并存三年，M03 就是来收这个摊子的——别再挖一个新坑。
# ============================================================

def _is_all(r: ReaderSet) -> bool:
    return r is ALL_READERS


def _readers_repr(readers) -> str:
    """读者集合的可读渲染。⚠️ ALL_READERS 是哨兵、不可迭代——裸 sorted() 会抛 TypeError。

    本函数与 VectorStore._readers_repr 是同一份逻辑（前者为引擎、后者为解析器，
    两边都要渲染读者集合）。刻意不互相 import：source_resolver 不该依赖 vector_store
    （会成环：vector_store 反过来 import source_resolver）。两处都极短，且守卫测试
    test_m03 覆盖了"ALL 也能渲染"这条行为。
    """
    if readers is ALL_READERS:
        return "ALL_READERS"
    try:
        return str(sorted(readers))
    except TypeError:
        return str(readers)


# ================================================================
# 来源引用与快照
# ================================================================

@dataclass(frozen=True)
class SourceRef:
    """一条来源引用。kind 决定用哪个 provider 解析；id 是提供方命名空间内的标识。"""
    kind: str      # 'chunk' | 'conversation_event' | 'media'
    id: str

    @classmethod
    def parse(cls, raw: Any) -> "SourceRef":
        """把裸值归一成 SourceRef。

        ⚠️ 纯字符串 id 默认按 chunk 解释（历史 source_ids 就是 chunk id 数组）；
           但**不**按 ':' 猜类型——那会把 mem-xxx:yyy 这类 id 猜歪。要别的类型请显式给 dict。
        """
        if isinstance(raw, SourceRef):
            return raw
        if isinstance(raw, dict):
            return cls(str(raw.get("kind") or "chunk"), str(raw.get("id") or ""))
        return cls("chunk", str(raw or ""))

    def as_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass
class SourceSnapshot:
    """来源在**某一时刻**的 (读者集合, 版本) 快照——T17 的"旧计划失效"靠它。

    为什么版本不是 updated_at 一个数：ACL 收紧可能不改 updated_at（收紧是 UPDATE，
    但历史代码有直接写库的路径），只比对时间戳会漏判。所以版本 = 若干列的指纹。
    """
    ref: SourceRef
    present: bool                 # 来源是否真的存在（查不到=False，保守处理）
    readers: ReaderSet
    revision: str                 # 权限/内容/生命周期的指纹（变了就必须弃用旧计划）
    state: str = ""
    namespace: str = ""
    visibility: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref.as_dict(), "present": self.present,
            "readers": "ALL" if _is_all(self.readers) else sorted(self.readers),
            "revision": self.revision, "state": self.state,
            "namespace": self.namespace, "visibility": self.visibility,
        }


@dataclass
class ResolveResult:
    """一次多源解析的结论。派生写侧只认这个对象，【不】再自己算 visibility。

    ★R1-2（2026-10-14 返修）：新增 known_readers / missing 两个字段。
    为什么必须有（独立验收实测的绕过面）：
      来源里有**查不到**的 id 时，旧实现把整个交集压成 CONFINED，然后保守档直接落
      'private'。于是 `source_ids=[已知的 dsh/private, 不存在的 id]` ⇒ 落 zcode/private
      ⇒ **zcode 拿到了已知私来源的派生**，而单引已知来源时是会被拒的。
      "加一个不存在的来源就能绕过已知来源的限制"是纯绕过。
    ⇒ 正确语义：查不到的来源**只让交集不可知，不解除已知来源的约束**。
       所以单独记一份"仅由**可解析来源**求出的交集"（known_readers）——
       它是**真交集的上界**（真交集 ⊆ known_readers），任何落库值都必须 ⊆ 它。
    """
    readers: ReaderSet
    visibility: Optional[str]          # None = 交集编码不出来 ⇒ 必须拒绝生成
    refusals: List[str] = field(default_factory=list)
    snapshots: List[SourceSnapshot] = field(default_factory=list)
    # 仅由 present=True 的来源求出的交集（真交集的上界）。全部可解析时 == readers。
    known_readers: ReaderSet = ALL_READERS
    # 查不到的来源条数（>0 ⇒ 真交集不可知 ⇒ 保守档）
    missing: int = 0

    @property
    def ok(self) -> bool:
        return self.visibility is not None and not self.refusals

    def as_dict(self) -> Dict[str, Any]:
        return {
            "readers": "ALL" if _is_all(self.readers) else sorted(self.readers),
            "known_readers": "ALL" if _is_all(self.known_readers) else sorted(self.known_readers),
            "missing": self.missing,
            "visibility": self.visibility,
            "refusals": list(self.refusals),
            "snapshots": [s.as_dict() for s in self.snapshots],
        }


# ================================================================
# SourceResolver
# ================================================================

# chunk 版本指纹用到的列。⚠️ 加列必须同步 _CHUNK_SQL 的 SELECT，否则指纹算了个寂寞。
# ★R5（2026-10-14 返修）：**必须**包含 source_ids。它此前漏了，于是"派生来源从 A 换成 B"
#   这种**血缘改写**（授权语义变了！）不算版本变化，revalidate 会判旧计划仍然有效。
#   本接口存在的唯一理由就是"值变了就弃旧计划"，而来源引用本身就是授权语义的一部分。
#   （踩坑记录见错题本：指纹列漏血缘 = 用 A 的授权去干 B 的活。）
# ★★G3（2026-10-14 第四轮返修）：**必须**包含 retire_reason——它是"权限隔离"的权威判据
#   （见 memory/visibility.is_quarantined：前缀 m03-acl-unexpressible:）。
#   病灶（独立验收 v3 实测，F2-quarantine-marker-invalidates-plan / -between-checks）：
#     捕获来源快照后**只改 retire_reason**（其余列含 updated_at 一律不动）⇒ 来源 readers
#     从 ALL 变空、授权语义已翻，但 revision 逐字节相同 ⇒ revalidate 判"计划仍有效"、
#     写侧权威复验也放行 ⇒ 新派生 active/shared 落库，统一读闸已拒绝该来源给 zcode，
#     zcode 却能读派生。
#   ⚠️ revalidate 里那句 `not is_subset(cur.readers, s.readers)` 拦不住"ALL → 空"
#     （空集是任何集合的子集）——所以**不能靠读者集蕴含兜底**，值指纹必须自己覆盖它。
#   ⚠️ 隔离标记既然能单独改变权限，它就必须与 visibility/state 同列进指纹；
#     不许依赖"所有写者都会同时更新时间戳"这种约定来让旧计划失效。
#   ⚠️ 与"普通知识退役"的区别不受影响：判据是**前缀**不是 state=='archived'
#     （普通退役 retire_reason 无该前缀，读路径照旧可作历史读取）。
_CHUNK_VERSION_COLS = ("updated_at", "content_hash", "state", "visibility", "namespace",
                       "superseded_by", "derived_from", "source_ids", "retire_reason")
# ★F2（2026-10-14 第二轮返修）：SELECT 必须带上 retire_reason——隔离态的唯一识别依据是
#   **退役原因前缀**（见 memory/visibility.is_quarantined），不带它就算不出"这行被隔离了"，
#   于是隔离行会被当成普通 shared 来源继续参与交集（正是 F2 多级传播那条的病灶）。
_CHUNK_SQL = (
    "SELECT id, namespace, visibility, state, updated_at, content_hash, superseded_by, "
    "derived_from, source_ids, retire_reason "
    "FROM chunks WHERE id=?"
)


def _chunk_revision(row: sqlite3.Row) -> str:
    """chunk 行的版本指纹：ACL(namespace/visibility) + 内容哈希 + 生命周期 + 代次。

    ⚠️ 语义是"值变了指纹就变"，不是密码学承诺——别拿它当签名用。
    """
    parts = [str(row[k] if k in row.keys() else "") or "" for k in _CHUNK_VERSION_COLS]
    return "|".join(parts)


class SourceResolver:
    """统一来源解析器：source kind → (读者集合, 版本)。

    契约：
    入参：conn（sqlite3 连接；None = 延迟到首次使用时由调用方注入）
    返回：——
    副作用：只读；tighten_source() 例外（显式写路径，调用方负责事务）
    谁调用：vector_store / context_builder / 未来 CST01、UX02
    改动影响：providers 的键是 source kind，加类型走 register_provider()，别改 if-else 链
    """

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self._conn = conn
        # provider: (conn, ref) -> SourceSnapshot
        self._providers: Dict[str, Callable[[Optional[sqlite3.Connection], SourceRef], SourceSnapshot]] = {
            "chunk": self._resolve_chunk,
        }
        # ★F01（2026-10-16）：把**进程级默认 provider** 一并装上。
        #   病根实测：扩展类型（conversation_event）只在“某个已经建好的 resolver”上补挂，
        #   而引擎可能在注册之后才创建按库 resolver ⇒ 新槽没有 provider ⇒ install 报成功、
        #   实际查询仍不可用。修法是从**构造点**就带上，堵住“注册早于建槽”的时间差。
        self._providers.update(_DEFAULT_PROVIDERS)
        # 明确登记"规范要求但当前不可用"的来源类型——T17/U08 的其它来源不得被当成"已支持"。
        self._unavailable: Dict[str, str] = {
            "conversation_event": "conversation event 来源表尚未建成（CST01）",
            "media": "媒资来源表尚未建成（UX02）",
        }
        for _kind in _DEFAULT_PROVIDERS:
            self._unavailable.pop(_kind, None)

    # ---------- 连接 ----------
    def bind(self, conn: sqlite3.Connection) -> "SourceResolver":
        """绑定/替换连接（引擎常驻连接会被重建，别在构造时就把连接钉死）。"""
        self._conn = conn
        return self

    def register_provider(self, kind: str,
                          fn: Callable[[Optional[sqlite3.Connection], SourceRef], SourceSnapshot],
                          *, unavailable_reason: Optional[str] = None) -> None:
        """注册一种来源类型的解析器（扩展点，铁律 4）。"""
        if not kind or not callable(fn):
            raise ValueError("register_provider 需要 (kind, callable)")
        self._providers[str(kind)] = fn
        if unavailable_reason is None:
            self._unavailable.pop(str(kind), None)
        else:
            self._unavailable[str(kind)] = str(unavailable_reason)

    # ---------- 单条解析 ----------
    def _resolve_chunk(self, conn, ref: SourceRef) -> SourceSnapshot:
        if conn is None:
            return SourceSnapshot(ref=ref, present=False, readers=CONFINED,
                                  revision="", state="", namespace="", visibility="")
        try:
            row = conn.execute(_CHUNK_SQL, (ref.id,)).fetchone()
        except Exception:
            # ⚠️ 查询失败 ≠ 来源是共享的。保守：围栏（铁律 3）。
            return SourceSnapshot(ref=ref, present=False, readers=CONFINED,
                                  revision="", state="", namespace="", visibility="")
        if row is None:
            # 来源查不到（硬删过 / id 写错 / 权限收紧后被清）——保守围栏，【不】回落 private 字串。
            return SourceSnapshot(ref=ref, present=False, readers=CONFINED,
                                  revision="", state="", namespace="", visibility="")
        ns = str(row["namespace"] or "")
        vis = str(row["visibility"] or "")
        state = str(row["state"] or "")
        # ★★F2-5/F2-6（2026-10-14 第二轮返修）：**隔离来源的读者集合归零**。
        #   病灶（独立验收实测）：A(dsh/shared)→B(zcode/shared)→C(zcode/shared)，
        #   收紧 A 后 B 被隔离（archived，visibility 仍是 shared），但 _resolve_chunk
        #   只按旧 visibility 求 readers ⇒ 读 B 时仍得 ALL_READERS ⇒ C 重算后
        #   依然是 shared/active，且以 B 为 source_ids 再派生新行也照样放行。
        #   ⇒ 隔离行的**权限必须归零**（CONFINED），这样：
        #     ① 多级传播：C 的来源交集 = {zcode} ∩ ∅ = ∅ ⇒ 编不出来 ⇒ C 一并被隔离；
        #     ② 新派生：derived_visibility_for_write 拿到的交集为空 ⇒ 拒绝/隔离，
        #        隔离正文不得经新派生重获普通读取资格。
        #   ⚠️ 判据是**退役原因前缀**而不是 state=='archived'（见 visibility.is_quarantined）：
        #      正常知识退役的权限面没有被否定，把它也归零会误伤历史读取。
        #   ⚠️【不】把它当成 present=False：行还在、证据还在（只是权限归零）。
        #      两者对 known_readers 的作用相同（都压到 CONFINED），语义更诚实。
        if is_quarantined(row):
            return SourceSnapshot(
                ref=ref, present=True, readers=CONFINED,
                revision=_chunk_revision(row),
                state=state, namespace=ns, visibility=vis,
            )
        return SourceSnapshot(
            ref=ref, present=True, readers=reader_set_of(vis, ns),
            revision=_chunk_revision(row),
            state=state, namespace=ns, visibility=vis,
        )

    def resolve(self, ref: Any) -> SourceSnapshot:
        """解析单条来源。未知类型 / 不可用类型 → present=False + 围栏（保守）。"""
        r = SourceRef.parse(ref)
        if r.kind in self._unavailable and r.kind not in self._providers:
            return SourceSnapshot(ref=r, present=False, readers=CONFINED, revision="")
        fn = self._providers.get(r.kind)
        if fn is None:
            return SourceSnapshot(ref=r, present=False, readers=CONFINED, revision="")
        return fn(self._conn, r)

    def resolve_many(self, refs: Iterable[Any]) -> List[SourceSnapshot]:
        return [self.resolve(r) for r in refs]

    # ---------- 多源交集 ----------
    def intersection_of(self, refs: Sequence[Any], *,
                        require_all_present: bool = True) -> ResolveResult:
        """全部来源读者集合的**交集**；查不到的来源把交集压到围栏。

        契约：
        入参：refs（来源序列；空序列 → 拒绝，见下）/ require_all_present（缺一即拒）
        返回：ResolveResult（visibility=None 表示"编码不出来，调用方必须拒绝生成"）
        为什么空来源也拒：Spec §9.3「空/缺失来源保守处理」——
            "没有来源"不等于"人人都能读"，也不等于"owner 能读"；一律拒绝生成。
        """
        snapshots = self.resolve_many(refs)
        refusals: List[str] = []
        if not snapshots:
            return ResolveResult(readers=CONFINED, visibility=None,
                                 refusals=["empty_source_set"], snapshots=[],
                                 known_readers=CONFINED, missing=0)
        readers: ReaderSet = ALL_READERS
        # ★R1-2：与 readers 平行维护"只看可解析来源"的交集。查不到的来源不进这一份——
        #   它只表示"真交集不可知"，**不**表示"已知来源的约束不算数"。
        known: ReaderSet = ALL_READERS
        missing = 0
        for s in snapshots:
            if not s.present:
                refusals.append("source_unavailable:%s:%s" % (s.ref.kind, s.ref.id))
                missing += 1
                if require_all_present:
                    # ⚠️ 保守：一条来源不可解析 ⇒ 真交集不可知。
                    #     【不】"跳过这条继续算"——那会把被移除来源的读者重新放进来。
                    readers = CONFINED
                    continue
            readers = intersect(readers, s.readers)
            known = intersect(known, s.readers)
        return ResolveResult(readers=readers, visibility=None, refusals=refusals,
                             snapshots=snapshots, known_readers=known, missing=missing)

    def derived_visibility(self, refs: Sequence[Any], namespace: str, *,
                           require_all_present: bool = True) -> ResolveResult:
        """派生记忆应落的 visibility：= best_visibility_for(来源交集, namespace)。

        ⚠️ 本函数是**纯解析**：交集编不出来时 visibility=None，**不**替调用方做
           "拒绝还是围栏"的决定。写路径请用 derived_visibility_for_write()——
           那个才是"落库决策"的唯一权威（两个写入口必须得到同一个答案）。
        """
        res = self.intersection_of(refs, require_all_present=require_all_present)
        vis = best_visibility_for(res.readers, namespace)
        if vis is None and not res.refusals:
            # 交集非空但编码不出来 —— 典型形态：交集是空的 frozenset，或交集里没有拟落的域。
            res.refusals.append("readers_not_expressible_for_namespace:%s" % (namespace or "<empty>"))
        res.visibility = vis
        return res

    def derived_visibility_for_write(self, refs: Sequence[Any], namespace: str, *,
                                     declared_visibility: Optional[str] = None) -> Dict[str, Any]:
        """**写路径唯一决策函数**：派生行该落什么 visibility / 能不能落。

        derived_visibility_for_write()：契约
        入参：refs（来源序列）/ namespace（拟落的域）
        返回：{"ok": bool, "visibility": str|None, "action": str, "readers": ..., "refusals": [...]}
        副作用：无（纯计算）
        谁调用：context_builder.store() 与 vector_store 的写侧闸——**两个入口必须调同一个**
        改动影响：返回的 visibility 会同时写进 WAL 和 DB（M01 契约 §5「同一份规范化载荷」），
                 所以本函数必须确定性：同样的入参永远给同样的答案。

        为什么要单独开一个函数（而不是让 context_builder 用 derived_visibility 自己判）：
          写路径有两个入口（context_builder.store 与 vector_store 的 _normalize/闸），
          M01 契约要求 WAL 与 DB 的 visibility **同源**。两个入口各判一次 = 两套口径，
          正是 M03 要根治的病。⇒ 决策只在这里做一次，两边都问它。

        ★严格性分级（**有意为之**，Spec §9.3 末句给了两个选项，按情形取不同选项）：
          · action="strict"：来源**全部可解析** → 交集就是真交集。
              - 交集能编码 → 用它（这就是 §9.3 的正解）。
              - 交集**编不出来**（不含拟落域 / 为空）→ **拒绝落库**（ok=False）。
                为什么不围栏成 private：private 会授予**拟落域**阅读权，而拟落域根本不在
                来源读者集合里 ⇒ 那是**凭空授权**，正是 I09 的复发形态。
          · action="conservative"：有来源**查不到** → 真交集不可知。
              取 §9.3 的第二个选项「**保留受限归属**」：落库，但**已知来源的约束照旧**。

        ★★R1-1 / R1-2（2026-10-14 独立验收返修，两处都是实机复现的绕过面）：
          R1-1 **声明范围是额外上界**：调用方主动把派生收窄到 private，不能因为
               "来源是 shared" 就被改宽成 shared。正解 = 同时满足【声明范围】与【全部来源范围】
               ⇒ 落库读者 = 来源交集 ∩ 声明读者集合。旧实现把 declared 只拿去打 warning
               然后无条件覆盖（declared 形同虚设）。
          R1-2 **查不到的来源不得解除已知来源的约束**：旧实现见一条 missing 就把整个
               交集压到 CONFINED，保守档直接落 'private' ⇒ 落成**拟落域**的 private。
               于是 `source_ids=[已知的 dsh/private, 不存在的 id]` 落 zcode/private：
               已知来源本该拒的，**加一条不存在的来源就能绕过**。
               正解 = 用 known_readers（仅可解析来源的交集）当**上界**，
               再与"保留受限归属"的保守值（拟落域 private）取交：
                 · 交得出来 → 落那个值（M01 的"来源全不可解析"遗留行仍能落库，不丢数据）
                 · 交不出来（如已知源 {dsh} ∩ 拟落域 {zcode} = ∅）→ **拒绝落库**：
                   此时唯一能保全数据又不出卖权限的编码不存在，凭空授权必须被挡住。
        """
        res = self.intersection_of(refs, require_all_present=True)
        # ★R1-1：声明范围是**额外上界**。调用方主动收窄（声明 private）必须保住——
        #   "来源是 shared" 只说明**可以**给更多人读，不说明调用方**必须**给。
        #   声明缺省（None）或声明 shared ⇒ ALL_READERS ⇒ 交集不变（对旧行为零影响）。
        declared_readers: ReaderSet = ALL_READERS
        if declared_visibility:
            declared_readers = reader_set_of(str(declared_visibility), namespace)

        if not res.refusals:
            readers = intersect(res.readers, declared_readers)
            vis = best_visibility_for(readers, namespace)
            if vis is None:
                res.refusals.append(
                    "readers_not_expressible_for_namespace:%s" % (namespace or "<empty>"))
                return {"ok": False, "visibility": None, "action": "strict",
                        "reason": "inexpressible_intersection",
                        "readers": readers, "known_readers": res.known_readers,
                        "declared_readers": declared_readers,
                        "refusals": res.refusals, "snapshots": res.snapshots}
            return {"ok": True, "visibility": vis, "action": "strict", "reason": "intersection",
                    "readers": readers, "known_readers": res.known_readers,
                    "declared_readers": declared_readers,
                    "refusals": [], "snapshots": res.snapshots}

        # 有来源查不到 → 真交集不可知。**不拒绝**（否则 WAL 重放/已删源遗留行整批打死），
        # 取 §9.3「保留受限归属」：候选值 = 拟落域的 'private'（最窄的、能表达该域存在的编码）。
        #
        # ★R1-2：但必须再与**已知来源的交集**取交（known_readers）。查不到的来源
        #   只让真交集不可知，**不**解除已知来源的约束——否则"多加一条不存在的 id"
        #   就成了绕过已知 private 来源的万能钥匙（独立验收实机复现）。
        confinement = reader_set_of("private", namespace)
        readers = intersect(confinement, intersect(res.known_readers, declared_readers))
        vis = best_visibility_for(readers, namespace)
        if vis is None:
            # 交得出来的话就落；交不出来 ⇒ **拒绝**。这里没有既保住行、又不凭空授权的编码：
            # 已知来源的读者里若不含拟落域（例如已知源 {dsh} 而拟落 zcode），落 'private'
            # 就是把这条**送给了 zcode**——即使原始来源查不到，也不该把它交给一个
            # 已知来源明确排除的域。宁可拒（证据仍在来源行/WAL 里）也不凭空授权。
            res.refusals.append(
                "known_readers_exclude_target_namespace:%s" % (namespace or "<empty>"))
            return {"ok": False, "visibility": None, "action": "conservative",
                    "reason": "unresolvable_source_cannot_exclude_known_readers",
                    "readers": readers, "known_readers": res.known_readers,
                    "declared_readers": declared_readers,
                    "refusals": res.refusals, "snapshots": res.snapshots}
        return {"ok": True, "visibility": vis, "action": "conservative",
                "reason": "unresolvable_source_confined",
                "readers": readers, "known_readers": res.known_readers,
                "declared_readers": declared_readers,
                "refusals": res.refusals, "snapshots": res.snapshots}

    # ---------- 受众投影（U08 / T11） ----------
    def project_for_audience(self, rows: Sequence[Any], audience: str, *,
                             entry_of: Callable[[Any], Tuple[str, str]] = None) -> List[Any]:
        """按**目标受众**投影一组记忆行：只保留「受众整体都能读」的行（U08 分享回忆册）。

        契约：
        入参：rows（记忆行，可为 sqlite3.Row / dict）/ audience（目标受众身份）/
               entry_of(row) -> (visibility, namespace)（缺省自动适配 Row/dict）
        返回：投影后的子集（顺序保持）
        为什么判据是"读者集合 ⊇ {audience}"而不是"当前调用者能读"：
            分享是**替别人做代理决策**——调用者自己能读 ≠ 受众能读。
            拿调用者视角过滤就是经典越权投影（I09 的同族）。
        ★F2-3（2026-10-14 第二轮返修）：**被权限隔离的行不得投影给任何非管理员受众**。
            病灶（独立验收实测）：隔离行 state='archived' 但 visibility 仍是 shared，
            本函数只按 visibility 算读者集 ⇒ 隔离行照样进了受众投影（实测 projected_ids
            含 'quarantined'）。隔离的语义是"该给谁读**算不出来**"，那就不存在
            "受众整体都能读"这回事 ⇒ 必须拦。判据走 visibility.is_quarantined（唯一实现）。
            ⚠️ 用 state=='archived' 判是**错的**：正常知识退役不是权限语义，
            把它也拦掉属过度收紧（独立验收明说"不能仅使 active-only 召回看不到就宣称拦"，
            反过来也不能把所有 archived 都当越权）。
        """
        out: List[Any] = []
        for row in rows:
            # ★F2-3：隔离行对任何普通受众都不可投影（管理员审计另走显式授权路径）
            if str(audience or "") != "__admin__" and is_quarantined(row):
                continue
            vis, ns = entry_of(row) if entry_of else _auto_entry(row)
            reader_set = reader_set_of(vis, ns)
            if _is_all(reader_set) or (audience and audience in reader_set):
                out.append(row)
        return out

    def renderable_spans(self, rows: Sequence[Any], viewer: str, *,
                         purpose_ids: Optional[Iterable[str]] = None,
                         entry_of: Callable[[Any], Tuple[str, str]] = None,
                         id_of: Callable[[Any], str] = None) -> List[Any]:
        """把一组行裁剪成「本轮目的相关且 viewer 有权限」的可渲染片段（T11）。

        契约：
        入参：rows / viewer / purpose_ids（本轮目的涉及的 id；None = 不过滤）/
               entry_of、id_of（缺省自动适配）
        返回：可进入生成正文的行子集
        与 project_for_audience 的区别（长得像、判据完全不同）：
            project_for_audience 问「**受众**能不能读」——用于分享/投影；
            renderable_spans    问「**本轮目的**用得着吗 + viewer 有没有权」——用于生成。
        ⚠️ T11 的红线是"接了必要内容、禁止多带细节"：DM 相关的私人细节若与本轮目的无关，
            即使 viewer 有权限也不进正文。所以这里必须**先过权限、再过目的**，两个条件都满足。
        ★F2-4（2026-10-14 第二轮返修）：**目的相关性不能替代当前授权**。
            病灶（独立验收实测）：隔离行（archived）只按 visibility 过闸 ⇒
            renderable_spans(viewer='zcode', purpose_ids=[隔离行]) 照样保留它
            （实测 renderable_ids 含 'quarantined'）。
            隔离 = 该给谁读算不出来 ⇒ 它**不**因为"本轮目的正好涉及"而获得可渲染资格。
            ⇒ 权限闸改用 visibility.entry_visibility_ok（统一闸，自动消费隔离态）。
        """
        purpose = None if purpose_ids is None else {str(x) for x in purpose_ids}
        out: List[Any] = []
        for row in rows:
            vis, ns = entry_of(row) if entry_of else _auto_entry(row)
            # 闸一：权限（永远先跑，Spec §5.5#1「可见性筛选先于候选/摘录/返回」）
            # ★F2-4：走统一闸——它同时消费"可见性"与"权限隔离"，隔离行一律不进正文
            #   （管理员显式审计除外，由 entry_visibility_ok 内部放行 __admin__）。
            #   ⚠️ 这里【不】直接用 visibility_ok：那样隔离行会被放进来（本条的病灶）。
            if not entry_visibility_ok(row, viewer or ""):
                continue
            # 闸二：本轮目的相关性
            if purpose is not None:
                rid = id_of(row) if id_of else _auto_id(row)
                if str(rid) not in purpose:
                    continue
            out.append(row)
        return out

    # ---------- 收紧传播（I10） ----------
    def tighten_source(self, src_id: str, new_visibility: str, *,
                       _seen: Optional[set] = None, _depth: int = 0) -> Dict[str, Any]:
        """源权限收紧的唯一入口：把新值写回，并**重算全部后代派生**的读者集合。

        契约：
        入参：src_id（被收紧的 chunk）/ new_visibility（新可见性，必须合法）
              _seen / _depth = **内部递归参数，调用方【不】传**
        返回：{"ok", "source_changed", "derived_scanned", "derived_tightened",
               "derived_refused", "derived_quarantined", "detail", "changed_ids"}
        副作用：UPDATE chunks(源) + UPDATE/quarantine(派生) + xtmem_events 留痕
        事务：★R4-2（2026-10-14 返修）**只在"事务由本函数开启"时才 commit**。
              调用方已经 BEGIN 的，本函数【不】偷提交——那会让调用方的 rollback 失效
              （独立验收实测：调用方 BEGIN 后写一条审计记录，本函数一 commit，
               调用方 rollback 后那条记录仍然存在）。

        ⚠️ 为什么重算是必须的：旧 follow_source_visibility 只对每个派生取
            「源新值 + 该派生当前值」两者 min —— 多源派生在**某一个**源收紧时，
            其余来源的读者集合根本没被重新纳入，交集可能算错（漏收 or 过收）。
            必须回到「该派生的全部来源重新求一次交集」才是 Spec §9.3 的语义。

        ★★R2 四连修（2026-10-14 独立验收，全部实机复现）：
          R2-1 **值没变也要传播**：旧实现见 `old_vis == nv` 直接返回 changed=0。
               但既有调用方（vector_store.follow_source_visibility /
               schema.quarantine_bad_visibility）是**先把源改掉再调用本函数**的——
               等本函数看到时值已经相等，于是"传播"整条空转（实测 changed 恒 0）。
               ⇒ 值相等只表示"源不用再改"，**不**表示"派生是对的"。一律重算。
          R2-2 **跨域收紧不得用拟落域 private 顶替**：交集在拟落域下编码不出来时，
               旧实现在**拟落域**落 'private' ⇒ 那个域反而能读了（凭空授权）。
               正解见下方 _quarantine：**隔离/失效**（行留着、证据留着、谁都读不到）。
          R2-3 **必须传播到多级**：A→B→C 链上收紧 A，旧实现只改 B，C 仍 shared。
               ⇒ 每改一个后代就递归传播它自己的后代（带 _seen 防环）。
          R2-4 **【不】接受放宽**：本函数文档一直写着"只收紧不放宽"，但**代码里没有校验**。
               实测 tighten(private→shared) 返回 ok=true 且源真的变宽了。
               ⇒ 落库前校验 new ⊄ old 即拒（放宽要走另一条显式授权路径）。

        ★★F4（2026-10-14 第二轮返修）：**自持事务中途失败必须回滚**。
          病灶（独立验收实测）：owns_transaction=true，把来源改成 private 后，
          枚举子代时抛异常——异常向上传递，但连接仍 in_transaction=true、没有回滚；
          随后同一 VectorStore 正常写一条无关原始记录，**它的 commit 顺手把半截收紧
          提交了**（来源 private、派生仍 shared）⇒ 重新产出越源状态。
          ⇒ 修法：事务归属判定提到**写库之前**，impl 抛异常时若本函数持锁就回滚。
          ⚠️【不】在"事务属于调用方"时也回滚：那会切碎调用方的事务
             （R4-2 的同一条纪律：谁开的事务谁负责收）。
          ⚠️ 要回滚的是**异常**，不是"返回 ok=False"：后者（放宽被拒、来源不存在）
             发生在任何写之前，没有半成品可回滚。
        """
        conn = self._conn
        # ★F4：事务归属必须在**第一次写之前**判定——impl 里先写后判会不准
        #   （写语句本身会把 in_transaction 变成 True）。
        owns_txn = (conn is not None) and (not conn.in_transaction)
        try:
            return self._tighten_source_impl(
                src_id, new_visibility, owns_txn=owns_txn, _seen=_seen, _depth=_depth)
        except BaseException:
            # ★F4：拥有事务时异常回滚——否则半截收紧留在事务里，等着被下一条
            #   "无关的正常写入"顺手提交（独立验收实测的正是这个时序）。
            if owns_txn and conn is not None:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                    logger.warning(
                        "tighten_source 中途失败，已回滚自持事务（F4）：src=%s", src_id)
                except Exception:
                    logger.exception(
                        "tighten_source 回滚失败（F4）：src=%s —— 事务可能残留半成品", src_id)
            raise

    def _tighten_source_impl(self, src_id: str, new_visibility: str, *,
                             owns_txn: bool, _seen: Optional[set] = None,
                             _depth: int = 0) -> Dict[str, Any]:
        """tighten_source 的实际实现（★F4 拆出：让外层统一做"异常即回滚"）。

        ⚠️【不】直接调本方法——事务回滚契约在外层 tighten_source 上。
           拆出它是因为"异常时回滚自持事务"需要一个能包住整个函数体的 try 块，
           而 Python 的 try 只能包住一段代码 ⇒ 被包的代码得是独立函数。
        """
        from memory.visibility import normalize_visibility, reader_set_of, is_subset
        nv = normalize_visibility(new_visibility)
        if nv is None:
            return {"ok": False, "error": "invalid_visibility", "value": new_visibility}
        conn = self._conn
        if conn is None:
            return {"ok": False, "error": "no_connection"}
        # ★F4：owns_txn 由外层 tighten_source 在"第一次写之前"判定并传入
        #   （理由见外层 docstring：写语句会污染 in_transaction）。

        row = conn.execute("SELECT id, visibility, namespace FROM chunks WHERE id=?", (src_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "source_not_found", "id": src_id}
        old_vis = str(row["visibility"] or "")
        src_ns = str(row["namespace"] or "")

        # ★R2-4：**只收紧不放宽**。读者集必须 ⊆ 旧读者集。
        #   ⚠️ 这里比的是"读者集合"而不是"字串"——跨域时字串比较会说谎（见模块抬头）。
        old_readers = reader_set_of(old_vis, src_ns)
        new_readers = reader_set_of(nv, src_ns)
        if not is_subset(new_readers, old_readers):
            logger.warning(
                "tighten_source 拒绝放宽：%s 的读者集合 %s → %s 不是收缩（R2-4）",
                src_id, _readers_repr(old_readers), _readers_repr(new_readers))
            return {"ok": False, "error": "widening_not_allowed", "id": src_id,
                    "old_visibility": old_vis, "requested": nv,
                    "old_readers": _readers_repr(old_readers),
                    "new_readers": _readers_repr(new_readers)}

        now = time.time()
        source_changed = (old_vis != nv)
        if source_changed:
            conn.execute("UPDATE chunks SET visibility=?, updated_at=? WHERE id=?",
                         (nv, now, src_id))

        detail: Dict[str, Any] = {}
        tightened = refused = quarantined = 0
        changed_ids: List[str] = []
        child_ok = True
        if source_changed:
            changed_ids.append(src_id)

        # ★R2-1：**不管源值有没有变，都重算后代**。值相等只说明源本身不用改。
        seen = _seen if _seen is not None else set()
        seen.add(str(src_id))

        for cid in self._find_derived_of(src_id):
            if str(cid) in seen:
                continue
            sub = SourceResolver(conn)
            refs = sub._source_ids_of(cid)
            ns_c = sub._namespace_of(cid)
            # ★用 derived_visibility_for_write（而不是 derived_visibility）：
            #   后者只算交集、不管"落不落得下去"，本函数需要的是**落地决策**——
            #   而且这两个写入口的决策必须同源（铁律 5），不能在这里另判一套。
            decision = sub.derived_visibility_for_write(refs, ns_c)
            cur = sub._current_visibility(cid)

            if not decision["ok"]:
                # ★R2-2：交集**编不出来** ⇒ 隔离/失效，而不是"落拟落域 private"。
                #   （落拟落域 private = 那个域反而能读 = 凭空授权，正是 I09 复发形态。）
                act = sub._quarantine(cid, ns_c, now, decision.get("refusals") or [])
                quarantined += 1
                refused += 1
                if act["changed"]:
                    tightened += 1
                    changed_ids.append(cid)
                detail[cid] = {"action": "quarantined", "from": cur, **act}
                # 已被隔离的行自身权限面归零，但它**子代**仍要重算
                # （子代的来源集合含这条，交集随之变化）。传它**当前**值即可——
                # 值没变不表示子代没变，这正是 R2-1 的要点。
                effective = cur
            else:
                effective = sub._tightest_of(cur, decision["visibility"], ns_c)
                if effective != cur:
                    conn.execute("UPDATE chunks SET visibility=?, updated_at=? WHERE id=?",
                                 (effective, now, cid))
                    tightened += 1
                    changed_ids.append(cid)
                detail[cid] = {"action": "recomputed", "from": cur, "to": effective}

            # ★R2-3：递归传播到**多级**（A→B→C 链上只改 B 是旧实现的漏洞）。
            # ⚠️ 递归传的是 `effective`（本行**实际落到**的值），不是 decision["visibility"]：
            #    后者可能比当前值**宽**，而在 R2-4 之后 tighten_source 会拒绝放宽 ⇒
            #    递归会整条中止（这是返修中自己踩出来的，别改回去）。
            _r = sub.tighten_source(cid, effective,
                                    _seen=seen | {str(cid)}, _depth=_depth + 1)
            detail[cid]["downstream"] = _r.get("derived_tightened", 0)
            tightened += int(_r.get("derived_tightened", 0) or 0)
            quarantined += int(_r.get("derived_quarantined", 0) or 0)
            if _r.get("changed_ids"):
                changed_ids.extend([x for x in _r["changed_ids"] if x not in changed_ids])

        # 只在最外层记一条事件（递归层【不】重复记，否则事件表被同一个源刷屏）
        if _depth == 0:
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-vis-follow', ?)",
                (now, json.dumps({
                    "source_id": src_id, "old_vis": old_vis, "new_vis": nv,
                    "scanned": len(detail), "tightened": tightened, "refused": refused,
                    "quarantined": quarantined,
                    "by": "M03-SourceResolver",
                }, ensure_ascii=False)),
            )
            # ★R4-2：**只提交自己的事务**。调用方 BEGIN 的，交还给他。
            if owns_txn:
                conn.commit()
        return {"ok": True, "source_changed": source_changed,
                "derived_scanned": len(detail),
                "derived_tightened": tightened, "derived_refused": refused,
                "derived_quarantined": quarantined,
                "detail": detail, "changed_ids": changed_ids,
                "owns_transaction": owns_txn, "child_ok": child_ok}

    def _quarantine(self, cid: str, ns: str, now: float,
                    refusals: Optional[List[str]] = None) -> Dict[str, Any]:
        """把一个"权限算不出来"的派生行**隔离/失效**（Spec §9.3「保留受限归属」的正解）。

        _quarantine()：契约
        入参：cid（派生行）/ ns（它的域）/ now（时间戳）/ refusals（为什么隔离，进审计）
        返回：{"changed": bool, ...}
        副作用：UPDATE chunks SET state='archived' + 退役三列 + updated_at；**不删数据**

        ⚠️ 为什么【不】用"落拟落域 private"当围栏（这是独立验收 R2-2 的返修点）：
            'private' 是 **namespace 相对** 语义。对一条挂在 zcode 的行写 private
            = **zcode 能读**。而"算不出该给谁读"恰恰意味着**不能确定 zcode 该不该读**，
            通常它根本不在来源读者集合里 ⇒ 落 private 就是把这条**白送给 zcode**。
            实测（m03-review/evidence/boundary-results.json 的
            I10-cross-owner-tightening-keeps-reader-subset）：旧实现正是这么漏的。
        ⚠️ 为什么用 state 而不是新增一个 visibility 字面量：
            chunks.visibility 上有 BEFORE INSERT/UPDATE 触发器，只认
            shared/private/shared-with:*（memory/schema.py 的 trg_chunks_visibility_*）。
            塞第四种值会被触发器 ABORT（或被 quarantine_bad_visibility 当脏值清掉）。
            而 state≠'active' 会让所有 `state='active'` 的读路径（CLI/hybrid/recall/
            embedding 反查）**一致地看不到它**——正好是"隔离"要的语义。
        ⚠️ 【不】把 state 改成 'superseded'：那是"被某条新记忆取代"（要 superseded_by），
            语义不同。这里用 'archived' = 知识退役/不再参与召回，与 trace.py 的口径一致。
        """
        conn = self._conn
        # ★F2：隔离标记走 visibility.QUARANTINE_MARK（唯一实现）——读侧六条入口一律按
        #   同一个前缀识别。⚠️【不】在本地重写这个字面量：改一处漏一处就是 M03 的病根。
        why = QUARANTINE_MARK + ";".join((refusals or [])[:2])
        cur = conn.execute("SELECT state, visibility FROM chunks WHERE id=?", (cid,)).fetchone()
        if cur is None:
            return {"changed": False, "reason": "row_missing"}
        old_state = str(cur["state"] or "")
        old_vis = str(cur["visibility"] or "")
        if old_state == "archived":
            # 已隔离：只补更新原因（幂等，不重复计数）
            return {"changed": False, "reason": "already_quarantined",
                    "old_visibility": old_vis, "state": "archived"}
        conn.execute(
            "UPDATE chunks SET state='archived', retire_reason=?, retired_by=?, retired_at=?, "
            "updated_at=? WHERE id=?",
            (why, QUARANTINE_OWNER, now, now, cid),
        )
        logger.warning(
            "M03 隔离派生 %s（ns=%r）：来源读者交集在拟落域下无法表达，"
            "已置 state='archived'（保留证据、拒绝一切普通读取）refusals=%s",
            cid, ns, refusals)
        return {"changed": True, "reason": "quarantined", "from_state": old_state,
                "old_visibility": old_vis, "state": "archived", "retire_reason": why}

    # ---------- 内部小工具 ----------
    def _source_ids_of(self, chunk_id: str) -> List[Any]:
        conn = self._conn
        row = conn.execute("SELECT source_ids FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if row is None:
            return []
        raw = row["source_ids"]
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            parsed = json.loads(raw or "[]")
        except Exception:
            return []
        return list(parsed) if isinstance(parsed, (list, tuple)) else []

    def _namespace_of(self, chunk_id: str) -> str:
        row = self._conn.execute("SELECT namespace FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return str((row["namespace"] if row else "") or "")

    def _current_visibility(self, chunk_id: str) -> str:
        row = self._conn.execute("SELECT visibility FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        return str((row["visibility"] if row else "") or "")

    def _tightest_of(self, a: str, b: str, ns: str) -> str:
        """只收紧不放宽的合并：取两者读者集合的**交集**再编码（同名域下等价于取更严）。

        ⚠️ 【不】用 min_visibility(a, b) 的字串口径：a 与 b 可能挂在不同域，
            字串比较会得到一个既不属于 a 也不属于 b 的第三种语义。
        ⚠️ ns 必须由调用方显式传入：本函数是纯函数（无连接依赖），
            早先版本读 self._namespace_of_current 这个恒为 "" 的占位属性，
            等于把两个集合都换算到空域上——是个"看起来能跑"的静默错。
        """
        ra = reader_set_of(a, ns)
        rb = reader_set_of(b, ns)
        best = best_visibility_for(intersect(ra, rb), ns)
        return best if best is not None else "private"

    def _find_derived_of(self, src_id: str) -> List[str]:
        """查 source_ids 含 src_id 的 active 派生（LIKE 带引号防半匹配，与旧实现同款）。"""
        try:
            rows = self._conn.execute(
                "SELECT id FROM chunks WHERE COALESCE(derived_from,'') != '' AND state='active' "
                "AND source_ids LIKE ?",
                ('%"' + str(src_id) + '"%',),
            ).fetchall()
        except Exception:
            return []
        return [r[0] for r in rows]

    # ---------- T17：旧计划失效 ----------
    def revalidate(self, snapshots: Sequence[SourceSnapshot]) -> Tuple[bool, List[Dict[str, Any]]]:
        """发送前复验来源快照（T17：source 修改/撤回发生在生成期间 → 弃旧计划）。

        契约：
        入参：生成期捕获的 snapshots
        返回：(仍然有效?, 变化清单)
        为什么必须逐条重解析而不是比时间戳：见 SourceSnapshot.revision 的注释。
        """
        changes: List[Dict[str, Any]] = []
        for s in snapshots:
            cur = self.resolve(s.ref)
            if (not cur.present) or cur.revision != s.revision \
                    or not is_subset(cur.readers, s.readers):
                changes.append({
                    "ref": s.ref.as_dict(),
                    "was": {"present": s.present, "revision": s.revision,
                            "readers": _readers_repr(s.readers)},
                    "now": cur.as_dict(),
                    "reason": ("source_removed" if not cur.present else
                               "revision_changed" if cur.revision != s.revision else
                               "acl_tightened_or_shifted"),
                })
        return (not changes), changes


def _auto_entry(row: Any) -> Tuple[str, str]:
    """从 sqlite3.Row / dict / 带属性的对象里取 (visibility, namespace)。"""
    try:
        if isinstance(row, dict):
            return str(row.get("visibility") or ""), str(row.get("namespace") or "")
        keys = row.keys()  # sqlite3.Row
        return str(row["visibility"] or ""), str(row["namespace"] or "")
    except Exception:
        return (str(getattr(row, "visibility", "") or ""),
                str(getattr(row, "namespace", "") or ""))


def _auto_id(row: Any) -> str:
    try:
        if isinstance(row, dict):
            return str(row.get("id") or "")
        return str(row["id"] or "")
    except Exception:
        return str(getattr(row, "id", "") or "")


# ================================================================
# 进程级单例
#
# 为什么要有：resolver 是无状态的（除了连接），每次 new 一个也行；
#   但调用点很多，单例能让"忘了 bind 连接"这件事尽早暴露（少一个都不行）。
# ⚠️ 连接会随引擎重建而失效，凡是用完一轮的都该 bind() 一次当前连接。
#
# ★★R-限制2（2026-10-14 返修）：**按库分槽**，不再是一个全局可变单例。
#   病灶（独立验收 I09-resolver-does-not-rebind-another-db 实测）：
#     旧实现只有一个 _RESOLVER。`get_resolver(c1)` 拿到 DB1 的解析器，
#     之后 `get_resolver(c2)` 会把**同一个对象**重绑到 DB2 ⇒ 先前持有的 r1
#     再 resolve 会读到 DB2 的内容（实测：DB1 的 same-id 读回来变成 DB2 的 zcode/shared）。
#     当前两个网关是分进程分库，所以不是"正在串库"，但它是同进程多库/并发的正确性地雷。
#   ⇒ 修法：以**连接指向的库文件**为键分槽。每个库一个 resolver，互不重绑。
#     ⚠️ 同一库重新建连接（引擎重连）时仍要 bind 到当前连接——否则缓存里的旧连接
#        可能是已关闭的句柄。所以"命中缓存 + 传了 conn"仍然 bind 一次（见下）。
#     取不到库路径（:memory: / 权限）时退回按连接身份分槽，避免两个内存库互相顶掉。
# ================================================================

_RESOLVERS: Dict[str, SourceResolver] = {}
_RESOLVER_DEFAULT: Optional[SourceResolver] = None


def _conn_key(conn: Optional[sqlite3.Connection]) -> str:
    """给连接算一个"它指向哪个库"的键。取不到路径时退回按对象身份。"""
    if conn is None:
        return "<none>"
    try:
        for row in conn.execute("PRAGMA database_list"):
            # row[1] 是库名（main/temp/...），row[2] 是文件路径（内存库为空串）
            if str(row[1]) == "main":
                p = str(row[2] or "")
                if p:
                    return p
                break
    except Exception:
        pass
    return "<conn:%d>" % id(conn)


def register_default_provider(kind: str, fn: Callable[..., SourceSnapshot]) -> int:
    """把一个来源类型登记为**进程级默认**：已存在的槽立刻补挂，新建的槽构造时自动带上。

    契约：
    入参：kind（来源类型名）、fn（(conn, ref) -> SourceSnapshot）
    返回：本次被立刻补挂的**已存在** resolver 数量
    副作用：改进程内解析器的 provider 表（不改任何库、不放宽权限）
    谁调用：CST01 conversation_source.install() / UX02 的同类安装入口
    改动影响：不登记 ⇒ 新建的按库 resolver 会漏掉该来源类型（F01 的假就绪形态）。

    ⚠️ 已经 bind 过的按库槽也要补：它们正是引擎现在手里用的那些实例。
    """
    if not kind or not callable(fn):
        raise ValueError("register_default_provider 需要 (kind, callable)")
    _DEFAULT_PROVIDERS[str(kind)] = fn
    bound = 0
    for resolver in iter_resolvers():
        try:
            resolver.register_provider(str(kind), fn)
            bound += 1
        except Exception:  # noqa: BLE001 —— 单槽失败不阻断其它槽（下次构造仍会自动带上）
            continue
    return bound


def iter_resolvers() -> List["SourceResolver"]:
    """列出当前进程内**已经创建**的全部解析器实例（默认槽 + 各按库槽）。

    为什么需要它（F01 实测形态）：旁路 provider（如 conversation_event）可能在
    “某个库的解析器已经建好之后”才注册。只挂默认槽的话，真实引擎用的那个按库实例
    仍然没有 provider —— 于是 install() 报 installed=True，实际库里却查不到来源。
    本函数让“后装注册”能把**已存在的槽**一次性补齐。

    ⚠️【不】拿它替代按库注册：正常情况下仍必须走 get_resolver(conn)（那才是引擎用的实例）。
       它只解决“注册晚于建槽”的时间差，不改变“一个库一个 resolver”的语义。
    ⚠️ 返回的是**活对象**列表（不是快照）：调用方注册 provider 会作用到引擎手里的同一个实例。
    """
    out: List[SourceResolver] = list(_RESOLVERS.values())
    if _RESOLVER_DEFAULT is not None:
        out.append(_RESOLVER_DEFAULT)
    return out


def get_resolver(conn: Optional[sqlite3.Connection] = None) -> SourceResolver:
    """取**该连接所指向的库**对应的 resolver；传 conn 则同时重新绑定连接。

    ⚠️ 与旧实现的语义差别（R-限制2）：旧版是"一个全局对象，谁传 conn 就重绑给谁"，
       于是先前取到的引用会被后来的调用悄悄改到别的库上。现在按库分槽，
       拿到的引用**只会指向它自己那个库**。
    """
    global _RESOLVER_DEFAULT
    if conn is None:
        if _RESOLVER_DEFAULT is None:
            _RESOLVER_DEFAULT = SourceResolver(None)
        return _RESOLVER_DEFAULT
    key = _conn_key(conn)
    r = _RESOLVERS.get(key)
    if r is None:
        r = SourceResolver(conn)
        _RESOLVERS[key] = r
    else:
        # 同一库可能换了连接对象（引擎重连）⇒ 必须 rebind 到当前这个
        if r._conn is not conn:
            r.bind(conn)
    return r
