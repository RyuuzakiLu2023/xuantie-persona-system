# -*- coding: utf-8 -*-
"""memory/tuning.py — XTMEM 自演化旋钮外置（P3.1-a）。

读 mimox-data/xtmem_tuning.json（默认路径，可 XTMEM_TUNING 覆盖）；缺失/损坏 → 用当前硬编码默认值兜底（删 json 行为与改前一致）。
"""
import json
import os

_DEFAULT_PATH = os.environ.get(
    "XTMEM_TUNING",
    __import__("memory.public_runtime", fromlist=["home_file"]).home_file("xtmem_tuning.json"),
)

_DEF = {
    "health": {
        "vector_min": 0.90, "garbled_max": 0.005, "untagged_max": 0.20,
        "heartbeat_max_h": 48, "size_budget_mb": 100, "size_delta_ratio": 0.20,
    },
    "retriever": {
        "recency_weight": 0.1, "importance_weight": 0.05, "recency_half_life_hours": 720.0,
        "stream_ttl_seconds": 2592000,
        "refined_top": 5, "refined_hit_threshold": 0.6,
        # P5.0-c（2026-09-02）：图传播重排权重——默认 0（行为与现状逐字节一致；影子期攒样本后才开默认值）
        "graph_prop_weight": 0.0,
        # P5.0-d（2026-09-02）：引力探针开关——默认 False（动态阈值不激活，行为与现状逐字节一致）
        "probe_enabled": False,
        # G2（2026-09-04）：FTS5 热接线模式——off=内存 BM25 现状 / shadow=双跑对照 / on=SQL FTS5 词级腿
        "fts5_mode": "off",
        "half_life_by_track": {"profile": 0, "preference": 8760, "identity": 8760, "emotion": 168},
    },
    "evolution": {
        "jaccard_replace": 0.2, "jaccard_enrich": 0.5, "jaccard_confirm": 0.3,
        "confidence_min": 0.6,
        "search_limit": 100, "jaccard_candidate": 0.1,
    },
    "polarity": {
        "words": ["不再", "改为", "取消", "不是", "换了", "替换成", "更新为", "修正为"],
    },
}

_cache = None


def _deep_merge(base, overlay):
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_tuning() -> dict:
    """返回合并后的配置：默认值 + json 覆盖。json 缺失/损坏 → 纯默认（行为与改前一致）。"""
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    try:
        with open(_DEFAULT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _cache = _deep_merge(_DEF, data)
    return _cache


def get(section: str, key: str, default=None):
    return load_tuning().get(section, {}).get(key, default)
