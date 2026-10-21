"""XTMEM recall policy — decide when a turn is worth searching memory.

Not every turn: retrieval is driven by the user message plus context state.
Pure regex rules, zero LLM cost.

Rules (evaluation order):
- Rule 0b: correction signals ("you're wrong" / "misremembered") force retrieval.
- Rule 0:  short follow-up within 15s inherits the previous decision (max 3 times).
- Rule 1:  context was just compressed -> retrieve.
- Rule 2:  always-recall keyword hit -> retrieve.
- Rule 3/4: short acknowledgement / pure chit-chat -> skip.
- Rule 5:  task-intent verb -> retrieve.
- Rule 6:  message long enough to be substantive -> retrieve.

Opt-in switch `substantive_short_queries`: when true, short non-chit-chat questions
are retrieved too (per profile; the shared default stays legacy).
"""

import logging
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_TASK_INTENT_PATTERNS = re.compile(
    r"^(帮我|给我|请|写|修|改|创建|生成|部署|测试|安装|配置|"
    r"优化|重构|调试|排查|审计|检查|导出|导入|转换|分析|编译|"
    r"打包|发布|回滚|帮我|fix|build|create|deploy|test|install|config|"
    r"optimize|refactor|debug|audit|export|import|convert|analyze)"
)

# Phase 1: 纠正信号正则
# 注意：中文短语必须在句首或标点/空格后才匹配，防止"咋就有不对了"误触发
_CN_BNDRY = r'(?:(?:^)|(?<=[。！？，,、\s]))'  # 中文句首/标点/空格边界
_CORRECTION_CN = (
    r'^不对|(?:' + _CN_BNDRY + r'不对(?:[，,。！？\s]|$))|'  # 句首"不对"无条件匹配；句中需标点/结尾
    + _CN_BNDRY + r'不是这|'              # 不是这个
    + _CN_BNDRY + r'你记错|'              # 你记错了
    + r'错了[，,。！？\s]|'               # 错了，... / 错了。（可出现在句中）
    + _CN_BNDRY + r'记错了|'              # 记错了
    + _CN_BNDRY + r'你说错|'              # 你说错了
    + _CN_BNDRY + r'不对啊|'              # 不对啊
    + _CN_BNDRY + r'搞错了|'              # 搞错了
)
CORRECTION_PATTERNS = re.compile(
    _CORRECTION_CN +
    r'no, |wrong|actually|not really',   # English patterns (unchanged)
    re.IGNORECASE,
)


def _has_task_intent(text: str) -> bool:
    # Y-1（2026-08-31）：单字「改」「请」不再触发任务意图
    return len(text) >= 2 and bool(_TASK_INTENT_PATTERNS.match(text))


DEFAULT_ALWAYS_RECALL: List[str] = [
    "之前", "上次", "记得", "recall", "项目",
    "之前那", "上次的", "你之前", "还记得", "之前做", "上次做",
    "之前说", "上次说", "之前改", "上次改", "之前写", "上次写",
    "那个项目", "那个任务", "那个bug", "那个问题", "那个功能",
    "上次的代码", "之前的配置",
    "document", "project", "task", "previous",
]

# 注意："继续"只在 never 列表里（裸"继续"= 催 agent 继续干活，跳过检索）；
# 含更多上下文的"继续上次的项目"会被上面的"上次/项目"命中而触发检索。
# 别把"继续"加回 always —— 那样裸"继续"会被子串匹配永远强制检索（见踩坑：规则2先于规则3/4）。
DEFAULT_NEVER_RECALL: List[str] = [
    "好", "收到", "1", "ok", "嗯", "哦", "行", "对了",
    "对的", "是的", "没错", "no", "yes", "谢谢", "thanks",
    "继续",
]

DEFAULT_MIN_LENGTH = 10
_SHORT_FOLLOWUP_MAX_LEN = 12  # 短追问最大字符数
_SHORT_FOLLOWUP_WINDOW = 15.0  # 继承窗口秒数


class RecallPolicy:
    """Memory 检索时机控制器。"""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.always_kw = [
            kw.lower() for kw in cfg.get("always_recall_keywords", DEFAULT_ALWAYS_RECALL)
        ]
        self.never_kw = [
            kw.lower() for kw in cfg.get("never_recall_keywords", DEFAULT_NEVER_RECALL)
        ]
        self.min_length = cfg.get("min_message_length", DEFAULT_MIN_LENGTH)
        # Opt in per profile; the shared plugin keeps legacy defaults elsewhere.
        self.substantive_short_queries = cfg.get("substantive_short_queries", False) is True
        # Phase 1: 短追问继承状态
        self._last_recall_ts: float = 0.0
        self._last_recall_result: bool = False
        self._inherit_count: int = 0  # R2.5（Y-2）：短追问继承计次，>=3 后强制重判

    def should_recall(
        self,
        user_message: str,
        context_is_compressed: bool = False,
    ) -> bool:
        """判断是否应该触发 memory 检索。

        Args:
            user_message: 当前用户消息文本。
            context_is_compressed: 上下文是否刚经历过压缩（压缩后需要恢复记忆）。

        Returns:
            True = 触发检索，False = 跳过。
        """
        if not self.enabled:
            return True  # 策略关闭时默认检索（向后兼容）

        msg = (user_message or "").strip()
        if not msg:
            return False

        lower = msg.lower()
        now = time.time()

        if self.substantive_short_queries:
            from memory.bundle_selector import is_chitchat
            normalized = re.sub(r"[\s。！？!?，,、；;：:~～…\.]+$", "", lower)
            skip = (
                normalized in self.never_kw
                or is_chitchat(msg)
                or not any(ch.isalnum() for ch in msg)
                or msg.startswith("/")
            )
            # Pure acknowledgements stay quiet even immediately after recall.
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = not skip
            return not skip

        # ── Rule 0b: 纠正信号感知（优先级最高）──
        # 用户说"不对/记错了" → 强制检索，绕过所有规则
        if CORRECTION_PATTERNS.search(msg):
            logger.info("recall_policy: triggered — correction signal detected in '%s'", msg[:30])
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = True
            return True

        # ── Rule 0: 短追问继承 ──
        # 上一轮触发了检索 + 本轮短追问 + 15s 内 → 继承上一轮结果
        if (
            self._last_recall_result
            and len(msg) < _SHORT_FOLLOWUP_MAX_LEN
            and (now - self._last_recall_ts) < _SHORT_FOLLOWUP_WINDOW
            and self._inherit_count < 3
        ):
            logger.info(
                "recall_policy: inherited — short follow-up (%d chars) within %.0fs window",
                len(msg),
                _SHORT_FOLLOWUP_WINDOW,
            )
            # R2.5（Y-2）：不再刷新 _last_recall_ts（此前注释自称不更新但时间戳照刷）；
            # 连续 >=3 次继承后强制走完整判定（下方规则逻辑）。
            self._inherit_count += 1
            return self._last_recall_result

        # 规则1: 上下文刚压缩过 → 必须检索，恢复丢失的记忆
        if context_is_compressed:
            logger.info("recall_policy: triggered — context was compressed")
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = True
            return True

        # 规则2: 命中 always_recall 关键词 → 触发
        for kw in self.always_kw:
            if kw in lower:
                logger.info("recall_policy: triggered — keyword '%s'", kw)
                self._inherit_count = 0
                self._last_recall_ts = now
                self._last_recall_result = True
                return True

        # 规则3: 消息过短 + never 关键词 → 跳过
        if len(msg) < self.min_length:
            for kw in self.never_kw:
                if kw == lower:
                    logger.info("recall_policy: skipped — short confirm '%s'", kw)
                    self._inherit_count = 0
                    self._last_recall_ts = now
                    self._last_recall_result = False
                    return False

        # 规则4: 简单确认/闲聊 → 跳过
        if lower in self.never_kw:
            logger.info("recall_policy: skipped — simple confirmation")
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = False
            return False

        # 规则5: 短消息但包含任务指示词 → 仍然是任务请求
        if _has_task_intent(lower):
            logger.info("recall_policy: triggered — task intent detected")
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = True
            return True

        # 规则6: 消息足够长且有实质内容 → 触发
        if len(msg) >= self.min_length:
            logger.info("recall_policy: triggered — substantive message (%d chars)", len(msg))
            self._inherit_count = 0
            self._last_recall_ts = now
            self._last_recall_result = True
            return True

        # 默认不检索
        self._inherit_count = 0
        self._last_recall_ts = now
        self._last_recall_result = False
        return False


# ── 全局单例 ────────────────────────────────────────────
_policy: Optional[RecallPolicy] = None


def get_recall_policy(config: Optional[Dict] = None) -> RecallPolicy:
    global _policy
    if _policy is None:
        _policy = RecallPolicy(config)
    return _policy


def should_recall_memory(msg: str, compressed: bool = False) -> bool:
    return get_recall_policy().should_recall(msg, compressed)


__all__ = ["RecallPolicy", "get_recall_policy", "should_recall_memory", "CORRECTION_PATTERNS"]
