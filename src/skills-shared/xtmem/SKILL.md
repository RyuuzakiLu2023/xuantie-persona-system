---
name: xtmem
description: "XTMEM 共享记忆使用指引——跨端共享记忆库的读写规范：recall/store/lookup --cross 用法、namespace 与可见性语义（private/shared/shared-with）、去重与演化、隐私红线。适用：多角色协作时的跨端记忆读写、查历史事实、写共享事实、私密记忆管理。触发短语：'查共享记忆', '跨端记忆', '写记忆', '共享库', 'xtmem', '记忆同步'。Do NOT use for: 本地会话记忆（用本地 memory 文件）、需要实时性的对话历史。"
agent_created: true
version: 1.0.0
tags: [memory, xtmem, cross-agent, shared-memory]
triggers: [查共享记忆, 跨端记忆, 写记忆, 共享库, xtmem, 记忆同步]
source: <XT_ROOT>\mimox-data\xtmem\xtmem.py
---

# XTMEM 共享记忆使用指引

> 多角色共享记忆库（SQLite + bge-m3 embedding via Ollama 11434），ZCode/玄铁/DSH/WorkBuddy 四端共用。
> 库文件：`<XT_ROOT>\mimox-data\.hermes\memory\vector_store.db`；引擎 CLI：`<XT_ROOT>\mimox-data\xtmem\xtmem.py`。

## 这是什么

跨端共享记忆：任何一端写入，其他端 `lookup --cross` / `recall --cross` 可查。ZCode 侧通过 MCP 三工具直连（无需直接跑 CLI）；其他端走 `xtmem.py` 命令（store / recall / lookup / forget / whisper / reveal 等）。

## 三工具速查（ZCode MCP）

| 工具 | 用法 | 注意 |
|---|---|---|
| `xtmem_recall` | 语义检索（embedding） | query 必填；**不带 `cross` 只查当前域（zcode）**——跨域查全库必须 `cross=true`；`namespace` 可指定其他域 |
| `xtmem_store` | 写一条事实 | text 必填；默认 namespace=zcode / agent=zcode；importance 0~1；tags 逗号分隔；project 可选 |
| `xtmem_lookup_cross` | 关键词直查（LIKE） | 不走 embedding，空 query = 列出最近；按 importance 降序 |

## 可见性语义（P0 级，务必遵守）

- `shared`：默认，所有端可见
- `private`：仅写入者可见（`lookup --cross` 查不到）
- `shared-with:<端名>`：仅指定端可见
- `whisper` 写入即 private；`reveal` 选择性揭露（private → shared-with）

## 铁律

1. **写库前确认意图**：跨端共享的事实才写；一次性/过程性内容不写（防污染共享库）
2. **recall 空结果先怀疑域过滤**，再怀疑没数据——`cross=true` 试一次再下结论
3. **更新事实 = forget 旧 id + store 新的**（`xtmem.py forget --id <id>`；MCP 无 forget，需 CLI）
4. **涉私内容默认 private**（whisper 写入），跨端分享前想清楚可见范围
5. **store 偶尔超时但实际已写入**：先 `lookup` 验证再重试，别重复写

## 常见坑

- Git Bash 的 `python` 是微软商店 stub（静默失败/退出码 41/49）：一律用 `<PYTHON_EXE>` 或 `py`
- 引擎路径：`<XT_ROOT>\mimox-data\xtmem\xtmem.py`（插件里相对推导的路径会落空——已在 MCP 侧加绝对路径候选修复）
- 库里混有历史 absorb 的整段旧对话（`host_origin: absorb:hermes-webui`）——语义检索可能命中噪音，对结果做可信度判断
