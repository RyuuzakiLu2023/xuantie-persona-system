# -*- coding: utf-8 -*-
"""xtmem_lineage_stale_report.py — 反向血缘健康盘点（只读）。

【这是什么】
只查不写，给两个数：
  ① stale 候选：active 派生指向 archived 源（派生基于失效来源，内容可能已经不对）；
  ② vis 泄漏候选：shared 派生指向 private 源（明文越源泄漏面）。

设计上写时已由可见性继承闸拦住，本脚本查的是**历史脏数据**与漏网行，
输出 counts + 样例，供审计/定时观察。

用法：python xtmem_lineage_stale_report.py [--db <path>] [--top 10]
输出：JSON 一行
"""
import argparse
import json
import os
import sqlite3
import sys

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common


def main() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_lineage_stale_report")
    ap.add_argument("--db", default=None)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    db_path = common.resolve_db_path(args.db)
    if not os.path.exists(db_path):
        print(json.dumps({"ok": False, "error": f"db not found: {db_path}"}, ensure_ascii=False))
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        derived = conn.execute(
            "SELECT id, source_ids, derived_from, visibility FROM chunks "
            "WHERE derived_from != '' AND state='active'"
        ).fetchall()
        stale = []     # (派生id, 派生类型, 源id, 源state)
        vis_leak = []  # (派生id, 派生类型, 源id)
        for d in derived:
            sid = d["source_ids"] or ""
            if not sid.startswith("["):
                continue  # 文件名型（日记蒸馏等）不是 chunk 血缘
            try:
                src_ids = json.loads(sid)
            except Exception:
                continue
            for s2 in src_ids:
                src = conn.execute(
                    "SELECT state, visibility FROM chunks WHERE id=?", (str(s2),)
                ).fetchone()
                if src and src["state"] == "archived":
                    stale.append([d["id"], d["derived_from"], str(s2), src["state"]])
                elif (src and src["state"] == "active" and src["visibility"] == "private"
                      and d["visibility"] == "shared"):
                    vis_leak.append([d["id"], d["derived_from"], str(s2)])
        out = {
            "ok": True,
            "db": db_path,
            "stale_archived_src": len(stale),
            "vis_leak_shared_on_private": len(vis_leak),
            "stale_samples": stale[:args.top],
            "vis_leak_samples": vis_leak[:args.top],
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
