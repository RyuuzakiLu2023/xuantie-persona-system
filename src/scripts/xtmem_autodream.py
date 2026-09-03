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
    r"<XT_ROOT>\mimox-data\.hermes\memory\vector_store.db",
)
DEFAULT_DREAM_DIR = os.environ.get(
    "XTMEM_DREAM_DIR",
    r"<XT_ROOT>\mimox-data\memories",
)
WEEK = 7 * 24 * 3600


def main() -> int:
    db = DEFAULT_DB
    conn = sqlite3.connect(db, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    now = time.time()
    cutoff = now - WEEK
    # 2026-08-31（P1）：梦境文件是公共资产 → 只蒸馏 visibility='shared' 的记忆，
    # private / shared-with 内容绝不进公共梦境文件。
    rows = conn.execute(
        "SELECT namespace, project, content, importance, timestamp, visibility FROM chunks "
        "WHERE state='active' AND visibility='shared' AND updated_at >= ? "
        "ORDER BY importance DESC, updated_at DESC LIMIT 200",
        (cutoff,),
    ).fetchall()
    conn.execute(
        "INSERT INTO xtmem_state(side,last_sync) VALUES(?,?) "
        "ON CONFLICT(side) DO UPDATE SET last_sync=excluded.last_sync",
        ("autodream", now),
    )
    conn.commit()
    conn.close()

    # P4.0 止血（2026-08-31，AF-5 教训·纵深防御）：SQL 过滤是第一道，落盘前断言是第二道。
    # 任何直连 db 的脚本产出公共文件时，必须显式过滤 visibility，且该过滤是脚本自身的责任。
    for r in rows:
        if r["visibility"] != "shared":
            raise RuntimeError(
                f"autodream 落盘前断言失败：非 shared 记忆越过 SQL 过滤（chunk_id 未选，namespace={r['namespace']}, visibility={r['visibility']}）。"
            )

    dream_dir = Path(DEFAULT_DREAM_DIR)
    dream_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    path = dream_dir / f"xtmem-autodream-{stamp}.md"
    # P0.5（2026-08-31）：curator 中立身份形式化——cron no_agent 蒸馏，作者标注为中立 curator
    CURATOR_IDENTITY = "xtmem_curator"
    lines = [f"# XTMEM AutoDream · {stamp}", "",
             f"- 蒸馏执行身份：{CURATOR_IDENTITY}（中立 curator，no_agent）",
             f"- 蒸馏范围：近 7 天（{WEEK // 86400} 天）· 仅公开记忆（visibility=shared）· 共 {len(rows)} 条候选", ""]
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
