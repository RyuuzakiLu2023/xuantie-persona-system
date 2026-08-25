# -*- coding: utf-8 -*-
"""xtmem_autodream.py — XTMEM AutoDream 蒸馏（阶段 2.8，每周）：把共享库近 7 天高重要记忆蒸馏成"梦境"记录。

挂载建议（mimox-data/cron/jobs.json，切换窗口加）：
    {"id": "xtmem-autodream", "name": "xtmem-autodream", "script": "xtmem_autodream.py",
     "no_agent": true, "schedule": {"kind": "cron", "expr": "0 4 * * 0", "display": "0 4 * * 0"}, "enabled": true}

环境：XTMEM_DB_PATH 可覆盖（测试副本）；XTMEM_DREAM_DIR 可覆盖输出目录（默认 mimox-data/memories）。
零 LLM（不调模型），只做统计蒸馏——"梦境"= 本周高重要度记忆摘要清单。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "XTMEM_DB_PATH",
    r"E:\2222\3333\mimox-data\.hermes\memory\vector_store.db",
)
DEFAULT_DREAM_DIR = os.environ.get(
    "XTMEM_DREAM_DIR",
    r"E:\2222\3333\mimox-data\memories",
)
WEEK = 7 * 24 * 3600


def main() -> int:
    db = DEFAULT_DB
    conn = sqlite3.connect(db, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    now = time.time()
    cutoff = now - WEEK
    rows = conn.execute(
        "SELECT namespace, project, content, importance, timestamp FROM chunks "
        "WHERE state='active' AND updated_at >= ? ORDER BY importance DESC, updated_at DESC LIMIT 200",
        (cutoff,),
    ).fetchall()
    conn.execute(
        "INSERT INTO xtmem_state(side,last_sync) VALUES(?,?) "
        "ON CONFLICT(side) DO UPDATE SET last_sync=excluded.last_sync",
        ("autodream", now),
    )
    conn.commit()
    conn.close()

    dream_dir = Path(DEFAULT_DREAM_DIR)
    dream_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    path = dream_dir / f"xtmem-autodream-{stamp}.md"
    lines = [f"# XTMEM AutoDream · {stamp}", "", f"- 蒸馏范围：近 7 天（{WEEK // 86400} 天），共 {len(rows)} 条候选", ""]
    by_ns: dict = {}
    for r in rows:
        ns = r["namespace"]
        by_ns.setdefault(ns, []).append(r)
    for ns, items in by_ns.items():
        lines.append(f"## {ns}（{len(items)} 条）")
        for r in items[:20]:
            lines.append(f"- ★{r['importance']:.2f} [{r['project'] or '—'}] {str(r['content'])[:160]}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "dream": str(path), "candidates": len(rows), "namespaces": len(by_ns)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
