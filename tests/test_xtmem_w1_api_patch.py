# -*- coding: utf-8 -*-
"""test_xtmem_w1_api_patch.py — W1 PATCH 归档接 propagate 守卫（W 批，2026-09-05，assistant-c）。

判据（DoD）：
  ① 经 HTTP PATCH 把源置 state=archived → 指向它的 active 派生必须被打 lineage-stale 标
     + 事件留痕（api/脚本路径同语义——V1.1 propagate 在 API 路径单点断线补上）；
  ② PATCH state=superseded【不】触发打标（与 CLI/演化设计一致）；
  ③ PATCH state=active/不动 state 也不触发。
"""
import json
import os
import sqlite3
import sys
import uuid

import pytest

import conftest
from fastapi.testclient import TestClient

CLI = conftest.CLI


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _run_cli(cli_env, *argv):
    import subprocess
    env = dict(cli_env)
    env.pop("XTMEM_ACTOR", None)
    env.pop("XTMEM_ADMIN", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def _tags_of(db, cid):
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM chunks WHERE id=?", (cid,)).fetchone()
    con.close()
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


@pytest.fixture()
def ap_client(tmp_db, cli_env):
    """TestClient 起 api_server（指向 tmp_db，noauth 调试模式）。"""
    import os as _os
    import memory.api_server as ap
    from memory.vector_store import VectorStore
    _os.environ["XTMEM_REST_AUTH_FILE"] = str(__import__("pathlib").Path(tmp_db).parent / "w1-no-auth.json")
    _os.environ["XTMEM_REST_ALLOW_LOOPBACK_NOAUTH"] = "1"
    # 不设 ap._AUTH_TOKEN —— tokens 为空时 noauth 调试模式放行（viewer=__admin__）
    vs = VectorStore(db_path=str(tmp_db))
    app = ap.create_app(vector_store=vs, host="127.0.0.1", allow_noauth=True)
    c = TestClient(app)
    yield c
    vs.close()
    _os.environ.pop("XTMEM_REST_ALLOW_LOOPBACK_NOAUTH", None)


def test_w1_patch_archived_propagates_stale(tmp_db, cli_env, ap_client):
    """HTTP PATCH state=archived → 派生打 lineage-stale 标 + 事件 + 响应带计数。"""
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 PATCH 源 {_uid()}")
    assert src.get("ok") and "id" in src, src
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 PATCH 派生 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    assert der.get("ok") and "id" in der, der
    der_id = der["id"]

    r = ap_client.patch(f"/memory/{src_id}", json={"state": "archived"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("lineage_stale_derived") == 1, body

    tags = _tags_of(tmp_db, der_id)
    assert any(t.startswith("lineage-stale:") for t in tags), f"派生应打 stale 标，实际 {tags}"
    con = sqlite3.connect(str(tmp_db))
    ev = con.execute("SELECT meta FROM xtmem_events WHERE kind='lineage-stale' ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    assert ev, "必须有 lineage-stale 事件"
    assert "api-patch" in (ev[0] or ""), f"事件 reason 应为 api-patch: {ev[0]}"


def test_w1_patch_superseded_not_stale(tmp_db, cli_env, ap_client):
    """PATCH state=superseded【不】触发 stale——聚合型派生引 superseded 是设计。"""
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 superseded 源 {_uid()}")
    assert src.get("ok") and "id" in src, src
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 superseded 派生 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    assert der.get("ok") and "id" in der, der
    der_id = der["id"]

    r = ap_client.patch(f"/memory/{src_id}", json={"state": "superseded"})
    assert r.status_code == 200, r.text
    assert r.json().get("lineage_stale_derived") == 0, r.json()

    tags = _tags_of(tmp_db, der_id)
    assert not any(t.startswith("lineage-stale:") for t in tags), f"superseded 不应打标，实际 {tags}"


def test_w1_patch_active_not_stale(tmp_db, cli_env, ap_client):
    """PATCH 不动 state（如只改 track）不触发 stale。"""
    src = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 active 源 {_uid()}")
    assert src.get("ok") and "id" in src, src
    src_id = src["id"]
    der = _run_cli(cli_env, "store", "--agent", "dsh", "--text", f"W1 active 派生 {_uid()}",
                   "--tier", "refined", "--derived-from", "distill", "--source-ids", src_id)
    assert der.get("ok") and "id" in der, der
    der_id = der["id"]

    r = ap_client.patch(f"/memory/{src_id}", json={"track": "identity"})
    assert r.status_code == 200, r.text
    assert r.json().get("lineage_stale_derived") == 0, r.json()
    tags = _tags_of(tmp_db, der_id)
    assert not any(t.startswith("lineage-stale:") for t in tags), f"改 track 不应打标，实际 {tags}"
