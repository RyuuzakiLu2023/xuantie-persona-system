# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_466c_schema_guard.py · 466c 件①守卫
#
#   ▍这是什么
#   466c 收尾三合一（XTMEM-派单工单-466c收尾三合一-20260913）件①守卫，
#   封锁存量雷错题本#52：conflict_candidate.decision_reason 列在 schema.py 的
#   CREATE TABLE 里缺席、全树无 ALTER 迁移——手工 ALTER 过的生产库活着，
#   任何新建库跑 record_conflict_result（conflict.py INSERT 17 列）即炸。
#   修法：schema.py CREATE TABLE 补列（管新库）+ vector_store._init_db 里
#   PRAGMA 缺列 ALTER（管存量老库）；user_version=3 不动（D1 先例）。
#
#   ▍守卫内容
#   1. 静态：schema.py 源码 conflict_candidate 块必含 decision_reason（删列=改坏=红）；
#   2. 新建空库全流程（VectorStore→ensure_xtmem_schema→迁移段）→ 17 列在场；
#   3. 16 列旧形态存量夹具库 → VectorStore 迁移器补齐到 17 列（老库路径）；
#   4. record_conflict_result 三路（judged supersedes / enriches / error）新库不炸
#      且 reason 落对；error 路空理由【不】覆盖旧值（防洗 p3污染回退标记）；
#   5. user_version 仍 3（纯列补齐不动 uv）。
#
#   ▍修改铁律
#   1. MASTER 直载运行时 schema.py / vector_store.py / conflict.py（错题本 M1：
#      必须打中真实读取路径）。
#   2. 断言只用 tempfile 夹具库，严禁连生产库。
#   3. 三镜像同步（xtmem-project/tests ↔ XTMEM/tests ↔ XTMEM-public/tests），
#      sha 不等即未交付。
#
#   ▍更新记录
#   *更新：2026-09-13 · 创建（maintainer 代执 466c 件①）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import os
import sqlite3
import sys
import tempfile

import pytest

MASTER_PARENT = _REPO / "src" / "hermes-plugin"
if MASTER_PARENT not in sys.path:
    sys.path.insert(0, MASTER_PARENT)

import memory.schema as S  # noqa: E402
import memory.vector_store as VS  # noqa: E402
import memory.conflict as C  # noqa: E402

# 生产库现况 17 列（09-12 手工 ALTER 后），CREATE TABLE 补列后须逐列对齐
C17 = [
    "pair_key", "source_id", "target_id", "namespace", "project",
    "candidate_score", "llm_score", "decision", "policy_version", "model",
    "prompt_hash", "status", "attempt", "last_error", "created_at",
    "updated_at", "decision_reason",
]


def _mk_vs(db_path):
    """建库后立刻关连接——Windows 下 VectorStore 持连接不撒手，TemporaryDirectory
    清理必炸 WinError 32（进程内单连接，关掉不影响断言路径）。"""
    vs = VS.VectorStore(db_path)
    if vs._conn is not None:
        vs._conn.close()
        vs._conn = None
    return vs


def _cols(db_path):
    con = sqlite3.connect(db_path)
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(conflict_candidate)").fetchall()]
    finally:
        con.close()


def _schema_src_conflict_block():
    src = open(os.path.join(MASTER_PARENT, "memory", "schema.py"), encoding="utf-8").read()
    i = src.find("CREATE TABLE IF NOT EXISTS conflict_candidate")
    j = src.find(")", i)
    return src[i:j]


# ── 1. 静态断言：删列必红 ─────────────────────────────────────────

def test_schema_create_table_has_decision_reason():
    """schema.py 的 conflict_candidate 块必含 decision_reason（有人删列跑这条即红）"""
    block = _schema_src_conflict_block()
    assert "decision_reason" in block, "schema.py conflict_candidate CREATE TABLE 缺 decision_reason（错题本#52 复发）"


# ── 2/3. 新库 + 旧形态存量库双路径 ────────────────────────────────

def test_new_db_gets_17_cols():
    """新建空库走 VectorStore 全流程 → 17 列，列序对齐生产库"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "fresh.db")
        _mk_vs(db)
        assert _cols(db) == C17


def test_legacy_16col_db_migrated():
    """手工造 16 列旧形态存量库（漏列时代的库）→ VectorStore 迁移器 ALTER 补齐"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "legacy.db")
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE conflict_candidate (
                pair_key TEXT PRIMARY KEY, source_id TEXT, target_id TEXT,
                namespace TEXT, project TEXT, candidate_score REAL, llm_score REAL,
                decision TEXT, policy_version TEXT, model TEXT, prompt_hash TEXT,
                status TEXT, attempt INTEGER DEFAULT 0, last_error TEXT,
                created_at REAL, updated_at REAL
            )
        """)
        con.commit()
        con.close()
        assert "decision_reason" not in _cols(db)
        _mk_vs(db)
        assert _cols(db) == C17


def test_user_version_untouched():
    """纯列补齐不动 user_version（仍 3，D1 先例；动 uv 会触发 open_db 版本闸降级）"""
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "uv.db")
        _mk_vs(db)
        con = sqlite3.connect(db)
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == 3
        finally:
            con.close()


# ── 4. record_conflict_result 三路（新库不炸） ────────────────────

def _mk_chunk(cid):
    return {"id": cid, "namespace": "t", "project": "", "content": "内容 " + cid}


def _fresh_db_with_schema(td, name):
    db = os.path.join(td, name)
    _mk_vs(db)
    return db


def test_record_conflict_result_three_ways(tmp_path):
    """judged supersedes / enriches / error 三路在新库全不炸，reason 按档落对"""
    db = _fresh_db_with_schema(tmp_path, "three.db")
    con = sqlite3.connect(db)
    try:
        # ① judged supersedes（LLM 档 reason = 双向均分/阈值；is_conflict 带影子边）
        C.record_conflict_result(con, _mk_chunk("mem-a1"), _mk_chunk("mem-a2"), {
            "decision": "supersedes", "llm_score": 0.8, "model": C.LLM_MODEL,
            "policy_version": "guard", "prompt_hash": "466c-guard",
            "is_conflict": True,
            "details": {"threshold": 0.65, "newer_id": "mem-a1", "older_id": "mem-a2"},
        })
        row = con.execute(
            "SELECT status, decision, decision_reason, source_id, target_id "
            "FROM conflict_candidate WHERE pair_key=?",
            (C.pair_key_of("mem-a1", "mem-a2"),),
        ).fetchone()
        assert row[0] == "judged" and row[1] == "supersedes"
        assert row[2] == "LLM双向均分 0.80/0.65"
        assert (row[3], row[4]) == ("mem-a1", "mem-a2")  # 方向=新取代旧（details 直供）

        # ② enriches：is_conflict=False，不写影子边
        C.record_conflict_result(con, _mk_chunk("mem-b1"), _mk_chunk("mem-b2"), {
            "decision": "enriches", "llm_score": 0.3, "model": C.LLM_MODEL,
            "policy_version": "guard", "prompt_hash": "466c-guard",
            "is_conflict": False, "details": {"threshold": 0.65},
        })
        row = con.execute(
            "SELECT status, decision, decision_reason FROM conflict_candidate WHERE pair_key=?",
            (C.pair_key_of("mem-b1", "mem-b2"),),
        ).fetchone()
        assert row == ("judged", "enriches", "LLM双向均分 0.30/0.65")

        # ③ error：status=failed，reason 空串
        C.record_conflict_result(con, _mk_chunk("mem-c1"), _mk_chunk("mem-c2"), {
            "decision": "error", "last_error": "boom", "policy_version": "guard",
        })
        row = con.execute(
            "SELECT status, decision, decision_reason FROM conflict_candidate WHERE pair_key=?",
            (C.pair_key_of("mem-c1", "mem-c2"),),
        ).fetchone()
        assert row == ("failed", "error", "")

        # 影子边：仅 supersedes 路写了一条（is_conflict=True），enriches/error 不写
        n = con.execute("SELECT COUNT(*) FROM memory_evolution").fetchone()[0]
        assert n == 1
    finally:
        con.close()


def test_error_empty_reason_does_not_overwrite(tmp_path):
    """error 重试空理由【不】覆盖旧 reason（ON CONFLICT CASE 回退，防洗追溯标记）"""
    db = _fresh_db_with_schema(tmp_path, "ow.db")
    con = sqlite3.connect(db)
    try:
        C.record_conflict_result(con, _mk_chunk("mem-d1"), _mk_chunk("mem-d2"), {
            "decision": "supersedes", "llm_score": 0.9, "model": C.LLM_MODEL,
            "policy_version": "guard", "prompt_hash": "466c-guard",
            "is_conflict": False, "details": {"threshold": 0.65},
        })
        C.record_conflict_result(con, _mk_chunk("mem-d1"), _mk_chunk("mem-d2"), {
            "decision": "error", "last_error": "retry", "policy_version": "guard",
        })
        row = con.execute(
            "SELECT status, decision_reason FROM conflict_candidate WHERE pair_key=?",
            (C.pair_key_of("mem-d1", "mem-d2"),),
        ).fetchone()
        assert row[1] == "LLM双向均分 0.90/0.65"  # 旧理由保住
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
