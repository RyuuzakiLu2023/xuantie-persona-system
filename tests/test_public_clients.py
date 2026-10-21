# -*- coding: utf-8 -*-
# ================================================================
# test_public_clients.py · 公开接头自测（ZCode MCP + hooks）
#
# 这是什么：对「公开三端接头」做黑盒验证——不导入宿主、不连真实 MCP 客户端，
#   只把接头脚本当命令行程序跑，用合成库断言协议与权限行为。
#
# 技术栈：pytest + subprocess，纯标准库。需要本机已 `pip install -e .`（或 wheel），
#   因为被测 MCP 默认走 `python -m xtmem`。
#
# 覆盖：
#   MCP  initialize 版本协商 / ping / tools/list 八工具 / 未知方法报错
#   MCP  tools/call：store → recall → lookup_id 全链路
#   MCP  权限拒绝：跨域写、非 cross 跨域读、未 confirm 的 forget/msg_inbox
#   MCP  forget 真正落库（confirm=true 转 --confirm）
#   MCP  stdout 只出 JSON-RPC 帧（坏字节行被丢弃且不污染后续帧）
#   hook SessionStart / UserPromptSubmit / Stop 三事件的输出形状
#
# 铁律：
# 1. 每个用例一个临时库（tmp_path）；【不】碰 XTMEM_DB_PATH 指向的真实库。
# 2. 身份用合成的 role-a / role-b，不出现任何真实角色名。
# 3. 断言"失败也要是合法 JSON-RPC 帧"——协议坏掉比业务失败更难查。
#
# 跑法：
#   pytest tests/test_public_clients.py -v
#   XTMEM_CLI_AVAILABLE=0 pytest tests/test_public_clients.py -v   # 无 CLI 时只跑协议部分
# ================================================================
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MCP = REPO / "src" / "zcode" / "mcp" / "xtmem_mcp.py"
HOOK = REPO / "src" / "zcode" / "hooks" / "xtmem_hook.py"
PY = sys.executable


def _cli_available() -> bool:
    try:
        proc = subprocess.run([PY, "-m", "xtmem", "--help"], capture_output=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


CLI_READY = _cli_available()
requires_cli = pytest.mark.skipif(not CLI_READY, reason="xtmem CLI not installed (python -m xtmem)")

CHECKS = {
    "mcp_init": False, "mcp_tools": False, "mcp_call": False,
    "hook_inject": False, "hook_stop": False,
}


def _env(tmp_path: Path, actor: str = "role-a") -> dict:
    env = dict(os.environ)
    env.update({
        "XTMEM_ACTOR": actor,
        "XTMEM_DB_PATH": str(tmp_path / "vector_store.db"),
        "XTMEM_PYTHON": PY,
        "XTMEM_TIMEOUT_S": "45",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    env.pop("XTMEM_SCRIPT", None)
    env.pop("XTMEM_ADMIN", None)
    env.pop("XTMEM_ADMIN_TOKEN", None)
    env.pop("XTMEM_SERVE_TOKEN", None)
    return env


def mcp_roundtrip(env: dict, frames: list) -> list:
    """把若干 JSON-RPC 帧喂给 MCP，返回解析后的响应列表。"""
    stdin = "\n".join(f if isinstance(f, str) else json.dumps(f, ensure_ascii=False)
                      for f in frames) + "\n"
    proc = subprocess.run([PY, str(MCP)], input=stdin.encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]
    out = proc.stdout.decode("utf-8").strip()
    result = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        result.append(json.loads(line))  # 非 JSON 行会在这里炸——协议污染必须暴露
    return result


def call_payload(resp: dict) -> dict:
    text = resp["result"]["content"][0]["text"]
    return json.loads(text)


def hook_run(env: dict, event: str, payload: dict) -> dict:
    proc = subprocess.run([PY, str(HOOK), event],
                          input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    out = proc.stdout.decode("utf-8").strip()
    return json.loads(out or "{}")


# ---------------------------------------------------------------
# 协议层（不需要 CLI）
# ---------------------------------------------------------------
def test_mcp_handshake_and_tools(tmp_path):
    rs = mcp_roundtrip(_env(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    ])
    assert rs[0]["result"]["serverInfo"]["name"] == "xtmem"
    assert rs[0]["result"]["protocolVersion"] == "2025-06-18"
    assert rs[1]["result"] == {}
    names = {t["name"] for t in rs[2]["result"]["tools"]}
    assert names == {
        "xtmem_recall", "xtmem_store", "xtmem_lookup_cross", "xtmem_lookup_id",
        "xtmem_msg_recent", "xtmem_msg_inbox", "xtmem_agents", "xtmem_forget",
    }
    CHECKS["mcp_init"] = True
    CHECKS["mcp_tools"] = True


def test_mcp_unknown_protocol_falls_back(tmp_path):
    rs = mcp_roundtrip(_env(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        {"jsonrpc": "2.0", "id": 2, "method": "unknown/method"},
    ])
    assert rs[0]["result"]["protocolVersion"] in ("2025-06-18", "2025-03-26", "2024-11-05")
    assert rs[1]["error"]["code"] == -32601


def test_mcp_drops_bad_bytes_without_breaking_stream(tmp_path):
    """坏字节行被丢弃，后续帧照常应答——stdout 不允许出现半帧垃圾。"""
    good = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    stdin = b"\xff\xfe{not json}\n" + good.encode() + b"\n"
    proc = subprocess.run([PY, str(MCP)], input=stdin, capture_output=True, timeout=30,
                          env=_env(tmp_path))
    assert proc.returncode == 0
    lines = [l for l in proc.stdout.decode("utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == 9


def test_mcp_refuses_admin_identity(tmp_path):
    env = _env(tmp_path, actor="owner")
    proc = subprocess.run([PY, str(MCP)], input=b"", capture_output=True, timeout=30, env=env)
    assert proc.returncode == 2
    assert b"not allowed" in proc.stderr


def test_mcp_confirm_required_before_any_cli_call(tmp_path):
    """破坏性工具未传 confirm 时必须在本地拒绝，不产生任何子进程副作用。"""
    rs = mcp_roundtrip(_env(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_forget", "arguments": {"id": "mem-00000000"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "xtmem_msg_inbox", "arguments": {}}},
    ])
    assert rs[0]["result"]["isError"] is True
    assert "confirm=true" in call_payload(rs[0])["error"]
    assert rs[1]["result"]["isError"] is True


# ---------------------------------------------------------------
# 全链路（需要已安装 CLI）
# ---------------------------------------------------------------
@requires_cli
def test_store_recall_lookup_and_isolation(tmp_path):
    env = _env(tmp_path)
    rs = mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_store",
                    "arguments": {"text": "role-a build uses python -m xtmem",
                                  "importance": 0.8, "tags": "bootstrap"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "xtmem_recall",
                    "arguments": {"query": "build python", "top": 5}}},
    ])
    stored = call_payload(rs[0])
    assert stored["ok"] is True and stored["namespace"] == "role-a"
    mem_id = stored["id"]

    recalled = call_payload(rs[1])
    assert recalled["ok"] is True
    assert any(it["id"] == mem_id for it in recalled["items"])
    CHECKS["mcp_call"] = True

    # lookup_id 精确回查
    rs2 = mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "xtmem_lookup_id", "arguments": {"id": mem_id}}},
    ])
    assert call_payload(rs2[0])["count"] == 1

    # 写别人域必须拒绝（身份只认 XTMEM_ACTOR）
    rs3 = mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "xtmem_store",
                    "arguments": {"text": "should be denied", "namespace": "role-b"}}},
    ])
    assert rs3[0]["result"]["isError"] is True
    assert "role-a" in call_payload(rs3[0])["error"]

    # 非 cross 查别人域必须拒绝
    rs4 = mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "xtmem_recall",
                    "arguments": {"query": "x", "namespace": "role-b"}}},
    ])
    assert rs4[0]["result"]["isError"] is True


@requires_cli
def test_forget_confirm_actually_removes(tmp_path):
    env = _env(tmp_path)
    first = call_payload(mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_store",
                    "arguments": {"text": "temporary fact to forget", "importance": 0.3}}},
    ])[0])
    mem_id = first["id"]

    removed = call_payload(mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "xtmem_forget",
                    "arguments": {"id": mem_id, "confirm": True}}},
    ])[0])
    assert removed["ok"] is True
    assert removed.get("removed") == 1


@requires_cli
def test_hooks_three_events(tmp_path):
    env = _env(tmp_path)
    # 先经 MCP 落一条，保证 hook 有料可召回
    mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_store",
                    "arguments": {"text": "role-a deployment uses local embedding service",
                                  "importance": 0.8}}},
    ])

    start = hook_run(env, "SessionStart", {})
    assert "additionalContext" in start

    prompt = hook_run(env, "UserPromptSubmit", {"prompt": "deployment embedding service"})
    ctx = prompt.get("additionalContext", "")
    assert "XTMEM" in ctx and "[m:mem-" in ctx
    CHECKS["hook_inject"] = True

    stop = hook_run(env, "Stop", {"response": "决定了：以后部署一律用 python -m xtmem 入口。"})
    assert stop == {"additionalContext": ""}
    CHECKS["hook_stop"] = True


@requires_cli
def test_hook_stop_gate_rejects_chitchat(tmp_path):
    """短寒暄不应该写库（门控 fail-open 但长度闸仍在）。"""
    env = _env(tmp_path)
    hook_run(env, "Stop", {"response": "好的"})
    rs = mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_lookup_cross", "arguments": {"top": 20}}},
    ])
    items = call_payload(rs[0]).get("items") or []
    assert not any(str(it.get("content", "")).strip() == "好的" for it in items)


# ---------------------------------------------------------------
# 库路径劫持防护：显式 XTMEM_DB_PATH 不能被宿主 home 环境顶掉
# ---------------------------------------------------------------
def _hostile_home(tmp_path: Path) -> Path:
    """造一个"恶意"宿主 profile：里面的 xtmem.yaml 指向 guard.db。

    用于验证：即使进程环境里带着 HERMES_HOME/MIMOX_HOME，显式库也必须赢。
    """
    home = tmp_path / "hostile-home"
    home.mkdir()
    (home / "xtmem.yaml").write_text(
        "db_path: %s\n" % (tmp_path / "guard.db").as_posix(), encoding="utf-8")
    return home


@pytest.mark.parametrize("home_key", ["HERMES_HOME", "MIMOX_HOME", "XTMEM_HERMES_HOME"])
@requires_cli
def test_explicit_db_survives_hostile_home_env(tmp_path, home_key):
    env = _env(tmp_path)
    home = _hostile_home(tmp_path)
    env[home_key] = str(home)

    payload = call_payload(mcp_roundtrip(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "xtmem_store",
                    "arguments": {"text": "guard-test fact", "importance": 0.6}}},
    ])[0])
    assert payload["ok"] is True, payload

    # 显式库有数据
    import sqlite3
    target = tmp_path / "vector_store.db"
    assert target.exists(), "explicit database was never created"
    with sqlite3.connect(str(target)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n >= 1

    # 被劫持的库必须一个字节都没写（文件都不该存在）
    assert not (tmp_path / "guard.db").exists(), "hostile host config captured the write"


@requires_cli
def test_hook_explicit_db_survives_hostile_home_env(tmp_path):
    env = _env(tmp_path)
    env["HERMES_HOME"] = str(_hostile_home(tmp_path))
    hook_run(env, "Stop", {"response": "决定了：hook 显式库写入测试，长度足够通过门控。"})
    assert (tmp_path / "vector_store.db").exists()
    assert not (tmp_path / "guard.db").exists()
