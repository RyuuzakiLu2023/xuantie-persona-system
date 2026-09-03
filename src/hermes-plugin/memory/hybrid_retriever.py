# ================================================================
# hybrid_retriever.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：L4 混合检索器——BM25(3) + 向量(7) 加权、时间衰减、MMR 去重、
#           四级降级链 + 相关性阈值（AdaMEM 层）。XTMEM 召回的唯一打分引擎。
#
# 结构：MemoryDoc dataclass（记忆文档，含 tier/namespace/visibility）→ BM25 中文分词索引 +
#       _embeddings 向量 → index()/append() 建索引 → search()（tier_filter 先选池后算分，AF-3）→
#       search_two_tier()（P4.8 两阶段 refined 首跳/raw 兜底）。
#
# 修改铁律：
# 1. search() 的 BM25/向量/评分循环只在 valid_idx（tier 池）内算，别把过滤加在打分循环里（延迟翻倍）。
# 2. tier_filter=None 必须与改前逐字节一致（只在 tier_filter 非空时收敛池）。
# 3. 半衰期按 track 分层（P4.1 3.3）：profile=0 不衰减，用 half_life_by_track 查表；缺项回落全局。
# 4. 判据（_classify_ttl / 检索阈值）一律从 xtmem_tuning.json 读，别硬编码。
#
# 特殊机制：
# - _tier_index = {'raw':[...],'refined':[...]}：index()/append() 维护，两阶段首跳只算 refined 池。
# - _classify_ttl 流水豁免收口（T0）：流水不吃 track/保护符豁免，走 stream_ttl_seconds。
#
# 外号表：「tier 索引」= _tier_index；「两阶段」= search_two_tier。
# ================================================================
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
    # P0.3（2026-08-31）：检索每跳校验——带可见性/域，供 viewer 过滤
    namespace: str = ""
    visibility: str = "shared"
    # P4.8 T2（2026-09-01）：分层库 tier——raw=全量流水层 / refined=蒸馏+显式要点+画像预判层；缺省 raw
    tier: str = "raw"


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
    # T0（2026-08-31，P4 第 1 波）：流水豁免收口——对话流水不吃 track/保护符/important 豁免，
    # 一律按"过渡期 TTL"（默认 30 天，xtmem_tuning.json retriever.stream_ttl_seconds 可调）衰减。
    # 与 T6 蒸馏器配套：T0 加速淘汰流水，T6 负责在淘汰前把事实提炼进 refined 层。
    _tags_lower = [t.lower() for t in (tags or [])]
    _clower = (content or "").lower()
    _is_stream = (
        "conversation" in _tags_lower
        or ("hermes" in _tags_lower and _clower.startswith("user: "))
        or "会话" in [t for t in (tags or [])]
    )
    if _is_stream:
        try:
            from memory.tuning import load_tuning
            _stream_ttl = int(load_tuning().get("retriever", {}).get("stream_ttl_seconds", 30 * 86400))
        except Exception:
            _stream_ttl = 30 * 86400
        return _stream_ttl

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
        viewer_utility_weight: float = 0.05,  # P1.1：per-viewer utility 加权（灰度调权）
        relevance_threshold: float = 0.15,
        # MIMOX: 语义去重 — 工具输出相似度>threshold 只保留最新一条
        semantic_dedup_enabled: bool = True,
        semantic_dedup_threshold: float = 0.85,
    ):
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.recency_half_life = recency_half_life_hours
        # P3.1-a（2026-08-31）：旋钮外置——recency/importance 权重与半衰期从 xtmem_tuning.json 读，缺项用默认兜底
        try:
            from memory.tuning import load_tuning
            _r = load_tuning().get("retriever", {})
            self.recency_weight = float(_r.get("recency_weight", 0.1))
            self.importance_weight = float(_r.get("importance_weight", 0.05))
            self.recency_half_life = float(_r.get("recency_half_life_hours", self.recency_half_life))
            # P4.1 3.3（2026-09-01）：分层半衰期按 track；profile=0 永不衰减
            self.half_life_by_track = dict(_r.get("half_life_by_track", {}) or {})
            # P5.0-c（2026-09-02）：图传播重排权重——默认 0（行为与现状逐字节一致；影子期灰度先例见总 Spec P5.0）
            self.graph_prop_weight = float(_r.get("graph_prop_weight", 0.0))
            # P5.0-d（2026-09-02）：引力探针开关——默认 False（动态阈值不激活，行为与现状逐字节一致；灰度见 Spec）
            self.probe_enabled = bool(_r.get("probe_enabled", False))
        except Exception:
            self.recency_weight = 0.1
            self.importance_weight = 0.05
            self.half_life_by_track = {}
            self.graph_prop_weight = 0.0
            self.probe_enabled = False
        # P4.4 ⑱：记录两阶段路由实际用的路径（防谄媚/诚实输出用）
        self._last_two_tier_mode: Optional[str] = None
        self.mmr_lambda = mmr_lambda
        self.relevance_threshold = relevance_threshold
        self.viewer_utility_weight = viewer_utility_weight  # P1.1
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
        # P4.8 T2（2026-09-01）：tier 索引——第一跳只算 refined 池 cosine，防两跳全量遍历假优化（AF-3）
        self._tier_index: Dict[str, List[int]] = {"raw": [], "refined": []}
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

        ⚠️ 疑似死代码（2026-08-31 P2-12 确认）：当前生产调用链零引用，保留不删。

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
        # P4.8 T2：同步维护 tier 索引（hydrate 全量加载时建立；append 时增量补）
        self._tier_index = {"raw": [], "refined": []}
        for _i, _d in enumerate(docs):
            _t = (getattr(_d, "tier", "raw") or "raw")
            self._tier_index.setdefault(_t, []).append(_i)
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

    def search(self, query: str, top_k: int = 10, now: Optional[float] = None, viewer: Optional[str] = None, utility_lookup=None, last_access_lookup=None, tier_filter: Optional[str] = None, time_decay: Optional[bool] = None) -> List[Tuple[MemoryDoc, float]]:
        if not self.docs:
            return []
        import time as _time
        if now is None:
            now = _time.time()

        # P4.8 T2（2026-09-01）：tier_filter 只对指定 tier 池算分（AF-3：第一跳 refined 池远小于全量；
        #   不把过滤加在打分循环里——那样 cosine 已算完，两跳=两次全量遍历，延迟翻倍零收益）
        if tier_filter:
            valid_idx = set(self._tier_index.get(tier_filter, []))
            if not valid_idx:
                return []
        else:
            valid_idx = None

        # BM25 分数（归一化，仅 tier 池内）
        bm25_raw = self.bm25.search(query, top_k=top_k * 3)
        if valid_idx is not None:
            bm25_raw = [(idx, s) for idx, s in bm25_raw if idx in valid_idx]
        max_bm25 = max(s for _, s in bm25_raw) if bm25_raw else 1
        bm25_scores = {idx: score/max_bm25 for idx, score in bm25_raw}

        # 向量分数（仅 tier 池内）
        query_vec = self._embed(query)
        vector_scores = {}
        if valid_idx is not None:
            _loop = [i for i in valid_idx if i < len(self._embeddings)]
        else:
            _loop = list(range(len(self._embeddings)))
        for i in _loop:
            sim = self._cosine(query_vec, self._embeddings[i])
            if sim > 0:
                vector_scores[i] = sim

        # P5.0-d（2026-09-02）：引力探针——默认关（probe_enabled=False 时阈值=现状值，逐字节一致）。
        # 开启时按候选向量分布 L/S 动态算阈值（公式 + clamp 见 memory/probe.py docstring）。
        _probe_th = self.relevance_threshold
        if self.probe_enabled and len(vector_scores) >= 2:
            try:
                from memory.probe import dynamic_threshold, probe_entropy
                _p = probe_entropy([self._embeddings[i] for i in vector_scores])
                _probe_th = dynamic_threshold(_p["L"], _p["S"])
            except Exception:
                logger.debug("probe skipped (non-fatal)", exc_info=True)

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
            # Phase 1 R9: superseded / archived 条目不参与检索（P0-5）
            if getattr(doc, 'state', 'active') in ("superseded", "archived"):
                continue
            # P1.2（2026-08-31）：视角衰减钟——recency 按本 viewer 的 last_access 计时（A 召回不重置 B 的钟）
            _la = last_access_lookup(doc.id) if last_access_lookup else None
            age_base = _la or doc.timestamp
            age_hours = (now - age_base) / 3600
            # P4.1 3.3：分层半衰期按 track；profile=0 永不衰减
            # P5.0-e（2026-09-02）：--time-decay off = 半衰期按视角关闭（recency 项清零，纯相关性+重要性）；默认 None = 现状逐字节一致
            _hl = self.half_life_by_track.get(getattr(doc, "track", "default"))
            if time_decay is False:
                recency = 0.0
            elif _hl == 0:
                recency = 1.0
            elif _hl and _hl > 0:
                recency = math.exp(-age_hours / _hl)
            else:
                recency = math.exp(-age_hours / self.recency_half_life)
            final = (bm + vec) * ttl_decay + recency * self.recency_weight + doc.importance * self.importance_weight
            # P1.1（2026-08-31）：per-viewer utility 加权项——只提升该 viewer 自己的视角
            if utility_lookup is not None:
                final += utility_lookup(doc.id) * self.viewer_utility_weight
            # AdaMEM层：低于相关性阈值的结果直接丢弃
            # P4.9-d（2026-09-02）锚通道：importance>=0.75 且 BM25 命中 → 免 relevance_threshold 否决（第一版双条件）
            _anchor = (raw_bm > 0) and (doc.importance >= 0.75)
            if final >= _probe_th or _anchor:
                scored.append((idx, final))
        # P0.3（2026-08-31）：检索每跳校验——在 top-k/MMR 之前按 viewer 过滤可见性
        if viewer:
            from .visibility import visibility_ok
            scored = [(idx, s) for idx, s in scored
                      if visibility_ok(getattr(self.docs[idx], "visibility", "shared"),
                                       getattr(self.docs[idx], "namespace", ""), viewer)]
        scored.sort(key=lambda x: x[1], reverse=True)

        # P5.0-c（2026-09-02）：图传播重排——传播分插在 MMR 之前（总 Spec 落点 hybrid_retriever.py:446-448 之间）。
        # graph_prop_weight=0（默认）跳过整段，行为与现状逐字节一致；>0 时 B_G 非负只加分不扣分。
        if self.graph_prop_weight > 0 and scored and query:
            try:
                from memory.graph import doc_boosts
                _boosts = doc_boosts(query, [self.docs[idx].id for idx, _ in scored])
                scored = [(idx, s + self.graph_prop_weight * _boosts.get(self.docs[idx].id, 0.0))
                          for idx, s in scored]
                scored.sort(key=lambda x: x[1], reverse=True)
            except Exception:
                logger.debug("graph propagation skipped (non-fatal)", exc_info=True)

        # MMR 去重
        selected = self._mmr_rerank(query_vec, scored, top_k)

        # 追踪引用计数（供定期清理使用）
        for idx, _ in selected:
            doc = self.docs[idx]
            self._recall_counts[doc.id] = self._recall_counts.get(doc.id, 0) + 1

        return [(self.docs[idx], score) for idx, score in selected]

    def search_two_tier(self, query: str, top_k: int = 6, now: Optional[float] = None, viewer: Optional[str] = None, utility_lookup=None, last_access_lookup=None, time_decay: Optional[bool] = None) -> List[Tuple[MemoryDoc, float]]:
        """P4.8 T4（2026-09-01）：两阶段检索路由——refined 首跳高分即返，miss 回落 raw 兜底。

        实现：
          第一阶段 search(tier_filter='refined')（refined 池远小于全量，AF-3 真优化）；
          任一 score >= refined_hit_threshold（tuning，默认 0.6）→ 直接返回 refined 命中；
          否则第二阶段 search(tier_filter='raw') 兜底。
        去重：refined 与 raw 是两个不相交 tier 池（一个 chunk 非 refined 即 raw），
          故两跳结果 id 天然不重复（T4 DoD：两跳结果无重复 id）。

        :param refined_hit_threshold_ref: 由 tuning.retriever.refined_hit_threshold 决定（默认 0.6）；见调用方。
        """
        try:
            from memory.tuning import load_tuning
            _r = load_tuning().get("retriever", {})
            _ref_top = int(_r.get("refined_top", 5))
            _th = float(_r.get("refined_hit_threshold", 0.6))
        except Exception:
            _ref_top, _th = 5, 0.6
        # 第一跳：refined
        refined = self.search(query, top_k=_ref_top, now=now, viewer=viewer,
                              utility_lookup=utility_lookup, last_access_lookup=last_access_lookup,
                              tier_filter="refined", time_decay=time_decay)
        hits = [(d, s) for d, s in refined if s >= _th]
        if hits:
            hits.sort(key=lambda x: x[1], reverse=True)
            self._last_two_tier_mode = "refined-first"  # P4.4 ⑱
            return hits[:top_k]
        # 第二跳：raw 兜底（与 refined 不相交，天然去重）
        self._last_two_tier_mode = "raw-fallback"
        return self.search(query, top_k=top_k, now=now, viewer=viewer,
                           utility_lookup=utility_lookup, last_access_lookup=last_access_lookup,
                           tier_filter="raw", time_decay=time_decay)

    def get_recall_stats(self) -> Dict[str, int]:
        """返回所有文档的引用计数并清理，供定期清理判断冷记忆"""
        result = dict(self._recall_counts)
        self._recall_counts.clear()
        return result

    def recall_with_fallback(self, query: str, token_budget: int, top_k: int = 10) -> dict:
        """
        ⚠️ 疑似死代码（2026-08-31 P2-12 确认）：当前生产调用链零引用，保留不删。
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
                and getattr(self.docs[idx], 'state', 'active') not in ("superseded", "archived")
            ]
            return {"level": 2, "results": [(self.docs[idx], s) for idx, s in scored]}

        # L3: 正则 FTS 全文扫描
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        fts_hits: List[Tuple[MemoryDoc, float]] = []
        for doc in self.docs:
            # 与 L1 一致：refuted / superseded / archived 都不参与检索（P0-5）
            if doc.status == "refuted" or getattr(doc, 'state', 'active') in ("superseded", "archived"):
                continue
            if pattern.search(doc.content):
                fts_hits.append((doc, 1.0))
        if fts_hits:
            return {"level": 3, "results": fts_hits[:top_k]}

        # L4: 文件名/路径匹配
        path_hits: List[Tuple[MemoryDoc, float]] = []
        q_lower = query.lower()
        for doc in self.docs:
            # 与 L1 一致：refuted / superseded / archived 都不参与检索（P0-5）
            if doc.status == "refuted" or getattr(doc, 'state', 'active') in ("superseded", "archived"):
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
