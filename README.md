# XTMEM · 玄铁通用记忆系统（多角色共享记忆 · 独立人格 · 玩法增强）

> 本仓库 = 可推送 GitHub 的 **XTMEM 项目完整资产**：hermes 记忆插件、外部 CLI、DSH/zcode 端接入、技能分发、玩法增强框架、全套测试与规格文档。
> 不含：本地密钥/生产 config/实机状态（本地施工文档里含本地路径，无凭据）。

---

## 0. 一句话架构

```
                          ┌─────────────────────────────────────┐
                          │   共享记忆库（唯一权威）              │
                          │   SQLite: chunks + embedding_cache   │
                          │   + xtmem_state + relations + messages│
                          └──────────────┬──────────────────────┘
                namespace 自动分域 / visibility 三级可见 / 二段式写入
        ┌───────────────┬────────────────┴──────────┬───────────────┐
      hermes 插件       xtmem.py CLI               DSH 工具          zcode 插件
   (进程内 MemoryProvider) (外部端 MCP/桥)   (profile 插件三工具) (stdio MCP + hooks)
└─ 引擎：0.19 自建 memory/ 包整体平移（ContextBuilder/WAL/混合检索/FTS5/bge-m3）
└─ 人格：各端独立（玄铁/小 D/江北/Z老师），XTMEM 零人格逻辑
└─ 玩法：E1 可见性 / E2 关系网 / E3 留言板 / E4 反思 / E6 whisper（框架已搭）
```

## 1. 目录与源路径映射

| 本仓库路径 | 本地源路径（实机） | 职责 |
|---|---|---|
| `src/cli/xtmem.py` | `E:/2222/3333/mimox-data/xtmem/xtmem.py` | 外部端 CLI：migrate/store/observe/recall/lookup/forget/absorb/export/import/reembed/agents/agents.md/sync/whisper/reveal/msg/relations/reflect |
| `src/hermes-plugin/` | `E:/2222/3333/mimox-data/plugins/xtmem/` | 0.20.5 MemoryProvider 插件（__init__/provider/recall_policy/memory 引擎/plugin.yaml/xtmem.yaml.example） |
| `src/hermes-plugin/memory/` | 同左（0.19 memory/ 包平移，原名 engine/） | 记忆引擎：上下文构建、WAL、混合检索、成本压缩、LLM 提取、每日日志、AutoDream、静态面板 |
| `src/dsh/xtmem-tools/index.mjs` | `C:/Users/Ryuuzaki/.dsh/profiles/{web,xuantie}/xtmem-tools/index.mjs` | DSH profile 插件：xtmem_recall/store/lookup_cross + 分角色记忆注入 + cwd→project 映射 |
| `src/dsh/cordis.patch.example.yml` | `C:/Users/Ryuuzaki/.dsh/profiles/web/cordis.patch.yml` | 挂载示例（含小 D 全局人格 persona 块） |
| `src/zcode/` | `E:/2222/3333/xtmem-zcode-plugin/` | zcode 官方插件：mcp/xtmem_mcp.py（stdio MCP 三工具）+ hooks（UserPromptSubmit 注入 / Stop 观察）+ marketplace.json |
| `src/scripts/` | `E:/2222/3333/mimox-data/scripts/` | cron 脚本：xtmem-sync（5 分钟台账收敛）/ xtmem-autodream（每周蒸馏）/ monthly_knowledge_metabolism（月度报告） |
| `src/skills-shared/` | `E:/2222/3333/skills-shared/` | 技能母本库 + skill-sync.py（单源分发到 WorkBuddy/hermes/DSH/玄铁 profile） |
| `patches/hermes_feishu_card_manifest.json` | 生产替换目录 `.hermes_feishu_card_manifest` | 官方飞书卡片包 4.2.8 打补丁清单（sha256 校验） |
| `tests/` | v0.21 仓库 `tests/xtmem_*.py` | 永久回归：插件 smoke（含 S2 挂点）+ CLI 16 项（含玩法框架） |
| `docs/` | `F:/Ryuuzaki/Documents/XTMEM-*.md` | 规格书 v1.0 / 玩法增强 / skill-sync / 阶段 1.1/1.4/1.7/1.8 / 阶段 2 验收 / spec 审计 |

## 2. 关键设计（实现必读）

- **二段式写入**：`ContextBuilder.store()`（WAL→去重→演化→FTS）→ `UPDATE chunks SET namespace/project/host_origin/track/updated_at`（`_infer_track` 硬编码忽略 metadata，故必须补刀）。
- **namespace 自动分域**：hermes 插件按 `agent_identity`（profile 名）；DSH 默认 `dsh`、zcode `zcode`，均可配。
- **E1 可见性**：`visibility ∈ {shared, private, shared-with:a,b}`；检索 `--as <角色>`：自己域全见，他人仅 shared / shared-with:我。
- **E2 关系网/E3 留言板/E4 反思/E6 whisper**：relations/messages 表 + whisper/reveal/msg/relations/reflect 命令（框架 v1。人设与起始关系由用户后续灌注）。
- **RecallPolicy 闸门**：短追问继承 / 中文纠正边界 / “继续”仅 never；压缩后强制检索（on_session_switch reason=compression/rewound）。
- **S2 漏网封堵**：provider 实现 `on_memory_write`（内建 MEMORY.md 写入镜像）、`on_delegation`（子代理观测）、`backup_paths`（纳入 hermes backup）。
- **飞书卡片**：官方 `hermes-feishu-card 4.2.8` 安装器对 0.20.5 打补丁（策略 gateway_run_013_plus；run.py 13 块 / base.py 2 块 / scheduler.py 1 块），运行时包入 venv。
- **零人格**：插件/工具不注入 SOUL/USER 人设；注入层仅输出本角色记忆摘要（不 cross）。
- **skill-sync**：母本 git 单源 → 四端单向分发 + 台账（.sync-state.json schema 兼容既有），同名未管理目标跳过+警告。

## 3. 版本与提交

| 仓库 | 关键提交 |
|---|---|
| hermes 0.20.5（v0.21 开发仓库，现驻生产替换副本） | `44abdce`(baseline) → `30030ff`(插件 smoke) → `31af6b4`(HFC) → `8a49a1a`(CLI smoke) → `f32bf65` → `ebab80b`(S2 挂点) → `b63ec44`(玩法框架) |
| skills-shared 母本库 | `c5e3463`(江北 .gitattributes) → `d7110ee` → `b00dc25` → `4194e65` |

## 4. 发布状态

| 项 | 状态 |
|---|---|
| 阶段 0-1 升级工程（引擎插件化/HFC/配置迁移/切换清单） | ✅（生产已切换，9/10 项验证通过；压缩后问旧事=人工剧本） |
| 阶段 2 同步工程（CLI/DSH/zcode/absorb/reembed/cron） | ✅（absorb 1503+461 / reembed missing=0 / cron 14 任务） |
| skill-sync v1 | ✅（4 端对齐；zcode v2 待插件壳） |
| 玩法增强框架 v1（E1-E6 骨架） | ✅（人设/起始关系待用户提供） |
| agents.md --sync（指令分发） | ⏳ 待用户完善版本后实施（v2.3 已删 soul --sync） |

## 5. 快速开始

```bash
# hermes 0.20.5 安装插件（任意 profile）
#   plugins/memory/ 下放本仓库 src/hermes-plugin/（目录含 MemoryProvider 字样即被识别）
#   全局每 profile config.yaml:  memory.provider: xtmem
# CLI 自检（必须用库副本，勿写生产）
python src/cli/xtmem.py --help
python tests/xtmem_cli_smoke.py      # 16 项（默认用临时库副本）
python tests/xtmem_plugin_smoke.py   # 插件两段式/namespace 隔离/S2 挂点
python src/zcode/tests/xtmem_plugin_smoke.py
```

## 6. 已知边界

- `memory_char_limit=4000`（用户拍板保留，非官方默认 2200）；USER.md 1375 不动。
- 孤岛 1503 条含 `User: [Workspace::v1:…` 噪音前缀——**原样保留**（清洗会破坏 content_hash 去重，待拍板）。
- zcode 文件轨（export/import --zcode）已实现；zcode 端 skill 投放 v2（插件壳）。
- 飞书卡片 sidecar 由用户自理；HFC 补丁清单随仓库提供。

*小 D（DSH）· 2026-08-25 · 推送前先 `git remote add origin <你的仓库>` 再 `git push -u origin main`*