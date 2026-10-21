"""LLM 记忆提取器 — 从对话中提取结构化记忆事实

Phase 1 R5: 新增 _infer_track() — 根据记忆内容自动推断分轨标签
"""
import atexit
import json
import logging
import re
import concurrent.futures
from typing import List, Callable, Optional

logger = logging.getLogger(__name__)

from memory.utils import estimate_tokens as _estimate_tokens
_extraction_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="llm-extract"
)
atexit.register(_extraction_executor.shutdown, wait=False)

EXTRACTION_PROMPT = """你是一个记忆提取器。从对话中提取值得长期记住的事实。

已有记忆（避免重复提取）：
{existing_memories}

最近对话：
{conversation}

请提取值得长期记住的事实，返回 JSON 数组，每条记忆格式：
{{"text": "★ 提取的事实", "importance": 0.1-1.0, "tags": ["标签"]}}

规则：
- 只提取事实、偏好、决策
- 忽略寒暄、调试信息、临时讨论
- 不要提取已存在的记忆
- 如果没有值得提取的内容，返回空数组 []
- importance >= 0.7 的记忆，text 前加 ★（保护符号，防止被清理）

重要：你的回复必须是纯 JSON，不要有任何解释文字、前缀或后缀。直接以 [ 开头。"""


def _extract_json_from_text(text: str):
    """从可能包含非 JSON 文本的返回值中提取 JSON 数组。
    
    处理 LLM 返回 "这是 JSON: [...]" 之类的情况。
    """
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # 找 [ ... ] 部分
    import re
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    
    # 找 { ... } 部分（可能是单个对象）
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            obj = json.loads(match.group())
            return [obj]  # 包装成数组
        except json.JSONDecodeError:
            pass
    
    logger.warning("无法从 LLM 返回中提取 JSON: %s", text[:200])
    return []


def extract_memories(
    conversation: str,
    existing_memories: List[str],
    llm_fn: Callable[[str], str],
    timeout: float = 30.0,
) -> List[dict]:
    """调用 LLM 提取记忆事实
    
    Args:
        conversation: 最近对话内容
        existing_memories: 已有记忆列表（避免重复提取）
        llm_fn: LLM 调用函数，接受 prompt 返回字符串
        timeout: LLM 调用超时秒数，默认 30 秒
    
    Returns:
        提取的记忆列表 [{"text": "...", "importance": 0.8, "tags": [...]}]
    """
    prompt = EXTRACTION_PROMPT.format(
        existing_memories="\n".join(existing_memories[:10]) if existing_memories else "（无）",
        conversation=conversation
    )
    
    def _call_llm():
        return llm_fn(prompt)
    
    try:
        future = _extraction_executor.submit(_call_llm)
        result = future.result(timeout=timeout)
        
        # 从返回文本中提取 JSON（处理非纯 JSON 输出）
        memories = _extract_json_from_text(result)
        if not isinstance(memories, list):
            logger.warning("LLM 返回非数组格式: %s", type(memories))
            return []
        return memories
    except concurrent.futures.TimeoutError:
        logger.warning("LLM 提取超时 (%.1fs)", timeout)
        return []
    except json.JSONDecodeError as e:
        logger.error("LLM 返回的 JSON 解析失败: %s", e)
        return []
    except Exception as e:
        logger.error("提取记忆失败: %s", e)
        return []


# ── Phase 1 R5: 零退化分轨自动推断 ──────────────────────

_IDENTITY_PATTERNS = re.compile(r'生日|名字|年龄|职业|喜欢|讨厌|我是|我叫|我住在|真名')
_PREFERENCE_PATTERNS = re.compile(r'铁律|底线|规则|不要|必须|偏好|禁止|绝不|永远')
_EMOTION_PATTERNS = re.compile(r'开心|难过|累|烦|兴奋|生气|伤心|高兴|焦虑|失望')


def _infer_track(content: str) -> str:
    """根据记忆内容自动推断分轨标签。

    identity: 身份信息（生日/名字/年龄/职业/喜欢/讨厌）
    preference: 偏好/规则（铁律/底线/不要/必须）
    emotion: 情绪状态（开心/难过/累/烦）
    default: 普通事实
    """
    if _IDENTITY_PATTERNS.search(content):
        return "identity"
    if _PREFERENCE_PATTERNS.search(content):
        return "preference"
    if _EMOTION_PATTERNS.search(content):
        return "emotion"
    return "default"
