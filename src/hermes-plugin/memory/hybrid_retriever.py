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
# 外号表：「tier 索引」= _tier_index；「两阶段」= search_two_tier；「锁芯」= embedding_version 闸
# ================================================================
# *更新：2026-09-04 · 贰③c embedding_version 锁芯——_vector_leg_ok 闸（search 向量腿 fail-closed
#   退 BM25-only）+ close/reopen_vector_leg + EMBEDDING_META_KEY/make_embedding_fingerprint*
# *更新：2026-10-12 · 常驻书架缓存文档范数与噪声判据（内容/配置/增删改失效）；
#   FTS5 on 跳过立即被覆盖的内存 BM25；评分、阈值与可见性规则不变。*
# *更新：2026-10-12 · O01 记录层路由与后端降级，raw兜底不当故障；查询只落哈希。*
import math, re, hashlib, logging, threading, time as _time
import os
import json
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from memory.vector_store import VectorStore, fts_search_bm25  # G2：FTS5 腿只读复用（SQL 单点）
from collections import Counter
from .utils import estimate_tokens
from .diagnostics import diagnosed, emit, fingerprint

# P1-L / G-01：tier 白名单——与 vector_store.TIERS 同语义（防两处漂移在此留锚点注释）
TIERS = frozenset({"raw", "refined"})

# O5（P6 波3，2026-09-05）：refined 首跳派生白名单——derived_from 在此集合内的 refined
# 才进首跳；集合外（wiki-entity/timeline/prediction-engine/...）整页排除，只能当 raw 源。
# B0-4（2026-09-11）：补 "distill-judge"。Gen2 LLM 蒸馏写 derived_from='distill-judge'
#   （xtmem_distill_llm.py:318,335），318 条核心事实；白名单写于 09-05、当天 18:31 首批
#   distill-judge 才落库，此后没同步 → 这 318 条在两阶段路由里【完全不可达】：refined
#   首跳被此集合排除，raw 回落也进不来（它们 tier=refined，不在 raw 池）。
#   ⚠️ 守卫 tests/test_xtmem_o5_firsthop.py 用 == 断言精确集合，改此处须同步更新（预期内红）。
# '' 与 NULL（手动 store 主体，4021 行空串实锤）必须放行——白名单检查写
#   df in FIRST_HOP_ALLOWED or not df（等价 IS NULL OR =''，V11 全库零 NULL 口径）。
FIRST_HOP_ALLOWED = frozenset({"distill", "profile-aggregator", "diary-distill", "distill-judge"})

# ================================================================
# embedding_version 锁芯（贰③c，v1.3.2 工单增补，2026-09-04 小D）
# ================================================================
# 指纹格式：<model>@<dim>（如 bge-m3@1024）。生产/口径只认这个字符串的【组成】，
# 不在常量里写死具体值——值由调用方（context_builder）按实际 embedding provider 现算。
# 与 tokenizer_version 闸（vector_store._tokenizer_version_ok）同范式：各自校验各自降级。
EMBEDDING_META_KEY = "embedding_version"


class EmbeddingVersionMismatch(Exception):
    """xtmem_meta.embedding_version 与当前 provider 指纹不一致时抛出（fail-closed 信号）。

    【不】当通用异常用——只表达"向量腿必须停"这一件事；调用方捕获后转 BM25-only
    并写 embedding_version_mismatch 事件，而不是让检索整体崩掉。
    """


def make_embedding_fingerprint(model: str, dim: int) -> str:
    """构造 embedding_version 键值指纹：<model>@<dim>（纯函数，供生产方/测试共用口径）。"""
    return f"{model}@{int(dim)}"

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
    # ★R01-FIX1（2026-10-21，独立验收 F2-B）：project 进文档模型。
    #   原 MemoryDoc 只有 namespace/visibility，没有 project —— 于是 CLI 传下来的
    #   --project 在引擎侧**根本没地方比对**，只能等拿回 top_k 之后再在 Python 里筛，
    #   那正是「先占名额、后查范围」的病根（独立验收实测 --project right 仍返回 wrong）。
    #   ⚠️ 缺省空串 = 「未声明项目」，与 DB 里 project='' 的行同义，不参与过滤。
    project: str = ""
    visibility: str = "shared"
    # P4.8 T2（2026-09-01）：分层库 tier——raw=全量流水层 / refined=蒸馏+显式要点+画像预判层；缺省 raw
    tier: str = "raw"
    # O5（P6 波3，2026-09-05）：血缘派生随 doc 进内存——refined 首跳白名单据此过滤。
    #   白名单 = {distill, profile-aggregator, diary-distill, ''(手动)}；wiki-entity/timeline/
    #   prediction-engine 等机器派生大页排除，防机器产物压过真实人工蒸馏/手记。
    derived_from: str = ""


class _AclTruthUnavailable(Exception):
    """★F3（2026-10-14 第二轮返修）：**配置了权威库**却查不到权限真值。

    与"根本没配库（纯内存用法）"必须分开：
      · 没配库 = 构造时就确定的**模式**，调用方允许退回快照过滤；
      · 配了库但连接失败/查询异常 = **运行期故障**，必须 fail-closed 拒绝候选，
        绝不能因为"查不到权限"就把旧 shared 授权当有效（独立验收实测的正是这条）。
    ⚠️【不】把本异常静默吞掉改回"退回快照"：那正是 F3 的病灶形态。
    """


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


def _classify_ttl(content: str, tags: list = None, track: str = "default",
                  tier: str = "raw", derived_from: str = "") -> int:
    """根据内容分类返回 TTL 秒数。0 表示永不衰减。

    D0-2（2026-09-09 SupportBundle Spec v1.2，小D）：五类永不过期豁免（返回 0）：
    1. tier == 'refined'（全体豁免，找回 246 条隐身蒸馏事实）
    2. derived_from != ''（一切派生产物：timeline/profile/distill/wiki）
    3. track ∈ {identity, preference, procedural, checkpoint, explicit, timeline, profile}
    4. 符号保护：★/🔒/❗/⚠️
    5. 标签保护：important/永久/关键/core

    收窄规则：
    - 临时/中间/草稿/Traceback 等 24h/1h 规则仅在 tier='raw' AND derived_from='' 时生效；
    - tags 含玄铁误判或正文含错误·已纠正的原料条目本批不提前豁免，保持现 TTL 不动；
    - 对话流水 30 天，emotion 5 天，默认 7 天。
    """
    tier_norm = (tier or "raw").lower()
    der_norm = (derived_from or "").strip()
    trk_norm = (track or "default").lower()

    # D0-2 豁免一：refined 层全体豁免
    if tier_norm == "refined":
        return 0

    # D0-2 豁免二：一切派生产物豁免
    if der_norm:
        return 0

    # D0-2 豁免三：免衰减分轨
    if trk_norm in ("identity", "preference", "procedural", "checkpoint", "explicit", "timeline", "profile"):
        return 0

    _tags_lower = [t.lower() for t in (tags or [])]
    _clower = (content or "").lower()

    # D0-2 豁免四：保护符号
    if any(sym in (content or "") for sym in ("★", "🔒", "❗", "⚠️")):
        return 0

    # D0-2 豁免五：关键决策 metadata
    if any(t in _tags_lower for t in ("important", "永久", "关键", "core")):
        return 0

    # T0 流水过渡期（仅 raw 且非派生）：对话流水 30 天
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

    if trk_norm == "emotion":
        return 432000  # 5 天

    # 调试日志：含 Traceback/error（仅 raw 且非派生生效）
    if any(kw in _clower for kw in ("traceback", "error:", "exception:", "failed:", "errno")):
        return _TTL_DEBUG_LOG

    # 中间步骤：含 临时/中间/草稿（仅 raw 且非派生生效）
    if any(kw in _clower for kw in ("中间", "临时", "草稿", "wip", "temporary", "intermediate")):
        return _TTL_INTERMEDIATE

    return _TTL_DEFAULT


# ════════════════════════════════════════════════════════════════════
# R01 · RQ02/RQ04（2026-10-21 小D）：「这条候选到底有没有字面证据」
#
# 为什么单独抽一个函数，而不是继续用 raw_bm > 0：
#   BM25._tokenize 对中文同时产出【单字】与 bigram（range(len(cn_chars)-1)）。
#   于是"任何常见汉字"都能让一条无关文档得到正分：
#     实测「ZXQ731920 冥王星奶酪关税征收标准」对一条 C03 记录的 bm_norm=0.2877，
#     唯一重叠是单字「关」——可它连"关税"这个词都没有。
#   单字命中不是证据，是噪声。判据必须落在【有内容的词】上。
#
# 判据（任一成立即算有字面证据）：
#   ① 查询里的 ASCII/数字 token（>=3 字符，且不是通用词）整体出现在正文里
#      —— C03 / lowres_scale / ZXQ731920 这类标识符；
#   ② 查询里的中文词（>=2 字）出现在正文里 —— 用同一个精确模式分词，
#      单字词直接丢弃（"的""了""关"永远不算）。
#
# ⚠️【不】用相似度/编辑距离近似匹配：那会退化成"看起来像就算"，
#   正是本条要防的形态（同 RQ02 的"相对最高分凑数"）。
# ⚠️【不】让这个函数查库/查频次：它在检索热路径上，纯字符串比较。
# ════════════════════════════════════════════════════════════════════

def _query_content_terms(query: str):
    """抽出查询里"有内容"的词元：中文词>=2字 + ASCII/数字>=3字符，去掉通用词。"""
    q = (query or "").strip()
    if not q:
        return []
    try:
        from memory.bundle_selector import _is_generic_anchor
    except Exception:
        def _is_generic_anchor(_t):
            return False
    terms = []
    seen = set()
    # ① ASCII / 数字标识符
    for m in re.finditer(r'[A-Za-z0-9][A-Za-z0-9._-]{2,}', q):
        tok = m.group(0).strip('._-')
        if not tok or _is_generic_anchor(tok):
            continue
        if tok.lower() not in seen:
            seen.add(tok.lower())
            terms.append(tok)
    # ② 中文词（>=2 字）：与 FTS 同一分词口径，单字一律不算
    try:
        import jieba
        words = [w for w in jieba.cut(q, cut_all=False) if len(w) >= 2]
    except Exception:
        # 无 jieba 时退回 CJK bigram——同样是"多字"粒度的证据
        cn = [c for c in q if '\u4e00' <= c <= '\u9fff']
        words = [''.join(cn[i:i+2]) for i in range(len(cn) - 1)]
    for w in words:
        if not w.strip() or w.lower() in seen:
            continue
        if _is_generic_anchor(w):
            continue
        seen.add(w.lower())
        terms.append(w)
    return terms


def _has_literal_evidence(query: str, content: str, terms=None) -> bool:
    """正文里是否真的出现了查询的内容词（>=1 个多字/标识符词）。"""
    body = (content or "").lower()
    if not body:
        return False
    terms = terms if terms is not None else _query_content_terms(query)
    return any(t.lower() in body for t in terms)


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
        # G2（2026-09-04）FTS5 热接线：db_path 供 fts 只读查询；fts5_mode = off/shadow/on。
        # None → 从 xtmem_tuning.json retriever.fts5_mode 读（缺省 off）；与 P5.0 旋钮同机制——改 json 需重建实例。
        db_path: Optional[str] = None,
        fts5_mode: Optional[str] = None,
        # ★G1（2026-10-14 第五轮）：**权限真值的来源模式**，显式二选一：
        #   "authoritative"（缺省）= 有权威库，库不可用即故障 ⇒ fail-closed；
        #   "memory"          = 显式纯内存：不查权威库，退回快照过滤。
        # ⚠️ 这是**唯一**决定模式的入口；运行期不许再按 exists() 推断（见 __init__）。
        acl_mode: Optional[str] = None,
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
            # D3-1（2026-09-10 派单工单）：噪声切片召回降权——默认 enabled=false 时
            # self.noise_recall_weight=1.0 且不打分支，行为与现状逐字节一致（灰度假例同 P5.0）。
            # ⚠️ 判据单一源：这里只取【系数】，是否噪声统一问 memory.noise_filter（与建图期同一函数）。
            # 🔴 系数铁律：只能取 (0,1]，永不设 0——见 noise_filter.get_recall_weight 的说明。
            # 🔧 开闸分法（2026-09-10 Z指导实测定案）：系数从「单一标量」改为【按规则逐条取】——
            #   R1/R2=0.3 参与降权；R3=null 表示【跳过，final 不动】（专有词密集≠机器生成：
            #   实测用户贴配置/JSON 被 R3 命中 138 条真对话，召回必须放过）。
            #   null ≠ 0：0 会被 get_recall_weight 硬拒绝并回落 1.0（失去兜底），null 是合法语义。
            try:
                from memory.noise_filter import (
                    get_recall_weight, _cfg as _noise_cfg, RULES as _NOISE_RULES,
                )
                self._noise_enabled = bool(_noise_cfg().get("enabled"))
                self.noise_rule_weights = (
                    {r: get_recall_weight(r) for r in _NOISE_RULES} if self._noise_enabled else {}
                )
                # legacy 单系数：老调用方（含既有守卫）读它；enabled=false 时必须为 1.0
                self.noise_recall_weight = (get_recall_weight() if self._noise_enabled else 1.0)
                # ⚠️ R3 的召回降权【按设计不实现】：召回期的文本判据（is_noise_text）只覆盖 R1/R2，
                #   R3 是实体密度——要查库算域阈值，属建图期的活。若有人给 R3 配了非 null 系数，
                #   那是配置误用（R3 只该是 null），如实告警而不是假装生效。
                _r3w = self.noise_rule_weights.get("R3")
                if _r3w is not None:
                    logger.warning(
                        "noise_filter.recall_weight.R3=%r 被忽略：R3（实体密度）按设计只参与建图期"
                        "排除，召回期不做密度查询。要表达「R3 不降权」请写 null。", _r3w
                    )
            except Exception:
                self._noise_enabled = False
                self.noise_rule_weights = {}
                self.noise_recall_weight = 1.0
            # B0-2（2026-09-11）：相关性【相对】门限（加层限幅，不替换原 0.15 绝对常量）。
            # 病根：relevance_threshold=0.15 对 bge-m3 的 cosine 分布几乎恒真（顶模实测
            # 3947/3953 全过阈），"相关性过滤"名存实亡，全量候选进下游 MMR。
            # 修法：在绝对阈值之上【再加】一道相对闸——低于「本批候选 top-1 分的
            # relative_gate_ratio 倍」的候选同样丢弃。两道闸取并集过滤（低于任一即丢）。
            # 原 0.15 常量原样保留（它是所有调用方的公共默认，直接改常量会让没接 tuning
            # 的路径一夜变行为，风险不对等）。锚通道豁免（importance>=0.75 且 BM25 命中）不变。
            self.relative_gate_enabled = bool(_r.get("relative_gate_enabled", True))
            self.relative_gate_ratio = float(_r.get("relative_gate_ratio", 0.6))
            # R01·RQ03（2026-10-21）：锚通道的"相关性附条件"系数——见 _search_locked 的准入段。
            # 0.5 = 锚点候选的纯相关性至少要过 relevance_threshold 的一半。
            self.anchor_admission_ratio = float(_r.get("anchor_admission_ratio", 0.5))
            # R01·RQ02（2026-10-21）：语义充分性下限——本条没有任何词法命中时，
            #   余弦要够到这个数才算"有证据"。⚠️ 初值，须在固定正反例集上校准。
            self.vector_admission_min = float(_r.get("vector_admission_min", 0.5))
            # R01·RQ06（2026-10-21）：单域过滤时的域内名额比例（配额，不是加分）。
            #   0 = 关闭（回到纯分数排序）；0.5 = 一半名额留给请求域。
            self.domain_quota_ratio = float(_r.get("domain_quota_ratio", 0.5))
            # B0-1（2026-09-11）：MMR 候选池上限。原 _mmr_rerank 在全量候选上跑，
            # 复杂度 O(top_k² × N)——外循环 top_k 次 × 内循环遍历 remaining × 对每个
            # selected 逐对调 _cosine（纯 Python）。N 是过阈后全量（B0-2 未修时≈全库），
            # 实测是查询延迟主因（非 embedding）。修法：先按相关性取前 M 再 MMR。
            self.mmr_candidate_pool = int(_r.get("mmr_candidate_pool", 40))
        except Exception:
            self.recency_weight = 0.1
            self.importance_weight = 0.05
            self.half_life_by_track = {}
            self.graph_prop_weight = 0.0
            self.probe_enabled = False
            self._noise_enabled = False
            self.noise_rule_weights = {}
            self.noise_recall_weight = 1.0
            self.relative_gate_enabled = True
            self.relative_gate_ratio = 0.6
            self.anchor_admission_ratio = 0.5
            self.vector_admission_min = 0.5
            self.domain_quota_ratio = 0.5
            self.mmr_candidate_pool = 40
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
        # Derived document data lives alongside the resident shelf. Embeddings
        # are replaced through index/append/patch; those paths refresh norms.
        self._vector_norms: dict = {}
        self._query_norms: dict = {}
        self._noise_cache: dict = {}
        self._noise_cache_signature = None
        # 追踪检索统计（供定期清理使用）
        self._recall_counts: Dict[str, int] = {}  # doc_id -> recall_count
        # TTL 过期统计
        self._expired_count: int = 0
        # P4.8 T2（2026-09-01）：tier 索引——第一跳只算 refined 池 cosine，防两跳全量遍历假优化（AF-3）
        self._tier_index: Dict[str, List[int]] = {"raw": [], "refined": []}
        # G2：id→docs 下标映射（index() 同步维护；FTS5 腿返回 chunk_id 后反查）
        self._id_to_idx: Dict[str, int] = {}
        # S1（B方案，2026-09-06）：改用 RLock，允许 patch/append/remove 与 search 重入
        self._lock = threading.RLock()
        # G2：FTS5 腿——只读库路径 + 模式（off/shadow/on）
        self._fts_db_path = db_path or os.environ.get("XTMEM_DB_PATH", "") or VectorStore.DEFAULT_DB_PATH
        # ★★G1（2026-10-14 第五轮返修）：**权限真值的来源模式由显式配置唯一决定**。
        #
        #   演进史（前两版都被独立验收打回，别再退回其中任何一种）：
        #     · 第一版：`_acl_truth` 里 `not db or not os.path.exists(db)` 合成一个分支
        #       返回 None ⇒ 任何一次 exists() 为假都自动降级成"不校验权限"（fail-open）。
        #       【独立验收 v3 打回：F3-configured-db-file-disappearance-denies-cache】
        #     · 第二版：把 exists() 从每次查询挪到**构造期**，判"从未就绪 = 纯内存兼容"。
        #       ⇒ 仍分不清「调用方**明确**要纯内存」与「权威库**启动时缺失/路径不可访问**」；
        #         库不可用时**重建实例**又变回 fail-open。
        #       【独立验收 v4 打回：G1-configured-db-missing-during-instance-reconstruction、
        #         G1-explicitly-configured-db-unavailable-at-start】
        #   ⇒ 现在：模式**只**能由显式配置表达，运行期不做任何推断：
        #     · acl_mode="memory"  → 显式纯内存：不查权威库，退回快照过滤；
        #     · acl_mode 缺省/其它 → **authoritative**：只要配了库路径
        #                            （显式 db_path / XTMEM_DB_PATH / 默认库），
        #                            库缺失、搬迁、不可访问、查询异常**一律**是故障
        #                            ⇒ fail-closed 拒绝整批候选。
        #   ⚠️【不】再用 exists() 或"曾经就绪"推断模式——那正是被驳回的形态。
        #   ⚠️【不】按测试路径名放行（验收方明令："不得靠识别测试路径名字放行"）。
        #   ⚠️ 既有两份单测（test_recall_wiring / test_recall_performance）过去靠传
        #      **从不存在的假路径**来暗示纯内存，那只是旧夹具约定；本轮已让它们
        #      **显式**声明 acl_mode="memory"，而不是靠路径名字被放行。
        _acl_mode = (acl_mode or "").strip().lower()
        if _acl_mode not in ("authoritative", "memory"):
            _acl_mode = "authoritative"
        self._acl_mode = _acl_mode
        self._acl_authoritative = (_acl_mode == "authoritative") and bool(self._fts_db_path)
        _m = (fts5_mode or "").strip().lower()
        if _m not in ("off", "shadow", "on"):
            try:
                from memory.tuning import load_tuning
                _m = str(load_tuning().get("retriever", {}).get("fts5_mode", "off") or "off").lower()
            except Exception:
                _m = "off"
        self._fts5_mode = _m if _m in ("off", "shadow", "on") else "off"
        # 件②（2026-09-13 顶模"第一刀"）：FTS5 腿降级记账——进腿 query 总数 + 降级次数。
        #   只在本进程内存累加；跨进程健康口径走 xtmem_events 事件表（kind=fts5_fallback）。
        #   V1.5 错题本 #27 教训：进程内存跨进程读不到，health 的唯一通路是 DB。
        self._fts_query_count = 0
        self._fts_fallback_count = 0
        # 贰③c（2026-09-04）：embedding_version 锁芯——向量腿通断状态。
        # True=向量腿正常（默认，现状逐字节一致）；False=键不匹配 fail-closed，search() 只走 BM25。
        # 断闸只由 context_builder._hydrate_retriever() 在启动校验时执行（唯一合法入口），检索侧只消费。
        self._vector_leg_ok: bool = True
        self._vector_leg_reason: str = ""

    def _embed(self, text: str) -> List[float]:
        """优先使用 embedding_provider（Ollama/Gemini/OpenAI），fallback SimpleEmbedder

        P1-D / R-08：一律走 embed_single（单条接口），不再把 str 传给 embed()（批量接口）。
        """
        if self.embedding_provider:
            try:
                return self.embedding_provider.embed_single(text)
            except Exception as exc:
                from .diagnostics import emit
                emit('retriever', 'retrieval.embedding', 'degraded', error=exc,
                     fallback_reason='hash_embedding', retrieval_backend='hash_embedding')
        return self.embedder.embed(text)

    def _cosine(self, a: List[float], b: List[float]) -> float:
        """优先使用 embedding_provider 的 cosine，fallback SimpleEmbedder。

        B0-10（2026-09-11）：**维度不等直接返回 0**。原实现无条件下沉到
        SimpleEmbedder.cosine，而它是 `n = min(len(a), len(b))` —— 拿 1024 维向量的
        【前 128 维】去和 128 维哈希向量算相似度，得到的是**非零垃圾**而非 0。
        触发路径：Ollama 不可达 → _embed 回落 SimpleEmbedder.embed（128 维）→
        store 时仍以 model="bge-m3" 标签写进 embedding_cache（context_builder.py:524-529）。
        库里已实锤 1 行 dim=128。
        """
        if not a or not b or len(a) != len(b):
            return 0.0
        if self.embedding_provider is not None and hasattr(self.embedding_provider, 'cosine'):
            return self.embedding_provider.cosine(a, b)
        na, nb = self._vector_norm(a), self._vector_norm(b)
        if not (na > 0 and nb > 0):
            return 0.0
        return sum(x * y for x, y in zip(a, b)) / (na * nb)

    @staticmethod
    def _compute_vector_norm(vector):
        return math.sqrt(sum(x * x for x in vector))

    def _vector_norm(self, vector):
        key = id(vector)
        cached = self._vector_norms.get(key)
        if cached is not None and cached[0] is vector:
            return cached[1]
        # query_vec is a public mutable input; the same list may be edited
        # between searches. Document vectors use the shelf mutation APIs.
        fingerprint = tuple(vector)
        cached = self._query_norms.get(key)
        if cached is not None and cached[0] is vector and cached[2] == fingerprint:
            return cached[1]
        norm = self._compute_vector_norm(vector)
        # Only transient query vectors go here. Hold their references to avoid
        # object-id reuse, with a strict bound independent of conversation size.
        if len(self._query_norms) >= 2:
            self._query_norms.pop(next(iter(self._query_norms)))
        self._query_norms[key] = (vector, norm, fingerprint)
        return norm

    def _prepare_noise_cache(self):
        from memory.noise_filter import _cfg, is_noise_text

        patterns = _cfg()
        signature = (id(is_noise_text), json.dumps(patterns, sort_keys=True))
        if signature != self._noise_cache_signature:
            self._noise_cache.clear()
            self._noise_cache_signature = signature
        return is_noise_text, patterns

    def _cached_noise_result(self, doc, classifier, patterns):
        cached = self._noise_cache.get(doc.id)
        if cached is not None and cached[0] == doc.content:
            return cached[1]
        result = classifier(doc.content, patterns=patterns)
        self._noise_cache[doc.id] = (doc.content, result)
        return result

    def _prime_search_caches(self):
        """Prepare query-independent work during hydration, once per shelf."""
        self._vector_norms = {
            id(vector): (vector, self._compute_vector_norm(vector))
            for vector in self._embeddings
        }
        self._query_norms.clear()
        self._noise_cache.clear()
        self._noise_cache_signature = None
        if self._noise_enabled:
            try:
                classifier, patterns = self._prepare_noise_cache()
                for doc in self.docs:
                    if doc is not None:
                        self._cached_noise_result(doc, classifier, patterns)
            except Exception:
                logger.debug("noise cache warmup skipped", exc_info=True)

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
            if doc is None:
                continue
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
        self._id_to_idx = {d.id: _i for _i, d in enumerate(self.docs)}
        self._embeddings = kept_vecs
        self._prime_search_caches()
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
        # G2：id→idx 映射（FTS5 腿 chunk_id 反查 docs 下标；index 全量时重建）
        self._id_to_idx = {d.id: _i for _i, d in enumerate(docs)}
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
        self._prime_search_caches()

    # ================================================================
    # S1（B 方案常驻书架，2026-09-06）：增量维护接口（纯新增，先行）
    # ================================================================

    def append(self, doc: MemoryDoc, embedding: Optional[List[float]] = None) -> None:
        """增量插入单条：docs/_id_to_idx/_tier_index/_embeddings 四处同步追加 + bm25.add()。
        embedding 为 None 时现算 _embed()。与 search 并发安全（内部 RLock）。"""
        with self._lock:
            if doc.id in self._id_to_idx:
                self.remove(doc.id)
            idx = len(self.docs)
            self.docs.append(doc)
            self._id_to_idx[doc.id] = idx
            tier = getattr(doc, "tier", "raw") or "raw"
            self._tier_index.setdefault(tier, []).append(idx)
            self.bm25.add(doc.content)
            if embedding is not None:
                self._embeddings.append(embedding)
            else:
                try:
                    vec = self._embed(doc.content)
                    self._embeddings.append(vec)
                except Exception:
                    logger.debug("append: embedding failed, appending dummy zeros", exc_info=True)
                    dim = len(self._embeddings[0]) if self._embeddings else 1024
                    self._embeddings.append([0.0] * dim)
            vector = self._embeddings[-1]
            self._vector_norms[id(vector)] = (vector, self._compute_vector_norm(vector))
            if self._noise_enabled:
                try:
                    self._cached_noise_result(doc, *self._prepare_noise_cache())
                except Exception:
                    logger.debug("noise cache append skipped", exc_info=True)

    def remove(self, doc_id: str) -> bool:
        """按 id 剔除（supersede 场景）：docs 置 None 哨兵 + 三索引同步摘除；
        不做列表压缩（避免检索途中索引位移），哨兵在打分循环里跳过。"""
        with self._lock:
            idx = self._id_to_idx.pop(doc_id, None)
            if idx is None or idx >= len(self.docs):
                return False
            doc = self.docs[idx]
            if doc is None:
                return False
            tier = getattr(doc, "tier", "raw") or "raw"
            if tier in self._tier_index:
                try:
                    self._tier_index[tier].remove(idx)
                except ValueError:
                    pass
            self.docs[idx] = None
            self._noise_cache.pop(doc_id, None)
            if idx < len(self._embeddings):
                self._vector_norms.pop(id(self._embeddings[idx]), None)
            return True

    def patch(self, doc: MemoryDoc, embedding: Optional[List[float]] = None) -> None:
        """updated_at 变化的条目：等价 remove + append。"""
        with self._lock:
            self.remove(doc.id)
            self.append(doc, embedding=embedding)

    def _fts_db(self):
        """G2：FTS5 只读连接（独立 ro 连接，不共享 VectorStore 写连接；查不到库返回 None）。"""
        import sqlite3
        db = self._fts_db_path
        if not db or not os.path.exists(str(db)):
            return None
        from memory.open_db import open_db
        con = open_db(str(db), ro=True)
        con.row_factory = sqlite3.Row
        return con

    def _acl_truth(self, ids):
        """★R3-1（2026-10-14）：批量取**库中当下**的 {id: (state, namespace, visibility)}。

        _acl_truth()：契约
        入参：ids（chunk id 序列）
        返回：dict；查得到就是映射，**权威库不可用时返回 None**（= 无法回查）
        副作用：只读（独立 ro 连接，用完即关）

        ⚠️ 返回 None 与返回 {} 是**不同的意思**，调用方必须区别对待：
            None = "我查不到真值"（没配库/连不上）——调用方**必须 fail-closed**
            {}   = "查得到，但这些 id 一条都不在库里" → **全部淘汰**（幽灵封口）

        ★★F3（2026-10-14 第二轮返修）：**生产配置了权威库时，回查失败必须拒绝候选**。
            病灶（独立验收实测）：正常以 shared 文档建索引 → DB 权限收紧为 dsh/private →
            注入真实 sqlite3.OperationalError 表示回查不可用 → 本函数返回 None →
            检索**跳过权限校验**、把旧缓存文档照样返回给 zcode。
            更早的版本把 None 当成"保持快照结果"的合法退路，理由是"兼容纯内存用法
            （单测不建库）"——但那条退路被**运行期异常**也能走到，
            等于"任何一个异常都自动降级成不校验权限"，这是 fail-open。
            ★★G1 第五轮（2026-10-14）：纯内存模式**必须显式声明**（acl_mode="memory"）。
              判据只剩一条，且完全由**显式配置**决定：
                · acl_mode="memory"（显式选择）→ 返回 None，调用方按无权威可查处理；
                · 其余（缺省 authoritative，含显式 db_path / XTMEM_DB_PATH / 默认库）→
                  库缺失、搬迁、不可访问、查询失败**一律**视为故障，
                  抛 _AclTruthUnavailable，由调用方 fail-closed 拒绝整批候选。
              ⚠️【不】再按 exists() 或"曾经就绪"推断模式（v4 驳回的正是这个）；
              ⚠️【不】按测试路径名放行。
            ⚠️ 纯内存不该由**异常**推断出来，只能由**配置**决定——这是 F3 的核心。
            ⚠️ 正常的 `_truth={}`（查得到但行不在库里）语义保持：全部淘汰。
        """
        if not ids:
            return {}
        import sqlite3
        db = self._fts_db_path
        # ★★G1（第五轮）：模式由显式配置唯一决定，运行期**不做任何推断**。
        #   acl_mode="memory" ⇒ 显式纯内存：不查权威库，调用方按无权威可查处理。
        #   否则（authoritative）⇒ 库不可用（缺失/搬迁/连不上/查询异常）
        #   一律是**故障**，必须 fail-closed。判据见 __init__ 的 _acl_authoritative。
        if not self._acl_authoritative:
            return None
        con = None
        try:
            con = self._fts_db()
            if con is None:
                # 配了库却拿不到连接 = 异常路径 ⇒ 必须 fail-closed（不再当"纯内存"）
                raise _AclTruthUnavailable(
                    "acl truth lookup: configured db unavailable (%s)" % self._fts_db_path)
            marks = ",".join("?" * len(ids))
            rows = con.execute(
                "SELECT id, state, namespace, visibility, retire_reason FROM chunks "
                "WHERE id IN (" + marks + ")",
                tuple(str(i) for i in ids),
            ).fetchall()
            # ★F2-1/F2-2：带出 retire_reason —— 隔离态的唯一识别依据（见 visibility.is_quarantined）。
            #   病灶（独立验收实测）：检索只按 (state, namespace, visibility) 判，
            #   隔离行的 visibility 仍是 shared ⇒ 被当作正常可读行返回。
            return {str(r["id"]): (str(r["state"] or ""), str(r["namespace"] or ""),
                                   str(r["visibility"] or ""), str(r["retire_reason"] or ""))
                    for r in rows}
        except _AclTruthUnavailable:
            raise
        except Exception as e:
            # ★F3：**配置了权威库但查询失败** ⇒ 不得退回旧授权。
            logger.warning("acl truth lookup failed on authoritative db (%s) — failing closed: %s",
                           self._fts_db_path, e)
            raise _AclTruthUnavailable(
                "acl truth lookup failed: %s" % (type(e).__name__,)) from e
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    def _fts_bm25(self, query: str, top_k: int, viewer) -> List[Tuple[int, float]]:
        """G2：SQL FTS5 词级腿 → [(idx, score)]（id→idx 映射；docs 查不到的 id 跳过+debug）。

        score 语义与内存 BM25 同向（越大越好），后续 max 归一化天然适配，不加额外缩放（方案甲要点）。
        """
        con = self._fts_db()
        if con is None:
            logger.debug("fts5: db not available (%s)", self._fts_db_path)
            return []
        try:
            self._fts_query_count += 1
            # ★M03（2026-10-13）：原写法 `viewer or "__admin__"`——**未声明身份 = 上帝视角**。
            #   搜索主循环末尾还有一道 visibility_ok 复核（见 _search_locked 的 P0.3/P1-E 段），
            #   所以它不是"正在漏"，但它让"忘了传 viewer"这条腿**先以全库视角取 top-k**：
            #   ① 违反 Spec §5.5#1「可见性筛选先于候选模型、FTS 摘录与向量返回」；
            #   ② 一旦下游那道复核被重构/挪动，这里立刻变成真泄漏——
            #      缺一道闸不会报错，是最危险的那种隐患。
            #   实测（<TEMP>\DSH\m03-probe7d.py）：不传 viewer 时 fts_search_bm25
            #   直接命中他人 private 行。
            #   改为透传 None → visibility_ok("") = 空身份 fail-closed，只放行 shared。
            #   ⚠️【不】把 `or "__admin__"` 加回来。要上帝视角必须在调用点显式写出来
            #      （显式才 review 得到；隐式缺省等于给越权开了个"什么都不写"的后门）。
            hits = fts_search_bm25(con, query, top_k=top_k, viewer=viewer)
        except Exception as e:
            # 件②（2026-09-13）：静默降级升级为事件留痕——降级不再只有一行 warning，
            #   事件落 xtmem_events(kind=fts5_fallback) 供 health/巡检消费（V1.5 同款 DB 通路）
            self._fts_fallback_count += 1
            from .diagnostics import emit
            emit('retriever', 'retrieval.fts', 'degraded', error=e,
                 fallback_reason='vector_only', retrieval_backend='fts5', query=query)
            self._write_fts_fallback_event(query, e)
            return []
        finally:
            try:
                con.close()
            except Exception:
                pass
        out = []
        idmap = getattr(self, "_id_to_idx", None) or {}
        for cid, score in hits:
            i = idmap.get(str(cid))
            if i is None:
                logger.debug("fts5 hit id not in docs: %s", cid)
                continue
            out.append((i, float(score)))
        return out

    def _write_fts_fallback_event(self, query: str, err: Exception) -> None:
        """件②（2026-09-13 顶模"第一刀"）：FTS5 腿降级写 xtmem_events(kind='fts5_fallback')。

        _write_fts_fallback_event()：契约
        入参：query（原始 query，只用于算 qsha，不落库）、err（异常对象，只取长度）
        返回：无
        副作用：生产库 xtmem_events 加一行（若库可达）；失败仅 debug 日志
        谁调用：_fts_bm25() except 分支
        改动影响：照 provider._write_probe_event 先例抄的独立短连接模式——
          ① meta 只带 qsha(8 位)+errlen，【不】带 query 原文：events 表无 visibility
          列，原文=越源暴露面；连 err 文本都【不】落（OperationalError 会回显 query
          分词内容，如 "no such column: local" 的 local 正是 query 里的词）。
          ② 尽力而为：记账失败【不】许挡检索主链路。
          ③ 不走 _record_event（受 stats 闸控默认关）——降级记账必须恒开。
        """
        try:
            import json as _json
            import sqlite3 as _sql
            dp = self._fts_db_path
            if not dp or not os.path.exists(str(dp)):
                return
            conn = _sql.connect(str(dp), timeout=5)
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS xtmem_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        kind TEXT NOT NULL,
                        agent TEXT,
                        namespace TEXT,
                        project TEXT,
                        target_chunk TEXT,
                        meta TEXT DEFAULT '{}')"""
                )
                conn.execute(
                    "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
                    (_time.time(), "fts5_fallback", None, None, None, "",
                     _json.dumps({"qsha": hashlib.sha256(query.encode("utf-8", "replace")).hexdigest()[:8],
                                  "errlen": len(str(err))}, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.debug("xtmem fts5_fallback event write skipped", exc_info=True)

    def fts5_health(self) -> dict:
        """件②.2（2026-09-13 顶模"第一刀"）：FTS5 腿健康出口——fallback>0 即不健康。

        本进程内口径（内存计数）；跨进程（20000 health 端点）走 xtmem_events 数
        fts5_fallback——展示层联动段在 api_server.py，范围锁外，见本单回执附件待放行。
        """
        return {"mode": self._fts5_mode,
                "queries": self._fts_query_count,
                "fallback": self._fts_fallback_count,
                "healthy": self._fts_fallback_count == 0}

    def _is_expired(self, doc: MemoryDoc, now: float = None) -> bool:
        """检查文档是否 TTL 过期。TTL=0 表示永不过期。"""
        ttl = _classify_ttl(
            doc.content, doc.tags, getattr(doc, 'track', 'default'),
            tier=getattr(doc, 'tier', 'raw') or 'raw',
            derived_from=getattr(doc, 'derived_from', '') or '',
        )
        if ttl == 0:
            return False  # permanent
        if now is None:
            now = _time.time()
        return (now - doc.timestamp) > ttl

    def get_expired_stats(self) -> int:
        """返回最近一次 search 跳过的过期文档数"""
        return self._expired_count

    def search(self, query: str, top_k: int = 10, now: Optional[float] = None, viewer: Optional[str] = None, utility_lookup=None, last_access_lookup=None, tier_filter: Optional[str] = None, time_decay: Optional[bool] = None, query_vec: Optional[List[float]] = None, route: Optional[str] = None, namespace_hint: Optional[str] = None, namespace_filter: Optional[tuple] = None, project_filter: Optional[str] = None) -> List[Tuple[MemoryDoc, float]]:
        with self._lock:
            return self._search_locked(query, top_k=top_k, now=now, viewer=viewer, utility_lookup=utility_lookup, last_access_lookup=last_access_lookup, tier_filter=tier_filter, time_decay=time_decay, query_vec=query_vec, route=route, namespace_hint=namespace_hint, namespace_filter=namespace_filter, project_filter=project_filter)

    def _search_locked(self, query: str, top_k: int = 10, now: Optional[float] = None, viewer: Optional[str] = None, utility_lookup=None, last_access_lookup=None, tier_filter: Optional[str] = None, time_decay: Optional[bool] = None, query_vec: Optional[List[float]] = None, route: Optional[str] = None, namespace_hint: Optional[str] = None, namespace_filter: Optional[tuple] = None, project_filter: Optional[str] = None) -> List[Tuple[MemoryDoc, float]]:
        if not self.docs:
            return []
        import time as _time
        if now is None:
            now = _time.time()

        # P4.8 T2（2026-09-01）：tier_filter 只对指定 tier 池算分（AF-3：第一跳 refined 池远小于全量；
        #   不把过滤加在打分循环里——那样 cosine 已算完，两跳=两次全量遍历，延迟翻倍零收益）
        # P1-L / G-01：白名单防御——未来扩展 tier 必须同步 _tier_index，否则这里直接拒
        if tier_filter is not None and tier_filter not in TIERS:
            raise ValueError("tier_filter must be 'raw' or 'refined', got %r" % (tier_filter,))
        if tier_filter:
            valid_idx = set(self._tier_index.get(tier_filter, []))
            if not valid_idx:
                return []
        else:
            valid_idx = None

        # BM25 分数（归一化，仅 tier 池内）
        # G2（2026-09-04）：FTS5 热接线——off=内存 BM25（现状逐字节一致）；
        # shadow=双跑对照（输出仍走内存，info 日志记 overlap）；on=FTS5 词级腿替换（id→idx 映射）
        # In on mode _fts_bm25 already owns the existing failure/empty-result
        # behavior. The memory scan's result was immediately overwritten.
        bm25_raw = [] if self._fts5_mode == "on" else self.bm25.search(query, top_k=top_k * 3)
        if self._fts5_mode in ("shadow", "on"):
            _fts_raw = self._fts_bm25(query, top_k * 3, viewer)
            if self._fts5_mode == "shadow":
                try:
                    _mem_ids = [self.docs[i].id for i, _ in bm25_raw[:top_k] if i < len(self.docs) and self.docs[i] is not None]
                    _fts_ids = [self.docs[i].id for i, _ in _fts_raw[:top_k] if i < len(self.docs) and self.docs[i] is not None]
                    _ov = len(set(_mem_ids) & set(_fts_ids)) / float(max(1, len(_mem_ids)))
                    logger.info("fts5_shadow query_hash=%s topk=%s overlap=%s", fingerprint(query), top_k, round(float(_ov), 3))
                except Exception:
                    pass
            else:
                bm25_raw = _fts_raw
        if valid_idx is not None:
            bm25_raw = [(idx, s) for idx, s in bm25_raw if idx in valid_idx]
        max_bm25 = max(s for _, s in bm25_raw) if bm25_raw else 1
        bm25_scores = {idx: score/max_bm25 for idx, score in bm25_raw}

        # 向量分数（仅 tier 池内）
        # 贰③c（2026-09-04）：embedding_version 锁芯闸——键不匹配（_vector_leg_ok=False）时
        # 向量腿整段跳过（fail-closed 退 BM25-only），【不】现场 embed 查询（防旧指纹向量
        # 混进新维度池算出垃圾 cosine）。原因记在 _vector_leg_reason，供事件/健康接口取证。
        vector_scores = {}
        # 闸断时保持 None（下游探针/MMR 均判空走纯相关性路径）；
        # query_vec 由调用方预置时（search_two_tier 两跳共享）不再现算——
        # ⚠️ 省一次 Ollama 往返：实测 embed 单发 0.9~1.8s 波动，两跳 miss 双现算=白付一倍
        #（2026-09-06 serve 延迟分相剖析，<TEMP>\serve_latency_profile.py）。
        if not self._vector_leg_ok:
            query_vec = None
        elif query_vec is None:
            # 预置向量（search_two_tier 两跳共享）时跳过现算——省一次 Ollama 往返
            #（实测 embed 单发 0.9~1.8s，两跳 miss 双现算=白付一倍；2026-09-06 剖析，
            # <TEMP>\serve_latency_profile.py）
            query_vec = self._embed(query)
        if query_vec is not None:
            if valid_idx is not None:
                _loop = [i for i in valid_idx if i < len(self._embeddings)]
            else:
                _loop = list(range(len(self._embeddings)))
            for i in _loop:
                if i >= len(self.docs) or self.docs[i] is None:
                    continue
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
        noise_context = None
        if self._noise_enabled:
            try:
                noise_context = self._prepare_noise_cache()
            except Exception:
                logger.debug("noise cache configuration unavailable", exc_info=True)
        _anchored = set()          # B0-2：命中锚通道的候选（免相对闸，见下）
        _downweighted = set()      # B0-2×D3-1 调和：被噪声降权的候选（同样免相对闸）
        # R01（2026-10-21）：查询的内容词只抽一次——循环里逐条重抽会把分词开销
        #   放大成 O(候选数)，而它跟候选无关（纯 query 属性）。
        # R01-FIX1：统一核心一次导入，术语/标识符/拒绝计数一起准备。
        from memory.evidence_admission import (
            assess as _assess, content_terms as _c_terms, strict_identifiers as _s_ids,
        )
        _q_terms_list = _c_terms(query)
        _q_ids_list = _s_ids(query)
        _admission_notes = {}      # doc_id -> 准入判定（供 CLI/hook 追溯）
        _admission_rejects = 0
        self._expired_count = 0
        for idx in all_indices:
            if idx >= len(self.docs):
                continue
            doc = self.docs[idx]
            if doc is None:
                continue
            # MIMOX: Query-conditioned TTL — 根据当前查询相关性动态调整过期衰减
            raw_bm = bm25_scores.get(idx, 0)
            raw_vec = vector_scores.get(idx, 0)
            bm = raw_bm * self.bm25_weight
            vec = raw_vec * self.vector_weight
            ttl = _classify_ttl(
                doc.content, doc.tags, getattr(doc, 'track', 'default'),
                tier=getattr(doc, 'tier', 'raw') or 'raw',
                derived_from=getattr(doc, 'derived_from', '') or '',
            )
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
            # P1-10（2026-09-06 外部审计整改总单-3）：lineage-stale 在线 fail-closed 消费——
            # 源被 forget/archive 的派生条带 lineage-stale:* 标，检索端直接排除
            #（离线 lineage_resolve.py 清标前，带病条不得被召回。V1.1 只写不消费的活证据：
            # 预判通道曾漏出废数据 mem-11f05cc2，报告 §8.3）。
            # 2026-09-13 收紧（stale判据收紧单）：只认带冒号真标记（写端后缀式
            # lineage-stale:<源id后缀>）；裸词是写手列的技术关键词不是病条——
            # 旧裸词命中曾屏蔽正经条，人工摘标回执又引用该词致新条再被屏蔽，闭环自复制。
            if any(str(t).startswith("lineage-stale:") for t in (doc.tags or [])):
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
            # D3-1（2026-09-10 派单工单）：噪声切片召回降权（防御纵深，与建图期剔除互补）。
            # 🔴【铁律】系数只能取 (0,1]，永不设 0——理由（用户 2026-09-10 点破，实锤下面
            #   第 711 行的 if final >= _probe_th or _anchor:）：若库里【所有】有效记忆都掉到
            #   及格线以下，被压到 0 的噪声也永不复活 = 失去兜底。乘系数（×0.3）才表达正确
            #   语义——「平时没你份，实在没别的了，你顶上」（同 refined 空则回落 raw 的哲学）。
            # ⚠️ 判据单一源：复用 memory.noise_filter.is_noise_text（与建图期 noise_filter 同一模块）。
            # enabled=false（默认）时 _noise_enabled=False → 整段跳过，final 逐字节不变。
            # 🔧 开闸分法（2026-09-10）：命中后【先取规则代号，再按该规则的系数决定降不降】——
            #   R1/R2 → ×0.3；R3 → 系数 null → 直接跳过（final 不动）。
            #   注意这里用 is_noise_text（R1/R2 文本判据）：R3 是实体密度、要查库算域阈值，
            #   按设计只做建图期排除，召回不碰（见 __init__ 的 R3 告警注释）。
            if self._noise_enabled:
                try:
                    from memory.noise_filter import is_noise_text, rule_of
                    _hit, _why = (self._cached_noise_result(doc, *noise_context)
                                  if noise_context else is_noise_text(doc.content))
                    if _hit:
                        _w = self.noise_rule_weights.get(rule_of(_why))
                        # None = 该规则不参与召回降权（R3）→ 显式跳过，与「×1.0」区分开
                        # （语义：这条规则在召回期没话语权，不是「它被判定为干净」）
                        if _w is not None and _w < 1.0:
                            final *= _w
                            # B0-2×D3-1 调和（2026-09-11）：被降权的噪声条目**免相对闸**。
                            # 依据 = 本段上方铁律（"系数永不设 0"）：降权（×0.3）的语义是
                            #   「平时没你份，实在没别的了，你顶上」，其兜底恰恰靠绝对线
                            #   relevance_threshold 存活。B0-2 的相对闸会把这条降权后的
                            #   低分条直接杀死（实测 r1: 0.270085 < 0.6×1.125=0.675），
                            #   与铁律冲突——D3-1 守卫 8.1a 因此红。
                            # 故降权条与锚通道同等待遇：只受绝对线约束，不受相对闸约束。
                            _downweighted.add(idx)
                except Exception:
                    logger.debug("noise recall downweight failed (non-fatal)", exc_info=True)
            # 待办池 #11（2026-09-06，W 批后续）：utility_score 霸榜修复——
            # 病灶：utility_score=access_count×0.1 裸累加无界（dsh 实测 6073 次=607 分），
            # ×viewer_utility_weight(0.05)=+30 直接进 final，碾压 bm+vec(0~2 量级)，
            # dsh 视角排序被"出场多"的骨架文档霸榜。治的是"进 final 的权重"（increment_utility
            # 的累加语义不动——那是热度真值）。
            # 修法（a+c 组合定案）：log1p 压缩 + cap 护栏——
            #   final += log1p(min(u_raw, 1000)) / log1p(1000) × viewer_utility_weight
            #   → [0,1000] 压到 [0,1]，最大增量 = viewer_utility_weight（0.05），与
            #   recency(0.1)/importance(0.05) 同量级；log 保单调（热度序不丢），cap 防极端。
            # ⚠️ viewer_utility_weight 语义收口（总则 13 灰度欠账）：不再是"裸分×权重"，
            #   而是"归一化热度加成上限"——0.05 = 该项对 final 的最大贡献 0.05，与
            #   importance_weight 同档，双重打折消除。
            # B2（2026-09-07）：route/namespace_hint 透传保留（签名兼容），加权不进两跳
            #   打分——boost 进 final 会把 refined 抬过 0.6 阈值、饿死 raw 回落（X 卷 gold
            #   几乎全是 raw）。加权改在 CLI 侧对候选池重排，见 xtmem.py cmd_recall。
            if utility_lookup is not None:
                import math as _m
                _u_raw = utility_lookup(doc.id)
                if _u_raw:
                    try:
                        _u = float(_u_raw)
                    except (TypeError, ValueError):
                        _u = 0.0
                    if _u > 0:
                        _UTIL_CAP = getattr(self, "_utility_cap", 1000.0)
                        final += (math.log1p(min(_u, _UTIL_CAP)) / math.log1p(_UTIL_CAP)) * self.viewer_utility_weight
            # ════════════════════════════════════════════════════════════════
            # R01·RQ02/RQ03（2026-10-21 小D）：证据准入与排序加分分离
            #
            # 病灶（核查结论.md §一.2，生产只读副本实测）：
            #   旧判据是 `if final >= _probe_th or _anchor`，而 final 里混着
            #     (bm+vec)*ttl_decay + recency*0.1 + importance*0.05 + utility 加成
            #   ⇒ "新鲜/重要/热门"能帮一条【不相关】的记录跨过门槛：
            #     实测无关问题「木卫九璃鲸赎月税率」仍返回 6 条（0.28~0.31 分），
            #     其中旧回执的分数构成：向量 0.270 + 新鲜度 0.100 + 重要性 0.035 + 热度。
            #   这违反 §9.7.2-2「证据相关性准入先于新鲜度/重要性/热度排序」。
            #
            # 修法：【准入只看相关性】，加分只影响【排序】。
            #   _admission = (bm + vec) * ttl_decay   ← 不含 recency/importance/utility
            #   准入线 = relevance_threshold（既有 0.15 不变，可 tuning 覆盖）
            #     ⚠️ 不抬全局阈值——方案明确禁止"以抬高阈值代替修入口"（§9.7.3 末段）。
            #     0.15 之所以够用：现在它作用在【纯相关性】上，而不是被加分注过水的 final。
            #   过线后 final 仍按原公式（含加分）计算，排序语义与改前一致。
            #
            # 锚通道（importance>=0.75 且 BM25 命中）保留，但改为"相关性有词法依据"才放行：
            #   旧形态里 raw_bm>0 只证明"沾了词"，现在额外要求 _admission 不低于
            #   锚点准入线 anchor_admission_ratio × relevance_threshold（默认 0.5），
            #   免得一条重要性拉满但主题无关的旧回执仅凭词法微弱命中豁免进门。
            #   ⚠️【不】直接把锚通道删掉：它是 B0-2 相对闸的配套豁免，删了会让
            #     「重要且的确词法命中」的正例被相对闸误杀（RQ03 后半句）。
            # ════════════════════════════════════════════════════════════════
            _admission = (bm + vec) * ttl_decay
            # ── 证据充分性（RQ02 的第二半）─────────────────────────────────
            # 只把加分摘出去还不够：核查报告实测无关问题的 cosine 就有 0.385~0.49，
            #   乘 vector_weight(0.7) = 0.27~0.34，照样越过 0.15 的绝对线。
            #   原因不是"阈值太低要往上抬"，而是【这一批根本没有证据】——
            #   向量相似度对"两段都挺长的中文"普遍偏高，它证明不了"这条回答了这个问题"。
            # 判据（二者其一即可，任一成立 = 这条有可追溯的证据）：
            #   ① 词法有据：raw_bm > 0 —— FTS5/jieba 词级真的命中了（不是单字擦边）；
            #   ② 语义极近：raw_vec >= vector_admission_min —— 余弦高到不需要词面佐证。
            # 两条都不成立 ⇒ 这个候选"既没字面证据、语义也不够近" ⇒ 不入候选池。
            #   ⚠️【不】把它写成"再抬一道全局阈值"：①是新增的证据通道（词法），
            #     ②只在①缺席时才用，且它是【充分性】判定不是【排序】加权。
            #   ⚠️ vector_admission_min 的默认值是初值（0.5），必须在固定正反例集上校准；
            #     本端未在真实库全量校准，交付时如实标"未验"（见 R01 回执）。
            # ⚠️ 注意：raw_bm > 0 本身【不够】。BM25._tokenize 对中文会同时产出
            #   【单字】和 bigram（见 BM25._tokenize），于是"关""系""原"这种常见字
            #   撞上任何一个含它们的文档就算命中——实测无关问句
            #   「ZXQ731920 冥王星奶酪关税征收标准」对一条 C03 记录拿到 bm_norm=0.2877，
            #   唯一重叠就是单字「关」。这正是"只沾一个词就算命中"的变体，
            #   也是核查报告里"非空率冒充正确率"的机械根源。
            #   ⇒ 词法证据必须是【有内容的多字词】命中，单字不算。
            # ★R01-FIX1（2026-10-21，独立验收 F4）：**判据收敛到唯一核心**
            #   这段原来自己拼规则（词法/语义/锚点三条件分散写着），补召在 CLI 里
            #   又抄了一份、hook 出口什么都不查 —— 三处各说各话。
            #   现在统一问 memory.evidence_admission.assess()，主路/补召/关联扩展/
            #   最终出口共用同一份判据（§9.7.3：核心给结果，端侧只消费）。
            #   主路额外把 has_reported 记进 doc 之外的旁路字典，供 CLI 写进返回体
            #   （「可追溯」是 F4 的硬要求：返回体要说得出这条凭什么进门）。
            _verdict = _assess(query, doc.content, raw_bm=raw_bm, raw_vec=raw_vec,
                               ttl_decay=ttl_decay, importance=doc.importance,
                               relevance_threshold=_probe_th,
                               vector_admission_min=self.vector_admission_min,
                               anchor_admission_ratio=self.anchor_admission_ratio,
                               terms=_q_terms_list, strict_ids=_q_ids_list)
            if _verdict["admitted"]:
                scored.append((idx, final))
                _admission_notes[str(doc.id)] = _verdict
                if _verdict["anchor_ok"]:
                    _anchored.add(idx)
            else:
                _admission_rejects += 1
        # B0-2（2026-09-11）：第二道闸——相对门限（加层限幅）。
        # 绝对阈值 0.15 对 bge-m3 恒真，此处再按「本批 top-1 分的 ratio 倍」限一遍。
        # 锚通道命中的候选免此闸（与原语义一致：锚是"重要且词法命中"，不该被相对分埋掉）。
        # 取并集过滤 = 低于任一闸即丢。参数进 xtmem_tuning.json retriever 段，可一键关。
        if self.relative_gate_enabled and scored:
            _und = [(i, s) for i, s in scored if i not in _anchored and i not in _downweighted]
            if _und:
                _top1 = max(s for _, s in _und)
                _cut = _top1 * self.relative_gate_ratio
                _keep = [(i, s) for i, s in scored
                         if (i in _anchored) or (i in _downweighted) or (s >= _cut)]
                _dropped = len(scored) - len(_keep)
                if _dropped:
                    # 可观测性：B0-2 要求"静默失效"类修完留信号
                    logger.info(
                        "B0-2 相对闸: 丢弃 %d/%d 条低于 %.4f（top1 %.4f × %.2f）；锚豁免 %d 条",
                        _dropped, len(scored), _cut, _top1, self.relative_gate_ratio, len(_anchored))
                scored = _keep
        # P0.3（2026-08-31）/ P1-E（R-09）：检索每跳校验——top-k/MMR 前始终按 viewer 过滤可见性。
        # 空 viewer 时 visibility_ok 对 shared 放行、对 private/unknown 拒绝（fail-closed）。
        #
        # ★★R3-1（2026-10-14 独立验收返修）：**必须回查数据库真值**，不能只信内存快照。
        # 病灶（实测，见 m03-review/evidence/integration-results.json 的
        #      I10-actual-retriever-rechecks-database）：
        #   self.docs[].visibility 是 hydrate/index 那一刻的**快照**。来源被收紧后，
        #   真实检索路仍然把这行返回给已无权限的 viewer —— 数据库已经不允许 zcode 读，
        #   retriever.search(viewer='zcode') 却照样吐出来。
        #   施工方原先的 I10-stale 判据是"手工读 DB 再调 visibility_ok"，**根本没走 retriever**，
        #   所以它证明不了这条；这正是判据必须走真实入口的教训（见错题本）。
        # 修法：先按快照粗筛（省掉显然不可见的），再对**筛后留下的候选**做一次批量 DB 回查，
        #   以库中当下的 (state, namespace, visibility) 为准。
        # ⚠️ 为什么用批量一次查而不是逐条：候选通常几十条，逐条查是 N 次往返；
        #   用一条 `WHERE id IN (...)` 拿回真值即可（本函数在 RLock 内，别做多余 IO）。
        # ★★F2-1/F2-2/F3（2026-10-14 第二轮返修）：三件事一起收口——
        #   ① 回查失败 **fail-closed**：配置了权威库却拿不到真值时，**整批淘汰**
        #      （不再"退回快照"把旧授权文档放出去）；
        #   ② 真值里带 retire_reason ⇒ 消费**权限隔离态**（隔离行一律淘汰）；
        #   ③ 正常 `_truth={}`（行都不在库里）语义保持：全部淘汰。
        from .visibility import visibility_ok, is_quarantined
        scored = [(idx, s) for idx, s in scored
                  if visibility_ok(getattr(self.docs[idx], "visibility", "shared"),
                                   getattr(self.docs[idx], "namespace", ""), viewer)]
        try:
            _truth = self._acl_truth([self.docs[idx].id for idx, _ in scored])
        except _AclTruthUnavailable as _e:
            # ★F3：权威库不可用 ⇒ **拒绝候选**，返回空结果而不是旧授权文档。
            #   调用方拿到空集 = 本轮无可信召回；这比"按旧权限放行"安全得多。
            logger.warning("acl truth unavailable — dropping all candidates (fail-closed): %s", _e)
            _truth = {}
        if _truth is not None:
            _kept = []
            for idx, s in scored:
                _t = _truth.get(str(self.docs[idx].id))
                if _t is None:
                    continue      # 库中已不存在（被删/被 merge）⇒ 不得返回
                # ★F2-2：真值四元组（含 retire_reason）；兼容三元组的旧结构
                _state, _ns, _vis = _t[0], _t[1], _t[2]
                _why = _t[3] if len(_t) > 3 else ""
                if _state != "active":
                    continue      # 已退役 ⇒ 幽灵封口
                if _why and is_quarantined({"retire_reason": _why}):
                    # ★F2-2：**权限隔离**行不得经检索返回（它的 visibility 可能仍是 shared,
                    #   只按 visibility 判会放行——独立验收实测的正是这条）。
                    continue
                if not visibility_ok(_vis, _ns, viewer):
                    continue      # 以库中**当下**权限为准（快照已失效）
                _kept.append((idx, s))
            scored = _kept
        if namespace_filter is not None:
            scored = [(idx, score) for idx, score in scored
                      if getattr(self.docs[idx], "namespace", "") in namespace_filter]
        # ★R01-FIX1（2026-10-21，独立验收 F2-B）：**project 同属候选范围，必须与
        #   namespace 在同一处生效**——也就是 sort/MMR/top_k 之前，而不是 CLI 拿回
        #   结果之后再筛。独立验收实测：主路筛掉了 wrong，补召又把 mem-60000001
        #   （project=wrong）塞回来，返回体写着 project=wrong + b2_like=true。
        #   引擎这里补上 project 过滤位，CLI 才能把"同域同项目"交到取名额之前。
        #   ⚠️ project_filter 为空串时**不过滤**（等价"不限项目"，与 CLI 的 --project 缺省语义一致）；
        #      不要把它写成 falsy 就丢——空串在 CLI 里表示"没指定"，不是"指定为空项目"。
        if project_filter:
            scored = [(idx, score) for idx, score in scored
                      if (getattr(self.docs[idx], "project", "") or "") == project_filter]
        scored.sort(key=lambda x: x[1], reverse=True)
        # ══════════════════════════════════════════════════════════════
        # R01·RQ06（2026-10-21 小D）：限额外名额保护（配额，不是加分）
        #
        # 病灶（r01 隔离夹具实测，子代理如实报告）：
        #   外域同主题记录 F 的字面匹配 0.300 顶格、本域正例 A 是 0.279，
        #   于是 F 排在 A 前面。若 F 又多又强，本域正确答案会被整体挤出 top_k。
        #   这不是相关性错，而是【名额分配】错——A 与 F 都相关，
        #   但 A 在请求域内、F 在请求域外。
        #
        # 修法：**配额不是加分**。给请求域（namespace_filter 的第一个/唯一域）
        #   预留名额：先按原序把域内候选填进配额，再用剩余名额补域外。
        #   ⚠️【不】用"域内 +0.15 分"那种加分：那会改变排序语义、把不相关的
        #     域内条目也抬上来（B2 的 NS_HINT_BONUS 就是这种，且它只在
        #     namespace_hint 生效时用）。配额只决定"谁拿到名额"，不动分数。
        #   ⚠️ 只在**单域过滤**（namespace_filter 恰好一个域）时启用：
        #     多域/无域过滤时"域内"概念不成立，硬做配额会凭空造偏好。
        #   ⚠️ 域内候选不足配额时，剩余名额【全部】还给域外——不许因为配额
        #     减少总产出（空槽不等于更安全，RQ02 的空包结论只适用于"无证据"，
        #     不适用于"有证据但域外"）。
        # ══════════════════════════════════════════════════════════════
        if (namespace_filter is not None and len(namespace_filter) == 1
                and self.domain_quota_ratio > 0 and scored):
            _home = next(iter(namespace_filter))
            _home_rows = [r for r in scored if getattr(self.docs[r[0]], "namespace", "") == _home]
            _other_rows = [r for r in scored if getattr(self.docs[r[0]], "namespace", "") != _home]
            if _home_rows and _other_rows:
                _quota = max(1, int(round(top_k * self.domain_quota_ratio)))
                _take_home = _home_rows[:_quota]
                _take_other = _other_rows[:(top_k - len(_take_home))]
                _rest = _home_rows[_quota:] + _other_rows[(top_k - len(_take_home)):]
                scored = _take_home + _take_other + _rest

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

        # R01-FIX1（F4）：把本轮准入判定挂到实例上，供 CLI 写进返回体。
        #   ⚠️ 用实例属性而不是改返回签名：返回值是 (doc, score) 二元组，
        #      全树 5 处调用都按二元组解包，改签名会静默炸掉所有调用方。
        self._last_admission_notes = dict(_admission_notes)
        self._last_admission_rejects = int(_admission_rejects)
        # 追踪引用计数（供定期清理使用）
        for idx, _ in selected:
            doc = self.docs[idx]
            self._recall_counts[doc.id] = self._recall_counts.get(doc.id, 0) + 1

        return [(self.docs[idx], score) for idx, score in selected]

    @diagnosed('retriever', 'retrieval')
    def search_two_tier(self, query: str, top_k: int = 6, now: Optional[float] = None, viewer: Optional[str] = None, utility_lookup=None, last_access_lookup=None, time_decay: Optional[bool] = None, route: Optional[str] = None, namespace_hint: Optional[str] = None, namespace_filter: Optional[tuple] = None, project_filter: Optional[str] = None) -> List[Tuple[MemoryDoc, float]]:
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
        # 两跳共享一次 query embedding：miss 路径省一整次 Ollama 往返（实测 0.9~1.8s）。
        # 闸断（_vector_leg_ok=False）时保持 None——两跳各自也不会现算，语义与逐跳独立 embed 一致。
        _qv = self._embed(query) if self._vector_leg_ok else None
        # 第一跳：refined
        refined = self.search(query, top_k=_ref_top, now=now, viewer=viewer,
                              utility_lookup=utility_lookup, last_access_lookup=last_access_lookup,
                              tier_filter="refined", time_decay=time_decay, query_vec=_qv,
                              route=route, namespace_hint=namespace_hint, namespace_filter=namespace_filter,
                              project_filter=project_filter)
        # O5（P6 波3，2026-09-05）：refined 首跳白名单——机器派生大页（wiki-entity/
        #   timeline/prediction-engine）不进首跳，只留人工蒸馏/画像/日记蒸馏+手动 refined。
        #   空 ''/NULL = 手动 store（4021 行主体）必须放行（写 not df 而非 df in set）。
        _whitelisted = []
        for _d, _s in refined:
            _df = (getattr(_d, "derived_from", "") or "")
            if _df in FIRST_HOP_ALLOWED or not _df:
                _whitelisted.append((_d, _s))
        refined = _whitelisted
        hits = [(d, s) for d, s in refined if s >= _th]
        if hits:
            hits.sort(key=lambda x: x[1], reverse=True)
            self._last_two_tier_mode = "refined-first"  # P4.4 ⑱
            emit('retriever', 'retrieval.tier', 'ok', tier_route='refined-first',
                 retrieval_backend='hybrid', query=query, count=min(len(hits), top_k))
            return hits[:top_k]
        # 第二跳：raw 兜底（与 refined 不相交，天然去重）
        self._last_two_tier_mode = "raw-fallback"
        emit('retriever', 'retrieval.tier', 'ok', tier_route='raw-fallback',
             fallback_reason='raw_tier', retrieval_backend='hybrid', query=query)
        return self.search(query, top_k=top_k, now=now, viewer=viewer,
                           utility_lookup=utility_lookup, last_access_lookup=last_access_lookup,
                           tier_filter="raw", time_decay=time_decay, query_vec=_qv,
                           route=route, namespace_hint=namespace_hint, namespace_filter=namespace_filter,
                           project_filter=project_filter)

    # ================================================================
    # 贰③c：embedding_version 锁芯——向量腿通断控制（唯一合法入口：context_builder）
    # ================================================================
    def close_vector_leg(self, reason: str):
        """向量腿 fail-closed：断闸 + 记原因。键不匹配/维度缺失时由启动校验调用。

        【不】在检索热路径里调这个——断闸是启动期决策，运行中翻闸会让同一次会话
        前后两跳的召回口径不一致（半路换算法=没法归因）。
        """
        self._vector_leg_ok = False
        self._vector_leg_reason = str(reason)[:200]
        logger.warning(
            "vector leg fail-closed (BM25-only): %s", self._vector_leg_reason
        )

    def reopen_vector_leg(self):
        """健康启动自愈写回后重开向量腿（键已匹配才许调）。"""
        self._vector_leg_ok = True
        self._vector_leg_reason = ""

    @property
    def vector_leg_ok(self) -> bool:
        """向量腿通断状态（健康接口/事件取证用）。"""
        return self._vector_leg_ok

    @property
    def vector_leg_reason(self) -> str:
        return self._vector_leg_reason

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
                if self.docs[idx] is not None
                and self.docs[idx].status != "refuted"
                and getattr(self.docs[idx], 'state', 'active') not in ("superseded", "archived")
            ]
            return {"level": 2, "results": [(self.docs[idx], s) for idx, s in scored]}

        # L3: 正则 FTS 全文扫描
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        fts_hits: List[Tuple[MemoryDoc, float]] = []
        for doc in self.docs:
            if doc is None:
                continue
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
        # 贰③c：embedding_version 闸断时 query_vec=None——没有查询向量就没法算 MMR 多样性项，
        # 退化为按 final 分排序直接取 top_k（语义=向量缺席，与 BM25-only 一致）
        if query_vec is None:
            return scored[:top_k]
        if len(scored) <= top_k:
            return scored
        # B0-1（2026-09-11）：MMR 只在 top-M 候选上做。scored 进入本函数前已按 final 降序
        # 排过（_search_locked 的 scored.sort），故 here 取前 M = 按相关性取前 M。
        # 复杂度从 O(top_k² × N) 降到 O(top_k² × M)，M 缺省 40（xtmem_tuning.json
        # retriever.mmr_candidate_pool，可配）。top_k 本身大于 M 时不截断（否则取不满）。
        _pool = max(self.mmr_candidate_pool, top_k)
        if len(scored) > _pool:
            scored = scored[:_pool]
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
