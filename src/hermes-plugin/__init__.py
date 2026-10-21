# -*- coding: utf-8 -*-
# ================================================================
# __init__.py · 总导游图（Hermes 记忆提供者插件的入口）
#
# 这是什么：XTMEM 作为 Hermes MemoryProvider 插件的注册入口。
#   宿主加载本目录后调用 register(ctx)，把 XtmemMemoryProvider 挂进记忆链。
#
# 技术栈：纯 Python；依赖宿主提供的 agent.memory_provider（MemoryProvider / RecallStatus）
#   与引擎包 memory（随 wheel 或本插件同级目录提供）。
#
# 修改铁律：
# 1. register() 是宿主唯一约定入口，签名 (ctx) 不能改。
# 2. 本文件必须含 "MemoryProvider" 字样 + register()（宿主按此发现插件）。
# 3. 本插件零人格逻辑：不注入任何角色设定，身份由宿主的 agent_identity/profile 决定。
# 4. 导入方式必须同时兼容两种加载形态——
#    ① 包形态：宿主按 namespace package 加载 → 相对导入可用；
#    ② 目录/文件形态：没有包上下文，相对导入会 ImportError → 退同目录绝对导入。
#    两条都必须拿到同一个类，否则「目录能跑、装包就炸」这类故障只在部署时暴露。
#
# 更新记录：
# *更新：2026-10-22 · 公开接头首批：补双形态导入兼容；去掉私有产品名。*
# ================================================================
"""XTMEM — Hermes MemoryProvider plugin.

- Engine: the `memory` package shipped with the wheel (or next to this plugin).
- Interface: Hermes 0.20.5 MemoryProvider ABC; discovery relies on this module
  containing the word "MemoryProvider" and a register(ctx) function.
- Persona: none. Namespace is derived from the host's agent_identity/profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # ① 包形态（宿主按 namespace package 加载）
    from .provider import XtmemMemoryProvider
except ImportError:  # ② 目录/文件形态：没有包上下文，退同目录绝对导入
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from provider import XtmemMemoryProvider  # type: ignore


def register(ctx) -> None:
    """Register XTMEM as a Hermes memory provider plugin."""
    ctx.register_memory_provider(XtmemMemoryProvider())


__all__ = ["XtmemMemoryProvider", "register"]
