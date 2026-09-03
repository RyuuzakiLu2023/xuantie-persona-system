# -*- coding: utf-8 -*-
"""axis_predicates.py — Phase T T-3：跳过轴判据（共用一份实现，杜绝探测器另写一套判据）。

三轴：
  ollama    httpx GET http://127.0.0.1:11434/api/tags 200 → True
  jieba     importlib.util.find_spec("jieba") 非空 → True
  canonical 基准库只读连接 integrity_check == "ok" → True
用法：测试里 pytest.mark.skipif(not axis_predicates.jieba_available(), reason="jieba 不可用")。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

# 缺省 canonical 路径（与 conftest 一致：干净快照优先，其次主库）
_CANONICAL_CANDIDATES = [
    r"<USER_DOCS>/xtmem-test/vector_store_r3_canonical.db",
    r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db",
]


def ollama_available(timeout: float = 1.5) -> bool:
    """Ollama 服务是否在 11434 端口可达（embedding 相关用例的 skip 判据）。"""
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def jieba_available() -> bool:
    """jieba 是否安装（BM25 中文分词的 skip 判据）。"""
    return importlib.util.find_spec("jieba") is not None


def canonical_ok(path: str = None) -> bool:
    """canonical 基准库是否可只读且 integrity_check=ok（依赖基准库数据分布用例的 skip 判据）。"""
    p = path or next((c for c in _CANONICAL_CANDIDATES if Path(c).exists()), None)
    if not p:
        return False
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        return ok == "ok"
    except Exception:
        return False


# 轴登记表：key → (predicate, 说明)。用于四重自证。
AXES = {
    "ollama": (ollama_available, "所有 embedding 相关用例"),
    "jieba": (jieba_available, "BM25 中文分词相关用例"),
    "canonical": (canonical_ok, "依赖基准库数据分布的用例（tmp_db 拷贝源）"),
}
