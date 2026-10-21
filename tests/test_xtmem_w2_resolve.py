# -*- coding: utf-8 -*-
"""test_xtmem_w2_resolve.py — W2 lineage-stale 消费方守卫（W 批，2026-09-05，assistant-c）。

判据（DoD）：
  ① 人造 stale 标（非personal 域）→ 跑 xtmem_lineage_resolve.resolve_stale → 派生换源集
     （剔除 archived/MISSING 源）+ 标清 + 事件 lineage-stale-resolved；
  ② 源集全失效（全 archived/查无）→ 派生 state 置 archived + 连带嵌套 stale；
  ③ ⚠️ 硬红线防回归：personal 域人造 stale 标 → 跑消费 → 标必须还在（judge_skipped）；
  ④ dry-run 只盘点不落库。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import sys
import uuid

import pytest

import conftest

CLI = conftest.CLI
# 被测消费方脚本（主树；镜像由 sha 守卫兜 EQ）
RESOLVE_PY = _REPO / "scripts"/ "xtmem_lineage_resolve.py"


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


def _seed_stale(db, ns="dsh", derived_from="distill", src_states=("archived", "active")):
    """人造样本：archived/查无源 + active 源，派生打 lineage-stale:<src8> 标。"""
    suf = _uid()
    con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
    srcs = []
    for i, st in enumerate(src_states):
        sid = f"mem-w2s{suf}{i}"
        if st == "MISSING":
            pass  # 不插入 = 查无此 id
        else:
            con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                        "VALUES (?, '源内容', 1700000000, 0.5, '[]', 'default', ?, 'dsh', 'shared', 'raw')",
                        (sid, st))
        srcs.append(sid)
    der_id = f"mem-w2d{suf}"
    stale_tag = "lineage-stale:" + srcs[0][-8:]
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from, source_ids) "
                "VALUES (?, 'stale 派生', 1700000001, 0.5, ?, 'default', 'active', ?, 'shared', 'refined', ?, ?)",
                (der_id, json.dumps(["xtmem", stale_tag]), ns, derived_from, json.dumps(srcs)))
    con.commit()
    con.close()
    return der_id, srcs


def _tags(db, cid):
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM chunks WHERE id=?", (cid,)).fetchone()
    con.close()
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


def _row(db, cid):
    con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
    con.close()
    return dict(row) if row else None


def test_w2_resolve_prunes_archived_source(tmp_db):
    """archived 源 + active 源 → 消费后源集剔 archived、标清、事件。"""
    der_id, srcs = _seed_stale(tmp_db, src_states=("archived", "active"))
    sys.path.insert(0, os.path.dirname(RESOLVE_PY))
    import xtmem_lineage_resolve as resolver
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    out = resolver.resolve_stale(con)
    con.close()
    assert out["pruned"] == 1, out
    row = _row(tmp_db, der_id)
    assert row["state"] == "active", "有剩余源应保持 active"
    keep = json.loads(row["source_ids"])
    assert srcs[0] not in keep and srcs[1] in keep, f"源集应剔 archived: {keep}"
    tags = _tags(tmp_db, der_id)
    assert not any(t.startswith("lineage-stale:") for t in tags), f"标应清，实际 {tags}"
    con = sqlite3.connect(str(tmp_db))
    ev = con.execute("SELECT meta FROM xtmem_events WHERE kind='lineage-stale-resolved' ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    assert ev, "必须有 lineage-stale-resolved 事件"
    assert "pruned" in (ev[0] or ""), ev[0]


def test_w2_resolve_archives_when_all_sources_gone(tmp_db):
    """源全 archived（MISSING 也算失效）→ 派生 state=archived + 连带。"""
    der_id, srcs = _seed_stale(tmp_db, src_states=("archived", "MISSING"))
    # 再挂一个指向 der 的二级派生（验证连带打标）
    con = sqlite3.connect(str(tmp_db))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from, source_ids) "
                "VALUES ('mem-w2c', '二级派生', 1700000002, 0.5, '[]', 'default', 'active', 'dsh', 'shared', 'refined', 'profile-aggregator', ?)",
                (json.dumps([der_id]),))
    con.commit(); con.close()

    sys.path.insert(0, os.path.dirname(RESOLVE_PY))
    import xtmem_lineage_resolve as resolver
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    out = resolver.resolve_stale(con)
    con.close()
    assert out["archived"] == 1, out
    row = _row(tmp_db, der_id)
    assert row["state"] == "archived", f"源集全失效应归档派生，实际 {row['state']}"
    # 二级派生被打嵌套 stale 标
    tags2 = _tags(tmp_db, "mem-w2c")
    assert any(t.startswith("lineage-stale:") for t in tags2), f"连带应打标，实际 {tags2}"


def test_w2_redline_xtmem_untouched(tmp_db):
    """⚠️ 硬红线：personal 域 stale 标 → 消费【不】动——judge_skipped 计数，标保留。"""
    con = sqlite3.connect(str(tmp_db))
    suf = _uid()
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                "VALUES (?, 'assistant-a源', 1700000000, 0.5, '[]', 'default', 'active', 'personal', 'shared', 'raw')",
                (f"mem-w2xsrc{suf}",))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from, source_ids) "
                "VALUES (?, 'assistant-a派生', 1700000001, 0.5, ?, 'default', 'active', 'personal', 'shared', 'refined', 'distill', ?)",
                (f"mem-w2xder{suf}", json.dumps(["xtmem", f"lineage-stale:{suf[-8:]}"]), json.dumps([f"mem-w2xsrc{suf}"])))
    con.commit(); con.close()

    sys.path.insert(0, os.path.dirname(RESOLVE_PY))
    import xtmem_lineage_resolve as resolver
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    out = resolver.resolve_stale(con)
    con.close()
    assert out["judge_skipped"] == [f"mem-w2xder{suf}"], out
    tags = _tags(tmp_db, f"mem-w2xder{suf}")
    assert any(t.startswith("lineage-stale:") for t in tags), f"personal 域标必须还在（红线），实际 {tags}"


def test_w2_dry_run_no_write(tmp_db):
    """dry-run：只盘点不落库（标/source_ids/state 全不动）。"""
    der_id, srcs = _seed_stale(tmp_db, src_states=("archived", "active"))
    sys.path.insert(0, os.path.dirname(RESOLVE_PY))
    import xtmem_lineage_resolve as resolver
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    out = resolver.resolve_stale(con, dry_run=True)
    con.close()
    assert out["pruned"] == 1, out
    row = _row(tmp_db, der_id)
    assert json.loads(row["source_ids"]) == srcs, "dry-run 不应改源集"
    tags = _tags(tmp_db, der_id)
    assert any(t.startswith("lineage-stale:") for t in tags), "dry-run 不应清标"
