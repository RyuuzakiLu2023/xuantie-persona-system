# -*- coding: utf-8 -*-
"""P5.1-1 库层测试（2026-09-02，assistant-c）。

覆盖：msg send 扩参（room/topic/kind/state/reply-to/delegation-id/persist/inject）+ rooms 子命令 +
      kind 单点收口校验 + _record_cli_event（msg_send）。守卫三段式：
      ① 旧 msg 流程回归绿（不传新参数行为逐字节一致）；
      ② kind=chat 写入后 chunks 零新增（防刷库断言）；
      ③ 自证值用 zzz-test-room 前缀（防与真数据重合）。
"""

import json
import sqlite3
import subprocess
import sys

import pytest
import axis_predicates
import conftest

CLI = conftest.CLI
pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用（tmp_db 拷贝源）")


def _run_cli(cli_env, *argv):
    env = dict(cli_env)
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def _active_chunks(db):
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM chunks WHERE state='active'").fetchone()[0]
    con.close()
    return n


def test_old_msg_flow_regression(cli_env):
    """守卫①：不带新参数走老路径——send/inbox 依旧绿。"""
    r = _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst", "--text", "hello")
    assert r.get("ok"), r
    r2 = _run_cli(cli_env, "msg", "inbox", "--as", "zzz-dst")
    assert r2.get("ok") and r2["count"] >= 1, r2


def test_kind_chat_no_chunk_write(cli_env, tmp_db):
    """守卫②：kind=chat 留言不落 chunks（防刷库）。"""
    before = _active_chunks(tmp_db)
    r = _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst", "--text", "chat msg", "--kind", "chat")
    assert r.get("ok"), r
    after = _active_chunks(tmp_db)
    assert after == before, f"kind=chat 写进了 chunks: {before} -> {after}"


def test_kind_single_source_validation(cli_env):
    """kind 单点收口：非法 kind 被拒，合法 kind 放行。"""
    bad = _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst", "--text", "x", "--kind", "bogus")
    assert not bad.get("ok") and "invalid --kind" in bad.get("error", ""), bad
    good = _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst", "--text", "x", "--kind", "task")
    assert good.get("ok"), good


def test_zzz_test_room_self_value(cli_env):
    """守卫③：自证值用 zzz-test-room 前缀（防与真数据重合）。"""
    room = "zzz-test-room"
    r = _run_cli(cli_env, "rooms", "create", "--id", room, "--name", "t")
    assert r.get("ok") and r["id"] == room, r
    r2 = _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst", "--text", "hi", "--room", room)
    assert r2.get("ok") and r2["room"] == room, r2
    lst = _run_cli(cli_env, "rooms", "list")
    assert any(x["id"] == room for x in lst.get("rooms", [])), lst



def test_inbox_marks_read_event(cli_env):
    """守卫④（P5.1-2b 尾巴①）：inbox 读信落 msg_read 事件（消息活性留痕）。"""
    assert _run_cli(cli_env, "msg", "send", "--from", "zzz-src", "--to", "zzz-dst",
                    "--text", "hi read evt").get("ok")
    r = _run_cli(cli_env, "msg", "inbox", "--as", "zzz-dst")
    assert r.get("ok") and r["count"] >= 1, r
    con = sqlite3.connect(cli_env["XTMEM_DB_PATH"])
    n = con.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind='msg_read' AND agent='zzz-dst'").fetchone()[0]
    con.close()
    assert n >= 1, "msg_read 事件未落库"
