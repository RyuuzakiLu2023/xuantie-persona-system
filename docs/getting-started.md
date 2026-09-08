# 快速开始 (Getting Started)

XTMEM 是一个专为 Multi-Agent 生态打造的**单 SQLite 权威底座、跨端共享记忆与上下文支援系统**。

本文档带你在干净的开发环境中，**从零搭建并跑通最小记忆闭环**（约 3 分钟）。

---

## 1. 环境准备

XTMEM 核心仅依赖标准 Python，轻量无冗余。

- **Python**：>= 3.9
- **系统支持**：Linux、macOS、Windows (PowerShell / Git Bash)

```bash
# 1. 克隆代码
git clone https://github.com/RyuuzakiLu2023/xuantie-persona-system.git
cd xuantie-persona-system

# 2. 创建并激活干净的虚拟环境
python -m venv .venv
source .venv/bin/activate       # Linux / macOS
# .venv\Scripts\activate        # Windows

# 3. 安装依赖（支持本地可编辑安装）
pip install -e .
```

---

## 2. 核心 CLI 体验（30 秒跑通）

XTMEM 提供原生跨平台命令行工具 `xtmem`。在未配置外置向量服务时，XTMEM 具备优雅降级能力（支持内存 BM25 与 SQLite 全文索引检索）。

### ① 初始化或指定测试数据库
设置环境变量指定数据库位置（默认为内存或当前目录）：
```bash
export XTMEM_DB_PATH="./test_memory.db"
# Windows PowerShell: $env:XTMEM_DB_PATH="./test_memory.db"
```

### ② 写入第一条跨角色记忆（Store）
```bash
python src/cli/xtmem.py store \
  --agent developer \
  --namespace project_x \
  --tier refined \
  --visibility shared \
  --text "系统首选单库 SQLite 架构，严禁随意引入重型图数据库"
```

### ③ 跨域检索记忆（Recall）
```bash
python src/cli/xtmem.py recall \
  --query "系统选用什么数据库架构" \
  --cross \
  --top 3
```

输出将返回结构化 JSON，包含匹配的分数、置信度判定与记忆块原文：
```json
{
  "ok": true,
  "count": 1,
  "items": [
    {
      "id": "mem-...",
      "content": "系统首选单库 SQLite 架构，严禁随意引入重型图数据库",
      "tier": "refined",
      "visibility": "shared"
    }
  ]
}
```

---

## 3. 启用本地向量模型（可选加分项）

若需要高精度向量召回，推荐在本地运行开源轻量向量服务：

1. **安装并启动 Ollama**：
   ```bash
   ollama run bge-m3
   ```
2. **连接向量端点**：
   XTMEM 默认检测 `http://127.0.0.1:11434` 上的 `bge-m3` 模型。只要服务在线，写入与检索自动升级为**稠密向量 + 稀疏关键词的双层混合检索（Hybrid Search）**。

---

## 4. 运行验证套件

仓库提供隔离的自测套件，不会污染任何真实数据：

```bash
# 运行冒烟测试
python tests/test_smoke.py

# 运行脱敏门禁检查
python scripts/scan_drive_letters.py
```

---

## 5. 跨端 Agent 接入速览

- **Hermes Agent**：直接加载 `src/hermes-plugin/memory/` 作为 Memory Provider 插件。
- **DeepSeek Harness (DSH)**：将 `src/dsh/xtmem-tools/` 复制到 dsh 插件目录即可挂载。
- **zcode**：使用 `src/zcode/mcp/` 运行标准 MCP 服务。

更多深度设计请阅读 `docs/architecture.md` 与 `docs/XTMEM-白皮书-技术产品导向-v1.md`。
