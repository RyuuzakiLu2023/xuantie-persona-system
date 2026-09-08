# -*- coding: utf-8 -*-
"""xtmem_mcp.py — XTMEM zcode MCP 服务器（Python stdlib 最小 stdio MCP 子集，v2.5）。

协议：JSON-RPC 2.0（newline-delimited），MCP 2024-11-05 子集：
  initialize / notifications/initialized / tools/list / tools/call。
工具（八工具）：记忆 recall / store / lookup_cross / lookup_id，信箱 msg_recent / msg_inbox，
杂项 agents / forget —— 全部通过 subprocess 调 xtmem.py（共享记忆库）。
⚠️ msg send【不】暴露为工具：对外发起对话是授权闸（龙崎纪律：未授权不发信），只留 CLI 人工层；
msg_inbox（置读即消费）与 forget（删除）破坏性操作强制 confirm=true 才执行。
零依赖；需要环境：XTMEM_PLUGIN_ROOT（插件根，用于定位 xtmem.py）或 XTMEM_SCRIPT 显式指定。

*更新：2026-09-05 · v2.5 信箱/纠错全面 MCP 化：新增 msg_recent/msg_inbox/lookup_id/agents/forget*
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
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
        Path(os.environ.get("XTMEM_SCRIPT", "")) if os.environ.get("XTMEM_SCRIPT") else None,
    ):
        if _cand and _cand.exists():
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
    {
        "name": "xtmem_msg_recent",
        "description": "XTMEM 信箱：查看最近留言（只读，不改已读状态）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "number", "default": 20, "description": "条数"},
            },
        },
    },
    {
        "name": "xtmem_msg_inbox",
        "description": "XTMEM 信箱：领取并置读本域（zcode）未读留言，返回正文。⚠️ 调用即消费：置读不可逆，请先逐条处理完再调；confirm 必须传 true，否则报错不执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "必须 true：确认未读已处理完"},
            },
            "required": ["confirm"],
        },
    },
    {
        "name": "xtmem_lookup_id",
        "description": "XTMEM 按记忆 ID 精确查询（走索引；lookup_cross 只搜正文不搜 id 字段，拿 ID 查只有这条路）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "mem-xxxxxxxx"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "xtmem_agents",
        "description": "XTMEM 列出全部已注册角色。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "xtmem_forget",
        "description": "XTMEM 按 ID 删除（归档）一条记忆。⚠️ 破坏性：confirm 必须传 true；引擎侧身份校验，只删得动 zcode 域。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "mem-xxxxxxxx"},
                "confirm": {"type": "boolean", "description": "必须 true"},
            },
            "required": ["id", "confirm"],
        },
    },
]


def run_xtmem(argv):
    try:
        env = dict(os.environ)
        env["XTMEM_ACTOR"] = "zcode"   # P0-1：身份固定（CLI 端也会强制该身份）
        proc = subprocess.run(
            [PYTHON, XTMEM_PY, *argv],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False, env=env,
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


# ── B方案 S2 接入（2026-09-06，zcode 自理）：常驻 serve 客户端 ──────────────
# 病灶：每条 recall/store 起短命子进程全量水化 2700+ 条（2~3s）。接 serve 后
# 进程内书架复用，秒级。⚠️ 身份模型=per-actor serve（serve spawn 时 XTMEM_ACTOR
# 定死，_resolve_actor 读进程 env 非 per-request）——故 zcode 用【专属端口 8766 +
# 专属 PID 文件】起自己的 serve，绝不共用 dsh 的 8765（否则 as_agent 冲突或串号）。
# 降级：连不上→懒启动一次→仍不行回落 run_xtmem 子进程（功能不丢，I3 同族）。
_SERVE_HOST = "127.0.0.1"
_SERVE_PORT = int(os.environ.get("XTMEM_SERVE_PORT", "8766") or "8766")
_SERVE_PID = os.environ.get(
    "XTMEM_SERVE_PID",
    str(Path(XTMEM_PY).resolve().parent / ".xtmem_serve_zcode.pid"),
)
_serve_deadline = 0.0  # 连不上后的冷却点（防每条命令都重试 spawn 拖慢）


def _serve_send(req: dict, timeout: float = 8.0):
    """向 serve 发一行 JSON、收一行响应。任何异常返回 None（触发降级）。"""
    try:
        s = socket.create_connection((_SERVE_HOST, _SERVE_PORT), timeout=timeout)
        try:
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            s.settimeout(timeout)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.decode("utf-8", "replace").strip()
            return json.loads(line) if line else None
        finally:
            s.close()
    except Exception:
        return None


def _serve_alive(timeout: float = 1.0) -> bool:
    """TCP 握手即就绪（serve 在 get_builder() 水化完成后才 listen，连上=书架已热）。"""
    try:
        s = socket.create_connection((_SERVE_HOST, _SERVE_PORT), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _serve_spawn_once() -> bool:
    """懒启动 zcode 专属 serve（detached，XTMEM_ACTOR=zcode 定死）。等就绪最多 25s。"""
    global _serve_deadline
    try:
        env = dict(os.environ)
        env["XTMEM_ACTOR"] = "zcode"          # per-actor 身份：本 serve 只服务 zcode
        env["XTMEM_SERVE_PID"] = _SERVE_PID    # 专属 PID 文件，不碰 dsh 的
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [PYTHON, XTMEM_PY, "serve", "--host", _SERVE_HOST, "--port", str(_SERVE_PORT)],
            cwd=str(Path(XTMEM_PY).resolve().parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=flags,
        )
    except Exception:
        _serve_deadline = time.time() + 60
        return False
    for _ in range(50):
        time.sleep(0.5)
        if _serve_alive():
            return True
    _serve_deadline = time.time() + 30  # 起不来，冷却 30s 再试（期间走子进程）
    return False


def _dispatch(req: dict, argv: list):
    """优先常驻 serve（省水化）；连不上→懒启动一次→再不行回落子进程。"""
    global _serve_deadline
    if time.time() >= _serve_deadline:
        r = _serve_send(req)
        if r is not None:
            return r
        if _serve_spawn_once():
            r = _serve_send(req)
            if r is not None:
                return r
    return run_xtmem(argv)


def handle_call(name, args: dict):
    args = args or {}
    text = str(args.get("text", "")).strip() if name == "xtmem_store" else str(args.get("query", "")).strip()
    actor = "zcode"   # P0-1：身份固定，外部参数不能决定 --as
    if name == "xtmem_recall":
        ns = str(args.get("namespace", actor))
        if not args.get("cross") and ns != actor:
            return {"ok": False, "error": "非 cross 召回只能查本域 zcode，跨角色请用 cross=true"}
        argv = ["recall", "--query", text, "--top", str(int(args.get("top", 6)))]
        argv += ["--namespace", ns, "--as", actor]
        req = {"cmd": "recall", "args": {"query": text, "top": int(args.get("top", 6)),
                                          "namespace": ns, "as_agent": actor, "cross": bool(args.get("cross"))}}
        if args.get("project"):
            argv += ["--project", str(args["project"])]
            req["args"]["project"] = str(args["project"])
        if args.get("cross"):
            argv += ["--cross"]
        return _dispatch(req, argv)
    if name == "xtmem_store":
        if not text:
            return {"ok": False, "error": "text is required"}
        ns = str(args.get("namespace", "zcode"))
        # Knowledge 豁免（2026-09-07，与 xtmem.py cmd_store _kx_exempt 同口径）：
        # ns=knowledge 共享域放行（CLI 侧不传 --visibility 默认 shared，豁免条件必然满足；
        # 真正的安全校验仍由引擎 _check_injection 兜底）。其余非本域照拒。
        if ns not in ("zcode", "knowledge"):
            return {"ok": False, "error": "本端记忆只能写入本域 zcode（自己记自己的）；knowledge 共享域已豁免可直接写"}
        argv = ["store", "--text", text, "--agent", str(args.get("agent", "zcode"))]
        req = {"cmd": "store", "args": {"text": text, "agent": str(args.get("agent", "zcode")),
                                         "namespace": ns, "as_agent": actor}}
        if args.get("project"):
            argv += ["--project", str(args["project"])]
            req["args"]["project"] = str(args["project"])
        if args.get("importance") is not None:
            argv += ["--importance", str(args["importance"])]
            req["args"]["importance"] = float(args["importance"])
        if args.get("tags"):
            argv += ["--tags", str(args["tags"])]
            req["args"]["tags"] = str(args["tags"])
        argv += ["--namespace", ns]
        return _dispatch(req, argv)
    if name == "xtmem_lookup_cross":
        argv = ["lookup", "--cross", "--top", str(int(args.get("top", 50))), "--as", "zcode"]
        req = {"cmd": "lookup", "args": {"cross": True, "top": int(args.get("top", 50)), "as_agent": "zcode"}}
        if text:
            argv += ["--query", text]
            req["args"]["query"] = text
        return _dispatch(req, argv)
    if name == "xtmem_msg_recent":
        return run_xtmem(["msg", "recent", "--top", str(int(args.get("top", 20)))])
    if name == "xtmem_msg_inbox":
        # ⚠️ 置读即消费不可逆——confirm 防手滑吞掉观察者还没处理的信
        if args.get("confirm") is not True:
            return {"ok": False, "error": "confirm=true required：置读会消费本域全部未读，请先逐条处理"}
        return run_xtmem(["msg", "inbox", "--as", actor])
    if name == "xtmem_lookup_id":
        mem_id = str(args.get("id", "")).strip()
        if not mem_id:
            return {"ok": False, "error": "id is required"}
        return _dispatch({"cmd": "lookup", "args": {"id": mem_id, "as_agent": actor}},
                         ["lookup", "--id", mem_id, "--as", actor])
    if name == "xtmem_agents":
        return run_xtmem(["agents"])
    if name == "xtmem_forget":
        # ⚠️ 删除属破坏性操作，同样强制显式确认；归属校验在引擎侧（--as 写死）
        if args.get("confirm") is not True:
            return {"ok": False, "error": "confirm=true required：删除记忆属破坏性操作"}
        mem_id = str(args.get("id", "")).strip()
        if not mem_id:
            return {"ok": False, "error": "id is required"}
        return run_xtmem(["forget", "--id", mem_id, "--as", actor])
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
