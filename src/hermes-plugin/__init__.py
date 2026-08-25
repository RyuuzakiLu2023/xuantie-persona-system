# -*- coding: utf-8 -*-
"""XTMEM — 玄铁通用记忆系统 hermes MemoryProvider 插件。

- 引擎：0.19 自建 memory/ 包整体平移至 engine/（零代码改动）。
- 接口：0.20.5 MemoryProvider ABC（plugins/memory 目录约定：
  __init__.py 含 MemoryProvider 字样 + register() 或 MemoryProvider 子类）。
- 人格：本插件零人格逻辑（D4）；namespace 按 agent_identity 自动分域。
"""

from __future__ import annotations

from .provider import XtmemMemoryProvider


def register(ctx) -> None:
    """Register XTMEM as a hermes memory provider plugin."""
    ctx.register_memory_provider(XtmemMemoryProvider())


__all__ = ["XtmemMemoryProvider", "register"]
