"""ContextBuilder — the glue layer that wires together all memory sub-modules.

Orchestrates the full lifecycle of advanced memory:
    1. **prefetch** — retrieve relevant memories before a conversation turn.
    2. **store** — persist new memories after a turn.
    3. **compress** — lazily compact old memories when thresholds are exceeded.

Phase 1 (current): prefetch only.  The official ``memory_manager`` still
handles MEMORY.md / USER.md reads and writes.  ContextBuilder complements
it with semantic retrieval via the hybrid retriever + Ollama embedding.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory.wal_engine import WALEngine, MemoryEntry
from memory.vector_store import VectorStore, Chunk
from memory.hybrid_retriever import HybridRetriever, MemoryDoc
from memory.cost_compressor import CostCompressor
from memory.embedding_provider import OllamaEmbedding, EmbeddingProvider
from memory.utils import compute_content_hash
from memory.llm_extractor import _infer_track

logger = logging.getLogger(__name__)


def _jaccard(a: str, b: str) -> float:
    """字符级 Jaccard 相似度"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _shared_nouns(a: str, b: str) -> int:
    """统计中文共同 2-gram 名词数量（简化版）"""
    # 提取连续 2 个汉字的组合
    cn_pat = re.compile(r'[\u4e00-\u9fff]{2,}')
    grams_a = set(cn_pat.findall(a))
    grams_b = set(cn_pat.findall(b))
    return len(grams_a & grams_b)

# ---------------------------------------------------------------------------
# Sensible defaults (overridable via config dict)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # -- embedding --
    "embedding_model": "bge-m3",
    "embedding_base_url": "http://127.0.0.1:11434",
    "embedding_timeout": 30.0,
    # -- retrieval --
    "retrieval_top_k": 10,
    "retrieval_token_budget": 2000,
    "relevance_threshold": 0.15,
    # -- compression --
    "compression_enabled": True,
    "compression_max_entries": 10000,
    "compression_trigger_ratio": 0.9,
    # -- lifecycle --
    "prefetch_only": True,
}


class ContextBuilder:
    """Central coordinator for the advanced memory subsystem.

    Parameters
    ----------
    workspace_dir:
        Agent workspace root.  Sub-module data (WAL, vector DB) is stored
        under ``<workspace>/.hermes/memory/``.
    context_window:
        Model context window size in tokens (used by the cost compressor).
    config:
        Optional override dict merged on top of ``DEFAULT_CONFIG``.
        Typically sourced from the ``memory_advanced`` section of the
        agent's ``config.yaml``.
    """

    def __init__(
        self,
        workspace_dir: str,
        context_window: int = 128_000,
        config: Optional[Dict[str, Any]] = None,
    ):
        # ---- resolve config ----
        self._cfg = dict(DEFAULT_CONFIG)
        if config:
            self._cfg.update(config)

        self._workspace = Path(workspace_dir).resolve()
        self._context_window = int(context_window)

        # ---- data directories ----
        self._mem_dir = self._workspace / ".hermes" / "memory"
        os.makedirs(self._mem_dir, exist_ok=True)
        self._wal_dir = str(self._mem_dir / "wal")

        _db_override = str(self._cfg.get("db_path", "") or "").strip()
        self._db_path = _db_override if _db_override else str(self._mem_dir / "vector_store.db")

        # ---- embedding provider ----
        self._embedding: EmbeddingProvider = OllamaEmbedding(
            model=str(self._cfg["embedding_model"]),
            base_url=str(self._cfg["embedding_base_url"]),
            timeout=float(self._cfg["embedding_timeout"]),
        )

        # ---- sub-modules ----
        self._wal = WALEngine(wal_dir=self._wal_dir)

        self._vector_store = VectorStore(db_path=self._db_path)

        self._retriever = HybridRetriever(
            embedding_provider=self._embedding,
            relevance_threshold=float(self._cfg["relevance_threshold"]),
        )

        self._compressor = CostCompressor(
            context_window=self._context_window,
            wal_engine=self._wal,
        )

        # Hydrate the retriever with any chunks already persisted in
        # the vector store so prefetch has data to search.
        self._hydrate_retriever()

        # ---- state ----
        self._prefetch_only = bool(self._cfg.get("prefetch_only", True))
        self._compress_lock = threading.Lock()

        # ---- R8 观测计数器 ----
        self._store_count = 0
        self._prefetch_count = 0
        self._probe_ok_count = 0
        self._probe_fail_count = 0
        self._timeline = []  # 最近 96 个数据点（24h 趋势）

        # Startup hygiene: compact once if the store is near capacity.
        # Prevents silent growth across long-running agent processes.
        if not self._prefetch_only and self._should_compress():
            try:
                self.compress()
            except Exception:
                logger.exception("startup compress failed")

        logger.info(
            "ContextBuilder initialized: workspace=%s window=%d prefetch_only=%s",
            self._workspace,
            self._context_window,
            self._prefetch_only,
        )

        # ---- R8: 自动启动 REST 观测服务 ----
        # ⚠️ 2026-08-16 修「抢 20000 端口」：rest_api=False 时完全不启动
        # （memory_bridge 独立进程场景），避免每次构造都抢 20000 撞上
        # gateway/MIMOX API → WinError 10048。默认 True 保持观测面板。
        if self._cfg.get("rest_api", True):
            try:
                from memory.api_server import start_server_background
                self._rest_thread = start_server_background(
                    context_builder=self,
                    vector_store=self._vector_store,
                )
            except Exception as e:
                logger.warning("REST API not started: %s", e)
                self._rest_thread = None
        else:
            self._rest_thread = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prefetch(self, query: str, top_k: Optional[int] = None) -> str:
        """Retrieve relevant memory snippets for the current conversation turn.

        Returns a formatted string suitable for injection into the user
        message (empty string when nothing is found).  Each snippet is
        tagged with a trust level: [TRUSTED] for curated MEMORY.md
        entries, [UNVERIFIED] for conversation-stored memories.
        """
        if not query.strip():
            return ""

        k = top_k or int(self._cfg["retrieval_top_k"])
        budget = int(self._cfg["retrieval_token_budget"])

        try:
            results = self._retriever.search(query, top_k=k)
        except Exception:
            logger.exception("prefetch recall failed for query=%r", query[:120])
            return ""

        if not results:
            return ""

        lines = ["[MEMORY CONTEXT — relevant past memories]", ""]
        used = 0
        idx = 0
        for doc, _score in results:
            if idx >= k:
                break
            trust = "[TRUSTED]" if "memory_md" in (doc.tags or []) else "[UNVERIFIED]"
            tokens = self._retriever._est(doc.content) if hasattr(self._retriever, "_est") else len(doc.content) // 3
            if used + tokens > budget:
                remaining = budget - used
                if remaining > 50:
                    snip = doc.content[:remaining * 2] + " [...]"
                    lines.append(f"{idx+1}. {trust} {snip.strip()}")
                break
            lines.append(f"{idx+1}. {trust} {doc.content.strip()}")
            used += tokens
            idx += 1
        self._prefetch_count += 1
        self._timeline.append({
            "ts": __import__('datetime').datetime.now().isoformat(),
            "prefetch_chars": len("\n".join(lines)),
            "store_writes": 0,
        })
        if len(self._timeline) > 96:
            self._timeline = self._timeline[-96:]
        return "\n".join(lines)

    def store(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Persist a new memory entry.

        Writes to WAL, indexes into VectorStore, and feeds the retriever
        so future :meth:`prefetch` calls can find it.

        Returns the entry id, or None if skipped (duplicate content).
        """
        # === Hash 去重 ===
        content_hash = compute_content_hash(content)
        if self._vector_store.chunk_exists_by_hash(content_hash):
            logger.debug("Skipping duplicate memory (hash match): %s", content[:50])
            return None  # 返回 None 表示跳过
        
        meta = metadata or {}

        # 1. WAL
        entry = self._wal.store(
            content=content,
            importance=float(meta.get("importance", 0.5)),
            tags=list(meta.get("tags", [])),
            session_id=str(meta.get("session_id", "default")),
            type=str(meta.get("type", "conversation")),
        )

        # 2. VectorStore
        chunk = Chunk(
            id=entry.id,
            content=content,
            timestamp=entry.timestamp,
            importance=entry.importance,
            tags=entry.tags,
            source=entry.session_id,
            token_count=entry.token_count,
            content_hash=content_hash,
            track=_infer_track(content),  # Phase 1 R5: 自动分轨
        )
        self._vector_store.store_chunk(chunk)
        self._last_stored_chunk = chunk  # 供 consolidation 使用

        # Phase 1 R9: 知识演化检测（日志模式，不自动标 superseded）
        try:
            evolutions = self._detect_evolution(content, entry.id)
            for target_id, relation, confidence in evolutions:
                logger.info(
                    "Evolution detected: %s %s (conf=%.2f) → target=%s [LOG ONLY]",
                    content[:30], relation, confidence, target_id,
                )
                self._vector_store.save_evolution(entry.id, target_id, relation, confidence)
                # 日志模式：不执行 mark_superseded，等验证准确率后再开
        except Exception as e:
            logger.debug("Evolution detection skipped: %s", e)

        # 3. Retriever (live update)
        doc = MemoryDoc(
            id=entry.id,
            content=content,
            timestamp=entry.timestamp,
            importance=entry.importance,
            tags=entry.tags,
            track=_infer_track(content),  # Phase 1 R5
        )
        with self._retriever._lock:
            self._retriever.docs.append(doc)
            self._retriever.bm25.add(content)
            # Compute + cache embedding for the new chunk
            try:
                vec = self._retriever._embed(content)
                self._retriever._embeddings.append(vec)
                self._vector_store.cache_embedding(
                    text=content,
                    embedding=vec,
                    model=str(self._cfg.get("embedding_model", "bge-m3")),
                )
            except Exception:
                pass  # non-fatal

        # 4. Lazy compression check
        if not self._prefetch_only and self._should_compress():
            self.compress()

        logger.debug("ContextBuilder stored entry=%s tokens=%d", entry.id, entry.token_count)
        self._store_count += 1
        self._timeline.append({
            "ts": __import__('datetime').datetime.now().isoformat(),
            "prefetch_chars": 0,
            "store_writes": 1,
        })
        if len(self._timeline) > 96:
            self._timeline = self._timeline[-96:]
        return entry.id

    # ── Phase 1 R9: 知识演化检测 ──────────────────────────

    def _detect_evolution(self, new_content: str, new_id: str) -> list:
        """检测新记忆和已有记忆的演化关系。

        返回 [(target_id, relation, confidence), ...]
        relation: replaces / enriches / confirms
        """
        candidates = self._vector_store.search_similar_for_evolution(new_content, top_k=5)
        if not candidates:
            return []

        results = []
        for target_id, target_content, _ in candidates:
            jaccard = _jaccard(new_content, target_content)

            # 极性翻转检测（"不再"、"改为"、"取消" 等）
            polarity_flip = bool(re.search(
                r'不再|改为|取消|不是|换了|替换成|更新为|修正为',
                new_content,
            ))

            # 共同名词检测（中文 2+ 汉字词交集）
            shared_nouns = _shared_nouns(new_content, target_content)

            if polarity_flip and jaccard > 0.2 and shared_nouns >= 2:
                results.append((target_id, "replaces", min(jaccard + 0.2, 0.9)))
            elif jaccard > 0.5:
                results.append((target_id, "enriches", jaccard * 0.8))
            elif jaccard > 0.3 and shared_nouns >= 1:
                results.append((target_id, "confirms", jaccard * 0.6))

        # confidence 门控：低于 0.6 不执行
        return [(tid, rel, conf) for tid, rel, conf in results if conf >= 0.6]

    def forget(self, query: str = None, chunk_id: str = None) -> int:
        """删除记忆。按 id 精确删或按 query 模糊匹配删除。
        
        Returns:
            实际删除的 chunk 数量
        """
        removed = 0
        if chunk_id:
            # 精确删除
            if self._vector_store.delete_chunk(chunk_id):
                removed = 1
        elif query:
            # 模糊匹配删除
            results = self._retriever.search(query, top_k=10)
            for doc, score in results:
                if score > 0.3:  # 相关性阈值
                    self._vector_store.delete_chunk(doc.id)
                    removed += 1
        
        # 删除后同步 retriever 的内存索引，防止 prefetch 返回已删 chunk
        if removed > 0:
            self._hydrate_retriever()
        
        return removed

    def improve(self) -> dict:
        """优化记忆：去重 + 过期清理"""
        # 1. Hash 去重
        dupes = self._vector_store.dedup_by_hash()
        # 2. 重建索引
        self._hydrate_retriever()
        return {"deduped": dupes}

    def compress(self) -> Dict[str, Any]:
        """Compact old / low-value memories.

        Delegates to :class:`CostCompressor` to summarise WAL entries
        and prunes the embedding cache via VectorStore LRU eviction.

        Thread-safe — concurrent calls serialise on an internal lock.
        Returns a stats dict.
        """
        if not self._compress_lock.acquire(blocking=False):
            logger.debug("compress already running, skipping")
            return {"skipped": True}

        try:
            wal_stats: Dict[str, Any] = {}

            # WAL compaction
            try:
                delta = self._wal.get_delta()
                if delta:
                    entries = [{"role": "memory", "content": e.content if hasattr(e, "content") else str(e)} for e in delta]
                    summary = self._compressor._summarize_rules(entries)
                    if summary:
                        summary_entry = self._wal.store(
                            content=summary,
                            type="fact",
                            importance=0.3,
                        )
                        ids = [e.id for e in delta if hasattr(e, "id")]
                        self._wal.mark_compressed(ids, summary_entry.id)
                        wal_stats["compacted"] = len(ids)
            except Exception:
                logger.exception("WAL compaction failed")

            # LRU cache eviction
            max_entries = int(self._cfg["compression_max_entries"])
            try:
                evicted = self._vector_store.cleanup_old_cache(max_entries=max_entries)
                wal_stats["cache_evicted"] = evicted
            except Exception:
                logger.exception("cache eviction failed")

            logger.info("ContextBuilder compress: %s", wal_stats)
            return wal_stats
        finally:
            self._compress_lock.release()

    def get_stats(self) -> Dict[str, Any]:
        """Return memory subsystem health stats."""
        vs = self._vector_store.get_stats()
        wal = self._wal.get_stats()
        recall = self._retriever.get_recall_stats() if hasattr(self._retriever, "get_recall_stats") else {}
        return {
            "vector_store": vs,
            "wal": wal,
            "retriever_docs": len(self._retriever.docs),
            "recall_stats": recall,
            "prefetch_only": self._prefetch_only,
        }

    def close(self) -> None:
        """Release resources (DB connections, etc.)."""
        self._vector_store.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_compress(self) -> bool:
        if not self._cfg.get("compression_enabled", True):
            return False
        max_entries = int(self._cfg["compression_max_entries"])
        ratio = float(self._cfg["compression_trigger_ratio"])
        count = self._vector_store.get_stats().get("chunk_count", 0)
        return count > int(max_entries * ratio)

    def _hydrate_retriever(self) -> None:
        """Load existing chunks from VectorStore into the retriever.

        Rebuilds both BM25 and vector index.  When a cached embedding
        exists in VectorStore, it is reused directly — avoiding N cold
        Ollama HTTP calls on every startup.  Only cache misses trigger
        ``_embed()``.

        2026-08 新增：hydrate 前先 replay WAL 里未落盘的条目——
        WAL 写成功但 VectorStore 写失败时，重启后自动补写，防数据丢。
        """
        # === WAL replay：未压缩未过期的条目补写 VectorStore ===
        # 幂等：hash 去重保证同内容不会重复入库存。
        # 在 _prefetch_only 设置前调用无碍——replay 只补缺失，不新增。
        try:
            cleaned = self._wal.cleanup_expired()
            if cleaned:
                logger.info("WAL cleanup: removed %d expired entries", cleaned)
        except Exception:
            logger.debug("WAL cleanup failed (non-fatal)", exc_info=True)

        try:
            pending = self._wal.read_uncompacted()
        except Exception:
            logger.debug("WAL read failed (non-fatal)", exc_info=True)
            pending = []

        if pending:
            replayed = 0
            for entry in pending:
                try:
                    content_hash = compute_content_hash(entry.content)
                    if self._vector_store.chunk_exists_by_hash(content_hash):
                        continue  # 已落盘，跳过
                    chunk = Chunk(
                        id=entry.id,
                        content=entry.content,
                        timestamp=entry.timestamp,
                        importance=entry.importance,
                        tags=list(entry.tags),
                        source=entry.session_id,
                        token_count=entry.token_count,
                        content_hash=content_hash,
                        track=_infer_track(entry.content),
                    )
                    self._vector_store.store_chunk(chunk)
                    replayed += 1
                except Exception:
                    logger.debug("WAL replay entry %s failed (non-fatal)", entry.id, exc_info=True)
            if replayed:
                logger.info("WAL replay: recovered %d entries into VectorStore", replayed)

        # === 原有 hydrate 逻辑：从 VectorStore 重建检索索引 ===
        try:
            chunks = self._vector_store.list_chunks(limit=10_000)
        except Exception:
            logger.exception("failed to list chunks during hydration")
            return

        if not chunks:
            return

        docs = []
        pre_computed = []  # None = cache miss; list[float] = cache hit
        cache_hits = 0

        # 批量加载所有缓存 embedding（避免 N+1 查询）
        cache_map = {}
        try:
            all_cache_rows = self._vector_store._get_conn().execute(
                "SELECT text_hash, embedding, dim FROM embedding_cache"
            ).fetchall()
            for row in all_cache_rows:
                cache_map[row["text_hash"]] = (row["embedding"], row["dim"])
        except Exception:
            pass  # 缓存表不存在或为空

        for ch in chunks:
            doc = MemoryDoc(
                id=ch.id,
                content=ch.content,
                timestamp=ch.timestamp,
                importance=ch.importance,
                tags=list(ch.tags),
            )
            docs.append(doc)
            # 从批量缓存中查找
            import hashlib
            text_hash = hashlib.sha256(ch.content.encode()).hexdigest()
            cached = cache_map.get(text_hash)
            if cached:
                cached_vec = self._vector_store._deserialize_embedding(cached[0], cached[1])
                cache_hits += 1
            else:
                cached_vec = None
            pre_computed.append(cached_vec)

        # index() with pre-computed embeddings — only computes for None entries
        self._retriever.index(docs, embeddings=pre_computed)

        # Cache any newly-computed embeddings (the cache misses)
        model = str(self._cfg.get("embedding_model", "bge-m3"))
        newly_cached = 0
        for i, d in enumerate(docs):
            if pre_computed[i] is None and i < len(self._retriever._embeddings):
                try:
                    self._vector_store.cache_embedding(
                        text=d.content,
                        embedding=self._retriever._embeddings[i],
                        model=model,
                    )
                    newly_cached += 1
                except Exception:
                    pass  # non-fatal

        logger.info(
            "hydrated retriever with %d chunks (%d cache hits, %d newly computed)",
            len(chunks), cache_hits, newly_cached,
        )


# Needed by _summarize_rules fallback path inside CostCompressor
import json  # noqa: E402
