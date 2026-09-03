# -*- coding: utf-8 -*-
"""memory/probe.py — P5.0-d 查询引力探针 · 轻量 EPA（2026-09-02，小D）。

VCP P4.9-3 的简版：用候选向量分布的主成分解释率做“聚焦 vs 广谱”判别，
驱动检索侧动态阈值（relevance_threshold）。完整数学（透视投影熵+Gram-Schmidt
分解）是 VCP 专属深度实现，XTMEM 低频嵌入式用简版投影熵即可（vcp移植方案 §2 判定）。

实现（按“禁止为一个探针装 sklearn”/“用 scipy.linalg.svd 或 numpy 手写”约束，纯 numpy）：
  - 输入：候选向量矩阵 X（n × d），n>=2 才有效；
  - 中心化后 numpy SVD：Xc = U·S·Vᵀ，总方差 = Σσᵢ²；
  - 逻辑深度 L = σ₁² / Σσᵢ²（第一主成分解释率，0~1，越聚焦越大）；
  - 语义宽度 Sₓ = 1 − L（非主成分残差占比）；
  - 动态阈值 = clamp(0.30 + 0.15·L − 0.10·Sₓ, 0.25, 0.50)（vcp移植方案 P4.9-3 公式）。

无状态幂等：输入相同输出相同（纯函数，不碰 db/缓存）。
降级：numpy 缺失 / n<2 / 全零向量 → L=0、Sₓ=1（阈值打到底边，保守等同现状下限取 clamp 下界 0.25——
  等等，测试边界按公式值；降级返回值以 docstring 为准：L=0.0, S=1.0）。
"""

from __future__ import annotations

import logging
from typing import List, Sequence

logger = logging.getLogger(__name__)

# 动态阈值公式常数（vcp移植方案 P4.9-3：clamp(0.30+0.15L-0.10S, 0.25, 0.50)）
_TH_BASE = 0.30
_TH_L_COEF = 0.15
_TH_S_COEF = 0.10
_TH_MIN = 0.25
_TH_MAX = 0.50


def probe_entropy(vectors: Sequence[Sequence[float]]) -> dict:
    """候选向量分布 → {L, S, ratio, n}（轻量 EPA，纯 numpy，幂等）。

    L=第一主成分解释率；S=1-L。n<2 / 降维退化 → L=0.0, S=1.0（诚实降级，不造数据）。
    """
    if not vectors or len(vectors) < 2:
        return {"L": 0.0, "S": 1.0, "n": len(vectors or [])}
    try:
        import numpy as np
    except Exception:
        logger.warning("numpy missing; probe degraded (non-fatal)")
        return {"L": 0.0, "S": 1.0, "n": len(vectors)}
    try:
        X = np.asarray(vectors, dtype=np.float64)
        if X.ndim != 2 or X.shape[0] < 2 or X.shape[1] < 2:
            return {"L": 0.0, "S": 1.0, "n": X.shape[0] if X.ndim == 2 else len(vectors)}
        Xc = X - X.mean(axis=0, keepdims=True)
        # numpy SVD（n×d，n 小 d 大用瘦身分解；等价 scipy.linalg.svd，不引 sklearn）
        _, s, _ = np.linalg.svd(Xc, full_matrices=False)
        total = float(np.sum(s * s))
        if total <= 0:
            return {"L": 0.0, "S": 1.0, "n": X.shape[0]}
        ratio = float(s[0] * s[0]) / total
        return {"L": ratio, "S": 1.0 - ratio, "n": X.shape[0]}
    except Exception:
        logger.debug("probe_entropy failed (non-fatal)", exc_info=True)
        return {"L": 0.0, "S": 1.0, "n": len(vectors)}


def dynamic_threshold(L: float, S: float = None) -> float:
    """公式 clamp(0.30+0.15L-0.10S, 0.25, 0.50)；S 缺省按 1-L。"""
    s = (1.0 - L) if S is None else S
    v = _TH_BASE + _TH_L_COEF * L - _TH_S_COEF * s
    return max(_TH_MIN, min(_TH_MAX, v))
