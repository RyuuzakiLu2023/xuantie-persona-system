# -*- coding: utf-8 -*-
"""xtmem_lineage_resolve.py — lineage-stale 标记的消费方（血缘闭环）。

【这是什么】
反向血缘（V1.1）在源被归档时会给引用它的派生记忆打 `lineage-stale:<src后缀>` 标，
但"打标"只是记账，不会自己修。本脚本补上消费腿，对每条带标且 active 的派生：

  ① 源 archived（内容仍在）或查无此 id（真没了）→ 从派生 source_ids 剔除；
  ② 剔除后源集为空 → 派生归档（并沿反向血缘给引用它的派生再打标，嵌套处理）；
  ③ 剔除后仍有源 → 更新 source_ids + 清 `lineage-stale:*` 标（保留其它 tag）+ 事件留痕；
  ④ 正文换新不归本脚本：内容更新由各生成器周期重建（distill/wiki/profile 跑时 supersede）。

【为什么要单独一个脚本】
打标不改 state 是刻意的（标记可回退、可审计）。但标只进不出，库会一直挂着
"来源已失效"的派生，检索仍会把它们当有效知识。消费腿必须有，且要幂等。

【域的取舍（公开化改动）】
运行正本里写死了一组"judge 域"（走人工评判链，stale 标只留不动）。公开版改成配置：
  `$XTMEM_LINEAGE_SKIP_DOMAINS`（逗号分隔）→ 这些域的标只计数、不代清理。
默认空 = 全部域都处理。这样"有人工审档流程的域"不需要改代码就能保护起来。

【默认只诊断（--apply 才写库）】
血缘修复会改 source_ids、甚至归档整条派生——那是不可逆的内容层后果。
所以默认 `--dry-run` 语义：只盘点、只打印将要发生什么，零写入。
真要执行必须显式 `--apply`；这是"已知风险默认不启"，不是砍功能。

【安全口径】
  - 归档时 `superseded_by` 必须是空串或真指针 `mem-xxxxxxxx`：原因字串一律写
    `retire_reason`。把原因塞进指针列会污染血缘回溯（历史存量 1226 条就是这么来的）。
  - 只处理 `tags LIKE '%lineage-stale:%'` 且 state='active' 的行；无标零副作用。

用法：
  python xtmem_lineage_resolve.py                    # 默认：只诊断（dry-run）
  python xtmem_lineage_resolve.py --apply            # 真执行（写库）
  python xtmem_lineage_resolve.py --apply --skip-domain my-judged-ns
  （--dry-run 保留为显式同义参数，便于老调用）
输出：JSON 一行

*创建：2026-10-22 · 从运行正本公开化（域名单改为环境变量，路径走 xtmem_common）*
"""
import argparse
import json
import sqlite3
import sys

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
    from .xtmem_sources import skip_domains
except ImportError:  # pragma: no cover
    import xtmem_common as common
    from xtmem_sources import skip_domains


def _clean_tags(tags, stale_prefix="lineage-stale:"):
    """移除所有 lineage-stale:* 前缀标，保留其它 tag。返回 (新tags, 移除数)。"""
    out = []
    removed = 0
    for t in tags or []:
        if str(t).startswith(stale_prefix):
            removed += 1
        else:
            out.append(t)
    return out, removed


def _find_derived_conn(conn, src_id):
    """源集含 src_id 的 active 派生（用 JSON 字符串边界匹配，防半匹配）。"""
    rows = conn.execute(
        "SELECT id FROM chunks WHERE derived_from != '' AND state='active' AND source_ids LIKE ?",
        ('%"' + src_id + '"%',),
    ).fetchall()
    return [r[0] for r in rows]


def resolve_stale(conn, domain_skip=None, dry_run=False):
    """消费 lineage-stale 标。返回统计 dict；dry_run=True 只盘点不写。"""
    import time as _t

    domain_skip = set(domain_skip if domain_skip is not None else skip_domains())
    rows = conn.execute(
        "SELECT id, namespace, derived_from, source_ids, tags FROM chunks "
        "WHERE state='active' AND tags LIKE '%lineage-stale:%'"
    ).fetchall()
    now = _t.time()
    out = {"scanned": len(rows), "judge_skipped": [], "pruned": 0, "archived": 0,
           "pruned_ids": [], "archived_ids": [], "skipped_unchanged": 0}
    for r in rows:
        cid = r["id"]
        ns = r["namespace"] or ""
        # 配置里列出的域：只留标、只计数，不代清理（那类域有人工审档流程）
        if ns in domain_skip:
            out["judge_skipped"].append(cid)
            continue
        try:
            srcs = json.loads(r["source_ids"] or "[]")
            if not isinstance(srcs, list):
                srcs = []
        except Exception:
            srcs = []
        if not r["source_ids"] or not str(r["source_ids"]).startswith("["):
            out["skipped_unchanged"] += 1
            continue  # 文件名型/非 JSON 源集（如日记蒸馏）不是 chunk 血缘，无源可剔
        keep = []
        removed = []
        for s in srcs:
            s = str(s)
            row = conn.execute("SELECT state FROM chunks WHERE id=?", (s,)).fetchone()
            if row and row["state"] in ("archived", "superseded"):
                # superseded 是演化正常覆盖（聚合型派生引多代 superseded 是设计）——
                # 只剔 archived；查无此 id 的 MISSING 也算失效剔除。
                if row["state"] == "archived":
                    removed.append(s)
                else:
                    keep.append(s)
            elif row and row["state"] == "active":
                keep.append(s)
            else:
                removed.append(s)  # MISSING（源真没了）
        tags, _ = _clean_tags(
            json.loads(r["tags"] or "[]") if (r["tags"] or "").startswith("[") else [])
        if dry_run:
            if removed and keep:
                out["pruned"] += 1
                out["pruned_ids"].append(cid)
            elif removed and not keep:
                out["archived"] += 1
                out["archived_ids"].append(cid)
            else:
                out["skipped_unchanged"] += 1
            continue
        if removed and not keep:
            # 源集全失效 → 派生归档 + 连带 propagate（嵌套 stale）。
            # 归档没有后继条目，指针列必须空串；原因写 retire_reason 三列。
            conn.execute(
                "UPDATE chunks SET state='archived', superseded_by='', "
                "retire_reason='lineage-stale-resolve', retired_by='lineage_resolve_cron', "
                "retired_at=?, tags=?, updated_at=? WHERE id=?",
                (now, json.dumps(tags, ensure_ascii=False), now, cid),
            )
            out["archived"] += 1
            out["archived_ids"].append(cid)
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-stale-resolved', ?)",
                (now, json.dumps({"action": "archived", "chunk_id": cid,
                                  "removed_sources": removed, "by": "lineage-resolve"},
                                 ensure_ascii=False)),
            )
            for d in _find_derived_conn(conn, cid):
                _tag_stale(conn, d, cid, now, "lineage-stale-resolve-cascade")
        elif removed and keep:
            # 剔除失效源 + 清 stale 标 → 血缘闭环（内容换新等生成器周期重建 supersede）
            conn.execute(
                "UPDATE chunks SET source_ids=?, tags=?, updated_at=? WHERE id=?",
                (json.dumps(keep, ensure_ascii=False),
                 json.dumps(tags, ensure_ascii=False), now, cid),
            )
            out["pruned"] += 1
            out["pruned_ids"].append(cid)
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-stale-resolved', ?)",
                (now, json.dumps({"action": "pruned", "chunk_id": cid,
                                  "removed_sources": removed, "keep_sources": keep,
                                  "by": "lineage-resolve"}, ensure_ascii=False)),
            )
        else:
            out["skipped_unchanged"] += 1
        conn.commit()
    return out


def _tag_stale(conn, cid, src_id, now, reason):
    """给指向已归档派生的 active 派生打 lineage-stale 标（嵌套连带）。"""
    row = conn.execute("SELECT tags FROM chunks WHERE id=?", (cid,)).fetchone()
    try:
        tags = json.loads(row["tags"] or "[]") if row and (row["tags"] or "").startswith("[") else []
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    tag = "lineage-stale:" + str(src_id)[-8:]
    if tag not in tags:
        tags.append(tag)
    conn.execute("UPDATE chunks SET tags=?, updated_at=? WHERE id=?",
                 (json.dumps(tags, ensure_ascii=False), now, cid))
    conn.execute("INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-stale', ?)",
                 (now, json.dumps({"source_id": src_id, "reason": reason, "derived": [cid],
                                   "by": "lineage-resolve-cascade"}, ensure_ascii=False)))


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="xtmem_lineage_resolve",
        description="lineage-stale 标记消费：默认只诊断；加 --apply 才真正改库")
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="真正执行修复（改 source_ids / 归档空血统派生）。不加=只盘点")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式只诊断（默认即此行为，保留此参数兼容老调用）")
    ap.add_argument("--skip-domain", action="append", default=None,
                    help="只留标不代清理的域（可重复；默认取 $XTMEM_LINEAGE_SKIP_DOMAINS）")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        print(json.dumps({"ok": False,
                          "error": "--apply 与 --dry-run 冲突，请二选一"}, ensure_ascii=False))
        return 2
    effective_dry = not args.apply
    db_path = common.resolve_db_path(args.db)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        skip = set(args.skip_domain) if args.skip_domain else None
        out = resolve_stale(conn, domain_skip=skip, dry_run=effective_dry)
        print(json.dumps({"ok": True, "db": db_path, "mode": "apply" if args.apply else "diagnose",
                          **out}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
