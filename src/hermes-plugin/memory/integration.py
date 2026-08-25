import logging, functools, os

logger = logging.getLogger(__name__)


def _flatten_memory_config(raw: dict) -> dict:
    """Flatten the nested ``memory_advanced`` config into flat keys.

    ``config.yaml`` uses a nested structure for readability::

        memory_advanced:
          embedding:
            model: bge-m3
          retrieval:
            top_k: 10

    :class:`ContextBuilder` expects flat keys (``embedding_model``,
    ``retrieval_top_k``).  This helper does the one-time translation.
    """
    flat: dict = {}
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
    # Top-level scalar keys pass through directly.
    _scalars = {"enabled", "prefetch_only", "db_path"}
    for key, value in raw.items():
        if key in _scalars:
            flat[key] = value
        elif key in _nest_map:
            if isinstance(value, dict):
                for sub_key, flat_key in _nest_map[key].items():
                    if sub_key in value:
                        flat[flat_key] = value[sub_key]
    return flat


def init_context_builder(agent, config=None):
    """Initialize the MIMOX advanced memory context builder.

    Parameters
    ----------
    agent:
        The AIAgent instance (must have ``workspace_dir`` and
        ``context_length`` attributes).
    config:
        Optional dict with ``memory_advanced`` overrides.  If
        omitted, :class:`ContextBuilder` uses its built-in defaults.
    """
    if hasattr(agent, '_context_builder') and agent._context_builder:
        return agent._context_builder
    try:
        from memory import ContextBuilder
        workspace = getattr(agent, 'workspace_dir', '.')
        ctx_window = getattr(agent, 'context_length', 128_000)
        flat_config = _flatten_memory_config(config) if config else None
        builder = ContextBuilder(
            workspace_dir=workspace,
            context_window=ctx_window,
            config=flat_config,
        )
        agent._context_builder = builder

        # 注册到 memory_manager
        memory_manager = getattr(agent, '_memory_manager', None)
        if memory_manager:
            memory_manager._context_builder = builder

        # Inject MEMORY.md curated entries into the builder's retriever
        # so the advanced prefetch can search them alongside VectorStore chunks.
        try:
            from memory.hybrid_retriever import MemoryDoc
            import hashlib, time
            store = getattr(agent, '_memory_store', None)
            if store and store.memory_entries:
                injected = 0
                cache_hits = 0
                vs = builder._vector_store
                for entry in store.memory_entries:
                    # Parse status prefix from stored entry
                    raw = entry.strip()
                    if raw.startswith("[confirmed] "):
                        ent_status, ent_content = "confirmed", raw[len("[confirmed] "):]
                    elif raw.startswith("[refuted] "):
                        ent_status, ent_content = "refuted", raw[len("[refuted] "):]
                    elif raw.startswith("[uncertain] "):
                        ent_status, ent_content = "uncertain", raw[len("[uncertain] "):]
                    else:
                        ent_status, ent_content = "confirmed", raw

                    doc = MemoryDoc(
                        id=hashlib.md5(ent_content.encode()).hexdigest()[:12],
                        content=ent_content,
                        timestamp=time.time(),
                        status=ent_status,
                        importance=0.8,
                        tags=["memory_md"],
                    )
                    # Try cached embedding first — avoid Ollama HTTP per entry
                    cached_vec = vs.get_cached_embedding(ent_content)
                    builder._retriever.docs.append(doc)
                    builder._retriever.bm25.add(doc.content)
                    if cached_vec is not None:
                        builder._retriever._embeddings.append(cached_vec)
                        cache_hits += 1
                    else:
                        vec = builder._retriever._embed(doc.content)
                        builder._retriever._embeddings.append(vec)
                        # Cache for next startup
                        try:
                            vs.cache_embedding(
                                text=ent_content,
                                embedding=vec,
                                model=builder._cfg.get("embedding_model", "bge-m3"),
                            )
                        except Exception:
                            pass  # non-fatal
                    injected += 1
                if injected:
                    logger.info(
                        "Injected %d MEMORY.md entries into context builder retriever "
                        "(%d cache hits)",
                        injected, cache_hits,
                    )
        except Exception:
            logger.warning("Failed to inject MEMORY.md into retriever", exc_info=True)

        logger.info("Context builder initialized (window=%d)", ctx_window)
        return builder
    except Exception as e:
        logger.warning("Failed to init context builder: %s", e)
        agent._context_builder = None
        return None


def wrap_run_conversation(original_fn):
    """包装 run_conversation，在调用前初始化上下文构建器

    注意：original_fn 可能是绑定方法（bound method），
    也可能是普通函数。需要处理这两种情况。
    """
    @functools.wraps(original_fn)
    def wrapped(*args, **kwargs):
        # 检查第一个参数是否是 agent（绑定方法 vs 普通函数）
        if args and hasattr(args[0], 'run_conversation'):
            # 这是绑定方法，第一个参数是 self（agent）
            agent = args[0]
            args = args[1:]
        elif 'agent' in kwargs:
            # 通过关键字参数传递的 agent
            agent = kwargs.pop('agent')
        else:
            # 没有 agent 参数，直接调用
            return original_fn(*args, **kwargs)

        # 初始化上下文构建器
        builder = init_context_builder(agent)

        # ContextBuilder 暂无 build() — 直接透传
        pass

        # 调用原始函数，重新组合参数
        result = original_fn(agent, *args, **kwargs)

        # Auto Dream hook — 每次对话结束后检查
        try:
            _check_auto_dream()
        except Exception as e:
            logger.debug("Auto dream check skipped: %s", e)

        return result

    return wrapped


def _resolve_memory_root() -> str:
    """解析 memory 根目录路径。"""
    return os.path.join(os.getcwd(), "memory")


def _check_auto_dream():
    """检查并触发 Auto Dream/Distill，在后台派发子 Agent 执行。"""
    try:
        from memory.auto_dream import AutoDream
        root = _resolve_memory_root()
        dream = AutoDream(root)

        if dream.should_run("dream"):
            logger.info("Auto Dream triggered — dispatching background consolidation")
            dream.mark_completed("dream")
            _dispatch_dream_task(dream, "dream")

        if dream.should_run("distill"):
            logger.info("Auto Distill triggered — dispatching skill discovery")
            dream.mark_completed("distill")
            _dispatch_dream_task(dream, "distill")
    except Exception as e:
        logger.debug("Auto dream check skipped: %s", e)


def _dispatch_dream_task(dream, kind: str):
    """在后台线程中派发 Dream/Distill 子 Agent。

    使用 delegate_task 以 leaf 角色派发，只给只读工具，
    不限时运行，静默失败不影响主对话。
    """
    try:
        from tools.delegate_tool import delegate_task
        import threading

        task_prompt = (
            dream.get_dream_task() if kind == "dream"
            else dream.get_distill_task()
        )

        def _run():
            try:
                delegate_task(
                    goal=task_prompt,
                    context=(
                        "Background maintenance task. Run silently — "
                        "do not ask questions or request user input. "
                        "Use read-only tools only. Write a brief summary "
                        "of what was done as the final output."
                    ),
                    role="leaf",
                    background=True,
                    tool_whitelist=["file", "web"],
                    max_iterations=30,
                )
            except Exception:
                logger.debug("%s background task failed silently", kind)

        thread = threading.Thread(target=_run, daemon=True, name=f"auto-{kind}")
        thread.start()
    except Exception as e:
        logger.debug("Failed to dispatch %s task: %s", kind, e)
