# -*- coding: utf-8 -*-
"""test_xtmem_audit_batch.py — 外部审计整改总单守卫（2026-09-06，assistant-c）。

守卫清单（整改总单七件）：
  ① test_store_chunk_rollback_on_error：store_chunk 异常 → rollback 无事务残留 → 后续 store 干净；
  ② test_reembed_cache_text_placeholder：cmd_reembed 写空占位 text（不落明文）；
  ③ test_lineage_stale_excluded_retriever：lineage-stale 标签条检索不召回；
  ④ test_lineage_stale_excluded_provider：provider 注入 SQL 排除 stale（画像/预判）；
  ⑤ test_graph_viewer_filter：私有贡献实体不出现在他人图（entities/tag_edges）；
  ⑥ test_build_claim_skips_placeholder：占位标题过滤 + preference 板块优先；
  ⑦ test_predict_distill_gate_units_fallback：distill_units 成功面回退放行（观测口修复）。
⚠️ R7：全部临时库自证，禁写生产。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import sys
import time
import uuid

import pytest

from conftest import PLUGIN_DIR

sys.path.insert(0, str(PLUGIN_DIR))

from memory.hybrid_retriever import HybridRetriever, MemoryDoc
from memory.vector_store import VectorStore, Chunk


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _mk_chunk(cid, content, ns="dsh", vis="private", tags=None, track="default"):
    return Chunk(
        id=cid, content=content, timestamp=time.time(), importance=0.6,
        tags=tags or [], source="t", token_count=len(content), content_hash="",
        track=track, state="active", namespace=ns, visibility=vis, tier="raw",
    )


# ── ① store_chunk 异常回滚 ──────────────────────────────────────

def test_store_chunk_rollback_on_error(tmp_db, monkeypatch):
    """P1-5：FTS 分词抛异常 → 事务回滚 → 无残疾行 → 后续 store 干净。"""
    from memory import vector_store as VS
    vs = VectorStore(db_path=str(tmp_db))
    # 故障注入：_tokenize_for_fts5 抛异常（第一次调用时）
    orig = VS._tokenize_for_fts5
    calls = {"n": 0}
    def boom(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected fts failure")
        return orig(text)
    monkeypatch.setattr(VS, "_tokenize_for_fts5", boom)
    bad_id = "mem-bad" + _uid()
    try:
        vs.store_chunk(_mk_chunk(bad_id, "坏行正文"))
    except RuntimeError:
        pass
    else:
        pytest.fail("故障注入应抛异常")
    # 事务无残留：连接 in_transaction 应为 False
    conn = vs._get_conn()
    assert not conn.in_transaction, "异常后连接不应有残留事务"
    # 无残疾行：本次注入的坏行不得落库（用唯一 id，别查 LIKE——canonical 副本可能有历史 mem-bad*）
    con = sqlite3.connect(str(tmp_db))
    n_bad = con.execute("SELECT COUNT(*) FROM chunks WHERE id=?", (bad_id,)).fetchone()[0]
    assert n_bad == 0, "异常残留的 chunk 不得落库"
    con.close()
    # 后续 store 干净（成对）
    ok = vs.store_chunk(_mk_chunk("mem-okc3" + _uid(), "好行正文"))
    assert ok is True
    con = sqlite3.connect(str(tmp_db))
    r = con.execute("SELECT c.id, f.id fts_id FROM chunks c LEFT JOIN chunks_fts f ON f.rowid=c.rowid WHERE c.id LIKE 'mem-ok%'").fetchall()
    assert len(r) == 1 and r[0][1] is not None, "后续 store chunks/fts 应成对"
    con.close()
    vs.close()


# ── ② reembed 空占位 ────────────────────────────────────────────

def test_reembed_cache_text_placeholder(tmp_db, monkeypatch):
    """cmd_reembed 写 cache：text 列空占位（不落明文正文）。"""
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    # 造一条 chunk
    vs = VectorStore(db_path=str(tmp_db))
    vs.store_chunk(_mk_chunk("mem-re" + _uid(), "reembed 明文样本正文"))
    vs.close()
    con = sqlite3.connect(str(tmp_db))
    # 手动塞一条缺 embedding 的 cache 记录？直接跑 cmd_reembed 的写入逻辑验证 text='' 
    # 用 CLI 完整跑太重（要 ollama）——静态验证：源码 cache INSERT 的 text 参数为 ''
    src = open(_REPO / "xtmem"/ "xtmem.py", encoding="utf-8").read()
    # 找 cmd_reembed 的 INSERT 行上下文
    assert "text, chunk_id, embedding, dim, model, created_at" in src
    # 取 INSERT 语句附近文本——参数元组 text 位必须是 ""（空占位），不能是正文变量
    idx = src.index("INSERT OR REPLACE INTO embedding_cache (text_hash, text")
    seg = src[idx:idx + 400]
    assert '""' in seg and "text, mid, blob" not in seg.replace('("", mid, blob', "OK"), seg[:300]
    # 更强断言：参数元组形如 (th, "", mid, blob, ...)
    assert 'th, "", mid' in seg, "reembed 写 cache 必须空占位: " + seg[:200]
    con.close()


# ── ③ retriever stale 排除 ──────────────────────────────────────

def test_lineage_stale_excluded_retriever(tmp_db):
    """P1-10：检索端带 lineage-stale 标签条不召回。"""
    from memory.embedding_provider import SimpleHashEmbedding
    emb = SimpleHashEmbedding(dim=64)
    ret = HybridRetriever(embedding_provider=emb, relevance_threshold=0.0,
                          db_path=str(tmp_db))
    stale = MemoryDoc(id="mem-stale" + _uid(), content="带病派生条", timestamp=time.time(),
                      importance=0.9, tags=["lineage-stale:abcdef"], namespace="dsh",
                      visibility="shared", track="default", state="active", tier="refined")
    good = MemoryDoc(id="mem-good" + _uid(), content="健康条", timestamp=time.time(),
                     importance=0.9, tags=[], namespace="dsh", visibility="shared",
                     track="default", state="active", tier="refined")
    ret.index([stale, good], embeddings=[emb.embed_single(stale.content), emb.embed_single(good.content)])
    res = ret.search("带病派生条", top_k=5, viewer="dsh")
    ids = [d.id for d, _s in res]
    assert not any(i.startswith("mem-stale") for i in ids), "stale 条不得召回: %s" % ids
    assert any(i.startswith("mem-good") for i in ids), "健康条应召回: %s" % ids


# ── ④ provider 注入 SQL 排除 ────────────────────────────────────

def test_lineage_stale_excluded_provider(tmp_db):
    """provider 注入 SQL 含 lineage-stale 排除（画像/预判两处静态断言）。"""
    src = open(_REPO / "plugins"/ "xtmem"/ "provider.py", encoding="utf-8").read()
    # 断言随判据同步（2026-09-13 二合二件①）：provider 排除判据已收紧为
    # 引号+冒号精确真标记（裸词=技术关键词不排除，防裸词误伤自复制，与前单
    # 主通道同口径）——静态断言若仍背书旧松串，判据一收紧守卫自己就红
    #（"断言给旧行为背书"，P7.5 同族）。
    # 预判 SQL
    assert "tags NOT LIKE '%\\\"lineage-stale:%'" in src, "预判注入缺 stale 排除"
    # 画像 SQL
    assert src.count("tags NOT LIKE '%\\\"lineage-stale:%'") >= 3, "画像/时间骨架/预判应全有排除"


# ── ⑤ 图 API viewer 过滤 ───────────────────────────────────────

def test_graph_viewer_filter(tmp_db, monkeypatch):
    """P1-9：私有贡献实体不出现在他人图（entities 重算+边两端过滤）。"""
    monkeypatch.setenv("XTMEM_ACTOR", "t")
    vs = VectorStore(db_path=str(tmp_db))
    # 造共享+私有各一条：共享=dsh 域，私有=zcode 域（他人私有——dsh 视角不该看到）
    shared = _mk_chunk("mem-sh" + _uid(), "共享实体A 内容", ns="dsh", vis="shared")
    shared.tags = ["实体A"]
    priv = _mk_chunk("mem-pr" + _uid(), "私有实体B 内容", ns="zcode", vis="private")
    priv.tags = ["实体B"]
    vs.store_chunk(shared)
    vs.store_chunk(priv)
    vs.close()
    # 打 chunk_entities（用真实 chunk id，前缀匹配）
    con = sqlite3.connect(str(tmp_db))
    rows = con.execute("SELECT id FROM chunks WHERE id LIKE 'mem-sh%' OR id LIKE 'mem-pr%'").fetchall()
    ids_by_pref = {}
    for (cid,) in rows:
        if cid.startswith("mem-sh"):
            ids_by_pref["A"] = cid
        else:
            ids_by_pref["B"] = cid
    con.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES (?,?)", (ids_by_pref["A"], "实体A"))
    con.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES (?,?)", (ids_by_pref["B"], "实体B"))
    con.execute("INSERT INTO entities (canon, count, first_seen, last_seen) VALUES ('实体A', 1, ?, ?)", (time.time(), time.time()))
    con.execute("INSERT INTO entities (canon, count, first_seen, last_seen) VALUES ('实体B', 1, ?, ?)", (time.time(), time.time()))
    con.commit()
    con.close()
    # 调 graph_of（viewer=dsh 非 admin）
    import memory.cockpit_contract as cc
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    g = cc.graph_of(con, viewer="dsh")
    entities = [e["label"] for e in g.get("entities", [])]
    assert "实体B" not in entities, "私有贡献实体不得出现在 dsh 视角图"
    assert "实体A" in entities, "共享贡献实体应在图"
    con.close()


# ── ⑥ _build_claim 双修 ────────────────────────────────────────

def test_build_claim_skips_placeholder(tmp_db):
    """占位标题过滤 + preference 板块优先。"""
    sys.path.insert(0, _REPO / "scripts")
    from xtmem_predict import _build_claim
    # 画像：identity 板块首行=反思占位 + preference 板块有真实行为
    portrait = ("x", "# 画像\n## identity\n- 【反思 · 2026-09-04 18:00】\n- 旧内容\n"
                "## preference\n- 先对账再动手\n- 每次跑测试前看基线")
    cluster = [{"id": "m1", "content": "审计对账流程要点"}]
    claim = _build_claim(portrait, cluster)
    assert "反思" not in claim, "占位标题不得进 claim"
    assert "先对账再动手" in claim, "preference 板块行为应优先"
    # 无 preference 板块：兜底扫全文跳占位
    portrait2 = ("x", "# 画像\n## identity\n- 【反思 · 2026-09-04 18:00】\n- 真实画像行")
    claim2 = _build_claim(portrait2, cluster)
    assert "反思" not in claim2
    assert "真实画像行" in claim2, "兜底应取非占位行"


# ── ⑦ distill 熔断回退 ─────────────────────────────────────────

def test_predict_distill_gate_units_fallback(tmp_db):
    """事件面无 distill 事件但 distill_units 有近期成功 → 放行。"""
    from memory.open_db import open_db
    # 建 distill_units + 近期 distilled 行
    con = open_db(str(tmp_db))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS distill_units (
          unit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source TEXT NOT NULL,
          ts_start REAL NOT NULL, ts_end REAL NOT NULL, turn_count INTEGER NOT NULL,
          char_count INTEGER NOT NULL, raw_ids TEXT NOT NULL, state TEXT NOT NULL,
          distilled_at TEXT, refined_ids TEXT, created_at REAL NOT NULL
        )
    """)
    con.execute("INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, turn_count, char_count, raw_ids, state, distilled_at, refined_ids, created_at) VALUES (?,?,?,?,?,?,?,?, 'distilled', ?, '[]', ?)",
        ("t1", "dsh", "s", time.time()-4000, time.time()-3900, 2, 100, "[]",
         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()-3600)), time.time()-3600))
    con.commit()
    con.close()
    sys.path.insert(0, _REPO / "scripts")
    import xtmem_predict as P
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    ok, why = P._distill_ok_recently(con)
    assert ok, "distill_units 成功面应放行: " + str(why)
    con.close()
