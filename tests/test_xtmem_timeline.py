# -*- coding: utf-8 -*-
"""P5.0-f 时间骨架生成器测试（2026-09-02，assistant-c）。

覆盖 maintainer 裁决要点：chunk 路线（track=timeline/tier=refined/importance=1.0/derived_from=timeline）、
  月内重跑幂等（月度锚 YYYYMM 跳过不翻版）、--refresh-48h 产新版 + 旧版 superseded、
  空域不报错（"无记录"口径）。
"""

from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import axis_predicates
import conftest

CLI = conftest.CLI
TIMELINE = Path(_REPO / "scripts"/ "xtmem_timeline.py")
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


def _run_gen(db, *extra):
    env = dict(os_environ_like())
    env["XTMEM_DB_PATH"] = str(db)
    env["XTMEM_CLI"] = str(CLI)
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, str(TIMELINE), "--domain", "dsh", *extra],
                       capture_output=True, text=True, encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def os_environ_like():
    import os
    return dict(os.environ)


def _timeline_rows(db):
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT id, track, tier, importance, derived_from, state, source_ids, content "
        "FROM chunks WHERE track='timeline' ORDER BY timestamp DESC").fetchall()
    con.close()
    return rows


def test_timeline_generate_and_idempotent(tmp_db, cli_env):
    # 先造两笔原料（时间骨架的源）
    assert _run_cli(cli_env, "store", "--agent", "dsh", "--text", "时间骨架回归：assistant-a偏好本地模型部署").get("ok")
    assert _run_cli(cli_env, "store", "--agent", "dsh", "--text", "时间骨架回归：XTMEM 图传播已上线").get("ok")

    r1 = _run_gen(tmp_db)
    dsh = next((x for x in r1.get("results", []) if x.get("ns") == "dsh"), None)
    assert r1.get("ok") and dsh and dsh.get("ok"), r1
    rows = _timeline_rows(tmp_db)
    assert len(rows) == 1, f"应恰 1 条活时间骨架，实际 {len(rows)}"
    rid, track, tier, imp, df, state, src, content = rows[0]
    assert track == "timeline" and tier == "refined" and imp == 1.0
    assert df == "timeline" and state == "active"
    assert src not in ("[]", None) and len(src) > 2, "血缘 source_ids 必须非空（P0.1 闸）"
    assert "## 月度摘要" in content and "## 近 48h" in content and "rule + 时间窗" in content
    stamp = datetime.now(timezone.utc).strftime("%Y%m")
    assert stamp in content, "纵标题必须带 YYYYMM 月度锚"

    # 月内重跑：默认跳过（不产新版、不翻版）
    r2 = _run_gen(tmp_db)
    dsh2 = next((x for x in r2.get("results", []) if x.get("ns") == "dsh"), None)
    assert dsh2.get("skipped") == "same-month-exists", dsh2
    assert len(_timeline_rows(tmp_db)) == 1, "月内默认重跑不得产新版"

    # --refresh-48h：强制产新版，旧版 superseded（P2.1 生命周期）
    r3 = _run_gen(tmp_db, "--refresh-48h")
    dsh3 = next((x for x in r3.get("results", []) if x.get("ns") == "dsh"), None)
    assert dsh3.get("ok") and dsh3.get("superseded", 0) >= 1, dsh3
    rows3 = _timeline_rows(tmp_db)
    actives = [x for x in rows3 if x[5] == "active"]
    supers = [x for x in rows3 if x[5] == "superseded"]
    assert len(actives) == 1 and len(supers) >= 1, f"refresh 后应 1 活 + ≥1 旧，实际 act={len(actives)} sup={len(supers)}"


def test_timeline_empty_domain_safe(tmp_db):
    """空域（--domain nope 无任何 chunk）：不崩，reason 落在 empty-domain/no-sources。"""
    r = _run_gen(tmp_db, "--domain", "nope")
    assert r.get("ok"), r
    item = r["results"][0]
    assert item.get("ok") is False and item.get("reason") in ("empty-domain", "no-sources"), item
