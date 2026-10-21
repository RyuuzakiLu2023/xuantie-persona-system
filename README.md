# XTMEM

> **记忆是水，全楼通；人格是房，各自关门。**  
> 面向多 Agent / 多角色协作的本地化共享记忆引擎与上下文底座。

---

## 💡 为什么需要 XTMEM？

在多 Agent 协同场景中，记忆管理往往面临两难：
1. **各记各的（信息孤岛）**：Agent A 刚推导出的重要结论，Agent B 毫不知情，重复踩坑；
2. **混在一起（人格串味）**：把所有人设与记忆强行塞进同一个大上下文或单一文件，不仅迅速撑爆上下文窗口，还会导致不同角色的语气、性格相互污染。

**XTMEM 的解法**：  
将**记忆存储**与**人格表达**彻底解耦。底层共享单一权威 SQLite 库与向量索引，上层通过命名空间（Namespace）与三态可见性严格控界——各角色既能随时跨端查阅共享事实，又能守住各自的隐私与独特性格。

---

## ⚡ 核心招牌

- 🎭 **共享而不串味（记忆与人格解耦）**  
  所有 Agent 共享同一套引擎与数据库，但通过 `shared`（全员公开）、`shared-with`（指定小圈子）、`private`（仅自己可见）三态权限严格隔离。角色间存在自然的信息差，绝不把别人的说话风格混进自己的提示词。
- 🧊 **分层消化（Raw 原料 vs Refined 精选）**  
  拒绝把没营养的对话流水全量喂给大模型。原始交互沉淀在 `raw` 层；后台提炼管线或人工确认的关键事实沉淀在 `refined` 精选层。检索时**先看精选卡片，未达门槛再翻原文**，大幅降低 Token 浪费与幻觉噪音。
- 🧬 **血缘追溯与确定性审计**  
  每一条派生事实都携带显式来源标识（`source_ids` 与 `derived_from`），读者权限严格受限于来源上限。归档（forget）走安全存根与事务审计，杜绝幽灵数据复活与死循环。
- 🔌 **一底多端，单核驱动**  
  CLI、Hermes、DSH、ZCode (MCP) 只是不同的接入插头，底层共用同一个已安装的 Python 引擎与本地库。换角色不用复制四份系统，单机即开即用。
- 🔒 **纯本地与优雅降级**  
  完全基于本地 SQLite (WAL) + FTS5 全文检索 + Ollama 本地向量模型（默认 `bge-m3`）。断网可用、数据零泄露；若本地向量服务暂未就绪，自动平滑回退至关键词与 BM25 全文检索。

---

## 🏗️ 架构概览

```text
┌────────────────────────────────────────────────────────┐
│                        多端入口                         │
│   CLI 终端    │   Hermes 插件   │   DSH 接头   │   ZCode/MCP │
└───────┬───────────────┬──────────────┬──────────────┬──────────┘
        │               │              │              │
        ▼               ▼              ▼              ▼
┌────────────────────────────────────────────────────────┐
│                   XTMEM 核心引擎                        │
│  ├── 身份闸门（Actor 固定与防越权校验）                  │
│  ├── 来源血缘与读者范围交集计算                          │
│  ├── 写入流水线（操作重试识别、事务 WAL、遗忘存根）       │
│  └── 混合检索调度（Refined 优先门槛 + Raw 回落）        │
└───────────────────────────┬────────────────────────────┘
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                       ▼
┌─────────────────────────┐           ┌───────────────────┐
│     本地 SQLite 单库     │           │   本地 Ollama     │
│ ├── chunks (双层记忆本体)│           │   /api/embed      │
│ ├── chunks_fts (全文检索)│           │   (默认 bge-m3)    │
│ └── embedding_cache 缓存│           └───────────────────┘
└─────────────────────────┘
```

---

## 🚀 极速上手

### 1. 环境准备与安装

- Python 3.10+
- （可选但推荐）安装 [Ollama](https://ollama.com/) 并拉取向量模型：

```bash
# 1. 本地启动 Ollama 后拉取推荐向量模型
ollama pull bge-m3

# 2. 安装 XTMEM
python -m venv .venv
# Linux / macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install .
```

*开发者调试可使用 `pip install -e .`。*

### 2. 基础配置

XTMEM 采用环境变量或工作区配置驱动：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `XTMEM_HOME` | `~/.xtmem` | 运行文件与配置文件目录 |
| `XTMEM_DB_PATH` | `~/.xtmem/vector_store.db` | SQLite 数据库绝对路径 |
| `XTMEM_ACTOR` | `default` | 当前客户端绑定的固定角色身份 |
| `XTMEM_EMBEDDING_URL` | `http://127.0.0.1:11434` | 本地 Ollama 服务接口 |
| `XTMEM_EMBEDDING_MODEL` | `bge-m3` | 语义向量模型名称 |

### 3. 三行命令体验

设置当前环境并启动：

**Linux / macOS:**
```bash
export XTMEM_ACTOR=alice
export XTMEM_DB_PATH="$PWD/shared-memory.db"
```

**PowerShell:**
```powershell
$env:XTMEM_ACTOR = 'alice'
$env:XTMEM_DB_PATH = Join-Path (Get-Location) 'shared-memory.db'
```

**执行记忆存取：**

```bash
# 写入一条共享精选事实
xtmem store --agent alice --namespace alice --tier refined --visibility shared --text "重构方案确定采用本地 SQLite 方案"

# 跨域语义召回（语义优先）
xtmem recall --query "项目数据库选型是什么？" --cross --top 3

# 跨域精准关键词直查
xtmem lookup --query "SQLite" --cross --top 3
```

---

## 🔄 记忆代谢与提炼管线

XTMEM 不把人类当成打标苦力，内置成熟的自动提炼管线：

1. **原始沉淀**：端上交互流水无感写入 `raw` 原始层；
2. **窗口切割**：根据对话进度与时钟划定待办窗口账本；
3. **模型提炼**：通过调度脚本调用你的通用大模型，总结关键进展与偏好，生成高密度记忆卡片；
4. **精选入库**：提炼出的卡片标明原始来源引用，写入 `refined` 精选层供后续工作流秒级命中。

> 完整参数调优与后台调度配置，参见 [蒸馏与后台整理指南](docs/distillation.md)。

---

## 🔌 多端生态接入

| 接入形态 | 适用环境 | 特性说明 | 接入文档 |
|---|---|---|---|
| **CLI** | 终端 / 自动化脚本 | 完整命令行支持，提供 JSON 标准输出与退出码，适合作为系统胶水 | [快速入门](docs/getting-started.md) |
| **Hermes** | 平台型网关 / 多 Profile | 提供进程内 `MemoryProvider` 适配，支持上下文预算限制与自动记忆沉淀 | [各端安装](docs/clients.md) |
| **DSH** | 极客开发工作台 | 原生 Node.js / TypeScript 工具适配，无缝挂接工作区会话 | [各端安装](docs/clients.md) |
| **ZCode / MCP** | 智能编程助手 | 标准 Model Context Protocol 工具与生命周期 Hooks 联动 | [各端安装](docs/clients.md) |

---

## 🛡️ 设计哲学与边界

1. **数据主权在手**：拒绝任何云端记忆黑盒。所有记忆文件纯本地落盘，不依赖外部闭源 API，不包含任何外部埋点。
2. **干净的开源交付**：公开仓库仅交付通用的核心引擎、接头规范与自动化治理工具，彻底剥离维护者私有的凭据、飞书桥接及私人生产历史数据。
3. **确定性优先于幻觉**：记忆系统的核心是“真实可信”。我们不使用不可控的模型直接修改底层数据库，所有变更均有审计事件与事务防护支持。

---

## 📚 延伸阅读

- 📖 [系统设计与技术白皮书](docs/XTMEM-白皮书-技术产品导向-v1.md)
- 📐 [系统通用架构说明](docs/architecture.md)
- 🛠️ [各宿主端完整配置与部署](docs/clients.md)
- 🧪 [公开整备验证与自测报告](docs/public-validation.md)
- ⚠️ [已知边界与限制说明](docs/known-issues.md)

---

## 📄 许可证

本项目基于 [MIT 许可证](LICENSE) 开源发布。
