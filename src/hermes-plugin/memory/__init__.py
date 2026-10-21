
# ================================================================
# memory/__init__.py · 包导览
#
# 导出：WALEngine/MemoryEntry（wal）· HybridRetriever/MemoryDoc（检索）· CostCompressor（压缩）
#       VectorStore/Chunk（存储）· ContextBuilder（胶合）· embedding_provider（向量）。
#
# 无自身逻辑，只是包门面；改导出需与各子系统保持同名。
# ================================================================
from .wal_engine import WALEngine, MemoryEntry
from .hybrid_retriever import HybridRetriever, MemoryDoc
from .cost_compressor import CostCompressor
from .vector_store import VectorStore, Chunk
from .context_builder import ContextBuilder
from .embedding_provider import (
    EmbeddingProvider,
    SimpleHashEmbedding,
    OllamaEmbedding,
)

__all__ = [
    "WALEngine", "MemoryEntry",
    "HybridRetriever", "MemoryDoc",
    "CostCompressor",
    "VectorStore", "Chunk",
    "ContextBuilder",
    "EmbeddingProvider",
    "SimpleHashEmbedding",
    "OllamaEmbedding",
]
