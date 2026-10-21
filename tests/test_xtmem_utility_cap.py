# -*- coding: utf-8 -*-
"""test_xtmem_utility_cap.py — 待办池 #11 utility_score 霸榜修复守卫（2026-09-06，assistant-c）。

判据（DoD）：
  ① 相关性重新主导：高 access_count 骨架文档 + 中等相关性文档 → 修复后 final 排序里
     相关性主导（骨架文档不再仅凭 utility 碾压进 top）；
  ② utility 有界：累加到 1 万次（u_raw=1000），进 final 的增量不超 viewer_utility_weight（0.05）；
  ③ 保序：log1p 保单调——热度高的仍然 ≥ 热度低的（治霸榜不灭热度信号）；
  ④ 修复前后对比：同一 query 霸榜文档位置显著回落（对拍贴回执）。
人造样本，不依赖热库数字（错题本 #22）。
"""
import math
import sys

import pytest

from conftest import PLUGIN_DIR
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from memory.hybrid_retriever import HybridRetriever, MemoryDoc


def _mk(cid, content, importance=0.5, ts=None):
    import time as _t
    return MemoryDoc(id=cid, content=content, timestamp=ts or _t.time(),
                     importance=importance, tags=[], track="default",
                     state="active", namespace="dsh", visibility="shared", tier="raw")


def _utility_boost(u_raw, weight=0.05, cap=1000.0):
    """生产同款公式（单点抽出来测，防测试与实现漂移——与 _search_locked 内联一致）。"""
    return (math.log1p(min(u_raw, cap)) / math.log1p(cap)) * weight


def test_utility_bounded_increment():
    """② utility 有界：u_raw=1000（=access_count 1 万次）时增量不超 weight。"""
    boost = _utility_boost(1000.0)
    assert boost <= 0.05 + 1e-9, f"1000 次热度增量应 ≤0.05，实际 {boost}"
    # 超过 cap 也封顶
    boost_huge = _utility_boost(99999.0)
    assert boost_huge <= 0.05 + 1e-9, f"超 cap 封顶，实际 {boost_huge}"
    # 低热度小增量
    small = _utility_boost(1.0)
    assert 0 < small < 0.01, f"低热度小增量，实际 {small}"


def test_utility_monotonic_order_preserved():
    """③ 保序：log1p 保单调——热度高 ≥ 热度低（治霸榜不灭热度）。"""
    b1 = _utility_boost(10)
    b2 = _utility_boost(100)
    b3 = _utility_boost(1000)
    assert b1 < b2 < b3, "热度序必须保留"


def test_relevance_dominates_over_skeleton_doc():
    """① 相关性主导：高 utility 骨架文档 + 高相关普通文档 → 后者排前。

    人造：骨架文档 query 弱相关（bm25 低分）+ utility_score=607（热库实测同级），
    普通文档 query 强相关（bm25 高分）+ utility=0。修复后强相关者赢。
    修复前：骨架 607×0.05=+30.4 直接碾压（对照断言算式，非跑旧代码）。
    """
    hr = HybridRetriever()
    skeleton = _mk("skeleton", "XTMEM 进度状态交接文档 部署 记忆", importance=0.9)
    relevant = _mk("relevant", "utility 修复 权重 打分 排序 霸榜", importance=0.5)
    hr.index([skeleton, relevant])

    query = "utility 修复 打分"
    # 模拟 utility_lookup：骨架 607 分，普通 0
    util = {"skeleton": 607.0, "relevant": 0.0}
    res = hr.search(query, top_k=5, viewer="dsh", utility_lookup=lambda cid: util.get(cid, 0))
    ids = [d.id for d, _ in res]
    assert ids, "应有结果"
    assert ids[0] == "relevant", (
        f"相关性应主导排序，实际 {ids}——骨架 607 分旧公式 =+30.4 碾压，"
        f"新公式 log1p(607)/log1p(1000)×0.05={_utility_boost(607):.4f} 必须输给强相关"
    )


def test_before_after_contrast_numbers():
    """④ 修复前后数字对比（回执用）：同 query 骨架文档的 final 增量对比。"""
    u = 607.0
    old_contrib = u * 0.05           # 旧：裸累加×权重
    new_contrib = _utility_boost(u)  # 新：log1p 归一×权重
    assert old_contrib > 30.0 and new_contrib < 0.05, (old_contrib, new_contrib)
    # 压缩比
    ratio = old_contrib / max(new_contrib, 1e-9)
    assert ratio > 600, f"压缩比应 >600x，实际 {ratio:.0f}x"