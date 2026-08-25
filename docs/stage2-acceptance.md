# XTMEM · 阶段 2 验收汇总（v1，2026-08-25）

> 用途：把阶段 2 每个子项的**实现 + 测试证据 + 待用户动作**收拢成一页；切换/激活后按此表逐项打勾。
> 生产红线：本节所有测试均使用**临时库副本**（XTMEM_DB_PATH / XTMEM_CANONICAL_DB），生产库零写入（当时仍 450 条、无 namespace 列，已验证）。

## 1. 验收矩阵

| 子项 | 状态 | 实现 | 测试证据 | 待用户动作 |
|---|---|---|---|---|
| 2.1 xtmem.py CLI | ✅ | `mimox-data/xtmem/xtmem.py`：migrate/store/observe/recall/lookup/forget/absorb/export/import/reembed/agents/agents.md/sync；**soul 已删（v2.3）** | `v0.21/tests/xtmem_cli_smoke.py` 全绿（commit `8a49a1a`） | — |
| 2.2 DSH 接入 | ✅（待激活） | `C:/Users/Ryuuzaki/.dsh/profiles/xuantie/xtmem-tools/index.mjs` 三工具 + system-prompt/assemble 注入层（仅 xuantie preset）；`cordis.patch.yml` 已 insert | fake-ctx 实测：工具 3、lookup/recall/store OK；注入 xuantie→xtmem:memory 段 / liangshen→无 | **重启 DSH xuantie 实例（28001）** |
| 2.3 soul --sync | ❌ 已删（v2.3） | 独立人格零分发 | — | — |
| 2.4/2.5 zcode 插件 | ✅（待安装） | `E:/2222/3333/xtmem-zcode-plugin/`：MCP 3 工具 + UserPromptSubmit 注入 / Stop 观察写入 + marketplace.json | `tests/xtmem_plugin_smoke.py` 5/5 全绿 | **zcode Discover 添加本地 marketplace 并安装 xtmem** |
| 2.6 文件轨（可选） | ✅ 核心 | export/import --zcode（mem- 分区、防循环自痕迹） | CLI 回归（export/import/dedupe） | — |
| 2.7 absorb 孤岛+活库 | ✅（dry-run） | absorb 修复（Row.get/NULL 处理）+`reembed` 批量重建向量 | fresh 副本：孤岛 +1503 / 活库 +461 → 2414 条 | **切换后正式执行**（写正式库，原库只读保留） |
| 2.8 cron 同步/梦境 | ✅（脚本就绪） | `mimox-data/scripts/xtmem_sync.py`（5 分钟级）+ `xtmem_autodream.py`（每周） | 脚本级运行：sync 收敛、autodream 5 候选/3 namespace | **切换窗口在 jobs.json 加两条**（样例见 1.8 清单 Step 7） |

## 2. 永久测试资产（D12 留存）

| 测试 | 命令 | 结果 |
|---|---|---|
| hermes 插件 | `cd v0.21 && PYTHONPATH=. python tests/xtmem_plugin_smoke.py` | ✅（两段式写入轮询、namespace 隔离、Windows 清理修复，commit 最新） |
| CLI 回归 | `python v0.21/tests/xtmem_cli_smoke.py` | ✅ 10 项 |
| zcode 插件 | `python E:/2222/3333/xtmem-zcode-plugin/tests/xtmem_plugin_smoke.py` | ✅ 5 项 |

## 3. 阶段 1 引用（切换凭据）

- 切换清单：`F:/Ryuuzaki/Documents/XTMEM-阶段1.8-汇总测试报告与切换清单-v1.md`
- config 迁移：`F:/Ryuuzaki/Documents/XTMEM-阶段1.7-config迁移清单.md`（v1.1）
- 升级源码提交：`44abdce`(baseline) → `30030ff`(插件 smoke) → `31af6b4`(HFC) → `8a49a1a`(CLI smoke) → 插件 smoke 加固（HEAD）

*江北（WorkBuddy）· 2026-08-25 · 本文件 = 阶段 2 验收凭据；打勾后即可进入用户切换/激活*

---

## 4. D12 隔离审计（2026-08-25 终版）

| 项 | 证据 | 结论 |
|---|---|---|
| 生产源码 | 9 个关键文件（gateway/run.py、gateway/platforms/base.py、cron/scheduler.py、agent/memory_recall_policy.py、agent/memory_provider.py、hermes_cli/plugins.py、hermes_bootstrap.py、run_agent.py、setup.py）sha256 与只读快照 **逐一相同** | ✅ 未动 |
| 共享库（正式） | 仍 **450 条**；chunks 基础 6 列（id/content/timestamp/importance/tags/source），**无 namespace 列**、无 xtmem_state 表 | ✅ 未动（schema 迁移仅在测试副本/插件 initialize 时发生） |
| 生产 config | jobs.json mtime 08-01（未写过）；config.yaml 未在本项目期间编辑；1.7 迁移**未落生产** | ✅ 未动 |
| mimox-data 侧新增（本工程） | 插件 mimox-data/plugins/xtmem/（+2 junction）；xtmem.py；scripts 新增 xtmem_sync.py/xtmem_autodream.py 与 5 个脚本 env 可覆盖改造（默认行为不变）；cron/jobs.json **未改** | ✅ 只增不毁，默认行为不变 |
| DSH profile 变更 | cordis.patch.yml 增 insert（xtmem-tools）+ xtmem-tools/；未重启实例（激活前不生效） | ✅ 用户批准范围（2.2） |

---

## 5. 切换后验证记录（2026-08-25 晚，hermes 本体修复后）

| # | 项 | 结果 |
|---|---|---|
| 1 | 网关运行 | ✅ PID 21492/42932（新 venv + --profile xuantie_lark），cron ticker heartbeat 正常 |
| 2 | 飞书连接 | ✅ 对外 443：115.238.218.132（飞书系）+ 172.65.90.22；**用户已发消息确认（2026-08-25）✅** |
| 3 | XTMEM provider | ✅ 正式库真实 turn 二段式落库（default 网关写入）+ 插件 smoke 全绿 |
| 4 | schema/框架 | ✅ visibility/relations/messages 已迁移；agents：hermes-webui 1503 / xuantie_lark 911 / default 2 |
| 5 | 三套永久测试 | ✅ plugin / CLI(16项) / zcode 全绿 |
| 6 | reembed 全量 | ✅ 口径修复后 missing=0（补 161 条）；cache 2569 行，语义 recall 实测命中（score 0.93） |
| 7 | absorb 正式 | ✅ 1503（hermes-webui）+ 461（xuantie_lark）→ 库合计约 2416+ |
| 8 | cron 迁移 | ⚠️ 旧 13 任务 0.20.5 未继承（不读 jobs.json）；已重建 **14 个任务**：12 个 no-agent（10 旧 + xtmem-sync/xtmem-autodream）+ **3 个 prompt 型**（daily-summary / 记忆整理(mimox 脚本路径已改) / mimox-monitor —— 后两个已 pin opencode-go/deepseek-v4-flash，mimox-monitor 按旧态 **paused**） |
| 9 | HFC 卡片 sidecar | 🟡 **用户自理**：龙崎说飞书卡片她自己在弄（本工程不再跟进） |
| 10 | 压缩后问旧事 | ⏳ 人工剧本（长对话→压缩→问旧事），最终人工验收项 |

*江北（WorkBuddy）· 验证记录*
