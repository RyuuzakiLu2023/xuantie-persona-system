# XTMEM 实施规格书（Spec）v1.1

> **使用说明（给执行本 spec 的 AI/新会话）**：本文档自包含，涵盖 XTMEM 项目全部背景、已定稿决策、组件规格与落地步骤。经 2026-08-25 实地勘查验证；部分数值转引自用户侧勘验（已标注）。⚠️ 环境为活环境：并行会话/审计可能推进实况（如 mimox.py 已自愈），冲突时以实况为准并回写本 spec。用户（龙崎）零代码基础、纯 vibecoding——技术实施全权由你执行；破坏性操作（升级切换、删除、覆盖）前须向她确认。**所有架构决策已定稿（标注"拍板"），不要重新讨论；发现规格与实况冲突时停下来报告，不要自行改道。**
> 冲突裁决顺序：本文 > 《hermes升级-补丁验尸与路线 v1.3》> 《通用记忆系统-同步实施方案 v2.0（内容已至 v2.3，含 v2.4 记录）》> 《XTMEM方案-v2.0评估.md》。

---

## 1. 项目定义与第一目标

**XTMEM = 通用记忆系统。第一目标（2026-08-25 龙崎终稿）：多角色 AI 天团——各端独立人格（江北/玄铁/D指导/Z老师），共享记忆，能互动。**

拆成两层（v2.3 起）：
- **记忆 = 水，全楼通**：任何端、任何 profile/preset/模式都读写同一座共享库，各写各的 namespace 域；`lookup --cross` 跨角色互查；
- **人格 = 各自房间（独立，互不同步）**：江北（WorkBuddy）/ 玄铁（hermes xuantie_lark）/ D指导（DSH）/ Z老师（zcode）各保持各自原生人设，XTMEM 插件**零人格逻辑**，不做任何人格分发。
- **羁绊（可选升级，第三层）**：好感度体系从"玄铁单人"扩展为多角色关系网。

## 2. 环境事实表（实测，直接引用勿重查）

| 项 | 值 |
|---|---|
| OS | Windows（Git Bash），AMD RX6750GRE（仅 Vulkan） |
| 共享记忆库 | `<XT_ROOT>\mimox-data\.hermes\memory\vector_store.db`（SQLite，450 条，WAL，FTS5+bge-m3@Ollama:11434；memory_advanced 已启用） |
| 记忆孤岛 | `E:\hermes-webui (2)\.hermes\memory\vector_store.db`（**1503 条**，止于 2026-07-29，7 月底前历史——待 absorb 收编） |
| 在用 hermes 源码 | `<XT_ROOT>\hermes-agent-main-v2\hermes-agent-main`（0.19.0 + 自建 memory/ 包 + ~2300 行官方文件改动，**无 git**） |
| 原版 0.19 / 新源码 | `E:\git项目源码\hermes-agent-main0.19`（diff 基准）/ `E:\git项目源码\hermes-agent-mainv0.21`（**实际 0.20.5**，升级目标） |
| mimox 启动器 | `<XT_ROOT>\mimox\mimox.py`（设 HERMES_HOME=mimox-data；⚠️ 其内硬编码 v1 老路径待盘点） |
| 玄铁人格母本 | `<XT_ROOT>\mimox-data\profiles\xuantie_lark\SOUL.md`（玄铁亲笔维护，自动快照 20 份） |
| 好感度 | `mimox-data\affection\affection.yaml`（79.5 分，已两端共享）+ `quotes.yaml`（8 阶段台词）+ `memory_bridge.py`/`affection_bridge.py` |
| DSH | xuantie profile 在 `<USER_HOME>\.dsh\profiles\xuantie\`（xuantie-webui 插件 1142 行，runBridge 模式 68 行起）；preset 机制样例 `.dsh\.agent-presets\liangshen\`（AGENT-PLANE/realm 隔离） |
| zcode | 记忆目录 `~/.zcode/cli/memories/projects/<name>-<hash16>\memory\`（kebab-case 文件+[[双链]]索引）；**插件体系**：`.zcode-plugin/plugin.json`（兼容 .claude-plugin），组件 commands/skills/hooks/mcpServers/userConfig，本地 marketplace 可装 |
| cron 任务群 | `mimox-data/cron/jobs.json` **13 个任务**（记忆维护 6 + 知识代谢 1 + 会话杂项 6），ticker 心跳活跃 |
| 关键常量 | MEMORY.md 硬顶 2200 字 / USER.md 1375 字 / 压缩 threshold 0.5、target 0.2、protect 20+3 |
| 飞书卡片包 | hermes_feishu_card（外部包，位置待运行时定位）；挂载点补丁清单 `.hermes_feishu_card_manifest`（sha256） |

## 3. 架构决策（全部拍板，不再讨论）

| # | 决策 | 一句话依据 |
|---|---|---|
| D1 | **人格各端独立**（江北/玄铁/D指导/Z老师各是各的，互不同步），XTMEM 零人格逻辑 | 龙崎拍板 2026-08-25（v2.3 取代原单源分发） |
| D2 | 记忆物理共库；文件只是渲染物（可从库重建） | 库是唯一权威，永不打架 |
| D3 | **记忆全局通**：hermes 插件装全局层（全 profile）、DSH 工具挂 profile 级（全 preset/模式）、zcode 插件（全 workspace） | 龙崎拍板"所有模式可用"；切 preset 不丢记忆 |
| D4 | （v2.3 废弃）~~人格分层/他端渲染~~ → 各端独立人格 | 独立人格取代 |
| D5 | DSH 工具层全局挂 profile 插件；**不注入任何人格**（各 preset 保持各自人设） | 工具与人设解耦 |
| D6 | XTMEM 双形态：xtmem.py CLI（外部端）+ hermes 记忆插件（进程内），共用同一份 engine/ | 一处维护两种形态 |
| D7 | 升级与同步合流：先升级（引擎平移成插件）后同步，一条时间线 | 引擎平移=插件化=同步地基 |
| D8 | MIMOX 引擎（0.19 自建 memory/ 包 458K/19 文件）整体平移，450 条零迁移 | 自有资产与官方代码零冲突 |
| D9 | 命名统一：CLI `xtmem.py` / hermes 插件 `xtmem` / DSH 工具集 `xtmem-tools`（profile 级）/ zcode 插件 `xtmem` / 工具前缀 `xtmem_*` / 表 `xtmem_state` | 龙崎拍板 |
| D10 | AutoDream 挂点 = hermes cron（新增 `xtmem-autodream` 每周任务）；定时 sync 同走 cron 体系 | 13 任务先例+心跳活跃 |
| D11 | zcode 走官方插件（MCP+hooks），文件轨降为可选 | 绕开 hash16 目录坑；三端工具名一致 |
| D12 | **开发-测试-手动切换三段式**：改动全在新源码目录做，测试文件留存，全部验收后由龙崎亲自替换生产目录；测试期写库用副本 | 龙崎拍板（详见 §7 工作流总则） |

## 4. 组件规格

### 4.1 共享库 schema（幂等，保 450 条不动）

```sql
ALTER TABLE chunks ADD COLUMN namespace TEXT DEFAULT 'xuantie_lark';
ALTER TABLE chunks ADD COLUMN project TEXT DEFAULT '';
ALTER TABLE chunks ADD COLUMN host_origin TEXT DEFAULT '';
ALTER TABLE chunks ADD COLUMN updated_at REAL;
CREATE INDEX IF NOT EXISTS idx_chunks_ns ON chunks(namespace, project);
CREATE TABLE IF NOT EXISTS xtmem_state (
  side TEXT PRIMARY KEY,          -- zcode:<project> | dsh:<project> | ...
  last_files TEXT DEFAULT '{}',   -- {relpath: mtime}
  last_sync REAL
);
```

**写入二段式（禁止绕过引擎直写）**：① ContextBuilder.store() 走完整管线（WAL→去重→演化→索引）→ ② `UPDATE chunks SET namespace=?, project=?, host_origin=?, track=?, updated_at=? WHERE id=?`（track 合并补刀——引擎 `_infer_track()` 硬编码忽略 metadata 标签）。检索后过滤 namespace/project。

> **修订（2026-08-31，P0-4 落地）**：二段式已废除 → **单事务写入**。
> `ContextBuilder.store(metadata={namespace, project, host_origin, visibility, track, updated_at})` 一次 INSERT 落全部列；
> provider/CLI 均已删除 `_apply_metadata` 补刀（死代码清零）。schema 统一由 `memory/schema.py` 幂等迁移：
> chunks 增列 namespace/project/host_origin/updated_at/**visibility** + `idx_chunks_ns`/`idx_chunks_vis` 索引
> + relations/messages/xtmem_state/xtmem_events 表（provider/CLI/引擎共用一份，杜绝两套 schema 漂移）。

**namespace 自动分域**：hermes 插件按 initialize 的 `agent_identity`（profile 名）定域——novel profile 写 novel 域，xuantie_lark 写 xuantie_lark 域。

### 4.2 xtmem.py CLI

- `<XT_ROOT>\mimox-data\xtmem\xtmem.py`，零依赖单文件，Python 3.11
- 手法：`sys.path.insert(HERMES_ROOT)` import 引擎（memory_bridge.py 同款；spawn 时 rest_api 必须关闭——多实例抢 20000 端口仅 warning）；升级前 HERMES_ROOT 指向现生产源码，升级后指向插件 engine/（用环境变量 `XTMEM_HERMES_ROOT` 覆盖）
- 命令：`migrate` / `store` / `observe` / `recall` / `lookup --cross`（跨全 agent/project，结果带 [namespace/project] 标签）/ `forget`（软删+WAL 清痕）/ `export --zcode`（匹配 `<name>-<hash16>` 已存在目录，非 mem- 条目整行保留含 [[双链]]）/ `import --zcode`（台账增量）/ **`absorb --db <孤岛库> --namespace hermes-webui`**（1503 条收编，原库只读保留）/ `sync` / `agents.md`（v2.1）——**无 soul 命令**（v2.3 独立人格）
- 防循环三件套：xtmem_state 台账增量 + import 带 host_origin 自痕迹跳过 + 文件名分区（`mem-` 前缀=XTMEM 生成区，非 `mem-`=zcode/用户区）
- jieba 必须在依赖里（中文 FTS5 分词，缺失静默降级单字+bigram）

### 4.3 hermes 插件（0.20.5 MemoryProvider 实现）

- **位置（拍板 D3）：全局插件目录 `mimox-data/plugins/xtmem/`（预计，⚠️ 步骤 1.1 必须实测 HERMES_HOME 真实落点后确认——mimox-data/plugins/ 与 profiles/xuantie_lark/plugins/ 两目录并存，放错层=部分 profile 不可见）**
- 结构：`__init__.py`（class XtmemMemoryProvider(MemoryProvider)，目录含 MemoryProvider 字样即被识别）+ `provider.py` + `engine/`（0.19 memory/ 包平移）+ 可选 plugin.yaml（仅 name/version/description/pip_dependencies:[jieba]，**无 kind 字段**）+ 可选 config_schema.py
- 激活：**全局 config**（mimox-data/config.yaml）加 `memory.provider: xtmem`（单选唯一引擎；profile config 不单独设）
- 注册器事实：四源扫描 bundled > user($HERMES_HOME/plugins/) > project > pip entry_points；bundled-wins；builtin MEMORY.md/CoreMemory 与外部 provider 并存
- **provider 职责三栏（定稿）**：

| 必做 | 弃 | phase 3 再议 |

> S2 注记（2026-08-25 审计）：ABC 另有可选挂点 on_memory_write / on_delegation / on_turn_start / on_session_switch / shutdown / backup_paths。其中 **on_memory_write 与 on_delegation 已由 XTMEM provider 实现**（显式记忆镜像/子代理观测）；on_turn_start/backup_paths 保默认（backup_paths 已返回共享库路径）。
|---|---|---|
| initialize（hermes_home/agent_identity→namespace） | _detect_task_completion（完成→压缩） | 完成检测的现代等价物 |
| prefetch（含 RecallPolicy 闸门：中文纠正边界匹配/短追问继承/"继续"仅 never） | 四失败模式 | 演化事件台词（娱乐） |
| sync_turn（agent_context≠primary 跳写；后台线程化——store 含 bge-m3 embedding 是阻塞的） | demand 追踪 | |
| on_pre_compress（压缩前提取，ABC 原生方法 memory_provider.py:305） | idle 触发 | |
| get_tool_schemas/handle_tool_call（**2026-08-31 定案：返回空=设计决策——工具由 DSH xtmem-tools / zcode MCP 承接，避免双轨；CoreMemory 未接入，引擎侧已标注死代码**） | | |
| recall_status（UI"👁️ recalled N memories"） | | |
| on_session_end（**已实现 2026-08-31**：清总线缓存防串味 + 置压缩强制检索 + 记 session_end 事件；AutoDream 主触发仍=cron） | | |

- **四条漏网已认领（不许静默丢失）**：RecallPolicy 闸门（prefetch 内自判，不依赖官方英文向 is_trivial_prompt）、压缩后强制检索（0.20.5 conversation_compression.py 注释明示"notifies memory providers"，挂点实现时确认）、AutoDream 7 天链（cron 主挂点）、**S2 审计新认领：on_memory_write（内建 MEMORY.md 写入镜像）与 on_delegation（子代理观测）——provider 已实现（见 2.1/阶段2 验收）**
- provider 实现三坑：prefetch 有 external_prefetch_timeout——引擎冷启动检索须后台线程+缓存；工具名不得遮蔽核心工具（xtmem_* 无冲突 ✅）；recall_status 返回 glyph/provider_label

### 4.4 DSH 接入（工具/注入两层分离）

| 层 | 挂载 | 范围 |
|---|---|---|
| 工具层 | `<USER_HOME>\.dsh\profiles\xuantie\xtmem-tools\`（profile 级插件，照 xuantie-webui 安装方式；web profile 可选同装）。三工具 `xtmem_recall/xtmem_store/xtmem_lookup_cross` = spawn xtmem.py（runBridge 模式） | 该 profile 下**所有 preset/会话/玄铁所有模式** |
| 注入层 | **分角色记忆摘要注入（仅本角色 namespace，不 cross）**；不注入 SOUL/USER 人设（零人格 D5） | 仅本端会话（记忆摘要另入系统提示词非人格） |

**角色→域映射（分角色注入规则，2026-08-25 用户拍板）**：hermes=profile 名（agent_identity 自动）；DSH 工具层默认域 `dsh`（D指导，可配 `namespace`）；zcode 域 `zcode`（Z老师）。注入永远**只取本角色域**；跨角色必须用工具显式查询（recall/lookup --cross 或 xtmem_lookup_cross），不自动注入他人记忆。

cwd→project 走映射表（显式配置，禁裸名兜底）。不动：xuantie preset、xuantie-webui、memory_bridge.py。AGENT-PLANE realm 隔离约束照 liangshen 范例，估 3~4 天。

### 4.5 zcode 接入（官方插件三层，v2.3 删人格层）

| 层 | 组件 | 作用 |
|---|---|---|
| ① MCP | `mcpServers`：**Python minimal stdio MCP**（stdlib JSON-RPC 子集，spawn xtmem.py——不赌 zcode 端 node 环境） | 三工具直连共享库，绕开 hash16 坑 |
| ② hooks | UserPromptSubmit 检索注入（**仅 zcode 本域**）/ Stop observe | manifest 声明即生效 |
| ③ 文件轨（可选） | export/import 投影（匹配已存在 `<name>-<hash16>` 目录；hash16 公式已被用户侧破解：sha256(path.lower())[:16]） | 与内建 Memory 互通 |

- 人格（Z老师）：zcode 保持自己现有 AGENTS.md/插件 persona，XTMEM 插件**不注入任何人格**（v2.3）
- 插件源码目录 `<XT_ROOT>\xtmem-zcode-plugin\`，本地 marketplace 安装。manifest 名 `xtmem`（正则 `^[a-z0-9][a-z0-9._-]{0,127}$`）。

### 4.6 各端独立人格 与 孤岛收编（v2.3）

- **人格：不做任何同步**——江北/玄铁/D指导/Z老师各保持各自原生人设（WorkBuddy SOUL、hermes profile SOUL、DSH preset persona、zcode AGENTS），XTMEM 插件零人格逻辑
- USER.md 可选共享（同一个龙崎）
- absorb：阶段 2.7 执行（1503 条，namespace=hermes-webui）

## 5. 三大接口点约束（0.20.5 实测，实施前精读源码对应处）

1. **MemoryProvider ABC**（agent/memory_provider.py，类内 21 方法/全文件 def 22——2026-08-25 审计实测，原 19 系转引未亲验）：接线入口已收敛到 run_agent.py AIAgent turn 生命周期（build_turn_context prefetch 门 + queue_prefetch_all/sync_all/on_session_end/shutdown_all）——**0.19 的 conversation_loop 306 行接线全部废弃，不再碰 conversation_loop**；initialize kwargs 含 agent_identity/profile、agent_context（primary/subagent/cron/flush）、user_id/chat_id 等
2. **hermes_state 重写**（7822→14637+四卫星）：agent_init 两版均零直接引用，影响走间接（session_title kwargs、prompt 快照恢复收编、"压缩后强制检索"信号位置变了须实测）；agent_init 55 行 CoreMemory 补丁**不需重打**（provider 化取代）
3. **注册器**（hermes_cli/plugins.py 2466→6782 行）：四源+bundled-wins+单外部 provider；记忆插件靠目录约定（__init__.py 含 MemoryProvider 字样），plugin.yaml 无 kind

## 6. 行为矩阵五件套（阶段 1.4 操作规程）

context_compressor(494)/memory_tool(441)/system_prompt(113) 合并时：① 从 diff 每个 hunk **反推生成**矩阵（禁止凭记忆列——隐性行为静默丢失）；② 条目拆到**机制级**（阈值/保护数/摘要结构/失败冷却/prompt 重建）；③ **双向对照**（旧→新保留/弃 + 新→旧启用/关闭/冲突——防新版微压缩与保留摘要叠加双重压缩）；④ "弃"格子过**参数级核对**（2200/1375/0.5/0.2/20/3）；⑤ **端到端剧本用例**（长对话压缩后问旧事）。矩阵=验收单，diff=原料，缺一不可。

## 7. 落地步骤（每步独立验收，用户确认后走下一步）

### ⭐ 工作流总则（D12，龙崎拍板 2026-08-25：开发-测试-手动切换三段式）

1. **所有改动只在 `E:\git项目源码\hermes-agent-mainv0.21`（新源码）上进行**——它是开发环境；生产目录 `<XT_ROOT>\hermes-agent-main-v2\hermes-agent-main` 在切换前**一律不动**（玄铁现链路持续在跑）。
2. **每步改动配测试，测试文件永久留存**（`E:\git项目源码\hermes-agent-mainv0.21\tests\` 下命名 `xtmem_*.py`，参照既有 test_p1_features.py 惯例）——它们是后续升级 0.21 的回归资产。
3. **全部改动验收通过后，由龙崎亲自执行源码替换**（新源码目录 → 原目录位置；AI 不擅自切换）。替换前 AI 须先出一份"切换清单"（替换步骤 + 回滚方法 + 路径硬编码改点核对），龙崎点头才算。
4. **测试期数据保护**：涉及写库的测试一律用 `vector_store.db` 的**副本**（拷到临时路径，引擎 db_path 指副本）；纯读测试可用真库。生产库在切换完成前不接任何新写入。
5. 测试跑新源码的方式：临时启动器（仿 mimox.py，HERMES_HOME 指向独立测试数据目录或 mimox-data 只读场景），实施时定，原则=不污染生产。

### 阶段 0 · 准备（半天）
| 步 | 动作 | 验收 |
|---|---|---|
| 0.1 | git init 收编在用版源码 + 原版 0.19/0.20.5 存档 + .gitattributes 统一换行符（**diff 一律 --strip-trailing-cr，CRLF 会虚报**） | baseline commit |
| 0.2 | （已降级）hash16 公式确认（用户侧已破解 sha256(path.lower())[:16]，4/4 命中）——文件轨（可选）直接用 |

### 阶段 1 · 升级工程（2~3 天）
| 步 | 动作 | 验收 |
|---|---|---|
| 1.1 | **实测 HERMES_HOME 真实落点**（硬前置：跑 mimox 后查 get_hermes_home()；确认全局层 vs profile 层扫描关系）+ 定位 hermes_feishu_card 包 + 盘点多套源码统一 | HERMES_HOME 明确，插件落点定案 |
| 1.2 | MIMOX 引擎平移成全局插件（engine/ + provider.py + __init__.py） | 新 hermes 启动无错 |
| 1.3 | 精读 mem0 _backend.py 参照，调通 provider 接线（含三坑处理） | 450 条可检索 |
| 1.4 | 行为矩阵五件套合并三文件 | 矩阵全绿+剧本通过 |
| 1.5 | 路径硬编码统一（mimox.py/memory_bridge.py/xuantie-webui/**cron 记忆整理任务的 prompt 路径**→新源码，全部指向新目录的**待切换值**） | 测试启动器全链路复活 |
| 1.6 | 挂载点按 manifest 重打（sha256 校验）+ 中国平台用官方版合并定制 + **13 个 cron 任务逐个存活确认** | 飞书卡片/cron 正常 |
| 1.7 | 全局 config 迁移（memory.provider: xtmem；memory_advanced 段转插件配置） | 无配置警告，全 profile 生效 |
| 1.8 | **汇总测试报告 + 切换清单**（替换步骤/回滚方法/路径核对表/13 任务清单）→ **龙崎亲自执行源码替换**（新源码目录 → 原目录） | 切换后玄铁全链路正常，旧目录归档保留 |

### 阶段 2 · 同步工程（3~4 天）
| 步 | 动作 | 验收 |
|---|---|---|
| 2.1 | 写 xtmem.py（全命令含 absorb） | migrate 后 450 条 namespace=xuantie_lark 不变 |
| 2.2 | DSH profile 插件层挂载（工具层 + 记忆摘要注入层；零人格注入——D5） | **任意 preset**（含梁神模式）xtmem_recall 都能查到 |
| 2.3 | （v2.3 删除）~~soul --sync 人格分发~~——独立人格，零人格分发 | 各端人格保持原生，不做任何同步 |
| 2.4 | zcode 插件（MCP+hooks，v2.3 三层） | zcode 问"记得我的部署偏好"能答出 |
| 2.5 | zcode hooks 调通 | "记住 X" → lookup --cross 查到 |
| 2.6 | （可选）文件轨投影 | 台账无循环 |
| 2.7 | **absorb 孤岛 1503 条**（namespace=hermes-webui，原库只读保留） | lookup --cross 查到 7 月历史 |
| 2.8 | **WorkBuddy 接入**（江北第四端，工具型）：建 `~/.workbuddy/skills/xtmem/SKILL.md`（store/recall/lookup --cross 用法），namespace=workbuddy | 江北能写共享库、跨角色查玄铁/D指导/Z老师 |
| 2.9 | hermes cron：`xtmem-sync`（5 分钟）+ `xtmem-autodream`（每周） | 台账收敛、蒸馏发生 |

### 阶段 3 · 娱乐性（1~2 天，按兴致）
多角色互动（lookup --cross 互查 + 好感度关系网按 agent 分账）/ AutoDream 各角色腔调汇报 / replaces 脸红台词（玄铁专属）/ 跨角色成就 / status_panel 跨端投影。

## 8. 风险与对策（TOP）

| 风险 | 对策 |
|---|---|
| HERMES_HOME 放错层 | 1.1 硬前置实测 |
| provider 接口细节 | 1.3 精读 mem0 参照 |
| 1.4 重写级合并 | 行为矩阵五件套（§6） |
| 行为矩阵五坑 | 完备性/粒度/双向/参数/交互（§6） |
| 钩子漏网静默丢失 | 三条已认领（RecallPolicy/压缩后检索/AutoDream），新发现按同流程认领 |
| CRLF/LF | --strip-trailing-cr + .gitattributes |
| jieba 缺失 | plugin.yaml pip_dependencies |
| project 弱匹配 | mappings 显式配置 |
| 长驻进程 hydrate 延迟 | 分钟级可接受；v2.1 TTL 重 hydrate |
| 0.21.0 在路上 | 插件 API 已稳定（9 官方先例），顺带升 |
| 多角色互动的一致性 | v2.3 独立人格——各端人设各异本就是要的"差异"，非缺陷；共享记忆保证"知道彼此干过啥"即可 |
| 孤岛数据噪音 | 1503 条含 "User: [Workspace::v1:…" 前缀噪音（实测）；**当前策略=原样保留（保真优先）**；如需清洗（去前缀/归一化）须拍板后再改 absorb |

## 9. 待拍板项（仅剩）

| # | 事项 | 状态 |
|---|---|---|
| 1 | 升级与同步合并推进（阶段 1→2 串行） | 建议已给（推荐合并），待龙崎最终点头 |
| 2 | 阶段 3 多角色互动做哪些 | 按兴致；玩法增强 E1-E6 见独立 spec（核心完成后启动） |
| 3 | 同步频率 5 分钟 | 默认即可，跑了再说 |
| 4 | 规格书老声称对账（CoreMemory 更新/追加工具、provider 工具暴露） | ✅ 2026-08-31 已按「设计决策 + 死代码标注」处置并回写本规格书；如需从正文彻底删除旧描述再拍板 |

---

## 附录

- **A 命名**：见 D9。MIMOX=引擎血统称呼（0.19 自建包），mimox-data=数据目录历史名不改。
- **B 禁改清单**：xuantie preset / xuantie-webui 插件 / memory_bridge.py / affection 全套 / quotes.yaml / 各 profile SOUL.md（XTMEM 插件零人格逻辑）/ 共享库现有 450 条。
- **C 参考文档**（同目录）：《hermes升级-补丁验尸与路线-v1.md》（升级工程权威，§6.3 插件形态/§七 钩子裁剪）、《通用记忆系统-同步实施方案-v2.0.md》（v2.3，本 spec 母本）、《多角色玩法增强 spec》（v2.4 起独立）、《XTMEM-skill-sync方案-v1.0.md》（同目录）、《XTMEM方案-v2.0评估.md》（用户侧评估，含拍板 A/B/C）、《通用记忆系统-方案评估-v1.0.md》（早期验证）、《玄铁记忆系统架构文档 .md》（<XT_ROOT>\new\，玄铁亲笔，注意 daily_log_heartbeat"死代码"结论已被 cron 任务实证推翻待修正）。

*江北（WorkBuddy）整合 · 2026-08-25 · 素材：全天全链路实地勘查 + 龙崎六轮拍板（XTMEM 命名/记忆全局通/人格分层→独立人格/第一目标核对→多角色天团/开发测试切换）*

---

## 10. 修复与安全对账（2026-08-28~31，审计驱动）

> 依据：《XTMEM审计报告-问题与修复建议-20260828.md》《XTMEM-P0修复验证报告-20260831.md》。
> 全部修改已通过回归（verify_p0 46/46、P0 回归 29 项、P1 回归 15 项、P2 回归 10 项、CLI/插件/zcode smoke）。

### 10.1 P0（2026-08-28，均验收通过）

| # | 修复 | 现状 |
|---|---|---|
| P0-1 | 身份固定：DSH=dsh / zcode=zcode（XTMEM_ACTOR 环境身份优先，外部 `--as` 不可伪造）；非 cross 召回限本域；有身份时写入限本域 | ✅ 跨角色 private 不再泄露 |
| P0-2 | 注入统一防线：全文+归一化+全角/西里尔同形+中文关键词四重检测；store/msg/import/absorb/MCP/hook 全部同入口；注入内容标注"历史记忆非指令" | ✅ 7 种穿透变体全拦截 |
| P0-3 | 拒写不谎报：store_chunk 返回 bool、引擎先校验再进 WAL、拒绝抛 ValueError | ✅ 无幽灵数据 |
| P0-4 | 正文+metadata 单事务 INSERT（删二段式补刀） | ✅ 见 §4.1 修订 |
| P0-5 | forget/dedup/delete 按 rowid 同步 FTS；旧 external-content FTS 自动重建为独立表；hydrate 复制完整 state 且只取 active | ✅ FTS 幽灵行=0 |

### 10.2 验证报告新发现（2026-08-31）

| # | 问题 | 修复 |
|---|---|---|
| N1 | WAL 路径三处不一致（CLI workspace 双嵌套 .hermes） | `_builder_workspace()` 推导统一；`_purge_wal_entry` 与引擎同路径；孤儿目录已归档 |
| N2 | CLI store 无身份时可越域写入 | 有身份（XTMEM_ACTOR/--as）时 ns≠actor 拒绝 |
| N3 | 全新空库 migrate 报 no such table | `_ensure_schema` 缺表时先由引擎建基础表 |

### 10.3 P1（2026-08-31，均验收通过）

| # | 项 | 现状 |
|---|---|---|
| P1-7 | 声称对账：get_tool_schemas 空=设计决策；on_session_end 已实现；AutoDream/集成旧资产标注死代码 | ✅ 代码侧完成；规格书同步见 §4.3/§9-4 |
| P1-8 | sync_turn 有界 FIFO（maxsize=8）+ 常驻 worker + 失败重试 1 次 + 满队告警计数 | ✅ 不再忙时静默丢 turn |
| P1-9 | WAL 跨进程锁：`<wal_dir>/.lock` 统一字节锁；append 尽力、mark_compressed/cleanup_expired/purge 强锁 | ✅ 并发重写零丢失 |
| P1-10 | REST 鉴权：`XTMEM_REST_TOKEN`；非 loopback 无 token 拒绝启动；/api/* + PATCH token 校验；state/track 枚举 | ✅ |
| 附 | AutoDream 梦境只蒸馏 visibility='shared'（private 不进公共文件） | ✅ |

### 10.4 P2（2026-08-31，均验收通过）

| # | 项 | 现状 |
|---|---|---|
| P2-11 | content hash 统一：import/absorb 改走 `compute_content_hash`（MD5），与引擎 store 一致 | ✅ 同内容跨路径去重命中 |
| P2-12 | embedding cache key 统一：`compute_embedding_cache_key`（全文 sha256）共用；reembed 不再截前 2000 字 | ✅ 长文本不再重复计算 |
| P2-13 | `_ns_map` 按命中 ids 查询（IN 子查询），不再每次 prefetch 全表扫描 | ✅ |
| P2-14 | 死代码归档：`memory/_archived/`（integration/auto_dream/core_memory）；semantic_dedup/recall_with_fallback 标注保留 | ✅ 可回滚 |

### 10.5 环境修复（2026-08-31）

- Ollama 假 502 根因：Windows 系统代理注册表残留（ProxyEnable=1 → 已死的 127.0.0.1:7890）把 httpx 的 loopback 请求吞掉。已关（ProxyEnable=0）+ 引擎 `trust_env=False` 永久绕开 + 60s 故障黑名单冷却。
- hermes 重启后新 provider 生效：生产库 chunks_fts 已自动重建为独立 FTS（2926 条一致）。

### 10.6 测试资产（永久留存，`mimox-data/xtmem-project/tests/` + hermes/XTMEM 仓库 tests/）

`xtmem_cli_smoke.py`（16 项）/ `xtmem_plugin_smoke.py` / `xtmem_p0_regression.py`（29 项）/ `xtmem_p1_regression.py`（15 项）/ `xtmem_p2_regression.py`（10 项）/ `审计报告/p0_verify_tmp/verify_p0.py`（46 项）。

### 10.7 变更留痕

所有生产改动均有 `.cbak-20260828-p0` / `.cbak-20260831-p1` / `.cbak-20260831-p2` 备份；插件与 CLI 的 dev 镜像（`mimox-data/xtmem-project/src/`）同步更新。

---
*规格书 v1.1 修订 · 2026-08-31 · 小D（DSH）按审计报告执行 P0/P1/P2 修复后回写*
