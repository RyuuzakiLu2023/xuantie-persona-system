"""MIMOX memory 子系统公共工具"""

import hashlib
import re


def compute_content_hash(content: str) -> str:
    """计算内容的 MD5 哈希值（用于去重）。
    
    选择 MD5 而非 SHA256 的理由：
    - 碰撞概率对记忆去重场景足够低
    - 计算速度快 ~2x
    - 存储空间小（32 vs 64 字符）
    """
    return hashlib.md5(content.strip().encode()).hexdigest()


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数。

    中文字符约 1.5 字/token，英文单词约 1.3 token/word。
    """
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    en_text = re.sub(r'[\u4e00-\u9fff]', ' ', text)
    en_words = len(en_text.split())

    cn_tokens = cn_chars * 1.5
    en_tokens = en_words * 1.3

    return int(cn_tokens + en_tokens)
