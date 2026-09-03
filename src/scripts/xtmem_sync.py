# -*- coding: utf-8 -*-
"""xtmem_sync.py — XTMEM 定时同步（阶段 2.8）：台账收敛（zcode 文件轨增量 import + 状态打卡）。

挂载建议（mimox-data/cron/jobs.json，切换窗口加）：
    {"id": "xtmem-sync", "name": "xtmem-sync", "script": "xtmem_sync.py",
     "no_agent": true, "schedule": {"kind": "interval", "minutes": 5}, "enabled": true}

环境：XTMEM_DB_PATH 可覆盖（测试副本）；XTMEM_ZCODE_MEMORIES 可覆盖 zcode 记忆根。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# BF-4（2026-09-01）：脚本可能被硬链到 profile 侧调用，`__file__.resolve().parent` 取调用路径、
# 不追溯主树；`HERE.parent/xtmem/xtmem.py` 会推导出 profile 侧不存在的路径。改用 env 显式覆盖 + 生产绝对路径兜底。
XTMEM_PY = Path(os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"))
PYTHON = sys.executable


def _run(argv, env_extra=None):
    """BF-5（2026-09-01）：返回 (returncode, parsed)。

    按 returncode 判成败（带码可信）；空输出但 rc=0 = 正常 no-op（无待导入/无变化），
    空输出但 rc!=0 = 子进程真没跑起来（如路径错），绝不能当 no-op 吞掉（旧版 `error=="empty"` 判定危险）。
    """
    import os as _os
    env = {**_os.environ, **(env_extra or {})}
    try:
        proc = subprocess.run(
            [PYTHON, str(XTMEM_PY), *argv], capture_output=True, text=True,
            encoding="utf-8", timeout=120, check=False, env=env,
        )
        out = (proc.stdout or "").strip()
        try:
            parsed = json.loads(out) if out else {}
        except Exception:
            parsed = {}
        return proc.returncode, parsed
    except Exception as exc:
        return 1, {"ok": False, "error": str(exc)}


def main() -> int:
    result = {"ok": True, "synced_at": time.time(), "import": None, "sync": None}
    # R2.7（2026-08-31）：import 身份收口——sync 裸 import 需注入 XTMEM_ACTOR=zcode
    # BF-5（2026-09-01）：按 returncode 判成败 + 空输出当 no-op（import/sync 都适用）。
    rc, imp = _run(["import"], {"XTMEM_ACTOR": "zcode"})
    if rc != 0:
        return print(json.dumps({"ok": False, "error": "import failed (rc=%s): %s" % (rc, imp)}, ensure_ascii=False)), 1
    result["import"] = imp or {"ok": True, "skipped": "empty-input"}
    rc2, sync = _run(["sync", "--side", "hermes"])
    if rc2 != 0:
        return print(json.dumps({"ok": False, "error": "sync failed (rc=%s): %s" % (rc2, sync)}, ensure_ascii=False)), 1
    result["sync"] = sync or {"ok": True, "skipped": "no-change"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
