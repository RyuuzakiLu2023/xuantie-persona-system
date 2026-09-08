"""MIMOX REST 观测层 — FastAPI 服务

提供系统健康、记忆分轨、演化关系、调用统计等只读端点。
热路径（store/prefetch）不经过此服务，永远在进程内直接调用。

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
from typing import Optional
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
# P1-10 鉴权 token：XTMEM_REST_TOKEN 环境变量。未设置且绑非本机地址 → 拒绝启动。
_AUTH_TOKEN = os.environ.get("XTMEM_REST_TOKEN", "").strip()


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _require_auth(x_xtmem_token: Optional[str] = Header(default=None)) -> None:
    """数据接口鉴权：配置了 token 就必须带对（X-XTMEM-Token 头）。

    未配置 token 时放行——非 loopback 绑定且无 token 已在 create_app 层拒绝启动。
    """
    if not _AUTH_TOKEN:
        return
    if x_xtmem_token != _AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized: X-XTMEM-Token required")


def _db_path():
    """P5.1-2（2026-09-02）：共享库路径——env 优先，否则 mimox-data 根 .hermes。"""
    p = os.environ.get("XTMEM_DB_PATH", "").strip()
    if p:
        return p
    home = os.environ.get("MIMOX_HOME", r"<XT_ROOT>\mimox-data")
    return os.path.join(home, ".hermes", "memory", "vector_store.db")


def _ro_conn():
    """只读连接（总则 5：探查一律只读）。"""
    db = _db_path()
    if not os.path.exists(db):
        raise RuntimeError("XTMEM DB not found: " + db)
    c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _run_cli(argv):
    """写路径走 CLI 同一入口（message send / rooms / schedule），不绕库直写。"""
    py = os.environ.get("XTMEM_PYTHON", sys.executable)
    script = os.environ.get("XTMEM_SCRIPT", r"<XT_ROOT>\mimox-data\xtmem\xtmem.py")
    env = dict(os.environ)
    try:
        proc = subprocess.run([py, script, *argv], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False, env=env)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": (proc.stdout or proc.stderr or "")[:300]}


def create_app(context_builder=None, vector_store=None, host: str = "127.0.0.1") -> "FastAPI":
    """创建 FastAPI 应用，注入 ContextBuilder 和 VectorStore 实例（P1-10 鉴权）。"""
    if not HAS_FASTAPI:
        raise RuntimeError("FastAPI is required for MIMOX REST API")

    if not _is_loopback(host) and not _AUTH_TOKEN:
        raise RuntimeError(
            "REST API refuses non-loopback binding (%s) without XTMEM_REST_TOKEN "
            "env var — set the token or bind 127.0.0.1" % host
        )

    app = FastAPI(title="MIMOX Memory API", version="1.0")

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
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

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

            return {
                "track_distribution": track_dist,
                "evolution": evolution,
                "active_count": active,
                "superseded_count": superseded,
                "superseded_ratio": round(superseded / total, 3) if total > 0 else 0,
                "recent_7d_growth": recent,
            }
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

        conn = sqlite3.connect(db_path)
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

        return {"ok": True, "chunk_id": chunk_id, "updated": body}

    # ── P5.1-2 驾驶舱后端（2026-09-02，P5.1-2b 契约缝合版）────────────────
    # 八端点按照渊台契约为准组装（形状唯一出处 <COCKPIT_ROOT>\\src\\api\\contract.ts +
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
        """写路径连接（row_factory 与 _ro_conn 同款，响应组装可复用）。"""
        c = sqlite3.connect(_db_path())
        c.row_factory = sqlite3.Row
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
            c = sqlite3.connect(_db_path())
            c.execute("INSERT INTO xtmem_events (ts, kind, agent, meta) VALUES (?,?,?,?)",
                      (time.time(), kind, agent, json.dumps(meta or {}, ensure_ascii=False)))
            c.commit()
            c.close()
        except Exception:
            pass

    @app.get("/api/cockpit/state", dependencies=[Depends(_require_auth)])
    def cockpit_state():
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.cockpit_state(c, _probe_agents())
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.get("/api/rooms", dependencies=[Depends(_require_auth)])
    def list_rooms():
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.rooms_of(c)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.get("/api/room/{room_id}/context", dependencies=[Depends(_require_auth)])
    def room_context(room_id: str, topic_id: str = "", n: int = 40):
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.room_context_of(c, room_id, topic_id or None, n)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

    @app.post("/api/msg/send", dependencies=[Depends(_require_auth)])
    async def msg_send(req: Request):
        """SendReq → CLI msg send（单入口不绕库）。to 契约允许多值，现投递语义取 to[0]（群发排期后补）。"""
        body = await req.json()
        to_list = body.get("to") or []
        to0 = str(to_list[0]) if isinstance(to_list, list) and to_list else ""
        if not to0 or to0 not in cc.CONTRACT_AGENT_IDS:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": "没有收件人：to[0] 必填契约 AgentId（xuantie/dsh/workbuddy/zcode）。"})
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
        argv = ["msg", "send", "--from", "ryuuzaki", "--to", cc.NS_OF[to0],
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
        res = _run_cli(argv)
        if not res.get("ok"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape",
                                                            "message": res.get("error", "send failed")})
        c = _wconn()
        try:
            row = _fetch_message(c, str(res.get("id") or ""))
            if not row:
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": "written but not found: " + str(res.get("id"))})
            return {"ok": True, "message": cc.to_message(row)}
        finally:
            c.close()

    @app.post("/api/task/ack", dependencies=[Depends(_require_auth)])
    async def task_ack(req: Request):
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
                # ryuuzaki 发出的差帖（to=agent 干活类）不需要回写——活干完 agent 自己会回帖。
                src = str(row.get("from_agent") or "")
                if src in cc.ID_OF_NS:
                    _run_cli(["msg", "send", "--from", "ryuuzaki", "--to", src, "--text",
                              "差帖已回执（" + str(row.get("id") or "") + "）：" + (note or "已回执"),
                              "--kind", "msg", "--reply-to", mid])
                return {"ok": True, "message": cc.to_message(_fetch_message(c, mid))}
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
            res = _run_cli(["msg", "send", "--from", "ryuuzaki", "--to", cc.NS_OF[from_agent],
                            "--text", decided, "--kind", "decision", "--visibility", "shared"])
            if not res.get("ok"):
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": res.get("error", "widget decision send failed")})
            row2 = _fetch_message(c, str(res.get("id") or ""))
            if not row2:
                return JSONResponse(status_code=500, content={"ok": False, "code": "server_error",
                                                                "message": "decision written but not found"})
            return {"ok": True, "message": cc.to_message(row2)}
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

    @app.post("/api/widget", dependencies=[Depends(_require_auth)])
    async def widget(req: Request):
        """WidgetReq（from/kind/title/body/visibility）→ widgets 表；注入过滤保留（script/on* → 400）。"""
        body = await req.json()
        from_agent = str(body.get("from") or "")
        if from_agent not in cc.CONTRACT_AGENT_IDS:
            return JSONResponse(status_code=400, content={"ok": False, "code": "bad_shape", "message": "from 必填契约 AgentId"})
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

    @app.get("/api/graph/memory", dependencies=[Depends(_require_auth)])
    def graph_memory():
        try:
            c = _ro_conn()
        except Exception as e:
            return JSONResponse(status_code=503, content={"ok": False, "code": "server_error", "message": str(e)})
        try:
            return cc.graph_of(c)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "code": "server_error", "message": str(e)})
        finally:
            c.close()

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