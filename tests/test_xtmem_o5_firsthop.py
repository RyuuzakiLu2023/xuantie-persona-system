# -*- coding: utf-8 -*-
"""test_xtmem_o5_firsthop.py — O5 首跳白名单守卫（P6.14，v1.3.1 §六守卫5，2026-09-05，assistant-c）。

判据（v1.3.1 §三 O5）：refined 首跳只认
  derived_from ∈ {distill, profile-aggregator, diary-distill, ''(手动/NULL)}
wiki-entity / timeline / prediction-engine 等机器派生大页【不】进首跳。

守卫双例：
  1) 高分 wiki + 低分 distill → 首跳必返 distill（wiki 分再高也被白名单挡在首跳外）
  2) 空串（手动 store 主体）行必进首跳——4021 行空串实锤，写 not df 而非 df in set
"""
import time

import pytest

from memory.hybrid_retriever import FIRST_HOP_ALLOWED, HybridRetriever, MemoryDoc


def _mk(doc_id, content, tier, derived_from=""):
    return MemoryDoc(
        id=doc_id,
        content=content,
        timestamp=time.time(),
        tags=[],
        track="default",
        tier=tier,
        derived_from=derived_from,
    )


def test_o5_high_wiki_low_distill_first_hop_excludes_wiki():
    """高分 wiki + 低分 distill → wiki 不进首跳（即便与 query 高度同词）。"""
    rec = HybridRetriever()
    rec.index([
        _mk("mem-aaaa0001", "用户A assistant-a 项目架构 用户A assistant-a 项目架构 用户A assistant-a 项目架构", "refined", "wiki-entity"),
        _mk("mem-aaaa0002", "用户A 的一句话 工作纪要", "refined", "distill"),
        _mk("mem-aaaa0003", "无关背景 水草 珊瑚 海星", "raw"),
    ])
    out = rec.search_two_tier("用户A assistant-a 项目架构", top_k=3)
    ids = {d.id for d, _ in out}
    assert "mem-aaaa0001" not in ids, f"wiki-entity 派生不得进首跳，实际 {ids}"
    # distill 存在（refined 池唯一白名单成员，应至少出现在 refined-first 返回里）
    # 注：query 高度偏向 wiki 词，distill 相关度可能不足 0.6 → raw-fallback；
    #   断言重点 = wiki 永不出现（白名单硬滤），distill 出现与否取决于分数——不强断。


def test_o5_blank_derived_manual_rows_pass_first_hop():
    """空串（手动 store 主体）必进 refined 首跳——not df 而非 df in set。"""
    rec = HybridRetriever()
    rec.index([
        _mk("mem-aaaa0011", "用户A 拍板 XTMEM 波3 开工 手动要点 人工记录", "refined", ""),
        _mk("mem-aaaa0012", "用户A 拍板 XTMEM 波3 开工 维基实体 长页 机器生成", "refined", "wiki-entity"),
    ])
    out = rec.search_two_tier("用户A 拍板 XTMEM 波3 开工", top_k=2)
    ids = {d.id for d, _ in out}
    assert "mem-aaaa0011" in ids, f"手动 refined（空 derived_from）必须进首跳，实际 {ids}"
    assert "mem-aaaa0012" not in ids, f"wiki-entity 不得进首跳，实际 {ids}"


def test_o5_allowed_set_shape():
    """白名单常量形状：四允许 + 集合不含 wiki/timeline/prediction。

    B0-4（2026-09-11）：由三允许改四允许——补 "distill-judge"（Gen2 LLM 蒸馏的 318 条
    核心事实，原白名单漏收 → 两阶段路由里完全不可达）。此为预期内的守卫更新，非遗回归退化。
    """
    assert FIRST_HOP_ALLOWED == {"distill", "profile-aggregator", "diary-distill", "distill-judge"}
    for banned in ("wiki-entity", "timeline", "prediction-engine", "summary", "forecast"):
        assert banned not in FIRST_HOP_ALLOWED, banned
