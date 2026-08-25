# -*- coding: utf-8 -*-
"""xtmem_plugin_smoke.py — zcode 插件（阶段 2.4/2.5）永久冒烟测试。

验证：MCP initialize/tools-list/tools-call + hooks(UserPromptSubmit 注入 / Stop 观察写入)。
数据保护：临时 DB 副本 + 临时 zcode 记忆根；全部写操作不碰生产库。
用法：python tests/xtmem_plugin_smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CANONICAL = os.environ.get("XTMEM_CANONICAL_DB", r"E:\2222\3333\mimox-data\.hermes\memory\vector_store.db")
CHECKS = {"mcp_init": False, "mcp_tools": False, "mcp_call": False,
          "hook_inject": False, "hook_stop": False}


def mcp_roundtrip(env, lines):
    proc = subprocess.run([PYTHON, str(ROOT / "mcp" / "xtmem_mcp.py")], input="\n".join(lines) + "\n",
                          capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
    out = (proc.stdout or "").strip().split("\n")
    return [json.loads(x) for x in out if x.strip().startswith("{")]


def hook_run(env, event, payload):
    proc = subprocess.run([PYTHON, str(ROOT / "hooks" / "xtmem_hook.py"), event],
                          input=json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {}


def main() -> int:
    if not os.path.exists(CANONICAL):
        print("SKIP: canonical DB missing:", CANONICAL)
        return 0
    with tempfile.TemporaryDirectory(prefix="xtmem-zcode-") as td:
        td = Path(td)
        db = td / "vector_store.db"
        shutil.copyfile(CANONICAL, db)
        env = {**os.environ, "XTMEM_DB_PATH": str(db), "XTMEM_ZCODE_MEMORIES": str(td / "zcode")}
        rs = mcp_roundtrip(env, [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"xtmem_lookup_cross","arguments":{"top":2}}}',
        ])
        CHECKS["mcp_init"] = bool(rs and rs[0].get("result", {}).get("serverInfo", {}).get("name") == "xtmem")
        tools = (rs[1].get("result", {}).get("tools") or []) if len(rs) > 1 else []
        CHECKS["mcp_tools"] = {t["name"] for t in tools} == {"xtmem_recall", "xtmem_store", "xtmem_lookup_cross"}
        call = (rs[2].get("result", {}) if len(rs) > 2 else {})
        CHECKS["mcp_call"] = bool(call.get("content")) and call.get("isError") is False
        # 先存一条可命中记忆（走 CLI 引擎管线）
        subprocess.run([PYTHON, "E:/2222/3333/mimox-data/xtmem/xtmem.py", "store",
                        "--text", "zcode冒烟：玄铁部署偏好=Ollama 11434 本地 bge-m3", "--agent",
                        "zcode", "--importance", "0.8", "--namespace", "zcode"],
                       capture_output=True, text=True, encoding="utf-8", timeout=180, env=env)
        h1 = hook_run(env, "UserPromptSubmit", {"prompt": "玄铁部署偏好"})
        CHECKS["hook_inject"] = bool(h1.get("additionalContext") and "XTMEM" in h1.get("additionalContext", ""))
        h2 = hook_run(env, "Stop", {"response": "这是Stop观察写入冒烟测试回复，长度足够二十字符以上用于验证。"})
        CHECKS["hook_stop"] = bool(h2.get("additionalContext") == "")
        all_ok = all(CHECKS.values())
        # 清理测试行（临时库无需）
        print(json.dumps({"ok": all_ok, "checks": CHECKS}, ensure_ascii=False))
        return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
