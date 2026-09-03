# XTMEM · 公开版发布 Changelog

> 目标仓库：`https://github.com/RyuuzakiLu2023/xuantie-persona-system`
> 版本基线：总 Spec v2.0.1（指令型重构 + 审计整改）· 审计 🟢 · 测试 83 passed / 83 collected
> 构建：2026-09-03（小D，公开脱敏版）

---

## 这是什么

XTMEM = 多角色 AI 共享记忆系统：**一座共享记忆库（SQLite），多个独立人格的 AI 角色同台**。
- **记忆 = 水，全楼通**：任何端读写同一座库，各写各的 namespace 域，跨角色可查。
- **人格 = 房间，互不同步**：玄铁 / 小D / 江北 / Z指导 各保持原生人设，XTMEM 零人格逻辑。
- **三端接入**：hermes 记忆插件（进程内 MemoryProvider）· xtmem.py 外部 CLI · DSH/zcode 工具层。
- **玩法**：私密记忆（信息差）、角色间留言、关系网（恩怨演化）、反思、选择性揭秘。

---

## 本次发布包含（R1 → P5.1 全量）

### 引擎（src/hermes-plugin/memory/）
- vector_store / wal_engine / hybrid_retriever / embedding_provider / context_builder —— 二段式写入、WAL、FTS5 + bge-m3 混合检索、分层库、血缘。
- schema / tuning / graph / probe / ner_tagger / visibility —— 统一迁移、旋钮外置、共现图、引力探针、实体打标、可见性三态。
- chat_scheduler / a2a_client / cockpit_contract / inbox —— 群聊调度、A2A v1.0 客户端、驾驶舱契约组装、信箱摘要。
- api_server —— 8 条驾驶舱路由 + legacy 观测路由（常驻 127.0.0.1:20000，keepalive 守护）。

### CLI（src/cli/xtmem.py）
store / recall / lookup --cross / whisper / reveal / promote / relations / msg(send·inbox·recent) / rooms(create·list·get·set) / graph / sign / prediction / reflect / forget / absorb / import / export / maintain / observe / mark / sync / migrate / agents.md —— 含消息总线、关系网、可见性、活性事件记账。

### 端接入
- srcdsh/xtmem-tools/index.mjs —— DSH 工具层三工具 + 记忆/信箱/画像注入 + 自动落库。
- src/zcode/ —— MCP 插件（stdio + hooks 信箱注入）+ marketplace。
- src/skills-shared/ —— skill-sync + xtmem / xtmem-bus-guide / ai-code-audit-handbook 技能。
- srccripts/ —— autodream / distill / events_purge / health / lark_notify / lark_talk / predict / profile / reflect / relations_evolve / sync / timeline / wiki / monthly_knowledge_metabolism。

### 前端（frontend/ — 照渊台）
- 一渊四灯共享记忆操作台（契约先行前端）。
- srcapi/contract.ts + endpoints.ts —— 八端点字段级契约（唯一出处）。
- srcmock/engine.ts + fixtures/ —— 有状态假后端（mock 行为基准）+ 三态场景数据。
- srclib/client.ts + hooks.ts —— mock 开关（?mock=0 / VITE_USE_MOCK）、轮询、降级态。
- scripts/verify-ui.mjs —— 31 项 UI 验收脚本。
- 默认对接后端：vite 代理 → http://127.0.0.1:20000；关 mock 即接通。

### 测试
tests/ 全套（pytest，conftest + axis_predicates + 六类守卫），83 passed。

---

## ⚠️ 脱敏说明（公开版必须遵守）

本公开版**已清除全部实机路径/用户名/密钥**。出现占位符是给部署者按环境填的模板：

| 占位符 | 含义 | 部署时 |
|---|---|---|
| <XT_ROOT> | 项目根（原本机 E:\2222\3333） | 设环境变量或替换为你自己的根 |
| <USER_HOME> / <USER_DOCS> | 用户目录 | 替换为你的实际目录 |
| <PYTHON_EXE> / <HERMES_EXE> | python / hermes 可执行文件 | 设 XTMEM_PYTHON 等或替换 |
| <CANONICAL_DB> | 测试基准库 | 测试用 env XTMEM_CANONICAL_DB 指定 |

- 所有默认路径优先读环境变量（XTMEM_DB_PATH / MIMOX_HOME / XTMEM_SCRIPT / XTMEM_PYTHON / XTMEM_TUNING / XTMEM_PLUGIN_DIR）。
- 公开版**不含**：config 里的 token、.env、实机路径、生产库数据。上传前请不要把含密钥/本地路径的文件加进来。
- 角色名「龙崎 / Ryuuzaki」是产品词表（世界观别名），保留（非敏感）。

---

## 上传步骤（给执行上传的人）

1. 确认本文件夹 = 公开脱敏版（已构建好并本地 git commit）。
2. 配置远端（一次性）：
   ```bash
   git remote add origin https://github.com/RyuuzakiLu2023/xuantie-persona-system.git
   ```
3. 推送到远端（分支 `master`；若远端是空仓库用 `-u` 建立上游）：
   ```bash
   git push -u origin master
   ```
4. 推送后到 GitHub 确认：README 渲染正常、src/ 与 tests/ 与 frontend/ 都可见、无本地路径溢出。

---

## 已知边界（如实）

- 驾驶舱前端（照渊台）已并入 frontend/（契约 + mock + UI 验收脚本）；后端 8 路由形状见 src/hermes-plugin/memory/api_server.py。
- 二期-C（系统自演化 P3.2-3.4）挂起等数据；Lint 巡检 / 折叠 / hanlp 未排期。
- messages/rooms 表初始为 0 行：驾驶舱房间需部署者自行播种（xtmem rooms create）。

---
*本 changelog 由小D（DSH）生成 · 与运行时镜像全树对账 0 DIFF · 通过 v8 全生命周期审计（🟢）*
