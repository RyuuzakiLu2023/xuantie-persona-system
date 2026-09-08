# XTMEM (玄铁记忆系统)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/RyuuzakiLu2023/xuantie-persona-system/actions/workflows/ci.yml/badge.svg)](https://github.com/RyuuzakiLu2023/xuantie-persona-system/actions/workflows/ci.yml)

> **面向 Multi-Agent 架构的单 SQLite 权威底座、跨端共享记忆与情境支援系统。**  
> 专为解决大模型上下文膨胀、Prompt Cache 击穿与虚假检索反馈而生。

---

## 💡 为什么需要 XTMEM？

在多智能体（Multi-Agent）开发中，常见以下核心痛点：
1. **记忆孤岛与重复灌水**：多 Agent 各自保存碎片，跨端交互时重复灌入相同的背景事实，严重稀释长上下文注意力并击穿 Prompt Cache；
2. **闭眼抓药与低效利用**：简单语义相似度匹配导致闲聊时误捞海量无关记忆，实测检索命中率高但真实采纳率极低；
3. **缺乏真实反馈账本**：系统往往把“检索捞到”误当成“模型已使用”，无法精准衡量哪些记忆有价值、哪些已陈旧失效。

**XTMEM 的核心主张**：
- **一座 SQLite 搞定一切**：坚守纯单机 SQLite (WAL) 底座，存储层零外置数据库依赖；
- **定制小份支援包 (Support Bundle)**：场景路由器按需分流，只递送当前请求最需要的 2~4 条事实；
- **曝光五态闭环**：精确度量 `recalled → exposed → cited → confirmed → corrected`，让记忆具备自我迭代与淘汰能力。

---

## ⚡ 30 秒极速上手

### 1. 安装
```bash
git clone https://github.com/RyuuzakiLu2023/xuantie-persona-system.git
cd xuantie-persona-system

# 安装依赖
pip install -e .
```

### 2. 写入与跨角色检索
```bash
# 写入一条跨端共享规范
xtmem store --agent dev --namespace core --tier refined --visibility shared --text "单库 SQLite 为主，生产环境严禁随意变更数据拓扑"

# 跨域快速召回
xtmem recall --query "数据库架构要求" --cross --top 3
```

返回结构化 JSON：
```json
{
  "ok": true,
  "count": 1,
  "items": [
    {
      "id": "mem-7f8d19a2",
      "content": "单库 SQLite 为主，生产环境严禁随意变更数据拓扑",
      "tier": "refined",
      "visibility": "shared"
    }
  ]
}
```

---

## 🧭 能力与支持矩阵

| 模块 | 当前状态 | 运行形态 / 依赖说明 |
|---|:---:|---|
| **通用 CLI (`xtmem.py`)** | 🟢 **Stable** | 跨平台纯 Python CLI，存储层零外置数据库依赖，装完即用 |
| **双层混合检索** | 🟢 **Stable** | Refined-first 首跳 + Raw-fallback 兜底；内存 BM25 + SQLite FTS5 |
| **本地向量服务** | 🟢 **Stable** | 默认连接 Ollama `bge-m3`（可选，服务离线自动降级至关键词与 BM25 检索） |
| **Hermes 插件** | 🟢 **Stable** | `src/hermes-plugin/memory/`，原生接入 Hermes Memory Provider |
| **DeepSeek Harness** | 🟢 **Stable** | `src/dsh/xtmem-tools/`，DSH 多工具与端侧注入插件 |
| **zcode MCP 接入** | 🟢 **Stable** | `src/zcode/mcp/`，标准 MCP 协议 Stdio 服务端 |
| **曝光五态记账** | 🟢 **Stable** | recalled / exposed / cited / confirmed / corrected 全生命周期追踪 |
| **照渊台前端工作台** | 🔨 **Planned** | 前端控制台独立演进中，目前专注于轻量化无头核心 |

---

## 📚 延伸文档

- [快速入门指南 (Getting Started)](docs/getting-started.md)
- [系统架构与拓扑设计 (Architecture)](docs/architecture.md)
- [安全策略与漏洞披露 (Security Policy)](SECURITY.md)
- [贡献指南 (Contributing)](CONTRIBUTING.md)
- [变更日志 (Changelog)](CHANGELOG.md)
- [内部历史 Spec 与审计记录 (Historical Specs)](docs/history/)

---

## 📄 开源许可证

本项目采用 [MIT License](LICENSE) 许可证。
