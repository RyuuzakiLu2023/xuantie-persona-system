# -*- coding: utf-8 -*-
"""test_xtmem_w3_quarantine_follow.py — W3 quarantine 接 follow 守卫（W 批，2026-09-05，assistant-c）。

判据（DoD）：
  ① 人造「源 vis 非法值 + 派生 shared」样本 → 跑 quarantine_bad_visibility(conn, vector_store=vs)
     → 派生 vis 必须降到 private + 事件 lineage-vis-follow（vis 收紧面接线——方法造好了
     生产零调用方，收紧后派生 shared 仍 shared 的漏洞补上）；
  ② 不传 vector_store（纯 schema 迁移）行为不变——只置 private + visibility-quarantine 事件；
  ③ follow 失败不中断 quarantine（主目标=源转 private）。
"""
import json
import sqlite3
import uuid

import pytest

from memory.vector_store import VectorStore
from memory.schema import quarantine_bad_visibility


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _drop_vis_triggers(db):
    """canonical 库带 trg_chunks_visibility_ins/upd——造历史坏值样本前先 DROP（模拟 trigger
    上线前就存在的脏数据；quarantine 就是为这种库写的）。"""
    con = sqlite3.connect(str(db))
    con.execute("DROP TRIGGER IF EXISTS trg_chunks_visibility_ins")
    con.execute("DROP TRIGGER IF EXISTS trg_chunks_visibility_upd")
    con.commit()
    con.close()


def _seed_bad_vis_sample(db, suffix=None):
    """人造样本：源 vis 非法值（'garbage'）+ 指向它的 shared 派生（历史漏洞面）。"""
    suf = suffix or _uid()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                "VALUES (?, '坏源内容', 1700000000, 0.5, '[]', 'default', 'active', 'dsh', 'garbage', 'raw')",
                (f"mem-w3src{suf}",))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from, source_ids) "
                "VALUES (?, '共享派生', 1700000001, 0.5, '[]', 'default', 'active', 'dsh', 'shared', 'refined', 'distill', ?)",
                (f"mem-w3der{suf}", json.dumps([f"mem-w3src{suf}"])))
    con.commit()
    con.close()


def _ev_count(db, kind):
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind=?", (kind,)).fetchone()[0]
    con.close()
    return n


def test_w3_quarantine_follows_derived_visibility(tmp_db):
    """quarantine 转 private 的源 → 指向它的 shared 派生必须降到 private + 事件。"""
    suf = _uid()
    _drop_vis_triggers(tmp_db)
    _seed_bad_vis_sample(tmp_db, suf)
    # 源本身就是坏 vis，直接 SQL 造（trigger 只在合法库路径生效；这里模拟历史脏数据）
    vs = VectorStore(db_path=str(tmp_db))
    res = quarantine_bad_visibility(vs._get_conn(), vector_store=vs)
    assert res["quarantined"] == 1, res
    assert res["vis_followed"] == 1, f"应 follow 1 个派生，实际 {res}"
    con = sqlite3.connect(str(tmp_db))
    vis = con.execute("SELECT visibility FROM chunks WHERE id=?", (f"mem-w3der{suf}",)).fetchone()[0]
    con.close()
    assert vis == "private", f"派生 vis 应降为 private，实际 {vis}"
    assert _ev_count(tmp_db, "lineage-vis-follow") >= 1, "必须有 lineage-vis-follow 事件"


def test_w3_quarantine_no_vs_backward_compat(tmp_db):
    """不传 vector_store（纯 schema 迁移）→ 只置 private + quarantine 事件，行为不变。"""
    suf = _uid()
    _drop_vis_triggers(tmp_db)
    con = sqlite3.connect(str(tmp_db))
    con.execute("INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
                "VALUES (?, '坏源2', 1700000000, 0.5, '[]', 'default', 'active', 'dsh', 'badbad', 'raw')",
                (f"mem-w3bare{suf}",))
    con.commit()
    con.close()
    rc = sqlite3.connect(str(tmp_db))
    rc.row_factory = sqlite3.Row
    res = quarantine_bad_visibility(rc)  # 不传 vs
    rc.close()
    assert res["quarantined"] == 1 and res.get("vis_followed") == 0, res
    con = sqlite3.connect(str(tmp_db))
    vis = con.execute("SELECT visibility FROM chunks WHERE id=?", (f"mem-w3bare{suf}",)).fetchone()[0]
    con.close()
    assert vis == "private", f"源应置 private，实际 {vis}"
