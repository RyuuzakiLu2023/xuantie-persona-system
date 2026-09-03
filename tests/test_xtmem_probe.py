# -*- coding: utf-8 -*-
"""P5.0-d 引力探针（轻量 EPA）测试（2026-09-02，小D）。

覆盖：聚焦 vs 广谱分离（探针值稳定分离）→ 公式边界（不越 clamp 界）→ search() 接线
      （probe_enabled=False 不调探针 = 现状逐字节一致；True 调一次不崩）。
"""

import math
import time

import pytest

from memory import probe as P
from memory.hybrid_retriever import HybridRetriever, MemoryDoc


def _mk(doc_id, content):
    return MemoryDoc(id=doc_id, content=content, timestamp=time.time(),
                     importance=0.6, namespace="dsh", visibility="shared", tier="raw")


def test_focused_vs_broad_separation():
    """聚焦（近聚类向量）L 高；广谱（互相分散向量）L 低——两者稳定分离。"""
    d = 16
    base = [0.1] * d
    cluster = [[base[j] + 0.001 * (i * 7 % 5) for j in range(d)] for i in range(6)]
    spread = []
    for i in range(6):
        v = [0.0] * d
        v[i % d] = 1.0
        spread.append(v)
    pf = P.probe_entropy(cluster)
    ps = P.probe_entropy(spread)
    assert pf["L"] > 0.8, f"聚焦 query 首主成分应解释绝大部分方差，实际 {pf}"
    assert ps["L"] < 0.5, f"广谱 query 首主成分不应主导，实际 {ps}"
    assert pf["L"] > ps["L"] + 0.3, "聚焦与广谱必须稳定分离"


def test_formula_boundaries():
    """公式 clamp(0.30+0.15L-0.10S, 0.25, 0.50) 边界不越界。"""
    assert P.dynamic_threshold(1.0, 0.0) == pytest.approx(0.45)      # 高聚焦：0.30+0.15
    assert P.dynamic_threshold(0.0, 1.0) == pytest.approx(0.25)      # 广谱下界 clamp
    assert P.dynamic_threshold(0.0, 0.0) == pytest.approx(0.30)
    assert P.dynamic_threshold(0.5, 0.5) == pytest.approx(0.325)
    for L, S in [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (0.5, 0.5), (0.99, 0.2)]:
        v = P.dynamic_threshold(L, S)
        assert 0.25 <= v <= 0.50, f"越 clamp 界: L={L} S={S} → {v}"


def test_probe_degrade_safe():
    """n<2 / 全零 → L=0, S=1（诚实降级不抛错）。"""
    assert P.probe_entropy([])["L"] == 0.0
    assert P.probe_entropy([[0.0, 0.0]])["L"] == 0.0
    assert P.probe_entropy([[0.0, 0.0], [0.0, 0.0]])["L"] == 0.0


def test_search_wiring_probe_enabled_false_and_true(monkeypatch):
    """接线：False 不调用探针（现状逐字节一致）；True 调用一次且不崩。"""
    docs = [_mk(f"p-{i}", f"zzz-probe-内容{i}") for i in range(4)]
    rec = HybridRetriever()
    rec.index(docs)
    calls = {"n": 0}

    def fake_probe(vectors):
        calls["n"] += 1
        return {"L": 0.9, "S": 0.1, "n": len(vectors)}

    monkeypatch.setattr(P, "probe_entropy", fake_probe)

    now = time.time()
    rec.probe_enabled = False
    base = {d.id: s for d, s in rec.search("zzz-probe", top_k=10, now=now)}
    assert calls["n"] == 0, "probe_enabled=False 不得调探针（默认行为逐字节一致）"

    rec.probe_enabled = True
    got = {d.id: s for d, s in rec.search("zzz-probe", top_k=10, now=now)}
    assert calls["n"] == 1, "probe_enabled=True 每查询调一次探针"
    assert got, "开启探针后检索不崩且返回结果"
