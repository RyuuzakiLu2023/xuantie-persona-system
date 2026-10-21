"""MIMOX REST 观测层 — FastAPI 服务

提供系统健康、记忆分轨、演化关系、调用统计等观测端点（⚠️ R6a 修正 2026-09-04：原文谎称“只读端点”——实际含 msgs/widgets/rooms/task-ack 等 7 处写路由，写点碰 chunks/events/messages/rooms/widgets 五表；鉴权面见 _require_auth）。
热路径（store/prefetch）不经过此服务，永远在进程内直接调用。
2026-10-12 O01：后台回程/轮询故障可追踪；线程传递关联上下文，日志只记固定字段。

启动方式：
    python -m memory.api_server --port 20000
    或在 agent_init.py 里后台线程启动
"""

import os
import sys
import json
import re
import time
import logging
import sqlite3
import subprocess
import socket as _socket
import threading
import hashlib
import uuid
from typing import Optional
from contextvars import copy_context
from .diagnostics import diagnosed, emit
# R02（2026-10-15）：终态判定统一走这个归一化函数——'TASK_STATE_COMPLETED' 必须能判成 COMPLETED，
# 别再各写各的 rsplit('.')（那对下划线枚举永不成立，会把同步终态答案漏掉）。
from .a2a_client import normalize_state, TERMINAL_STATES, FAILURE_STATES
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import JSONResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    logger.warning("FastAPI not installed. MIMOX REST API disabled.")

_start_time = time.time()


def _get_db_path():
    """获取 vector_store.db 的默认路径"""
    candidates = [
        os.environ.get("MIMOCODE_DB_PATH", ""),
        os.path.join(os.path.dirname(__file__), ".hermes", "memory", "vector_store.db"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return candidates[1]


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# ── R-02（2026-09-03）角色级 token 鉴权 ─────────────────────
# 凭证来源（优先级）：XTMEM_REST_TOKENS 内联 JSON > XTMEM_REST_AUTH_FILE 所指文件
# （默认 <MIMOX_HOME>/xtmem_rest_auth.json）> 旧版单 token XTMEM_REST_TOKEN（兼容，viewer=admin）。
# token → {"viewer": <角色ns|admin|owner>, "admin": bool}；服务端解析，禁止请求体自报身份。
_AUTH_TOKEN = os.environ.get("XTMEM_REST_TOKEN", "").strip()


def _default_auth_file() -> str:
    """与CLI签发使用同一用户目录，不从site-packages向上猜私人部署。"""
    from memory.public_runtime import home_file
    return home_file("xtmem_rest_auth.json")


def _parse_role_tokens(text: str) -> dict:
    """校验 + 解析 token 映射 JSON。文件/环境坏 JSON → 抛错（fail-closed，不裸启）。"""
    try:
        raw = json.loads(text) if text.strip() else {}
    except Exception as e:
        raise RuntimeError("XTMEM REST token 配置不是合法 JSON: %s" % e)
    out = {}
    for token, meta in (raw or {}).items():
        token = str(token).strip()
        if not token or not isinstance(meta, dict):
            continue
        viewer = str(meta.get("viewer") or "admin").strip()
        out[token] = {"viewer": viewer, "admin": bool(meta.get("admin")) or viewer == "admin"}
    return out


# token 文件热缓存：path -> (mtime_ns,size, tokens)。mtime 变化即重读——
# 新签 token 无需重启生效；常态请求零文件 IO（少开一次明文 secret 的读取面）。
_TOKEN_FILE_CACHE: dict = {}


def _load_role_tokens() -> dict:
    inline = os.environ.get("XTMEM_REST_TOKENS", "").strip()
    if inline:
        return _parse_role_tokens(inline)
    path = os.environ.get("XTMEM_REST_AUTH_FILE") or _default_auth_file()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    hit = _TOKEN_FILE_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            tokens = _parse_role_tokens(f.read())
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("_load_role_tokens: 读取 %s 失败: %r", path, e)
        return {}
    _TOKEN_FILE_CACHE[path] = (key, tokens)
    return tokens


def _allow_loopback_noauth() -> bool:
    return os.environ.get("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", "").strip() == "1"


def _active_tokens() -> dict:
    """生效 token 表：新角色配置优先；无则回退旧版单 token（兼容期）。"""
    role_tokens = _load_role_tokens()
    if role_tokens:
        return role_tokens
    if _AUTH_TOKEN:
        return {_AUTH_TOKEN: {"viewer": "admin", "admin": True}}
    return {}





def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


# 2026-09-06（Knowledge 批修，token 重签暴露）：测试豁免开关——模块级（_require_auth 是
# 模块级函数，Depends 在路由装饰时绑定，create_app 参数传不进去，只能走模块级状态）。
# create_app(allow_noauth=True) 置 True；生产恒 False（默认）。测试专用，生产代码路径零触达。
_TEST_NOAUTH_FORCE = False


def _require_auth(x_xtmem_token: Optional[str] = Header(default=None)) -> dict:
    """角色级鉴权依赖：解析 X-XTMEM-Token → {viewer, admin}，直接返回给路由参数。

    - 配置了角色 token：只认表内 token；无 token/错 token → 401（匿名拒绝）。
    - 完全没配置 token：仅当显式 XTMEM_REST_ALLOW_LOOPBACK_NOAUTH=1 调试模式放行
      （viewer=__admin__，测试/本地 mock 用）；生产（无该 flag）在 create_app 层已拒绝启动。
    - _TEST_NOAUTH_FORCE：create_app(allow_noauth=True) 的测试豁免通道（auth 文件非空时
      env noauth 只管启动闸管不到请求级——token 重签后暴露，本开关只由测试注入）。
    """
    tokens = _active_tokens()
    # _TEST_NOAUTH_FORCE 语义=「模拟无 token 环境」：只兜 tokens 为空的无头请求，
    # 显式带 token 的请求照常走解析（cockpit 脱敏测试要靠 token 区分普通视角——
    # 开关短路会把它顶成 admin 造成越权假象，2026-09-06 Knowledge 批修）。
    if _TEST_NOAUTH_FORCE and not (x_xtmem_token and tokens):
        return {"viewer": "__admin__", "admin": True, "via": "test-noauth"}
    if not tokens:
        if not _allow_loopback_noauth():
            raise HTTPException(status_code=503, detail="REST auth not configured")
        return {"viewer": "__admin__", "admin": True, "via": "loopback-debug"}
    role = tokens.get(x_xtmem_token or "")
    if not role:
        raise HTTPException(status_code=401, detail="unauthorized: 有效 X-XTMEM-Token required")
    viewer = str(role.get("viewer") or "")
    # R-02 验收修正：admin/owner token 在读取链路归一为 __admin__ 全可见视角。
    # 否则 cockpit_contract._is_admin 只认 None/__admin__，admin 会被当成普通角色过滤，
    # 导致 private / shared_with 对管理员不可见。
    if role.get("admin") and viewer in ("admin", "owner"):
        viewer = "__admin__"
    return {"viewer": viewer, "admin": bool(role.get("admin")), "via": "token"}


def _db_path():
    """P5.1-2（2026-09-02）：共享库路径——env 优先，否则 mimox-data 根 .hermes。"""
    p = os.environ.get("XTMEM_DB_PATH", "").strip()
    if p:
        return p
    from memory.public_runtime import db_default
    return db_default()


def _ro_conn():
    """只读连接（总则 5：探查一律只读；R5 收编 open_db ro 面）。"""
    from memory.open_db import open_db
    db = _db_path()
    if not os.path.exists(db):
        raise RuntimeError("XTMEM DB not found: " + db)
    c = open_db(db, ro=True)
    c.row_factory = sqlite3.Row
    return c


def _run_cli(argv, actor: Optional[str] = None):
    """写路径走 CLI 同一入口（message send / rooms / schedule），不绕库直写。

    R-03：调用方指定 actor → 注入 XTMEM_ACTOR，CLI 端强制 --from == actor；
    未指定 actor 的子命令（rooms set 等无身份命令）不注入。
    """
    py = os.environ.get("XTMEM_PYTHON", sys.executable)
    script = os.environ.get("XTMEM_SCRIPT", __import__("importlib.util", fromlist=["find_spec"]).find_spec("xtmem").origin)
    env = dict(os.environ)
    env.pop("XTMEM_ADMIN", None)
    env.pop("XTMEM_ADMIN_TOKEN", None)
    if actor:
        env["XTMEM_ACTOR"] = str(actor)
    # 2026-09-03：api_server 常驻为无窗后台进程，直接 spawn python.exe（控制台程序）
    # 会为每次 msg send / 回帖弹一个新 cmd 黑窗。加 CREATE_NO_WINDOW 让其继承隐藏。
    try:
        _kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        proc = subprocess.run([py, script, *argv], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False, env=env, **_kw)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": (proc.stdout or proc.stderr or "")[:300]}


def create_app(context_builder=None, vector_store=None, host: str = "127.0.0.1",
              allow_noauth: Optional[bool] = None, provider=None) -> "FastAPI":
    """创建 FastAPI 应用，注入 ContextBuilder 和 VectorStore 实例（P1-10/R-02 鉴权）。

    allow_noauth: 显式覆盖 XTMEM_REST_ALLOW_LOOPBACK_NOAUTH（测试注入用）；None=读环境。
    """
    if not HAS_FASTAPI:
        raise RuntimeError("FastAPI is required for MIMOX REST API")

    noauth = _allow_loopback_noauth() if allow_noauth is None else bool(allow_noauth)
    # 测试豁免落地到请求级依赖（模块级开关，见 _TEST_NOAUTH_FORCE 注释）
    global _TEST_NOAUTH_FORCE
    _TEST_NOAUTH_FORCE = bool(allow_noauth)
    if not _active_tokens() and not noauth:
        raise RuntimeError(
            "REST 生产模式拒绝无 token 启动：请为各角色签发 token —— "
            "python xtmem.py rest-token issue --role <dsh|assistant|workbuddy|zcode|admin|owner>，"
            "或本地调试显式设 XTMEM_REST_ALLOW_LOOPBACK_NOAUTH=1"
        )
    # V1.7 钉边：bind 127.0.0.1 显式钉死，任何非回环绑定一律拒绝（鉴权半条留用户签 token，别碰）
    if not _is_loopback(host):
        raise RuntimeError(
            "REST API refuses non-loopback binding (%s); 127.0.0.1 explicitly pinned" % host
        )

    app = FastAPI(title="MIMOX Memory API", version="1.0")

    # F03（2026-10-15 返修）：本 app 实例的所有权标识。
    # 为什么要实例号而不是 pid：默认重试线程要能区分"本实例本次发起的单"与"上次启动/
    # 别的进程留下的单"。pid 做不到——同一 pytest 进程里父进程造的历史单与新 app 同 pid，
    # 只看 pid 会把历史单误判成自产，RESUME=0 就关不住（正是 F03 的病灶）。
    # 实例号每次 create_app 重新生成，历史单的 owner_instance 与它天然不同 ⇒ 默认排除。
    _instance_id = "inst-" + uuid.uuid4().hex[:12]
    app.state.instance_id = _instance_id

    # V1.7 api_server 钉边（v1.3.2 工单批，2026-09-05）：非回环客户端拦截 + 包体上限 + 配额。
    # 环回内服务也钉（纵深防御）；鉴权半条按工单不做（fail-closed token 面已有）。
    _MAX_BODY = 1 * 1024 * 1024          # 1MB：消息/调度面绰绰有余，防 zip 炸弹级 body
    _RATE_LIMIT = 120                    # 次/分钟/IP
    _rate_window = {}                    # ip -> [ts,...]（进程内；api 是单进程）

    @app.middleware("http")
    async def _edge_guard(request: Request, call_next):
        # V1.7 钉边：从非回环地址连接必须被拒（验穿标准：从非回环地址连接必须被拒）
        client_ip = request.client.host if request.client else ""
        if client_ip and client_ip != "testclient" and not _is_loopback(client_ip):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "code": "forbidden_non_loopback",
                         "message": "access from non-loopback address (%s) is forbidden" % client_ip}
            )

        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_BODY:
            return JSONResponse(status_code=413, content={"ok": False, "code": "body_too_large",
                                    "message": "body exceeds 1MB"})
        ip = (request.client.host if request.client else "?")
        now = time.time()
        win = [x for x in _rate_window.get(ip, []) if now - x < 60]
        win.append(now)
        _rate_window[ip] = win
        # 窗口表自收敛（防 ip 洪泛堆积）
        if len(_rate_window) > 256:
            for k in [k for k, v in _rate_window.items() if not v or now - v[-1] > 120]:
                _rate_window.pop(k, None)
        if len(win) > _RATE_LIMIT:
            return JSONResponse(status_code=429, content={"ok": False, "code": "rate_limited",
                                    "message": "too many requests"})
        return await call_next(request)

    # ── 健康检查（探针免鉴权；仅返回存活信息）──────────

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": "mimox-1.0",
            "uptime_seconds": int(time.time() - _start_time),
        }

    # ── 记忆健康报告 ──────────────────────────────────

    @app.get("/api/memory/health", dependencies=[Depends(_require_auth)])
    def memory_health():
        # 优先用 vector_store 实例，否则降级读 SQLite
        conn = None
        if vector_store:
            conn = vector_store._get_conn()
        else:
            # F-3（2026-09-03 审计整改）：常驻模式（keepalive 独立起）无 vector_store 注入，
            # 改走 _db_path()（env/MIMOX_HOME 正确共享路径）——与 8 条驾驶舱路由同源，
            # 不再因 _get_db_path()（相对 __file__）找不到库而恒 503
            db_path = _db_path()
            if not os.path.exists(db_path):
                return JSONResponse(status_code=503, content={"error": "VectorStore not available"})
            from memory.open_db import open_db
            conn = open_db(db_path)

        try:
            # 分轨分布
            track_rows = conn.execute(
                "SELECT track, COUNT(*) as cnt FROM chunks WHERE state='active' GROUP BY track"
            ).fetchall()
            track_dist = {row["track"]: row["cnt"] for row in track_rows}

            # 演化关系
            evo_rows = conn.execute(
                "SELECT relation, COUNT(*) as cnt FROM memory_evolution GROUP BY relation"
            ).fetchall()
            evolution = {row["relation"]: row["cnt"] for row in evo_rows}

            # active / superseded
            active = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='active'").fetchone()[0]
            superseded = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='superseded'").fetchone()[0]
            total = active + superseded

            # 近 7 天增长
            week_ago = time.time() - 7 * 86400
            recent = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE timestamp > ?", (week_ago,)
            ).fetchone()[0]

            resp = {
                "track_distribution": track_dist,
                "evolution": evolution,
                "active_count": active,
                "superseded_count": superseded,
                "superseded_ratio": round(superseded / total, 3) if total > 0 else 0,
                "recent_7d_growth": recent,
            }
            # P1-G / R-11：注入 provider 时暴露同步队列健康面
            if provider is not None and hasattr(provider, "sync_status"):
                resp["sync"] = provider.sync_status()
            # V1.3 stale 端探测（v1.3.2 #8，2026-09-05）：比对 xtmem_meta.runtime_actors 的
            # code_sha vs 磁盘（本进程）指纹——不匹配 = 旧码进程还在跑 → stale_actors 非空 = 亮红。
            # 自兜底：比对失败不影响 health 主体（探测永不阻塞）。
            try:
                from memory.open_db import get_runtime_actors, _runtime_code_sha
                disk_sha = _runtime_code_sha()

                def _pid_alive(pid_str):
                    """pid 存活检查（2026-09-05 误报面收口：CLI 一命令一进程残留把 health 打成假红）。
                    psutil 可用优先；否则 win32 OpenProcess / POSIX kill(pid,0) 探测。失败按死处理。"""
                    try:
                        pid = int(pid_str)
                    except (TypeError, ValueError):
                        return False
                    try:
                        import psutil  # noqa
                        return psutil.pid_exists(pid)
                    except ImportError:
                        pass
                    import os as _os
                    if _os.name == "nt":
                        try:
                            import ctypes
                            SYNCHRONIZE = 0x00100000
                            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                            if h:
                                ctypes.windll.kernel32.CloseHandle(h)
                                return True
                            return False
                        except Exception:
                            return False
                    try:
                        _os.kill(pid, 0)
                        return True
                    except OSError:
                        return False

                stale = []
                for pid, info in get_runtime_actors(conn).items():
                    if not isinstance(info, dict):
                        continue
                    if info.get("code_sha") and info["code_sha"] != disk_sha:
                        if not _pid_alive(pid):
                            continue  # 残留条目（进程已死）→ 不标红（误报面收口）
                        stale.append({"pid": pid, "actor": info.get("actor"),
                                      "code_sha": info["code_sha"], "ts": info.get("ts")})
                resp["runtime_code_sha"] = disk_sha
                resp["stale_actors"] = stale
                resp["status"] = "stale" if stale else "ok"  # stale 端在场 = 亮红
            except Exception:
                pass  # 比对面失败【不】拖垮 health
            # V1.5 熔断记账（v1.3.2 工单批，2026-09-05）：失败率 + 降级连续计数 → degraded（黄）。
            # stale（红）优先于 degraded（黄）——红黄不叠加，红盖黄。
            # ⚠️ V1.5 修复单（2026-09-05，错题本 #27）：记账面 _probe_ok/fail_count 在 9900
            # 网关进程内存累加，20000 独立进程跨进程读不到 → 黄灯结构性永灭。唯一通路=DB：
            # provider 现已把 probe 成败直写 xtmem_events（kind='probe-ok'/'probe-fail'）。
            # 旧 getattr(provider,...) 内存路径【已删】——单一 DB 通路，防双源漂移。
            try:
                ok_n = 0
                fail_n = 0
                # 24h 窗口：数 probe-ok/probe-fail 事件（provider 每次 prefetch 成败各写一条）
                _win = time.time() - 86400
                try:
                    _prow = conn.execute(
                        "SELECT kind, COUNT(*) c FROM xtmem_events "
                        "WHERE kind IN ('probe-ok','probe-fail') AND ts>=? GROUP BY kind",
                        (_win,),
                    ).fetchall()
                    for _pr in _prow:
                        if _pr["kind"] == "probe-ok":
                            ok_n = int(_pr["c"])
                        elif _pr["kind"] == "probe-fail":
                            fail_n = int(_pr["c"])
                except Exception:
                    pass
                total = ok_n + fail_n
                resp["probe"] = {"ok": ok_n, "fail": fail_n,
                                 "fail_rate": round(fail_n / total, 3) if total >= 5 else None}
                # 第二触发道（V1.5 修复单改口径）：最近【连续】probe-fail（无 probe-ok 打断）>=3
                # 旧口径"24h 内 schema_mismatch/降级类事件数"误把不同源的事件累加当熔断信号，
                # 且降级事件本身不来自 probe 链路——改为 probe 事件序列的连续失败段。
                deg = 0
                try:
                    _seq = conn.execute(
                        "SELECT kind FROM xtmem_events "
                        "WHERE kind IN ('probe-ok','probe-fail') AND ts>=? "
                        "ORDER BY id DESC LIMIT 50",
                        (_win,),
                    ).fetchall()
                    for _s in _seq:
                        if _s["kind"] == "probe-fail":
                            deg += 1
                        else:
                            break  # 被 probe-ok 打断 → 连续段结束
                except Exception:
                    pass
                resp["consecutive_fail"] = deg
                resp["degrade_events_24h"] = deg  # 兼容旧响应键（语义=连续 fail 数）
                # P6.10 verdict 分布（v2.1.2 波2）：近 24h recall 事件按 meta.verdict 聚合
                try:
                    vrows = conn.execute(
                        "SELECT meta FROM xtmem_events WHERE kind='recall' AND ts>=?",
                        (time.time() - 86400,),
                    ).fetchall()
                    dist = {}
                    for vr in vrows:
                        try:
                            v = (json.loads(vr[0]) or {}).get("verdict") or "无标记"
                        except Exception:
                            v = "无标记"
                        dist[v] = dist.get(v, 0) + 1
                    resp["verdict_dist_24h"] = dist
                except Exception:
                    pass
                if resp.get("status") == "ok" and ((total >= 5 and fail_n / total > 0.5) or deg >= 3):
                    resp["status"] = "degraded"
                    resp["degraded_reason"] = ("probe fail_rate>0.5" if (total >= 5 and fail_n / total > 0.5)
                                               else "consecutive_fail>=3")
            except Exception:
                pass  # 熔断记账自兜底
            # FTS5 降级留痕（2026-09-13 件②.2 联动，随"第一刀"回执附件落地）：
            #   24h 内有 fts5_fallback 事件 → FTS5 腿带伤，health 不得显示正常。
            #   口径对齐 probe 段（红黄不叠加：只在 status=ok 时降黄）。
            try:
                _frow = conn.execute(
                    "SELECT COUNT(*) c FROM xtmem_events WHERE kind='fts5_fallback' AND ts>=?",
                    (_win,),
                ).fetchone()
                _f24h = int(_frow["c"]) if _frow else 0
                resp["fts5"] = {"fallback_24h": _f24h, "ok": _f24h == 0}
                if resp.get("status") == "ok" and _f24h > 0:
                    resp["status"] = "degraded"
                    resp["degraded_reason"] = "fts5_fallback events>0 (24h)"
            except Exception:
                pass  # FTS5 口径自兜底，不拖垮 health
            return resp
        finally:
            if not vector_store:
                conn.close()

    # ── 调用统计 ──────────────────────────────────────

    @app.get("/api/stats", dependencies=[Depends(_require_auth)])
    def stats():
        if context_builder:
            return {
                "store_count_total": getattr(context_builder, '_store_count', 0),
                "prefetch_count_total": getattr(context_builder, '_prefetch_count', 0),
                "probe_ok_count": getattr(context_builder, '_probe_ok_count', 0),
                "probe_fail_count": getattr(context_builder, '_probe_fail_count', 0),
                "timeline": getattr(context_builder, '_timeline', [])[-96:],
            }

        # 降级：context_builder 不可用时返回基本统计（F-3：常驻模式改走 _db_path() 读真库）
        db_path = _db_path()
        if not os.path.exists(db_path):
            return JSONResponse(status_code=503, content={"error": "VectorStore not available"})

        from memory.open_db import open_db
        conn = open_db(db_path)
        try:
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            return {
                "store_count_total": chunk_count,
                "prefetch_count_total": 0,
                "probe_ok_count": 0,
                "probe_fail_count": 0,
                "timeline": [],
            }
        finally:
            conn.close()

    # ── 检索审计 ──────────────────────────────────────

    @app.get("/search_trace", dependencies=[Depends(_require_auth)])
    def search_trace(limit: int = 20):
        if not context_builder:
            return JSONResponse(status_code=503, content={"error": "ContextBuilder not available"})

        timeline = getattr(context_builder, '_timeline', [])
        return {"traces": timeline[-limit:]}

    # ── 手动修复 ──────────────────────────────────────

    @app.patch("/memory/{chunk_id}", dependencies=[Depends(_require_auth)])
    def update_memory(chunk_id: str, body: dict):
        if not vector_store:
            return JSONResponse(status_code=503, content={"error": "VectorStore not available"})

        # P1-10 枚举校验：state 三态固定；track 限已知集合（新增分轨需同步这里）
        # ⚠️ W3（2026-09-05，W 批接线）：本端点【不】接受 visibility 变更——
        # visibility 收紧入口 = quarantine_bad_visibility（转 private 已接 follow_source_visibility）。
        # 未来若加 visibility 变更入口，【必】同步接 vector_store.follow_source_visibility(id, new_vis)
        # （源收紧 → shared 派生必须跟随降，防明文越源泄漏）。
        _VALID_STATES = ("active", "superseded", "archived")
        _VALID_TRACKS = ("default", "identity", "preference", "emotion", "explicit", "delegation")
        for key in ("state", "track"):
            if key in body:
                allowed = _VALID_STATES if key == "state" else _VALID_TRACKS
                if body[key] not in allowed:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "invalid %s: %r (allowed: %s)" % (key, body[key], allowed)},
                    )

        conn = vector_store._get_conn()
        updates = []
        params = []
        for key in ("state", "track", "superseded_by"):
            if key in body:
                updates.append(f"{key} = ?")
                params.append(body[key])

        if not updates:
            return JSONResponse(status_code=400, content={"error": "No valid fields to update"})

        params.append(chunk_id)
        sql = f"UPDATE chunks SET {', '.join(updates)} WHERE id = ?"
        try:
            cur = conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            return JSONResponse(status_code=500, content={"error": "Database update failed"})

        if cur.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "Chunk not found"})

        # W1（2026-09-05，W 批接线）：PATCH 置 archived 与 CLI/脚本路径同语义——
        # 源主动归档 → 指向它的 active 派生必须打 lineage-stale 标+事件留痕（V1.1 反向血缘）。
        # 只 state=archived 触发；state=superseded【不】触发（与 cmd_forget/演化设计一致，
        # 聚合型派生引多代 superseded 是设计）。propagate 失败不否定归档本身（源已 archived，
        # 派生标可下次补），错误带在响应里供审计。
        _propagated = 0
        _propagate_error = None
        if body.get("state") == "archived" and vector_store is not None:
            try:
                _propagated = vector_store.propagate_source_archived(chunk_id, reason="api-patch")
            except Exception as _exc:  # 归档已提交，打标失败仅记录（可重跑 propagate 补）
                _propagate_error = str(_exc)

        return {"ok": True, "chunk_id": chunk_id, "updated": body,
                "lineage_stale_derived": _propagated,
                "lineage_stale_error": _propagate_error}

    # ── P5.1-2 驾驶舱后端（2026-09-02，P5.1-2b 契约缝合版）────────────────
    # 八端点按照渊台契约为准组装（形状唯一出处 <WORKBENCH>\\src\\api\\contract.ts +
    # endpoints.ts；组装纯函数在 memory/cockpit_contract.py，mock 是行为基准）。
    # 写侧 msg/schedule/widget 走 CLI 单入口或本表专属直写（widgets 非消息域）；
    # 读侧 _ro_conn（mode=ro）；鉴权继承 XTMEM_REST_TOKEN + 非 loopback 无 token 拒启动。
    from memory import cockpit_contract as cc

    _probe_cache = {"t": 0.0, "v": {}}

    def _probe_agents():
        """socket 探活四端（127.0.0.1:9900/28000/5778），缓存 10s——轮询 2-5s 别把端口捶死。"""
        now = time.time()
        if _probe_cache["v"] and now - _probe_cache["t"] < 10.0:
            return dict(_probe_cache["v"])
        out = {}
        for _aid, _port in cc.PROBE_PORTS.items():
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(0.4)
                    out[_aid] = _s.connect_ex(("127.0.0.1", _port)) == 0
            except Exception:
                out[_aid] = False
        _probe_cache.update({"t": now, "v": out})
        return dict(out)

    def _wconn():
        """写路径连接（R5 收编 open_db；row_factory 与 _ro_conn 同款，响应组装可复用）。"""
        from memory.open_db import open_db
        c = open_db(_db_path())
        return c

    def _fetch_message(c, mid):
        r = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None

    def _fetch_room(c, room_id):
        r = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return dict(r) if r else None

    def _write_event(kind, agent, meta=None):
        """调度活性留痕（msg_invite/msg_timeout 等）——事件尽力而为，失败静默。"""
        try:
            from memory.open_db import open_db
            c = open_db(_db_path())
            c.execute("INSERT INTO xtmem_events (ts, kind, agent, meta) VALUES (?,?,?,?)",
                      (time.time(), kind, agent, json.dumps(meta or {}, ensure_ascii=False)))
            c.commit()
            c.close()
        except Exception:
            pass

    def _set_msg_state(mid, state):
        """直写消息链上位置（走链观感；与 task_ack 直写 state 同口径）。失败静默。"""
        try:
            from memory.open_db import open_db
            c = open_db(_db_path())
            c.execute("UPDATE messages SET state=? WHERE id=?", (state, mid))
            c.commit()
            c.close()
        except Exception as e:
            # 消息状态写入失败不阻断主流程，但必须有审计痕迹——静默 pass 会导致消息状态
            # 不一致且无日志可查（2026-09-16 审计发现-3 修复）。
            emit('rest', 'delivery.task', 'failed', error=e, fallback_reason='deferred_recovery')

    def _looks_like_ack(reply):
        """判别 A2A 响应是否只是「入站确认」而非对端真回复（防污染对话流）。
        例：小D A2A-2 返回 '[pending] A2A 入站已注入小D会话'。"""
        r = str(reply or "")
        return any(k in r for k in ("[pending]", "已注入", "入站", "A2A 已", "正在唤醒", "轮询"))

    def _delivery_db_path():
        """恢复投递状态使用与消息表相同的权威 SQLite 文件。"""
        return _db_path()

    def _accept_task_reply(ns, mid, operation_id, peer_task_id, peer_context_id, text):
        """poller 与直接回帖共享唯一回程事务，只有首个成功回程可入总线。

        ⚠️【不】把"是否成功"写成黑名单 `code not in ("task_mismatch","context_mismatch")`
        （2026-10-15 审计建议）：黑名单是**漏判型**判据——将来新增任何失败码，只要忘带
        `ok=False`（或本函数忘了补名单），就会把失败当成功、把源消息置成 replied。
        改为正面白名单：只有明确"回程已写入/已存在"的码才算成功。
        当前 record_reply 的成功码只有 inserted / duplicate / legacy_inserted / legacy_duplicate；
        缺源与无有效回程（no_valid_reply/missing_source）都在 ok=False 且不在白名单里。
        """
        result = _write_reply_back(ns, mid, text, operation_id, peer_task_id, peer_context_id)
        if result.get("ok") and result.get("code") in ("inserted", "duplicate",
                                                       "legacy_inserted", "legacy_duplicate"):
            _set_msg_state(mid, "replied")
        return result

    def _source_row_in(c, mid):
        """在**给定连接**上取回程绑定锚点行（源消息 from_agent + 可见性列）。

        ⚠️【不】在 record_reply 的写事务里另开读连接：同一库上再开一条连接去读，
        在有写锁的库上等于自造锁等待。调用方手里有连接就复用（`_insert(c, ...)` 传的是
        签收册连接），没有连接时才由 `_reply_source_row` 自己开只读连接。
        """
        try:
            cur = c.execute(
                "SELECT from_agent, visibility, shared_with, room_id, topic_id FROM messages WHERE id=?",
                (mid,))
            cols = [d[0] for d in (cur.description or [])]
            r = cur.fetchone()
            # ⚠️ 不假定 row_factory：签收册事务连接没有 row_factory，回来的是 tuple，
            # 直接 dict(r) 会 TypeError。按 description 拼 dict 才与连接设置无关。
            return dict(zip(cols, r)) if r else None
        except Exception:
            return None

    def _reply_source_row(mid):
        """取回程绑定的锚点行；读不到返回 None。

        G01：源行是"答案该回到谁手里"的唯一依据，读不到就一律不认回程（不消费名额）。
        """
        try:
            from memory.open_db import open_db
            c = open_db(_db_path(), ro=True)
            c.row_factory = sqlite3.Row
            try:
                return _source_row_in(c, mid)
            finally:
                c.close()
        except Exception as e:
            emit('rest', 'delivery.task', 'degraded', error=e)
            return None

    def _bound_reply_rows(ns, mid, job):
        """本单**已绑定**的回程行（actor=投递目标、接收者=原请求人），由新到旧。

        G01 病根就在这里：原实现只按 from_agent+reply_to 取行，于是"错收件人的普通帖"
        被当成"(ns) 已回帖"，poller/worker 记为完成、真答案再也写不进去。
        ⚠️ 绑定判定的唯一实现是 delivery_jobs.row_is_bound_reply，别在本地再写一份。
        """
        src = _reply_source_row(mid)
        if not src:
            return []
        try:
            from memory.open_db import open_db
            from memory import delivery_jobs as _dj
            c = open_db(_db_path(), ro=True)
            c.row_factory = sqlite3.Row
            try:
                return _dj.bound_reply_rows(c, ns, mid, src, (job or {}).get("target_agent"), limit=8)
            finally:
                c.close()
        except Exception as e:
            emit('rest', 'delivery.task', 'degraded', error=e)
            return []

    def _reply_exists(ns, mid, job=None):
        """该对端是否已把真回复写回总线（防双写：poller 与对端 runXtmemReply 都可能写）。

        只认「真正给原请求人」的回程：非 ack 文本 **且** 身份绑定成立（G01）。
        错收件人的普通帖、资格不符的关联帖一律不算——它们没让请求人拿到答案。
        ⚠️ 容错方向（异常时返回 False）保持不变：那是"保守地认为还没收到回复"，
        让 poller 继续按协议取回真答案；放宽成 True 才会造成真实答案永远不落地。
        """
        if job is None:
            try:
                from memory import delivery_jobs as _dj
                job = _dj.get_job(_delivery_db_path(), "a2a-" + str(mid))
            except Exception:
                job = None
        for row in _bound_reply_rows(ns, mid, job):
            content = row.get("content")
            if content and not _looks_like_ack(content):
                return True
        return False

    def _write_reply_back(ns, mid, text, operation_id="", task_id="", context_id=""):
        """通过持久投递行去重写回总线；CLI 调用仍以可信 peer namespace 注入 actor。

        ⚠️ R08（2026-10-15）：回程行的可见性必须【继承源消息】，不再硬写 'shared'。
        原先两个 INSERT 都写死 visibility='shared'，等于"私下问的话，因为另一个 agent 答复了
        就自动贴到公共墙上"。改为从源消息（mid）整行继承 visibility / shared_with，
        既不放宽权限，也不把 owner 改成 peer 让请求人自己反而读不到（请求人仍是接收方）。

        注意 INSERT...SELECT 是从源消息行取值：源消息若不存在（异常路径），
        SELECT 无行 → 不插入，不会产生无权限锚点的孤儿回程。

        ⚠️ G01（2026-10-15 返修）：去重与完成登记都必须带**身份绑定**，不能只比正文。
        原 `_insert` 查到"from_agent + reply_to + content 相同"就 return —— 错收件人的
        同文普通帖正好满足这三列，于是它把名额占了、真答案不再 INSERT，而
        record_reply 照旧盖 completed（系统假报成功、请求人零答案）。
        修法：判重改走 delivery_jobs.existing_bound_reply_id（绑定过的同文行才算数），
        并把"是否真有有效回程"回报给 record_reply 决定能否盖完成章。
        """
        from memory import delivery_jobs

        def _insert(c, job, reply, digest):
            # ⚠️ 源行必须在本事务连接上取：另开连接读会被写锁挡住（见 _source_row_in）。
            src = _source_row_in(c, mid)
            target = (job or {}).get("target_agent")
            # 缺源：无从判定"答案该给谁"，不插入、也不许记完成（G01 关闭判据）。
            if not src:
                return {"reply_present": False, "inserted": False}
            existing = delivery_jobs.existing_bound_reply_id(c, ns, mid, str(reply), src, target)
            if existing:
                # 已有一份绑定到本单的同文回程 → 不重复插，但**确实存在有效回程**。
                return {"reply_present": True, "inserted": False, "id": existing}
            reply_id = "reply-" + hashlib.sha256((job["operation_id"] + "\\0" + digest).encode("utf-8")).hexdigest()[:24]
            # ⚠️【不】用 c.total_changes 直接判本条是否插进去：那是**连接累计**值，
            # 签收册连接在建表/补列后累计值天然非 0，拿它当"插了"会把"源行不存在
            # 所以没插"也判成插成功（假完成）。取前后差值才是本条语句的效果。
            before = c.total_changes
            c.execute(
                "INSERT INTO messages (id,from_agent,to_agent,content,ts,read,room_id,topic_id,kind,state,reply_to,visibility,shared_with) "
                "SELECT ?,?,?,?, ?,0,COALESCE(room_id,''),COALESCE(topic_id,''),'msg','',?,"
                "COALESCE(NULLIF(visibility,''),'shared'),COALESCE(shared_with,'') FROM messages WHERE id=?",
                (reply_id, ns, src.get("from_agent") or "owner", str(reply), time.time(), mid, mid),
            )
            inserted = (c.total_changes - before) > 0
            return {"reply_present": inserted, "inserted": inserted, "id": reply_id}

        result = delivery_jobs.record_reply(_delivery_db_path(), operation_id, task_id, context_id, str(text), _insert)
        if result.get("code") == "unknown_operation":
            # 旧入口/人工回帖在未关联 outbox 的情况下保留兼容行为，但用确定性消息 ID 去重。
            # R08：兼容路径同样继承源消息可见性，不硬写 shared；G01：同样要过身份绑定。
            src = _reply_source_row(mid)
            if not src:
                emit('rest', 'delivery.task', 'failed', fallback_reason='missing_source')
                return {"ok": False, "code": "missing_source", "inserted": False, "reply_present": False}
            reply_id = "reply-" + hashlib.sha256((str(mid) + "\\0" + str(ns) + "\\0" + str(text)).encode("utf-8")).hexdigest()[:24]
            c = _wconn()
            try:
                # 兼容路径没有签收册行 → 没有 job.target_agent 可绑；但接收者由本处
                # 依源消息推导（to_agent=源发起人），确定性 ID 负责幂等。
                had = c.execute("SELECT id FROM messages WHERE id=?", (reply_id,)).fetchone()
                if had:
                    result = {"ok": True, "code": "legacy_duplicate", "inserted": False, "reply_present": True}
                else:
                    # 同上：用差值为准，源行缺失导致没插进去时不得报"有效回程存在"。
                    before = c.total_changes
                    c.execute(
                        "INSERT OR IGNORE INTO messages (id,from_agent,to_agent,content,ts,read,room_id,topic_id,kind,state,reply_to,visibility,shared_with) "
                        "SELECT ?,?,?,?, ?,0,COALESCE(room_id,''),COALESCE(topic_id,''),'msg','',?,"
                        "COALESCE(NULLIF(visibility,''),'shared'),COALESCE(shared_with,'') FROM messages WHERE id=?",
                        (reply_id, ns, src.get("from_agent") or "owner", str(text), time.time(), mid, mid),
                    )
                    c.commit()
                    inserted = (c.total_changes - before) > 0
                    result = {"ok": True, "code": "legacy_inserted", "inserted": inserted,
                              "reply_present": inserted}
            finally:
                c.close()
        emit('rest', 'delivery.task', 'ok' if result.get('ok') else 'failed')
        return result

    def _poll_task_reply(peer, ns, mid, task_id, tries=None, wait_s=None, operation_id=""):
        """P5.1-2d：A2A 方案 B 异步产出真回复 → 轮询 tasks/get 取回写总线。
        对端已自己回帖（runXtmemReply 新代码）→ 只把原帖 state 拨到 replied，不双写。
        轮询窗口默认约 30s（6 次 x 5s）；到点没真回则原帖转 timeout（内容不丢）。
        测试可用 XTMEM_A2A_POLL_TRIES / XTMEM_A2A_POLL_WAIT_S 提速。"""
        _tries = int(tries if tries is not None else os.environ.get("XTMEM_A2A_POLL_TRIES", "6"))
        _wait = float(wait_s if wait_s is not None else os.environ.get("XTMEM_A2A_POLL_WAIT_S", "5"))
        _tries = max(1, min(_tries, 20))
        _wait = max(0.05, min(_wait, 30.0))
        if not operation_id:
            operation_id = "a2a-" + str(mid)

        @diagnosed('rest', 'delivery.poll')
        def _poller():
            # ⚠️ 本地 import 必须留着：delivery_jobs 不是模块级导入，下面几处都要用。
            from memory import delivery_jobs
            try:
                from memory.a2a_client import a2a_get_task
                for _ in range(_tries):
                    time.sleep(_wait)
                    try:
                        reply, _ctx, _st, actual_task_id = a2a_get_task(peer, task_id)
                    except Exception as exc:
                        emit('rest', 'delivery.poll', 'retry', error=exc, fallback_reason='retry')
                        continue
                    # R05（2026-10-15）：核对响应里【实际】的 task id，不看我们请求的那个。
                    # 错号 → 这不是本单的回程，拒绝入库并继续等（不补成预期值、不假装成功）。
                    if not actual_task_id:
                        emit('rest', 'delivery.poll', 'degraded', fallback_reason='missing_task_identity')
                        continue
                    if str(actual_task_id) != str(task_id):
                        emit('rest', 'delivery.poll', 'degraded', fallback_reason='task_id_mismatch')
                        continue
                    if _reply_exists(ns, mid, delivery_jobs.get_job(_delivery_db_path(), operation_id)):
                        # 旧版平台可能先直接回帖；读到结果后只登记完成，不再制造第二条回程。
                        existing = delivery_jobs.get_job(_delivery_db_path(), operation_id)
                        if existing and existing.get("status") not in ("completed", "acked"):
                            delivery_jobs.mark_completed(_delivery_db_path(), operation_id, str(actual_task_id), _ctx)
                        _set_msg_state(mid, "replied")
                        return
                    # 2026-09-07 修「poller 双写」：WORKING/SUBMITTED 等中间态的
                    # status.message 是协议包装（不是真回复），写回总线就是
                    # {'id':'task-...'} 垃圾帖——仅终态（部分失败态由下行处理）才写。
                    # R02（2026-10-15）：终态只认协议 state，不看正文长得像不像答案——
                    # 对端 WORKING 带普通进度文本（如 "still preparing result"）也不能算最终回程。
                    _norm = normalize_state(_st)
                    if _norm in FAILURE_STATES:
                        _set_msg_state(mid, "timeout")
                        return
                    if _norm not in TERMINAL_STATES:
                        continue
                    if reply and not _looks_like_ack(reply):
                        _accept_task_reply(ns, mid, operation_id, str(actual_task_id), _ctx, reply)
                        return
                _set_msg_state(mid, "timeout")
                emit('rest', 'delivery.poll', 'timeout')
            except Exception as exc:
                emit('rest', 'delivery.poll', 'failed', error=exc)
                _set_msg_state(mid, "timeout")
        context = copy_context()
        threading.Thread(target=lambda: context.run(_poller), daemon=True).start()

    def _spawn_a2a_worker(mid, to0, content, room_id):
        """发消息后先持久入队再后台发送；恢复依赖 delivery_jobs，不另建任务状态中枢。"""
        from memory import delivery_jobs
        peer = cc.A2A_OF.get(to0, to0)
        ns = cc.NS_OF[to0]
        rid = str(room_id or "").strip()
        route = "[XTMEM:single] " if rid.startswith("dm:") else "[XTMEM:room:" + (rid or "none") + "] "
        payload = route + str(content)
        operation_id = "a2a-" + str(mid)
        job = delivery_jobs.enqueue(_delivery_db_path(), str(mid), to0, peer, str(mid), payload, operation_id,
                                    owner_instance=_instance_id)

        @diagnosed('rest', 'delivery.task')
        def _worker():
            claimed = delivery_jobs.claim(_delivery_db_path(), operation_id,
                                         lease_s=float(os.environ.get("XTMEM_A2A_LEASE_S", "45")),
                                         max_attempts=int(os.environ.get("XTMEM_A2A_MAX_ATTEMPTS", "4")))
            if not claimed:
                return
            _set_msg_state(mid, "waking")
            try:
                from memory.a2a_client import a2a_send_operation
                # 超时前先查回程/已完成行；未知结果转有限退避，不能立刻用新身份重做。
                current = delivery_jobs.get_job(_delivery_db_path(), operation_id)
                if current and current.get("status") in ("completed", "acked"):
                    return
                reply, _ctx, state, task_id, request_id, message_id = a2a_send_operation(
                    peer, payload, str(mid), operation_id=operation_id)
                delivery_jobs.record_sent(_delivery_db_path(), operation_id, request_id, task_id, _ctx, reply, state)
                if _reply_exists(ns, mid, delivery_jobs.get_job(_delivery_db_path(), operation_id)):
                    # 对端已自己回帖（直回路径先到）→ 只补完成记账，不再写第二条。
                    delivery_jobs.mark_completed(_delivery_db_path(), operation_id, task_id, _ctx)
                    _set_msg_state(mid, "replied")
                    return
                # R01/R02（2026-10-15）：同步终态答案必须【先入总线、再记完成】。
                # 原实现先 record_sent 把状态置 completed，随后 record_reply 看到 completed 直接
                # 判 duplicate 返回，正文永远进不了消息表——同步成功响应静默丢失、源消息却显示
                # replied。判终态只认协议 state（completed/succeeded），不看文本像不像答案。
                _norm = normalize_state(state)
                if _norm in TERMINAL_STATES and reply and not _looks_like_ack(reply):
                    _accept_task_reply(ns, mid, operation_id, task_id, _ctx, reply)
                    return
                name = cc.AGENT_NAME.get(to0, to0)
                _run_cli(["msg", "send", "--from", "owner", "--to", "owner",
                          "--text", "已唤醒" + str(name) + "，等待其回复……",
                          "--kind", "notice", "--reply-to", mid], actor="owner")
                if task_id:
                    _poll_task_reply(peer, ns, mid, task_id, operation_id=operation_id)
                else:
                    # Message-only A2A 响应没有 task ID：只登记"传输已受理"，不伪造 tasks/get 参数，
                    # 也不冒充业务完成（§3 裁定：没有 task 号不等于对方已答复）——留给直接回帖路径收尾。
                    delivery_jobs.mark_transport_accepted(_delivery_db_path(), operation_id, _ctx)
                    _set_msg_state(mid, "waking")
            except Exception as e:
                emit('rest', 'delivery.task', 'failed', error=e)
                # 有界退避：tries 次上限由持久 attempts 计数，保留 uncertain 供人工/恢复核查。
                current = delivery_jobs.get_job(_delivery_db_path(), operation_id) or {}
                attempt = int(current.get("attempts") or 1)
                delivery_jobs.record_error(_delivery_db_path(), operation_id, type(e).__name__,
                                           _retry_delay(attempt),
                                           max_attempts=int(os.environ.get("XTMEM_A2A_MAX_ATTEMPTS", "4")))
                _set_msg_state(mid, "timeout")
                _write_event("msg_timeout", to0, meta={"room": str(room_id or ""), "error_type": type(e).__name__})
        context = copy_context()
        threading.Thread(target=lambda: context.run(_worker), daemon=True).start()

    def _retry_delay(attempt):
        """有界指数退避：首次重试要"快"，避免把瞬时失败拖成长期停摆。

        ⚠️ 【不】退回 `min(300, 2.0 ** attempt)`（2026-10-15 实测边界踩到）：
        attempt=1 时它给 2.0 秒，而回归判据的等候窗口是 2.3 秒——第一轮重试刚好卡在
        窗口边上，调度慢一点就判"没有消费者"（假红）。R03 的契约是"有界退避 + 有真实
        消费者"，不是"必须等 2 秒才准重试"。改成 0.5/1/2/4…秒起步，上限仍 30 秒，
        attempts 封顶逻辑不变（终态仍有界）。
        """
        return min(30.0, 0.5 * (2.0 ** max(0, min(int(attempt) - 1, 6))))


    def _resume_one_job(job, db_path=None):
        """恢复单条投递：已知 task 先查回程，未知结果保持同 operation 且有界（R03）。

        返回 'completed' | 'requeued' | 'skipped'，供调用方计数/日志。
        【不】盲目重发：peer_task_id 有值 = 对端可能已在处理，先 tasks/get 对账；
        拿不到终态就原地保留（同 operation、不动 payload），交给下一轮或人工核查。

        ⚠️ db_path 显式传入优先（2026-10-15）：后台重试线程跨测试存活，若这里现读
        _delivery_db_path()（每次读 os.environ），线程会跑到下一个测试的库上去消费，
        制造幽灵重试。线程侧必须在启动时把库路径钉死并一路传进来。
        """
        from memory import delivery_jobs
        dbp = db_path or _delivery_db_path()
        op = job["operation_id"]
        peer = job.get("peer") or ""
        task_id = str(job.get("peer_task_id") or "")
        mid = job.get("source_message_id")
        ns = cc.NS_OF.get(job.get("target_agent"), job.get("target_agent"))
        try:
            if task_id:
                from memory.a2a_client import a2a_get_task
                try:
                    reply, ctx, st, actual_task_id = a2a_get_task(peer, task_id)
                except Exception as exc:
                    emit('rest', 'delivery.resume', 'retry', error=exc, fallback_reason='lookup_failed')
                    return 'skipped'
                # R05 同口径：先核对实际 task 身份，错号不当成本单回程。
                if not actual_task_id or str(actual_task_id) != task_id:
                    emit('rest', 'delivery.resume', 'degraded', fallback_reason='task_id_mismatch')
                    return 'skipped'
                _norm = normalize_state(st)
                if _norm in TERMINAL_STATES and reply and not _looks_like_ack(reply):
                    _accept_task_reply(ns, mid, op, actual_task_id, ctx, reply)
                    return 'completed'
                if _norm in FAILURE_STATES:
                    _set_msg_state(mid, "timeout")
                    return 'skipped'
                # 仍在跑：续租，别抢（有界：attempts 计数不变，等同"观察"而非"重发"）
                emit('rest', 'delivery.resume', 'ok', fallback_reason='still_working')
                return 'skipped'
            # 没有 task 号 = 可能是崩溃在"发出前"：可安全重新投递（同 operation，对端可去重）。
            claimed = delivery_jobs.claim(
                dbp, op,
                lease_s=float(os.environ.get("XTMEM_A2A_LEASE_S", "45")),
                max_attempts=int(os.environ.get("XTMEM_A2A_MAX_ATTEMPTS", "4")))
            if not claimed:
                return 'skipped'
            from memory.a2a_client import a2a_send_operation
            reply, _ctx, state, new_task_id, request_id, _message_id = a2a_send_operation(
                peer, job.get("payload") or "", job.get("context_id") or "", operation_id=op)
            delivery_jobs.record_sent(dbp, op, request_id, new_task_id, _ctx, reply, state)
            _norm = normalize_state(state)
            if _norm in TERMINAL_STATES and reply and not _looks_like_ack(reply):
                _accept_task_reply(ns, mid, op, new_task_id, _ctx, reply)
                return 'completed'
            if new_task_id:
                _poll_task_reply(peer, ns, mid, new_task_id, operation_id=op)
            else:
                delivery_jobs.mark_transport_accepted(dbp, op, _ctx)
            return 'requeued'
        except Exception as exc:
            emit('rest', 'delivery.resume', 'failed', error=exc)
            current = delivery_jobs.get_job(dbp, op) or {}
            attempt = int(current.get("attempts") or 1)
            delivery_jobs.record_error(dbp, op, type(exc).__name__,
                                       _retry_delay(attempt),
                                       max_attempts=int(os.environ.get("XTMEM_A2A_MAX_ATTEMPTS", "4")))
            return 'skipped'

    def _run_recovery_once(limit=None, db_path=None, owner_instance=None):
        """扫一遍到期的待恢复投递并逐条处理；返回统计（R03 的消费者入口）。

        覆盖两类，语义不同、开关也不同：
          ① 崩溃恢复（status='sending' 且租约过期）——跨进程接手别人留下的在途单；
          ② 失败重试（pending/uncertain 且退避时刻已到）——同进程内自己没送成的单。
        【不】在 create_app 里无条件起"跨进程恢复"线程：默认 off，由 XTMEM_A2A_RESUME=1
        或显式调用 _run_recovery_once() 触发——"把恢复代码写完并接到可测试入口"属交付范围，
        "生产自动接别人的单"属上线授权（短单与验收结论 §3 同口径）。
        但失败重试不属上线授权：本来就有 pending 债必须有人还，见 _retry_loop。
        db_path 显式传入优先（后台线程必须钉死库，理由见 _resume_one_job ⚠️）。

        F03（2026-10-15）：owner_instance 控制所有权边界。
          缺省 None = 显式恢复/运维对账，扫全部到期单（含历史/他人单）——这是"授权显式恢复"
          的语义，调用方自己就是那个授权；
          传具体实例号 = 只扫本实例的单。
        默认重试线程必须传实例号，见 _retry_loop。
        """
        from memory import delivery_jobs
        dbp = db_path or _delivery_db_path()
        stats = {"scanned": 0, "completed": 0, "requeued": 0, "skipped": 0}
        try:
            due = delivery_jobs.due_jobs(dbp, limit=limit or 50, owner_instance=owner_instance)
        except Exception as exc:
            emit('rest', 'delivery.resume', 'failed', error=exc)
            return stats
        stats["scanned"] = len(due)
        for job in due:
            # ⚠️ 每条只处理一次：_resume_one_job 有副作用（改状态/发请求），
            # 写成 stats[f(job)] 与 f(job) 两次调用会让同一单被恢复两遍。
            outcome = _resume_one_job(job, dbp)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _retry_loop(stop_evt, db_path, instance_id):
        """同进程内消费"失败后退避到点"的单，直到 attempts 用尽进 uncertain。

        为什么这条默认开、而跨进程恢复默认关：失败的 pending 债是本进程自己欠的，
        不还就是"账还在但消息永远不再处理"（R03 原始病灶，独立判据
        test_failure_has_a_real_retry_consumer 直接盯这个）。跨进程接别人的 sending
        才涉及"替另一个进程做决定"，那才需要显式授权。
        有界：attempts 由持久计数封顶（XTMEM_A2A_MAX_ATTEMPTS），到顶转 uncertain 停下。

        ⚠️【不】去掉 owner_instance 过滤（2026-10-15 F03 原始病灶）：原来这里
        `due_jobs(db_path)` 读全部到期单，于是 RESUME=0 只关掉了第二个循环，
        这条默认管道照样把历史/别的进程的 sending、pending、awaiting_reply 抽走并真的外发
        （判据 test_resume_off_does_not_consume_old_sending 实测：不发新任务也发出一次
        SendMessage）。"同进程失败重试默认开"的正当边界是**本实例自己发起的单**，
        不是"库里所有还没送成的单"。只过滤 sending 字符串也不够——历史 pending/awaiting_reply
        同样会外发。

        ⚠️【不】把 db_path 改成循环内现读 env（2026-10-15 实测踩到）：本线程是 daemon，
        跨测试存活，而 _db_path() 每次读 os.environ——下一个测试换了 XTMEM_DB_PATH 后，
        老线程会跑到新库上去消费，制造幽灵重试与随机红。库路径必须在启动时钉死，
        由 stop_evt 随 app 退出收工。
        """
        interval = float(os.environ.get("XTMEM_A2A_RETRY_INTERVAL_S", "1"))
        while not stop_evt.is_set():
            try:
                if stop_evt.wait(max(0.2, interval)):
                    return
                from memory import delivery_jobs
                due = delivery_jobs.due_jobs(db_path, limit=50, owner_instance=instance_id)
                for job in due:
                    if stop_evt.is_set():
                        return
                    _resume_one_job(job, db_path)
            except Exception as exc:
                # 单轮失败不停线程（库锁/瞬时错误都会自愈），但必须留痕
                emit('rest', 'delivery.resume', 'failed', error=exc)

    @app.get("/api/cockpit/state")
    def cockpit_state(auth: dict = Depends(_require_auth)):
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.cockpit_state(c, _probe_agents(), viewer=auth.get("viewer"))
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.post("/api/rooms", dependencies=[Depends(_require_auth)])
    async def create_room(req: Request):
        """POST /api/rooms —— 创建房间（照渊台前端「＋新建」）。对席 dm:{to} / 合席 gr-*。

        幂等：同 id 房已存在 → 返回现有房（前端以返回 room.id 选中，不关心新建/复用）。
        只建房，开轮次走 POST /api/room/{id}/schedule（别混）。
        """
        import hashlib as _hl
        body = await req.json()
        rtype = str(body.get("type") or "dm")
        if rtype not in ("dm", "group"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": "bad type: dm|group"})
        c = _wconn()
        try:
            if rtype == "dm":
                to = str(body.get("to") or "")
                if to not in cc.CONTRACT_AGENT_IDS:
                    return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                                    "message": "对席房必须给在册 to（assistant/dsh/workbuddy/zcode）"})
                room_id = "dm:" + to
                name = "与" + cc.AGENT_NAME[to]
                ex = _fetch_room(c, room_id)
                if ex:
                    return {"ok": True, "room": cc.room_of(c, ex)}
                c.execute(
                    "INSERT INTO rooms (id, name, members, mode, invite_prompt, current_round, pending_speakers, ts)"
                    " VALUES (?,?,?,?,?,0,'[]',?)",
                    (room_id, name, json.dumps(["owner", to], ensure_ascii=False),
                     "sequential", "", time.time()))
                c.commit()
                return {"ok": True, "room": cc.room_of(c, _fetch_room(c, room_id))}
            # 合席
            members = [m for m in (body.get("members") or []) if str(m) in cc.CONTRACT_AGENT_IDS]
            if not members:
                return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                                "message": "合席房至少一个在册成员"})
            sm = str(body.get("speak_mode") or "sequential")
            sm_db = cc.MODE_OF_CONTRACT.get(sm, "sequential")
            room_id = "gr-" + _hl.md5("".join(members).encode("utf-8")).hexdigest()[:8]
            name = "群聊 · " + "、".join(cc.AGENT_NAME[m] for m in members)
            ex = _fetch_room(c, room_id)
            if ex:
                return {"ok": True, "room": cc.room_of(c, ex)}
            c.execute(
                "INSERT INTO rooms (id, name, members, mode, invite_prompt, current_round, pending_speakers, ts)"
                " VALUES (?,?,?,?,?,0,'[]',?)",
                (room_id, name, json.dumps(["owner", *members], ensure_ascii=False),
                 sm_db, "", time.time()))
            c.commit()
            return {"ok": True, "room": cc.room_of(c, _fetch_room(c, room_id))}
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.get("/api/rooms")
    def list_rooms(auth: dict = Depends(_require_auth)):
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.rooms_of(c, viewer=auth.get("viewer"))
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.get("/api/room/{room_id}/context")
    def room_context(room_id: str, topic_id: str = "", n: int = 40,
                     auth: dict = Depends(_require_auth)):
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.room_context_of(c, room_id, topic_id or None, n, viewer=auth.get("viewer"))
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.post("/api/msg/send")
    async def msg_send(req: Request, auth: dict = Depends(_require_auth)):
        """SendReq → CLI msg send（单入口不绕库）。to 契约允许多值，现投递语义取 to[0]（群发排期后补）。"""
        body = await req.json()
        to_list = body.get("to") or []
        to0 = str(to_list[0]) if isinstance(to_list, list) and to_list else ""
        if not to0 or to0 not in cc.CONTRACT_AGENT_IDS:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": "没有收件人：to[0] 必填契约 AgentId（assistant/dsh/workbuddy/zcode）。"})
        kind = str(body.get("kind") or "msg")
        if kind not in ("msg", "chat", "task", "decision", "system"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "bad kind: " + str(kind)})
        content = str(body.get("content") or "")
        if not content.strip():
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "content required"})
        vis = str(body.get("visibility") or "shared")
        if vis not in ("shared", "private", "shared_with"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "bad visibility"})
        # 契约 system（风讯）↔ 库 KINDS 的 notice：CLI 侧收 notice，读侧 notice→system（cc.to_message）
        kind_db = "notice" if kind == "system" else kind
        viewer = str(auth.get("viewer") or "owner")
        sender = "owner" if (auth.get("admin") or viewer in ("admin", "owner")) else viewer
        if not sender:
            return JSONResponse(status_code=403, content={"ok": False, "code": "forbidden",
                                                            "message": "无可信发送身份"})
        argv = ["msg", "send", "--from", sender, "--to", cc.NS_OF[to0],
                "--text", content, "--kind", kind_db, "--visibility", vis]
        if body.get("room_id"): argv += ["--room", str(body["room_id"])]
        if body.get("topic_id"): argv += ["--topic", str(body["topic_id"])]
        if vis == "shared_with":
            argv += ["--shared-with", json.dumps(body.get("shared_with") or [], ensure_ascii=False)]
        if body.get("deadline_s") is not None:
            try:
                argv += ["--deadline-s", str(int(body["deadline_s"]))]
            except Exception:
                return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "bad deadline_s"})
        res = _run_cli(argv, actor=sender)
        if not res.get("ok"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": res.get("error", "send failed")})
        c = _wconn()
        try:
            row = _fetch_message(c, str(res.get("id") or ""))
            if not row:
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": "written but not found: " + str(res.get("id"))})
            # P5.1-2c 主链：notify 型消息落库后异步触发对端 A2A 上工
            # 2026-09-07 修订：dm:* 单聊且对端有飞书映射时跳过 A2A——身份桥已把先生原话送进飞书，
            # 玄铁在飞书里应答（cockpit_mirror 镜像回流归房）；A2A 再派会让玄铁干两遍、驾驶舱收到
            # 两路回复（A2A 回复 vs 飞书镜像）混淆对话流。
            rid_ = str(body.get("room_id") or "")
            _feishu_mapped = to0 == "assistant"
            if kind in ("task", "chat") and not (_feishu_mapped and rid_.startswith("dm:")):
                _spawn_a2a_worker(str(res.get("id") or ""), to0, content, body.get("room_id"))
            # 飞书身份桥（2026-09-07）：先生在驾驶舱发的消息，以用户本人身份同步进飞书 DM/群。
            # 尽力而为后台线程；只对真人发帖（sender=owner）生效，镜像回流不进这里（防环）。
            if sender == "owner" and kind != "notice":
                try:
                    pass  # 私人飞书桥不属于公共消息投递接口。
                except Exception as _fb_exc:
                    logger.warning("feishu bridge skipped: %s", _fb_exc)
            return {"ok": True, "message": cc.to_message(row, auth.get("viewer"))}
        finally:
            c.close()

    @app.post("/api/task/ack")
    async def task_ack(req: Request, auth: dict = Depends(_require_auth)):
        """双义（Q3）：message_id 命中 messages=差帖回执；命中 widgets=拍板（handled=1 + decision 帖）。"""
        body = await req.json()
        mid = str(body.get("message_id") or "").strip()
        if not mid:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "message_id required"})
        action_id = str(body.get("action_id") or "")
        note = str(body.get("note") or "")
        c = _wconn()
        try:
            row = _fetch_message(c, mid)
            if row:
                c.execute("UPDATE messages SET state=? WHERE id=?", ("ack", mid))
                c.commit()
                # 尾巴④（P5.1-2b）：agent 发来的请求类差帖（from=四灯）→ 回执通知写进它信箱
                # （inbox 兜底通道；A2A 即时 live-session 推送依赖端会话可达性，留给端级注册表）。
                # owner 发出的差帖（to=agent 干活类）不需要回写——活干完 agent 自己会回帖。
                src = str(row.get("from_agent") or "")
                if src in cc.ID_OF_NS:
                    _run_cli(["msg", "send", "--from", "owner", "--to", src, "--text",
                              "差帖已回执（" + str(row.get("id") or "") + "）：" + (note or "已回执"),
                              "--kind", "msg", "--reply-to", mid], actor="owner")
                return {"ok": True, "message": cc.to_message(_fetch_message(c, mid), auth.get("viewer"))}
            w = c.execute("SELECT * FROM widgets WHERE id=? AND handled=0", (mid,)).fetchone()
            if not w:
                return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                                "message": "找不到这条：" + str(mid)})
            wd = dict(w)
            c.execute("UPDATE widgets SET handled=1 WHERE id=?", (mid,))
            c.commit()
            label = ""
            if action_id:
                try:
                    for a in json.loads(wd.get("actions") or "[]"):
                        if str(a.get("id") or "") == action_id:
                            label = str(a.get("label") or "")
                            break
                except Exception:
                    pass
            decided = "就「%s」拍板：%s" % (str(wd.get("title") or ""), label or note or "已处置")
            from_agent = str(wd.get("from_agent") or "dsh")
            if from_agent not in cc.NS_OF:
                from_agent = "dsh"
            res = _run_cli(["msg", "send", "--from", "owner", "--to", cc.NS_OF[from_agent],
                            "--text", decided, "--kind", "decision", "--visibility", "shared"],
                           actor="owner")
            if not res.get("ok"):
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": res.get("error", "widget decision send failed")})
            row2 = _fetch_message(c, str(res.get("id") or ""))
            if not row2:
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": "decision written but not found"})
            return {"ok": True, "message": cc.to_message(row2, auth.get("viewer"))}
        finally:
            c.close()

    @app.post("/api/room/{room_id}/schedule", dependencies=[Depends(_require_auth)])
    async def room_schedule(room_id: str, req: Request):
        """ScheduleReq{mode?, invite?}：契约词→库词，切 mode 走 CLI rooms set；invite 点名。"""
        body = await req.json()
        from memory.chat_scheduler import run_round
        from memory.a2a_client import a2a_call
        c = _wconn()
        try:
            room = _fetch_room(c, room_id)
            if not room:
                return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                                "message": "没有这间房：" + str(room_id)})
            members = [m for m in cc._loads_list(room.get("members")) if str(m) in cc.CONTRACT_AGENT_IDS]
            req_mode = str(body.get("mode") or "").strip()
            invite = [i for i in (body.get("invite") or []) if str(i) in cc.CONTRACT_AGENT_IDS]
            db_mode = str(room.get("mode") or "invite_only")
            if req_mode:
                if req_mode in cc.MODE_OF_CONTRACT:
                    db_mode = cc.MODE_OF_CONTRACT[req_mode]
                elif req_mode in cc.MODE_OF_DB:
                    db_mode = req_mode
                if db_mode != str(room.get("mode") or "invite_only"):
                    setres = _run_cli(["rooms", "set", "--id", room_id, "--mode", db_mode])
                    if not setres.get("ok"):
                        return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                        "message": setres.get("error", "mode set failed")})
            user_msg = " ".join("@" + m for m in invite) if invite else ""
            res = run_round(c, room_id, members, db_mode, user_msg, a2a_call,
                            write_event=_write_event)
            speakers = [m for m in (res.get("speakers") or []) if str(m) in cc.CONTRACT_AGENT_IDS]
            return {"ok": True, "room_id": room_id, "round": int(res.get("round") or 0),
                    "mode": cc.MODE_OF_DB.get(db_mode, "inviteonly"),
                    "order": speakers, "pending_speakers": speakers}
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.post("/api/widget")
    async def widget(req: Request, auth: dict = Depends(_require_auth)):
        """WidgetReq（from/kind/title/body/visibility）→ widgets 表；注入过滤保留（script/on* → 400）。"""
        body = await req.json()
        from_agent = str(body.get("from") or "")
        if from_agent not in cc.CONTRACT_AGENT_IDS:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "from 必填契约 AgentId"})
        if not (auth.get("admin") or auth.get("viewer") in ("admin", "owner")):
            ns_map = {"assistant": "assistant", "dsh": "dsh", "workbuddy": "workbuddy", "zcode": "zcode"}
            if ns_map.get(from_agent) != auth.get("viewer"):
                return JSONResponse(status_code=403, content={"ok": False, "code": "forbidden",
                                                                "message": "角色 token 只能以自己视角发 widget"})
        kind = str(body.get("kind") or "note")
        if kind not in cc.WIDGET_KINDS:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "bad widget kind: " + str(kind)})
        title = str(body.get("title") or "").strip()
        body_txt = str(body.get("body") or "")
        if not title:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "title required"})
        vis = str(body.get("visibility") or "shared")
        if vis not in ("shared", "private", "shared_with"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "bad visibility"})
        _BANNED = re.compile(r"<\s*(script|iframe|object|embed|form)\b", re.I)
        _EVENT = re.compile(r"\son[a-z]+\s*=", re.I)
        payload = title + " " + body_txt
        if _BANNED.search(payload) or _EVENT.search(payload):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": "widget content contains banned tag/event"})
        low = payload.lower()
        if any(k in low for k in ["ignore previous", "system:", "</system>", "forget /all"]):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": "widget content rejected: injection pattern"})
        import hashlib as _hashlib
        actions = json.dumps([{"id": "a-yes", "label": "照办"}, {"id": "a-no", "label": "先搁着"}],
                             ensure_ascii=False) if kind == "ask" else "[]"
        wid = "widget-" + str(int(time.time() * 1000)) + "-" + _hashlib.md5((from_agent + title).encode("utf-8")).hexdigest()[:6]
        c = _wconn()
        try:
            c.execute("INSERT INTO widgets (id, from_agent, kind, title, body, visibility, actions, ts, handled)"
                      " VALUES (?,?,?,?,?,?,?,?,0)",
                      (wid, from_agent, kind, title, body_txt, vis, actions, time.time()))
            c.commit()
            row = c.execute("SELECT * FROM widgets WHERE id=?", (wid,)).fetchone()
            return {"ok": True, "widget": cc.widget_of(dict(row))}
        finally:
            c.close()

    @app.get("/api/graph/memory")
    def graph_memory(auth: dict = Depends(_require_auth)):
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.graph_of(c, viewer=auth.get("viewer"))
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    # ── 投递恢复入口（R03）──────────────────────────
    # app.state 上挂可调用入口：显式调用 = 授权恢复，扫全部到期单（含历史/他人单）。
    # F03：显式入口【不】传 owner_instance —— 那就是"授权显式恢复"的语义；
    # 默认重试线程才必须限本实例（见下）。
    # 库路径在 create_app 时钉死（同 _retry_loop 的理由：调用点可能在别的上下文里）。
    _explicit_db = _delivery_db_path()

    def _run_recovery_all(limit=None):
        return _run_recovery_once(limit=limit, db_path=_explicit_db, owner_instance=None)

    app.state.run_recovery_once = _run_recovery_all

    # ① 失败重试消费者：默认开。还的是**本实例自己欠的** pending 债（F03：必须带实例号过滤，
    #    否则历史/他人单会被这条默认管道抽走外发）。有界：attempts 由持久计数封顶。
    #    XTMEM_A2A_RETRY=0 可关。库路径启动即钉死，随 app 关闭收工（见 _retry_loop ⚠️）。
    _retry_stop = threading.Event()
    app.state._retry_stop = _retry_stop
    if os.environ.get("XTMEM_A2A_RETRY", "1") != "0":
        app.state._retry_thread = threading.Thread(
            target=_retry_loop, args=(_retry_stop, _delivery_db_path(), _instance_id), daemon=True)
        app.state._retry_thread.start()

    # ② 跨进程恢复扫描：默认 off，XTMEM_A2A_RESUME=1 才开（替别的进程接单属上线授权）。
    #    它才是"接历史单"的正规入口，所以 owner_instance=None（扫全部）。
    _pinned_db = _delivery_db_path()

    def _resume_loop():
        interval = float(os.environ.get("XTMEM_A2A_RESUME_INTERVAL_S", "10"))
        while not _retry_stop.is_set():
            try:
                if _retry_stop.wait(max(1.0, interval)):
                    return
                _run_recovery_once(db_path=_pinned_db, owner_instance=None)
            except Exception as exc:
                emit('rest', 'delivery.resume', 'failed', error=exc)

    if os.environ.get("XTMEM_A2A_RESUME") == "1":
        threading.Thread(target=_resume_loop, daemon=True).start()

    @app.on_event("shutdown")
    def _stop_retry_loop():
        """app 退出即停重试线程：否则 daemon 线程会跨测试/跨进程活到下个库上（⚠️ 见 _retry_loop）。"""
        _retry_stop.set()

    # ── 静态文件（dashboard） ──────────────────────────
    # 注意：mount 放在最后，FastAPI 路由优先级高于 mount

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def start_server(context_builder=None, vector_store=None, host="127.0.0.1", port=20000):
    """启动 REST 观测服务（阻塞）。非本机绑定必须配 XTMEM_REST_TOKEN，否则拒绝启动。"""
    app = create_app(context_builder, vector_store, host=host)
    logger.info("MIMOX REST API starting on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def start_server_background(context_builder=None, vector_store=None, host="127.0.0.1", port=20000):
    """后台线程启动 REST 观测服务"""
    import socket
    import threading

    # ⚠️ 2026-08-16 修「抢 20000 端口」：启动前先探测端口是否已被占用
    # （gateway/MIMOX API 常驻 20000）。被占用时直接跳过、不启动线程，
    # 避免 uvicorn 在后台线程里抛 WinError 10048 刷 traceback——
    # REST 只是观测面板，缺了不影响记忆功能。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                logger.warning(
                    "MIMOX REST API not started: %s:%d already in use (skipping)",
                    host, port,
                )
                return None
    except OSError:
        pass  # 探测失败时照常尝试启动，让 uvicorn 自己报错

    app = create_app(context_builder, vector_store, host=host)

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="mimox-rest-api")
    t.start()
    logger.info("MIMOX REST API started on %s:%d (background)", host, port)
    return t


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MIMOX REST API Server")
    parser.add_argument("--port", type=int, default=20000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-rest", action="store_true", help="Disable REST API (headless mode)")
    args = parser.parse_args()

    if args.no_rest:
        print("REST API disabled (--no-rest)")
    else:
        logging.basicConfig(level=logging.INFO)
        start_server(host=args.host, port=args.port)
