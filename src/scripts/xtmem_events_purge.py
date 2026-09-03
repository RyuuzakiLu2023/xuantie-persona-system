# -*- coding: utf-8 -*-
"""xtmem_events_purge.py — XTMEM 事件表治理（P4.0-d）：调 xtmem.py maintain 清理 xtmem_events（分级 TTL）。

挂载（mimox-data/cron/jobs.json）：
    {"id": "xtmem-events-purge", "name": "xtmem-events-purge", "script": "xtmem_events_purge.py",
     "no_agent": true, "schedule": {"kind": "cron", "expr": "0 2 * * 1", "display": "0 2 * * 1"}, "enabled": true}

环境：XTMEM_DB_PATH 可覆盖（测试副本）；其余沿 xtmem.py 环境约定。
"""
import os
import subprocess
import sys
from pathlib import Path

# BF-4（2026-09-01）：硬链/profile 侧调用时 __file__ 不追溯主树，HERE.parent/xtmem 推导出错。
# 用 env 显式覆盖 + 生产绝对路径兜底。
XTMEM_PY = Path(os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"))
PYTHON = sys.executable


def main() -> int:
    proc = subprocess.run([PYTHON, str(XTMEM_PY), "maintain"], capture_output=True, text=True, encoding="utf-8")
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"events_purge failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
