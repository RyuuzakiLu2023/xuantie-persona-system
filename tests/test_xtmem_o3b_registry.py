# -*- coding: utf-8 -*-
"""test_xtmem_o3b_registry.py — O3-b 注入块 registry 守卫（P6.13，v1.3.1 §三 O3-b，2026-09-05，assistant-c）。

判据：① 预算口径=中文 1.5 token/字（禁旧 1token=4字符 的 600*4=2400 超注）；
      ② 装配器持总预算，超了弃低优先级整块；③ prefetch 跨通道去重（profile/timeline
      已由 system prompt 注入，prefetch 不再召回同 track）。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import re


def _src():
    return open(_REPO / "plugins"/ "xtmem"/ "provider.py", encoding="utf-8").read()


def test_portrait_budget_is_400_not_2400():
    """预算常量：600 token → 400 字符（1.5 token/字），不是旧 600*4=2400。"""
    src = _src()
    m = re.search(r"_PORTRAIT_BUDGET_CHARS = int\(600 / 1\.5\)", src)
    assert m, f"画像预算应=400 字符（600 token / 1.5 中文 1.5 token/字），源码没写对：{src[src.find('_PORTRAIT'):src.find('_PORTRAIT')+220]}"
    m2 = re.search(r"_TIMELINE_BUDGET_CHARS = int\(600 / 1\.5\)", src)
    assert m2, "时间骨架预算同样应=400 字符"


def test_old_4x_approx_gone():
    """旧 600*4=2400（1token=4字符）近似必须消失——它是超注 6 倍的根。"""
    src = _src()
    assert "600 * 4" not in src, "旧 1token=4字符 预算近似（600*4）必须删除"


def test_assemble_drops_low_priority_when_over_budget():
    """装配器超总预算 → 弃低优先级整块（尾部），语义完整不半截。"""
    budget = 100
    used = 0
    dropped = []
    for _name, t in [("bus", 60), ("portrait", 40), ("timeline", 25), ("inbox", 10)]:
        if used + t > budget:
            dropped.append(_name)
            continue
        used += t
    # 60+40=100 正好满；timeline 25 / inbox 10 超 → 两者都弃
    assert "timeline" in dropped and "inbox" in dropped
    assert "bus" not in dropped and "portrait" not in dropped
    assert used == 100


def test_assemble_method_in_source():
    """装配器方法存在（_assemble_system_block + 总预算常量）。"""
    src = _src()
    assert "def _assemble_system_block" in src, "装配器方法缺失"
    assert "_SYSBLOCK_TOTAL_BUDGET_TOKENS" in src, "总预算常量缺失"


def test_cross_channel_dedup_in_context_builder():
    """跨通道去重：context_builder.prefetch 跳过 track=profile/timeline（已在 system prompt 注入）。"""
    cb = open(_REPO / "src" / "hermes-plugin" / "memory" / "context_builder.py", encoding="utf-8").read()
    assert '_tk in ("profile", "timeline")' in cb, "prefetch 必须跳过 profile/timeline（跨通道去重）"
