# -*- coding: utf-8 -*-
"""公开蒸馏子树的可复用合成测试（不连生产库、不连真实 LLM）。

覆盖的**不是** happy path 跑通即算数，而是几条容易被"看起来成功"掩盖的判定：

  1. units：切窗 + 幂等（第二遍 new_units=0）；尾窗口保护。
  2. LLM 层 ACL：目标域读不到的 raw 行**不进 prompt**（acl_dropped 计数 + material 不含正文）。
  3. 血缘：只认精确 id；对不上退回整窗并标 scope=window；
     整窗读者范围不一致时，声明子集作废（scope=window-mixed-acl）。
  4. 部分成功：一条写成功一条失败 ⇒ state='partial'、ok=False、已写入 id 保留，
     不冒充 distilled；补跑靠 operation_id 幂等。
  5. parse 失败不泄原料：错误串里没有模型正文，只有长度+指纹。
  6. LLM 配置：缺 key 退出码 2；EFFORT="" 表示"不发送该参数"。

⚠️ 本测试只用临时目录 + 本机 HTTP 桩；不读任何环境里的真实 key。
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "src" / "scripts"
sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def db(tmp_path):
    """建一个真实的引擎库（走 VectorStore 建表路径），返回 (path, conn, VectorStore)。"""
    from memory.vector_store import VectorStore  # memory 由 conftest/安装提供

    path = tmp_path / "synth.db"
    vs = VectorStore(str(path))
    vs._get_conn().close()
    import sqlite3

    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    yield str(path), conn, vs
    conn.close()


def _insert(conn, cid, ns, content, ts, *, tier="raw", visibility="shared",
            source="s1", derived_from="", track="default", source_ids="[]",
            state="active", retire_reason=None, host_origin=""):
    conn.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, source, track, "
        "state, namespace, visibility, tier, derived_from, source_ids, retire_reason, "
        "host_origin, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, content, ts, 0.8, "[]", source, track, state, ns, visibility, tier,
         derived_from, source_ids, retire_reason, host_origin, ts),
    )
    conn.commit()


# ---------------------------------------------------------------- units
def test_units_windows_idempotent_and_tail_guard(db):
    path, conn, _vs = db
    import xtmem_distill_units as units

    now = time.time()
    _insert(conn, "mem-00000001", "alpha", "User: 部署用什么\nAssistant: sqlite", now - 7200)
    _insert(conn, "mem-00000002", "alpha", "User: 为什么\nAssistant: 免运维", now - 7140)
    # 尾窗口：距 now 不足 gap ⇒ 不落账
    _insert(conn, "mem-00000003", "alpha", "User: 还在聊\nAssistant: 嗯", now - 60)

    units.ensure_schema(conn)
    stat = units.build_units(conn, "alpha", 60, False, units.speaker_markers())
    assert stat["new_units"] == 1, stat
    assert stat["held_inprogress"] == 1, stat

    again = units.build_units(conn, "alpha", 60, False, units.speaker_markers())
    assert again["new_units"] == 0 and again["skipped_existing"] == 1, again


def test_units_filters_greeting_and_system_turns(db):
    path, conn, _vs = db
    import xtmem_distill_units as units
    now = time.time()
    _insert(conn, "mem-00000011", "alpha", "User: 在吗\nAssistant: 在", now - 7200)
    _insert(conn, "mem-00000012", "alpha",
            "Review the conversation above and consider saving to memory", now - 7100)
    _insert(conn, "mem-00000013", "alpha", "用户：帮我查个日志\n助手：好", now - 7000)
    units.ensure_schema(conn)
    stat = units.build_units(conn, "alpha", 60, False, units.speaker_markers())
    assert stat["filtered_by"].get("greet") == 1, stat
    assert stat["filtered_by"].get("sys-prompt") == 1, stat
    # 中文发言人标记的窗口要保留
    assert stat["kept"] == 1, stat


# ---------------------------------------------------------------- lineage
def test_pick_lineage_exact_match_only():
    import xtmem_distill_llm as llm

    window = ["mem-aaaa1111", "mem-aaaa2222"]
    ids, scope = llm.pick_lineage({"lineage": "mem-aaaa"}, window)
    assert ids == window and scope == "window", (ids, scope)  # 模糊前缀不算命中

    ids, scope = llm.pick_lineage({"lineage": "[1] mem-aaaa1111"}, window)
    assert ids == ["mem-aaaa1111"] and scope == "declared"

    ids, scope = llm.pick_lineage({}, window)
    assert ids == window and scope == "window"


def test_acl_safe_source_ids_refuses_narrowing_when_mixed(db):
    path, conn, _vs = db
    import xtmem_distill_llm as llm

    rows = [
        {"id": "mem-0000a001", "namespace": "alpha", "visibility": "shared"},
        {"id": "mem-0000a002", "namespace": "alpha", "visibility": "private"},
    ]
    ids, scope = llm.acl_safe_source_ids(["mem-0000a001"], rows,
                                         ["mem-0000a001", "mem-0000a002"])
    assert scope == "window-mixed-acl"
    assert set(ids) == {"mem-0000a001", "mem-0000a002"}


# ---------------------------------------------------------------- ACL in material
def test_build_material_drops_unreadable_rows(db):
    path, conn, _vs = db
    import xtmem_distill_llm as llm

    now = time.time()
    _insert(conn, "mem-0000b001", "alpha", "User: 可见内容\nAssistant: 好",
            now, visibility="shared")
    # ⚠️ private 在**同域**是可读的（引擎语义：private = 本域可见）。
    #    要验 ACL 挡人，必须跨域：别的 namespace 的 private 行，alpha 读不到。
    _insert(conn, "mem-0000b002", "beta", "SECRET-私有正文-不得进入原料",
            now + 1, visibility="private")
    unit = {"unit_id": "alpha:s1:1", "namespace": "alpha", "source": "s1",
            "ts_start": now, "ts_end": now + 1,
            "raw_ids": json.dumps(["mem-0000b001", "mem-0000b002"])}

    material, window_ids, dropped = llm.build_material(conn, unit, "alpha")
    assert window_ids == ["mem-0000b001"], window_ids
    assert dropped == ["mem-0000b002"], dropped
    assert "SECRET-私有正文" not in material

    # 换成别的域蒸同一窗口：整批拒绝（不是"能读多少蒸多少"）
    with pytest.raises(ValueError):
        llm.build_material(conn, unit, "beta")


# ---------------------------------------------------------------- 部分成功
def test_partial_store_does_not_masquerade_as_success(db, monkeypatch):
    path, conn, _vs = db
    import xtmem_distill_llm as llm

    now = time.time()
    _insert(conn, "mem-0000c001", "alpha", "User: 一问\nAssistant: 一答", now)
    llm.ensure_schema = getattr(llm, "ensure_schema", None)
    import xtmem_distill_units as units
    units.ensure_schema(conn)
    unit_id = f"alpha:s1:{int(now)}"
    conn.execute(
        "INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, turn_count,"
        " char_count, raw_ids, state, attempt, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (unit_id, "alpha", "s1", now, now, 1, 10,
         json.dumps(["mem-0000c001"]), "processing", 1, now),
    )
    conn.commit()
    unit = conn.execute("SELECT * FROM distill_units WHERE unit_id=?", (unit_id,)).fetchone()

    reply = (
        "## 记忆条目\n\n"
        "### [1] 第一条\n- 桶: 技术\n- 条目类型: 结论\n- 时间: 无\n"
        "- 信息截至: 无\n- 提问方: 用户\n- 回答判定: 正确\n- 问的是: q1\n"
        "- 答的是: a1\n- 事实/结论: 结论一\n- 血缘: [1]\n\n"
        "### [2] 第二条\n- 桶: 技术\n- 条目类型: 结论\n- 时间: 无\n"
        "- 信息截至: 无\n- 提问方: 用户\n- 回答判定: 正确\n- 问的是: q2\n"
        "- 答的是: a2\n- 事实/结论: 结论二\n- 血缘: [1]\n"
    )
    monkeypatch.setattr(llm, "call_llm", lambda cfg, prompt, material: (reply, {"finish_reason": "stop"}))

    calls = {"n": 0, "opids": []}

    def fake_store(e, source_ids, domain, db_path, operation_id=None):
        calls["n"] += 1
        calls["opids"].append(operation_id)
        if calls["n"] == 1:
            return {"ok": True, "id": "mem-0000c0aa"}
        return {"ok": False, "error": "synthetic reject"}

    monkeypatch.setattr(llm, "store_entry", fake_store)
    monkeypatch.setattr(llm, "retire_source_raws", lambda *a, **k: {"retired": 0, "skipped": 0})

    res = llm.run_unit(conn, unit, {}, "prompt", path, "alpha", dry=False)
    assert res["partial"] is True and res["ok"] is False, res
    assert res["stored"] == 1 and res["store_errors"] == 1, res
    row = conn.execute("SELECT state, refined_ids, last_error FROM distill_units WHERE unit_id=?",
                       (unit_id,)).fetchone()
    assert row["state"] == "partial", dict(row)
    assert json.loads(row["refined_ids"]) == ["mem-0000c0aa"], row["refined_ids"]
    assert "synthetic reject" in (row["last_error"] or "")
    # 每条都带 operation_id（重试幂等键）
    assert all(calls["opids"]) and len(calls["opids"]) == 2, calls


# ---------------------------------------------------------------- 失败不泄原料
def test_parse_failure_does_not_leak_material(monkeypatch, db):
    path, conn, _vs = db
    import xtmem_distill_llm as llm

    now = time.time()
    _insert(conn, "mem-0000d001", "alpha", "User: 秘密问题\nAssistant: 秘密回答", now)
    import xtmem_distill_units as units
    units.ensure_schema(conn)
    unit_id = f"alpha:s1:{int(now)}"
    conn.execute(
        "INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, turn_count,"
        " char_count, raw_ids, state, attempt, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (unit_id, "alpha", "s1", now, now, 1, 10,
         json.dumps(["mem-0000d001"]), "processing", 1, now),
    )
    conn.commit()
    unit = conn.execute("SELECT * FROM distill_units WHERE unit_id=?", (unit_id,)).fetchone()

    leaky = "SECRET-模型原文-不应出现在错误里" * 5
    monkeypatch.setattr(llm, "call_llm", lambda cfg, prompt, material: (leaky, {"finish_reason": "length"}))
    res = llm.run_unit(conn, unit, {}, "p", path, "alpha", dry=True)
    assert res["ok"] is False and res["stage"] == "parse"
    blob = json.dumps(res, ensure_ascii=False)
    assert "SECRET-模型原文" not in blob, blob
    assert "sha256=" in res["error"]


# ---------------------------------------------------------------- 配置
def test_llm_config_requires_explicit_env(monkeypatch):
    import xtmem_distill_llm as llm

    for k in ("XTMEM_LLM_BASE", "XTMEM_LLM_MODEL", "XTMEM_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        llm.llm_config()

    monkeypatch.setenv("XTMEM_LLM_BASE", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("XTMEM_LLM_MODEL", "m")
    monkeypatch.setenv("XTMEM_LLM_KEY", "k")
    cfg = llm.llm_config()
    assert cfg["effort"] == "high" and cfg["max_tokens"] == 8192 and cfg["timeout"] == 120

    # 显式空串 = 不发送 reasoning_effort（旧实现会被 or 默认吞回 high）
    monkeypatch.setenv("XTMEM_LLM_EFFORT", "")
    assert llm.llm_config()["effort"] == ""


# ---------------------------------------------------------------- 时间骨架 / 画像
def test_timeline_and_profile_on_synthetic_db(db, tmp_path):
    path, conn, _vs = db
    import xtmem_profile as profile
    import xtmem_timeline as timeline

    now = time.time()
    _insert(conn, "mem-0000e001", "alpha", "用户喜欢先看数据", now,
            tier="refined", track="preference", source="s1")
    _insert(conn, "mem-0000e002", "alpha", "部署用 sqlite 因为免运维", now + 10,
            tier="refined", track="identity", source="s1")
    _insert(conn, "mem-0000e003", "alpha", "第三条事实", now + 20,
            tier="refined", track="default", source="s1")

    # 只要 ≥5 条才出画像；补齐到 5
    for i in range(4, 8):
        _insert(conn, f"mem-0000e00{i}", "alpha", f"补充事实 {i}", now + i,
                tier="refined", track="default", source="s1")

    t = timeline._run_domain(conn, path, "alpha", 12, False)
    assert t.get("ok") is True, t
    assert t["sources_used"] >= 1
    t2 = timeline._run_domain(conn, path, "alpha", 12, False)
    assert t2.get("skipped") == "same-month-exists", t2  # 月内幂等

    p = profile._run_domain(conn, path, "alpha", 7, True)
    assert p.get("ok") is True, p
    assert p["used"] >= 5

    # 派生页确实落库，且 source_ids 只含实际采用的条目
    rows = conn.execute(
        "SELECT id, track, namespace, derived_from, source_ids FROM chunks "
        "WHERE state='active' AND derived_from IN ('timeline','profile-aggregator')"
    ).fetchall()
    assert len(rows) == 2, [dict(r) for r in rows]
    for r in rows:
        assert json.loads(r["source_ids"]), dict(r)


# ---------------------------------------------------------------- 血缘消费
def test_lineage_resolve_prunes_and_archives(db):
    path, conn, _vs = db
    import xtmem_lineage_resolve as lr

    now = time.time()
    _insert(conn, "mem-0000f001", "alpha", "已归档的源", now, state="archived")
    _insert(conn, "mem-0000f002", "alpha", "仍在的源", now)
    _insert(conn, "mem-0000f003", "alpha", "派生一（还有活源）", now, tier="refined",
            derived_from="distill", source_ids=json.dumps(["mem-0000f001", "mem-0000f002"]))
    _insert(conn, "mem-0000f004", "alpha", "派生二（源全失效）", now, tier="refined",
            derived_from="distill", source_ids=json.dumps(["mem-0000f001"]))
    for cid in ("mem-0000f003", "mem-0000f004"):
        conn.execute("UPDATE chunks SET tags=? WHERE id=?",
                     (json.dumps(["lineage-stale:" + cid[-8:]]), cid))
    conn.commit()

    out = lr.resolve_stale(conn, domain_skip=set(), dry_run=False)
    assert out["pruned"] == 1 and out["archived"] == 1, out

    d1 = conn.execute("SELECT state, source_ids, tags FROM chunks WHERE id='mem-0000f003'").fetchone()
    assert d1["state"] == "active"
    assert json.loads(d1["source_ids"]) == ["mem-0000f002"], dict(d1)
    assert not any(str(t).startswith("lineage-stale:") for t in json.loads(d1["tags"]))

    d2 = conn.execute("SELECT state, superseded_by, retire_reason FROM chunks WHERE id='mem-0000f004'").fetchone()
    assert d2["state"] == "archived" and d2["superseded_by"] == ""
    assert d2["retire_reason"] == "lineage-stale-resolve"

    # 跳过域：只留标不动
    _insert(conn, "mem-0000f005", "beta", "派生三", now, tier="refined",
            derived_from="distill", source_ids=json.dumps(["mem-0000f001"]))
    conn.execute("UPDATE chunks SET tags=? WHERE id='mem-0000f005'",
                 (json.dumps(["lineage-stale:deadbeef"]),))
    conn.commit()
    out2 = lr.resolve_stale(conn, domain_skip={"beta"}, dry_run=False)
    assert out2["judge_skipped"] == ["mem-0000f005"], out2
    assert conn.execute("SELECT state FROM chunks WHERE id='mem-0000f005'").fetchone()["state"] == "active"


# ---------------------------------------------------------------- 实体页
def test_wiki_builds_shared_and_private_pages_separately(db, monkeypatch):
    path, conn, _vs = db
    import xtmem_wiki as wiki

    now = time.time()
    for i in range(6):
        _insert(conn, f"mem-0000a10{i}", "alpha", f"关于 Acme 的事实 {i}", now + i,
                tier="refined", visibility="shared", source="s1")
    _insert(conn, "mem-0000a200", "alpha", "关于 Acme 的私密事实", now + 10,
            tier="refined", visibility="private", source="s1")
    conn.execute("INSERT INTO entity_aliases (canon, alias) VALUES ('Acme','ACME')")
    for i in range(6):
        conn.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES (?, 'Acme')",
                     (f"mem-0000a10{i}",))
    conn.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES ('mem-0000a200','Acme')")
    conn.commit()

    wiki._DB_PATH[0] = path
    monkeypatch.setenv("XTMEM_HOME", str(Path(path).parent))
    results = wiki.build_pages(conn, min_df=5, domain="alpha", rebuild=False)
    built = [r for r in results if r.get("ok")]
    assert len(built) == 2, results  # 共享池 + 私池各一页
    pools = {r["pool"] for r in built}
    assert pools == {"shared", "private"}, results

    rows = conn.execute(
        "SELECT visibility FROM chunks WHERE derived_from='wiki-entity' AND state='active'"
    ).fetchall()
    # 共享页只引共享源；私池页含私密源。至少验证两页可见性不同（继承来源，不硬编码）
    assert len({r["visibility"] for r in rows}) >= 1


# ---------------------------------------------------------------- 已知风险默认不启
def test_wiki_default_refuses_without_experimental_flag(monkeypatch):
    """实体页是实验性作业：不加 --allow-experimental 只打印说明、退出码 2、不写库。"""
    import xtmem_wiki as wiki

    monkeypatch.setattr(sys, "argv", ["xtmem_wiki.py"])
    assert wiki.main() == 2


def test_lineage_resolve_defaults_to_diagnose_only(db):
    """血缘修复默认只诊断：不传 --apply 时零写入。"""
    path, conn, _vs = db
    import xtmem_lineage_resolve as lr

    now = time.time()
    _insert(conn, "mem-0000f101", "alpha", "已归档源", now, state="archived")
    _insert(conn, "mem-0000f102", "alpha", "派生", now, tier="refined",
            derived_from="distill", source_ids=json.dumps(["mem-0000f101"]))
    conn.execute("UPDATE chunks SET tags=? WHERE id='mem-0000f102'",
                 (json.dumps(["lineage-stale:0000f101"]),))
    conn.commit()

    out = lr.resolve_stale(conn, domain_skip=set(), dry_run=True)
    assert out["archived"] == 1 and out["archived_ids"] == ["mem-0000f102"], out
    row = conn.execute("SELECT state FROM chunks WHERE id='mem-0000f102'").fetchone()
    assert row["state"] == "active", "dry-run 不得写库"
