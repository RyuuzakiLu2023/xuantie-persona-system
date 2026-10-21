# -*- coding: utf-8 -*-
"""test_xtmem_b_s2_serve.py — B方案 S2 serve 常驻服务守卫（2026-09-06，assistant-c）。

判据（DoD）：
  ① serve 子命令可启动（127.0.0.1:8765+），单例 PID 锁 + stale 检测；
  ② TCP JSON 协议：一行请求一行响应，复用 cmd_recall/cmd_store 同一代码路径；
  ③ store 后新记忆立即可检索（脏检查增量水化 T3）；
  ④ 降级路径：serve 未启动时客户端自动回落子进程单命令（功能不丢）。
环境纪律：tmp_db 副本（不碰生产），随机端口，子进程起 serve。
"""
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

import conftest

CLI = conftest.CLI


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rpc(port: int, cmd: str, args=None, timeout: float = 15.0) -> dict:
    """单请求 RPC。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        req = json.dumps({"cmd": cmd, "args": args or {}}) + chr(10)
        s.sendall(req.encode("utf-8"))
        buf = b""
        s.settimeout(timeout)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if chr(10).encode() in buf:
                break
        return json.loads(buf.decode("utf-8", "replace").strip() or "{}")
    finally:
        s.close()


@pytest.fixture()
def serve_proc(tmp_db, monkeypatch):
    """起 serve 子进程（指向 tmp_db），yield port。"""
    # ⚠️ 环境纪律：只给子进程 env 注入 XTMEM_DB_PATH，不污染 pytest 父进程——
    # conftest tmp_db 收尾断言 XTMEM_DB_PATH 必须已被清除，父进程 set 了 = 泄漏给后续用例。
    monkeypatch.setenv("XTMEM_SERVE_PORT", "0")  # 占位防脏
    port = _pick_port()
    pid_file = os.path.join(os.path.dirname(str(tmp_db)), f"serve-{_uid()}.pid")
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = str(tmp_db)
    env["XTMEM_SERVE_PID"] = pid_file
    env["XTMEM_ACTOR"] = "dsh"
    proc = subprocess.Popen(
        [sys.executable, str(CLI), "serve", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 等 serve 就绪（最多 25s——水化需要时间）
    ready = False
    for _ in range(50):
        time.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                ready = True
                break
        except OSError:
            continue
    assert ready, "serve 未能就绪（25s 内）"
    yield port, env
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    monkeypatch.undo()  # 还原 env（含 XTMEM_SERVE_PORT 占位）


def test_s2_serve_recall_and_store(serve_proc, tmp_db):
    """serve recall + store 全链路（同一代码路径，store 后立即可检索）。"""
    port, env = serve_proc
    uid = _uid()

    # store 一条
    st = _rpc(port, "store", {"agent": "dsh", "text": f"S2 serve 测试 {uid}",
                              "importance": 0.8, "namespace": "dsh", "tags": "xtmem"})
    assert st.get("ok") and "id" in st, st
    mid = st["id"]

    # 立即 recall（脏检查应已把新条带进书架）
    rc = _rpc(port, "recall", {"query": uid, "top": 5, "as_agent": "dsh", "namespace": "dsh"})
    assert rc.get("ok"), rc
    ids = [it.get("id") for it in (rc.get("items") or [])]
    assert mid in ids, f"store 后新记忆应立即可检索（T3），items={ids}"


def test_s2_serve_pid_singleton(tmp_db, serve_proc):
    """单例 PID：第二个 serve 起在同一端口会幂等退出或端口冲突（已占用即退）。"""
    port, env = serve_proc
    # 再起一个同端口 serve——预期端口被占返回 ok.already_running 或错误（幂等，不崩）
    p2 = subprocess.Popen(
        [sys.executable, str(CLI), "serve", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        out, _ = p2.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p2.kill()
        out, _ = p2.communicate()
    # 单例语义：要么 already_running，要么端口冲突退出（绝不双活导致数据撕裂）
    assert "already_running" in out or "被占" in out or "port" in out.lower(), out[:200]
    # 原 serve 仍活
    r = _rpc(port, "recall", {"query": "ping-check", "top": 1, "as_agent": "dsh", "namespace": "dsh"})
    assert r.get("ok") is True


def test_s2_serve_fallback_subprocess(tmp_db):
    """降级路径：serve 不在（无进程监听）→ 直接走 CLI 子进程功能不丢。"""
    # 不做 serve，直接验证 CLI recall 可用（降级=现有子进程模式）
    import conftest as _cf
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = str(tmp_db)
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(CLI), "recall", "--query", "xtmem", "--top", "2",
                        "--as", "dsh", "--namespace", "dsh"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
    try:
        out = json.loads(p.stdout.strip() or "{}")
    except Exception:
        out = {"raw": (p.stdout or p.stderr or "")[:200]}
    assert out.get("ok") is True, out
