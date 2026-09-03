# -*- coding: utf-8 -*-
"""xtmem_distill.py — XTMEM 蒸馏器（P4.8 T6，2026-09-01，小D）。

把 raw 层（对话流水）按 (namespace, project) 聚合成 refined 蒸馏产物：
  - tier='refined' / derived_from='distill' / source_ids=[组内 raw chunk id...] / 血缘闸 P0.1
  - 每条后缀 [src:xxxxxxxx]（句级溯源）
  - 可见性走 CLI store 的 min_visibility 继承最严源（部分源 private → 产物 private，绝不越源）
  - 幂等：同 (ns, project) 已有 active 蒸馏产物则跳过（不重蒸；改判据/回填用独立标记 distill-backfill）

回填分批（AF-8）后续做：每晚 50 组 importance DESC、首批 20 组人工验精度、独立标记 distill-backfill 可整批 DELETE。
用法：python xtmem_distill.py [--domain <ns>] [--project <p>] [--site <raw|all>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
DEFAULT_CLI = os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py")
PYTHON = sys.executable


def _rows(conn, ns=None, project=None):
    sql = ("SELECT id, namespace, project, content, importance, track, timestamp FROM chunks "
           "WHERE state='active' AND tier='raw'")
    params = []
    if ns:
        sql += " AND namespace=?"
        params.append(ns)
    if project:
        sql += " AND project=?"
        params.append(project)
    sql += " ORDER BY importance DESC, timestamp DESC"
    return conn.execute(sql, params).fetchall()


def _store_via_cli(content, ns, project, source_ids, domain, derived_from="distill"):
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = domain
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", domain, "--namespace", ns,
            "--project", project, "--text", content, "--importance", "0.7",
            "--derived-from", derived_from, "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "default", "--tags", "distill,xtmem"]
    proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def _distill_group(rows):
    """规则聚合：按 track 分组，抽高 importance 预览，带 [src] 句级溯源。"""
    by_track = defaultdict(list)
    for r in rows:
        by_track[r["track"]].append(r)
    lines = []
    for track in ("preference", "identity", "procedural", "default", "emotion"):
        items = by_track.get(track, [])
        if not items:
            continue
        lines.append(f"## {track}")
        for r in sorted(items, key=lambda x: -x["importance"])[:30]:
            lines.append(f"- {str(r['content'])[:160]} [src:{r['id'][:8]}]")
    if not lines:
        return None
    return "\n".join(lines)


def _run_group(conn, ns, project):
    rows = _rows(conn, ns, project)
    if len(rows) < 3:
        return {"ok": False, "ns": ns, "project": project or "", "reason": "raw<3", "n": len(rows)}
    # 幂等：同 (ns, project) 已有 active distill 产物则跳过
    existing = conn.execute(
        "SELECT 1 FROM chunks WHERE namespace=? AND project=? AND derived_from='distill' AND state='active' LIMIT 1",
        (ns, project or ""),
    ).fetchone()
    if existing:
        return {"ok": True, "ns": ns, "project": project or "", "skipped": "already-distilled", "n": len(rows)}
    source_ids = [r["id"] for r in rows]
    body = _distill_group(rows)
    if body is None:
        return {"ok": False, "ns": ns, "project": project or "", "reason": "empty-distill"}
    content = f"# 蒸馏 · {ns} · {project or '—'}\n" + body
    r = _store_via_cli(content, ns, project, source_ids, ns)
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "project": project or "", "reason": "store failed", "r": r}
    return {"ok": True, "ns": ns, "project": project or "", "id": r.get("id"), "n": len(rows)}


def main():
    ap = argparse.ArgumentParser(prog="xtmem_distill")
    ap.add_argument("--domain", default=None)
    ap.add_argument("--project", default=None)
    args = ap.parse_args()
    import sqlite3
    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    # 分组：按 (namespace, project) 遍历 raw 活跃 chunk
    groups = conn.execute(
        "SELECT DISTINCT namespace, project FROM chunks WHERE state='active' AND tier='raw'"
    ).fetchall()
    results = []
    for g in groups:
        ns = g["namespace"]
        project = g["project"]
        if args.domain and ns != args.domain:
            continue
        if args.project and project != args.project:
            continue
        results.append(_run_group(conn, ns, project))
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
