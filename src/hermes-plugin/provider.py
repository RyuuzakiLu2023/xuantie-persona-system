# -*- coding: utf-8 -*-
"""XTMEM hermes MemoryProvider — 0.20.5 MemoryProvider ABC 实现。

设计（XTMEM-实施规格书-v1.0 §4.3）：
- 引擎 = 0.19 自建 memory/ 包（engine/，未改行数整体平移）。
- 二段式写入：ContextBuilder.store()（WAL→去重→演化→索引）+ UPDATE chunks
  补 namespace/project/host_origin/track/updated_at（schema 由 _ensure_schema 幂等迁移）。
- 检索后按 namespace/project 过滤（引擎 retriever 无 namespace 列）。
- prefetch 后台线程 + 缓存（引擎冷启动/embedding 阻塞，不能卡 turn）。
- sync_turn 仅 primary agent_context 写入，后台线程非阻塞。
- 插件零人格逻辑（D4）。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

# 插件根目录提前挂到 sys.path，让 recall_policy 用绝对导入（provider 被插件加载器
# 作为子模块加载时，相对导入 .recall_policy 会因无包 __path__ 失败）
_PLUGIN_DIR_AT_IMPORT = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR_AT_IMPORT not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR_AT_IMPORT)
from recall_policy import RecallPolicy

logger = logging.getLogger(__name__)

# 引擎目录：插件内 memory/ 包（0.19 自建 memory 包平移；目录名 memory 才能满足
# engine 源码里 'from memory.xxx' 的绝对导入；同时保留 engine/ junction 兼容规格书布局）
ENGINE_DIR = str(Path(__file__).resolve().parent / "memory")
PLUGIN_DIR = str(Path(__file__).resolve().parent)

# 规格书定稿的共享物理库（龙崎拍板：以此路径为唯一权威库）
DEFAULT_SHARED_DB = r"E:\2222\3333\mimox-data\.hermes\memory\vector_store.db"
DEFAULT_NAMESPACE = "xuantie_lark"
DEFAULT_PROJECT = ""
DEFAULT_HOST_ORIGIN = "hermes"

_PREFETCH_WAIT_SECS = 2.0       # prefetch 热路径最多等待秒数
_SYNC_TIMEOUT_SECS = 5.0
_WARMUP_TIMEOUT_SECS = 30.0


def _ensure_engine() -> None:
    """把插件目录放到 sys.path 首位，使 'memory' 解析到插件内 memory/ 包。"""
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)
    # 触发模块缓存：若已有同名 memory 包（极不可能），保持一致使用插件内引擎
    if "memory" in sys.modules and getattr(sys.modules["memory"], "__file__", ""):
        mod_file = str(Path(sys.modules["memory"].__file__).resolve())
        if not mod_file.startswith(ENGINE_DIR):
            raise RuntimeError(
                f"XTMEM engine conflict: top-level 'memory' already imported from {mod_file}"
            )
    import memory  # noqa: F401   (engine 包)


class XtmemMemoryProvider(MemoryProvider):
    """XTMEM MemoryProvider（MIMOX 引擎插件化）。"""

    def __init__(self) -> None:
        self._namespace = DEFAULT_NAMESPACE
        self._project = DEFAULT_PROJECT
        self._host_origin = DEFAULT_HOST_ORIGIN
        self._hermes_home: Optional[str] = None
        self._agent_context = "primary"
        self._platform = "cli"
        self._session_id = ""
        self._user_id = ""

        self._builder: Any = None
        self._builder_ready = False
        self._builder_error: Optional[str] = None
        self._builder_lock = threading.Lock()
        self._warmup_thread: Optional[threading.Thread] = None

        # prefetch 后台线程状态
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        self._last_prefetch_count = 0

        # sync 后台线程状态
        self._sync_lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None

        self._atexit_registered = False

        # RecallPolicy 闸门（三段漏网之一：prefetch 内自判，不依赖英文向 is_trivial_prompt）
        self._recall_policy = RecallPolicy({})
        self._context_compressed = False

    # ------------------------------------------------------------------
    # ABC 必做项
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "xtmem"

    def is_available(self) -> bool:
        try:
            _ensure_engine()
            return True
        except Exception as e:  # pragma: no cover - 环境问题
            logger.error("xtmem engine unavailable: %s", e)
            return False

    def unavailable_reason(self) -> str:
        return self._builder_error or ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._platform = str(kwargs.get("platform") or "cli")
        self._user_id = str(kwargs.get("user_id") or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary")

        # namespace 自动分域：agent_identity/profile 名
        self._namespace = (
            str(kwargs.get("agent_identity") or kwargs.get("profile") or DEFAULT_NAMESPACE)
        ).strip() or DEFAULT_NAMESPACE
        self._project = str(kwargs.get("agent_workspace") or "").strip()
        self._host_origin = f"hermes:{self._namespace}"

        # 读取 plugin 侧配置（xtmem.yaml 覆盖 memory_advanced）
        self._plugin_cfg = self._read_plugin_config()

        # 预启动 builder（后台，不阻塞 initialize）
        if not self._atexit_registered:
            atexit.register(self.shutdown)
            self._atexit_registered = True
        with self._builder_lock:
            self._builder_ready = False
            if self._warmup_thread is None or not self._warmup_thread.is_alive():
                self._warmup_thread = threading.Thread(
                    target=self._warmup_safe, daemon=True, name="xtmem-warmup"
                )
                self._warmup_thread.start()

    # ------------------------------------------------------------------
    # 核心生命周期
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """检索当前问题相关记忆；后台线程预热，热路径最多等 _PREFETCH_WAIT_SECS。"""
        if not query or not query.strip():
            return ""
        # 闸门：RecallPolicy（中文纠正边界/短追问继承/“继续”仅 never/压缩后强制）
        if not self._recall_policy.should_recall(query, context_is_compressed=self._context_compressed):
            self._context_compressed = False
            return ""
        self._context_compressed = False
        cached = self._consume_prefetch()
        if cached is not None and cached["query"] == query and cached["done"]:
            return cached["text"]
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_prefetch()
        if cached is not None and cached["query"] == query and cached["done"]:
            return cached["text"]
        # 慢路径：不注入，等 tool 兜底
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        if self._last_prefetch_count <= 0:
            return None
        return RecallStatus(provider_label="xtmem", count=self._last_prefetch_count, glyph="👁️")

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """写完一 turn → 后台 store（非 primary 上下文跳写）。"""
        if self._agent_context != "primary":
            logger.debug("xtmem sync_turn skipped (agent_context=%s)", self._agent_context)
            return
        if not (user_content or assistant_content):
            return

        def _sync() -> None:
            try:
                builder = self._ensure_builder(timeout=_WARMUP_TIMEOUT_SECS)
                if builder is None:
                    return
                content = f"User: {user_content}\nAssistant: {assistant_content}"
                meta = {
                    "importance": 0.5,
                    "tags": ["hermes", self._namespace],
                    "session_id": session_id or self._session_id or "hermes",
                    "type": "conversation",
                }
                mem_id = builder.store(content, metadata=meta)
                if mem_id:
                    self._apply_metadata(mem_id)
            except Exception:
                logger.exception("xtmem sync_turn failed")

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=_SYNC_TIMEOUT_SECS)
            if self._sync_thread and self._sync_thread.is_alive():
                return
            self._sync_thread = threading.Thread(target=_sync, daemon=True, name="xtmem-sync")
            self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # 阶段 1.2：不暴露额外工具，避免遮蔽核心工具；1.4 行为矩阵后按需扩展。
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return json.dumps({"error": f"Unknown xtmem tool: {tool_name}"})

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """压缩前提取：把即将被压缩的对话尾段带回压缩摘要（引擎提取逻辑后续细化）。"""
        if not messages:
            return ""
        tail = messages[-8:]
        lines = []
        for m in tail:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content
                )
            if content:
                lines.append(f"{role}: {str(content)[:400]}")
        return "\n\n".join(lines)[-4000:] if lines else ""

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """压缩/rewind 后强制下一次 prefetch 检索（三条漏网之三：压缩后强制检索）。"""
        if kwargs.get("reason") == "compression" or rewound:
            self._context_compressed = True
        self._session_id = new_session_id
        if reset:
            self._recall_policy = RecallPolicy({})
        with self._prefetch_lock:
            self._prefetch_result = ""
            self._prefetch_done = False
            self._last_prefetch_count = 0

    def _store_bg(self, content: str, meta: dict, *, track: Optional[str] = None,
                   project: str = "", host_origin: str = "") -> None:
        """通用后台写库（on_memory_write / on_delegation 用，非阻塞 turn）。"""
        def _run() -> None:
            try:
                builder = self._ensure_builder(timeout=_WARMUP_TIMEOUT_SECS)
                if builder is None:
                    return
                mem_id = builder.store(content, metadata=meta)
                if mem_id:
                    self._apply_metadata(mem_id, track=track,
                                         project_override=project or None,
                                         host_origin_override=host_origin or None)
            except Exception:
                logger.exception("xtmem bg store failed")

        threading.Thread(target=_run, daemon=True, name="xtmem-bg-store").start()

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """S2 挂点：内建 MEMORY.md 写入旁路镜像（显式记忆 → 共享库）。

        0.20.5 内建 MemoryStore 写 MEMORY.md 不走 sync_turn；
        这里把 action=add 的显式记忆镜像进 XTMEM 共享库（namespace 自动分域）。
        """
        if action != "add":
            return
        text = (content or "").strip()
        if not text:
            return
        self._store_bg(
            text[:4000],
            {
                "importance": 0.7,
                "tags": ["hermes", "explicit_memory", str(target or "memory")],
                "session_id": self._session_id or "hermes",
                "type": "explicit_memory",
                "target": str(target or "memory"),
            },
            track="explicit",
            project=str(target or "memory"),
            host_origin=f"hermes:{self._namespace}:memory-write",
        )

    def on_delegation(self, task: str, result: str, *,
                      metadata: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        """S2 挂点：父侧观察子代理工作（sync_turn 跳过非 primary，这里补回）。"""
        text = f"子代理任务：{str(task or '')[:800]}\n结果：{str(result or '')[:2400]}"
        if not text.strip():
            return
        self._store_bg(
            text[:3000],
            {
                "importance": 0.5,
                "tags": ["hermes", "delegation"],
                "session_id": self._session_id or "hermes",
                "type": "delegation",
            },
            track="delegation",
            project=self._project,
            host_origin=f"hermes:{self._namespace}:delegation",
        )

    def backup_paths(self) -> List[str]:
        """S2 挂点：把共享库（+WAL 伴生）纳入 hermes backup。"""
        paths: List[str] = []
        dp = str(getattr(self, "_db_path", "") or "")
        if dp and os.path.exists(dp):
            paths.append(dp)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(dp + suffix):
                paths.append(dp + suffix)
        wal = os.path.join(os.path.dirname(dp), "wal", "current.wal") if dp else ""
        if wal and os.path.exists(wal):
            paths.append(wal)
        return paths

    def shutdown(self) -> None:
        for t in (self._warmup_thread, self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=3.0)
        with self._builder_lock:
            builder = self._builder
            self._builder = None
            self._builder_ready = False
        if builder is not None:
            try:
                builder.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # builder 管理
    # ------------------------------------------------------------------

    def _warmup_safe(self) -> None:
        # 直接构建（不要走 _ensure_builder 的“等待 warmup 线程”分支，否则自等 30s）：
        # 这是 warmup 线程本身，等自己永远等不到。
        try:
            with self._builder_lock:
                if self._builder is None:
                    self._builder = self._build_builder()
                    self._builder_ready = True
        except Exception as e:
            self._builder_error = str(e)
            logger.exception("xtmem warmup failed")

    def _ensure_builder(self, timeout: float = 0.0) -> Any:
        with self._builder_lock:
            if self._builder is not None and self._builder_ready:
                return self._builder
        # warmup 线程内部调用：直接构建
        if threading.current_thread() is self._warmup_thread:
            with self._builder_lock:
                if self._builder is None:
                    try:
                        self._builder = self._build_builder()
                        self._builder_ready = True
                    except Exception as e:
                        self._builder_error = str(e)
                        raise
                return self._builder
        # 其他线程：等待 warmup 线程
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._builder_lock:
                if self._builder is not None and self._builder_ready:
                    return self._builder
            time.sleep(0.05)
        # 超时：若 warmup 还没完成，自行尝试构建（非阻塞调用方尽量不走到这）
        with self._builder_lock:
            if self._builder is not None and self._builder_ready:
                return self._builder
            if self._builder is None:
                try:
                    self._builder = self._build_builder()
                    self._builder_ready = True
                except Exception as e:
                    self._builder_error = str(e)
                    raise
            return self._builder

    def _build_builder(self) -> Any:
        _ensure_engine()
        from memory.context_builder import ContextBuilder

        home = Path(self._hermes_home or DEFAULT_SHARED_DB).resolve() or Path(".")
        db_path = self._plugin_cfg.get("db_path", "").strip()
        if not db_path:
            db_path = os.environ.get("XTMEM_DB_PATH", "").strip()
        if not db_path:
            db_path = DEFAULT_SHARED_DB

        flat = self._flatten_memory_advanced(self._plugin_cfg.get("memory_advanced", {}))
        flat.setdefault("db_path", db_path)
        self._db_path = db_path
        # 插件进程内：REST 观测线程默认关（多 profile/多实例不抢 20000）
        flat.setdefault("rest_api", False)
        try:
            flat.setdefault("prefetch_only", False)
        except Exception:
            pass

        builder = ContextBuilder(
            workspace_dir=str(home),
            context_window=int(self._plugin_cfg.get("context_window", 128_000)),
            config=flat,
        )
        # 幂等 schema：加 namespace/project/host_origin/updated_at + xtmem_state
        self._ensure_schema(builder)
        return builder

    def _read_plugin_config(self) -> Dict[str, Any]:
        """读配置：config.yaml 的 memory_advanced + $HERMES_HOME/xtmem.yaml 覆盖。

        xtmem.yaml 可选键：db_path / namespace / project / context_window /
        memory_advanced（覆盖 config.yaml 同名字段）。
        """
        cfg: Dict[str, Any] = {}
        if not self._hermes_home:
            return cfg
        try:
            import yaml
            cfg_path = Path(self._hermes_home) / "config.yaml"
            if cfg_path.exists():
                raw_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                if isinstance(raw_cfg, dict) and isinstance(raw_cfg.get("memory_advanced"), dict):
                    cfg["memory_advanced"] = raw_cfg["memory_advanced"]
        except Exception as e:
            logger.warning("config.yaml memory_advanced unreadable: %s", e)
        xtmem_path = Path(self._hermes_home) / "xtmem.yaml"
        if xtmem_path.exists():
            try:
                import yaml
                raw = yaml.safe_load(xtmem_path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    cfg.update(raw)
            except Exception as e:
                logger.warning("xtmem.yaml unreadable: %s", e)
        return cfg

    def _flatten_memory_advanced(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """把 config.yaml 的 memory_advanced 嵌套段拍平成 ContextBuilder 期望的扁平键。"""
        flat: Dict[str, Any] = {}
        _nest_map = {
            "embedding": {
                "model": "embedding_model",
                "base_url": "embedding_base_url",
                "timeout": "embedding_timeout",
            },
            "retrieval": {
                "top_k": "retrieval_top_k",
                "token_budget": "retrieval_token_budget",
                "relevance_threshold": "relevance_threshold",
            },
            "compression": {
                "enabled": "compression_enabled",
                "max_entries": "compression_max_entries",
                "trigger_ratio": "compression_trigger_ratio",
            },
        }
        _scalars = {"enabled", "prefetch_only", "db_path", "rest_api"}
        for key, value in raw.items():
            if key in _scalars:
                flat[key] = value
            elif key in _nest_map and isinstance(value, dict):
                for sub_key, flat_key in _nest_map[key].items():
                    if sub_key in value:
                        flat[flat_key] = value[sub_key]
        return flat

    # ------------------------------------------------------------------
    # schema 与二段式写入
    # ------------------------------------------------------------------

    def _ensure_schema(self, builder: Any) -> None:
        """幂等迁移：chunks 增列 + xtmem_state + 索引（规格书 §4.1）。"""
        conn = builder._vector_store._get_conn()
        for col, col_type, default in (
            ("namespace", "TEXT", "'xuantie_lark'"),
            ("project", "TEXT", "''"),
            ("host_origin", "TEXT", "''"),
            ("updated_at", "REAL", "NULL"),
        ):
            cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
            if col not in cols:
                conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} {col_type} DEFAULT {default}")
        # 存量 450 条（旧共享库）按规格书归属 xuantie_lark 域
        conn.execute(
            "UPDATE chunks SET namespace='xuantie_lark' WHERE namespace IS NULL OR namespace=''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_ns ON chunks(namespace, project)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS xtmem_state (
                side TEXT PRIMARY KEY,
                last_files TEXT DEFAULT '{}',
                last_sync REAL
            )
            """
        )
        conn.commit()

    def _apply_metadata(self, chunk_id: str, track: Optional[str] = None,
                        project_override: Optional[str] = None,
                        host_origin_override: Optional[str] = None) -> None:
        """二段式补刀：UPDATE namespace/project/host_origin/track/updated_at。"""
        builder = self._builder
        if builder is None:
            return
        conn = builder._vector_store._get_conn()
        sets = ["namespace=?", "project=?", "host_origin=?", "updated_at=?"]
        params: List[Any] = [self._namespace,
                             project_override if project_override is not None else self._project,
                             host_origin_override if host_origin_override is not None else self._host_origin,
                             time.time()]
        if track:
            sets.append("track=?")
            params.append(track)
        params.append(chunk_id)
        try:
            conn.execute(f"UPDATE chunks SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()
        except Exception:
            logger.exception("xtmem metadata update failed for %s", chunk_id)

    def _ns_map(self) -> Dict[str, str]:
        """id -> namespace 映射（检索后过滤用；小规模库直接全量读）。"""
        builder = self._builder
        if builder is None:
            return {}
        try:
            conn = builder._vector_store._get_conn()
            rows = conn.execute("SELECT id, namespace, project FROM chunks WHERE state='active'").fetchall()
            return {r["id"]: (r["namespace"] or DEFAULT_NAMESPACE, r["project"] or "") for r in rows}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # prefetch 后台线程
    # ------------------------------------------------------------------

    def _consume_prefetch(self) -> Optional[Dict[str, Any]]:
        with self._prefetch_lock:
            if not self._prefetch_done:
                return None
            out = {
                "query": self._prefetch_query,
                "text": self._prefetch_result,
                "done": True,
            }
            self._prefetch_result = ""
            self._prefetch_done = False
            return out

    def _start_prefetch(self, query: str) -> None:
        with self._prefetch_lock:
            if self._prefetch_query == query and self._prefetch_done:
                return
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run() -> None:
            text = ""
            count = 0
            try:
                builder = self._ensure_builder(timeout=_WARMUP_TIMEOUT_SECS)
                if builder is not None:
                    ns_map = self._ns_map()
                    top_k = int(builder._cfg.get("retrieval_top_k", 10)) or 10
                    budget = int(builder._cfg.get("retrieval_token_budget", 2000))
                    results = builder._retriever.search(query, top_k=top_k)
                    lines: List[str] = []
                    used = 0
                    for idx, (doc, score) in enumerate(results):
                        if score < 0.01:
                            continue
                        entry = ns_map.get(doc.id)
                        if entry is None:
                            # 未标 namespace 的老数据：视为本端默认域（避免新库老数据丢失）
                            ns_ok = True
                            project_ok = True
                        else:
                            ns_ok = entry[0] == self._namespace
                            project_ok = (not self._project) or (entry[1] == self._project)
                        if not (ns_ok and project_ok):
                            continue
                        trust = "[TRUSTED]" if "memory_md" in (doc.tags or []) else "[UNVERIFIED]"
                        tokens = (
                            builder._retriever._est(doc.content)
                            if hasattr(builder._retriever, "_est")
                            else len(str(doc.content).strip()) // 3
                        )
                        if used + tokens > budget:
                            remaining = budget - used
                            if remaining > 50:
                                snip = str(doc.content).strip()[: remaining * 2] + " [...]"
                                lines.append(f"{len(lines)+1}. {trust} {snip.strip()}")
                            break
                        lines.append(f"{len(lines)+1}. {trust} {str(doc.content).strip()}")
                        used += tokens
                    if lines:
                        text = "[XTMEM MEMORY]\n" + "\n".join(lines)
                        count = len(lines)
            except Exception:
                logger.exception("xtmem prefetch failed for query=%r", query[:80])
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = text
                    self._prefetch_done = True
                    self._last_prefetch_count = count

        t = threading.Thread(target=_run, daemon=True, name="xtmem-prefetch")
        with self._prefetch_lock:
            self._prefetch_thread = t
        t.start()


def register(ctx) -> None:
    """memory provider 插件注册（loader 的 register 优先路径）。"""
    ctx.register_memory_provider(XtmemMemoryProvider())
