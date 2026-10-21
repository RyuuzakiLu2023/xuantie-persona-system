# -*- coding: utf-8 -*-
# ================================================================
# xtmem_plugin_smoke.py · ZCode 接头冒烟（不依赖真实宿主）
#
# 这是什么：把 mcp/xtmem_mcp.py 与 hooks/xtmem_hook.py 当命令行程序跑一遍，
#   验证 MCP 握手 / 八工具清单 / store→recall 一次调用，以及两个 hook 的输出形状。
#
# 技术栈：标准库 subprocess。前置：已安装 XTMEM CLI（`python -m xtmem`）。
#
# 数据保护：全程用 tempfile 目录里的独立库（XTMEM_DB_PATH 指向 tmp），
#   绝不写真实库、不读任何私有路径；库不存在时由 CLI 自行初始化。
#
# 铁律：
# 1. 身份用合成值（role-a），不写死任何真实角色名。
# 2. 超时给"秒级正常、慢机留余量"的 60s，不用 300s 那种掩盖卡死的长值。
# 3. CLI 缺失必须非零退出并说明原因——静默 SKIP 会让"没测"看起来像"通过"。
#
# 跑法：python src/zcode/tests/xtmem_plugin_smoke.py
# ================================================================
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
ACTOR = "role-a"

CHECKS = {"mcp_init": False, "mcp_tools": False, "mcp_call": False,
          "hook_inject": False, "hook_stop": False}


def _env(db_path: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "XTMEM_ACTOR": ACTOR,
        "XTMEM_DB_PATH": str(db_path),
        "XTMEM_PYTHON": PYTHON,
        "XTMEM_TIMEOUT_S": "45",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    env.pop("XTMEM_SCRIPT", None)
    return env


def cli_available() -> bool:
    try:
        return subprocess.run([PYTHON, "-m", "xtmem", "--help"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def mcp_roundtrip(env: dict, lines: list) -> list:
    proc = subprocess.run([PYTHON, str(ROOT / "mcp" / "xtmem_mcp.py")],
                          input=("\n".join(lines) + "\n").encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise RuntimeError("mcp exited %s: %s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace")[-1000:]))
    out = proc.stdout.decode("utf-8").strip()
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def hook_run(env: dict, event: str, payload: dict) -> dict:
    proc = subprocess.run([PYTHON, str(ROOT / "hooks" / "xtmem_hook.py"), event],
                          input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    return json.loads(proc.stdout.decode("utf-8").strip() or "{}")


def main() -> int:
    if not cli_available():
        print("FAIL: xtmem CLI not installed (`python -m xtmem --help` failed); "
              "install with `pip install -e .` first.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="xtmem-zcode-") as td:
        db = Path(td) / "vector_store.db"
        env = _env(db)

        rs = mcp_roundtrip(env, [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
            '{"name":"xtmem_store","arguments":{"text":"role-a smoke fact for zcode adapter",'
            '"importance":0.8,"tags":"smoke"}}}',
            '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":'
            '{"name":"xtmem_recall","arguments":{"query":"smoke fact","top":3}}}',
        ])
        CHECKS["mcp_init"] = rs[0].get("result", {}).get("serverInfo", {}).get("name") == "xtmem"
        tools = rs[1].get("result", {}).get("tools") or []
        CHECKS["mcp_tools"] = {t["name"] for t in tools} == {
            "xtmem_recall", "xtmem_store", "xtmem_lookup_cross", "xtmem_lookup_id",
            "xtmem_msg_recent", "xtmem_msg_inbox", "xtmem_agents", "xtmem_forget",
        }
        stored = json.loads(rs[2]["result"]["content"][0]["text"])
        recalled = json.loads(rs[3]["result"]["content"][0]["text"])
        CHECKS["mcp_call"] = (bool(stored.get("ok")) and bool(recalled.get("ok"))
                              and any(it.get("id") == stored.get("id")
                                      for it in recalled.get("items", [])))

        h1 = hook_run(env, "UserPromptSubmit", {"prompt": "smoke fact"})
        CHECKS["hook_inject"] = "[m:mem-" in h1.get("additionalContext", "")

        h2 = hook_run(env, "Stop", {"response": "决定了：smoke 观察写入一条，长度足够通过门控。"})
        CHECKS["hook_stop"] = h2 == {"additionalContext": ""}

    ok = all(CHECKS.values())
    print(json.dumps({"ok": ok, "checks": CHECKS}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
