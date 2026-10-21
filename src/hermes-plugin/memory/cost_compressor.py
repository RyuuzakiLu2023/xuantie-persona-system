
# ================================================================
# cost_compressor.py · 总导游图（改代码前必读）
#
# 这是什么：成本压缩器——把超长 WAL 条目摘要成短记忆。可达但休眠：
#           context_builder.py:26/132/154/373/488/535 引用，生产 chunk_count 未达 _should_compress 阈值未触发。
#
# 结构：CostCompressor(_summarize_rules) 纯规则摘要；trigger 在 context_builder.compress()。
#
# 修改铁律：别当死代码删——是「可达但未触发」，P4.3-d 长 chunk 治理会用上。
# ================================================================
import logging
from typing import List, Optional, Callable, Any
from .utils import estimate_tokens

logger = logging.getLogger(__name__)


class CostCompressor:
    """省钱压缩器 — 60% 阈值就开始压"""

    def __init__(
        self,
        context_window: int = 128_000,
        compress_threshold: float = 0.6,
        keep_recent_turns: int = 3,
        max_tool_output_chars: int = 500,
        max_inject_chars: int = 30000,
        llm_call_fn: Optional[Callable] = None,
        cheap_llm_call_fn: Optional[Callable] = None,
        wal_engine: Any = None,
    ):
        self.context_window = context_window
        self.compress_threshold = compress_threshold
        self.keep_recent_turns = keep_recent_turns
        self.max_tool_output_chars = max_tool_output_chars
        self.max_inject_chars = max_inject_chars
        self.llm_call = llm_call_fn
        self.cheap_llm_call = cheap_llm_call_fn
        self.wal_engine = wal_engine

    def should_compress(self, current_tokens: int) -> bool:
        return current_tokens >= int(self.context_window * self.compress_threshold)

    def compress_messages(self, messages: List[dict], current_tokens: int) -> List[dict]:
        if not self.should_compress(current_tokens):
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= self.keep_recent_turns * 2:
            return messages

        keep = self.keep_recent_turns * 2
        recent = non_system[-keep:]
        to_compress = non_system[:-keep]

        # 先修剪工具输出
        to_compress = self._trim_tool_outputs(to_compress)

        # 生成摘要
        summary = self._summarize(to_compress)

        # 新增：把摘要存进 WAL，避免截断内容永久丢失
        if self.wal_engine:
            try:
                self.wal_engine.store(
                    content=summary,
                    type="conversation",
                    importance=0.7,
                    tags=["compressed", "auto-summary"],
                )
            except Exception as e:
                logger.warning("Failed to store summary in WAL: %s", e)

        summary_msg = {"role": "system", "content": f"[历史摘要]\n{summary}"}
        result = system_msgs + [summary_msg] + recent

        new_tokens = self._estimate_tokens(result)
        logger.info("Compressed: %d → %d tokens (saved %d)",
                    current_tokens, new_tokens, current_tokens - new_tokens)
        return result

    def trim_tool_output(self, content: str, is_recent: bool = False) -> str:
        if not content:
            return content
        limit = self.max_tool_output_chars * (3 if is_recent else 1)
        if len(content) <= limit:
            return content
        if self._contains_error(content):
            return content[:limit*2] + "\n[... error truncated ...]"
        if self._is_code(content):
            return self._trim_code(content, limit)
        head = content[:int(limit * 0.7)]
        tail = content[-int(limit * 0.2):]
        return f"{head}\n[... {len(content)-limit} chars trimmed ...]\n{tail}"

    def _trim_tool_outputs(self, messages: List[dict]) -> List[dict]:
        result = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.max_tool_output_chars:
                    msg = {**msg, "content": self.trim_tool_output(content)}
            result.append(msg)
        return result

    def _summarize(self, messages: List[dict]) -> str:
        llm = self.cheap_llm_call or self.llm_call
        if llm:
            return self._summarize_llm(messages, llm)
        return self._summarize_rules(messages)

    def _summarize_llm(self, messages: List[dict], llm: Callable) -> str:
        parts = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                parts.append(f"[{role}]: {content[:200]}")
        prompt = f"用 300 字总结以下对话的关键信息。保留：决策、意图、未完成任务。丢弃：客套话、重复。\n\n{chr(10).join(parts[-20:])}\n\n摘要："
        try:
            return llm([{"role": "user", "content": prompt}])
        except Exception:
            return self._summarize_rules(messages)

    def _summarize_rules(self, messages: List[dict]) -> str:
        lines = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                lines.append(f"[{role}]: {content[:80]}")
        return "\n".join(lines[-10:])

    @staticmethod
    def _contains_error(content: str) -> bool:
        lower = content.lower()
        return any(kw in lower for kw in ["error", "exception", "traceback", "failed"])

    @staticmethod
    def _is_code(content: str) -> bool:
        indicators = ["def ", "class ", "import ", "function ", "const ", "{"]
        return sum(1 for i in indicators if i in content) >= 3

    @staticmethod
    def _trim_code(content: str, limit: int) -> str:
        lines = content.split("\n")
        kept = [l for l in lines if any(l.strip().startswith(k) for k in ("def ", "class ", "import ", "# ", "// "))]
        if kept:
            return "\n".join(kept[:15]) + f"\n[... {len(lines)-len(kept)} lines trimmed ...]"
        return content[:limit] + "\n[... trimmed ...]"

    @staticmethod
    def _estimate_tokens(messages) -> int:
        """估算消息列表的 token 数"""
        if isinstance(messages, list):
            return estimate_tokens("\n".join(m.get("content", "") for m in messages if isinstance(m, dict)))
        return estimate_tokens(str(messages))
