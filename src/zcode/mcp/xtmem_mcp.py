# -*- coding: utf-8 -*-
"""xtmem_mcp.py — XTMEM zcode MCP 服务器（Python stdlib 最小 stdio MCP 子集，v2.4）。

协议：JSON-RPC 2.0（newline-delimited），MCP 2024-11-05 子集：
  initialize / notifications/initialized / tools/list / tools/call。
工具：xtmem_recall / xtmem_store / xtmem_lookup_cross —— 全部通过 subprocess 调 xtmem.py（共享记忆库）。
零依赖；需要环境：XTMEM_PLUGIN_ROOT（插件根，用于定位 xtmem.py）或 XTMEM_SCRIPT 显式指定。
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

PLUGIN_ROOT = os.environ.get("XTMEM_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1]))
PYTHON = os.environ.get("XTMEM_PYTHON", sys.executable)
XTMEM_PY = os.environ.get(
    "XTMEM_SCRIPT",
    str(Path(PLUGIN_ROOT).parent / "mimox-data" / "xtmem" / "xtmem.py"),
)
# 2026-08-25 修复（病A）：相对推导在 zcode 插件缓存目录下必然落空——
# 增加绝对路径候选（生产引擎固定位置），三条候选按序探测。
if not os.path.exists(XTMEM_PY):
    for _cand in (
        Path(PLUGIN_ROOT).parents[1] / "mimox-data" / "xtmem" / "xtmem.py",
        Path(os.environ.get("XTMEM_SCRIPT", "xtmem/xtmem.py")),
    ):
        if _cand.exists():
            XTMEM_PY = str(_cand)
            break

TOOL_SCHEMAS = [
    {
        "name": "xtmem_recall",
        "description": "XTMEM 共享记忆语义检索（跨角色共享库；不传 cross 只查当前域）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询"},
                "top": {"type": "number", "description": "条数，默认 6"},
                "namespace": {"type": "string"},
                "project": {"type": "string"},
                "cross": {"type": "boolean", "description": "true=跨所有角色"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "xtmem_store",
        "description": "写一条事实进 XTMEM 共享记忆库（引擎管线：去重/演化/FTS）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "一句话事实"},
                "agent": {"type": "string", "default": "zcode"},
                "project": {"type": "string"},
                "importance": {"type": "number"},
                "namespace": {"type": "string"},
                "tags": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "xtmem_lookup_cross",
        "description": "XTMEM 全库直查（关键词 LIKE，按重要度排；跨所有角色）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，空=列出最近"},
                "top": {"type": "number", "default": 50},
            },
        },
    },
]


def run_xtmem(argv):
    try:
        proc = subprocess.run(
            [PYTHON, XTMEM_PY, *argv],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False,
        )
        out = (proc.stdout or "").strip()
        try:
            return json.loads(out) if out else {"ok": False, "error": "empty output"}
        except Exception:
            return {"ok": False, "error": "bad output: " + out[:300]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def handle_call(name, args: dict):
    args = args or {}
    text = str(args.get("text", "")).strip() if name == "xtmem_store" else str(args.get("query", "")).strip()
    if name == "xtmem_recall":
        argv = ["recall", "--query", text, "--top", str(int(args.get("top", 6)))]
        argv += ["--namespace", str(args.get("namespace", "zcode")), "--as", str(args.get("namespace", "zcode"))]
        if args.get("namespace"):
            argv += ["--namespace", str(args["namespace"])]
        if args.get("project"):
            argv += ["--project", str(args["project"])]
        if args.get("cross"):
            argv += ["--cross"]
        return run_xtmem(argv)
    if name == "xtmem_store":
        if not text:
            return {"ok": False, "error": "text is required"}
        argv = ["store", "--text", text, "--agent", str(args.get("agent", "zcode"))]
        if args.get("project"):
            argv += ["--project", str(args["project"])]
        if args.get("importance") is not None:
            argv += ["--importance", str(args["importance"])]
        if args.get("tags"):
            argv += ["--tags", str(args["tags"])]
        if args.get("namespace"):
            argv += ["--namespace", str(args["namespace"])]
        return run_xtmem(argv)
    if name == "xtmem_lookup_cross":
        argv = ["lookup", "--cross", "--top", str(int(args.get("top", 50)))]
        if text:
            argv += ["--query", text]
        return run_xtmem(argv)
    return {"ok": False, "error": f"unknown tool: {name}"}


def main() -> int:
    for raw in sys.stdin.buffer:
        line = raw.decode('utf-8', 'replace').strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            resp = {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xtmem", "version": "0.1.0"},
                },
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOL_SCHEMAS}}
        elif method == "tools/call":
            params = msg.get("params") or {}
            payload = handle_call(params.get("name", ""), params.get("arguments", {}))
            resp = {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                    "isError": bool(not payload.get("ok", False)),
                },
            }
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}, "error": {"code": -32601, "message": "method not found: " + str(method)}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
