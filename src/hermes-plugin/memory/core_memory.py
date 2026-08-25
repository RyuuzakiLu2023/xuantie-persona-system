"""核心记忆块 — LLM 可自编辑的结构化记忆"""
import json
import logging
import os
import re
from typing import Dict, List

from memory.utils import estimate_tokens

logger = logging.getLogger(__name__)


class CoreMemory:
    """LLM 可自编辑的核心记忆块"""

    DEFAULT_BLOCKS = {
        "user_profile": "",      # 用户是谁、偏好
        "current_project": "",   # 当前在做什么
        "key_decisions": "",     # 重要决策
    }

    def __init__(self, max_tokens: int = 500, persist_path: str = None):
        self.blocks = dict(self.DEFAULT_BLOCKS)
        self.max_tokens = max_tokens
        self.persist_path = persist_path
        self._load()

    # ── helpers ───────────────────────────────────────────

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> str:
        """截断文本到 max_tokens 以内（精确，中英文通用）。"""
        suffix = " [...]"
        target = max_tokens - estimate_tokens(suffix)
        if estimate_tokens(text) <= max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if estimate_tokens(text[:mid]) <= target:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + suffix

    # ── public api ────────────────────────────────────────

    def render(self) -> str:
        """渲染为 LLM 可读格式（带截断）"""
        parts = []
        total_tokens = 0
        for k, v in self.blocks.items():
            if v.strip():
                est_tokens = estimate_tokens(v)
                if total_tokens + est_tokens > self.max_tokens:
                    # 截断
                    remaining_tokens = self.max_tokens - total_tokens
                    if est_tokens > 0:
                        ratio = remaining_tokens / est_tokens
                        cut_len = int(len(v) * ratio)
                        if cut_len > 50:
                            parts.append(f"[{k}]\n{v[:cut_len]} [...]")
                    break
                parts.append(f"[{k}]\n{v}")
                total_tokens += est_tokens
        return "\n\n".join(parts) if parts else "（无核心记忆）"

    def update_block(self, block_name: str, new_content: str) -> bool:
        """更新块（带截断）"""
        if block_name not in self.blocks:
            logger.warning("未知的块名: %s", block_name)
            return False
        self.blocks[block_name] = self._truncate_to_tokens(
            new_content, self.max_tokens,
        )
        self._save()
        logger.info("更新核心记忆块: %s", block_name)
        return True

    def append_to_block(self, block_name: str, text: str) -> bool:
        """追加到块（带截断）"""
        if block_name not in self.blocks:
            logger.warning("未知的块名: %s", block_name)
            return False
        if self.blocks[block_name]:
            combined = self.blocks[block_name] + "\n" + text
        else:
            combined = text
        self.blocks[block_name] = self._truncate_to_tokens(
            combined, self.max_tokens,
        )
        self._save()
        logger.info("追加到核心记忆块: %s", block_name)
        return True
    
    def get_tool_schemas(self) -> List[dict]:
        """返回供 LLM 使用的工具 schema"""
        return [
            {
                "name": "update_core_memory",
                "description": "更新核心记忆块。当发现新信息需要覆盖旧信息时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_name": {
                            "type": "string",
                            "enum": list(self.blocks.keys()),
                            "description": "要更新的块名"
                        },
                        "content": {
                            "type": "string",
                            "description": "新的内容"
                        }
                    },
                    "required": ["block_name", "content"]
                }
            },
            {
                "name": "append_core_memory",
                "description": "追加到核心记忆块。当需要添加新条目而不是覆盖时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_name": {
                            "type": "string",
                            "enum": list(self.blocks.keys()),
                            "description": "要追加的块名"
                        },
                        "text": {
                            "type": "string",
                            "description": "要追加的文本"
                        }
                    },
                    "required": ["block_name", "text"]
                }
            }
        ]
    
    def _load(self):
        """从文件加载"""
        if self.persist_path and os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 结构验证
                if not isinstance(data, dict):
                    logger.warning("核心记忆文件格式错误：非字典")
                    return
                
                # 只更新已知的块，且值必须是字符串
                for key in self.blocks.keys():
                    if key in data and isinstance(data[key], str):
                        self.blocks[key] = data[key]
                
                logger.info("加载核心记忆: %s", self.persist_path)
            except json.JSONDecodeError:
                logger.warning("核心记忆文件 JSON 解析失败")
            except Exception as e:
                logger.warning("加载核心记忆失败: %s", e)
    
    def _save(self):
        """保存到文件"""
        if self.persist_path:
            try:
                os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
                with open(self.persist_path, 'w', encoding='utf-8') as f:
                    json.dump(self.blocks, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("保存核心记忆失败: %s", e)
