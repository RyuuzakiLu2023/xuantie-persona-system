# -*- coding: utf-8 -*-
"""xtmem_timeline.py — 时间骨架生成器（月度骨架 + 近 48h 摘要）。

【这是什么】
给每个 namespace 生成一条「时间骨架」chunk：
  - track='timeline' / tier='refined' / importance=1.0 / derived_from='timeline'
  - 正文 = 近 N 个月按月汇总 + 近 48h 按日汇总 + 尾行（生成方式）
  - source_ids = **正文实际采用**的原料 id（不是候选池全量）
  - 旧活骨架 supersede 到新版（保留历史，检索只见新版）

【为什么要 chunk 路线而不是独立月报文件】
注入面只读库：写进 chunks 就天然拿到血缘、回滚、幂等和可见性继承；
独立文件还得再造一套权限与生命周期。

【幂等口径】
  - 月度锚 = 标题里的 YYYYMM：同月已有活骨架 → 默认跳过（月内重跑不翻版）。
  - --refresh-48h：同月强制产新版，旧版 supersede。
  - ⚠️【不】把"同月已存在就跳过"改成纯 content_hash：48h 段每次运行都变，
    纯 hash 会月内反复翻版（历史结论）。
  - 生成器【不】读自己上一版：取材统一走 xtmem_sources.select_materials，
    否则正文里会嵌着旧骨架全文（实测出现过 `# 时间骨架` 重复三次）。

【公开化改动（相对运行正本）】
  - 库路径/CLI/引擎统一走 xtmem_common；不再写死生产路径。
  - 默认 namespace = 库里实际存在的域；可用 --domain 或 $XTMEM_NAMESPACE 指定。
  - 时区走 xtmem_common.tz()（默认 Asia/Shanghai，$XTMEM_TZ 可覆盖）。

用法：python xtmem_timeline.py [--domain <ns>] [--months 12] [--refresh-48h] [--db <path>]
输出：JSON 一行
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from calendar import monthrange
from datetime import datetime, timedelta

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
    from .xtmem_sources import (KIND_LABEL, actual_source_ids, ingested_time,
                                occurrence_time, quarantine_ok, select_materials)
except ImportError:  # pragma: no cover
    import xtmem_common as common
    from xtmem_sources import (KIND_LABEL, actual_source_ids, ingested_time,
                               occurrence_time, quarantine_ok, select_materials)

BJ = common.tz()
DERIVED = "timeline"
PREVIEW = 140  # 每行要点截断

_VS = None


def _vecstore(db_path: str):
    """supersede 的唯一写路径 = VectorStore.mark_superseded。

    为什么不许裸 UPDATE 把 state 置 superseded：那条路不写 retire_reason/retired_by/
    retired_at 三列、不删 chunks_fts 行（superseded 幽灵命中）、也不清 embedding_cache。
    """
    global _VS
    if _VS is None:
        _VS = common.vector_store(db_path)
    return _VS


def _month_bounds(ym: str):
    """'YYYYMM' → (start_ts, end_ts)。起止按本地时区 0 点/23:59:59 折算成 epoch。"""
    y, m = int(ym[:4]), int(ym[4:6])
    days = monthrange(y, m)[1]
    start = datetime(y, m, 1, tzinfo=BJ).timestamp()
    end = datetime(y, m, days, 23, 59, 59, tzinfo=BJ).timestamp()
    return start, end


def _recent_months(n):
    now = datetime.now(BJ)
    keys = []
    y, m = now.year, now.month
    for _ in range(n):
        keys.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(keys))


def _has_content_time(conn):
    """老库可能还没有 content_time 列（更早版本），缺列就别在 SQL 里引用它。"""
    return any(r[1] == "content_time" for r in conn.execute("PRAGMA table_info(chunks)"))


# 候选列清单写在一处：缺一列不会报错，只会**静默失去一项保护**。
_CAND_COLS = ("id, content, importance, timestamp, track, derived_from, tier, "
              "visibility, namespace, speaker, provenance_status, confirmation_type, "
              "retire_reason, source_ids")


def _load_candidates(conn, ns, months, now):
    """把本域可能的原料一次取出来（月窗口 ∪ 近 48h），后面的分组全在内存里做。

    窗口用 OR 同时覆盖两种时间：有 occurrence（content_time）的按发生时间进窗，
    没 occurrence 的按入库时间进窗——"三个月前发生、上周才入库"的记忆不会漏。
    """
    cutoff_ing = now - months * 31 * 86400  # 入库窗口给足（月长取上限 31 天）
    has_ct = _has_content_time(conn)
    cols = _CAND_COLS + (", content_time" if has_ct else "")
    if has_ct:
        sql = (f"SELECT {cols} FROM chunks WHERE state='active' AND namespace=? AND ("
               "timestamp>=? OR (content_time IS NOT NULL AND content_time>=?)) "
               "ORDER BY importance DESC, timestamp DESC LIMIT 4000")
        rows = conn.execute(sql, (ns, cutoff_ing, cutoff_ing)).fetchall()
    else:
        sql = (f"SELECT {cols} FROM chunks WHERE state='active' AND namespace=? "
               "AND timestamp>=? ORDER BY importance DESC, timestamp DESC LIMIT 4000")
        rows = conn.execute(sql, (ns, cutoff_ing)).fetchall()
    return rows


def _supersede_old(conn, db_path, ns, new_id):
    """旧活骨架逐条走 mark_superseded（reason=timeline-reaggregate / by=timeline）。"""
    rows = conn.execute(
        "SELECT id FROM chunks WHERE namespace=? AND state='active' AND track='timeline' AND id!=?",
        (ns, new_id),
    ).fetchall()
    vs = _vecstore(db_path)
    n = 0
    for rr in rows:
        if vs.mark_superseded(rr["id"], successor_id=new_id,
                              reason="timeline-reaggregate", by="timeline"):
            n += 1
    return n


def _month_exists(conn, ns, stamp):
    return conn.execute(
        "SELECT 1 FROM chunks WHERE namespace=? AND track='timeline' AND state='active' "
        "AND content LIKE '%' || ? || '%' LIMIT 1",
        (ns, stamp)).fetchone() is not None


# 一层汇总最多展开几行：月/日 → (类型 × 每类代表数)。这一级是**写死的**，不是递归——
# 【不】让汇总去读上一版骨架再汇总，那正是"刷新 30 次越滚越大"的成因。
MONTH_MAX_KINDS = 3
MONTH_PER_KIND = 2
DAY_MAX_KINDS = 3
DAY_PER_KIND = 2


def _bucket(kind_groups, label, max_kinds, per_kind):
    """把一段时间内按类型分好组的行压成有限行正文，并回传**实际采用**的行。

    返回 (lines, used_rows, counts)。used_rows 是真正的血缘依据——【不】从正文里反解
    [src:xxxxxxxx]：库里 id 是 mem-<8位>，正文只显示前 8 字符，前缀撞车会把没采用的
    条目也算成"已采用"。

    类型不混写：每个类型单独出块，块内每条一行、各带自己的 [src:]。

    ★M04 R01：**不输出"另 N 条未展开"这种计数行**。那些没展开的条目虽然正文没写，
      但"N 是多少"本身就是从它们推出来的信息。一条私有材料只要参与计数，就通过
      "另 1 条"泄露了**存在性**。计数改走 run_report（受控回执），不进页面。
    """
    order = ("verbatim", "fact", "unknown", "derived")
    lines, used = [], []
    counts = {}
    for kind in [k for k in order if kind_groups.get(k)][:max_kinds]:
        rows = sorted(kind_groups[kind], key=lambda r: -(r["importance"] or 0))
        label_txt = KIND_LABEL.get(kind, "未标注")
        for top in rows[:per_kind]:
            occ = occurrence_time(top)
            ing = ingested_time(top)
            # 三类时间分开显示：发生时间优先；只有入库时间就照实标 [入库:]，不冒充发生时间
            if occ is not None:
                stamp_txt = f"发生{datetime.fromtimestamp(occ, tz=BJ).strftime('%m-%d')}"
                if ing is not None and abs(ing - occ) > 86400:
                    stamp_txt += f"/入库{datetime.fromtimestamp(ing, tz=BJ).strftime('%m-%d')}"
            elif ing is not None:
                stamp_txt = f"[入库:{datetime.fromtimestamp(ing, tz=BJ).strftime('%m-%d')}]"
            else:
                stamp_txt = "[时间未知]"
            preview = str(top["content"])[:PREVIEW].replace(chr(10), " ")
            lines.append(f"- {label} · {label_txt} · {stamp_txt} · "
                         f"{preview} [src:{str(top['id'])[:8]}]")
            used.append(top)
        shown = min(len(rows), per_kind)
        if len(rows) > shown:
            counts[kind] = counts.get(kind, 0) + (len(rows) - shown)
    other = [k for k in order if kind_groups.get(k)][max_kinds:]
    if other:
        hid = sum(len(kind_groups[k]) for k in other)
        counts["_hidden_kinds"] = counts.get("_hidden_kinds", 0) + hid
    return lines, used, counts


def _run_domain(conn, db_path, ns, months, refresh):
    now = time.time()
    raw_rows = _load_candidates(conn, ns, months, now)

    # ① 权限闸：不可见的行在取材前剔除（不是生成后再过滤）
    visible, denied = [], 0
    for r in raw_rows:
        if quarantine_ok(r, ns):
            visible.append(r)
        else:
            denied += 1
    # ② 自引/聚合页闸 + ③ 血缘深度闸（传 conn 才会顺 source_ids 往上查）
    kept, rejected = select_materials(visible, "timeline", conn=conn)

    month_keys = _recent_months(months)
    cutoff48 = now - 48 * 3600
    month_kinds = {ym: {} for ym in month_keys}
    day_kinds = {}
    for row, kind in kept:
        occ = occurrence_time(row)
        ing = ingested_time(row)
        anchor = occ if occ is not None else ing
        if anchor is None:
            continue
        d = datetime.fromtimestamp(anchor, tz=BJ)
        ym = d.strftime("%Y%m")
        if ym in month_kinds:
            month_kinds[ym].setdefault(_kind_key(kind), []).append(row)
        # 48h 窗口按**入库时间**判（"最近发生了什么"是运行视角），但行内仍显示发生时间
        if ing is not None and ing >= cutoff48:
            day_kinds.setdefault(d.strftime("%Y-%m-%d"), {}).setdefault(
                _kind_key(kind), []).append(row)

    month_lines, used_rows, hidden_counts = [], [], {}
    for ym in month_keys:
        groups = month_kinds[ym]
        if not groups:
            month_lines.append(f"- {ym}: 无记录（不报错，空位留白）")
            continue
        lines, used, counts = _bucket(groups, ym, MONTH_MAX_KINDS, MONTH_PER_KIND)
        month_lines += lines
        used_rows += used
        for k, v in counts.items():
            hidden_counts[k] = hidden_counts.get(k, 0) + v

    day_lines = []
    for day in sorted(day_kinds)[-3:]:   # 深度上限：48h 最多跨 3 个自然日
        lines, used, counts = _bucket(day_kinds[day], day, DAY_MAX_KINDS, DAY_PER_KIND)
        day_lines += lines
        used_rows += used
        for k, v in counts.items():
            hidden_counts[k] = hidden_counts.get(k, 0) + v

    if not kept:
        return {"ok": False, "ns": ns, "reason": "no-materials",
                "denied": denied, "rejected": len(rejected)}

    stamp = datetime.now(BJ).strftime("%Y%m")
    # 幂等：同月已有活时间骨架 → 默认跳过（月内重跑不翻版）；--refresh-48h 才产新版
    if _month_exists(conn, ns, stamp) and not refresh:
        return {"ok": True, "ns": ns, "skipped": "same-month-exists", "stamp": stamp}

    # 来源登记 = 正文实际采用的那些行（去重保序）。
    source_ids = actual_source_ids([(r, "") for r in used_rows])

    generated_at = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# 时间骨架 · {ns} · {stamp}", "",
             f"## 月度摘要（近 {months} 个月；发生时间归月，缺发生时间标 [入库:]）"] + month_lines
    lines += ["", "## 近 48h（按入库时间取窗，行内显示发生时间）"] + (day_lines or ["- 近48h 无记录"])
    lines += ["", "---",
              f"生成时间：{generated_at}（不参与任何一条的时间判定）",
              "取材口径：仅原始证据与已确认事实；已排除本生成器旧视图、聚合页与隔离行",
              "层级：月 → 类型（展示两级；取材的血缘深度另受上限约束，见运行回执）"]
    content = chr(10).join(lines)

    r = common.store_via_cli(
        db_path=db_path, agent=ns or "default", namespace=ns or "default",
        text=content, importance=1.0, derived_from=DERIVED,
        source_ids=source_ids, tier="refined", track="timeline",
        tags=["timeline", "xtmem"],
    )
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "reason": "store failed", "r": r}
    new_id = r.get("id") or (r.get("chunk") or {}).get("id")
    sup = _supersede_old(conn, db_path, ns, new_id) if new_id else 0
    # run_report = 受控运行回执（不落库、不进正文）：诊断计数只在这里，供审计/日志。
    run_report = {
        "denied_quarantined_or_invisible": denied,
        "rejected_by_reason": _count_reasons(rejected),
        "unexpanded_counts": hidden_counts,
    }
    return {"ok": True, "ns": ns, "stamp": stamp, "id": new_id, "superseded": sup,
            "month_lines": len(month_lines), "day_lines": len(day_lines),
            "sources_used": len(source_ids), "run_report": run_report}


def _count_reasons(rejected):
    """把排除原因收成计数表（只进回执，不进正文）。"""
    out = {}
    for _rid, reason in rejected:
        out[reason] = out.get(reason, 0) + 1
    return out


def _kind_key(label):
    """正文标签 → 内部类型键（select_materials 回的是中文标签，这里反查）。"""
    for key, val in KIND_LABEL.items():
        if val == label:
            return key
    return "unknown"


def main():
    ap = argparse.ArgumentParser(prog="xtmem_timeline")
    ap.add_argument("--domain", default=None,
                    help="只处理该 namespace；缺省=库内全部")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--refresh-48h", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db_path = common.resolve_db_path(args.db)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        domains = ([args.domain] if args.domain
                   else common.resolve_namespaces(conn))
        results = [_run_domain(conn, db_path, ns, args.months, args.refresh_48h)
                   for ns in domains]
        print(json.dumps({"ok": True, "db": db_path, "results": results},
                         ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
