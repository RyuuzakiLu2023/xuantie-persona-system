# -*- coding: utf-8 -*-
"""test_xtmem_b_s1_incremental.py — B方案 S1 retriever 增量维护守卫与对拍测试（2026-09-06，assistant-c）。

判据（DoD）：
  ① append/remove/patch 三接口正常运作，四处索引同步（docs/_id_to_idx/_tier_index/_embeddings）+ bm25；
  ② remove 使用 None 哨兵，打分循环跳过，不发生列表压缩位移；
  ③ 并发安全：RLock 支持 patch/search 重入；
  ④ ★核心 CI 门槛：10 个随机 query 对拍测试——增量维护生成的索引 vs 一次性全量构建索引，
     Top-10 检索结果与次序必须 100% 完全一致（防悄悄漏条）。
"""
import random
import sys
from pathlib import Path
import pytest

from conftest import PLUGIN_DIR
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from memory.hybrid_retriever import HybridRetriever, MemoryDoc


import time as _tm

def _mk_doc(cid: str, content: str, tier: str = "raw", track: str = "default", ts: float = None) -> MemoryDoc:
    return MemoryDoc(
        id=cid,
        content=content,
        timestamp=ts if ts is not None else _tm.time(),
        importance=0.8,
        tags=["xtmem"],
        track=track,
        state="active",
        namespace="dsh",
        visibility="shared",
        tier=tier,
    )


def test_s1_append_syncs_all_structures():
    """append 单条同步维护 docs / _id_to_idx / _tier_index / _embeddings / bm25。"""
    hr = HybridRetriever()
    d1 = _mk_doc("mem-1", "第一条测试记忆 assistant-a系统", tier="refined")
    hr.append(d1)

    assert len(hr.docs) == 1 and hr.docs[0] == d1
    assert hr._id_to_idx["mem-1"] == 0
    assert hr._tier_index["refined"] == [0]
    assert len(hr._embeddings) == 1
    assert hr.bm25.N == 1

    d2 = _mk_doc("mem-2", "第二条测试记忆 用户A指令", tier="raw")
    hr.append(d2)
    assert len(hr.docs) == 2
    assert hr._id_to_idx["mem-2"] == 1
    assert hr._tier_index["raw"] == [1]
    assert len(hr._embeddings) == 2
    assert hr.bm25.N == 2


def test_s1_remove_sentinel_and_skip():
    """remove 置 None 哨兵，三索引同步摘除，search 不崩溃且跳过哨兵。"""
    hr = HybridRetriever()
    d1 = _mk_doc("mem-a", "苹果香蕉橙子", tier="raw")
    d2 = _mk_doc("mem-b", "苹果西瓜草莓", tier="raw")
    d3 = _mk_doc("mem-c", "香蕉葡萄哈密瓜", tier="raw")
    hr.append(d1)
    hr.append(d2)
    hr.append(d3)

    assert hr.remove("mem-b") is True
    assert hr.remove("non-existent") is False

    # 结构检验
    assert hr.docs[1] is None
    assert "mem-b" not in hr._id_to_idx
    assert 1 not in hr._tier_index["raw"]
    assert len(hr.docs) == 3  # 长度不变，索引不位移

    # 检索检验：搜索苹果，只能召回 mem-a，mem-b 绝不出现
    res = hr.search("苹果", top_k=10)
    returned_ids = [d.id for d, _ in res]
    assert "mem-a" in returned_ids
    assert "mem-b" not in returned_ids


def test_s1_patch_updates_inplace():
    """patch 等价 remove + append，更新后内容与索引立即生效。"""
    hr = HybridRetriever()
    d1 = _mk_doc("mem-p", "旧版本内容：数据库连接超时", tier="raw")
    hr.append(d1)

    d1_new = _mk_doc("mem-p", "新版本内容：数据库连接正常恢复", tier="refined")
    hr.patch(d1_new)

    assert "mem-p" in hr._id_to_idx
    new_idx = hr._id_to_idx["mem-p"]
    assert hr.docs[new_idx].content == "新版本内容：数据库连接正常恢复"
    assert new_idx in hr._tier_index["refined"]

    # 搜索新关键词能搜到，旧关键词得分衰减
    res = hr.search("正常恢复", top_k=5)
    assert any(d.id == "mem-p" for d, _ in res)


def test_s1_dual_run_random_10_queries_exact_match():
    """★核心验收：随机 10 query 对拍测试——增量维护 vs 全量重建 Top-10 100% 一致。"""
    vocab = ["assistant-a", "用户A", "assistant-c", "maintainer", "assistant-b", "记忆", "书架", "缓存", "网关", "检索",
             "架构", "数据库", "分轨", "调度", "飞书", "向量", "熔断", "血缘", "演化", "测试"]

    # 生成 60 条样本
    all_docs = []
    for i in range(60):
        words = random.sample(vocab, k=random.randint(3, 6))
        text = f"第 {i} 条语料：{' '.join(words)}，相关讨论与决策内容记录。"
        tier = "refined" if i % 3 == 0 else "raw"
        all_docs.append(_mk_doc(f"chunk-{i:03d}", text, tier=tier, ts=1700000000.0 + i * 100))

    # 路径 A：一次性全量 index()
    retriever_full = HybridRetriever()
    retriever_full.index(all_docs)

    # 路径 B：前 20 条先全量 index()，后 40 条逐条 append() 增量维护
    retriever_inc = HybridRetriever()
    retriever_inc.index(all_docs[:20])
    for d in all_docs[20:]:
        retriever_inc.append(d)

    # 10 个随机 query 对拍
    test_queries = [
        "assistant-a 记忆 检索",
        "用户A 架构 决策",
        "assistant-c 增量 书架",
        "maintainer 数据库 缓存",
        "飞书 网关 调度",
        "向量 熔断 血缘",
        "演化 测试 语料",
        "assistant-b 讨论 记录",
        "缓存 命中 延迟",
        "分轨 调度 决策",
    ]

    for q in test_queries:
        res_full = retriever_full.search(q, top_k=10)
        res_inc = retriever_inc.search(q, top_k=10)

        ids_full = [d.id for d, _ in res_full]
        ids_inc = [d.id for d, _ in res_inc]

        assert ids_full == ids_inc, (
            f"Query '{q}' 对拍不一致！\nFull: {ids_full}\nInc:  {ids_inc}"
        )
