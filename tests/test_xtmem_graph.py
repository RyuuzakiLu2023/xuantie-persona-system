# -*- coding: utf-8 -*-
"""P5.0 波2 图传播测试（2026-09-02，小D）。

覆盖：tag_edges 建图/增量幂等（P5.0-a）→ 传输场幂迭代 + IDF（P5.0-b）→ doc_boosts 链式传播 →
      接入 search() 的接线行为（P5.0-c，weight=0 逐字节一致 / weight>0 非负加分）。

人造实体全部 zzz-test-* 前缀（守卫③：自证值不与现实重合——总 Spec P5.0 验收样式）。
"""

import sqlite3
import time

import pytest

from memory import graph as G
from memory.hybrid_retriever import HybridRetriever, MemoryDoc


def _prep_db(db):
    """空库 + 最小 chunks 表 → ensure_xtmem_schema（建齐 P4.9/P5.0 表）。"""
    con = sqlite3.connect(str(db))
    # 最小 chunks 表：schema 迁移会 ALTER 补其余列；state 必须先有（idx_chunks_tier 三列索引）
    con.execute("CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, state TEXT DEFAULT 'active')")
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(con)
    con.close()


def _put_entities(db, mapping):
    """mapping: {chunk_id: [canon,...]} → 写 chunk_entities。"""
    con = sqlite3.connect(str(db))
    for cid, canons in mapping.items():
        # 与生产真实关联一致：每个 chunk_entities 条目必有 chunks 行（derived_from='' = 非派生，rebuild JOIN 需要）。
        # 仅写 ensure_xtmem_schema 后存在的列（minimal chunks 表无 content/track）。
        con.execute("INSERT OR IGNORE INTO chunks(id, derived_from, state) VALUES(?, '', 'active')", (cid,))
        for c in canons:
            con.execute("INSERT OR IGNORE INTO chunk_entities(chunk_id, canon) VALUES(?,?)", (cid, c))
    con.commit()
    con.close()


def _mk(doc_id, content):
    return MemoryDoc(id=doc_id, content=content, timestamp=time.time(),
                     importance=0.6, namespace="dsh", visibility="shared", tier="raw")


# ── P5.0-a：建图 ─────────────────────────────────────────────

def test_rebuild_edges_counts(empty_db):
    _prep_db(empty_db)
    _put_entities(empty_db, {
        "c1": ["zzz-test-A", "zzz-test-B", "zzz-test-C"],
        "c2": ["zzz-test-A", "zzz-test-B"],
        "c3": ["zzz-test-C"],
    })
    st = G.rebuild_edges(db_path=str(empty_db))
    assert st["edges"] == 3, st          # AB(2) AC(1) BC(1)
    assert st["nodes"] == 3
    assert st["tagged_chunks"] == 3
    con = sqlite3.connect(str(empty_db))
    row = dict(zip(("a", "b", "cnt"), con.execute(
        "SELECT a, b, cnt FROM tag_edges WHERE a='zzz-test-A' AND b='zzz-test-B'").fetchone()))
    con.close()
    assert row["cnt"] == 2, "共现次数 = 同时含两实体的 chunk 数"


def test_increment_idempotent(empty_db):
    _prep_db(empty_db)
    _put_entities(empty_db, {
        "c1": ["zzz-test-A", "zzz-test-B"],
        "c2": ["zzz-test-A", "zzz-test-B"],
    })
    G.rebuild_edges(db_path=str(empty_db))
    G.increment_for_chunk("c1", db_path=str(empty_db))
    G.increment_for_chunk("c1", db_path=str(empty_db))  # 重复调用不翻倍
    con = sqlite3.connect(str(empty_db))
    cnt = con.execute("SELECT cnt FROM tag_edges WHERE a='zzz-test-A' AND b='zzz-test-B'").fetchone()[0]
    con.close()
    assert cnt == 2, f"增量幂等：重建与两次增量后应同值，实际 {cnt}"


# ── P5.0-b：传播 ─────────────────────────────────────────────

def test_propagate_chain_and_idf(empty_db):
    """守卫①链式：A→B→C 共现链，seed=A → C 有传播分；孤立 D 得 0（守卫②不误伤）。"""
    _prep_db(empty_db)
    chunks = {
        "c1": ["zzz-test-A"],
        "c2": ["zzz-test-A", "zzz-test-B"],
        "c3": ["zzz-test-B", "zzz-test-C"],
        "c4": ["zzz-test-D"],
    }
    _put_entities(empty_db, chunks)
    G.rebuild_edges(db_path=str(empty_db))
    edges, df, N = G.load_graph(db_path=str(empty_db))
    # 纯传播（不依赖 db 的别名抽取——seed 直接给）
    sc = G.propagate({"zzz-test-A": 1.0}, edges, df, N)
    assert sc["zzz-test-A"] > sc["zzz-test-B"] > sc["zzz-test-C"] > 0, "链式衰减：A>B>C>0"
    assert "zzz-test-D" not in sc, "孤立节点（无共现边）不进图，传播分 0"
    # IDF：df 高节点权重低——A(2) 的扩散比 C(1) 弱，验证 min(idf) 已生效（方向性断言）
    assert len(sc) == 3, "节点集合 = seed ∪ 边端点"


# ── P5.0-c：读侧 ─────────────────────────────────────────────

def test_doc_boosts_chain(empty_db):
    _prep_db(empty_db)
    _put_entities(empty_db, {
        "d1": ["zzz-test-A"],
        "d2": ["zzz-test-A", "zzz-test-B"],
        "d3": ["zzz-test-B", "zzz-test-C"],
        "d4": ["zzz-test-D"],
    })
    G.rebuild_edges(db_path=str(empty_db))
    lex = {"zzz-test-a": "zzz-test-A"}
    boosts = G.doc_boosts("zzz-test-a 语义链", ["d1", "d2", "d3", "d4"],
                          db_path=str(empty_db), lexicon=lex)
    assert boosts["d1"] > boosts["d3"] > 0, "源实体文档 > 两跳外文档 > 0"
    assert boosts["d4"] == 0, "无实体/无路径文档传播分为 0（守卫②）"
    assert G.doc_boosts("完全无实体查询", ["d1"], db_path=str(empty_db), lexicon=lex) == {}, "空 seed 诚实降级为零"


def test_search_wiring_weight_zero_and_positive(monkeypatch):
    """接线测试：weight=0 不调 doc_boosts（逐字节一致）；weight>0 非负加分。"""
    docs = [
        _mk("zzz-d1", "zzz-test-aaa 第一条内容"),
        _mk("zzz-d2", "zzz-test-aaa 第二条内容"),
        _mk("zzz-d3", "zzz-test-aaa 第三条内容"),
    ]
    rec = HybridRetriever()
    rec.index(docs)
    called = {"n": 0}

    def fake_boosts(query, doc_ids, **kw):
        called["n"] += 1
        return {"zzz-d1": 0.2, "zzz-d2": 0.1, "zzz-d3": 0.9}

    now = time.time()  # 固定 now：recency 项不随两次查询间的墙钟漂移（浮点可比）
    baseline = {d.id: s for d, s in rec.search("zzz-test-aaa", top_k=10, now=now)}

    # weight=0（默认路径）：doc_boosts 不应被调用，排序与基线一致
    rec.graph_prop_weight = 0.0
    monkeypatch.setattr(G, "doc_boosts", fake_boosts)
    got0 = rec.search("zzz-test-aaa", top_k=10, now=now)
    assert called["n"] == 0, "weight=0 必须跳过图传播（行为与现状逐字节一致）"
    assert {d.id: s for d, s in got0} == baseline

    # weight>0：非负加分，且高 B_G 文档提升名次
    rec.graph_prop_weight = 1.0
    got1 = {d.id: s for d, s in rec.search("zzz-test-aaa", top_k=10, now=now)}
    assert called["n"] == 1
    assert got1["zzz-d3"] - baseline["zzz-d3"] == pytest.approx(0.9, abs=1e-9), "S_new = S0 + w·B_G"
    assert got1["zzz-d1"] - baseline["zzz-d1"] == pytest.approx(0.2, abs=1e-9)
    assert got1["zzz-d1"] >= baseline["zzz-d1"], "非负结构增量不扣分"


def test_doc_boosts_real_db_chain_via_search(monkeypatch, empty_db):
    """端到端：search() 走真库 doc_boosts（DEFAULT_DB monkeypatch + 固定 seed），
    链式 C 文档被传播分抬进 top 顺序。"""
    _prep_db(empty_db)
    _put_entities(empty_db, {
        "e-d1": ["zzz-test-A"],
        "e-d2": ["zzz-test-A", "zzz-test-B"],
        "e-d3": ["zzz-test-B", "zzz-test-C"],
        "e-d4": ["zzz-test-D"],
    })
    G.rebuild_edges(db_path=str(empty_db))
    monkeypatch.setattr(G, "DEFAULT_DB", str(empty_db))
    # seed 抽取用固定归一（抽取器本身由 test_doc_boosts_chain 覆盖）
    monkeypatch.setattr(G, "_query_seed",
                        lambda q, lexicon=None, db_path=None: {"zzz-test-A": 1.0} if "zzz-test-a" in q.lower() else {})

    rec = HybridRetriever()
    rec.index([
        _mk("e-d1", "zzz-test-aaa 甲"),
        _mk("e-d2", "zzz-test-aaa 乙"),
        _mk("e-d3", "zzz-test-aaa 丙"),
        _mk("e-d4", "完全无关的第四篇"),
    ])
    rec.graph_prop_weight = 1.0
    res = rec.search("zzz-test-aaa", top_k=10)
    by_id = {d.id: s for d, s in res}
    assert "e-d3" in by_id, "链式传播应让 C 文档进入召回（有传播分 > 0）"
    assert by_id["e-d3"] > 0
    assert "e-d4" not in by_id or by_id.get("e-d4", 0) == 0, "无实体/无路径文档传播分 0（不误伤）"
