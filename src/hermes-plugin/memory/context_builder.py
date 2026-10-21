
# ================================================================
# context_builder.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：ContextBuilder——把各记忆子模块（wal/vector_store/hybrid_retriever/llm_extractor）
#           粘起来的胶合层；编排 prefetch / store / compress 全生命周期。
#
# 结构：__init__（hydration + WAL replay）→ store()（校验→hash 去重→血缘闸→WAL→Chunk→
#       store_chunk→演化检测）→ prefetch()（会话前召回）→ _detect_evolution() → _hydrate_retriever()。
#
# 修改铁律：
# 1. store() 是单事务写入口（P0-4）；正文+metadata+tier 一起落，禁止二段式补刀。
# 2. 血缘闸（P0.1）：derived_from 非空 → source_ids 必须非空，否则 raise ValueError；min_visibility 覆盖可见性。
# 3. 演化检测（_detect_evolution）必须排除自身（BF-1：search exclude_id + 循环 if target_id==new_id），防自环边。
# 4. 旋钮（Jaccard/confidence/极性/evolution 阈值）从 xtmem_tuning.json 读，别硬编码。
# 5. 【不】在 store() 里把 track/source_ids/derived_from/tier 各算一遍——M01 已把它们归一成
#    track_val/source_ids_val/derived_from_val/tier_val 四个局部量，WAL、Chunk 落库、内存 retriever
#    三处【共用同一份】。各算各的正是 M01 修的病原（写库一套、日志一套 ⇒ 重放对不上）。
#
# 特殊机制：
# - _hydrate_retriever() 先 _check_embedding_version()（贰③c 锁芯三态：无键落键/匹配闸开/不匹配
#   close_vector_leg+事件+自愈写回）再 WAL replay（R2.2-fix 幂等合并 + M01 tombstone 拦截）再 index()。
# - WAL replay 的字段优先级（M01，Spec §9.1 契约 4）：WAL 有值 → 用 WAL 值（逐字还原）；
#   WAL 无值（旧行）→ 才回落 _infer_track / "wal-replay"。⚠️【不】把 WAL 优先分支删掉只留推断。
# - AUTO_EXECUTE_ENABLED=False（闸一 shadow）：演化只记不执行。
# ================================================================
# *更新：2026-09-04 · 贰③c embedding_version 锁芯——_check_embedding_version()（闸芯唯一写入口）
#   + _write_embedding_event()；mismatch 只惩罚一个启动周期（自愈写回后下次启动恢复向量腿）*
# *更新：2026-10-12 · O01 写入/WAL恢复/索引阶段可归因；异常不回显正文，拒写不冒充恢复成功。*
# *更新：2026-10-12 · M01 载荷补全：store 把血缘全量写进 WAL；重放优先用 WAL 字段（tier/track/
#   source_ids/derived_from/host_origin 五字段保真）；tombstone 拦已遗忘条目防重放复活；
#   索引阶段独立 emit（失败不假报全链完成）。*
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
import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory.wal_engine import WALEngine, MemoryEntry, derive_memory_id
from memory.diagnostics import diagnosed, emit, fingerprint, correlation_id
# ★第三轮：全库唯一可见性枚举函数（memory/visibility.py 无任何依赖，模块级导入无环）。
# 写入归一（store）与重放回落（_hydrate_retriever）两处共用同一份口径——
# 【不】各处各写一遍判断，那正是本件要修的"两套口径"病根。
from memory.visibility import normalize_visibility as _normalize_visibility


def _entry_field(entry, name):
    """按『键存在 **且值非 None**』从 WAL 条目取字段；取不到返回 None（= 真缺失）。

    ★M01 第三轮（2026-10-13）R2 空出处保真的根，契约 §5 判据表。

    ⚠️【不】许改回 `getattr(entry, name, None) or 默认值` 那种**真假值**写法——
       空串 `""` 是合法的**已知**值（"调用方明确说这条没有出处"），
       用 `or` 判会把它当成"没有值"再回落成 "wal-replay"，
       于是 `store(host_origin="")` 写进去、重放读出来变成 "wal-replay"：
       用户明确存的已知事实被洗成一个猜测值（round-2 实测红项）。
    ⚠️ 为什么要看 `_present_fields`：重写路径（mark_compressed）会用 to_json()
       把旧条目重新序列化，**原本不存在的键会变成显式 null**。若把 null 当"存在"，
       旧行经一次重写就被"洗成带值行"，向后兼容失效。所以：**None = 缺失**，
       是重写幂等的必要条件（契约 §5 原话）。
       `_present_fields` 由 from_json 记录原始键集合、由 __post_init__ 对新条目置全字段名；
       此处再做一层 None 判断，两重保险。
    """
    if name not in getattr(entry, "_present_fields", frozenset()):
        return None
    value = getattr(entry, name, None)
    return None if value is None else value


# Knowledge 升级方案阶段二-①（2026-09-06，小D）：技术类查询判定词表。
# 命中任一技术词（且不含闲聊词）才并入 knowledge 命名空间召回——防日常寒暄挤占配额。
# 挂在 ContextBuilder 模块级（retriever/prefetch/网关预热三方共用一份口径）。
_KNOWLEDGE_TECH_WORDS = (
    "api", "接口", "部署", "架构", "脚本", "配置", "环境", "python", "代码",
    "调试", "bug", "报错", "实现", "集成", "数据库", "docker", "git", "linux",
    "命令", "参数", "token", "服务", "端口", "测试", "规范", "升级", "版本",
    "插件", "工具", "开发", "编译", "依赖", "协议", "日志", "排查", "向量",
    "索引", "缓存", "检索", "xtmem", "迁移", "备份", "运维", "crontab", "cron",
)
_KNOWLEDGE_CHITCHAT_WORDS = (
    "我", "今天", "心情", "感觉", "日记", "聊天", "你好", "谢谢", "晚安",
    "早上", "喜欢", "记得吗", "上次", "我们", "好玩",
)


def _looks_technical(query: str) -> bool:
    """技术类查询判定：命中技术词表且不含闲聊词（阶段二-① 查询侧闸门）。"""
    ql = (query or "").lower()
    if not ql:
        return False
    if any(w in ql for w in _KNOWLEDGE_CHITCHAT_WORDS):
        return False
    return any(w in ql for w in _KNOWLEDGE_TECH_WORDS)


def _filter_srcraw_candidates(candidates):
    """O3-a 守卫（C-O6/V7，P6 波1）：含 [src-raw:] 句的演化候选剔除。

    纯函数——供 _detect_evolution 与守卫单测共用；演进候选为 (id, content, score) 元组。
    """
    if not candidates:
        return candidates
    return [c for c in candidates if "[src-raw:" not in (c[1] or "")]
from memory.vector_store import VectorStore, Chunk
from memory.hybrid_retriever import HybridRetriever, MemoryDoc, make_embedding_fingerprint, EMBEDDING_META_KEY  # 贰③c：embedding_version 锁芯
from memory.cost_compressor import CostCompressor
from memory.embedding_provider import OllamaEmbedding, EmbeddingProvider
from memory.utils import compute_content_hash
from memory.llm_extractor import _infer_track

# P2.2（2026-08-31）：四道闸地基——默认 shadow（只记不执行）；enriches 永不触发状态变更。
# 闸一 shadow 已默认达成（AUTO_EXECUTE_ENABLED=False）；闸二~四（精度抽检/单域灰度/全量+回滚）待数据就绪后逐级开启。
AUTO_EXECUTE_ENABLED = False
ENRICHES_NEVER_EXECUTE = True

logger = logging.getLogger(__name__)

# P4.5 遗漏 11（2026-09-01）：track 白名单校验——不在名单内落 default + warning，防第二个 explicit 式野生值。
# 合法集 = 引擎打标(identity/preference/emotion/default/explicit) + P4.1 扩展(profile/procedural/prediction)
#            + P5.0-f(2026-09-02)：timeline（时间骨架 chunk，显式传 --track timeline 防 _infer_track 误判——Z指导裁决）。
_VALID_TRACKS = {"identity", "preference", "emotion", "default", "explicit", "profile", "procedural", "prediction", "timeline"}


def _coerce_track(track: Optional[str]) -> str:
    """track 白名单校验：空/非法 → default。"""
    t = str(track or "").strip() or "default"
    if t not in _VALID_TRACKS:
        logger.warning("track %r 不在白名单，落 default（P4.5 遗漏11）", t)
        return "default"
    return t


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

        # 公共安装允许显式配置 Ollama 地址，不把本机服务位置带给使用者。
        self._embedding: EmbeddingProvider = OllamaEmbedding(
            model=os.environ.get("XTMEM_EMBEDDING_MODEL", str(self._cfg["embedding_model"])),
            base_url=os.environ.get("XTMEM_EMBEDDING_URL", str(self._cfg["embedding_base_url"])),
            timeout=float(os.environ.get("XTMEM_EMBEDDING_TIMEOUT", self._cfg["embedding_timeout"])),
        )

        # ---- sub-modules ----
        self._wal = WALEngine(wal_dir=self._wal_dir)

        self._vector_store = VectorStore(db_path=self._db_path)

        self._retriever = HybridRetriever(
            embedding_provider=self._embedding,
            relevance_threshold=float(self._cfg["relevance_threshold"]),
            db_path=self._db_path,  # G2（2026-09-04）：FTS5 腿只读查询用
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
        self._observation_lock = threading.RLock()

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
            except Exception as exc:
                # ⚠️★O01（2026-10-13）：原为 logger.exception（= 隐式 exc_info=True），
                #   已改只记类名。理由：compress() 是**写入路径**（WAL 压缩重写 + 摘要落库），
                #   其内部处理的就是记忆正文（`e.content` → `_summarize_rules` → `self._wal.store`）。
                #   内层各块已各自 catch，能冒到这里的是"意料外"的异常——
                #   意料外异常的 traceback 里带什么没人保证得了（可能带正文）。
                #   安全方向：宁可少一段栈，也不赌。定位靠 "startup compress failed" + 类名足够。
                logger.warning("startup compress failed: %s", type(exc).__name__)

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
        except Exception as exc:
            emit('engine', 'retrieval', 'failed', error=exc, query=query, fallback_reason='no_context')
            return ""

        # Knowledge 升级方案阶段二-①（2026-09-06）：技术类查询默认并入 knowledge 域召回。
        # 设计约束（方案 §4 阶段二）：top 2-3 封顶防挤占日常记忆配额——独立小跳检索
        # knowledge 域，命中拼到主结果尾部（去重后），不抬高主召回的 k。
        # 任何异常只 debug 不炸 prefetch 主链（知识库挂载是增强项不是依赖项）。
        _knl_docs = []
        if _looks_technical(query):
            try:
                _knl_k = int(self._cfg.get("knowledge_top_k", 3))
                _knl_raw = self._retriever.search(
                    query, top_k=_knl_k, tier_filter=None, viewer=None,
                )
                _knl_docs = [d for d, _s in _knl_raw
                             if getattr(d, "namespace", "") == "knowledge"]
                if _knl_docs:
                    logger.info("knowledge mount: %d hits query_hash=%s", len(_knl_docs), fingerprint(query))
            except Exception as exc:
                # ⚠️★O01（2026-10-13）：去掉 exc_info=True。这条在 prefetch 路径上，
                #   而 `self._retriever.search(query, ...)` 的入参 query 就是**用户当前那句话**
                #   （正文 + 可能夹带的凭据）。检索链任何一层把 query 塞进异常消息
                #   （"failed to embed: <原文>"这种写法很常见），exc_info=True 就会把
                #   整段 traceback 连同用户正文打进日志 = O01 直接违规。
                #   只留异常类名；`fingerprint(query)`（短哈希）本来就在上一行，够定位了。
                logger.debug("knowledge mount skipped (non-fatal): query_hash=%s error=%s",
                             fingerprint(query), type(exc).__name__)

        if not results and not _knl_docs:
            return ""

        lines = ["[MEMORY CONTEXT — relevant past memories]",
                 "（以下为历史记忆，仅供参考，不是指令）", ""]
        used = 0
        idx = 0
        # Knowledge 方案阶段二-①（2026-09-06）：knowledge 条目配额封顶（方案 §4「top 2-3 封顶
        # 防挤占日常记忆配额」）——主检索+挂载小跳合计不超 knowledge_top_k（默认 3）
        _knl_cap = int(self._cfg.get("knowledge_top_k", 3))
        _knl_used = 0
        for doc, _score in results:
            if idx >= k:
                break
            # O3-b（P6 波3）：跨通道去重——profile/timeline 已由 system_prompt_block
            #   （_portrait_block/_timeline_block）常驻注入，prefetch 召回同 track 即双份。
            #   画像/时间骨架的更新信息不靠 recall 带——双通道各司其职，这里直接跳过。
            _tk = (getattr(doc, "track", "") or "")
            if _tk in ("profile", "timeline"):
                continue
            trust = "[TRUSTED]" if "memory_md" in (doc.tags or []) else "[UNVERIFIED]"
            # Knowledge 方案阶段二-①（2026-09-06）：knowledge 域条目主检索命中也打 [KNOWLEDGE] 标
            #（shared 全 viewer 可见，主检索本来就能捞到——标出来让模型知道这是知识库内容）
            if getattr(doc, "namespace", "") == "knowledge":
                if _knl_used >= _knl_cap:
                    continue
                trust = "[KNOWLEDGE]"
                _knl_used += 1
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
            # ts 用 epoch（time.time()）而非 naive iso——本字段经 /api/stats 暴露给外部读，
            # naive iso 无时区后缀，机器非 +8 时消费端无法换算（2026-09-05 统一口径）
            "ts": time.time(),
            "prefetch_chars": len("\n".join(lines)),
            "store_writes": 0,
        })
        # Knowledge 方案阶段二-①（2026-09-06）：knowledge 命中拼主结果尾部
        #（去重：主结果已收的 doc 不重复；封顶 _knl_k=3；预算沿用同一 budget 池）
        for _kd in _knl_docs:
            if _knl_used >= _knl_cap:
                break
            if _kd.id in {getattr(d, "id", "") for d, _ in results}:
                continue
            _ktok = self._retriever._est(_kd.content) if hasattr(self._retriever, "_est") else len(_kd.content) // 3
            if used + _ktok > budget:
                break
            lines.append(f"{idx+1}. [KNOWLEDGE] {_kd.content.strip()}")
            used += _ktok
            idx += 1
            _knl_used += 1
        if idx == 0 and _knl_docs and not results:
            # 主域零召回但知识库有货：至少吐知识行（否则技术查询在空域直接哑火）
            for _kd in _knl_docs[:_knl_cap]:
                lines.append(f"1. [KNOWLEDGE] {_kd.content.strip()}")
        return "\n".join(lines)

    @diagnosed('engine', 'write')
    def store_observation(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Store one observation and return its explicit, durable result."""
        meta = dict(metadata or {})
        with self._observation_lock:
            prior = None
            operation_id = str(meta.get("operation_id") or "")
            if operation_id:
                prior = self._vector_store.find_observation(
                    operation_id, str(meta.get("namespace") or ""), str(meta.get("project") or ""))
            try:
                memory_id = self.store(content, meta)
            except ValueError as exc:
                if not any(reason in str(exc) for reason in (
                    "operation_id payload conflict", "operation_id refers to an archived or forgotten observation")):
                    raise
                return {"ok": False, "action": "conflict", "memory_id": None,
                        "observation_id": None, "error_code": "operation_conflict"}
        if memory_id is None:
            return {"ok": False, "action": "rejected", "memory_id": None, "observation_id": None}
        observation = self._vector_store.find_observation_by_memory(memory_id)
        if operation_id and observation is None:
            observation = self._vector_store.find_observation_by_operation(
                operation_id, str(meta.get("namespace") or ""), str(meta.get("project") or ""))
            if observation:
                memory_id = observation["memory_id"]
        if prior:
            action = "idempotent"
        else:
            action = "new_observation" if observation and observation.get("content_hash") == compute_content_hash(content) and self._vector_store.count_observations_for_content_scope(compute_content_hash(content), str(meta.get("namespace") or ""), str(meta.get("project") or "")) > 1 else "created"
        return {"ok": True, "action": action, "memory_id": memory_id,
                "observation_id": observation.get("observation_id") if observation else meta.get("observation_id")}

    @diagnosed('engine', 'write')
    def store(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Persist a new memory entry.

        Writes to WAL, indexes into VectorStore, and feeds the retriever
        so future :meth:`prefetch` calls can find it.

        Returns the entry id, or None if skipped (duplicate content).
        """
        # === P0-2/P0-3 安全校验：先验证再进 WAL/retriever，拒绝即失败返回 ===
        validated = self._vector_store._validate_content(content)
        if validated is None:
            emit('engine', 'capture', 'rejected')
            raise ValueError("memory write rejected: content failed safety validation")

        meta = metadata or {}

        content_hash = compute_content_hash(validated)

        # === ★M01 第三轮：稳定记忆 ID（契约 §3）===
        # 调用方**显式**给了 operation_id（= 明说"这是同一次操作，重试请认同一身份"）
        #   → id 由 (operation_id, namespace, content) 确定性派生：同操作重试必得同 id。
        #   为什么必须这样：旧实现每次现摇随机 id，第一次失败、第二次成功 ⇒ WAL 里留下
        #   **两条不同 id 的同内容行**，"重试"在账上看起来像"写了两条不同事实"，
        #   操作级幂等无从谈起（验收方 R3 实测红项）。
        # 没给（store 内部用 correlation_id() 自动生成）→ 保持现状随机 `mem-<uuid4[:8]>`，
        #   不动既有行为（control-process-crash-* 三条判据走的正是这条路）。
        explicit_operation_id = str(meta["operation_id"]) if meta.get("operation_id") else None
        observation_id = str(meta.get("observation_id") or ("obs-" + uuid.uuid4().hex))
        request_hash = hashlib.sha256(json.dumps({
            "content_hash": content_hash,
            "namespace": str(meta.get("namespace") or ""),
            "project": str(meta.get("project") or ""),
            "speaker": meta.get("speaker"), "subject": meta.get("subject"),
            "scope": meta.get("scope"), "valid_from": meta.get("valid_from"),
            "valid_until": meta.get("valid_until"),
            "confirmation_type": meta.get("confirmation_type"),
            "source_refs": meta.get("source_refs"),
            "visibility": meta.get("visibility"),
            "source_ids": meta.get("source_ids"),
            "derived_from": meta.get("derived_from"),
            "type": meta.get("type"),
        }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        memory_id: Optional[str] = None
        if explicit_operation_id:
            _identity_scope = str(meta.get("namespace") or "")
            if meta.get("project"):
                _identity_scope += "|project:" + str(meta.get("project"))
            _derived = derive_memory_id(explicit_operation_id, _identity_scope, validated)
            _existing = self._vector_store.get_chunk(_derived)
            if _existing is None:
                memory_id = _derived
            elif (_existing.content_hash or "") == content_hash:
                memory_id = _derived
            else:
                # 32 位哈希碰撞（极罕见）：同一个派生 id 底下已经躺着**不同内容**。
                # ⚠️【不】许覆盖既有条目——覆盖 = 静默改写历史事实。
                # 退回随机 id（记忆不丢），并显式 emit degraded 让这件事可查。
                emit('engine', 'write.index', 'degraded', fallback_reason='deferred_recovery')
                logger.warning(
                    "derived memory id collision: %s already holds different content — "
                    "falling back to random id (existing entry left untouched)", _derived)

        # P0.1 写入闸（2026-08-31）：derived_from 非空 → source_ids 必须非空数组，否则拒写。
        # 空血缘衍生物禁止落库——防"无中生有"的蒸馏/摘要记忆。
        df = str(meta.get("derived_from") or "")
        if df:
            src_ids = meta.get("source_ids") or []
            if not isinstance(src_ids, (list, tuple)) or not src_ids:
                raise ValueError("memory write rejected: derived_from 需要非空 source_ids")
            # ★M03（2026-10-13）：衍生记忆可见性 = **全部来源读者的交集**（Spec §9.3），
            #   改由 SourceResolver 统一解析——不再走 min_visibility 的"最严字串"口径。
            #
            #   为什么必须换（实机复现的 I09 缺陷）：
            #     visibility 是 **namespace 相对** 语义。源 (ns=dsh, vis='private') 的读者是 {dsh}，
            #     旧口径取"最严字串"得到 'private'；一旦派生落在 assistant，
            #     'private' 在新的域下被解释成 {assistant} —— **实际读者集合扩大了**。
            #     取交集才是对的：交集 {dsh} 编码不成 assistant 域下的任何字串
            #     ⇒ 该情形按 §9.3「拒绝生成」处理。
            #
            #   ⚠️ 决策**只有一处**：resolver.derived_visibility_for_write()。
            #      vector_store 的写侧闸问的是同一个函数 ⇒ WAL 与 DB 的 visibility 天然同源
            #      （M01 契约 §5）。【不】在这里自己判"拒还是围栏"——两处各判一次就是两套口径。
            #   ⚠️ 保守档（有来源查不到）会给出 'private'，这正是 M01 既有判据
            #      （test_store_writes_lineage_into_wal 断言 WAL 与 DB 同源、且空库引用
            #       不存在来源时取最严）所期望的行为，【不】能改成拒绝落库——
            #      那会把引用了已删源的遗留行与 WAL 重放整批打死。
            from memory.source_resolver import get_resolver
            _ns_for_derived = str(meta.get("namespace") or "")
            _resolver = get_resolver(self._vector_store._get_conn())
            # ★R1-1（2026-10-14 返修）：把**调用方声明的 visibility** 一并交给决策函数。
            #   声明范围是额外上界（§9.3 + 独立验收 R1），不能因为来源宽就被改宽。
            #   ⚠️ 这里读的值必须与下面写进 WAL/Chunk 的**同一个来源**（meta['visibility']），
            #      否则又成"两套口径"——决策用一个值、落库用另一个（M03 的病根）。
            _declared_for_decision = meta.get("visibility")
            _decision = _resolver.derived_visibility_for_write(
                list(src_ids), _ns_for_derived,
                declared_visibility=(str(_declared_for_decision)
                                     if _declared_for_decision else None))
            if not _decision["ok"]:
                # ⚠️ 渲染读者集合走 VectorStore._readers_repr：ALL_READERS 是哨兵、不可迭代，
                #    裸 sorted() 会抛 TypeError，把"该拒写的业务异常"变成"工具异常"（本单踩到）。
                raise ValueError(
                    "memory write rejected: 派生读者集合无法表达为合法 visibility"
                    "（Spec §9.3 拒绝生成）namespace=%r readers=%s refusals=%s"
                    % (_ns_for_derived,
                       self._vector_store._readers_repr(_decision["readers"]),
                       _decision["refusals"])
                )
            meta["visibility"] = _decision["visibility"]

        if explicit_operation_id:
            existing_operation = self._vector_store.find_observation(
                explicit_operation_id, str(meta.get("namespace") or ""),
                str(meta.get("project") or ""))
            if existing_operation:
                if existing_operation["request_hash"] != request_hash:
                    raise ValueError("operation_id payload conflict; existing observation was not changed")
                prior_chunk = self._vector_store.get_chunk(existing_operation["memory_id"])
                if prior_chunk is None or prior_chunk.state != "active":
                    raise ValueError("operation_id refers to an archived or forgotten observation")
                emit('engine', 'write.index', 'duplicate')
                return existing_operation["memory_id"]

        # 1. WAL（校验已通过，才允许进 WAL）
        content = validated
        # M01（2026-10-12）：WAL 与 DB 必须从【同一份规范化载荷】构造（Spec §9.1 契约 4）。
        # 因此在进 WAL 之前先把血缘/track/tier 一次性归一，下面 WAL 与 Chunk 两处都复用它——
        # 【不】许两处各算一次：各算一次正是本件要修的"两套口径"病根
        # （写库按 meta 算、日志里没有 ⇒ 重放读出来跟写入时不等）。
        track_val = _coerce_track(meta.get("track") or _infer_track(content) or "default")
        source_ids_val = list(meta.get("source_ids") or [])
        derived_from_val = str(meta.get("derived_from") or "")
        # tier 白名单（与 vector_store.TIERS 同口径）：不在 {raw, refined} 就落 raw。
        # ★第三轮：旧写法 `str(meta.get("tier") or "raw")` 对 tier="" 与 tier="garbage" 一样
        #   只在 store_chunk 里被拒写；这里先归一，保证 WAL 里存的与 DB 会落的同一个值
        #   （否则会出现"日志写 garbage、库拒写"的两套口径，重放再拿 garbage 去撞同一堵墙）。
        tier_val = str(meta.get("tier") or "raw").strip() or "raw"
        if tier_val not in ("raw", "refined"):
            tier_val = "raw"
        # ★R2 补件（2026-10-13 验收方反例）：visibility / host_origin / updated_at 也归一进
        # 同一份载荷——此前 WAL 存 '' 而 DB 落 'shared'，重放把 '' 当旧行缺失回落 'private'，
        # 同一载荷两套默认（实测 shared→private 降级）；updated_at 根本没进 WAL，重放拿新时间顶替。
        # 修法：这里就定死「DB 最终落库值」，WAL 与 DB 都用它。旧行缺字段（None）才走回落。
        #
        # visibility 合法性（契约 §5）：非法值→private。⚠️【不】用真假值判——空串是
        #   "调用方显式没给"，落 shared 是现状语义（与 P1-E 的 fail-closed 不冲突：那是
        #   store_chunk 的守门，这里是归一）；但若调用方给了个 normalize_visibility 不认的
        #   野值，直接送 store_chunk 会被**整条拒写**，所以先在归一阶段收敛成 private。
        visibility_val = str(meta.get("visibility") or "shared")
        if _normalize_visibility(visibility_val) is None:
            logger.warning("visibility %r 非法，归一为 private（防 store_chunk 整条拒写）", visibility_val)
            visibility_val = "private"
        host_origin_val = str(meta.get("host_origin") or "")
        # updated_at：**按"键在且非 None"采信**，不用真假值——0.0 是合法时间戳
        #（契约 §5 判据表：`0.0 也算存在`）。用真假值判会把显式 0.0 洗成"当前时刻"。
        updated_at_val = (float(meta["updated_at"])
                          if meta.get("updated_at") is not None else time.time())
        namespace_val = str(meta.get("namespace") or "")
        project_val = str(meta.get("project") or "")

        # ── ★第三轮：v2 信封载荷（契约 §5）────────────────────────────────
        # 全部 Optional，**缺就是缺，【不】编默认值造事实**——没给 speaker 就留 None，
        # 绝不用 "" 冒充"确实没有说话人"。重放侧据此区分"已知为空"与"根本没带"。
        format_version_val = 2
        speaker_val = meta.get("speaker")
        subject_val = meta.get("subject")
        # ★第三轮 R2（2026-10-13）：出处引用**原样透传**，【不】在这里 `str()`。
        #   旧写法 `[str(x) for x in (...)]` 会把类型化 SourceRef（dict/dataclass）压成
        #   Python 字面量串 `"{'kind': ...}"`，WAL 落盘后再读回来 kind/locator/revision
        #   全成了文本片段——出处不可机器消费，判据 `e.source_refs == [ref]` 当场不成立
        #   （验收 v3 `R2-typed-source-ref-preservation` 实测红项）。
        #   修法：这里只做"取出原始值"，归一/JSON 安全化交给 MemoryEntry.__post_init__
        #   的 `_coerce_source_refs` 一个落点——WAL 与 DB 从同一份规范化载荷构造
        #   （契约 §5），两处各洗一遍正是本件要修的"两套口径"病根。
        #   ⚠️ 归一用 MemoryEntry._coerce_source_refs（与 WAL 落盘同一个函数）——
        #      裸字符串会被包成单元素列表而不是被逐字符拆开（"abc" → ['a','b','c'] 是事故形态）。
        source_refs_val = MemoryEntry._coerce_source_refs(meta.get("source_refs"))
        scope_val = meta.get("scope")
        valid_from_val = meta.get("valid_from")
        valid_until_val = meta.get("valid_until")
        confirmation_type_val = meta.get("confirmation_type")
        # provenance_status：调用方显式给了合法值就采纳；否则按"到底带没带 provenance"判——
        # 带了 speaker/subject/source_refs 任一 → verified；一个都没有 → legacy_unverified。
        # ⚠️【不】在这里补造 speaker/subject/source_refs：补造 = 编事实，比缺字段更坏
        #   （缺字段是"不知道"，补造是"假装知道"）。
        _provided_provenance = bool(speaker_val or subject_val or source_refs_val)
        _ps = str(meta.get("provenance_status") or "")
        if _ps in ("verified", "legacy_unverified"):
            provenance_status_val = _ps
        else:
            provenance_status_val = "verified" if _provided_provenance else "legacy_unverified"

        # operation_id：调用方显式传入必须原样采纳（契约 §2）；未传才自动生成。
        operation_id_val = explicit_operation_id or correlation_id()
        # 记忆 id：派生路径用派生值；随机路径在这里**先定下来**——下面要拿它记 received
        # 回执，等 WALEngine.store 内部再生成就来不及了（回执要能对上账）。
        # 格式与 MemoryEntry 的 default_factory 完全一致：mem- + uuid4 hex[:8]。
        entry_id_val = memory_id or f"mem-{uuid.uuid4().hex[:8]}"

        emit('engine', 'write.wal', 'queued')
        # ★第三轮：received 回执（进 WAL 前）。语义 = "这单我接手了，准备落盘"。
        # 为什么放在 fsync 之前而不是之后：它的用武之地恰恰是"进程在写盘中途死掉"——
        # 只有写前落下它，事后才能分清"没收到"和"收到了但没写完"。
        self._wal.record_receipt(operation_id_val, entry_id_val, "received", status="ok")
        entry = self._wal.store(
            id=entry_id_val,
            content=content,
            importance=max(0.0, min(1.0, float(meta.get("importance", 0.5)))),
            tags=list(meta.get("tags", [])),
            session_id=str(meta.get("session_id", "default")),
            type=str(meta.get("type", "conversation")),
            namespace=namespace_val,
            project=project_val,
            host_origin=host_origin_val,
            visibility=visibility_val,
            tier=tier_val,  # P4.8 T3：WAL 重放保真
            # M01：血缘三件套 + 书写身份进日志。此前这三项根本没进 WAL，
            # 崩溃重放只能硬猜 track、丢来源链、把出处写成定值（B00 探针实测五字段丢失）。
            track=track_val,
            source_ids=source_ids_val,
            derived_from=derived_from_val,
            operation_id=operation_id_val,
            observation_id=observation_id,
            request_hash=request_hash,
            updated_at=updated_at_val,
            # ★第三轮：v2 信封随正文落盘（与上面各项同一份载荷，重放逐字还原）
            format_version=format_version_val,
            content_hash=content_hash,
            speaker=speaker_val,
            subject=subject_val,
            source_refs=source_refs_val,
            scope=scope_val,
            valid_from=valid_from_val,
            valid_until=valid_until_val,
            confirmation_type=confirmation_type_val,
            provenance_status=provenance_status_val,
        )
        # M01：WAL 已 fsync 返回，到此才是"durable commit"——报 persisted 名正言顺
        # （Spec §9.1 契约 2：received→persisted→indexed，只有 durable commit 后才报 persisted）。
        # ⚠️ persisted **回执**由 WALEngine.append 自己落（它才是唯一知道 fsync 成没成的人，
        #    契约 §4 明令【不】能用"WAL 行存在"反推），这里不重复记。
        emit('engine', 'write.index', 'queued')

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
            # P0-4：track 与 metadata 全部随正文单事务落库，不再二段式补刀
            # M01：全部复用上面那份归一值——与写进 WAL 的是【同一个值】，
            #   重放才可能逐字还原（契约 4）。★R2 后 visibility/host_origin/updated_at 也归一。
            track=track_val,
            namespace=namespace_val,
            project=project_val,
            host_origin=host_origin_val,
            visibility=visibility_val,
            updated_at=updated_at_val,
            # P0.1（2026-08-31）：血缘随正文落库
            source_ids=json.dumps(source_ids_val, ensure_ascii=False),
            derived_from=derived_from_val,
            # P4.8 T3：分层库 tier 随正文落库（手动/蒸馏 refined，流水 raw，缺省 raw）
            tier=tier_val,
            # ★B-补（2026-10-13）：v2 信封 provenance **同一份载荷**进 Chunk → 落 DB。
            #   病根（验收 v3 实测）：这几个值此前只传给了 `self._wal.store(...)`（写在上面），
            #   **Chunk(...) 这里一个都没传**——于是 WAL 有值、DB 全 NULL，
            #   契约 §5「WAL 与 DB 从同一份规范化载荷构造」不成立。
            #   ⚠️ 为什么用上面那几个 *_val 局部变量、不重新 `meta.get(...)`：
            #      重算一次 = 两套口径，正是本件要根治的病根；WAL 与 DB 必须字面同源。
            #      `source_refs_val` 已经是 MemoryEntry._coerce_source_refs 归一过的结构化列表，
            #      这里原样传（别再 str()/json.dumps 一次，那会把结构烧成文本）。
            speaker=speaker_val,
            subject=subject_val,
            source_refs=source_refs_val,
            scope=scope_val,
            valid_from=valid_from_val,
            valid_until=valid_until_val,
            confirmation_type=confirmation_type_val,
            # provenance_status：与写进 WAL 的同一个值（"这条到底带没带载荷"的权威表征）。
            #   ⚠️ 它决定 DB 侧能否区分"显式没出处"与"根本没带"，丢了它等于把缺省语义洗白。
            provenance_status=provenance_status_val,
            operation_id=operation_id_val,
            observation_id=observation_id,
            request_hash=request_hash,
        )
        try:
            stored = self._vector_store.store_chunk(chunk)
        except Exception as exc:
            raced = self._vector_store.find_observation(operation_id_val, namespace_val, project_val)
            if raced:
                if raced["request_hash"] == request_hash:
                    emit('engine', 'write.index', 'duplicate')
                    return raced["memory_id"]
                raise ValueError("operation_id payload conflict; existing observation was not changed") from exc
            # M01（契约 2）：索引阶段失败要显式标出来，【不】让上游以为"WAL 写了=全链完成"。
            # 此时 WAL 行已是 persisted，重放会在下次启动补写——诊断里必须看得出这条还差索引。
            # ★第三轮：持久回执同步标 failed（带 error_type），让"差索引"这件事逐笔可查，
            #   而不是只在诊断采样里闪一下。
            self._wal.record_receipt(operation_id_val, entry.id, "failed",
                                     status="failed", error_type=type(exc).__name__)
            emit('engine', 'write.index', 'failed', error=exc, fallback_reason='deferred_recovery')
            emit('engine', 'write.vector_store', 'failed', error=exc, fallback_reason='deferred_recovery')
            raise
        if not stored:
            self._wal.record_receipt(operation_id_val, entry.id, "indexed",
                                     status="rejected", error_type="Rejected")
            emit('engine', 'write.index', 'rejected', fallback_reason='deferred_recovery')
            emit('engine', 'write.vector_store', 'rejected', fallback_reason='deferred_recovery')
            raise ValueError("memory write rejected by vector store")
        # ★第三轮：indexed 回执——store_chunk 成功返回才是"已入索引"的证据。
        self._wal.record_receipt(operation_id_val, entry.id, "indexed", status="ok")
        emit('engine', 'write.index', 'ok')
        emit('engine', 'write.vector_store', 'ok')
        self._last_stored_chunk = chunk  # 供 consolidation 使用

        # P4.9-波1 D（2026-09-02，v1.12.1）：store 后置钩子——落库后打标写 chunk_entities + content_time（NER 接线）
        # 失败只记录不阻断（降级链：零填不 raise），gateway 需重启加载此接线。
        # 发现30 食尾蛇（2026-09-02）：派生产物（wiki/timeline/distill/profile）不再被 NER 打标/入图——
        #   否则页面把上一代页面当语料，df/共现随代际自灌水（Z指导 §29-§30 审计发现30）。
        if not (chunk.derived_from or ""):
            # ── ★M01 第三轮 R3 修复（2026-10-13）：抽取阶段状态由**实际结果**决定 ──
            # 病根（验收 v3 `R3-extraction-failure-not-completed` 实测红）：
            #   旧写法在 NER/图两个钩子各自 `except: 记日志` 之后，**无条件**写
            #   `extracted/ok`。两个钩子双双抛异常时，回执账本仍然是
            #   `received/ok → persisted/ok → indexed/ok → extracted/ok`——
            #   等于**假报全链完成**：事后查账的人看不出"抽取根本没跑"。
            #   契约血泪教训 4 原话：「捕获异常后函数走完」≠ 阶段成功。
            #
            # 修法：两个钩子的成败先各自记进 hook_failures；**只要有一个失败**，
            #   extracted 就如实标 `failed` + error_type（异常类名），**绝不写 ok**。
            #   ⚠️【不】改成抛异常：NER/图是**增强项**不是依赖项，失败不许阻断落库
            #      （正文/载荷此时已 persisted+indexed，为它炸掉整条写入才是真事故）。
            #      本单要修的是"**说它成功了**"，不是"不许它失败"。
            #   ⚠️【不】造新词：stage 用 WAL_WRITE_STATUSES 的 `extracted`，status 用
            #      RECEIPT_STATUSES 的 `failed`（契约 §1.1 / §4，两套词本就同源）。
            #   ⚠️ 为什么"一个成一个败"也算 failed：extracted 的语义是"后置抽取钩子
            #      **跑完**"（Spec §9.1），半跑完不是跑完。半成功留给 emit 的 degraded
            #      事件记明细；回执这一格只回答"全链终点到了没"——含糊 = 又给它留了假报的口子。
            #   ✅ 可重建性（【不】许误解为"数据被回滚"）：本改动**只**动回执账本，
            #      不动已落盘的正文/载荷，也不回滚 WAL/DB。抽取产物（chunk_entities /
            #      tag_edges）是正文的派生物，可由同一份正文按既有幂等重算语义补算
            #      （见下方 P5.0-a 注释与 memory/graph.py 的 increment_for_chunk）。
            hook_failures = []  # [(钩子名, 异常对象), ...]——异常对象**只**用于取类名

            try:
                from memory.ner_tagger import tag_chunk
                tag_chunk(entry.id, content)
            except Exception as exc:
                # ⚠️★O01 红线（2026-10-13）：**绝不能用 exc_info=True**。
                #   这两处就在 store() 的写入路径上，注入/真实异常正文里可能混着
                #   调用方的私密载荷与凭据；`exc_info=True` 会把整段 traceback
                #   （含 `RuntimeError(<正文+凭据>)`）打进 debug 日志 ⇒ 违反
                #   O01「日志不回显正文/凭据」。只留**异常类名** + 关联 ID + 阶段。
                #   血泪教训：本文件这两处正是验收 v3 `O01-extraction-error-does-not-leak`
                #   实测的泄漏点（改成类名后，日志里再也搜不到私密载荷）。
                hook_failures.append(("ner.tag_chunk", exc))
                logger.debug(
                    "ner tag_chunk skipped (non-fatal): op=%s chunk=%s stage=extracted error=%s",
                    operation_id_val, entry.id, type(exc).__name__)

            # P5.0-a（2026-09-02）：图增量维护——新 chunk 的 canon 共现边写 tag_edges（幂等重算，失败不阻断）
            try:
                from memory.graph import increment_for_chunk
                increment_for_chunk(entry.id)
            except Exception as exc:
                # ⚠️★O01 红线：同上——只记类名，【不】记 traceback（会把私密载荷带进日志）。
                hook_failures.append(("graph.increment_for_chunk", exc))
                logger.debug(
                    "graph increment skipped (non-fatal): op=%s chunk=%s stage=extracted error=%s",
                    operation_id_val, entry.id, type(exc).__name__)

            # ★第三轮：extracted 回执——后置抽取钩子（NER/图）跑完才是全链终点（Spec §9.1）。
            # ⚠️ 只在真的走了这条钩子的分支里记：派生产物（derived_from 非空）**刻意**跳过
            #    NER/图（发现30 食尾蛇），对它们记 extracted = 假报一个没发生的阶段。
            #    "没做"和"做了"必须分得开，这正是本单补 extracted 这个状态词的意义。
            if hook_failures:
                # 如实标失败（契约血泪教训 4）。error_type 取**第一个**失败的类名：
                #   回执单行字段是封闭集（契约 §4：ts/operation_id/memory_id/stage/status/
                #   error_type），不额外塞"哪个钩子败的"——那会把封闭账本撑成半自由格式。
                #   两个钩子的明细走下面的 emit 事件与上面的 logger（都只带枚举+类名）。
                _first_hook, _first_exc = hook_failures[0]
                self._wal.record_receipt(
                    operation_id_val, entry.id, "extracted",
                    status="failed", error_type=type(_first_exc).__name__)
                for _hook, _exc in hook_failures:
                    # status 用 diagnostics 的 `degraded`（那是 emit 的合法枚举，不是回执的）：
                    #   语义精确——"落了库、但这一步降级了"。⚠️ emit 内部把 error 收敛成
                    #   类名（见 memory/diagnostics.py emit 的 `type(error).__name__`），
                    #   正文与凭据进不去，O01 安全。
                    emit('engine', 'write.index', 'degraded',
                         error=_exc, fallback_reason='deferred_recovery')
            else:
                self._wal.record_receipt(operation_id_val, entry.id, "extracted", status="ok")

        # Phase 1 R9: 知识演化检测（日志模式，不自动标 superseded）
        try:
            evolutions = self._detect_evolution(content, entry.id, namespace=(meta.get("namespace") or None))
            for target_id, relation, confidence in evolutions:
                # P2.2（2026-08-31）：同域事实更新 supersedes（自动判定）；跨域分歧 disagrees_with（并存，禁止自动 supersede）
                if relation == "replaces":
                    relation = "supersedes"
                # D1-2（2026-09-09）：判定来源关不关，这里都闸死——边不落库、旧边不关闭、不触发
                # mark_superseded。为什么要两道闸：_detect_evolution 里删分支只挡当前这一条路，
                # 别的调用方（或将来有人把分支接回来）还会往里塞 supersedes；闸在落库口才叫关死。
                # 【不】严禁回填：放开本 continue = 25% 精度垃圾影子边按天复产（health 的 shadow_edge_backlog 会立刻抬头）。
                if relation in ("supersedes", "replaces"):
                    logger.debug("D1-2 auto %s edge suppressed: src=%s tgt=%s", relation, entry.id, target_id)
                    continue
                logger.info(
                    "Evolution detected: content_hash=%s %s (conf=%.2f) → target=%s [%s]",
                    fingerprint(content), relation, confidence, target_id,
                    "SHADOW" if not AUTO_EXECUTE_ENABLED else "EXECUTE",
                )
                # P4.0-c（2026-08-31）：新 supersedes/replaces 边落库前，关闭这对节点间仍开放的旧边（valid_to=now）
                if relation in ("supersedes", "replaces"):
                    self._vector_store.invalidate_evolution(entry.id, target_id)
                # 四道闸地基：默认 shadow（只记不执行）；仅 supersedes/replaces 有执行权，enriches/confirms 永不触发状态变更
                shadow_state = 1 if not AUTO_EXECUTE_ENABLED else 0
                self._vector_store.save_evolution(entry.id, target_id, relation, confidence, shadow_state=shadow_state)
                if relation in ("supersedes", "replaces") and AUTO_EXECUTE_ENABLED:
                    self._vector_store.mark_superseded(target_id, entry.id)
                    # S1（B方案，2026-09-06）：实时剔除被覆盖旧记忆（内存置哨兵）
                    try:
                        self._retriever.remove(target_id)
                    except Exception:
                        pass
        except Exception as e:
            emit('engine', 'write.index', 'degraded', error=e)

        # 3. Retriever (live update)
        doc = MemoryDoc(
            id=entry.id,
            content=content,
            timestamp=entry.timestamp,
            importance=entry.importance,
            tags=entry.tags,
            # M01：复用归一值（与 WAL/DB 同一份），防"三处各算一次 track"口径漂移
            track=track_val,  # Phase 1 R5 + P4.5 遗漏11 校验
            # P0.3（2026-08-31）：实时 append 位点同步补 namespace/visibility，新写入的即时可见
            namespace=str(meta.get("namespace") or ""),
            # ★R01-FIX1（F2-B）：project 同补——与 hydrate 位点同口径，
            #   否则「刚写入的」与「重启后加载的」在项目过滤上行为不一致。
            project=str(meta.get("project") or ""),
            visibility=str(meta.get("visibility") or "shared"),
            # P4.8 T2：分层库 tier——手动/蒸馏产物 refined，对话流水 raw（缺省 raw）
            tier=tier_val,
            # O5（P6 波3）：血缘随 append 进内存——refined 首跳白名单按它过滤机器派生
            derived_from=derived_from_val,
        )
        # S1（B方案常驻书架，2026-09-06）：使用 retriever.append 同步维护 docs/_id_to_idx/_tier_index/_embeddings/bm25
        vec = None
        try:
            vec = self._retriever._embed(content)
            # B0-10（2026-09-11）：写缓存前校验维度。Ollama 不可达时 _embed 回落
            # SimpleEmbedder（128 维），原实现仍以 model="bge-m3" 标签写进 embedding_cache
            # → 库里出现 dim=128 的行，日后与 1024 维混算得非零垃圾。维度不符则
            # 【不缓存】且该 doc 向量置零（下方 append 传 None 语义一致：退 BM25-only）。
            if len(vec) != self._expected_dim():
                logger.warning(
                    "B0-10 维度不符：embed 返回 %d 维，期望 %d 维 —— 不写 embedding_cache，"
                    "该条向量置零（退 BM25-only）。多半是 embedding provider 回落了 SimpleEmbedder。",
                    len(vec), self._expected_dim())
                vec = None
            else:
                self._vector_store.cache_embedding(
                    text=content,
                    embedding=vec,
                    model=str(self._cfg.get("embedding_model", "bge-m3")),
                    chunk_id=entry.id,
                )
        except Exception as exc:
            emit('engine', 'write.index', 'degraded', error=exc, fallback_reason='memory_bm25')
        try:
            self._retriever.append(doc, embedding=vec)
        except Exception as exc:
            emit('engine', 'write.index', 'failed', error=exc, fallback_reason='deferred_recovery')
            raise

        # 4. Lazy compression check
        if not self._prefetch_only and self._should_compress():
            self.compress()

        logger.debug("ContextBuilder stored entry=%s tokens=%d", entry.id, entry.token_count)
        self._store_count += 1
        self._timeline.append({
            "ts": time.time(),  # epoch，与全库 ts 口径一致（naive iso 隐患见 278 行注释）
            "prefetch_chars": 0,
            "store_writes": 1,
        })
        if len(self._timeline) > 96:
            self._timeline = self._timeline[-96:]
        emit('engine', 'write', 'ok')
        return entry.id

    # ── Phase 1 R9: 知识演化检测 ──────────────────────────

    def _detect_evolution(self, new_content: str, new_id: str, namespace: str = None) -> list:
        """检测新记忆和已有记忆的演化关系。

        返回 [(target_id, relation, confidence), ...]
        relation: replaces / enriches / confirms
        """
        # P3.1-a：旋钮外置——Jaccard 分界 / confidence 门槛 / 极性词表从 xtmem_tuning.json 读，缺项用默认兜底
        try:
            from memory.tuning import get as _tget
            _j_rep = float(_tget("evolution", "jaccard_replace", 0.2))
            _j_enr = float(_tget("evolution", "jaccard_enrich", 0.5))
            _j_con = float(_tget("evolution", "jaccard_confirm", 0.3))
            _conf = float(_tget("evolution", "confidence_min", 0.6))
            # D1-2（2026-09-09 Spec v1.2 §D1-2）：极性词表默认值清空——兜底词表就是误报本体，
            # 留着等于 tuning 缺失时悄悄把检测打开。
            _pol = "|".join(re.escape(w) for w in (_tget("polarity", "words", []) or []))
        except Exception:
            _j_rep, _j_enr, _j_con, _conf = 0.2, 0.5, 0.3, 0.6
            _pol = ""
        # BF-1（2026-09-01）：排除刚落库的自己（SQL 层 exclude_id），避免 self-loop 自环边。
        candidates = self._vector_store.search_similar_for_evolution(new_content, top_k=5, namespace=namespace, exclude_id=new_id)
        # C-O6/V7 守卫（O3-a，P6 波1）：含 [src-raw] 句的画像块不得作为演化输入——
        # 回落补的 raw 行是 hunger 域兜底，不参与 enriches/supersedes 判定
        candidates = _filter_srcraw_candidates(candidates)
        if not candidates:
            return []

        results = []
        for target_id, target_content, _ in candidates:
            if target_id == new_id:
                # BF-1 双保险：即便 SQL 漏过自己，循环层也绝不建自环边（source_id == target_id）。
                continue
            jaccard = _jaccard(new_content, target_content)

            # ── D1-2（2026-09-09）关闭误报极性检测，严禁回填 ────────────────
            # 为什么写死 False 而不是只清词表：_pol 为空串时 re.search("", x) 对任意文本恒真，
            # 「清词表」这种关法会把 polarity_flip 从偶发误报变成 100% 误报——反向放大。
            # 该分支产的 replaces→supersedes 边 B6 抽查精度仅 25%，是 shadow_edge_backlog 的主要来源。
            # 【不】要恢复：先补黄金评测集证明精度达标，再谈打开（工单见 Spec v1.2 §D1-2/D2）。
            polarity_flip = False

            # 共同名词检测（中文 2+ 汉字词交集）
            shared_nouns = _shared_nouns(new_content, target_content)

            # D1-2：原 replaces 分支（polarity_flip and jaccard > _j_rep and shared_nouns>=2）已整体移除，
            # 退役改由显式路径产：D1-3 蒸馏退役 / 人工 forget / CLI，均走 mark_superseded(reason,by)。
            if jaccard > _j_enr:
                results.append((target_id, "enriches", jaccard * 0.8))
            elif jaccard > _j_con and shared_nouns >= 1:
                results.append((target_id, "confirms", jaccard * 0.6))

        # confidence 门控（可调）
        return [(tid, rel, conf) for tid, rel, conf in results if conf >= _conf]

    # ⚠️【已于 2026-09-16 单A24 §五（B4）整体删除】原此处有一个 78 行的
    #   `def forget(self, query, chunk_id, confirm)` 方法：两条路径都走
    #   `self._vector_store.delete_chunk()` ⇒ **物理 DELETE FROM chunks**，
    #   与本项目核心红线「忘不掉（记忆永不物理删除）」直接冲突。
    #   用户 2026-09-16 裁定「B4 用 cli 软删」⇒ 引擎这份硬删语义整体移除；
    #   删除的唯一合法路径收敛到 CLI 的 `mimox-data/xtmem/xtmem.py cmd_forget`
    #   （三段式：解析→鉴权→副作用，带 --confirm 闸 + 域校验，写 state='archived'，
    #   并补齐 retire_reason/retired_by/retired_at 退役三列 —— 与单A23 补-3 同口径）。
    #
    #   ⚠️ 删除前实测：该方法【零调用点】（全树 AST 扫 `.forget(` = 0，含 DSH/zcode 生效面
    #      与 profiles/*），故移除无行为影响。
    #   ⚠️ 刻意【保留】存储层原语 `VectorStore.delete_chunk()`：它是"删一条"的底层能力，
    #      还背着 P1-C 的 embedding_cache 清理契约（tests/test_xtmem_p1.py:409 在测它）。
    #      本单只砍"引擎里那条会硬删记忆的对外语义出口"，砍原语才是越界。
    #   🔴【勿加回】：要给引擎加删除能力，一律改调 CLI 软删路径；
    #      不要在这里复活硬删，也不要给 delete_chunk 直接对外开口。

    def improve(self) -> dict:
        """检查重复候选并重建索引；不再物理删除观察证据。"""
        duplicate_candidates = self._vector_store.dedup_by_hash()
        # 2. 重建索引
        self._hydrate_retriever()
        return {"deduped": 0, "dedup_candidates": duplicate_candidates}

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
            except Exception as exc:
                emit('engine', 'recovery', 'failed', error=exc, fallback_reason='deferred_recovery')

            # LRU cache eviction
            max_entries = int(self._cfg["compression_max_entries"])
            try:
                evicted = self._vector_store.cleanup_old_cache(max_entries=max_entries)
                wal_stats["cache_evicted"] = evicted
            except Exception as exc:
                emit('engine', 'write.index', 'degraded', error=exc)

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
        try:
            self._vector_store.close()
        finally:
            close_embedding = getattr(self._embedding, "close", None)
            if close_embedding is not None:
                close_embedding()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expected_dim(self) -> int:
        """B0-10（2026-09-11）：期望向量维度（与 _hydrate_retriever 的零向量同源，缺省 1024）。

        单一取值口径 —— 避免 store / hydrate / 校验三处各写一个数。
        """
        return int(self._cfg.get("embedding_dim", 1024))

    def _should_compress(self) -> bool:
        if not self._cfg.get("compression_enabled", True):
            return False
        max_entries = int(self._cfg["compression_max_entries"])
        ratio = float(self._cfg["compression_trigger_ratio"])
        # B0-9（2026-09-11）：计数须 active-only；全表计数会把 retired/superseded 垫高触发点。
        # 原先用 get_stats()["chunk_count"]（= SELECT COUNT(*) FROM chunks，全表不带 state），
        # 生产库实测全表 6863 而 active 仅 3965 —— 差 2898 条是已退役/被取代的，
        # 照全表算会提前约 2 个月触发 compress()，而 compress 写 WAL 的路径绕过血缘闸。
        # 此处单独查 active（不动 get_stats 语义：它是公开健康面，另有调用方）。
        try:
            _conn = self._vector_store._get_conn()
            count = _conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE state='active'"
            ).fetchone()[0]
        except Exception as exc:
            # ⚠️★O01（2026-10-13）：去掉 exc_info=True。`_should_compress()` 由 store() 与
            #   __init__ 调用，异常消息里可能带 SQL 片段/绑定值。这里只记类名——
            #   本行的告警价值是"active 计数没算成、退全表了"，不是 traceback。
            logger.warning("_should_compress: active count query failed (%s), falling back to全表",
                           type(exc).__name__)
            count = self._vector_store.get_stats().get("chunk_count", 0)
        return count > int(max_entries * ratio)

    def _write_embedding_event(self, kind: str, meta: dict) -> None:
        """独立连接写 embedding_version 相关事件（照 wal-replay 事件写法：勿动共享连接）。

        任何异常只 warning 不炸主流程——事件是取证面，检索降级才是主语义。
        """
        try:
            import sqlite3 as _sql
            _ev = _sql.connect(self._vector_store.db_path, timeout=10)
            try:
                _ev.execute(
                    "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?,?,?)",
                    (time.time(), kind, json.dumps(meta, ensure_ascii=False)),
                )
                _ev.commit()
            finally:
                _ev.close()
        except Exception as exc:
            # ⚠️★O01（2026-10-13）：去掉 exc_info=True。`_write_embedding_event` 的
            #   `meta` 里带 fingerprint 等取值（本函数内还带 stored/expected），
            #   sqlite3 的异常消息会把整条 INSERT 语句连同**绑定值**带出来——
            #   绑定值就是 meta。只记类名 + kind（kind 是内部常量字面量，安全）。
            logger.warning("xtmem event insert failed (%s): %s", kind, type(exc).__name__)

    def _check_embedding_version(self) -> None:
        """贰③c（2026-09-04）：embedding_version 锁芯启动校验（闸芯唯一写入口）。

        流程：现算当前指纹（model+dim）→ 读 xtmem_meta.embedding_version →
          无键：首启落键 + 闸开（存量库首写不误伤，与 tokenizer_version 闸同语义）；
          匹配：闸开（自愈：键值格式不规范的存量行重写成规范指纹=「健康启动自愈写回」）；
          不匹配：close_vector_leg + embedding_version_mismatch 事件（检索退 BM25-only）。

        【不】把 Ollama 不可达当成 mismatch——dim 取不到（provider 抛异常）时：不落键、
        不关闸、只 warning，下次启动再试。没有证据就断向量腿，等于拿降级当默认。
        """
        model = str(self._cfg.get("embedding_model", "bge-m3"))
        try:
            dim = int(self._embedding.dim())  # OllmaEmbedding.dim() 内部含一次性探针
        except Exception as exc:
            # ⚠️★O01（2026-10-13）：去掉 exc_info=True。embedding provider 的异常里
            #   常把**被测文本**写进消息（"failed to embed: <文本>"），traceback 会把它带出来。
            #   只记类名；model 是配置项不是用户正文。
            logger.warning(
                "embedding dim probe failed (model=%s, %s) — skip embedding_version check, "
                "vector leg stays OPEN", model, type(exc).__name__,
            )
            return
        fingerprint = make_embedding_fingerprint(model, dim)
        from memory.open_db import get_meta, set_meta
        try:
            stored = get_meta(self._vector_store._get_conn(), EMBEDDING_META_KEY)
        except Exception as exc:
            # ⚠️★O01（2026-10-13）：去掉 exc_info=True。本段读的是 xtmem_meta 里的
            #   embedding_version 键，sqlite3 异常会带 SQL/绑定值。只记类名。
            logger.warning("embedding_version read failed — check skipped: %s", type(exc).__name__)
            return
        if stored is None:
            # ① 首启落键（写入走 set_meta 的 upsert；闸保持开=现状）
            try:
                set_meta(self._vector_store._get_conn(), EMBEDDING_META_KEY, fingerprint)
                logger.info("embedding_version seeded: %s", fingerprint)
                self._write_embedding_event("embedding_version_seeded", {"fingerprint": fingerprint})
            except Exception as exc:
                # 只读降级库写不进键：闸保持开（读路径不设防是设计——open_db 不做硬闸），下次启动再落
                # ⚠️★O01（2026-10-13）：去掉 exc_info=True。这里是**写** set_meta（正则写路径），
                #   sqlite3 异常消息会带 SQL 与绑定值（键值=指纹）。只记类名。
                logger.warning("embedding_version seed write failed (readonly?): %s",
                               type(exc).__name__)
            return
        stored = str(stored)
        if stored == fingerprint:
            # ② 键匹配 → 闸开（幂等；reopen 对闸本来就是开的实例是无操作）
            self._retriever.reopen_vector_leg()
            return
        # ③ 键不匹配（含格式不可信的存量漂移行——指纹是唯一口径，读不懂的键=不可信缓存，
        # 宁可 BM25-only 也不赌）→ fail-closed
        self._retriever.close_vector_leg(
            f"embedding_version mismatch: stored={stored!r} want={fingerprint!r}"
        )
        # 健康启动自愈写回：把规范指纹写回（下次启动键已匹配=自愈完成；向量腿要等下次启动重开，
        # 一次不匹配只惩罚一个启动周期——故意的：中途翻闸会让同会话前后两跳召回口径不一致）
        try:
            set_meta(self._vector_store._get_conn(), EMBEDDING_META_KEY, fingerprint)
            logger.info("embedding_version healed: %s -> %s", stored, fingerprint)
        except Exception as exc:
            # ⚠️★O01（2026-10-13）：去掉 exc_info=True。同上（写路径 + 绑定值）。
            logger.warning("embedding_version heal write failed (readonly?): %s",
                           type(exc).__name__)
        self._write_embedding_event("embedding_version_mismatch", {
            "stored": stored, "expected": fingerprint, "action": "bm25_only_degrade",
        })

    @diagnosed('engine', 'recovery')
    def _hydrate_retriever(self) -> None:
        """Load existing chunks from VectorStore into the retriever.

        Rebuilds both BM25 and vector index.  When a cached embedding
        exists in VectorStore, it is reused directly — avoiding N cold
        Ollama HTTP calls on every startup.  Only cache misses trigger
        ``_embed()``.

        2026-08 新增：hydrate 前先 replay WAL 里未落盘的条目——
        WAL 写成功但 VectorStore 写失败时，重启后自动补写，防数据丢。
        """
        # === 贰③c（v1.3.2 工单增补，2026-09-04）：embedding_version 锁芯启动校验 ===
        # 指纹 = <model>@<dim>（make_embedding_fingerprint）。三态：
        #   无键 → 首启落键（现算 dim；Ollama 挂则跳过不落键不关闸——下次启动再试，【不】在无证据时 fail-closed）
        #   键匹配 → 闸开（健康启动自愈：历史遗留键写回规范指纹）
        #   键不匹配 → close_vector_leg（BM25-only）+ embedding_version_mismatch 事件
        self._check_embedding_version()

        # === WAL replay：未压缩未过期的条目补写 VectorStore ===
        # 幂等：hash 去重保证同内容不会重复入库存。
        # 在 _prefetch_only 设置前调用无碍——replay 只补缺失，不新增。
        try:
            cleaned = self._wal.cleanup_expired()
            if cleaned > 0:
                logger.info("WAL cleanup: removed %d expired entries", cleaned)
            elif cleaned < 0:
                # ★R4 补件：-1 = 重写已放弃（坏行没留成证，原始 WAL 保留）——
                # 这是"宁可不清也不丢证据"的降级信号，【不】当成成功清零。
                logger.warning("WAL cleanup skipped: corrupt line evidence missing (originals kept)")
        except Exception as exc:
            emit('engine', 'recovery.cleanup', 'degraded', error=exc, fallback_reason='deferred_recovery')

        try:
            pending = self._wal.read_uncompacted()
        except Exception as exc:
            emit('engine', 'recovery.wal_read', 'failed', error=exc, fallback_reason='deferred_recovery')
            pending = []

        if pending:
            # M01（2026-10-12，Spec §9.1 契约 6）：tombstone 参与重放判断——
            # 被遗忘/归档的条目即便 WAL 行还在，也不许被重放复活（"忘不掉"是核心红线）。
            # ★R6 修复（2026-10-13 验收方反例）：read_tombstones() 三态——
            #   set  → 正常判断；None → 存根文件不可读/不可信，【fail-closed 暂停本批恢复】。
            # 旧版把 None（读不动）也当空集放行，实测往有效存根前塞一行非法 UTF-8，
            # 已遗忘条目被重放复活成 active——遗忘红线被击穿。
            # ⚠️【不】要把 None 分支改回"退化成空集继续恢复"：宁可少恢复（留待修复后重启
            #   再补），也不能把用户明确遗忘的数据悄悄写回来。少恢复可查、复活不可逆。
            try:
                tombstones = self._wal.read_tombstones()
            except Exception as exc:
                emit('engine', 'recovery.replay', 'degraded', error=exc, fallback_reason='deferred_recovery')
                tombstones = None
            if tombstones is None:
                emit('engine', 'recovery.replay', 'failed',
                     fallback_reason='deferred_recovery', count=len(pending))
                logger.warning(
                    "WAL replay paused: tombstone file unreadable/untrustworthy — "
                    "%d pending entries NOT restored this round (fail-closed, R6)", len(pending))
                pending = []
            replayed = 0
            skipped_tombstoned = 0
            for entry in pending:
                try:
                    # M01：tombstone 命中 → 跳过重放（放在最前面，连 hash 都不用算）
                    if entry.id in tombstones:
                        skipped_tombstoned += 1
                        emit('engine', 'recovery.replay', 'skipped',
                             fallback_reason='deferred_recovery', count=1)
                        continue
                    # R2.2-fix（2026-08-31）：幂等合并——id 已存在于 chunks（已落库）→ 跳过，
                    # 从 chunks 保真，绝不因 WAL 缺标签而把已落库存降级成 private。
                    if self._vector_store.get_chunk(entry.id) is not None:
                        continue
                    _replay_operation = _entry_field(entry, "operation_id")
                    if _replay_operation:
                        _existing_observation = self._vector_store.find_observation(
                            str(_replay_operation), str(_entry_field(entry, "namespace") or ""),
                            str(_entry_field(entry, "project") or ""))
                        if _existing_observation:
                            if _existing_observation["memory_id"] == entry.id:
                                continue
                            emit('engine', 'recovery.replay', 'rejected', fallback_reason='deferred_recovery')
                            continue
                    content_hash = compute_content_hash(entry.content)
                    # 仅库里真没有的 id 才走兜底 + 审计事件
                    # namespace：键在且非 None 用原值；否则回落 session_id 推断（现有逻辑）
                    _ns_field = _entry_field(entry, "namespace")
                    namespace = (str(_ns_field) if _ns_field is not None
                                 else (entry.session_id if entry.session_id and entry.session_id != "default" else ""))
                    # ★R2 修复（2026-10-13 验收方反例）：旧行为 `entry.visibility or "private"`
                    #   会把「新载荷显式存的 ''/shared」误判成旧行缺字段——实测 shared→private 降级。
                    # ★第三轮再修（契约 §5）：改按「键存在 且值非 None **且值合法**」判，空串照样
                    #   当"存在"，但**非法值回落 private**——生产 WAL 里有 2 行 visibility:""
                    #   （历史脏数据），空串直接送 store_chunk 会被 normalize_visibility 拒写，
                    #   那样这两行就从"可恢复"变"不可恢复"。按"非法值回落 private"处理，
                    #   保住旧数据的可恢复性（宁可少暴露，不可丢失）。
                    _vis_field = _entry_field(entry, "visibility")
                    if _vis_field is None:
                        visibility = "private"          # 键真缺失 = 旧版本写的行
                    elif _normalize_visibility(str(_vis_field)) is None:
                        visibility = "private"          # 键在但值非法（如历史脏数据 ""）
                    else:
                        visibility = str(_vis_field)    # 含空串在内的合法值原样保真
                        # ⚠️ 上面这行看着冗余（合法值不可能为空串），但保真链条必须显式：
                        #   将来若 normalize_visibility 放宽了合法集，这里不会静默吞掉新合法值。
                    # M01 核心修复（Spec §9.1 契约 4）：重放优先用 WAL 里的字段。
                    # 此前这里写死 track=_infer_track(entry.content)（关键词硬猜）、
                    # host_origin="wal-replay"（定值）——提炼过的记忆被降级成流水、
                    # 出处被覆盖成占位串，血缘全断。现改为：
                    #   ① WAL 有值 → 用它（写入时怎么标，重启后还是怎么标，逐字还原）
                    #   ② WAL 无值（旧行）→ 才回落到原来的推断，保证旧数据仍可重放（向后兼容）
                    # ⚠️【不】要把 ① 删掉只留推断：那等于把本件修的病原样装回去。
                    # ★第三轮：判定一律走 _entry_field（存在性 + 非 None），【不】用真假值。
                    track_field = _entry_field(entry, "track")
                    if track_field is not None:
                        replayed_track = _coerce_track(str(track_field))
                    else:
                        replayed_track = _coerce_track(_infer_track(entry.content) or "default")
                    # 出处保真：WAL 记了就用 WAL 的；没记（旧行）才标成"这是重放补写的"
                    # ——把"重放"当兜底标注而不是默认覆盖，保住真实出处不被抹掉。
                    # ⚠️ 这里就是 R2 判据的核心：host_origin 显式空串**必须原样保住**，
                    #   空串不是"没值"而是"调用方明确说没有出处"，回落 'wal-replay' 就是把
                    #   已知事实洗成猜测值（round-2 实测红项：initial '' ≠ rebuilt 'wal-replay'）。
                    host_field = _entry_field(entry, "host_origin")
                    replayed_origin = str(host_field) if host_field is not None else "wal-replay"
                    # 血缘保真：WAL 记了 source_ids/derived_from 就还原（旧行为二者皆丢）
                    replayed_sources = list(_entry_field(entry, "source_ids") or [])
                    replayed_derived = str(_entry_field(entry, "derived_from") or "")
                    # ★第三轮：v2 信封字段同样按存在性回落——缺就是缺，【不】补造。
                    # provenance 缺失一律标 legacy_unverified（记忆"这条出处未经验证"），
                    # 而不是假装它有 provenance（补造 = 编事实，比缺字段更坏）。
                    _replayed_ps = _entry_field(entry, "provenance_status")
                    replayed_provenance = (str(_replayed_ps) if _replayed_ps in ("verified", "legacy_unverified")
                                           else "legacy_unverified")
                    # updated_at：键在且非 None 才采信——否则回落重放时刻。⚠️不用真假值：
                    #   0.0 是合法时间戳，`or time.time()` 会把它洗成"恢复时刻"（契约 §5）。
                    _updated_field = _entry_field(entry, "updated_at")
                    replayed_updated_at = (float(_updated_field)
                                           if _updated_field is not None else time.time())
                    # tier：键在且值 ∈{raw,refined} 才采信；否则回落 raw（契约 §5 判据表）。
                    #   非法值（历史脏数据）回落 raw，而不是把非法值送进 store_chunk 被整条拒写。
                    _tier_field = _entry_field(entry, "tier")
                    replayed_tier = (str(_tier_field) if str(_tier_field) in ("raw", "refined") else "raw")
                    chunk = Chunk(
                        id=entry.id,
                        operation_id=_entry_field(entry, "operation_id"),
                        observation_id=_entry_field(entry, "observation_id"),
                        content=entry.content,
                        timestamp=entry.timestamp,
                        importance=entry.importance,
                        tags=list(entry.tags),
                        source=entry.session_id,
                        token_count=entry.token_count,
                        content_hash=content_hash,
                        track=replayed_track,
                        namespace=namespace,
                        project=str(_entry_field(entry, "project") or ""),
                        host_origin=replayed_origin,
                        visibility=visibility,
                        # ★R2 修复：updated_at 随 WAL 保真（旧行缺字段才回落重放时刻）
                        #   旧行为根本不传 → DB 拿恢复时新时间顶替，updated_at 失真。
                        updated_at=replayed_updated_at,
                        # P4.8 T3：tier 保真（旧行缺省走 raw）——此前重放漏了 tier，
                        # 导致 refined 蒸馏记忆重启后被降级成 raw 流水（B00 探针实测五字段之一）。
                        tier=replayed_tier,
                        # M01 血缘保真（与 store 侧同口径：JSON 文本进 DB）
                        source_ids=json.dumps(replayed_sources, ensure_ascii=False),
                        derived_from=replayed_derived,
                        # ★B-补（2026-10-13）：v2 信封字段随重放一并还原进 DB。
                        #   旧的"未做"注释（"Chunk 没这些字段、不加列"）**已过期**——
                        #   Chunk 已有字段、chunks 已有列（见 schema.D_PROV_CHUNK_COLS），
                        #   所以这里必须补上，否则"崩溃重放"出来的行仍然是 DB 侧无出处的残条：
                        #   同一份 WAL 载荷，正常写入路径有值、重放路径没值 = 又一套两套口径。
                        #   ⚠️ 取值一律走 `_entry_field`（键存在且非 None 才算存在）——
                        #      旧行缺字段 → None → 落库 NULL（缺就是缺，【不】补造）；
                        #      显式空串 '' → 原样保住（空串是合法已知值，不是"缺失"）。
                        #   ⚠️ source_refs【不】在这里归一/JSON 化：它是结构化列表，
                        #      交给 store_chunk → _normalize_chunk 那唯一的序列化落点统一处理。
                        speaker=_entry_field(entry, "speaker"),
                        subject=_entry_field(entry, "subject"),
                        source_refs=(_entry_field(entry, "source_refs") or []),
                        scope=_entry_field(entry, "scope"),
                        valid_from=_entry_field(entry, "valid_from"),
                        valid_until=_entry_field(entry, "valid_until"),
                        confirmation_type=_entry_field(entry, "confirmation_type"),
                        provenance_status=replayed_provenance,
                    )
                    if not self._vector_store.store_chunk(chunk):
                        emit('engine', 'recovery.replay', 'rejected', fallback_reason='deferred_recovery')
                        continue  # Rejected writes are not recovered entries.
                    try:
                        # 用独立连接写审计事件（勿动 _get_conn 返回的共享连接）
                        import sqlite3 as _sql
                        _ev = _sql.connect(self._vector_store.db_path, timeout=10)
                        _ev.execute(
                            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
                            (time.time(), "wal-replay", namespace or "unknown", namespace or "unknown", "",
                             entry.id, json.dumps({"host_origin": replayed_origin,
                                                   "visibility": visibility,
                                                   "replayed_track": replayed_track,
                                                   "lineage_restored": bool(replayed_sources or replayed_derived)},
                                                  ensure_ascii=False)),
                        )
                        _ev.commit()
                        _ev.close()
                    except Exception as exc:
                        emit('engine', 'accounting', 'degraded', error=exc)
                    replayed += 1
                    emit('engine', 'recovery.replay', 'ok')
                except Exception as exc:
                    emit('engine', 'recovery.replay', 'failed', error=exc, fallback_reason='deferred_recovery')
            if replayed:
                logger.info("WAL replay: recovered %d entries into VectorStore", replayed)
            if skipped_tombstoned:
                # M01：被 tombstone 拦下的条目单独记账——"跳过"和"没读到"必须分得开，
                # 否则将来查"为什么这条没回来"会误判成重放丢失。
                logger.info("WAL replay: skipped %d tombstoned entries", skipped_tombstoned)

        # === 原有 hydrate 逻辑：从 VectorStore 重建检索索引 ===
        # D1-3（2026-09-09）：10_000 硬编码换成分页全量取。原写法 active 超 1 万条就静默丢尾
        # （B14 触顶），丢的部分只表现为「记了但查不到」，事后无从定位——【不】要把 limit 改回去。
        try:
            chunks = self._vector_store.iter_chunks_all(state="active")
        except Exception as exc:
            emit('engine', 'recovery', 'failed', error=exc, fallback_reason='no_context')
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
                # P0-5：完整状态复制（list_chunks 默认只取 active，这里再兜一层）
                track=ch.track,
                state=ch.state,
                superseded_by=ch.superseded_by,
                # P0.3（2026-08-31）：hydrate 灌入 namespace/visibility，供检索 viewer 过滤
                namespace=ch.namespace,
                # ★R01-FIX1（F2-B）：project 一并灌进内存文档——否则引擎侧的
                #   project 过滤位永远读到空串，等于没装。
                project=getattr(ch, "project", "") or "",
                visibility=ch.visibility,
                # P4.8 T2：分层库 tier（NULL/缺省 → raw）
                tier=getattr(ch, "tier", None) or "raw",
                # O5（P6 波3）：血缘随 hydrate 进内存（ch 是 Chunk 对象，.derived_from 属性在）
                derived_from=getattr(ch, "derived_from", "") or "",
            )
            docs.append(doc)
            # 从批量缓存中查找（P2-12：统一键函数，与 VectorStore/reembed 一致）
            from memory.utils import compute_embedding_cache_key
            text_hash = compute_embedding_cache_key(ch.content)
            cached = cache_map.get(text_hash)
            if cached:
                cached_vec = self._vector_store._deserialize_embedding(cached[0], cached[1])
                cache_hits += 1
            else:
                cached_vec = None
            pre_computed.append(cached_vec)

        # index() with pre-computed embeddings — only computes for None entries
        # 2026-09-04 抢修：cache 大面积 miss 时（>500） hydration 现算会拖 30-60 分钟
        # （CLI 每次构建 builder 都触发，表现为 store 假死）。降级：miss 条目填零向量，
        # BM25 腿照常，向量腿待 backfill（xtmem_embed_backfill.py）补齐后自然恢复。
        misses = sum(1 for v in pre_computed if v is None)
        if misses > 500:
            logger.warning(
                "hydrate: %d/%d embedding cache misses — degraded mode "
                "(BM25 only, zero-vector for misses); run xtmem_embed_backfill.py",
                misses, len(docs),
            )
            dim = self._expected_dim()
            pre_computed = [v if v is not None else [0.0] * dim for v in pre_computed]
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
                        # P1-C / R-07：hydrate 补写缓存必须带 chunk_id（孤儿反查/清删追溯）
                        chunk_id=d.id,
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

