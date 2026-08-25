"""Auto Dream — 自动记忆蒸馏。

每 7 天（可配）自动触发一次，扫描近期对话轨迹，
提取有价值的信息写入 memory 条目，清理过时条目。
"""
import time
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认间隔：7 天（秒）
DEFAULT_DREAM_INTERVAL_S = 7 * 24 * 3600
DEFAULT_DISTILL_INTERVAL_S = 30 * 24 * 3600


class AutoDream:
    def __init__(self, memory_root: str, config: dict = None):
        self._root = Path(memory_root)
        self._config = config or {}
        self._state_file = self._root / ".dream_state.json"

    def should_run(self, kind: str = "dream") -> bool:
        """检查是否到了执行时间。"""
        interval = (
            DEFAULT_DREAM_INTERVAL_S if kind == "dream"
            else DEFAULT_DISTILL_INTERVAL_S
        )
        state = self._load_state()
        last_run = state.get(f"last_{kind}_run", 0)
        return (time.time() - last_run) >= interval

    def mark_completed(self, kind: str = "dream"):
        state = self._load_state()
        state[f"last_{kind}_run"] = time.time()
        self._state_file.write_text(json.dumps(state, indent=2))

    def get_dream_task(self) -> str:
        """返回 Dream 阶段的 prompt。"""
        return (
            "Run one automatic dream memory consolidation pass.\n"
            "1. Scan recent conversation trajectories from the session DB.\n"
            "2. Extract durable, verified facts into memory entries.\n"
            "3. Mark outdated entries as refuted.\n"
            "4. Do NOT modify the database directly.\n"
            "5. Write a summary of what was consolidated."
        )

    def get_distill_task(self) -> str:
        """返回 Distill 阶段的 prompt。"""
        return (
            "Run one automatic distill pass.\n"
            "1. Review past month of sessions.\n"
            "2. Identify repeated manual workflows worth packaging as skills.\n"
            "3. Inventory existing skills first to avoid duplication.\n"
            "4. Produce a shortlist of high-confidence candidates.\n"
            "5. Only create assets you are confident about."
        )

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text())
            except Exception:
                return {}
        return {}
