# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · memory/trace.py · D3-4 因果链查询器（只读）
#
#   ▍这是什么
#   给一条记忆查"家谱"：把历代版本串成时间线叙事，并溯源蒸馏原料。
#   供 CLI `xtmem trace <mem-id> [--depth N] [--json]` 复用（CLI 只做参数与打印，
#   判据与全部 DB 查询都收在本模块——CLI 里不写第二条血缘 SQL，防口径漂移）。
#
#   ▍本模块铁律
#   1. 只读：本模块【零写】。connect 一律 open_db(ro=True)，读库用 SELECT。
#      B-0 漏记补写是独立小修（见 xtmem.py cmd_trace 的 --fix-source-ids），不走本模块。
#   2. 数据来源优先级（2026-09-14 改道，替代原 Spec D3-4 顺序）：
#      🔴【chunks 层指针优先】chunks.superseded_by（谁接替我）+ retire_reason（为啥退）→
#        source_ids/derived_from（血缘）→ distill_units（蒸馏原料）。
#      ⚠️ 改道原因（2026-09-14 生产库体检 + 顶模裁定二）：memory_evolution 边表 689 条
#        supersedes(shadow=0) 中 680 条（98.7%）无正文依据，是平行旧账本；而 chunks 层指针
#        实测 100% 闭环（timeline 57/57、distill-retire 342/342、reaggregate 全带指针、
#        悬空 0、断链 0）。血缘查询改走指针 = 只读"确定发生过的替代"，不再展示含水量 98.7%
#        的边表关系。
#      【行为变化】改道后 relation 恒为 supersedes（指针无语义细分）、edge_state 恒为
#        confirmed（指针是退役动作留下的既成事实，非候选）。边表的 enriches（全 shadow=1
#        候选）与 replaces（72 条 0 依据）不再进家谱——这是有意的取舍：宁可少一行，
#        不可多一行假的（与原【宁少不串】取舍同源）。
#   3. 无血缘的条目：如实输出「本条为原始记录，无因果链」——【不要编】。
#   4. 环检测 + 深度上限（防 A→B→A 无限递归）。
#   5. 本模块【不】import 整个 memory 包——只 import open_db，规避循环依赖风险。
#   6. 🔴【可见性闸 · 2026-09-11 P7.0 修复】所有取 chunk 的路径必须过
#      memory.visibility.visibility_ok（全库唯一实现，禁本模块自写第二份判定）。
#      viewer 由调用方传入（CLI 走 --as / XTMEM_ACTOR）；viewer 为空 = fail-closed，
#      只放行 shared，【绝不】回落"全可见"。越权条一律不给正文。
#      ⚠️ 修前病灶：本模块零可见性过滤 + trace() 无 viewer 形参 → dsh 身份可完整读出
#      assistant 的 private 正文（顶模审计实测坐实，见 P7-A 工单 §一）。
#
#   ▍血缘口径与派生
#   - 🔴【指针方向 · 2026-09-14 改道后的唯一方向源，勿凭旧边表直觉改】
#     chunks.superseded_by：A.superseded_by = B ⟺ 【A 被 B 取代】（A 旧 / B 新）。
#     所以查某条的【前任】= 反查 `chunks WHERE superseded_by = 本条` 命中的那条；
#     查某条的【后辈】= 直接读本条自己的 superseded_by。
#     ⚠️ 别再用边表的"source=新/target=旧"直觉想这个列——两个账本方向表达不同。
#   - 【已退役】memory_evolution 边表：本模块不再读取（保留于库，供驾驶舱/审计，不再进家谱）。
#     历史方向铁律（source_id=新 / target_id=旧，三重铁证定案）存档于更新记录，勿删注释。
#   - chunks.source_ids：JSON 数组字符串（如 '["mem-aaa","mem-bbb"]'），refined 条目
#     记录它"吸收"了哪些来源——distill 蒸馏产物、merge 合并产物都会填。
#   - chunks.derived_from：生成来源【生成器名】（不是批次号！生产库实测取值：
#     distill-judge 587 / wiki-entity 98 / timeline 57 / distill 29 / prediction-engine 24 …）。
#     ⚠️ 所以它【不能】当批次键用：按 derived_from 并集 = 把该生成器产出过的【全部】条目的
#     source_ids 搅在一起（家谱里混进无关来源）。详见 _lineage_sources 的批次约束注释。
#   - distill_units.refined_ids：该蒸馏单元蒸出来的 refined 条目（JSON 数组）；
#     raw_ids：蒸进去的原始条目。
#   ⚠️ 环：指针链理论上可成环（数据损坏时）——本模块仍保留 visited set 防死循环。
#
#   ▍更新记录
#   *更新：2026-09-14 · 【数据源改道】祖先链/后辈从 memory_evolution 边表切到 chunks 指针
#     （修复方向 v3.2 §不记错-3 + 顶模裁定二）：_evolution_chain / _successors 两处 SQL 重写；
#     边表 98.7% 无依据不再进家谱；relation/edge_state 由指针语义定为常量 supersedes/confirmed；
#     守卫测试同步重写（test_xtmem_p74_trace_shadow.py / xtmem_d3_4_trace_guard_test.py 造指针）。*
#   *更新：2026-09-13 · P7.4 影子边打标 + 血缘批次约束（Spec [S2] Task [T-P7.4]，Z指导派单单G）：
#     ① _evolution_chain / _successors 两处 SQL 补 shadow_state 列，边节点加 edge_state="confirmed"/"candidate"
#        字段（只打标不过滤——取舍逻辑不动；【不】叫 state 因为 nodes 里 state 已被 chunk 生命周期占用）；② _lineage_sources 删掉 derived_from 跨条目并集
#        （生产库无批次判别符，宁少不串，决策依据见该函数注释）。*
#   *更新：2026-09-11 · P7.0 可见性闸（小D，Z指导派单 P7-A 任务 A）：trace() 加 viewer 形参；
#     全模块取 chunk 的行过 visibility_ok；越权条只留 id/state 不给正文；无身份 fail-closed 只放行 shared。*
#   *更新：2026-09-10 · 【纠方向】evolution 边 source=新/target=旧（原实现反了 180°，守卫夹具同向
#     造错导致自洽假绿；用生产数据三重铁证定案后修正，小D）*
#   *创建：2026-09-10 · D3-4 因果链查询器（小D）*
# ================================================================

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

_MAX_DEPTH = 8  # 深度上限：家谱超过 8 代就截断，防极端链打爆时间线
_REL_LABEL = {"supersedes": "取代", "enriches": "增强", "replaces": "替换"}


def _ts_str(ts: Any, default: str = "") -> str:
    """时间戳 → 可读串（容忍 int/float/str）。"""
    if ts in (None, "", 0):
        return default
    try:
        f = float(ts)
        if f <= 0:
            return default
        return datetime.fromtimestamp(f).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts)


def _parse_ids(raw: Any) -> List[str]:
    """解析 source_ids/refined_ids/raw_ids（JSON 数组字符串 → id 列表）。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    s = str(raw).strip()
    if not s:
        return []
    try:
        arr = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return [str(x) for x in arr if x]


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------
# P7.0（2026-09-11）可见性闸：本模块所有取 chunk 的地方都从这三个小工具过。
# 判定本身【不】在这里实现——一律转调 memory.visibility.visibility_ok（全库唯一实现），
# 防"trace 自己写一份口径"导致两处判定漂移（这正是本次越权病灶的根）。
# 为什么用函数内 import：memory/__init__.py 会拉 vector_store/embedding 一整套重依赖，
# 模块级 import 会把 trace 从"纯只读小模块"变成"带动全包子系统"（见文件头铁律 5）。
# ----------------------------------------------------------------
def _vis_map(conn: sqlite3.Connection, ids: List[str]) -> Dict[str, tuple]:
    """批量取 id → (visibility, namespace, retire_reason)。查不到的 id 不进结果（调用方按 fail-closed 处理）。

    ★F2（2026-10-14 第二轮返修）：**必须带出 retire_reason**——它是"权限隔离态"的
    唯一识别依据（见 memory/visibility.is_quarantined）。不带它，隔离行在 trace 里
    就与普通行无法区分，于是正文照给（独立验收实测 trace visible=true 且带正文）。
    """
    out: Dict[str, tuple] = {}
    uniq = [x for x in dict.fromkeys(ids) if x]
    if not uniq:
        return out
    placeholders = ",".join("?" * len(uniq))
    try:
        rows = conn.execute(
            f"SELECT id, visibility, namespace, retire_reason FROM chunks WHERE id IN ({placeholders})",
            uniq,
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        out[r["id"]] = (str(r["visibility"] or ""), str(r["namespace"] or ""),
                        str(r["retire_reason"] or ""))
    return out


def _id_visible(conn: sqlite3.Connection, cid: str, viewer: str) -> bool:
    """单条可见性判定。查不到该 chunk → 不可见（fail-closed，绝不乐观放行）。

    ★F2：走 visibility.entry_visibility_ok 统一闸 → 自动消费权限隔离态。
    ⚠️【不】退回自写"查 visibility + 查 state"：那就是第二套口径，
      且 state 判据会误伤正常知识退役（见 visibility.is_quarantined 的说明）。
    """
    pair = _vis_map(conn, [cid]).get(cid)
    if not pair:
        return False
    from memory.visibility import entry_visibility_ok
    return bool(entry_visibility_ok(
        {"visibility": pair[0], "namespace": pair[1], "retire_reason": pair[2]}, viewer))


def _visible_ids(conn: sqlite3.Connection, ids: List[str], viewer: str) -> List[str]:
    """按 viewer 过滤 id 列表（保序去重，只留可见的）。

    血缘/蒸馏/后辈这类"id 列表"出口统一走这里：不可见的 id 直接【不出现】，
    连存在性都不给（比"保留 id 但隐正文"更严——列表层没有"有 N 条你看不到"的展示需求）。

    ★F2：隔离行同样不得出现在 id 列表里（它是"该给谁读算不出来"，
    连存在性都不该给非管理员）。
    """
    from memory.visibility import entry_visibility_ok
    m = _vis_map(conn, ids)
    out: List[str] = []
    for i in ids:
        if i in out:
            continue
        pair = m.get(i)
        if pair and entry_visibility_ok(
                {"visibility": pair[0], "namespace": pair[1], "retire_reason": pair[2]}, viewer):
            out.append(i)
    return out


def _load_chunks(conn: sqlite3.Connection, ids: List[str], viewer: str = "") -> Dict[str, Dict[str, Any]]:
    """按 id 批量取 chunks 摘要（id → {content 摘要, ts, tier, state, retired_*, visible}）。

    P7.0：带 viewer 做可见性判定。越权条【保留 id/state/ts 等结构信息】以示"此处有记录"，
    但 content 与 retire_reason 一律打码为空（正文是敏感载荷，退籍理由可能带正文片段）。

    ★F2-2（2026-10-14 第二轮返修）：**权限隔离行必须打码**。
    病灶（独立验收实测）：隔离行 state='archived'、visibility 仍是 shared ⇒
    光按 visibility 判 ⇒ visible=true 且正文摘要照给（实测 trace 里拿到完整正文）。
    隔离的语义是"该给谁读算不出来"，所以它不是"越权"而是"权限未定"——
    对普通 viewer 一律按不可读处理（就是本函数 ok=False 的形态：留 id、留结构、不给正文）。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not ids:
        return out
    from memory.visibility import entry_visibility_ok
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT id, substr(content,1,120) content, timestamp, tier, state, "
            f"retired_by, retired_at, retire_reason, superseded_by, visibility, namespace "
            f"FROM chunks WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        # ★F2-2：统一闸（含隔离判定）——隔离行落到 ok=False 分支：留结构、不给正文/理由
        ok = bool(entry_visibility_ok(
            {"visibility": str(r["visibility"] or ""), "namespace": str(r["namespace"] or ""),
             "retire_reason": r["retire_reason"]}, viewer))
        out[r["id"]] = {
            "id": r["id"],
            "content": (str(r["content"] or "")[:120]) if ok else "",
            "ts": r["timestamp"],
            "tier": r["tier"] or "raw",
            "state": r["state"] or "active",
            "retired_by": r["retired_by"] or "",
            "retired_at": r["retired_at"],
            "retire_reason": (r["retire_reason"] or "") if ok else "",
            "superseded_by": r["superseded_by"] or "",
            "visible": ok,
        }
    return out


def _evolution_chain(conn: sqlite3.Connection, mem_id: str, depth: int = 3,
                     viewer: str = "") -> List[Dict[str, Any]]:
    """沿 chunks 指针收【祖先链】：从 mem_id 逐代向前任回溯。

    🔴【2026-09-14 改道】数据源从 memory_evolution 边表切到 chunks.superseded_by。
    方向（本列语义，见文件头）：A.superseded_by = B ⟺ A 被 B 取代（A 旧 / B 新）。
    所以"谁是 mem_id 的前任" = 反查命中的 `chunks WHERE superseded_by = 当前 id` 的那条。
    多前任取 timestamp 最新的一条（同一条记忆被多版本取代时，最近的前任代表最强的直接关联）。
    返回从【最老 → 最新】(时间序正序) 的链，末位 = mem_id 本身。
    环检测：visited 记录已访问 id，重复即断（防数据损坏成环时死循环）；depth 另设硬上限。

    P7.0 可见性：前任不可见时【链到此为止】——该前任仍作为一条 visible=False 的节点收进来
    （以示"此处有记录但无权看"，正文已打码），但【不】再借它的指针继续向上回溯。
    为什么停：继续走就要读它的指针，等于把无权看的家谱结构也一并送出去。

    relation/edge_state 恒定：指针是退役动作留下的既成事实（非候选），故
    relation="supersedes"、edge_state="confirmed"——不再读边表的 shadow_state。
    ⚠️ 为什么不再读边表：689 条 supersedes(shadow=0) 中 680 条（98.7%）无正文依据，
    边表是平行旧账本；chunks 指针实测 100% 闭环。详见文件头铁律 2。
    """
    chain: List[Dict[str, Any]] = []
    cur = mem_id
    visited = {mem_id}
    ancestors: List[Dict[str, Any]] = []
    for _ in range(depth):
        try:
            # mem_id 的前任 = 那些指向"我"的条（它们的 superseded_by == 当前 id）
            rows = conn.execute(
                "SELECT id, timestamp FROM chunks WHERE superseded_by=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (cur,),
            ).fetchall()
        except Exception:
            rows = []
        if not rows:
            break
        edge = rows[0]
        prev = str(edge["id"])
        if prev in visited:
            break  # 环：不再向前
        visited.add(prev)
        _vis = _id_visible(conn, prev, viewer)
        # 前任排在更前（插到头部）——ancestors 最终是"最老在前"
        ancestors.insert(0, {
            "id": prev, "relation": "supersedes",
            "confidence": 1.0,
            "edge_ts": edge["timestamp"] or 0,
            "edge_state": "confirmed",  # 指针 = 既成的退役事实，无候选态
            "visible": _vis,
        })
        if not _vis:
            break  # 越权前任：收下这条就不再向上（防结构探测）
        cur = prev
    for item in ancestors:
        chain.append(item)
    # 末位 = 查询目标本身，relation 记"它取代了前一个"的语义（在渲染层用）
    # edge_state 留空：它不是"被指向的边"，是链条端点，无确认态可言（防渲染层给它打上假标签）
    chain.append({"id": mem_id, "relation": "", "confidence": 0, "edge_ts": 0,
                  "edge_state": "", "visible": True})
    return chain


def _lineage_sources(conn: sqlite3.Connection, mem_id: str, viewer: str = "") -> List[str]:
    """血缘：取本条目自己的 source_ids 列（它"吸收"了哪些来源）。

    P7.0：过可见性闸——source_ids 里可能有别人的 private 源，不能列出来。
    source_ids 为空 → 返回 []（表示无血缘，输出"无因果链"）——【如实说没有，不猜】。

    ── P7.4（2026-09-13）删掉「derived_from 跨条目并集」的派生批次分支 ──
    🔴 决策依据（生产库只读实测，勿凭 Spec 字面回填）：
      Spec 原话是"derived_from 合并加「必须同批次号」约束"，但实勘下来【没有可用的批次判别符】：
        · derived_from 列取值是【生成器名】不是批次号（distill-judge 587 / wiki-entity 98 /
          timeline 57 / distill 29 / prediction-engine 24 / profile-aggregator 23 …）→ 按它并集
          = 把该生成器产出过的全部条目的 source_ids 搅在一起，家谱里必然混进无关来源；
        · chunks 23 列里没有 batch_id/version 这类列（见 schema：id…retired_at）；
        · 唯一近似的 content_time 在 distill 产物上实测 587/587 全 NULL；
        · source 只是端名（该 587 条里 assistant 385 / dsh 202），粒度太粗，区分不出批次；
        · tags 实测为零条带日期/批次形态（拿不出可用键）。
      结论=【宁少不串】：删掉跨条目并集，只保留本条目自己的 source_ids。
      代价=某些"自己 source_ids 为空、只能靠同批兄弟反推"的条目，血缘会从"有但可能串错"变成"如实为空"；
      这正是想要的取舍——家谱里宁可少一行，不可多一行假的。
      ⚠️ 若将来 schema 补上真正的批次列（batch_id），再按"同 batch_id"重做并集；在那之前不许硬造键。
    """
    try:
        r = conn.execute("SELECT source_ids FROM chunks WHERE id=?", (mem_id,)).fetchone()
    except Exception:
        return []
    if not r:
        return []
    direct = _parse_ids(r["source_ids"])
    if direct:
        return _visible_ids(conn, direct, viewer)
    # P7.4：不再做 derived_from 跨条目反向并集（无批次判别符，见 docstring 决策依据）
    return []


def _distill_sources(conn: sqlite3.Connection, mem_id: str, viewer: str = "") -> List[str]:
    """蒸馏溯源：找 refined_ids 含 mem_id 的蒸馏单元，取其 raw_ids 作原料。

    P7.0：distill_units 自身没有可见性列，原料的可见性以【raw chunk】为准——
    merged 出来的 id 一律过 _visible_ids，别人的 private 原料不出现在你的家谱里。
    """
    try:
        rows = conn.execute(
            "SELECT raw_ids FROM distill_units WHERE refined_ids LIKE ?",
            ('%"' + mem_id + '"%',),
        ).fetchall()
    except Exception:
        rows = []
    merged: List[str] = []
    for row in rows:
        for x in _parse_ids(row["raw_ids"]):
            if x not in merged:
                merged.append(x)
    return _visible_ids(conn, merged, viewer)


def _successors(conn: sqlite3.Connection, mem_id: str, limit: int = 5,
                viewer: str = "") -> List[Dict[str, Any]]:
    """反向：谁取代了本条目（读本条自己的 chunks.superseded_by 指针）。

    为什么需要：查一条【旧】条时，它的祖先链是空的（它就是最老的），但用户真正想知道的是
    "它后来被谁顶掉了"。只给祖先不给后辈，读旧条的人会以为这条记忆是孤岛。

    🔴【2026-09-14 改道】数据源从 memory_evolution 边表切到 chunks.superseded_by。
    语义：本条的 superseded_by 非空 ⟺ 本条已被该 id 取代（本条旧 / 对方新）。
    只会有至多 1 个直接后辈（指针是单值列），故 limit 仅作兼容保留。
    P7.4 的候选边概念随改道消失——指针是既成退役事实，edge_state 恒为 "confirmed"。

    P7.0：后辈不可见时连 id 都不给（指针目标是 single 值，直接判它是否可见即可）。
    """
    try:
        r = conn.execute(
            "SELECT superseded_by FROM chunks WHERE id=?", (mem_id,)
        ).fetchone()
    except Exception:
        return []
    if not r:
        return []
    sid = str(r["superseded_by"] or "").strip()
    if not sid:
        return []
    if not _id_visible(conn, sid, viewer):
        return []
    return [{"id": sid, "relation": "supersedes", "confidence": 1.0,
             "edge_state": "confirmed"}]


def trace(conn: sqlite3.Connection, mem_id: str, depth: int = 3, want_json: bool = False,
          viewer: str = "") -> Dict[str, Any]:
    """主入口：对 mem_id 生成因果链。

    depth：向上/向下的代数上限（默认 3，够一般家谱）。
    viewer：调用方身份（CLI 走 --as/XTMEM_ACTOR）。**空 viewer = fail-closed，只放行 shared**，
      与 memory/visibility.py 的三态语义一致——【不】回落"全可见"（P7.0）。
    返回结构（json 与文本共用同一数据源，信息等量）：
      {
        root, generations, latest, nodes, edges, distill, retired, summary,
        text  # 文本渲染（want_json=False 时 CLI 直接打印）
      }
    """
    depth = max(1, min(int(depth or 3), _MAX_DEPTH))

    # 1) 根条目基本信息
    # P7.0 fail-closed：根条目对 viewer 不可见 → 不给正文，且**报错文案与"确实不存在"完全一致**，
    # 防有人拿"未找到 vs 无权"的差异去探测某条 private 记忆是否存在（存在性本身也是信息）。
    info = _load_chunks(conn, [mem_id], viewer).get(mem_id)
    if not info or not info.get("visible"):
        return {"root": mem_id, "ok": False, "error": "未找到该条目",
                "text": "未找到条目 %s" % mem_id}
    is_retired = bool(info.get("retired_by"))

    # 2) 演化链（memory_evolution 祖先 + 自身）
    chain = _evolution_chain(conn, mem_id, depth, viewer)

    # 3) 血缘（source_ids / 派生批次）
    lineage = _lineage_sources(conn, mem_id, viewer)
    # 4) 蒸馏原料
    distill = _distill_sources(conn, mem_id, viewer)
    # 4b) 后辈：谁取代了我（读旧条时才是用户真正想要的答案）
    succ = _successors(conn, mem_id, viewer=viewer)

    # 5) 汇总节点（用于文本与 json 都一样）
    nodes: List[Dict[str, Any]] = []
    all_ids = [c["id"] for c in chain] + lineage + distill
    meta = _load_chunks(conn, [x for x in all_ids if x], viewer)
    for c in chain:
        m = meta.get(c["id"], {})
        _self = (c["id"] == mem_id)
        nodes.append({
            "id": c["id"],
            "generation": len(nodes),  # 0=最老
            "relation_to_child": c["relation"],
            "confidence": round(c["confidence"], 3),
            # P7.4：这条边的确认态（confirmed/candidate）。端点自身为空串（无被指向的边）。
            # 名字用 edge_state 不用 state：下面 state 已表示 chunk 生命周期（active/retired），
            # 两者语义正交（一条 retired 记忆的取代边照样可以是 confirmed），混用名字会读错。
            "edge_state": c.get("edge_state") or "",
            "content": (m.get("content") or "" if not _self else (info.get("content") or ""))[:120],
            "ts": m.get("ts") if not _self else (info.get("ts") or 0),
            "tier": m.get("tier") or (info.get("tier") or ""),
            "state": m.get("state") or (info.get("state") or ""),
            "retired_by": m.get("retired_by") or (info.get("retired_by") or ""),
            "retire_reason": m.get("retire_reason") or (info.get("retire_reason") or ""),
            # P7.0：不可见节点保留 id/state 以示"此处有记录"，正文已空——前端据此打码
            "visible": bool(m.get("visible")) if not _self else True,
        })

    # 6) 文本渲染（信息等量，只是排版不同）
    short = mem_id[-12:] if len(mem_id) > 12 else mem_id
    _hidden = sum(1 for n in nodes if not n.get("visible"))
    lines = ["# %s 的因果链（%d 代）" % (short, len(chain))]
    # 无血缘：没有祖先链、没有血缘、没有蒸馏 → 如实说"无因果链"，但若有后辈则另起一句说明
    # ⚠️ 不打印"无因果链"之后又重复打一节自身节点——那是同一件事说两遍（真数据复核时发现的）
    if len(nodes) <= 1 and not lineage and not distill and not _hidden:
        lines.append("  本条为原始记录，无更早的因果链（未检测到前代/血缘/蒸馏来源）。")
    for i, n in enumerate(nodes):
        arrow = "  └─ "
        rel = ("「%s」" % _REL_LABEL.get(n["relation_to_child"], "关联")) if n["relation_to_child"] else ""
        line = "%s[%s] %s %s" % (arrow, _ts_str(n["ts"], "?").split(" ")[0], n["id"], rel)
        if n["content"]:
            line += " " + (n["content"] if len(n["content"]) <= 40 else n["content"][:37] + "...")
        state_note = ""
        if not n.get("visible"):
            # 越权条：只示存在，不给正文（content 已在 _load_chunks 打码为空）
            state_note = "  [无权查看]"
        elif n.get("edge_state") == "candidate":
            # P7.4：候选边（影子期）——显示但标注，防用户把候选当成已确认结论
            state_note = "  [候选]"
        elif n["state"] == "retired":
            state_note = "  [已退役:%s]" % (n["retire_reason"] or n["retired_by"] or "无原因")
        elif n["state"] != "active":
            state_note = "  [%s]" % n["state"]
        lines.append(line + state_note)
    if _hidden:
        lines.append("  ⚠ 本链有 %d 条对当前身份不可见（正文已隐去，仅示其存在）" % _hidden)
    if succ:
        lines.append("  后续演化（本条被谁接替）: " + ", ".join(
            "%s(%s%s)" % (s["id"], _REL_LABEL.get(s["relation"], s["relation"] or "关联"),
                          "·候选" if s.get("edge_state") == "candidate" else "") for s in succ))
    if lineage:
        lines.append("  血缘来源: " + ", ".join(lineage[:8]) + ("…" if len(lineage) > 8 else ""))
    if distill:
        lines.append("  蒸馏原料: " + ", ".join(distill[:6]) + ("…" if len(distill) > 6 else ""))
    if is_retired:
        lines.append("  退籍: by=%s 理由=%s" % (info.get("retired_by"), info.get("retire_reason")))

    result: Dict[str, Any] = {
        "root": mem_id,
        "ok": True,
        "depth": depth,
        "viewer": viewer,
        "generations": len(chain),
        "hidden_count": _hidden,
        "nodes": nodes,
        "lineage": lineage,
        "distill": distill,
        "successors": succ,
        "retired": {
            "by": info.get("retired_by"),
            "at": _ts_str(info.get("retired_at")),
            "reason": info.get("retire_reason"),
        },
        "text": "\n".join(lines),
    }
    return result