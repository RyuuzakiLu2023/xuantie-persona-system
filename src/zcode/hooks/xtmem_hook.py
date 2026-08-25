# -*- coding: utf-8 -*-
"""xtmem_hook.py — zcode hooks（v2.5）：UserPromptSubmit 检索注入 / Stop 观察写入。

zcode 调用：python xtmem_hook.py <Event>；事件数据经 stdin JSON（容错：解析失败则按无载荷处理）。
输出：严格 JSON（仅 additionalContext 等允许键）；无结果时输出 {"additionalContext": ""}（空注入）。
停用：把 hooks/hooks.json 改为 hooks.json.disabled，或在插件设置里关闭。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import io
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

PLUGIN_ROOT = os.environ.get("ZCODE_PLUGIN_ROOT", os.environ.get("XTMEM_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent)))
PYTHON = os.environ.get("XTMEM_PYTHON", sys.executable)
XTMEM_PY = os.environ.get(
    "XTMEM_SCRIPT",
    str(Path(PLUGIN_ROOT).parent / "mimox-data" / "xtmem" / "xtmem.py"),
)
if not os.path.exists(XTMEM_PY):
    cand = Path(PLUGIN_ROOT).parents[1] / "mimox-data" / "xtmem" / "xtmem.py"
    if cand.exists():
        XTMEM_PY = str(cand)


def read_payload():
    try:
        data = sys.stdin.buffer.read().decode('utf-8', 'replace')
        if data.strip():
            return json.loads(data)
    except Exception:
        pass
    return {}


def run_xtmem(argv):
    try:
        proc = subprocess.run(
            [PYTHON, XTMEM_PY, *argv], capture_output=True, text=True,
            encoding="utf-8", timeout=45, check=False,
        )
        out = (proc.stdout or "").strip()
        try:
            return json.loads(out) if out else {}
        except Exception:
            return {}
    except Exception:
        return {}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "Unknown"
    payload = read_payload()
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or payload.get("userPrompt") or payload.get("text") or "").strip()
        if not prompt:
            print('{"additionalContext": ""}')
            return 0
        # 分角色注入：只注入 zcode 本域，不 cross；跨角色需 MCP 工具显式传 params
        r = run_xtmem(["recall", "--query", prompt, "--top", "5", "--namespace", "zcode", "--as", "zcode"])
        items = (r.get("items") or []) if r.get("ok") else []
        if not items:
            print('{"additionalContext": ""}')
            return 0
        lines = ["[XTMEM 记忆 %d/%d <%s>] %s" % (i + 1, len(items), it.get("namespace", ""), it.get("content", "")) for i, it in enumerate(items)]
        print(json.dumps({"additionalContext": "\n".join(lines)}, ensure_ascii=False))
        return 0
    if event == "Stop":
        resp = str(payload.get("response") or payload.get("content") or "").strip()
        if len(resp) >= 20:
            run_xtmem(["store", "--text", resp[:2000], "--agent", "zcode", "--importance", "0.4", "--namespace", "zcode"])
        print('{"additionalContext": ""}')
        return 0
    print('{"additionalContext": ""}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
