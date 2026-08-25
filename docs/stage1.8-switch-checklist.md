# XTMEM 阶段 1.8 · 汇总测试报告 + 切换清单（v1，待龙崎执行）

> 原则：**本清单只给龙崎自己执行**——源码替换、venv 刷新、config 修改、重启全部由她操作；AI 不擅自切换（D12 三段式）。
> 所有改动均在 `<GIT_SOURCE>/hermes-agent-mainv0.21`（新源码）完成并 commit；生产目录 `<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main` 全程未动（仅只读快照 `hermes-agent-main-prod-snapshot-20260825`）。

---

## 1. 已完成汇总（截至 2026-08-25 晚）

| 阶段 | 状态 | 证据 / 产物 |
|---|---|---|
| 0 准备 | ✅ | git 基线：prod 快照 `175f10e` / 0.19 `2e3e2ae` / 0.20.5 `44abdce`（+测试提交 `30030ff`、HFC `31af6b4`） |
| 1.1 实地勘查 | ✅ | `<DOCS>/XTMEM-阶段1.1-实地勘查报告-v1.md`：5 库、HERMES_HOME=profile、插件扫描四源 |
| 1.2 引擎平移成插件 | ✅ | `<HERMES_HOME>/plugins/xtmem/`（provider+memory 包+recall_policy+plugin.yaml）；xuantie_lark/novel 两 junction |
| 1.3 provider 接线调通 | ✅ | 测试 `v0.21/tests/xtmem_plugin_smoke.py`（commit `30030ff`）：二段式写入/namespace 过滤/novel 隔离 |
| 1.4 行为矩阵 | ✅ | `<DOCS>/XTMEM-阶段1.4-行为矩阵-v1.md`（95 行；0 代码改动，结论见档） |
| 1.5 路径硬编码 | ✅ 大半 | mimox.py / memory_bridge.py / 5 个 scripts 已 `XTMEM_HERMES_ROOT` 可覆盖（py_compile 通过）；**cron 「记忆整理」prompt 硬编码旧路径未改**（属生产数据，留到切换窗口改） |
| 1.6 feishu_card 重打 | ✅ | 官方安装器 hermes-feishu-card v4.2.8 **install** 到 v0.21：run.py 13 块 / base.py 2 块 / scheduler.py 1 块；manifest sha256 校验全部 OK；py_compile 通过；`plan_recovery` state=installed、0 findings；commit `31af6b4` |
| 1.7 config 迁移清单 | ✅ | `<DOCS>/XTMEM-阶段1.7-config迁移清单.md`（v1.1 含实测行号 + char_limit 拍板点）；**未落生产** |
| 1.8 本清单 | ✅ | 见下（用户执行后即为升级完成） |

### 1.6 官方版合并定制说明

- 安装器 detect 结果（0.20.5）：`supported=true, hook_strategy=gateway_run_013_plus, compatibility=full, base_required=true`，11 项能力全 true。
- 13 个公共补丁块与 prod 逐字节 **一致**（PATCH/COMPLETE/COMMAND_CARD* /PLATFORM_NOTICE/HFC_COMMAND/SLASH_CONFIRM/CRON/EXACT_BASE*）；
- 6 个回调块因 0.20.5 回调签名改为 `ctx` 上下文对象而**结构适配**（逻辑等价：同事件、同载荷）；prod 的 TOOL_PATCH / NATIVE_REDELIVERY 在 0.20.5 锚点已不存在，由 STABLE_TOOL 覆盖（官方 detect 判定）。
- **切换后待办**：新 venv 里跑一次 `hermes-feishu-card install` 装运行时包（v0.21 现无 venv，本次仅打了源码补丁，运行时包 skip）。

### 1.6 cron 13 任务存活确认（现状）

| 状态 | 任务 |
|---|---|
| ✅ 正常（11 个 enabled 脚本齐全） | daily-log-heartbeat / memory-cleanup / daily-progress / memory-migration-scan / memory-health-check / skills-catalog-update / cron-session-cleanup / weekly-session-cleanup / hourly-session-summary / daily-summary(prompt) / 记忆整理(prompt，见下) |
| ⚠️ 已坏（预先存在，与升级无关） | **knowledge-monthly-metabolism**：脚本 `monthly_knowledge_metabolism.py` 全盘不存在 → 自 08-01 起 last_status=error。**切换前请决定**：补脚本或停用该任务 |
| 🚫 原有意停用 | mimox-monitor（enabled=false，模型漂移被 #44585 拦截；与切换无关） |
| ⚠️ 待切换值 | 「记忆整理（Qwen2.5 智能清理）」prompt 硬编码 `<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main/tools/memory_cleanup.py`——旧路径在切换后仍指向同一位置（切换用同路径覆盖），**实际无需改**；仅在换目录名时才需改。另其 provider/model 漂移未 pin（#44585），切换后建议 pin |

---

## 2. 切换步骤（龙崎亲自执行，逐步确认）

### Step 0 · 备份（约 10 分钟）

1. 备份生产源码（保留 venv 原路径不变，用**覆盖式**替换，故先复制留底）：
   ```bash
   robocopy "<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main" "<PROJECT_ROOT>/hermes-agent-main-backup-20260825" /E /XD .venv .git
   ```
2. 备份 config（3 个文件）与数据库（含 WAL）：
   ```bash
   mkdir -p "<HERMES_HOME>/.backup-switch-20260825"
   copy "<HERMES_HOME>/config.yaml" "<HERMES_HOME>/.backup-switch-20260825/"
   copy "<HERMES_HOME>/profiles/xuantie_lark/config.yaml" "<HERMES_HOME>/.backup-switch-20260825/xuantie_lark-config.yaml"
   copy "<HERMES_HOME>/profiles/novel/config.yaml" "<HERMES_HOME>/.backup-switch-20260825/novel-config.yaml"
   copy "<HERMES_HOME>/.hermes/memory/vector_store.db" "<HERMES_HOME>/.backup-switch-20260825/"
   copy "<HERMES_HOME>/.hermes/memory/vector_store.db-wal" "<HERMES_HOME>/.backup-switch-20260825/" 2>nul
   copy "<HERMES_HOME>/.hermes/memory/vector_store.db-shm" "<HERMES_HOME>/.backup-switch-20260825/" 2>nul
   ```

### Step 1 · 停服务

```bash
hermes gateway stop
# 若飞书 sidecar 独立运行（可选确认）：
hermes-feishu-card status --config <你的hfc-config路径>
hermes-feishu-card stop --config <你的hfc-config路径>
```

### Step 2 · 覆盖式替换源码

```bash
# 推荐：用 v0.21 的 git 归档导出（保留 .git 与否均可，生产不托管 git 也行）
cd "<GIT_SOURCE>/hermes-agent-mainv0.21"
git archive --format=tar HEAD -o /tmp/xtmem-v021.tar
cd "<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main"
tar -xf /tmp/xtmem-v021.tar
```
> 等价做法：robocopy v0.21 全树 → 生产目录 /E（会带上 .git、tests、.hermes_feishu_card_manifest、三个 .bak 文件——**这些都要保留**）。绕开 .venv（新源码没有）。

### Step 3 · venv 刷新（同一 venv，路径不变）

```bash
cd "<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main"
uv pip install -e .          # pyvenv.cfg 显示 uv=0.11.25；无 uv 则用 .venv/Scripts/python.exe -m pip install -e .
```

### Step 4 · 飞书卡片运行时包入 venv

```bash
cd "<PROJECT_ROOT>/hermes-agent-main-v2/hermes-agent-main"
.\.venv\Scripts\hermes-feishu-card.exe install --hermes-dir . --yes
```
> 预期输出：`runtime package: installed into ...` + `feishu sdk: installed lark-oapi 1.6.8` + `install ok`（manifest 已在源码里，patched 文件 sha 匹配，不会重复打补丁）。

### Step 5 · config 迁移（3 个文件，见 1.7 v1.1）

```yaml
# <HERMES_HOME>/config.yaml —— 新增 memory 段（当前无）
memory:
  provider: xtmem

# <HERMES_HOME>/profiles/xuantie_lark/config.yaml（347-353 行）→ provider 改 xtmem
# <HERMES_HOME>/profiles/novel/config.yaml（346-352 行）→ provider 改 xtmem
```
- 其余 memory 键（memory_enabled/user_profile_enabled/write_approval/char_limit 等）**保持现状**；
- `memory_advanced` 段**不动**（XTMEM provider 自动读，可用 `$HERMES_HOME/xtmem.yaml` 覆盖）；
- ⚠️ char_limit=4000 与官方默认 2200 不一致——**保留 4000**（尚未拍板改回）。

### Step 6 · 启动与验证（对照 §3）

```bash
hermes gateway start
hermes memory status          # 期望：xtmem / active
```

---

## 3. 切换后验证命令（每个都给预期）

| # | 命令 | 预期 |
|---|---|---|
| 1 | `hermes memory status` | 显示 xtmem 激活，namespace=xuantie_lark |
| 2 | `cd v0.21 && python tests/xtmem_plugin_smoke.py` | 3 项断言通过（注意脚本用测试库副本；跑前设 `XTMEM_DB_PATH` 指向副本） |
| 3 | `git -C v0.21 status --short` | 干净；含 31af6b4 |
| 4 | `hermes-feishu-card doctor --config <hfc-config> --hermes-dir .` | doctor: ok（若无 config 先跑一次官方 setup 生成） |
| 5 | 飞书：发一条消息 → 工具流/完成卡片出现 | 卡片渲染正常（红/黄/绿 流程） |
| 6 | 发消息触发记忆写入 → 查库 | 新行带 namespace=xuantie_lark、updated_at 非空 |
| 7 | 长会话触发压缩后问一条旧事 | 命中并回复（压缩后强制检索链路） |
| 8 | cron 列表 | 13 任务都在；knowledge-monthly-metabolism 已处理（补脚本或停用） |
| 9 | `hermes-feishu-card status` | sidecar running |

---

## 4. 回滚方法（出问题时）

1. 停 gateway + sidecar；
2. 源码：把 `hermes-agent-main-backup-20260825` 内容拷回原目录（或反向 robocopy）；
3. config：把 `.backup-switch-20260825/` 三个 config 拷回原位置；
4. DB：把 `vector_store.db`(+wal/shm) 拷回 `<HERMES_HOME>/.hermes/memory/`（放弃新 schema 也无妨——migration 幂等，可重跑）；
5. 飞书：`hermes-feishu-card restore --hermes-dir .`（剥补丁）；
6. 重启 gateway，逐项复查 §3。

---

### Step 7（可选，切换窗口加）· 2.8 新增 cron 任务（jobs.json 追加两条）

```json
{"id": "xtmem-sync", "name": "xtmem-sync", "script": "xtmem_sync.py", "no_agent": true,
 "schedule": {"kind": "interval", "minutes": 5}, "enabled": true}
{"id": "xtmem-autodream", "name": "xtmem-autodream", "script": "xtmem_autodream.py", "no_agent": true,
 "schedule": {"kind": "cron", "expr": "0 4 * * 0"}, "enabled": true}
```
> 脚本已就位：`<HERMES_HOME>/scripts/xtmem_sync.py` / `xtmem_autodream.py`（均已自检）。

---

## 5. 切换后仍欠的 XTMEM 项目工作（不属于本次替换）

- 阶段 2.1 收尾：xtmem.py 的 export/import --zcode、soul(--sync 已删)、absorb 去重细节、observe LLM（380 行 CLI 草稿已验 core）。
- 阶段 2.2 DSH 插件 `xtmem-tools`；2.4/2.5 zcode 插件 + hooks；2.7 absorb 460+1503（写正式库，须在切换后）；2.8 cron xtmem-sync/xtmem-autodream。
- 1.4 矩阵 §5 遗留拍板：好感度 system_prompt 块与 D4（已 v2.3 删人格分发）的 profile 过滤问题。

*江北（WorkBuddy）· 2026-08-25 · 本清单 = 阶段 1 验收产出；执行前如与实况冲突先停下报告*