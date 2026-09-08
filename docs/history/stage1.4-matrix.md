# XTMEM 阶段 1.4 · 行为矩阵 v1

> 依据：XTMEM-实施规格书-v1.0 §6 行为矩阵五件套 / §7 阶段 1.4；hermes升级-补丁验尸与路线-v1 §2.1（三文件 494/441/113 行）、§6.3、§7。
> 原料：context_compressor/memory_tool/system_prompt 的 prod-vs-019 与 v021-vs-019 六份 diff + 实读 v0.21/0.19/生产三套源码。
> 符号：**保**=保留移植；**弃**=丢弃（附参数级核对）；**改**=改造适配新版结构。

---

## §0 基线机制（原版 0.19 已有、prod 未改，但升级重写须逐项验收防止静默丢失）

| 机制 | 旧版行为 | 新版等价物 | 决策 | 参数核对/理由 | 建议落点 |
|---|---|---|---|---|---|
| 结构化摘要模板（官方 13 段）| `## Goal / Constraints & Preferences / Completed Actions / Active State / Blocked / Key Decisions / Resolved Questions / Relevant Files / Critical Context` + Historical(任务/进行中/待问/剩余) 段 | v0.21 保留同模板，新增 focus_topic 引导（相关主题给 60-70% 摘要预算）与迭代更新（PREVIOUS SUMMARY 合并） | 改 | 段落结构 0.19→0.21 一致（v0.21 模块 docstring 明记）；仅加 focus 倾斜与迭代 | 直接用 v0.21 官方模板，XTMEM 无需自写 |
| 压缩阈值 0.5 / target 0.2 | threshold_percent=0.50，summary_target_ratio=0.20 | 同为 0.50/0.20；新增 per-model 覆盖（resolve_model_threshold）、threshold_tokens_cap、小窗下限（<512K 触发 75%） | 保 | 与 spec §2 数值吻合；v0.21 有效阈值可能被 per-model 覆盖，需回归 | 用官方默认，别覆盖 |
| 保护 20+3 | protect_first_n=3、protect_last_n=20 | 相同默认；新增 _effective_protect_first_n（压缩周期衰减）、_MAX_TAIL_MESSAGE_FLOOR（防尾部锁死） | 保 | prod 与 v0.21 均 3/20 | 官方默认 |
| 摘要失败冷却 | _summary_failure_cooldown_until、_consecutive_timeout_failures、DB 持久化 cooldown、abort_on_summary_failure | v0.21 保留并增强（cooldown 持久化、feasibility_skip 不计失败、失效清理） | 保 | prod diff 未触碰（基线）；v0.21 同族 | 用官方实现 |
| 确定性 fallback 摘要（LLM 不可用）| _fallback deterministic handoff，硬顶 _FALLBACK_SUMMARY_MAX_CHARS | v0.21 保留 handoff 与 summary-unavailable 落点 | 保 | 基线 | 用官方实现 |

---

## §1 agent/context_compressor.py（prod-vs-019 新增：L2/L3 + poisoning + 缓存）

| 机制 | 旧版行为 | 新版等价物 | 决策 | 参数核对/理由 | 建议落点 |
|---|---|---|---|---|---|
| L2 软 Skip（低相关度）| l2_enabled=False；l2_warn_ratio=0.40；l2_score_threshold=0.30；对非保护消息批量 LLM 打 0-1 相关分，<0.30 标记 `_skip_l2_low_priority` 并从 messages 移除；三层降级 LLM→bge-m3→lexical；按内容 md5 缓存 | 无等价（v0.21 grep l2/l3 均为 0）。v0.21 有 proactive prune（确定性 tool-result 修剪）与 micro-compaction（滚动微压缩）与 focus_topic 引导 | 弃 | 参数核对：l2_enabled=False（默认关闭，零生产影响）、l2_score_threshold=0.30、l2_warn_ratio=0.40 在 v0.21 均不存在。理由：硬迁会与官方 proactive prune/micro-compaction **双重压缩**、重复丢弃，且每轮 LLM 打分成 `call_llm(task=compression)` 高成本/延迟；官方已用确定性修剪+微压缩+focus 取代"软跳过" | 不移植。如未来确需"软跳过"，迁移到插件 provider 侧（prefetch 前 / on_pre_compress 前做相关性过滤），**勿进官方 ContextCompressor** |
| L3 元 Skip（低信息值）| l3_enabled=False；l3_trigger_ratio=0.55；l3_skip_threshold=3.0；非保护消息 0-10 价值分，<3.0 丢弃；批量打分+逐条降级+缓存；注释"仅确认决策点调用" | 无等价 | 弃 | 默认关闭；官方无 0-10 价值分概念；proactive prune 只处理 tool_result、不走 LLM 价值判断 | 同 L2，不移植 |
| 三层降级打分（LLM→embedding→lexical）| _relevance_scorer/_meta_skip_judge/_batch_score_messages 用 call_llm(compression)，失败→bge-m3 cosine（embedding_provider）→token Jaccard | 无等价；官方 focus_topic 直接进 summary prompt，不逐条打相关分 | 弃 | embedding_provider=None 默认；bge-m3 在 engine 有，但 context_compressor 不接 | 不移植 |
| 缓存与命中率监控 | _relevance_cache/_meta_cache 按 md5(text)[:16] 跨轮缓存；_embed_cache；_log_cache_rate 每 100 次 log | 无等价；v0.21 用 _proactive_prune_rearm_tokens 做 prompt-cache 防抖（属不同机制） | 弃 | _cache_log_threshold=100 为 MIMOX 私有 | 不移植 |
| 保护索引集 _protected_index_set | system + 头 protect_first_n + 尾 protect_last_n 永不跳过 | v0.21 保护逻辑更细（head decay、tail floor、dedup tail-agnostic） | 保（逻辑对齐） | 复用 protect_first_n=3/protect_last_n=20；v0.21 更完善 | 用官方保护 |
| 自动焦点主题 _derive_auto_focus_topic | prod 压缩前用其推导 query 供 L2/L3 打分 | v0.21 保留同名函数（5508/7618），并作为 summary_focus_topic 输入（相关主题 60-70% 预算） | 保 | 原义在 v0.21 更强 | 用官方 focus_topic |
| Poisoning 检测 _detect_poisoning | 对最近 5 条 assistant 消息用 auxiliary model 打 0-1 分，>0.7 视为脱轨 | 无等价（v0.21 grep poison 仅注释，无实现） | 弃 | 生产代码 **无任何调用点**（仅定义处），是死代码；>0.7 无开关 | 弃，不迁移 |

### §1.1 专项：ContextCompressor 重写说明（问题 5）

- **v0.21 确有新机制**：`native_compaction.py`（原生压缩检查点/审计）、**proactive prune**（`proactive_prune_tokens=0` 默认关、min_result_chars=8000、min_reclaim_tokens=4096、成功后才 rearm 直到重新长满触发区间，旨在先回收旧 tool_result）、**micro-compaction**（`_micro_compact_*`：rolling_summary、every_n_turns=1、defrag_threshold_tokens=2000、连续失败计数）、**pre-LLM feasibility skip**、**focus_topic 引导摘要**、**per-model threshold 覆盖**、**threshold_tokens_cap**。
- **MIMOX 的 L2/L3 软跳过与 poisoning 检测在新版没有落地位**：v0.21 不提供逐条消息的相关度/价值分接口，也不提供 poisoning 判分接口。  
- **建议**：二者一律**弃**（默认关闭 + 官方已有确定性替代 + 死代码）。若想保留"软跳过"的能力，迁到 **插件 provider 侧**：在 `prefetch` 前按 RecallPolicy 过滤、或在 `on_pre_compress(messages)->str` 里做"压缩前提取/筛选"（官方 ABC 305 行 + conversation_compression.py 明示通知 memory providers 走 on_pre_compress/on_session_switch）。**不要**把 L2/L3 再打进官方 ContextCompressor，否则与 proactive prune/micro-compaction 双重触发、且每次改写 message 都会破坏 prompt-cache 前缀。

---

## §2 tools/memory_tool.py（prod-vs-019 新增：三态/LRU/档案/retrieve/同步索引）

| 机制 | 旧版行为 | 新版等价物 | 决策 | 参数核对/理由 | 建议落点 |
|---|---|---|---|---|---|
| 状态三态 confirmed/refuted/uncertain | STATUS_PREFIX + _format_entry/_parse_status；无前缀→confirmed 兼容；_render_block 丢弃 refuted、uncertain 标 [?] | v0.21 无状态概念（grep status/refuted/uncertain=0；enum 仅 add/replace/remove） | 改 | 三字符串在 schema enum 中；无参数数值 | 迁 XTMEM provider 的 CoreMemory 工具（get_tool_schemas/handle_tool_call）或 xtmem CLI；built-in MEMORY.md 无状态 |
| 受保护前缀（12 项）| `_AUTO_EVICT_PROTECTED_PREFIXES`：群聊配置/Coding任务/MIMOX实例/知识库位置/目录结构/工作习惯/Cron规则/MIMOX配置路径/项目说明/技能/工作流/Coding 永不淘汰 | 无 | 弃 | 该列表是 MIMOX 对 file 快照的保全；v0.21 无 LRU；D2 规定 450 条引擎库为权威、文件只是渲染物 | 弃；若需保全走 provider 引擎的 namespace/保护元数据 |
| LRU 自动淘汰 auto_evict | __init__ auto_evict=True；_evict_to_fit 超限时淘汰最旧**非保护**条目到 memory_archive.md；add/replace 超限先 evict 再报错 | 无（v0.21 无 auto_evict/_evict_to_fit/archive） | 弃 | auto_evict=True 默认、archive_path=None、archive_retention_days=30 为 config 可覆盖；v0.21 官方靠 _consolidation_failure + replace/remove 提示，**无自动淘汰** | 弃；如需自动淘汰放到 provider/engine 或 cron，不进官方 memory_tool |
| 档案回收站 memory_archive.md + 30 天保留 | _append_to_archive 写 `[archived: YYYY-MM-DD HH:MM]`；_prune_archive 按 retention 删除过期 | 无 | 弃 | archive_retention_days=30、_DEFAULT_ARCHIVE_NAME=memory_archive.md；v0.21 无档案 | 弃 |
| 写入拦截器 memory_interceptor | add() 先 intercept_memory_write(target,content,context_builder) 再 threat 扫描 | 官方仍用 _scan_memory_content / first_threat_message（注入/外泄扫描）；无 memory_interceptor | 改 | memory_interceptor 为自建工具 | 并进 provider sync_turn/handle_tool_call 前；或弃（官方 threat 扫描已覆盖） |
| 去重（raw strip status）| add/apply_batch 用 {_parse_status(e)[1]} 集合去重，避免 status 前缀误判 | v0.21 官方原文 exact 去重（无 status） | 弃 | status 弃后回归官方 exact 去重 | 弃 |
| replace 保留旧状态 | status=None 保留旧条状态；有则更新 | 无 | 弃（随三态） | | 随三态迁移 |
| _render_block 过滤 + 表头变更 | refuted 不出现在 prompt；uncertain 标 [?]；表头改 "USER PROFILE (who the user is)"/"MEMORY (your personal notes)" | v0.21 仍用 MEMORY_BLOCK_HEADERS['user'/'memory']，无状态过滤 | 改 | 表头为文案；状态功能迁 provider | built-in 用官方表头；XTMEM 记忆块由 provider `system_prompt_block()` 渲染（可自带表头） |
| get_memory_store 配置项 | 读 config 的 auto_evict/archive_path/archive_retention_days（默认 True/None/30） | v0.21 读 memory.memory_char_limit/user_char_limit + memory_enabled/user_profile_enabled；无 auto_evict/archive | 弃 | 参数核对：auto_evict=True、archive_path=None、30 天在 v0.21 无对应；config 键不同 | 弃；char 上限与 enabled 用官方键 |
| _sync_retriever（写后同步 HybridRetriever 索引）| add/replace/remove/batch/apply_staged 成功后，把 MemoryStore 条目同步为 memory.hybrid_retriever.MemoryDoc（status、md5 id、now timestamp） | 无（v0.21 built-in memory_tool 无 _retriever/HybridRetriever，grep=0） | 改 | 语义检索在引擎库；官方无此索引 | 迁 provider prefetch/handle_tool_call；built-in memory_tool 不加 |
| retrieve 检索动作 | action=retrieve，query 必填，limit 默认 5（schema 描述 max 20），retriever.search(top_k=limit)，结果 content+score(round3)；schema 加 query/limit、WHEN TO USE | 无（官方 enum 仍 add/replace/remove；无 retrieve） | 改 | limit 默认 5、schema 说 max 20；官方无此动作 | 迁 provider 工具（xtmem_recall / lookup --cross）；built-in memory_tool 不加 |
| target=null 归一化删除 | prod 删除 `if target is None: target="memory"`（严格 provider 传 null 会报 invalid target） | v0.21 **保留**该归一化（l.1122） | 弃（回退官方） | target null→memory；prod 删除是偏离，官方保留 | 用 v0.21 官方行为 |
| schema/文案（ACTIONS、WHEN TO USE、status、query/limit、consolidation error 用格式化后长度）| 新增上述说明与参数 | v0.21 官方 schema 无 retrieve/status | 改 | status/retrieve 迁 provider 后，built-in schema 用官方 | 用官方 |

---

## §3 agent/system_prompt.py（prod-vs-019 新增：核心记忆块 + 好感度段 + 时间戳）

| 机制 | 旧版行为 | 新版等价物 | 决策 | 参数核对/理由 | 建议落点 |
|---|---|---|---|---|---|
| 核心记忆块（第 2 层）| `agent._core_memory.render()` 非 "（无核心记忆）" 则 volatile 追加 "## 核心记忆（LLM 可编辑）\n" + block | v0.21 无 _core_memory 块；改用 external memory provider block（memory_manager.build_system_prompt()，l.842-846） | 改 | 0.20.5 agent_init 55 行 CoreMemory 补丁被 provider 化取代，agent 上无 _core_memory | 由 XTMEM provider 实现 `system_prompt_block()`；**不要**重打 agent_init |
| 好感度阶段段（8 阶段）| _build_affection_block 读 HERMES_HOME/affection/affection.yaml 的 score+milestones，映射 8 阶段（初始/认识/熟悉/在意/嘴硬阈值/变软/破防边缘/终点），含 desc/behavior/tone（「哼」「才不是」频率）、下一里程碑提示、fail-open | v0.21 无 affection（grep=0）；新提供 PluginSystemPromptSection + provider system_prompt_block | 改 | 参数：8 阶段边界 10/25/42/60/75/90/100；口癖频率（初始 30/20、认识 40/20、熟悉 50/30、在意 60/40、嘴硬阈值 80/50、变软 40/25、破防 20/10、终点 0/0）；fail-open | 迁 provider system_prompt_block 或 **PluginSystemPromptSection**，并**按 profile 过滤（仅 xuantie_lark）**；与 D4「插件零人格」冲突需拍板（见风险） |
| 时间戳格式 | `%A, %B %d, %Y %H:%M %Z`（加分钟+时区缩写） | v0.21 保留 `%A, %B %d, %Y`（日期，TZ 用 IANA/缩写/UTC offset）+ Session/Model/**Provider/Platform** | 弃（分钟）/ 改（整体） | 参数：`%H:%M` 与官方 byte-stable 注释直接冲突（分钟精度会使每个重建路径失效前缀缓存）；v0.21 有意去分钟；prod 未加 Provider/Platform | 采用 v0.21：去 `%H:%M`，用官方时间戳（tz offset + Provider/Platform） |
| external memory provider block | 既有：`if agent._memory_manager: ...` 追加 provider block | v0.21 保留并以 memory_manager.build_system_prompt() 聚合 | 保 | XTMEM provider 注入 system prompt 块的原生通道 | 用官方通道 |
| PluginSystemPromptSection | 无 | v0.21 新增 _frozen_plugin_prompt_sections / _restore_plugin_prompt_sections / _plugin_section_blocks（恢复时冻结字节，不重跑插件） | 改 | 插件可注册 system prompt 段 | 若 XTMEM 要用插件段挂好感度/核心记忆块，优先走此通道 |

---

## §4 新→旧 双向冲突核对（问题 3）

| 冲突点 | 旧(prod) | 新(v0.21) | 是否叠加/冲突 | 处置 |
|---|---|---|---|---|
| 双重压缩 | L2/L3 软跳过（默认关）| proactive prune + micro-compaction 每轮都在压 | 若把 L2/L3 移植会**双重丢弃/双重改写**（软跳过删消息 + proactive prune 改 tool_result + micro-compaction 滚动摘要），并反复破坏 prompt-cache | 弃 L2/L3；需软跳过只做 provider 侧 pre-filter |
| 缓存冲突 | MIMOX _relevance_cache/_meta_cache 按 md5(text) 跨轮 | v0.21 _proactive_prune_rearm_tokens + model_config 持久化做防抖 | 两套缓存不同键、不同域；若共存无直接冲突，但软跳过改写会令 rearm 计数失真 | 弃 MIMOX 缓存 |
| prompt-cache 稳定性 | 时间戳带 `%H:%M`（分钟精度，每次重建都会坏缓存）| 官方刻意日期+tz（byte-stable）| **直接冲突**：官方注释明确分钟精度使每个重建路径失效前缀缓存 | 弃分钟，用官方 |
| 记忆渲染 | built-in MemoryStore 有状态过滤/表头/retrieve | 官方无状态、header 用 MEMORY_BLOCK_HEADERS | 不冲突（built-in 该功能弃/迁）；XTMEM 语义检索走 provider | built-in 用官方 |
| strict provider null target | prod 删了 null→memory 归一化 | 官方保留 | 旧版行为反而偏离官方；null target 会报 invalid | 回退官方 |
| 记忆引擎权威 | MIMOX 让文件快照做"第二层"，写后同步 HybridRetriever | D2：库是唯一权威；provider 来写 | 不冲突，但 built-in file 快照角色在 XTMEM 下被降级 | 自动淘汰/retrieve 归 provider，built-in 只留硬顶快照 |

---

## §5 风险与待拍板

1. **D4（插件零人格）vs 好感度段**：provider system_prompt_block 对全局 memory.provider=xtmem 生效，会注入到 novel 等所有 profile，违反 D4。建议好感度段走 **PluginSystemPromptSection**（可按 profile 条件）或留 profile 级补丁；需龙崎裁定。
2. **核心记忆块静默丢失**：v0.21 agent 上无 `_core_memory`，若不实现 provider `system_prompt_block()`，"核心记忆（LLM 可编辑）"段会悄悄消失。验收加一条"压缩重构后系统 prompt 仍含核心记忆段"。
3. **built-in MemoryStore vs provider 分工**：三态/LRU/档案/retrieve 是 MIMOX built-in 特性；provider 化后 built-in MemoryStore 与 provider 并存，需拍板 built-in 是否保官方最小形态、全部记忆能力归 provider（`xtmem_recall`/store/status）。这决定"弃/改"落点。
4. **double-compression**：已判弃 L2/L3，风险解除；但若未来有人在 provider prefetch 前做 soft-skip，需与官方 proactive/micro 对齐触发条件，避免 provider 侧删了消息、官方又压一遍。
5. **时间戳**：确定弃 `%H:%M`，用官方日期+tz；这是唯一"改"到官方字节稳定约定的点。
6. **_detect_poisoning 死代码**：弃，零风险。
7. **归档/自动淘汰**：若还想要 built-in 快照自动整理，需另设计（engine/cron），因官方无 archive/evict 通道；否则弃。
8. **端到端剧本**：须跑一条"长对话触发压缩 → 问旧事 → 仍能答出"用例（压缩后强制检索信号在 0.20.5 是 per-turn micro-compaction，挂点实测确认后再落矩阵）。
