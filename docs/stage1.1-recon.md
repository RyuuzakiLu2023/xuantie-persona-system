# XTMEM 阶段 1.1 实地勘查报告（含规格-实况冲突）

> 依据：XTMEM-实施规格书-v1.0.md §2 / §7 阶段 0-1
> 勘查时间：2026-08-25（与规格书同日实地复核）
> 结论：**阶段 0.1 已完成；阶段 1.1 发现 3 项规格-实况冲突，按规格书 §1 要求停下报告，未经龙崎拍板不继续施工。**

---

## 0. 已完成：阶段 0.1 源码收编（只读/非破坏）

| 目标 | 位置 | 结果 |
|---|---|---|
| 新源码（0.20.5 升级目标） | E:\git项目源码\hermes-agent-mainv0.21 | ✅ git init + baseline commit \`44abdce\`（含官方 .gitattributes LF 规范化） |
| 原版 0.19 diff 基准 | E:\git项目源码\hermes-agent-main0.19 | ✅ git init + baseline commit \`2e3e2ae\`（.gitattributes 补齐 LF 规则） |
| 生产源码"遗照" | E:\git项目源码\hermes-agent-main-prod-snapshot-20260825 | ✅ 只读快照（排除 .venv/数据/缓存，158M，6783 文件）+ git init + baseline commit \`175f10e\`；**生产目录本身零改动**（D12） |

另外盘点到 \`E:\git项目源码\hermes-agent-main-v2-new\`（2026-07-21 旧快照，含部分 memory/，非当前生产态）。

---

## 1. 规格-实况冲突清单（按严重度）

### 冲突 A（严重）：\`HERMES_HOME\` 运行实况 = profile 目录，不是 mimox-data 根

规格书把共享库默认在 \`mimox-data\.hermes\memory\vector_store.db\`（450 条），隐含假设 HERMES_HOME=mimox-data（根）。但实测：

- **当前活跃 gateway 装在 profile 里**：\`mimox-data\profiles\xuantie_lark\state.db\`（228MB）最后修改 2026-08-25 13:41；\`gateway_state.json\` 显示 feishu connected、updated_at 2026-08-25T05:41Z；\`gateway.pid\` argv 为 \`hermes_cli/main.py gateway run\`。
- **profile 配置**：\`mimox-data\profiles\xuantie_lark\config.yaml\` 的 \`memory_advanced.db_path: ''\` → ContextBuilder 用 \`workspace_dir/.hermes/memory/vector_store.db\`；0.19 接线里 \`workspace_dir\` 取 \`getattr(agent,'workspace_dir','.')\`（源码无显式赋值）→ 落点随进程 CWD/HERMES_HOME 浮动。
- 因此当前玄铁实际读写的是 **profile 本地的 vector_store.db（460 条，最新到 2026-08-25 13:30）**，规格书描述的"共享库 450 条"并非活库。

### 冲突 B（严重）：实际共有 5 个记忆库，规格只认 1 个根库 + 1 个孤岛

| # | 库 | 条数 | 最后写入 | 性质 |
|---|---|---|---|---|
| 1 | mimox-data\.hermes\memory\vector_store.db | **450** | 2026-08-16 | 规格书认定的"共享库"（实际已被 DSH/历史 bridge 停更） |
| 2 | mimox-data\profiles\xuantie_lark\.hermes\memory\vector_store.db | **460** | **2026-08-25 13:30** | **当前玄铁活库** |
| 3 | mimox-data\profiles\novel\.hermes\memory\vector_store.db | **167** | 2026-08-13 | novel profile 记忆 |
| 4 | hermes-agent-main-v2\hermes-agent-main\.hermes\memory\vector_store.db | 94 | 2026-07-29 | 源码根 CWD 时期遗留 |
| 5 | E:\hermes-webui (2)\.hermes\memory\vector_store.db | **1503** | 2026-07-29 | 规格书已认领的 webui 孤岛 |

- content_hash 去重后 **2648 条唯一**（5 库之间基本不重叠，只有 srcroot 与其它库 26 条重复）。
- 规格书 §4.2 只安排了 \`absorb --db <孤岛库>\`（1503），**没有安排 profile xuantie_lark(460)/novel(167)/srcroot(94) 的收编**。若严格按 spec 只保留 450 再切共享库，**当前 2026-08-16 之后的玄铁记忆（460 条活库）将不可见 → 数据断层**。

### 冲突 C（中）：插件"全局层"在 profile 模式下不可见

- 0.20.5 记忆 provider 发现路径：\`plugins/memory\` → \`$HERMES_HOME/plugins/<name>/\`（\`plugins/memory/__init__.py:_get_user_plugins_dir()\`，读取的是 \`get_hermes_home()/plugins\`）。
- 当 HERMES_HOME=profiles/xuantie_lark 时，扫描的是 \`mimox-data\profiles\xuantie_lark\plugins\\\`（实测已有 hermes-lark-streaming、lark-hls-v2-xuantie），**不会扫 \`mimox-data\plugins\\\`**。
- 所以规格书说的"放 mimox-data/plugins/xtmem/ 全局插件目录"在 profile 运行形态下**不会被发现**——除非：每 profile 的 plugins 目录放一份/软链，或把运行链路切回根 HERMES_HOME（会动现网状态，不推荐）。

---

## 2. 其余 1.1 勘查结果（无冲突）

- 0.20.5 MemoryProvider 注册器四源：bundled > user($HERMES_HOME/plugins) > project(需 HERMES_ENABLE_PROJECT_PLUGINS=1) > pip entry_point；**bundled-wins**；目录需 \`__init__.py\` 含 "MemoryProvider" 字样（或 register/register_memory_provider）。✅ 与规格书 §4.3 一致。
- 插件识别：\`_is_memory_provider_dir\` 查 \`__init__.py\` 前 8192 字节含 \`MemoryProvider\` 或 \`register_memory_provider\`；加载时优先 register(ctx)，否则实例化 MemoryProvider 子类。✅
- hermes_feishu_card 包已定位：\`E:\2222\3333\hermes-agent-main-v2\hermes-agent-main\.venv\Lib\site-packages\hermes_feishu_card\\\`（dist-info：hermes_feishu_streaming_card-4.2.8）；生产侧补丁 manifest \`.hermes_feishu_card_manifest\`（run.py / base.py / scheduler.py 三处 + .bak）。0.20.5 挂载点需按 manifest 重新打补丁（阶段 1.6）。
- cron：\`mimox-data/cron/jobs.json\` 13 个任务（1 个禁用）：记忆维护 6 + 知识代谢 1 + 杂项 6；**两个任务现有错误**：monthly_knowledge_metabolism → \`Script not found: .../scripts/monthly_knowledge_metabolism.py\`（脚本确实缺失）；Qwen 记忆整理任务 prompt 硬编码旧生产路径（\`E:/2222/3333/hermes-agent-main-v2/.../tools/memory_cleanup.py\`）。→ 列入 1.5/1.6。
- \`mimox.py\` 仍硬编码 \`E:\2222\3333\hermes-agent-main\hermes-agent-main\`（**该目录已不存在**），\`mimox.ps1\` 指向正确生产路径；这是 1.5 必改项。✅ 已确认。
- 系统级环境变量：HKCU 存在 \`HERMES_HOME=E:\Hermes\`（旧默认根）；mimox.ps1 进程内覆盖为 mimox-data。**profile 侧由谁注入 HERMES_HOME 尚未定位** → 1.5/1.8 切换清单必须查清，否则切换后 gateway 可能跑回 E:\Hermes。

---

## 3. 建议（待龙崎拍板）

1. **共享库基准**：仍以规格书路径 \`mimox-data\.hermes\memory\vector_store.db\` 为唯一物理共享库；但**在切换前把 5 库全部 absorb/合并**（去重后 2648 条，namespace 按来源：xuantie_lark / novel / hermes-webui / legacy-root 等），再让插件 db_path 指该库。这样"450 零迁移"保住，且零数据断层。
2. **插件落层**：\`mimox-data/plugins/xtmem/\` 作为唯一源码+engine；在 \`profiles/xuantie_lark/plugins/\` 与 \`profiles/novel/plugins/\` 建 **junction/软链**（或每 profile 复制一份），保证 profile 运行形态下可见。全 profile 可用（D3 本意）。
3. **后续**：确认后再进入 1.2 引擎平移（engine 取自生产 memory/ 包快照）。

*江北（WorkBuddy）整理 · 2026-08-25*
