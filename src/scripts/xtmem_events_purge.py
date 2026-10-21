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

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

# CLI 入口统一由 xtmem_common 解析（$XTMEM_CLI > 已安装 xtmem 模块 > 仓库内 src/cli）。
# ⚠️ 这里返回的是 argv 前缀，不能自己拼 `[python, "xtmem", ...]`（PATH 上的 console script
#   不是 .py 文件，解释器会把它当脚本路径直接报错）。
PYTHON = sys.executable


def main() -> int:
    proc = subprocess.run(common.cli_argv() + ["maintain"],
                          capture_output=True, text=True, encoding="utf-8")
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
