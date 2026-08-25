"""MIMOX REST 观测层 — FastAPI 服务

提供系统健康、记忆分轨、演化关系、调用统计等只读端点。
热路径（store/prefetch）不经过此服务，永远在进程内直接调用。

启动方式：
    python -m memory.api_server --port 20000
    或在 agent_init.py 里后台线程启动
"""

import os
import time
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI
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


def create_app(context_builder=None, vector_store=None) -> "FastAPI":
    """创建 FastAPI 应用，注入 ContextBuilder 和 VectorStore 实例"""
    if not HAS_FASTAPI:
        raise RuntimeError("FastAPI is required for MIMOX REST API")

    app = FastAPI(title="MIMOX Memory API", version="1.0")

    # ── 健康检查 ──────────────────────────────────────

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": "mimox-1.0",
            "uptime_seconds": int(time.time() - _start_time),
        }

    # ── 记忆健康报告 ──────────────────────────────────

    @app.get("/api/memory/health")
    def memory_health():
        # 优先用 vector_store 实例，否则降级读 SQLite
        conn = None
        if vector_store:
            conn = vector_store._get_conn()
        else:
            db_path = _get_db_path()
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

    @app.get("/api/stats")
    def stats():
        if context_builder:
            return {
                "store_count_total": getattr(context_builder, '_store_count', 0),
                "prefetch_count_total": getattr(context_builder, '_prefetch_count', 0),
                "probe_ok_count": getattr(context_builder, '_probe_ok_count', 0),
                "probe_fail_count": getattr(context_builder, '_probe_fail_count', 0),
                "timeline": getattr(context_builder, '_timeline', [])[-96:],
            }

        # 降级：context_builder 不可用时返回基本统计
        db_path = _get_db_path()
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

    @app.get("/search_trace")
    def search_trace(limit: int = 20):
        if not context_builder:
            return JSONResponse(status_code=503, content={"error": "ContextBuilder not available"})

        timeline = getattr(context_builder, '_timeline', [])
        return {"traces": timeline[-limit:]}

    # ── 手动修复 ──────────────────────────────────────

    @app.patch("/memory/{chunk_id}")
    def update_memory(chunk_id: str, body: dict):
        if not vector_store:
            return JSONResponse(status_code=503, content={"error": "VectorStore not available"})

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

    # ── 静态文件（dashboard） ──────────────────────────
    # 注意：mount 放在最后，FastAPI 路由优先级高于 mount

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def start_server(context_builder=None, vector_store=None, host="127.0.0.1", port=20000):
    """启动 REST 观测服务（阻塞）"""
    app = create_app(context_builder, vector_store)
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

    app = create_app(context_builder, vector_store)

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
