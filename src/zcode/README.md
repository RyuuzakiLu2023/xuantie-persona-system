# XTMEM · ZCode adapter（MCP + hooks）

ZCode 端接入 XTMEM 记忆库：MCP 工具直连 + 每轮提示注入 + Stop 观察写入。
零人格逻辑——本插件不注入任何角色人设，只做记忆搬运。

## 组件

| 层 | 文件 | 作用 |
|---|---|---|
| MCP | `mcp/xtmem_mcp.py` | 八工具（记忆读写 + 信箱 + 杂项），stdio JSON-RPC |
| hooks | `hooks/hooks.json` + `hooks/xtmem_hook.py` | `SessionStart` 恢复锚；`UserPromptSubmit` 单口注入；`Stop` 引用记账 + 观察写入 |
| 清单 | `.zcode-plugin/plugin.json`、`marketplace.json`、`.mcp.json` | 本地 marketplace 安装 |

本适配器（MCP + hooks）**不 import 引擎**，只调用外部已安装的 CLI——所以它与 DSH 端
共用同一条命令行入口和同一座库。Hermes 端不同：provider 在宿主进程内直接 import 引擎，
见 `docs/clients.md` §4。

## 前置条件

1. 已安装 XTMEM CLI（本版未上 PyPI，别直接 `pip install xtmem`——那会装到同名无关包）：
   ```bash
   python -m pip install .            # 仓库根目录：装 memory + xtmem_hermes + xtmem 命令
   # 或
   python -m build --wheel && python -m pip install dist/*.whl
   ```
   安装后 `python -m xtmem agents` 应输出一行 JSON。
2. 本机 `python` 与安装 CLI 的解释器一致，或显式设置 `XTMEM_PYTHON`。
3. 可选：本地 Ollama + `bge-m3`（`:11434`）。没有则自动降级为关键词检索，不会崩。

## 安装（本地 marketplace）

```text
Settings → Plugin Management → Discover → +（添加 marketplace）
→ 选择本目录（含 marketplace.json 的那一层）
→ 在 Discover 里安装 xtmem
```

安装后 ZCode 会按 `.zcode-plugin/plugin.json` 启动 MCP server，并按 `hooks/hooks.json`
挂三个 hook。插件缓存目录里的副本才是生效面，改仓库不会自动生效。

## 配置（全部环境变量，无私有默认路径）

| 变量 | 默认 | 说明 |
|---|---|---|
| `XTMEM_ACTOR` | `zcode` | 本端身份。**只从这里读**，工具参数不能改身份 |
| `XTMEM_PYTHON` | 当前解释器 | 调用 CLI 用的解释器 |
| `XTMEM_SCRIPT` | 空 | 可选：`xtmem.py` 绝对路径；不设则 `python -m xtmem` |
| `XTMEM_DB_PATH` | 引擎默认 | 显式库路径（测试副本 / 多库部署） |
| `XTMEM_TIMEOUT_S` | `55` | MCP 单次 CLI 调用超时 |
| `XTMEM_SERVE_MODE` | `off` | `auto` 才尝试常驻 serve 快路 |
| `XTMEM_SERVE_PORT` | 引擎解析 | serve 端口（`auto` 模式用） |
| `XTMEM_HOOK_TOP` | `4` | 每轮注入候选条数 |
| `XTMEM_HOOK_TIMEOUT_S` | `45` | hook 内单次 CLI 调用超时 |

多端共存示例（同一座库，不同身份）：

```bash
XTMEM_ACTOR=role-a
XTMEM_ACTOR=role-b
XTMEM_DB_PATH=/data/xtmem/vector_store.db   # 可选：显式共享库
```

## MCP 工具接口

| 工具 | 关键参数 | 说明 |
|---|---|---|
| `xtmem_recall` | `query`、`top`、`namespace`、`cross` | 语义检索；非 cross 只能查本域 |
| `xtmem_store` | `text`、`importance`、`namespace`、`tags`、`tier` | 写本域；`knowledge` + `shared` 是唯一跨域写例外 |
| `xtmem_lookup_cross` | `query`、`top` | 关键词直查，跨可见域 |
| `xtmem_lookup_id` | `id` | 按 ID 精确回查，仍校验可见性 |
| `xtmem_msg_recent` | `top` | 信箱只读，不置读 |
| `xtmem_msg_inbox` | `confirm=true` | 置读即消费，不可逆 |
| `xtmem_agents` | — | 列出已注册角色 |
| `xtmem_forget` | `id`、`confirm=true` | 归档一条记忆，归属校验在引擎侧 |

`msg send` **不**暴露为工具：对外发信属于授权动作，只留 CLI。

## 自动注入 / Stop 能力边界

| 事件 | 做什么 | 不做什么 |
|---|---|---|
| `SessionStart` | 取本域最近一条 `track=checkpoint` 记忆，给一行恢复锚 | 不注入画像/公告/信箱大盘；无锚就空注入 |
| `UserPromptSubmit` | `recall` 单口注入：≤4 条、按 route 预算阶梯截断、每条带 `[m:<id>]`；信箱未读只读自查 | 不做跨域补召（那是 MCP 的活）；不重复注入同一事实 |
| `Stop` | 扫回复里的 `[m:mem-xxxxxxxx]` 逐条 `mark`；按引擎 `store_quality_gate` 决定是否 `store` 一条观察（长度 ≥8） | 不做会话摘要、不做蒸馏、不删改历史记忆 |

要点：
- hook 对数据库**只读**（SessionStart 一条 SELECT）。写只走 CLI 子进程。
- 所有注入文本都过花括号隔断（连续 `{{`/`}}` 插零宽空格），防止宿主模板插值炸轮。
- 记账（`expose`/`mark`/`observe`）失败一律静默，不挡注入。

## 自检（不碰生产库）

```bash
export XTMEM_DB_PATH="$PWD/scratch/vector_store.db"
export XTMEM_ACTOR=role-a

# 1. MCP 握手 + 工具清单
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python mcp/xtmem_mcp.py

# 2. 注入路径
printf '%s' '{"prompt":"deployment preferences"}' | python hooks/xtmem_hook.py UserPromptSubmit

# 3. 观察写入
printf '%s' '{"response":"a sufficiently long reply for the observation gate"}' \
  | python hooks/xtmem_hook.py Stop
```

`mcp/xtmem_mcp.py` 的 stdout 只能出现 JSON-RPC 帧；日志全部走 stderr。
若宿主侧看到 "empty output"，先手动跑上面第 1 条看 stderr 里的失败原因。

## 已知边界

- serve 快路（`XTMEM_SERVE_MODE=auto`）需要引擎里存在 `memory.serve_protocol` 且能解析端口。
  连不上会回落 CLI；**鉴权拒绝不回退**（避免把"身份不对"伪装成"能用"）。
- hook stdin 按 UTF-8 解码（`errors=replace`）；GBK 客户端发来的内容会变成替换字符。
- 首次调用要水化引擎（2~3s，重启后更久），hook 超时上限 60s 是为它留的余量。
- 本插件不做记忆修剪/蒸馏；容量治理走 CLI 的 `maintain` / `distill`。

*XTMEM Maintainers · 2026-10-22 · 公开接头首批*
