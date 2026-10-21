# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · 记忆可见性 —— 全库唯一实现（判定 + 读者集代数）
#
# ▍这是什么
# ① 干什么：回答两个互逆的问题——
#      「某身份能不能读这条？」     → visibility_ok(vis, ns, viewer)
#      「这条到底谁能读？」         → reader_set_of(vis, ns)  （M03 新增）
#    以及多来源合成一条派生记忆时的读者集**交集**语义。
# ② 技术栈：纯 Python，零依赖（本模块被 CLI / provider / 引擎 / curator 到处 import，
#    不能有任何 import 环或重依赖）。
# ③ 依赖：无。★这是故意的★——所有人 import 它，它不 import 任何人。
# ④ 给谁看：CLI / provider / curator / vector_store / trace / cockpit 的维护者。
#
# ▍为什么 M03 要在这里加「读者集」（2026-10-13，血泪）
# visibility 是 **namespace 相对** 语义：
#     shared          → 所有人
#     private         → 仅 chunk_namespace 这个域
#     shared-with:a,b → chunk_namespace 域 + 名单 a,b
# ⇒ 'private' 这个字串**不带域名**：挂在 ns=dsh 上是「dsh 能读」，挂在 ns=zcode 上是
#    「zcode 能读」。于是「取最严可见性字串」（旧 min_visibility）与 Spec §9.3 要的
#    「取全部来源读者的**交集**」**不是一回事**：
#       源 = (ns=dsh, vis='private')  ⇒ 读者 {dsh}
#       取最严字串 → 'private'
#       派生落在 ns=assistant ⇒ 读者变成 {assistant}
#       🔴 实际读者集合扩大了：dsh 读不到，assistant 却读得到（I09 实机复现）
# 修法：把「读者集合」提成一等公民，字串只是它的一种编码（best_visibility_for 负责投影）。
#
# ▍读者集代数（⚠️ 改前必读）
# 顶元素 ALL_READERS（人人可读，对应 'shared'）与底元素 CONFINED（除管理员无人）都是
# **哨兵单例**，不是普通 frozenset——"人人"这个集合根本枚举不完。
# ⚠️【不】用 `a & b` / `a | b` 直接算：ALL_READERS 不是 set，Python 会先抛 TypeError；
#   更要命的是有人「顺手」把它换成"全集常量"，那一步就把顶元素语义换丢了。
#   一律走 intersect() / is_subset()。
# ⚠️【不】把 ALL 与空集写反：ALL ∩ X = X，CONFINED ∩ X = CONFINED。
#
# ▍修改铁律
# 1. 【不】在本模块以外再写一份判定或读者集换算。历史上本文件抬头就写着「全库唯一实现」，
#    结果 min_visibility 另立了一套「最严字串」口径，两套口径并存（M03 的病根）。
# 2. 【不】让「查不到来源」回落成 shared。查不到 = 围栏（CONFINED），见 §9.3「空/缺失来源保守处理」。
# 3. 【不】放宽 fail-closed：空 viewer / 未知 visibility / 空 namespace 一律 deny。
# 4. min_visibility() 是**历史接口**，只保字串语义供旧调用方与旧守卫使用；
#    新代码一律用 reader_set_of + intersect + best_visibility_for（见 source_resolver.py）。
#
# ▍更新记录
# *更新：2026-10-14 · M03 第二轮返修（小D，独立验收 F2）：新增「权限隔离态」的
#   唯一识别实现（QUARANTINE_MARK/is_quarantined/entry_visibility_ok）。
#   起因：隔离标记此前只在**写侧**打（state='archived'）、读侧各自只看 visibility，
#   于是 ID 回查/trace/投影/生成片段/多级传播/新派生六条读入口全部继续放行。
#   判定与 min_visibility 的**行为未改**（逐字节兼容既有守卫）*
# *更新：2026-10-13 · M03（小D）：新增读者集代数（reader_set_of/intersect/is_subset/
#   best_visibility_for/readers_of_rows）与 min_visibility 的跨域缺陷说明；
#   判定与 min_visibility 的**行为未改**（逐字节兼容既有守卫）*
# *历史：R1.3(2026-08-31) 三态统一；BF-3(2026-09-01) 查不到→private 兜底；
#   P1-E/R-09(2026-09-03) fail-closed 收紧；P6.1/R8(2026-09-04) 多源 shared-with 名单交集*
# ================================================================
"""XTMEM 记忆可见性三态判定 + 读者集代数——全库唯一实现。

R1.3（2026-08-31）：CLI / provider / curator 一律调本函数，禁止各写一遍。
P1-E / R-09（2026-09-03）：fail-closed 语义收紧——
- shared        → 全可见（含空 viewer，公共内容不拒）
- private       → 仅 chunk_namespace == viewer；空 viewer 一律 deny
- shared-with:a,b → 名单内或 chunk_namespace == viewer；空 viewer deny
- 未知 visibility → deny；显式上帝视角传 viewer='__admin__'

M03（2026-10-13）：新增 reader_set_of / intersect / is_subset / best_visibility_for——
把「谁能读」从"字串最严"升级为"读者集合交集"。判定函数语义未改。
"""
from typing import Any, FrozenSet, Iterable, Optional, Union

__all__ = [
    "normalize_visibility", "visibility_ok", "min_visibility",
    "ALL_READERS", "CONFINED", "ReaderSet",
    "reader_set_of", "readers_from_visibility", "intersect", "is_subset",
    "best_visibility_for", "readers_of_rows", "reader_set_matches_visibility_ok",
    "QUARANTINE_MARK", "QUARANTINE_OWNER", "is_quarantined", "entry_visibility_ok",
]


# ================================================================
# 读者集合代数 —— 顶/底元素
# ================================================================

class _Top:
    """顶元素：所有身份都能读（对应 visibility='shared'）。

    为什么是单例类而不是常量 True/None：
      ① 它要能参与 `in` 判断（`audience in readers`）与 len()，行为像集合才好用；
      ② 它**不是** frozenset——用真集合表示需要枚举宇宙，根本枚举不完；
      ③ 单例保证 `a is ALL_READERS` 这个身份判断永远可靠（intersect 靠它分流）。
    ⚠️ 别给它加 __and__/__or__：那会让 `ALL & x` 看起来能用，从而掩盖
       "有人把它当普通集合"的错误用法。要用就走 intersect()。
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ALL_READERS"

    def __contains__(self, item) -> bool:
        return True

    def __len__(self) -> int:
        # ⚠️ 只是"非空"的表示，不是真实势。别拿它做计数判断。
        return 1


ALL_READERS = _Top()
CONFINED: FrozenSet[str] = frozenset()   # 底元素：除 __admin__ 外无人可读

ReaderSet = Union[_Top, FrozenSet[str]]


def reader_set_of(visibility: str, namespace: str) -> ReaderSet:
    """(visibility, namespace) → 实际读者集合。全库唯一换算实现。

    reader_set_of()：契约
    入参：visibility（shared/private/shared-with:a,b，含历史空值与脏值）
          namespace（该行所属的域；空串表示"没有域"）
    返回：ALL_READERS（顶）| frozenset（具体身份集合）| CONFINED（空集）
    副作用：无（纯函数）
    谁调用：source_resolver（求交集/投影）、readers_of_rows、守卫对拍
    改动影响：这是**权限上界**的换算——改宽 = 越权，改窄 = 误伤正常读取。
             改前跑 tests/test_m03_derived_permissions.py 全量。

    ⚠️ 必须与 visibility_ok 逐一情形等价（同一语义的两面）：
       visibility_ok 判"某个 viewer 能不能读"，本函数算"所有能读的 viewer 是谁"。
       test_m03 有一条**穷举对拍**（所有 vis×ns×viewer 组合两边同判），不一致就红。
    ⚠️ ns 为空时一律 CONFINED/仅名单：旧代码用 `chunk_namespace or DEFAULT_NAMESPACE`
       把空域名回落成默认域 ⇒ 一条"无处归属"的 private 变成"默认域的人能读"。
       本函数【不】做这种回落（铁律 3）。
    """
    vis = str(visibility or "")
    ns = str(namespace or "")
    if vis == "shared":
        return ALL_READERS
    if vis == "private":
        # ⚠️ 空 ns ⇒ 无域名可归属 ⇒ 围栏（【不】回落 DEFAULT_NAMESPACE，铁律 3）
        return frozenset([ns]) if ns else CONFINED
    if vis.startswith("shared-with:"):
        allowed = {x.strip() for x in vis[len("shared-with:"):].split(",") if x.strip()}
        # owner（=该行的域）恒在读者集内，与 visibility_ok 的
        # `chunk_namespace == viewer or viewer in allowed` 严格对齐（Spec §9.3「包含 owner」）
        base = {ns} if ns else set()
        return frozenset(base | allowed)
    # 未知/空 visibility → fail-closed（与 visibility_ok 末尾 return False 同款）
    return CONFINED


def readers_from_visibility(visibility: str, namespace: str) -> ReaderSet:
    """readers_from_visibility()：reader_set_of 的别名。

    为什么留两个名字：新调用方（CST01/UX02）看 "readers_from_visibility" 更容易猜对方向，
    老调用方习惯 reader_set_of。两个名字指向同一实现，**不是**第二份语义。
    """
    return reader_set_of(visibility, namespace)


def intersect(a: ReaderSet, b: ReaderSet) -> ReaderSet:
    """读者集合求交（带顶/底元素语义）。多源派生读者 = 全部来源读者集的交集。

    ⚠️【不】用 `a & b`：见模块头「读者集代数」。ALL 是哨兵不是集合。
    """
    if a is ALL_READERS:
        return b
    if b is ALL_READERS:
        return a
    return frozenset(a) & frozenset(b)


def is_subset(x: ReaderSet, y: ReaderSet) -> bool:
    """x ⊆ y（读者集合版）？权限校验的唯一判据：派生读者集必须 ⊆ 来源交集。

    典型用法：
      收紧传播时校验"这次变更没有放宽"；T17 复验时校验"来源读者没扩大"。
    """
    if y is ALL_READERS:
        return True
    if x is ALL_READERS:
        return False
    return frozenset(x) <= frozenset(y)


def best_visibility_for(readers: ReaderSet, namespace: str) -> Optional[str]:
    """把读者集合投影回**能落库的** visibility 字串；编码不出来返回 None。

    best_visibility_for()：契约
    入参：readers（目标读者集合）/ namespace（拟落的域）
    返回：'shared' | 'private' | 'shared-with:a,b' | None（= 无法表达，调用方须拒绝生成）
    副作用：无
    谁调用：source_resolver.derived_visibility / _tightest_of
    改动影响：返回 None 是 fail-closed 的**载体**，【不】许把它改成回落 'private'。

    为什么可能 None：schema 的三种字串都要求「域 ∈ 读者集」（reader_set_of 的逆映射），
    所以  ① readers 为空（CONFINED），或
          ② readers 不含拟落的 namespace
    这两种情形都编码不出来。
    ⚠️【不】在 None 分支回落 'private'：那会把"编不出来"变成"给拟落域的人读"——
       正是 I09 的复发形态（源读者里根本没有这个域，却把这条送给了它）。
       Spec §9.3 对这两种情形的处置是「拒绝生成或保留受限归属」，由调用方决定。
    """
    if readers is ALL_READERS:
        return "shared"
    names = frozenset(readers)
    if not namespace:
        return None
    if namespace not in names:
        return None
    rest = names - {namespace}
    if not rest:
        return "private"
    return "shared-with:" + ",".join(sorted(rest))


def readers_of_rows(rows: Iterable, *, entry_of=None) -> ReaderSet:
    """一组记忆行的**公共读者集合**（交集）。U08 分享回忆册的投影判据。

    入参：rows（sqlite3.Row / dict / 带属性的对象）/ entry_of(row)->(vis, ns) 可选
    返回：交集读者集（空序列 → CONFINED，即可分享给"没有人"）
    ⚠️ 空序列返回 CONFINED 而不是 ALL：**没有内容**不等于"谁都能看"。
       调用方若拿它去投影会把所有内容滤掉——这是对的（没东西可分享）。
    """
    out: ReaderSet = ALL_READERS
    seen = False
    for row in rows:
        seen = True
        vis, ns = entry_of(row) if entry_of else _auto_entry(row)
        out = intersect(out, reader_set_of(vis, ns))
    return out if seen else CONFINED


def reader_set_matches_visibility_ok(samples_ns, samples_vis, samples_viewer) -> bool:
    """两套口径的**穷举对拍**（守卫用）：reader_set_of 与 visibility_ok 必须逐点一致。

    判据：对每个 (ns, vis, viewer) 三元组，
          `viewer in reader_set_of(vis, ns) or viewer == '__admin__'`
          必须等于 `visibility_ok(vis, ns, viewer)`。
    为什么要这条：判定与换算一旦漂移，就会出现"算出来的读者集"与"实际放行的人"不一致——
    而权限系统最怕的就是两套口径并存（M03 病根正是这么来的）。
    """
    for ns in samples_ns:
        for vis in samples_vis:
            rs = reader_set_of(vis, ns)
            for viewer in samples_viewer:
                expected = visibility_ok(vis, ns, viewer)
                got = True if viewer == "__admin__" else (viewer in rs)
                if bool(expected) != bool(got):
                    return False
    return True


def _auto_entry(row):
    """从 sqlite3.Row / dict / 普通对象里取 (visibility, namespace)。"""
    if isinstance(row, dict):
        return str(row.get("visibility") or ""), str(row.get("namespace") or "")
    try:
        return str(row["visibility"] or ""), str(row["namespace"] or "")
    except Exception:
        return (str(getattr(row, "visibility", "") or ""),
                str(getattr(row, "namespace", "") or ""))


# ================================================================
# 判定（R1.3 起的三态语义，M03 未改）
# ================================================================

def normalize_visibility(v):
    """校验并归一可见性（全库唯一枚举函数，P1-E/R-09）。

    返回 'shared'/'private'/'shared-with:...' 之一，非法值返回 None（落库前即拒绝）。
    """
    if v in ("shared", "private"):
        return v
    if isinstance(v, str) and v.startswith("shared-with:"):
        return v
    return None


def visibility_ok(visibility: str, chunk_namespace: str, viewer: str) -> bool:
    """三态判定，全库唯一实现。CLI / provider / curator 一律调它，禁止各写一遍。

    ⚠️ 空 viewer 是 fail-closed（只放行 shared），**不是**"上帝视角"。
       上帝视角必须显式传 '__admin__'（见 CLI _resolve_admin 的凭证闸）。
    ⚠️ 本函数与 reader_set_of 是一体两面，改一个必须同步另一个（有 reader_set_matches_visibility_ok 对拍）。
    """
    if viewer == "__admin__":
        return True
    vis = visibility or ""
    if vis == "shared":
        return True
    if not viewer:
        # 空 viewer：只放行公共 shared；private/shared-with/未知一律 deny（P1-E fail-closed）
        return False
    if vis == "private":
        return chunk_namespace == viewer
    if vis.startswith("shared-with:"):
        allowed = [x.strip() for x in vis[len("shared-with:"):].split(",") if x.strip()]
        return chunk_namespace == viewer or viewer in allowed
    return False


# ================================================================
# 权限隔离态（M03 第二轮返修 · 独立验收 F2 的唯一公共地基）
#
# ▍为什么要有这一段
# M03 把「来源读者交集在拟落域下编码不出来」的派生行，用 state='archived' +
# retire_reason='m03-acl-unexpressible:...' 表达「权限隔离」（见 source_resolver._quarantine）。
# 但**判定散落**：写侧打标记，读侧六个入口各自只看 visibility（有的连 state 都不看），
# 于是隔离行照样被 ID 回查 / trace / 受众投影 / 生成片段 / 多级传播 / 新派生放行。
# 独立验收实测六条全红（F2）。这与 M03 要根治的病**同型**：
# 同一个语义有两套口径。⇒ 识别逻辑收成**本模块唯一实现**，所有读入口一律调它。
#
# ▍隔离态 ≠ 普通知识退役（⚠️ 别把两者混为一谈）
#   · 知识退役（state='archived'，retire_reason 无本前缀）：语义是"不再参与召回"，
#     但它的**权限**没有被否定——历史读取照旧按 visibility 判定是合理的。
#   · 权限隔离（retire_reason 带本前缀）：语义是"**该给谁读算不出来**"⇒ 除管理员外
#     **一律不可读**，且**不得**作为来源参与再派生（否则隔离正文经新派生重获读取资格）。
#   ⇒ 所以识别依据是**退役原因前缀**，不是 state 本身。
#     ⚠️【不】把"state=='archived' 就不可读"当成规则：那会把全部正常知识退役
#       也打成越权，属过度收紧（会误伤 trace 的历史可读性）。
#
# ▍【不】新增第四种 visibility 字面量
#   chunks.visibility 上有 trg_chunks_visibility_ins/upd 触发器，只认
#   shared/private/shared-with:*；塞第四种值会被 ABORT 或被当脏值清掉。
#   故隔离必须走既有列的组合表达——这也是独立验收裁定的口径（不扩 schema）。
# ================================================================

# ⚠️ 前缀即契约：source_resolver._quarantine 与四组读入口共用这一个常量。
#    改它 = 让所有历史隔离行"解除隔离"（静默放行整个 F2 面）。
QUARANTINE_MARK = "m03-acl-unexpressible:"

# 写明隔离动作的执行方（便于审计：谁把这条隔离的）
QUARANTINE_OWNER = "M03-SourceResolver"


def is_quarantined(entry: Any) -> bool:
    """该记忆行是否处于「权限隔离」态（= 来源读者算不出来，除管理员外一律不可读）。

    is_quarantined()：契约
    入参：entry（sqlite3.Row / dict / 带属性的对象；【不】要求一定有 retire_reason 列）
    返回：True=权限隔离 / False=不是
    副作用：无（纯函数）
    谁调用：source_resolver（ID/投影/片段/多源解析）、hybrid_retriever、trace、xtmem CLI
    改动影响：返回 True 的行会被**所有**普通读入口拦下，且不得作为来源再派生。
              放宽本判定 = 直接复现 F2 六条；收紧（比如按 state 判）会误伤正常知识退役。

    ⚠️ 判据只有「退役原因前缀」一条：
       用 state=='archived' 判会把正常知识退役也拦掉（那不是权限语义）；
       用 visibility 判根本判不出来（隔离行的 visibility 仍是 shared/private 原值——
       独立验收实测隔离行是 zcode/shared/archived，光看 visibility 完全放行）。
    """
    try:
        if isinstance(entry, dict):
            why = entry.get("retire_reason")
        else:
            try:
                why = entry["retire_reason"]
            except Exception:
                why = getattr(entry, "retire_reason", None)
    except Exception:
        return False
    return str(why or "").startswith(QUARANTINE_MARK)


def entry_visibility_ok(entry: Any, viewer: str) -> bool:
    """**读路径统一闸**：一行记忆对某 viewer 是否可读（含隔离态判定）。

    entry_visibility_ok()：契约
    入参：entry（Row/dict/对象，需含 visibility、namespace；retire_reason 缺失=未隔离）
          viewer（查看身份；'' = 匿名，fail-closed）
    返回：True=可读 / False=不可读（隔离行**除 __admin__ 外一律 False**）
    副作用：无
    谁调用：所有普通读入口（ID 回查 / trace / 投影 / 片段 / 检索 / CLI）
    改动影响：这是 F2 的统一落点。改宽 = 隔离失效；改窄（如顺手加 state 判定）= 误伤历史退役。

    ★为什么必须有"统一"这一层（独立验收 F2 的六条全是同一个病）：
      分散判定时，每个出口都要各写一次"检查 visibility + 检查是否隔离"，
      漏一处就漏一条读路径——实测六个出口漏了六个。收成一处后，
      "隔离行不可读"成为**结构性**事实，新增读出口调本函数即自动继承。
    ⚠️ 管理员（viewer='__admin__'）不受限：那是**显式的审计授权**，
    不能由普通历史读取推导出来（独立验收 §F2「管理员的明确授权审计可单列」）。
    """
    vis = _entry_field(entry, "visibility")
    ns = _entry_field(entry, "namespace")
    if str(viewer or "") != "__admin__" and is_quarantined(entry):
        return False
    return bool(visibility_ok(str(vis or ""), str(ns or ""), viewer or ""))


def _entry_field(entry: Any, name: str):
    """从 Row/dict/对象取一列（取不到返回 None，绝不抛）。"""
    if isinstance(entry, dict):
        return entry.get(name)
    try:
        return entry[name]
    except Exception:
        return getattr(entry, name, None)


def min_visibility(source_ids, lookup=None):
    """取所有来源的最严可见性（历史接口）：private > shared-with > shared。

    ⚠️★M03（2026-10-13）重要更正：本函数**不是** Spec §9.3 说的「读者集合交集」。
      它比的是"可见性字串"，而 private / shared-with 都是 **namespace 相对** 语义，
      离开域名就没有意义。跨 owner 派生时两者会给出不同答案，且本函数的答案
      **让实际读者集合扩大**（实机复现：源 (ns=dsh,'private') → 派生落 assistant，
      读者从 {dsh} 变成 {assistant}，见 m03/evidence/ 探针输出）。

      本函数保留只为**兼容既有调用方与既有守卫**（它们锁定的是字串口径）。
      **新代码一律改用**：source_resolver.SourceResolver.derived_visibility(refs, namespace)。

    P0.2（2026-08-31）：蒸馏/摘要/预判等衍生记忆的可见性 = 全部来源的最严者。
    - lookup(chunk_id) -> visibility（None 视为 unknown）
    - 来源 id 查不到 / unknown -> 按最严 private 兜底（fail-closed，禁回落 shared）
    - 空 source_ids -> private（无血缘的衍生物一律最保守）

    P6.1/R8（2026-09-04，整改方案 v1.3.1 §三 R8）：多源 shared-with 由 first-wins
    改为**名单交集**——过度暴露不再依赖来源遍历顺序；无交集降 private；
    规范化序列化 ",".join(sorted(...))（乱序同值）。
    """
    if not source_ids:
        return "private"
    best_sw = None
    for sid in source_ids:
        # BF-3（2026-09-01）：查不到/unknown → 按最严 private 兜底（fail-closed，禁回落 shared）。
        # 上一版 `or "shared"` 与自身 docstring 矛盾，AF-1 解锁血缘后蒸馏/画像传已删源 id 会静默降级 shared。
        v = (lookup(sid) if lookup else None) or "private"
        if v == "private":
            return "private"
        if v.startswith("shared-with:"):
            names = {x.strip() for x in v[len("shared-with:"):].split(",") if x.strip()}
            # R8 交集：首个 shared-with 建立候选集，后续逐个取交集；来源顺序不再影响结果
            best_sw = names if best_sw is None else (best_sw & names)
    if not best_sw:
        # 有 shared-with 源但交集为空，或全部来源只有 shared →
        return "shared" if best_sw is None else "private"
    return "shared-with:" + ",".join(sorted(best_sw))
