# XTMEM zcode 插件（阶段 2.4/2.5）

跨端共享记忆的 Z老师端接入（官方插件体系，本地 marketplace 安装）。

## 组件

| 层 | 文件 | 作用 |
|---|---|---|
| ① MCP | `mcp/xtmem_mcp.py`（Python stdlib 最小 stdio MCP） | 三工具直连共享库：`xtmem_recall` / `xtmem_store` / `xtmem_lookup_cross`（内部 spawn xtmem.py） |
| ② hooks | `hooks/hooks.json` + `hooks/xtmem_hook.py` | `UserPromptSubmit` 检索注入（additionalContext）；`Stop` 观察写入（namespace=zcode） |
| ③ 文件轨 | （可选，未实现） | 见规格书 §4.5 ③；本插件靠 MCP 直连，绕开 hash16 目录坑 |

零人格逻辑：不注入任何 Z老师人设（v2.3 独立人格）。

## 安装（本地 marketplace）

Settings → Plugin Management → Discover → **+**（添加 marketplace）→ 选择本地目录：

```
<PROJECT_ROOT>/xtmem-zcode-plugin      （含 marketplace.json + .zcode-plugin/plugin.json）
```

然后在 Discover 安装 **xtmem**。MCP server 需本机 Python 3.11 与 Ollama(:11434) 可用（引擎 embedding）。

## 自检（已在本机验证）

```bash
# MCP 握手 + 工具列表 + 跨库直查（测试库副本）
export XTMEM_DB_PATH="<DOCS>/xtmem-test/vector_store.db"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'   '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'   | python mcp/xtmem_mcp.py

# hooks：用户提问检索注入
printf '%s' '{"prompt":"柴油机三维仿真项目"}' | python hooks/xtmem_hook.py UserPromptSubmit
# hooks：回复观察写入
printf '%s' '{"response":"……"}' | python hooks/xtmem_hook.py Stop
```

> 注意：所有写库测试务必设 `XTMEM_DB_PATH` 指向副本（D12 数据保护）；
> 正式使用时不设即写共享库 `<HERMES_HOME>/.hermes/memory/vector_store.db`。

## 已知边界

- hook stdin 假定 UTF-8（zcode 客户端按 UTF-8 发送）；如需兼容 GBK 客户端需再调研。
- `Stop` 观察写入：回复 ≥20 字才存（防刷屏），importance 0.4，namespace=zcode。
- MCP 子集未实现 resources/prompts（不需要）。

*江北（WorkBuddy）· 2026-08-25 · 阶段 2.4/2.5*
