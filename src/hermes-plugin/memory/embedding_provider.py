"""Embedding provider abstraction.

Defines an abstract interface for embedding providers so the memory
system can swap between local hash, Ollama, OpenAI, or other backends
without changing retrieval / storage logic.

Current implementations:
    - OllamaEmbedding (bge-m3) — production, requires local Ollama
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Abstract embedding provider interface."""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Compute embeddings for a batch of texts.

        Returns one vector per input text, in the same order.
        """

    @abstractmethod
    def embed_single(self, text: str) -> List[float]:
        """Compute embedding for a single text (convenience wrapper)."""

    @abstractmethod
    def dim(self) -> int:
        """Dimensionality of the embedding vectors."""


class SimpleHashEmbedding(EmbeddingProvider):
    """Deterministic hash-based embedding for testing / development.

    Uses SHA-256 so embeddings are stable across process restarts
    (unlike Python's built-in ``hash()`` which is randomized per
    process since 3.3).

    NOT suitable for semantic search — only for wiring validation.
    """

    def __init__(self, dim: int = 128):
        self._dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        results: List[List[float]] = []
        for text in texts:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            # Expand/repeat bytes to fill the requested dimension.
            vec: List[float] = []
            needed = self._dim
            while len(vec) < needed:
                for b in h:
                    vec.append((float(b) / 255.0) * 2.0 - 1.0)
                    if len(vec) >= needed:
                        break
            results.append(vec)
        return results

    def embed_single(self, text: str) -> List[float]:
        return self.embed([text])[0]

    def dim(self) -> int:
        return self._dim


class OllamaEmbedding(EmbeddingProvider):
    """Embedding via a local Ollama server.

    Defaults to ``bge-m3`` (1024-dim multilingual embedding model).
    Requires ``httpx`` to be installed.

    The first call to :meth:`embed` or :meth:`dim` will trigger a
    one-shot probe to determine the model's output dimensionality;
    subsequent calls reuse the cached value.
    """

    def __init__(
        self,
        model: str = "bge-m3",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 30.0,
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._dim: Optional[int] = None
        # 2026-08-31（N-收尾）：Ollama 故障黑名单冷却——服务 502/挂起时
        # 快速降级 SimpleEmbedder，不再每个请求干等满超时（实测 30s×2 次尝试）。
        self._blackout_until: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # 2026-08-25 修复（病B）：超长文本截断（Ollama bge-m3 超上下文会 400
    # "input length exceeds the context length"——库里混入的英文长 prompt 实证）
    _MAX_CHARS = 4000

    def embed(self, texts):
        # Polymorphic: accept str (single text) for HybridRetriever compat,
        # or List[str] for batch usage.
        if isinstance(texts, str):
            return self.embed_single(texts)
        if not texts:
            return []

        import httpx

        # 冷却期内直接快速失败，交 HybridRetriever 降级 SimpleEmbedder
        if time.time() < self._blackout_until:
            raise RuntimeError(
                "Ollama embedding blackout (recent failure, cooldown active)"
            )

        results: List[List[float]] = []
        url = f"{self._base_url}/api/embed"
        batch_size = 32  # Ollama handles batch input natively
        # 连接/写入 2s 快速失败；读取仍按业务超时（服务挂起时等待期有限）
        fast_timeout = httpx.Timeout(
            self._timeout, connect=2.0, write=2.0, pool=2.0
        )

        for start in range(0, len(texts), batch_size):
            batch = [
                (t[: self._MAX_CHARS] if isinstance(t, str) else t)
                for t in texts[start : start + batch_size]
            ]
            try:
                resp = httpx.post(
                    url,
                    json={"model": self._model, "input": batch},
                    timeout=fast_timeout,
                    trust_env=False,  # 2026-08-31 修复：本地 Ollama 不走系统代理（系统代理 127.0.0.1:7890 会吞 loopback 请求回 502）
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings", [])
                for vec in embeddings:
                    results.append(vec)
                    if self._dim is None:
                        self._dim = len(vec)
            except Exception:
                # 2026-08-25 修复（病C）：单批失败只零填该批（该批走 FTS-only），
                # 不再全局 raise——一颗老鼠屎不再坏一锅粥。
                # 先探测一次维度；探测也失败（Ollama 全挂）才 raise 触发全局降级。
                if self._dim is None:
                    try:
                        probe = httpx.post(
                            url,
                            json={"model": self._model, "input": ["dimension probe"]},
                            timeout=fast_timeout,
                            trust_env=False,  # 同上：本地请求绕开系统代理
                        )
                        probe.raise_for_status()
                        vecs = probe.json().get("embeddings", [])
                        if vecs:
                            self._dim = len(vecs[0])
                    except Exception:
                        # 进入 60s 冷却：期间不再叩 Ollama，快速降级
                        self._blackout_until = time.time() + 60.0
                        logger.error(
                            "Ollama embedding unreachable (model=%s) — "
                            "global fallback + 60s blackout",
                            self._model,
                        )
                        raise
                logger.warning(
                    "Ollama embedding batch failed (start=%d size=%d) — zero-fill %d "
                    "items (FTS-only for this batch)",
                    start, len(batch), len(batch),
                )
                results.extend([[0.0] * self._dim for _ in batch])

        return results

    def embed_single(self, text: str) -> List[float]:
        return self.embed([text])[0]

    def dim(self) -> int:
        if self._dim is None:
            # One-shot probe to discover dimensionality.
            vec = self.embed_single("dimension probe")
            self._dim = len(vec)
        return self._dim
