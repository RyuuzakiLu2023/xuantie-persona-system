# -*- coding: utf-8 -*-
"""test_xtmem_embver.py — 贰③c embedding_version 锁芯验收（2026-09-04 assistant-c,工单1实测）。

覆盖五态：
  1. 首启落键      — 全新库第一次 hydrate 后 xtmem_meta.embedding_version=<model>@<dim>
  2. 改错键值      — 手动改成异指纹 → 向量腿 fail-closed（BM25-only）+ embedding_version_mismatch 事件
  3. 改回恢复      — 键写回正确指纹（模拟下次健康启动）→ 新 builder 向量腿重开
  4. 自愈写回      — mismatch 启动周期内键已被重写成规范指纹（下次启动=态3）
  5. Ollama 不可达 — dim 探针抛异常 → 不落键不关闸（无证据不 fail-closed）

【不】依赖 canonical 生产库——全部 empty_db 模式自建（P1-I 环境隔离纪律）。
【不】真连 Ollama——SimpleHashEmbedding 注入 provider，dim 恒定可控。
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from conftest import PLUGIN_DIR

import sys as _sys
_sys.path.insert(0, str(PLUGIN_DIR))

from memory.context_builder import ContextBuilder
from memory.embedding_provider import SimpleHashEmbedding
from memory.hybrid_retriever import make_embedding_fingerprint, EMBEDDING_META_KEY
from memory.open_db import open_db, get_meta, set_meta


def _builder(eng_dir: str) -> ContextBuilder:
    """empty_db 模式建 builder：SimpleHashEmbedding（dim=128 恒定，指纹 hash-128@128）。

    ⚠️ config 必须传 embedding_model 覆盖默认 bge-m3——默认值会走 OllamaEmbedding。
    """
    eng = Path(eng_dir)
    engdb = str(eng / "v.db")
    b = ContextBuilder(
        workspace_dir=str(eng),
        config={"db_path": engdb, "prefetch_only": True, "rest_api": False,
                "compression_enabled": False, "embedding_model": "hash-128"},
    )
    # ⚠️ ctor 里 _hydrate_retriever 已用原 OllamaEmbedding 跑过锁芯（探针失败→跳过，无害）——
    # 替换成确定性 hash provider 后必须显式重跑 _check_embedding_version()，锁芯才真正走 hash 指纹
    b._embedding = SimpleHashEmbedding(dim=128)
    b._retriever.embedding_provider = b._embedding
    b._check_embedding_version()
    return b


def _embver(db_path: str):
    con = open_db(str(db_path))
    try:
        return get_meta(con, EMBEDDING_META_KEY)
    finally:
        con.close()


def _set_embver(db_path: str, value: str):
    con = open_db(str(db_path))
    try:
        set_meta(con, EMBEDDING_META_KEY, value)
    finally:
        con.close()


def _events(db_path: str, kind: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT meta FROM xtmem_events WHERE kind=? ORDER BY ts", (kind,)
        ).fetchall()
    finally:
        con.close()


FP = make_embedding_fingerprint("hash-128", 128)  # hash-128@128


def test_embver_1_seed_on_first_start(empty_db):
    """态①：首启落键——第一次 hydrate 后键=模型@维度指纹。"""
    b = _builder(str(empty_db.parent))
    try:
        assert _embver(str(empty_db)) == FP, "首启应落键 %r" % FP
        kinds = [k[0] for k in _events(str(empty_db), "embedding_version_seeded")]
        assert kinds, "落键应留 embedding_version_seeded 事件"
    finally:
        b.close()


def test_embver_2_mismatch_fails_closed(empty_db):
    """态②：手动改错键值 → 向量腿 fail-closed（BM25-only）+ mismatch 事件。

    工单验收口径：改错键 → 检索退 BM25 且有事件。
    """
    db = str(empty_db)
    b = _builder(str(empty_db.parent))
    try:
        assert _embver(db) == FP
        # 存两条不同命中的记忆（一条 BM25 强命中，一条只有向量可能救）
        b.store("XTMEM 锁芯验收记忆：蓝鲸在深海唱歌", metadata={})
        b.store("无关填充记忆：沙漠里的仙人掌开花", metadata={})
    finally:
        b.close()
    # 手动把键改成异指纹（模拟模型换了没重建缓存）
    _set_embver(db, "bge-m3@1024")
    b2 = _builder(str(empty_db.parent))
    try:
        # 闸断：向量腿关，只走 BM25
        assert b2._retriever.vector_leg_ok is False
        assert "mismatch" in b2._retriever.vector_leg_reason
        hits = b2._retriever.search("蓝鲸 深海", top_k=5)
        assert hits, "BM25 腿应照常出活"
        assert any("蓝鲸" in d.content for d, _ in hits), "BM25 命中不应受闸影响"
        # mismatch 事件在册
        evs = _events(db, "embedding_version_mismatch")
        assert evs, "mismatch 必须留事件"
        assert "bge-m3@1024" in evs[-1][0] and FP in evs[-1][0], evs[-1][0]
        # 自愈写回：mismatch 启动周期内键已重写成规范指纹
        assert _embver(db) == FP, "健康启动自愈写回"
    finally:
        b2.close()


def test_embver_3_recover_after_key_restored(empty_db):
    """态③：键写回正确指纹后重启 → 向量腿重开（恢复）。"""
    db = str(empty_db)
    b = _builder(str(empty_db.parent))
    try:
        b.store("恢复验收：锚点内容鲲鹏展翅", metadata={})
    finally:
        b.close()
    _set_embver(db, "wrong@1")
    b2 = _builder(str(empty_db.parent))
    try:
        assert b2._retriever.vector_leg_ok is False  # 第一次启动惩罚一个周期
    finally:
        b2.close()
    assert _embver(db) == FP  # 自愈已写回
    b3 = _builder(str(empty_db.parent))
    try:
        assert b3._retriever.vector_leg_ok is True, "键改回后新启动应恢复向量腿"
        hits = b3._retriever.search("鲲鹏 展翅", top_k=5)
        assert any("鲲鹏" in d.content for d, _ in hits)
    finally:
        b3.close()


def test_embver_4_no_dim_evidence_no_gate(empty_db, monkeypatch):
    """态④：dim 探针不可得（Ollama 挂）→ 不落键、不关闸（无证据不 fail-closed）。"""
    db = str(empty_db)
    b = _builder(str(empty_db.parent))
    try:
        assert _embver(db) == FP  # 先有健康基线
        def _boom():
            raise RuntimeError("ollama down")
        monkeypatch.setattr(b._embedding, "dim", _boom)
        b._check_embedding_version()  # 重跑锁芯
        assert b._retriever.vector_leg_ok is True, "dim 取不到不许关闸"
        assert _embver(db) == FP, "已有键不许被改"
    finally:
        b.close()


def test_embver_5_search_no_embed_call_when_gated(empty_db, monkeypatch):
    """态⑤（加固）：闸断时 search() 现场不许 embed 查询（防旧指纹向量混算）。"""
    b = _builder(str(empty_db.parent))
    try:
        b.store("闸断加固验收内容", metadata={})
        _set_embver(str(empty_db), "other@7")
        b._check_embedding_version()
        assert b._retriever.vector_leg_ok is False
        def _no_embed(text):
            raise AssertionError("闸断后不许现场 embed")
        monkeypatch.setattr(b._retriever, "_embed", _no_embed)
        hits = b._retriever.search("闸断 加固", top_k=5)
        assert isinstance(hits, list)  # 不炸、不 embed、BM25 照常
    finally:
        b.close()