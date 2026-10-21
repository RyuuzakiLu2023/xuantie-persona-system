# -*- coding: utf-8 -*-
# ================================================================
# xtmem_mcp.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：XTMEM 的通用 stdio MCP 服务端（Python 标准库实现，零第三方依赖）。
#   把「已安装的 XTMEM CLI」暴露成 MCP 工具，任何支持 stdio MCP 的宿主
#   （ZCode / Claude / Codex / 自建 agent）都能接同一座记忆库。
#
# 技术栈：Python 3.9+ 标准库（json / os / socket / subprocess / sys / threading）。
# 依赖：外部只需一个已安装的 XTMEM CLI——默认 `python -m xtmem`；
#   显式覆盖用 XTMEM_SCRIPT=/abs/path/xtmem.py。不依赖私有目录、不 import 引擎。
#
# 结构：
#   配置区（只读环境变量：身份 / 库 / 解释器 / 超时）
#   → run_cli()（唯一子进程出口，身份与环境清洗都在这里）
#   → dispatch()（可选 serve 快路；鉴权拒绝【不】回退）
#   → TOOL_SCHEMAS + handle_call()
#   → main()（JSON-RPC 2.0 逐行协议，stdout 只出 JSON）
#
# 修改铁律：
# 1. stdout 只许 JSON-RPC 帧。日志/诊断一律写 stderr——stdout 混一行人话，
#    宿主按行解析整条连接就报废。
# 2. 身份只从 XTMEM_ACTOR 读，【不】从工具参数读。参数里的 namespace 只决定
#    「查哪个域 / 写哪个域」，绝不能变成 --as。
# 3. 子进程环境必须删掉管理员凭据（XTMEM_ADMIN* / XTMEM_SERVE_TOKEN）并固定
#    XTMEM_ACTOR——否则宿主里一个继承来的管理员变量就能让普通端提权。
# 4. serve 快路只在「传输层连不上」时回落 CLI；鉴权/协议拒绝是终态，【不】回退——
#    回退会把「身份不对」悄悄变成「看起来能用的旧路径」。
# 5. 破坏性工具（forget / msg_inbox）必须 confirm=true 才执行；归属校验交给引擎。
# 6. 不写死任何盘符/用户目录/角色名；默认值是通用值（actor=host）。
#
# 已知问题：MCP 子集未实现 resources / prompts（本插件不需要）；serve 快路需要
#   能解析出端口（XTMEM_SERVE_PORT 或引擎 endpoint_config），否则自动走 CLI。
#
# 更新记录：
# *更新：2026-10-22 · 公开接头首批：统一 `python -m xtmem` 入口；actor/db/python 全可配；
#   去掉私有路径与固定角色；serve 改为可选且鉴权拒绝不回退；stdout 字节级收口。*
# ================================================================
"""xtmem_mcp.py — XTMEM 通用 stdio MCP 服务端（JSON-RPC 2.0，标准库实现）。

协议：newline-delimited JSON-RPC 2.0 子集：
  initialize / notifications/initialized / ping / tools/list / tools/call。

工具（八工具，不砍）：
  记忆  xtmem_recall / xtmem_store / xtmem_lookup_cross / xtmem_lookup_id
  信箱  xtmem_msg_recent / xtmem_msg_inbox（置读即消费，confirm 必填）
  杂项  xtmem_agents / xtmem_forget（破坏性，confirm 必填）

身份：只认环境变量 XTMEM_ACTOR（默认 "host"）。工具参数不能改身份。

环境变量（唯一配置来源）：
  XTMEM_ACTOR        本端身份（默认 host）。admin / owner / __admin__ 拒绝。
  XTMEM_PYTHON       CLI 解释器（默认当前解释器 sys.executable）
  XTMEM_SCRIPT       可选：xtmem.py 的绝对路径；不设则用 `python -m xtmem`
  XTMEM_DB_PATH      可选：显式库路径（测试副本 / 多库部署）
  XTMEM_TIMEOUT_S    单次 CLI 调用超时秒数（默认 55）
  XTMEM_SERVE_MODE   off（默认）| auto：auto 才尝试常驻 serve 快路
  XTMEM_SERVE_PORT   serve 端口；不设时尝试用引擎 endpoint_config 解析
  XTMEM_SERVE_PID    可选：serve PID 文件路径（默认库同目录、按身份分名）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVER_NAME = "xtmem"
SERVER_VERSION = "1.1.0.dev0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

MAX_OUTPUT_CHARS = 60_000      # 返回给宿主的文本上限
MAX_TEXT_CHARS = 20_000        # store 正文上限（Windows 命令行约 32K）
MAX_QUERY_CHARS = 2_000

ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# 与引擎 serve_protocol.validate_actor 同口径：admin/owner 是管理员级身份。
FORBIDDEN_ACTORS = {"admin", "owner", "__admin__"}
# 子进程绝不能继承的管理员/服务凭据：普通端拿到它们就能绕过身份闸。
STRIPPED_ENV = ("XTMEM_ADMIN", "XTMEM_ADMIN_TOKEN", "XTMEM_SERVE_TOKEN")


# ---------------------------------------------------------------
# stdout / stderr 收口
# ---------------------------------------------------------------
def log(msg: str) -> None:
    """日志只走 stderr——stdout 是协议通道，混一行就断连。"""
    try:
        sys.stderr.write("[xtmem-mcp] " + str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def write_frame(obj: dict) -> None:
    """按 UTF-8 字节写一帧 JSON-RPC。用 buffer 写，避免 Windows 控制台编码漂移。"""
    payload = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        out.write(payload)
        out.flush()
    else:  # pragma: no cover - 极端被替换的 stdout
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()


def fail_startup(msg: str) -> None:
    log("refusing to start: " + msg)
    raise SystemExit(2)


# ---------------------------------------------------------------
# 配置（只读环境变量，不使用任何私有默认路径）
# ---------------------------------------------------------------
def load_config() -> dict:
    actor = os.environ.get("XTMEM_ACTOR", "").strip() or "host"
    if not ACTOR_RE.fullmatch(actor) or actor.lower() in FORBIDDEN_ACTORS:
        fail_startup("XTMEM_ACTOR=%r is not allowed (letters/digits/_/-, max 64, no admin identities)." % actor)

    python = os.environ.get("XTMEM_PYTHON", "").strip() or sys.executable
    if not os.path.isfile(python):
        fail_startup("XTMEM_PYTHON=%r does not exist." % python)

    script = os.environ.get("XTMEM_SCRIPT", "").strip()
    if script and (not os.path.isabs(script) or not os.path.isfile(script)):
        fail_startup("XTMEM_SCRIPT=%r is not an existing absolute file path." % script)

    raw_timeout = os.environ.get("XTMEM_TIMEOUT_S", "").strip() or "55"
    try:
        timeout = float(raw_timeout)
        if not 0 < timeout <= 3600:
            raise ValueError
    except ValueError:
        fail_startup("XTMEM_TIMEOUT_S=%r must be seconds in (0, 3600]." % raw_timeout)

    mode = os.environ.get("XTMEM_SERVE_MODE", "off").strip().lower() or "off"
    if mode not in ("off", "auto"):
        fail_startup("XTMEM_SERVE_MODE=%r must be 'off' or 'auto'." % mode)

    db = os.environ.get("XTMEM_DB_PATH", "").strip()
    port_raw = os.environ.get("XTMEM_SERVE_PORT", "").strip()
    port = 0
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            fail_startup("XTMEM_SERVE_PORT=%r is not an integer." % port_raw)
        if not 0 < port < 65536:
            fail_startup("XTMEM_SERVE_PORT=%r is out of range." % port_raw)

    return {
        "actor": actor,
        "python": python,
        "script": script,
        "timeout": timeout,
        "serve_mode": mode,
        "serve_port": port,
        "db": db,
        "cwd": os.environ.get("XTMEM_CLI_CWD", "").strip(),
    }


CONFIG: dict = {}
_serve_lock = threading.Lock()
_serve_deadline = 0.0


# ---------------------------------------------------------------
# CLI 子进程（唯一出口）
# ---------------------------------------------------------------
# 宿主 profile 环境变量：一旦显式指定库，这些必须清掉。
# 为什么：CLI 的库解析顺序是 xtmem.yaml/config.yaml（按 home 找）→ XTMEM_DB_PATH
# → 默认库。只设 XTMEM_DB_PATH 而留下 HERMES_HOME，宿主 YAML 里的 db_path 会
# **压过** 环境变量，CLI 就悄悄写到另一个库去了（看着一切正常，数据落错地方）。
HOME_ENV_KEYS = ("XTMEM_HERMES_HOME", "HERMES_HOME", "MIMOX_HOME")


def child_env() -> dict:
    env = dict(os.environ)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    env["XTMEM_ACTOR"] = CONFIG["actor"]
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if CONFIG.get("db"):
        env["XTMEM_DB_PATH"] = CONFIG["db"]
        # ⚠️【勿删】显式库 + 残留 home = 库被宿主 YAML 劫持。
        for key in HOME_ENV_KEYS:
            env.pop(key, None)
    return env


def cli_argv(argv: list) -> list:
    """CLI 启动命令：优先显式 XTMEM_SCRIPT，否则 `python -m xtmem`。

    ⚠️【不】用 `python <console-script-exe>`：pip 生成的 xtmem.exe 是二进制启动器，
    Python 解释器执行它会直接报错（那正是「用 python 执行 exe」的坑）。
    """
    if CONFIG.get("script"):
        return [CONFIG["python"], CONFIG["script"], *argv]
    return [CONFIG["python"], "-m", "xtmem", *argv]


def cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated %d chars]" % (len(text) - limit)


def run_cli(argv: list) -> dict:
    """跑一次 CLI，返回解析后的 JSON；任何异常都收敛成 ok=false。"""
    cmd = cli_argv(argv)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CONFIG["timeout"],
            env=child_env(),
            check=False,
            cwd=CONFIG.get("cwd") or None,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        log("%s timeout after %.0fs" % (argv[0], CONFIG["timeout"]))
        return {"ok": False, "error": "xtmem CLI timed out after %.0f s (XTMEM_TIMEOUT_S)." % CONFIG["timeout"]}
    except OSError as exc:
        return {"ok": False, "error": "failed to start xtmem CLI: %s" % exc}

    # ⚠️ 必须按字节解 utf-8（CLI 已 reconfigure 成 utf-8），不能用 text=True 让
    #    Windows 本地代码页参与——中文会变 ?，JSON 也可能被 BOM 污染。
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if not out:
        tail = err[-1500:] if err else "(no stderr)"
        return {"ok": False, "error": "xtmem CLI produced no stdout (exit=%s): %s" % (proc.returncode, tail)}
    try:
        parsed = json.loads(out)
    except ValueError:
        return {"ok": False, "error": "xtmem CLI returned non-JSON output: " + cap(out[-1500:], 1500)}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "xtmem CLI returned unexpected JSON type"}
    if err:
        log("%s stderr tail: %s" % (argv[0], err[-300:]))
    log("%s ok=%s in %.2fs" % (argv[0], parsed.get("ok"), time.monotonic() - started))
    return parsed


# ---------------------------------------------------------------
# 可选 serve 快路（默认 off）
# ---------------------------------------------------------------
def _serve_module():
    """延迟 import 引擎的 serve 协议模块；拿不到就返回 None（自动走 CLI）。"""
    try:
        from memory import serve_protocol  # type: ignore
        return serve_protocol
    except Exception as exc:
        log("serve protocol unavailable: %s" % exc)
        return None


def _serve_port() -> int:
    if CONFIG.get("serve_port"):
        return int(CONFIG["serve_port"])
    try:
        from memory import endpoint_config  # type: ignore
        return int(endpoint_config.resolve_endpoint(CONFIG["actor"]).port)
    except Exception:
        return 0


def _serve_db() -> str:
    if CONFIG.get("db"):
        return CONFIG["db"]
    try:
        from memory import endpoint_config  # type: ignore
        return str(endpoint_config.resolve_db_path()["db_path"])
    except Exception:
        return ""


def _serve_send(protocol, port: int, req: dict, timeout: float = 8.0):
    db = _serve_db()
    if not db:
        return None
    try:
        token = protocol.actor_token(CONFIG["actor"], db, create=True)
        return protocol.send_request("127.0.0.1", port, req, token, timeout)
    except Exception as exc:
        log("serve send failed: %s" % exc)
        return None


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _serve_pid_path() -> str:
    explicit = os.environ.get("XTMEM_SERVE_PID", "").strip()
    if explicit:
        return explicit
    db = _serve_db()
    base = Path(db).resolve().parent if db else Path.home()
    return str(base / (".xtmem_serve_%s.pid" % CONFIG["actor"]))


def _serve_spawn_once(protocol, port: int) -> bool:
    """懒启动本端专属 serve（身份定死、凭据显式注入、库路径显式传递）。"""
    global _serve_deadline
    pid_path = Path(_serve_pid_path())
    try:
        if pid_path.exists():
            raw = pid_path.read_text(encoding="utf-8").strip()
            if raw.isdigit() and _pid_alive(int(raw)):
                _serve_deadline = time.time() + 10
                return False
    except Exception:
        pass

    db = _serve_db()
    if not db:
        _serve_deadline = time.time() + 60
        return False
    try:
        token = protocol.actor_token(CONFIG["actor"], db, create=True)
        env = child_env()
        env["XTMEM_ACTOR"] = CONFIG["actor"]
        env["XTMEM_SERVE_TOKEN"] = token
        env["XTMEM_DB_PATH"] = db
        env["XTMEM_SERVE_PID"] = str(pid_path)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            cli_argv(["serve", "--host", "127.0.0.1", "--port", str(port)]),
            cwd=CONFIG.get("cwd") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
        )
    except Exception as exc:
        log("serve spawn failed: %s" % exc)
        _serve_deadline = time.time() + 60
        return False

    for _ in range(50):
        time.sleep(0.5)
        try:
            probe = protocol.send_request(
                "127.0.0.1", port, {"cmd": "hello", "args": {}},
                protocol.actor_token(CONFIG["actor"], db, create=True), 6.0)
        except Exception:
            probe = None
        if isinstance(probe, dict) and probe.get("ok") is True:
            return True
    _serve_deadline = time.time() + 30
    return False


def dispatch(req: dict, argv: list) -> dict:
    """serve 优先（仅 auto 模式）；传输失败回落 CLI；鉴权拒绝是终态。"""
    global _serve_deadline
    if CONFIG.get("serve_mode") != "auto":
        return run_cli(argv)
    protocol = _serve_module()
    port = _serve_port()
    if protocol is None or not port:
        return run_cli(argv)
    if time.time() >= _serve_deadline:
        with _serve_lock:
            result = _serve_send(protocol, port, req)
            if result is not None:
                try:
                    if protocol.is_security_rejection(result):
                        # 身份/凭据不对：绝不能悄悄回退到 CLI 假装成功。
                        return result
                except Exception:
                    pass
                return result
            if _serve_spawn_once(protocol, port):
                result = _serve_send(protocol, port, req)
                if result is not None:
                    return result
    return run_cli(argv)


# 兼容别名：老调用方/测试用的是 run_xtmem(argv)；语义与 run_cli 完全一致。
run_xtmem = run_cli


# ---------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "xtmem_recall",
        "description": "XTMEM 共享记忆语义检索。默认只查当前身份域；cross=true 跨全部可见域。身份由宿主配置固定，不能由参数改变。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询"},
                "top": {"type": "number", "description": "条数，默认 6"},
                "namespace": {"type": "string", "description": "查询域；缺省=当前身份"},
                "project": {"type": "string", "description": "限定项目（可选）"},
                "cross": {"type": "boolean", "description": "true=跨域检索"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "xtmem_store",
        "description": "写一条事实进 XTMEM 共享记忆库（引擎管线：去重/演化/FTS）。只能写本域；knowledge 共享域在 visibility=shared 时放行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记住的事实"},
                "project": {"type": "string", "description": "项目名（可选）"},
                "importance": {"type": "number", "description": "重要度 0~1"},
                "namespace": {"type": "string", "description": "缺省=当前身份"},
                "tags": {"type": "string", "description": "逗号分隔标签"},
                "tier": {"type": "string", "description": "raw | refined（默认 refined）"},
                "visibility": {"type": "string", "description": "shared | private | shared-with:..."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "xtmem_lookup_cross",
        "description": "XTMEM 全库关键词直查（按重要度排，跨全部可见域）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词；空=列出最近"},
                "top": {"type": "number", "description": "条数，默认 50"},
            },
        },
    },
    {
        "name": "xtmem_lookup_id",
        "description": "XTMEM 按记忆 ID 精确查询（走索引；仍校验当前身份可见性）。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "mem-xxxxxxxx"}},
            "required": ["id"],
        },
    },
    {
        "name": "xtmem_msg_recent",
        "description": "XTMEM 信箱：查看最近留言（只读，不改已读状态）。",
        "inputSchema": {
            "type": "object",
            "properties": {"top": {"type": "number", "description": "条数，默认 20"}},
        },
    },
    {
        "name": "xtmem_msg_inbox",
        "description": "XTMEM 信箱：领取并置读本域未读留言。调用即消费，置读不可逆；confirm 必须传 true。",
        "inputSchema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean", "description": "必须 true"}},
            "required": ["confirm"],
        },
    },
    {
        "name": "xtmem_agents",
        "description": "XTMEM 列出已注册角色。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "xtmem_forget",
        "description": "XTMEM 按 ID 归档一条记忆。破坏性操作，confirm 必须传 true；归属校验在引擎侧。",
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


def _txt(args: dict, key: str, limit: int) -> str:
    value = args.get(key, "")
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % key)
    value = value.strip()
    if len(value) > limit:
        raise ValueError("%s is too long" % key)
    return value


def _num(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    if key not in args or args[key] is None:
        return default
    if isinstance(args[key], bool) or not isinstance(args[key], (int, float)):
        raise ValueError("%s must be a number" % key)
    value = int(args[key])
    if not lo <= value <= hi:
        raise ValueError("%s must be in %d..%d" % (key, lo, hi))
    return value


def handle_call(name: str, args: dict) -> dict:
    """工具分发：身份固定 CONFIG['actor']，参数只决定域与过滤条件。"""
    args = args or {}
    actor = CONFIG["actor"]
    try:
        if name == "xtmem_recall":
            query = _txt(args, "query", MAX_QUERY_CHARS)
            if not query:
                raise ValueError("query is required")
            top = _num(args, "top", 6, 1, 200)
            namespace = str(args.get("namespace") or actor).strip() or actor
            if not args.get("cross") and namespace != actor:
                return {"ok": False, "error": "非 cross 召回只能查本域 %s；跨域请用 cross=true" % actor}
            argv = ["recall", "--query", query, "--top", str(top), "--namespace", namespace, "--as", actor]
            req = {"cmd": "recall", "args": {"query": query, "top": top,
                                             "namespace": namespace, "as_agent": actor,
                                             "cross": bool(args.get("cross"))}}
            if args.get("project"):
                argv += ["--project", str(args["project"])]
                req["args"]["project"] = str(args["project"])
            if args.get("cross"):
                argv.append("--cross")
            return dispatch(req, argv)

        if name == "xtmem_store":
            text = _txt(args, "text", MAX_TEXT_CHARS)
            if not text:
                raise ValueError("text is required")
            namespace = str(args.get("namespace") or actor).strip() or actor
            visibility = str(args.get("visibility") or "shared").strip() or "shared"
            if namespace != actor and not (namespace == "knowledge" and visibility == "shared"):
                return {"ok": False,
                        "error": "本端只能写本域 %s（knowledge + shared 例外）" % actor}
            argv = ["store", "--text", text, "--agent", actor, "--namespace", namespace,
                    "--visibility", visibility]
            # ⚠️ store 没有 --as 参数（身份只认环境变量 XTMEM_ACTOR，见 CLI 的 cmd_store）。
            #    给它传 --as 会被 argparse 判成"unrecognized arguments"直接退出码 2。
            req = {"cmd": "store", "args": {"text": text, "agent": actor,
                                            "namespace": namespace, "visibility": visibility,
                                            "as_agent": actor}}
            if args.get("project"):
                argv += ["--project", str(args["project"])]
                req["args"]["project"] = str(args["project"])
            if args.get("importance") is not None:
                importance = float(args["importance"])
                if not 0.0 <= importance <= 1.0:
                    raise ValueError("importance must be in 0..1")
                argv += ["--importance", str(importance)]
                req["args"]["importance"] = importance
            if args.get("tags"):
                argv += ["--tags", str(args["tags"])]
                req["args"]["tags"] = str(args["tags"])
            if args.get("tier"):
                tier = str(args["tier"])
                if tier not in ("raw", "refined"):
                    raise ValueError("tier must be raw or refined")
                argv += ["--tier", tier]
                req["args"]["tier"] = tier
            return dispatch(req, argv)

        if name == "xtmem_lookup_cross":
            query = _txt(args, "query", MAX_QUERY_CHARS) if args.get("query") else ""
            top = _num(args, "top", 50, 1, 200)
            argv = ["lookup", "--cross", "--top", str(top), "--as", actor]
            req = {"cmd": "lookup", "args": {"cross": True, "top": top, "as_agent": actor}}
            if query:
                argv += ["--query", query]
                req["args"]["query"] = query
            return dispatch(req, argv)

        if name == "xtmem_lookup_id":
            mem_id = _txt(args, "id", 128)
            if not mem_id:
                raise ValueError("id is required")
            return dispatch({"cmd": "lookup", "args": {"id": mem_id, "as_agent": actor}},
                            ["lookup", "--id", mem_id, "--as", actor])

        if name == "xtmem_msg_recent":
            top = _num(args, "top", 20, 1, 200)
            return run_cli(["msg", "recent", "--top", str(top), "--as", actor])

        if name == "xtmem_msg_inbox":
            if args.get("confirm") is not True:
                return {"ok": False, "error": "confirm=true required：置读会消费本域全部未读"}
            return run_cli(["msg", "inbox", "--as", actor])

        if name == "xtmem_agents":
            return run_cli(["agents"])

        if name == "xtmem_forget":
            if args.get("confirm") is not True:
                return {"ok": False, "error": "confirm=true required：删除属破坏性操作"}
            mem_id = _txt(args, "id", 128)
            if not mem_id:
                raise ValueError("id is required")
            return run_cli(["forget", "--id", mem_id, "--as", actor, "--confirm"])

        return {"ok": False, "error": "unknown tool: " + str(name)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        log("tool %s failed:\n%s" % (name, __import__("traceback").format_exc()))
        return {"ok": False, "error": "工具执行失败，详见宿主 stderr"}


# ---------------------------------------------------------------
# JSON-RPC 主循环
# ---------------------------------------------------------------
def handle_message(msg: dict):
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": version,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOL_SCHEMAS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        payload = handle_call(str(params.get("name", "")), params.get("arguments") or {})
        text = json.dumps(payload, ensure_ascii=False, default=str)
        is_error = bool(not payload.get("ok", False))
        if len(text) > MAX_OUTPUT_CHARS:
            # ⚠️ 超限不能"截一半 JSON 当成功"——宿主拿到的是坏 JSON 却 isError=false，
            #    等于假绿。这里改成显式失败信封：协议仍合法（一个完整 JSON 对象），
            #    isError=true，并附可读的部分正文供排查。
            keep = max(0, MAX_OUTPUT_CHARS - 512)
            truncated = {
                "ok": False,
                "state": "response_truncated",
                "error": "response exceeded %d chars; raise the query limit or narrow the request"
                         % MAX_OUTPUT_CHARS,
                "original_chars": len(text),
                "partial_text": text[:keep],
            }
            text = json.dumps(truncated, ensure_ascii=False, default=str)
            is_error = True
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}],
                           "isError": is_error}}
    if method and str(method).startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "method not found: " + str(method)}}


def main() -> int:
    global CONFIG
    CONFIG = load_config()
    log("actor=%s mode=%s script=%s" % (
        CONFIG["actor"], CONFIG["serve_mode"], CONFIG["script"] or "<python -m xtmem>"))
    # 逐行读 stdin 字节流：显式 utf-8 + replace，坏字节不会把整条连接打死。
    # ⚠️ 单线程串行处理：工具调用本身是短命子进程，并发只会让多库/多身份更难排查。
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log("dropping malformed JSON line (%d bytes)" % len(raw))
            continue
        if not isinstance(msg, dict):
            continue
        resp = handle_message(msg)
        if resp is not None:
            write_frame(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
