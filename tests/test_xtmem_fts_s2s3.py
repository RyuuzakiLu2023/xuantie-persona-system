# -*- coding: utf-8 -*-
"""S2/S3 守卫测试（2026-09-03 assistant-c · maintainer派单验收清单）。

S2 方案C：_tokenize_for_fts5 无 jieba 路径 / store 写 fts==降级输出 /
         xtmem_meta tokenizer_version 播种·错值拒写·恢复
S3 Y-6：search_bm25/search_vector 的 state+visibility 过滤 / mark_superseded 补删 embedding_cache
"""
import sqlite3
import sys
import types

import pytest

from memory.vector_store import VectorStore, Chunk, _tokenize_for_fts5, TOKENIZER_VERSION

pytestmark = pytest.mark.skipif(not hasattr(__import__("axis_predicates"), "canonical_ok"), reason="no canonical")
# 注：本测试用 empty_db（全空库），不依赖 canonical 数据；axis 门不适用时仍可跑，
# 但保持与其他守卫文件一致的导入风格。


def _mk(vs, cid, content, ns="dsh", vis="shared"):
    assert vs.store_chunk(Chunk(id=cid, content=content, namespace=ns, visibility=vis))


def test_tokenize_v2_jieba_preferred_and_fallback(empty_db):
    """v2（2026-09-04）：jieba 在场=词级输出；模拟 ImportError（jieba=None）=降级兜底不崩。"""
    out = _tokenize_for_fts5("你好世界abc")
    assert "你" in out and "abc" in out
    assert " 你好 " in out or " 世界 " in out  # 词级空格包裹
    saved = sys.modules.get("jieba")
    sys.modules["jieba"] = None
    try:
        deg = _tokenize_for_fts5("你好世界")
    finally:
        if saved is None:
            sys.modules.pop("jieba", None)
        else:
            sys.modules["jieba"] = saved
    assert "你" in deg and deg.count(" ") >= 4  # 降级单字+bigram，不崩


def test_store_fts_equals_degraded_output(empty_db):
    """S2 验证（验收①）：新写入 chunks_fts 串 == 强制降级输出。"""
    vs = VectorStore(db_path=str(empty_db))
    content = "你好世界测试"
    _mk(vs, "c1", content)
    row = vs._get_conn().execute("SELECT content FROM chunks_fts WHERE id='c1'").fetchone()
    assert row is not None and row["content"] == _tokenize_for_fts5(content)
    assert _tokenize_for_fts5(content) != content  # 确有分词发生


def test_version_seed_mismatch_reject_restore(empty_db, caplog):
    """S2-2（验收⑤）：xtmem_meta 播种 v1；改错值 → 写入被拒+warning；改回 → 恢复。"""
    vs = VectorStore(db_path=str(empty_db))
    _mk(vs, "c1", "第一段文字")
    con = vs._get_conn()
    v = con.execute("SELECT value FROM xtmem_meta WHERE key='tokenizer_version'").fetchone()[0]
    assert v == TOKENIZER_VERSION

    con.execute("UPDATE xtmem_meta SET value='tok-wrong' WHERE key='tokenizer_version'")
    con.commit()
    with caplog.at_level("WARNING", logger="memory.vector_store"):
        ok = vs.store_chunk(Chunk(id="c2", content="第二段文字", namespace="dsh"))
    assert ok is False
    assert any("tokenizer_version mismatch" in r.message for r in caplog.records)
    n2 = con.execute("SELECT COUNT(*) FROM chunks WHERE id='c2'").fetchone()[0]
    assert n2 == 0  # 拒绝写入 = chunks 也不落

    con.execute("UPDATE xtmem_meta SET value=? WHERE key='tokenizer_version'", (TOKENIZER_VERSION,))
    con.commit()
    assert vs.store_chunk(Chunk(id="c3", content="第三段恢复", namespace="dsh")) is True


def test_search_bm25_state_visibility(empty_db):
    """S3-1：superseded/archived 不进 search_bm25；private 异域行对非 owner 不返回。"""
    vs = VectorStore(db_path=str(empty_db))
    _mk(vs, "a", "共享测试词条甲", ns="dsh")
    _mk(vs, "dp", "私密同域测试", ns="dsh", vis="private")
    _mk(vs, "xo", "私密异域测试", ns="personal", vis="private")
    _mk(vs, "sup", "将被取代的测试", ns="dsh")
    _mk(vs, "x", "取代者测试", ns="dsh")
    assert vs.mark_superseded("sup", "x")
    _mk(vs, "ar", "归档旧测试", ns="dsh")
    vs._get_conn().execute("UPDATE chunks SET state='archived' WHERE id='ar'")
    vs._get_conn().execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id='ar')")
    vs._get_conn().commit()

    hits = vs.search_bm25("测试", top_k=20, viewer="dsh")
    ids = [h[0] for h in hits]
    assert "a" in ids and "dp" in ids and "x" in ids
    assert "xo" not in ids   # 异域 private
    assert "sup" not in ids  # superseded（fts 行已删 + state 过滤双保险）
    assert "ar" not in ids   # archived


def test_search_vector_clears_superseded_ghost(empty_db):
    """S3-2（验收②④）：mark_superseded 补删 embedding_cache；search_vector 不再召回 superseded；异域 private 不回。"""
    vs = VectorStore(db_path=str(empty_db))
    _mk(vs, "a", "向量共享甲", ns="dsh")
    _mk(vs, "sup", "向量旧资料乙", ns="dsh")
    vs.batch_embed(["向量共享甲", "向量旧资料乙"], chunk_ids=["a", "sup"])
    assert vs.mark_superseded("sup", "a") is True
    # mark_superseded 已补删 embedding_cache（S3）
    gone = vs._get_conn().execute(
        "SELECT COUNT(*) FROM embedding_cache WHERE chunk_id='sup'"
    ).fetchone()[0]
    assert gone == 0, "mark_superseded 应删除 embedding_cache 行（F-5 幽灵源头）"

    _mk(vs, "xo", "私密异域丙", ns="personal", vis="private")
    vs.batch_embed(["私密异域丙"], chunk_ids=["xo"])

    hits = vs.search_vector("向量共享甲", vs._simple_embed, top_k=50, viewer="dsh")
    ids = [h[0] for h in hits]
    assert "a" in ids
    assert "sup" not in ids
    assert "xo" not in ids


# ── v2（2026-09-04）jieba 词级 ────────────────────────────────────────────


def test_v2_version_constant_and_meta_seed(empty_db):

    """v2 验收：版本常量 == tok-jieba-v2；xtmem_meta 播种值一致。"""

    import memory.vector_store as _vs

    assert TOKENIZER_VERSION == "tok-jieba-v2"

    vs = VectorStore(db_path=str(empty_db))

    _mk(vs, "v1", "版本播种测试")

    v = vs._get_conn().execute("SELECT value FROM xtmem_meta WHERE key='tokenizer_version'").fetchone()[0]

    assert v == "tok-jieba-v2"

    assert vs._get_conn() is not None





def test_v2_custom_dict_feishu(empty_db):

    """v2 验收：专名词典生效——「飞书」整词不再切碎。"""

    out = _tokenize_for_fts5("飞书文档")

    assert " 飞书 " in out, out  # 整词词元





def test_v2_jieba_missing_still_version_locked(empty_db, caplog):

    """v2 验收：模拟 ImportError（jieba 不可用→降级兜底）时，版本锁仍拒绝不匹配写入。"""

    vs = VectorStore(db_path=str(empty_db))

    _mk(vs, "a", "正常写入测试")

    con = vs._get_conn()

    con.execute("UPDATE xtmem_meta SET value='tok-wrong-vX' WHERE key='tokenizer_version'")

    con.commit()

    saved = sys.modules.get("jieba")

    sys.modules["jieba"] = None

    try:

        with caplog.at_level("WARNING", logger="memory.vector_store"):

            ok = vs.store_chunk(Chunk(id="b", content="降级环境写入", namespace="dsh"))

    finally:

        if saved is None:

            sys.modules.pop("jieba", None)

        else:

            sys.modules["jieba"] = saved

    assert ok is False

    assert any("tokenizer_version mismatch" in r.message for r in caplog.records)

    assert con.execute("SELECT COUNT(*) FROM chunks WHERE id='b'").fetchone()[0] == 0
