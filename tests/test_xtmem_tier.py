# -*- coding: utf-8 -*-
"""XTMEM tier 分层检索测试（Phase T 补 · 2026-09-01，小D + T-3 jieba 跳过轴）。"""

import time

import pytest
import axis_predicates
from memory.hybrid_retriever import HybridRetriever, MemoryDoc

pytestmark = pytest.mark.skipif(not axis_predicates.jieba_available(), reason="jieba 不可用（BM25 中文分词）")


def _mk(doc_id: str, content: str, tier: str) -> MemoryDoc:
    return MemoryDoc(id=doc_id, content=content, timestamp=time.time(),
                     importance=0.9, namespace="dsh", visibility="shared", tier=tier)


def test_tier_two_jump_disjoint():
    rec = HybridRetriever()
    rec.index([
        _mk("m-ref-1", "玄铁偏好本地 Ollama bge-m3 部署", "refined"),
        _mk("m-raw-1", "User: 这个方案怎么改\nAssistant: 必须先看下前提再改", "raw"),
    ])
    ref_ids = {d.id for d, _ in rec.search("部署", top_k=5, tier_filter="refined")}
    raw_ids = {d.id for d, _ in rec.search("部署", top_k=5, tier_filter="raw")}
    assert ref_ids.isdisjoint(raw_ids), "refined 与 raw 两跳必须不相交（分层语义）"


def test_tier_none_not_restricted():
    rec = HybridRetriever()
    rec.index([
        _mk("m-ref-1", "玄铁偏好本地 Ollama bge-m3 部署", "refined"),
        _mk("m-raw-1", "User: 这个方案怎么改\nAssistant: 必须先看下前提再改", "raw"),
        _mk("m-ref-2", "画像是私产，归属当前角色域", "refined"),
    ])
    res_all = rec.search("部署", top_k=5)
    assert res_all, "tier_filter=None 不应返回空（有无过滤都应有命中）"
    assert {d.id for d, _ in res_all}, "None 路径必须返回结果"


def test_tier_empty_pool_safe():
    rec = HybridRetriever()
    rec.index([_mk("m-ref-1", "玄铁偏好本地 Ollama bge-m3 部署", "refined")])
    assert rec.search("部署", top_k=5, tier_filter="raw") == [], "空 tier 池应安全返回 []，不抛错"


def test_search_two_tier_routing():
    rec = HybridRetriever()
    rec.index([
        _mk("m-ref-要点", "要点：玄铁部署偏好是本地 Ollama bge-m3", "refined"),
        _mk("m-raw-天气", "User: 今天天气怎么样\nAssistant: 挺好的", "raw"),
    ])
    r1 = [d.id for d, s in rec.search_two_tier("部署偏好", top_k=3)]
    assert r1 == ["m-ref-要点"], f"要点 query 应首跳命中 refined，实际 {r1}"
    r2 = [d.id for d, s in rec.search_two_tier("天气", top_k=3)]
    assert r2 == ["m-raw-天气"], f"闲聊 query 应回落 raw，实际 {r2}"
    assert set(r1).isdisjoint(set(r2)), "两跳结果必须不相交"
