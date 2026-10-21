# -*- coding: utf-8 -*-
"""xtmem_jobs — XTMEM 派生作业脚本包（蒸馏 / 时间骨架 / 画像 / 实体页 / 血缘）。

【这是什么】
`src/scripts/` 下的可执行作业脚本，随 wheel 一起安装成 `xtmem_jobs` 包；
既可用 `python -m xtmem_jobs.xtmem_distill_units` 跑，也可直接
`python src/scripts/xtmem_distill_units.py` 跑（同目录 import 已兼容）。

【为什么要有】
公开仓把这些脚本当作「记忆的定期整理工序」分发：粗蒸馏（xtmem_distill）、
评判式蒸馏（units + llm + commit）、时间骨架（timeline）、画像（profile）、
实体页（wiki）、血缘消费（lineage_resolve）。
它们共用同一套接线（库路径 / 引擎 / CLI），统一收在 xtmem_common。

【约定】
- 所有作业只读写 **$XTMEM_DB_PATH** 指向的库；不读宿主配置、不读 .env、不写死生产路径。
- LLM 相关作业（distill_llm）必须显式配置 XTMEM_LLM_BASE / XTMEM_LLM_MODEL / XTMEM_LLM_KEY。
- 作业之间没有隐式顺序依赖：每个脚本都可以单独跑、可重复跑（幂等口径见各自 docstring）。

【更新记录】
*创建：2026-10-22 · 公开蒸馏子树施工（脚本包化，兼容 -m 与直跑）*
"""
from __future__ import annotations

import os
import sys

# 直跑单个脚本时 `import xtmem_common` 需要脚本目录在 sys.path；
# 以包方式导入（python -m xtmem_jobs.xxx / wheel 安装）时这句是空操作。
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
