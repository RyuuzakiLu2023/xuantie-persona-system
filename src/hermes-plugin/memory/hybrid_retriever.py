import math, re, hashlib, logging, threading, time as _time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
from collections import Counter
from .utils import estimate_tokens

logger = logging.getLogger(__name__)


@dataclass
class MemoryDoc:
    id: str
    content: str
    timestamp: float
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    access_count: int = 0
    status: str = "confirmed"
    # Phase 1 R5: 零退化分轨
    track: str = "default"
    # Phase 1 R9: 知识演化
    state: str = "active"
    superseded_by: str = ""


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: List[List[str]] = []
        self.doc_len: List[int] = []
        self.avgdl: float = 0
        self.df: Counter = Counter()
        self.N: int = 0

    def index(self, documents: List[str]):
        self.docs = [self._tokenize(d) for d in documents]
        self.doc_len = [len(d) for d in self.docs]
        self.avgdl = sum(self.doc_len) / len(self.doc_len) if self.doc_len else 1
        self.N = len(self.docs)
        self.df = Counter()
        for doc in self.docs:
            for term in set(doc):
                self.df[term] += 1

    def add(self, document: str):
        """增量添加单个文档到 BM25 索引"""
        tokens = self._tokenize(document)
        self.docs.append(tokens)
        self.doc_len.append(len(tokens))
        self.N = len(self.docs)
        self.avgdl = sum(self.doc_len) / self.N
        for term in set(tokens):
            self.df[term] = self.df.get(term, 0) + 1

    def search(self, query: str, top_k: int = 20) -> List[Tuple[int, float]]:
        query_terms = self._tokenize(query)
        scores = []
        for i, doc in enumerate(self.docs):
            score = 0
            doc_counter = Counter(doc)
            dl = self.doc_len[i]
            for term in query_terms:
                if term not in doc_counter:
                    continue
                tf = doc_counter[term]
                df = self.df.get(term, 0)
                idf = math.log((self.N - df + 0.5) / (df + 0.5) + 1)
                tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                score += idf * tf_norm
            if score > 0:
                scores.append((i, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        text = text.lower()
        cn_chars = [c for c in text if '\u4e00' <= c <= '\u9fff']
        en_words = re.findall(r'[a-z0-9]+', text)
        cn_bigrams = [''.join(cn_chars[i:i+2]) for i in range(len(cn_chars)-1)]
        return cn_chars + cn_bigrams + en_words


class SimpleEmbedder:
    @staticmethod
    def embed(text: str, dim: int = 128) -> List[float]:
        vec = [0.0] * dim
        tokens = list(text.lower().strip())
        bigrams = [tokens[i:i+2] for i in range(len(tokens)-1)]
        for gram in bigrams:
            h = hashlib.md5(''.join(gram).encode()).hexdigest()
            idx = int(h[:8], 16) % dim
            sign = 1 if int(h[8:9], 16) % 2 == 0 else -1
            vec[idx] += sign
        norm = math.sqrt(sum(x*x for x in vec))
        if norm > 0:
            vec = [x/norm for x in vec]
        return vec

    @staticmethod
    def cosine(a: List[float], b: List[float]) -> float:
        n = min(len(a), len(b))
        dot = sum(a[i]*b[i] for i in range(n))
        na = math.sqrt(sum(x*x for x in a[:n]))
        nb = math.sqrt(sum(x*x for x in b[:n]))
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0




_TTL_DEBUG_LOG = 3600        # 1 hour
_TTL_INTERMEDIATE = 86400     # 24 hours
_TTL_DEFAULT = 604800         # 7 days


def _classify_ttl(content: str, tags: list = None, track: str = "default") -> int:
    """根据内容分类返回 TTL 秒数。

    规则：
    - identity/preference track → 永不衰减（0）
    - emotion track → 5 天（加速衰减）
    - 含 ★/🔒/❗/⚠️ → 永久（保护符号）
    - 含 Traceback/Error/Exception → 调试日志（1小时）
    - 含 中间/临时/草稿 → 中间步骤（24小时）
    - metadata 含 "important" / "永久" → 永久（0 = never expire）
    - 默认：7天
    """
    # Phase 1 R5: 分轨优先级最高
    if track in ("identity", "preference"):
        return 0  # 永不衰减
    if track == "emotion":
        return 432000  # 5 天

    tags_lower = [t.lower() for t in (tags or [])]
    clower = (content or "").lower()

    # 保护符号：含 ★/🔒/❗/⚠️ 的条目永久保留
    if any(sym in (content or "") for sym in ("★", "🔒", "❗", "⚠️")):
        return 0

    # 调试日志：含 Traceback/error
    if any(kw in clower for kw in ("traceback", "error:", "exception:", "failed:", "errno")):
        return _TTL_DEBUG_LOG

    # 中间步骤
    if any(kw in clower for kw in ("中间", "临时", "草稿", "wip", "temporary", "intermediate")):
        return _TTL_INTERMEDIATE

    # 关键决策：metadata 标记
    if any(t in tags_lower for t in ("important", "永久", "关键", "core")):
        return 0  # never expire

    return _TTL_DEFAULT


class HybridRetriever:
    """L4 混合检索：BM25:向量 = 3:7、时间衰减、MMR 去重、四级降级链 + 相关性阈值（AdaMEM层）"""

    def __init__(
        self,
        bm25_weight: float = 0.3,
        vector_weight: float = 0.7,
        recency_half_life_hours: float = 720.0,
        mmr_lambda: float = 0.7,
        embedding_provider=None,
        relevance_threshold: float = 0.15,
        # MIMOX: 语义去重 — 工具输出相似度>threshold 只保留最新一条
        semantic_dedup_enabled: bool = True,
        semantic_dedup_threshold: float = 0.85,
    ):
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.recency_half_life = recency_half_life_hours
        self.mmr_lambda = mmr_lambda
        self.relevance_threshold = relevance_threshold
        self.semantic_dedup_enabled = semantic_dedup_enabled
        self.semantic_dedup_threshold = semantic_dedup_threshold
        self.bm25 = BM25()
        self.embedder = SimpleEmbedder()
        self.embedding_provider = embedding_provider
        self.docs: List[MemoryDoc] = []
        self._embeddings: List[List[float]] = []
        # 追踪检索统计（供定期清理使用）
        self._recall_counts: Dict[str, int] = {}  # doc_id -> recall_count
        # TTL 过期统计
        self._expired_count: int = 0
        self._lock = threading.Lock()

    def _embed(self, text: str) -> List[float]:
        """优先使用 embedding_provider（Ollama/Gemini/OpenAI），fallback SimpleEmbedder"""
        if self.embedding_provider:
            try:
                return self.embedding_provider.embed(text)
            except Exception:
                logger.warning("Embedding provider failed, falling back to SimpleEmbedder", exc_info=True)
        return self.embedder.embed(text)

    def _cosine(self, a: List[float], b: List[float]) -> float:
        """优先使用 embedding_provider 的 cosine，fallback SimpleEmbedder"""
        emb = self.embedding_provider if self.embedding_provider else self.embedder
        if hasattr(emb, 'cosine'):
            return emb.cosine(a, b)
        return self.embedder.cosine(a, b)

    def semantic_dedup(self, threshold: float = 0.85) -> int:
        """公开去重入口：对已 index 的 self.docs 做语义去重（相似度>threshold 只留最新）。

        返回被移除的文档数；调用方通常在本批文档 index() 之后调用一次。
        """
        if not self.semantic_dedup_enabled or len(self.docs) < 2:
            return 0
        order = sorted(
            range(len(self.docs)),
            key=lambda i: self.docs[i].timestamp,
            reverse=True,
        )
        kept_docs: List[MemoryDoc] = []
        kept_vecs: List[List[float]] = []
        removed = 0
        for i in order:
            doc = self.docs[i]
            vec = (
                self._embeddings[i]
                if i < len(self._embeddings)
                else self._embed(doc.content)
            )
            is_dup = False
            for kv in kept_vecs:
                if self._cosine(vec, kv) > threshold:
                    is_dup = True
                    break
            if is_dup:
                removed += 1
                continue
            kept_docs.append(doc)
            kept_vecs.append(vec)
        if removed == 0:
            return 0
        kept_docs.sort(key=lambda d: d.timestamp)
        self.docs = kept_docs
        self.bm25.index([d.content for d in self.docs])
        self._embeddings = kept_vecs
        return removed

    def index(self, docs: List[MemoryDoc], embeddings: Optional[List[Optional[List[float]]]] = None):
        """Build BM25 index and vector embeddings.

        Parameters
        ----------
        docs:
            Memory documents to index.
        embeddings:
            Optional pre-computed embedding vectors (one per doc, same order).
            Entries can be ``None`` for docs whose embedding is not yet computed;
            those will be filled by calling ``_embed()``.  When *None* is passed,
            all embeddings are computed from scratch (legacy behaviour).
        """
        self.docs = docs
        texts = [d.content for d in docs]
        self.bm25.index(texts)
        if embeddings is not None and len(embeddings) == len(docs):
            self._embeddings = []
            for i, doc in enumerate(docs):
                if embeddings[i] is not None:
                    self._embeddings.append(embeddings[i])
                else:
                    self._embeddings.append(self._embed(doc.content))
        else:
            self._embeddings = [self._embed(t) for t in texts]

    def _is_expired(self, doc: MemoryDoc, now: float = None) -> bool:
        """检查文档是否 TTL 过期。TTL=0 表示永不过期。"""
        ttl = _classify_ttl(doc.content, doc.tags, getattr(doc, 'track', 'default'))
        if ttl == 0:
            return False  # permanent
        if now is None:
            now = _time.time()
        return (now - doc.timestamp) > ttl

    def get_expired_stats(self) -> int:
        """返回最近一次 search 跳过的过期文档数"""
        return self._expired_count

    def search(self, query: str, top_k: int = 10, now: Optional[float] = None) -> List[Tuple[MemoryDoc, float]]:
        if not self.docs:
            return []
        import time as _time
        if now is None:
            now = _time.time()

        # BM25 分数（归一化）
        bm25_results = self.bm25.search(query, top_k=top_k * 3)
        max_bm25 = max(s for _, s in bm25_results) if bm25_results else 1
        bm25_scores = {idx: score/max_bm25 for idx, score in bm25_results}

        # 向量分数
        query_vec = self._embed(query)
        vector_scores = {}
        for i, doc_vec in enumerate(self._embeddings):
            sim = self._cosine(query_vec, doc_vec)
            if sim > 0:
                vector_scores[i] = sim

        # 合并
        all_indices = set(bm25_scores.keys()) | set(vector_scores.keys())
        scored = []
        self._expired_count = 0
        for idx in all_indices:
            if idx >= len(self.docs):
                continue
            doc = self.docs[idx]
            # MIMOX: Query-conditioned TTL — 根据当前查询相关性动态调整过期衰减
            raw_bm = bm25_scores.get(idx, 0)
            raw_vec = vector_scores.get(idx, 0)
            bm = raw_bm * self.bm25_weight
            vec = raw_vec * self.vector_weight
            ttl = _classify_ttl(doc.content, doc.tags, getattr(doc, 'track', 'default'))
            if ttl > 0:
                age_ratio = (now - doc.timestamp) / ttl
                if age_ratio >= 1.0:
                    self._expired_count += 1
                    continue
                # Neural Paging: 相关性越高衰减越慢（0.3x~1.7x 有效TTL伸缩）
                relevance = (raw_bm + raw_vec) / 2
                ttl_factor = 0.3 + relevance * 1.4
                ttl_decay = math.exp(-age_ratio / ttl_factor)
            else:
                ttl_decay = 1.0  # permanent
            # MIMOX: 三态 — refuted 条目不参与检索评分
            if doc.status == "refuted":
                continue
            # Phase 1 R9: superseded 条目不参与检索
            if getattr(doc, 'state', 'active') == "superseded":
                continue
            age_hours = (now - doc.timestamp) / 3600
            recency = math.exp(-age_hours / self.recency_half_life)
            final = (bm + vec) * ttl_decay + recency * 0.1 + doc.importance * 0.05
            # AdaMEM层：低于相关性阈值的结果直接丢弃
            if final >= self.relevance_threshold:
                scored.append((idx, final))
        scored.sort(key=lambda x: x[1], reverse=True)

        # MMR 去重
        selected = self._mmr_rerank(query_vec, scored, top_k)

        # 追踪引用计数（供定期清理使用）
        for idx, _ in selected:
            doc = self.docs[idx]
            self._recall_counts[doc.id] = self._recall_counts.get(doc.id, 0) + 1

        return [(self.docs[idx], score) for idx, score in selected]

    def get_recall_stats(self) -> Dict[str, int]:
        """返回所有文档的引用计数并清理，供定期清理判断冷记忆"""
        result = dict(self._recall_counts)
        self._recall_counts.clear()
        return result

    def recall_with_fallback(self, query: str, token_budget: int, top_k: int = 10) -> dict:
        """
        四级降级链：
          L1 Hybrid — BM25 + Vector 混合搜索（正常路径）
          L2 BM25   — 纯关键词 BM25 搜索
          L3 FTS    — 正则全文扫描
          L4 File   — 文件名/路径匹配
        返回 {"level": 1-4, "results": [...]}
        """
        # L1: Hybrid BM25 + Vector 混合搜索（正常路径）
        results = self.search(query, top_k=top_k)
        if results:
            return {"level": 1, "results": results}

        # L2: 纯 BM25 降级
        bm25_results = self.bm25.search(query, top_k=top_k)
        if bm25_results:
            scored = [(idx, score) for idx, score in bm25_results if idx < len(self.docs)]
            # Filter refuted + superseded（与 L1 一致，见 search() 的三态过滤）
            scored = [
                (idx, s) for idx, s in scored
                if self.docs[idx].status != "refuted"
                and getattr(self.docs[idx], 'state', 'active') != "superseded"
            ]
            return {"level": 2, "results": [(self.docs[idx], s) for idx, s in scored]}

        # L3: 正则 FTS 全文扫描
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        fts_hits: List[Tuple[MemoryDoc, float]] = []
        for doc in self.docs:
            # 与 L1 一致：refuted 和 superseded 都不参与检索
            if doc.status == "refuted" or getattr(doc, 'state', 'active') == "superseded":
                continue
            if pattern.search(doc.content):
                fts_hits.append((doc, 1.0))
        if fts_hits:
            return {"level": 3, "results": fts_hits[:top_k]}

        # L4: 文件名/路径匹配
        path_hits: List[Tuple[MemoryDoc, float]] = []
        q_lower = query.lower()
        for doc in self.docs:
            # 与 L1 一致：refuted 和 superseded 都不参与检索
            if doc.status == "refuted" or getattr(doc, 'state', 'active') == "superseded":
                continue
            if q_lower in doc.id.lower() or q_lower in doc.content[:80].lower():
                path_hits.append((doc, 0.5))
        return {"level": 4, "results": path_hits[:top_k]}

    def recall_for_context(self, query: str, token_budget: int, top_k: int = 10) -> List[str]:
        results = self.search(query, top_k=top_k)
        recalled = []
        used = 0
        for doc, _ in results:
            tokens = self._est(doc.content)
            if used + tokens <= token_budget:
                recalled.append(doc.content)
                used += tokens
            else:
                remaining = token_budget - used
                if remaining > 50:
                    # MIMOX: 按 query 提取最相关 3 句，而非简单字节截断
                    snippet = self._extract_top_sentences(doc.content, query)
                    if snippet:
                        recalled.append(snippet)
                    else:
                        recalled.append(doc.content[: remaining * 2] + " [...]")
                break
        return recalled

    @staticmethod
    def _extract_top_sentences(content: str, query: str, top_k: int = 3) -> str:
        """Split content into sentences, rank by lexical overlap with query."""
        if len(content) < 500:
            return ""
        sentences = re.split(r"(?<=[。！？.!?\n])\s*", content)
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 10]
        if len(sentences) <= top_k:
            return " ".join(sentences)
        query_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", query.lower()))
        if not query_tokens:
            return " ".join(sentences[:top_k])
        scored = []
        for s in sentences:
            s_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", s.lower()))
            overlap = len(query_tokens & s_tokens)
            norm = len(query_tokens) or 1
            scored.append((overlap / norm, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return " ".join(s for _, s in scored[:top_k])

    def _mmr_rerank(self, query_vec, scored, top_k):
        """MMR (Maximal Marginal Relevance) 重排序，平衡相关性和多样性"""
        if len(scored) <= top_k:
            return scored
        # 复制列表避免原地修改
        remaining = scored.copy()
        selected = [remaining.pop(0)]
        while len(selected) < top_k and remaining:
            best_mmr, best_i = -float('inf'), 0
            for i, (cand_idx, rel) in enumerate(remaining):
                max_sim = 0
                cand_vec = self._embeddings[cand_idx] if cand_idx < len(self._embeddings) else []
                for sel_idx, _ in selected:
                    sel_vec = self._embeddings[sel_idx] if sel_idx < len(self._embeddings) else []
                    if cand_vec and sel_vec:
                        max_sim = max(max_sim, self._cosine(cand_vec, sel_vec))
                mmr = self.mmr_lambda * rel - (1 - self.mmr_lambda) * max_sim
                if mmr > best_mmr:
                    best_mmr, best_i = mmr, i
            selected.append(remaining.pop(best_i))
        return selected

    @staticmethod
    def _est(text):
        return estimate_tokens(text)
