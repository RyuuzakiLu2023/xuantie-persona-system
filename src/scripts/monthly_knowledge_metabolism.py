# -*- coding: utf-8 -*-
"""monthly_knowledge_metabolism.py — 月度知识代谢（补脚本，2026-08-25，用户拍板选 A）。

零 LLM、零网络：只做统计/报告，不修改任何库与记忆内容。
- 月度报告：当前月份代号 + 记忆目录（memories/）与共享库统计（只读）。
- 输出：<OUT_DIR>/knowledge-metabolism-YYYY-MM.md，并打印 JSON（供 cron 状态）。
- 环境：XTMEM_REPORT_DIR 可覆盖输出目录（默认 mimox-data/memories）；
        XTMEM_DB_PATH 可覆盖库路径（默认生产基准库，只读查询）。

挂载（cron，任务已存在 id=6968b3e559a3 指向本脚本）：
    script: monthly_knowledge_metabolism.py  no_agent: true  expr: 0 5 1 * *
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_MEMORIES = os.environ.get("XTMEM_MEMORIES", "memories")
REPORT_DIR = os.environ.get("XTMEM_REPORT_DIR", DEFAULT_MEMORIES)
DB = os.environ.get("XTMEM_DB_PATH", "xtmem.db")


def _dir_stats(d: Path) -> dict:
    files = sorted(d.glob("*.md")) if d.exists() else []
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except Exception:
            pass
    return {"count": len(files), "bytes": total, "newest": str(files[-1].name) if files else ""}


def _db_stats() -> dict:
    out = {"available": False}
    if not os.path.exists(DB):
        return out
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        out["available"] = True
        out["total"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)")]
        if "namespace" in cols:
            out["by_namespace"] = {ns: n for ns, n in conn.execute(
                "SELECT namespace, count(*) FROM chunks WHERE state='active' GROUP BY namespace ORDER BY 2 DESC")}
        if "updated_at" in cols:
            out["week_new"] = conn.execute(
                "SELECT count(*) FROM chunks WHERE updated_at >= ?", (__import__("time").time() - 7 * 86400,)).fetchone()[0]
        conn.close()
    except Exception as exc:
        out["error"] = str(exc)
    return out


def main() -> int:
    memories = Path(DEFAULT_MEMORIES)
    now = datetime.now()
    report_dir = Path(REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    md = _dir_stats(memories)
    db = _db_stats()
    path = report_dir / f"knowledge-metabolism-{now.year}-{now.month:02d}.md"
    lines = [
        f"# 知识代谢 · {now.year}-{now.month:02d}",
        "",
        f"- 生成时间：{now.isoformat(timespec='seconds')}",
        f"- 记忆目录（{memories}）：{md['count']} 个 md，共 {md['bytes']} 字节",
        f"- 共享库（{DB}）：{'不可用' if not db.get('available') else str(db.get('total')) + ' 条'}",
    ]
    if db.get("by_namespace"):
        lines.append("- 活跃分域：" + " / ".join(f"{k}={v}" for k, v in db["by_namespace"].items()))
    if db.get("week_new") is not None:
        lines.append(f"- 近 7 天新增：{db['week_new']} 条")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    result = {"ok": True, "report": str(path), "memories": md, "db": db}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
