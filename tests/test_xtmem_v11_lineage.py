# -*- coding: utf-8 -*-
"""test_xtmem_v11_lineage.py — V1.1 反向血缘守卫（#6，v1.3.2 账本，2026-09-05，assistant-c）。

判据（DoD）：
  ① 人造「源 private + 派生 shared」样本 → 收紧/归档源 → 派生必须跟随（stale 标记或 vis 降）；
  ② forget 源 → 派生打 lineage-stale:<src8> 标记 + 事件留痕；
  ③ 源 superseded（演化覆盖）不触发 stale（聚合型派生引多代 superseded 是设计）；
  ④ invalidate_evolution 零改动（只管演化边）。
"""
import json
import os
import sqlite3
import sys
import subprocess
import uuid

import pytest

import conftest
from memory.vector_store import VectorStore

CLI = conftest.CLI


def _uid() -> str:
    """每用例唯一后缀：防 store 内容去重（canonical 副本可能已含相同文本 → skipped 无 id）。"""
    return uuid.uuid4().hex[:10]


def _run_cli(cli_env, *argv):
    env = dict(cli_env)
    # 防 pytest 进程残留 XTMEM_ACTOR/ADMIN 泄漏给子进程（全量跑时早期用例会 set 且未清干净
    # → store --agent dsh 撞残留 actor 报"只能写本域"）。子进程身份一律显式传参。
    env.pop("XTMEM_ACTOR", None)
    env.pop("XTMEM_ADMIN", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "out": (p.stdout or "")[:200]}


def _tags_of(db, cid):
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM chunks WHERE id=?", (cid,)).fetchone()
    con.close()
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


def test_v11_forget_source_marks_derived_stale(tmp_db, cli_env):
    """forget 源 chunk → 指向它的 active 派生打 lineage-stale 标记 + 事件留痕。"""
    # 造源（raw）与派生（distill，source_ids=源）
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 测试源记忆 用户A拍板 {_uid()}")
    assert src.get("ok"), src
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 测试蒸馏产物 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    assert der.get("ok"), der
    der_id = der["id"]

    # forget 源（--as dsh 身份：store 的 namespace 是 dsh）
    fr = _run_cli(cli_env, "forget", "--as", "dsh", "--id", src_id, "--confirm")
    assert fr.get("ok"), fr

    # 派生 tags 含 lineage-stale
    tags = _tags_of(tmp_db, der_id)
    assert any(t.startswith("lineage-stale:") for t in tags), f"派生应打 stale 标记，实际 {tags}"

    # 事件留痕
    con = sqlite3.connect(str(tmp_db))
    ev = con.execute("SELECT meta FROM xtmem_events WHERE kind='lineage-stale' ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    assert ev, "必须有 lineage-stale 事件"


def test_v11_derived_vis_follows_source_private(tmp_db, cli_env):
    """源 private + 派生 shared（历史漏洞面）→ follow_source_visibility 降派生 vis。"""
    # 直接 SQL 造漏洞样本：源 private，派生 shared（绕开 min_visibility 的写时保护）
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                "VALUES ('mem-v11aaa1', '私有源内容', 1700000000, 0.5, '[]', 'default', 'active', 'dsh', 'private', 'raw')")
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from, source_ids) "
                "VALUES ('mem-v11bbb2', '共享派生泄漏', 1700000001, 0.5, '[]', 'default', 'active', 'dsh', 'shared', 'refined', 'distill', ?)",
                (json.dumps(["mem-v11aaa1"]),))
    con.commit()
    con.close()

    vs = VectorStore(db_path=str(tmp_db))
    changed = vs.follow_source_visibility("mem-v11aaa1", "private")
    vs.close()
    assert changed == 1, f"应降 1 个派生 vis，实际 {changed}"
    con = sqlite3.connect(str(tmp_db))
    vis = con.execute("SELECT visibility FROM chunks WHERE id='mem-v11bbb2'").fetchone()[0]
    con.close()
    assert vis == "private", f"派生 vis 应跟随降为 private，实际 {vis}"


def test_v11_superseded_source_not_stale(tmp_db, cli_env):
    """源 superseded（演化正常覆盖）不触发 stale——聚合型派生引多代 superseded 是设计。"""
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 被覆盖源 {_uid()}")
    assert src.get("ok"), src
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 聚合派生 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    der_id = der["id"]
    # mark_superseded 模拟演化覆盖（源被新 chunk 取代）
    vs = VectorStore(db_path=str(tmp_db))
    new_id = "mem-v11ccc3"
    con = sqlite3.connect(str(tmp_db))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                "VALUES (?, '新版本', 1700000002, 0.5, '[]', 'default', 'active', 'dsh', 'shared', 'raw')", (new_id,))
    con.commit(); con.close()
    vs.mark_superseded(src_id, new_id)
    vs.close()
    # 派生不应被打 stale（superseded 源不触发）
    tags = _tags_of(tmp_db, der_id)
    assert not any(t.startswith("lineage-stale:") for t in tags), f"superseded 源不应触发 stale，实际 {tags}"


def test_v11_vectorstore_propagate_method(tmp_db, cli_env):
    """VectorStore.propagate_source_archived 公共方法（api_server 等调用方路径）直接可用。"""
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 方法测试源 {_uid()}")
    assert src.get("ok"), f"store 源失败: {src}"
    assert "id" in src, f"store 源无 id（疑似去重 skipped）: {src}"
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"V1.1 方法测试派生 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    assert der.get("ok") and "id" in der, f"store 派生失败: {der}"
    der_id = der["id"]
    # 方法调用：把源归档（模拟 api/脚本路径直接调 VectorStore）
    vs = VectorStore(db_path=str(tmp_db))
    n = vs.propagate_source_archived(src_id, reason="test-archive")
    vs.close()
    assert n == 1, f"应传播 1 个派生，实际 {n}"
    tags = _tags_of(tmp_db, der_id)
    assert any(t.startswith("lineage-stale:") for t in tags), f"方法路径应打标，实际 {tags}"
