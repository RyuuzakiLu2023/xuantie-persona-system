# -*- coding: utf-8 -*-
"""xtmem_sources.py — 时间线/画像取材的共用来源挑选器。

【这是什么】
时间骨架（xtmem_timeline.py）和画像（xtmem_profile.py）都要回答同一个问题——
"这条 chunk 能不能当取材原料"。本模块给出唯一答案：本生成器旧视图、兄弟生成器页、
已知聚合页、页头特征一律排除，并沿 source_ids 做有限深度血缘回溯。

【为什么单独一个文件】
历史故障恰恰是两边各写一套 WHERE、各漏一处：
  - timeline 少排派生页 ⇒ 生成器读回自己上一版，正文里 `# 时间骨架` 出现多次；
  - profile 只排 track ⇒ 派生原料冒充第一手证据。
收成一份实现后，下次补一类只改这里。

【权限与写操作边界】
本模块【不】做任何写操作、不碰连接生命周期、不改可见性体系。
取材只读；派生落库的可见性由引擎按"实际传入的 source_ids"继承。
所以本模块越界改 source_ids 等于改权限，必须由调用方把真正采用过的 id 传进来。

【三类时间（契约）】
  - 发生时间 occurrence：chunk.content_time（从正文抽出的时间基准）
  - 入库时间 ingested：chunk.timestamp（落库 epoch）
  - 生成时间 generated：由生成器本次运行时决定，不从这里取
缺 content_time 的存量行 → occurrence 记 None（未知就是未知，【不】拿 timestamp 顶替）。

【修改铁律】
1. 【不】在 exclusion_reason 里做 visibility 判断——统一读闸是 quarantine_ok。
2. 【不】按派生前缀一刀切：distill 类要点在画像里是可以用的历史材料。
3. 【不】把"深度用完"当成"干净"：unfinished 必须判不采用。

【更新记录】
*创建：2026-10-22 · 从运行正本公开化：去掉私有域/私有路径依赖，口径不变*
"""
from __future__ import annotations

import json

try:  # 包内与同目录直跑两态
    from . import xtmem_common as _common
except ImportError:  # pragma: no cover
    import xtmem_common as _common

# ───────────────────────── 生成器身份（排除"自己"） ─────────────────────────
SELF_DERIVED = {
    "timeline": "timeline",                    # 时间骨架自己
    "profile": "profile-aggregator",           # 画像自己
}

# 血缘回溯深度上限。写死不用变量传参，是为了让"能拖进来多远"这件事可审计。
LINEAGE_MAX_DEPTH = 4

# 无明确用途聚合页 / 其他生成器的整页快照：读到只会把别人的总结再加工一遍。
AGGREGATE_DERIVED = {
    "wiki-entity": "实体页是 wiki 聚合产物，正文常整段复述来源",
    "capability-ledger": "能力总账是人工维护的清单，不是对话证据",
    "diary-distill": "日记蒸馏是同一批原文的二手摘要",
}

# 正文页头兜底：derived_from 可能为空（老数据/手写页），按首行特征再排一次。
PAGE_HEADER_PREFIXES = ("# 时间骨架", "# 画像 ·", "# 实体 ·", "# 蒸馏", "## 蒸馏结果", "【压缩存档")


def _as_list(raw):
    """source_ids 列可能是 JSON 数组、逗号串、NULL——统一成 list[str]，坏值当空。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x)]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x)]
    except (TypeError, ValueError):
        pass
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def is_page_header(content):
    """正文首行是不是"某个生成器的整页产物"。只看前 200 字符，避免大正文全表扫。"""
    head = str(content or "").lstrip()[:200]
    return head.startswith(PAGE_HEADER_PREFIXES)


def lineage_ancestors(conn, source_ids, owner, max_depth=LINEAGE_MAX_DEPTH):
    """沿 source_ids 解析有限深度的真实血缘，返回 (tainted, unknown, cycle, unfinished)。

    四种返回位各自可解释：
      tainted    = True  → 血缘里出现本生成器/同级生成器页面（必须排除）
      unknown    = True  → 有查不到的 id（悬空来源），不编 ID、不装已核
      cycle      = True  → 回溯中发现环（节点重新指回自己/已走过的栈上节点）
      unfinished = True  → 深度用完但 frontier 还有待解析节点（=没看完）

    ★环怎么判（与"合法菱形共祖"的分界）：
      用**当前回溯路径栈**（path）判断，而不是全局 seen。
      - 菱形共祖：两条支路指向同一个叶子 → 该叶子第二次被到达时不在当前路径栈上，属正常合并；
      - 真环：节点重新指回自己或在当前路径栈上的祖先 → 判环。

    ★为什么 unfinished 要判不干净：
      "查了四层没看见"不等于"祖上没有"。深度上限是资源边界，不是盖章依据。
    """
    if not source_ids:
        return False, False, False, False

    roots = [str(x) for x in source_ids if str(x)]
    tainted = unknown = cycle = unfinished = False
    # 深度随栈元素走（d=从根到本节点已走的步数），不存全局 depth_of：
    # 全局表会让"短路的低深度"给"长路的高深度"背书。
    stack = [(rid, 0, frozenset([rid])) for rid in roots]
    # ⚠️ 缓存键必须是 (节点, 剩余预算) 二元组，【不】能只存节点：
    #   只存节点就等于允许短路替长路背书——同一张图换个 source_ids 顺序结论就翻。
    expanded = {}
    while stack:
        sid, d, path = stack.pop()
        row = _fetch_one(conn, sid)
        if row is None:
            unknown = True
            continue
        if _is_generator_page(row, owner):
            tainted = True
            continue
        remaining = max_depth - d
        if remaining <= 0:
            ups = [u for u in _as_list(_col(row, "source_ids")) if u]
            if ups:
                unfinished = True
            continue
        if (sid, remaining) in expanded:
            continue
        expanded[(sid, remaining)] = True
        for up in _as_list(_col(row, "source_ids")):
            if not up:
                continue
            if up in path:
                cycle = True
                continue
            stack.append((up, d + 1, path | {up}))
    return tainted, unknown, cycle, unfinished


def _fetch_one(conn, cid):
    """单条取行（缺行返回 None，不抛）。"""
    try:
        return conn.execute(
            "SELECT id, derived_from, track, content, source_ids FROM chunks WHERE id=?",
            (cid,)).fetchone()
    except Exception:
        return None


def _is_generator_page(row, owner):
    """本生成器/同级生成器的页面判定（血缘里遇到即判祖先）。"""
    df = str(_col(row, "derived_from") or "")
    track = str(_col(row, "track") or "")
    if df and df in SELF_DERIVED.values():
        return True
    if track in ("timeline", "profile"):
        return True
    return is_page_header(_col(row, "content"))


def exclusion_reason(row, owner):
    """判定一条候选为什么不能当原料；能当原料返回 None。

    owner: 'timeline' 或 'profile'——表示"谁在取材"。本生成器自己的派生物一律排除，
    另一个生成器的产物按 AGGREGATE_DERIVED 规则排除（两者都不该被反复加工）。

    ⚠️ 本函数只看**当前行自身**标记。间接回流（两跳）由 lineage_ancestors() 顺
      source_ids 往上查，别指望这里能挡。
    """
    if row is None:
        return "missing-row"
    df = str(_col(row, "derived_from") or "")
    track = str(_col(row, "track") or "")
    if df and df == SELF_DERIVED.get(owner):
        return "self-derived"
    if df and df in AGGREGATE_DERIVED:
        return "aggregate-page:" + df
    for other, marker in SELF_DERIVED.items():
        if other != owner and df and df == marker:
            return "sibling-generated:" + other
    if track in ("timeline", "profile"):
        return "page-track:" + track
    if is_page_header(_col(row, "content")):
        return "page-header"
    return None


def quarantine_ok(row, viewer):
    """统一行级读闸——委派 memory.visibility.entry_visibility_ok，不另造一套。

    ★为什么不能只手调 visibility_ok：它只看 visibility/namespace 三态，**不看隔离标记**。
      已被隔离的行 visibility 仍是原值，单用 visibility_ok 会放行，隔离材料就这样进了公开摘要。

    ⚠️【不】在这里自己写隔离前缀判定：那是第三套权限实现。
      也别顺手加 state=='archived' 判定——那是"正常退役"不是权限语义，会误杀历史知识。
    """
    _common.ensure_engine()
    from memory.visibility import entry_visibility_ok
    return entry_visibility_ok(row, viewer)


def _col(row, name):
    """sqlite3.Row 没有 .get()；dict 有。两种都要能用（测试里用 dict）。"""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def occurrence_time(row):
    """发生时间：content_time。缺就是 None——【不】回落到 timestamp 冒充发生时间。"""
    val = _col(row, "content_time")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def ingested_time(row):
    """入库时间：timestamp（epoch）。缺失同样是 None，不补 now。"""
    val = _col(row, "timestamp")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def kind_of(row):
    """事实类型标签：原话 / 事实 / 推断 / 未标注。

    优先级（严格自上而下，命中即返回）：
      1. derived_from 非空                        → 推断（派生物不是第一手证据）
      2. confirmation_type == 'inferred'          → 推断
      3. provenance_status == 'legacy_unverified' → 未标注（老数据来源没核验过）
      4. track == 'prediction'                    → 推断
      5. confirmation_type == 'explicit' 且
         provenance_status == 'verified'          → 原话
      6. provenance_status == 'verified'          → 事实
      7. 其余（含确认/来源字段缺值）              → 未标注

    ⚠️【不】用 track（identity/preference/...）冒充"已核验"：track 只说明这条归哪类。
      缺确认就是未知，宁少不错。
    ⚠️【不】把 kind 当成"事件是否真实发生"的判定：它说的是**证据等级**，不是真值。
    """
    df = str(_col(row, "derived_from") or "")
    track = str(_col(row, "track") or "")
    status = str(_col(row, "provenance_status") or "")
    conf = str(_col(row, "confirmation_type") or "").strip().lower()

    if df:
        return "derived"
    if conf == "inferred":
        return "derived"
    if status == "legacy_unverified":
        return "unknown"
    if track == "prediction":
        return "derived"
    if conf == "explicit" and status == "verified":
        return "verbatim"
    if status == "verified":
        return "fact"
    return "unknown"


KIND_LABEL = {
    "verbatim": "原话",
    "fact": "事实",
    "derived": "推断",
    "unknown": "未标注",
}


def select_materials(rows, owner, allowed_source_ids=None, conn=None):
    """从候选行里挑出可当原料的行，并回传每条的排除原因。

    返回 (kept, rejected)：
      kept     = [(row, kind_label), ...]  —— 顺序保持调用方给的顺序
      rejected = [(row_id, reason), ...]   —— 供诊断/证据用，**不进正文**

    allowed_source_ids 不为 None 时，只保留 id 在其中集合的行。
    【不】在本函数里做 visibility 判断：统一闸在 quarantine_ok。

    conn 不为 None 时**顺带做血缘深度检查**：候选行可能是干净要点，
    但它两跳外挂着的正是本生成器旧页。
    """
    allowed = set(allowed_source_ids) if allowed_source_ids is not None else None
    kept, rejected = [], []
    for row in rows:
        rid = str(_col(row, "id") or "")
        if allowed is not None and rid not in allowed:
            rejected.append((rid, "not-authorized"))
            continue
        reason = exclusion_reason(row, owner)
        if reason:
            rejected.append((rid, reason))
            continue
        if conn is not None:
            tainted, unknown, cycle, unfinished = lineage_ancestors(
                conn, _as_list(_col(row, "source_ids")), owner)
            if tainted:
                rejected.append((rid, "lineage-self-ancestor"))
                continue
            if cycle:
                rejected.append((rid, "lineage-cycle"))
                continue
            if unfinished:
                rejected.append((rid, "lineage-unresolved-depth"))
                continue
            if unknown:
                rejected.append((rid, "lineage-unknown-source"))
                continue
        kept.append((row, KIND_LABEL.get(kind_of(row), "未标注")))
    return kept, rejected


def actual_source_ids(kept, cap=800):
    """正文实际采用的来源 id（去重保序）。

    【不】再把整个候选池的 id 一股脑塞进 source_ids 冒充覆盖：
    派生页的 visibility 由这批 id 的最严者继承，塞多了等于顺手放宽权限；
    而且"登记了却没采用"会让血缘查询把没进正文的条目算成依据。
    """
    out, seen = [], set()
    for row, _kind in kept:
        rid = str(_col(row, "id") or "")
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
        if len(out) >= cap:
            break
    return out


def skip_domains():
    """$XTMEM_LINEAGE_SKIP_DOMAINS：只留标、不代清理血缘的域。

    为什么需要：某些域有独立的人工审档流程，自动剔源会绕过审档。默认空=全部处理。
    """
    return set(_common.env_list("XTMEM_LINEAGE_SKIP_DOMAINS"))
