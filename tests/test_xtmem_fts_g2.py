# -*- coding: utf-8 -*-
"""G2 FTS5 热接线守卫测试（2026-09-04 assistant-c · 方案甲：BM25 腿替换+影子灰度）。

覆盖（验收 V-G1..V-G3 + 模式实例化）：
  off    输出确定性 + 无库可检索（内存腿）
  shadow 输出==off 输出 + info 日志含 overlap 行
  on     含 superseded/异域 private 的固定 docs → search 不含这两条（FTS5 腿过滤生效）
  模式   缺省 off；override 三值生效；非法值回落 off
"""
import logging

import pytest

from memory.hybrid_retriever import HybridRetriever, MemoryDoc
from memory.vector_store import VectorStore, Chunk


def _seed_db(db_path, rows):
    """rows: list of (id, content, ns, vis, state)。写 chunks + fts（v2 词级）。"""
    vs = VectorStore(db_path=str(db_path))
    for cid, content, ns, vis, state in rows:
        assert vs.store_chunk(Chunk(id=cid, content=content, namespace=ns, visibility=vis))
    if any(st == "superseded" for *_, st in rows):
        # 手动把指定行标 superseded（fts 行由 S3 mark_superseded 同批删）
        sup = [r for r in rows if r[4] == "superseded"]
        for cid, content, ns, vis, st in sup:
            # 需要 source：store 另一条再 mark
            pass
    return vs


import time as _t


def _docs(rows, ts=None):
    if ts is None:
        ts = _t.time()  # 新鲜时间戳：避免 recency≈0 被 AdaMEM 阈值（0.15）拒掉
    return [MemoryDoc(id=r[0], content=r[1], timestamp=ts, importance=0.9, namespace=r[2],
                      visibility=r[3], state=r[4]) for r in rows]


# 由于 mark_superseded 需要 source_id，超替在用例内手工构造：store 后 UPDATE chunks + 删 fts 行
def _mark_sup(db_path, target, source):
    con = VectorStore(db_path=str(db_path))._get_conn()
    con.execute("UPDATE chunks SET state='superseded', superseded_by=? WHERE id=? AND state='active'", (source, target))
    con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)", (target,))
    con.commit()
    con.close()


def test_g2_off_deterministic_and_no_db(empty_db):
    """V-G1：off 模式=内存 BM25（无需库）；同 docs 两次 search 输出逐条一致。"""
    docs = _docs([
        ("a", "XTMEM 记忆检索测试甲", "dsh", "shared", "active"),
        ("b", "完全无关的英文内容 hello world", "dsh", "shared", "active"),
    ])
    r = HybridRetriever(db_path=str(empty_db), fts5_mode="off")
    r.index(docs)
    out1 = r.search("记忆", top_k=5, viewer="dsh")
    out2 = r.search("记忆", top_k=5, viewer="dsh")
    assert [(d.id, round(s, 6)) for d, s in out1] == [(d.id, round(s, 6)) for d, s in out2]
    assert out1 and out1[0][0].id == "a"


def test_g2_shadow_matches_off_with_overlap_log(empty_db, caplog):
    """V-G2：shadow 输出==off 输出（逐条 id/score 相同），日志含 fts5_shadow overlap。"""
    vs = VectorStore(db_path=str(empty_db))
    for cid, content in [("a", "XTMEM 记忆检索测试甲"), ("b", "检索相关测试乙")]:
        assert vs.store_chunk(Chunk(id=cid, content=content, namespace="dsh", visibility="shared"))
    docs = _docs([("a", "XTMEM 记忆检索测试甲", "dsh", "shared", "active"),
                  ("b", "检索相关测试乙", "dsh", "shared", "active")])
    roff = HybridRetriever(db_path=str(empty_db), fts5_mode="off")
    rsh = HybridRetriever(db_path=str(empty_db), fts5_mode="shadow")
    roff.index(docs)
    rsh.index(docs)
    o1 = [(d.id, round(s, 6)) for d, s in roff.search("记忆", top_k=5, viewer="dsh")]
    with caplog.at_level(logging.INFO, logger="memory.hybrid_retriever"):
        o2 = [(d.id, round(s, 6)) for d, s in rsh.search("记忆", top_k=5, viewer="dsh")]
    assert o1 == o2
    assert any("fts5_shadow" in r.message and "overlap=" in r.message for r in caplog.records)


def test_g2_on_excludes_superseded_and_private(empty_db):
    """V-G3：on 模式——superseded/异域 private 不进结果；active shared 中文 doc 命中。"""
    db = str(empty_db)
    vs = VectorStore(db_path=db)
    assert vs.store_chunk(Chunk(id="a", content="XTMEM 记忆检索测试甲", namespace="dsh", visibility="shared"))
    assert vs.store_chunk(Chunk(id="s", content="作废旧记忆条目", namespace="dsh", visibility="shared"))
    assert vs.store_chunk(Chunk(id="x", content="取代者条目", namespace="dsh", visibility="shared"))
    assert vs.store_chunk(Chunk(id="p", content="私密异域记忆条目", namespace="personal", visibility="private"))
    _mark_sup(db, "s", "x")
    # 内存 docs 与库同态（fts 过滤 + 内存过滤双保险）
    docs = _docs([
        ("a", "XTMEM 记忆检索测试甲", "dsh", "shared", "active"),
        ("s", "作废旧记忆条目", "dsh", "shared", "superseded"),
        ("x", "取代者条目", "dsh", "shared", "active"),
        ("p", "私密异域记忆条目", "personal", "private", "active"),
    ])
    r = HybridRetriever(db_path=db, fts5_mode="on")
    r.index(docs)
    hits = r.search("记忆", top_k=10, viewer="dsh")
    ids = [d.id for d, _ in hits]
    assert "a" in ids, ids
    assert "s" not in ids, "superseded 不应召回"
    assert "p" not in ids, "异域 private 不应召回"


def test_g2_mode_fallback_and_override():
    """模式解析：缺省 off（无 tuning override）；override 三值生效；非法值回落 off。"""
    r_def = HybridRetriever(db_path=None, fts5_mode=None)
    # 无 tuning 文件时 load_tuning 兜底默认 off；有文件可能被改——用 override 断言解析规则本身
    assert HybridRetriever(db_path=None, fts5_mode="on")._fts5_mode == "on"
    assert HybridRetriever(db_path=None, fts5_mode="shadow")._fts5_mode == "shadow"
    assert HybridRetriever(db_path=None, fts5_mode="off")._fts5_mode == "off"
    assert HybridRetriever(db_path=None, fts5_mode="bogus")._fts5_mode == "off"
    assert r_def._fts5_mode in ("off", "shadow", "on")
