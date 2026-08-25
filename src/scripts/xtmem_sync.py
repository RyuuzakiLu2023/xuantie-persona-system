# -*- coding: utf-8 -*-
"""xtmem_sync.py — XTMEM 定时同步（阶段 2.8）：台账收敛（zcode 文件轨增量 import + 状态打卡）。

挂载建议（mimox-data/cron/jobs.json，切换窗口加）：
    {"id": "xtmem-sync", "name": "xtmem-sync", "script": "xtmem_sync.py",
     "no_agent": true, "schedule": {"kind": "interval", "minutes": 5}, "enabled": true}

环境：XTMEM_DB_PATH 可覆盖（测试副本）；XTMEM_ZCODE_MEMORIES 可覆盖 zcode 记忆根。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
XTMEM_PY = HERE.parent / "xtmem" / "xtmem.py"
PYTHON = sys.executable


def _run(argv):
    try:
        proc = subprocess.run(
            [PYTHON, str(XTMEM_PY), *argv], capture_output=True, text=True,
            encoding="utf-8", timeout=120, check=False,
        )
        out = (proc.stdout or "").strip()
        try:
            return json.loads(out) if out else {"ok": False, "error": "empty"}
        except Exception:
            return {"ok": False, "error": out[:200]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    result = {"ok": True, "synced_at": time.time(), "import": None, "sync": None}
    imp = _run(["import"])
    if not imp.get("ok"):
        return print(json.dumps({"ok": False, "error": "import failed: " + str(imp)}, ensure_ascii=False)), 1
    result["import"] = imp
    sync = _run(["sync", "--side", "hermes"])
    if not sync.get("ok"):
        return print(json.dumps({"ok": False, "error": "sync failed: " + str(sync)}, ensure_ascii=False)), 1
    result["sync"] = sync
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
