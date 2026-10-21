# 蒸馏与派生作业（xtmem_jobs）

> 面向：把 XTMEM 的「记忆整理工序」部署到自己机器/自己库的使用者。
> 这些脚本随 wheel 安装为 `xtmem_jobs` 包，也可以直接从 `src/scripts/` 直跑。

---

## 1. 一句话

记忆库不是只进不出：原始流水（`tier='raw'`）会越积越多，检索噪声和注入预算都被它吃掉。
这套作业负责把流水**按窗口评判蒸馏**成结论层（`tier='refined'`），再生成时间骨架、画像、
实体页，并消费血缘失效标记。每条派生物都带 `source_ids` 回溯到原料。

---

## 2. 作业清单

| 脚本 | 职责 | 是否调用 LLM |
|---|---|---|
| `xtmem_distill_job.py` | **定时入口**：规则层建窗 → LLM 层蒸馏，参数走环境变量 | 取决于是否 `--units-only` |
| `xtmem_distill_units.py` | 规则层：切窗口、过滤寒暄/系统轮、写 `distill_units` 账本 | 否 |
| `xtmem_distill_llm.py` | LLM 层：逐窗评判蒸馏 → CLI store → 回写账本 | **是** |
| `xtmem_distill_commit.py` | 人工/会话直跑蒸馏的落库助手（产物 md → 库 + 账本） | 否（模型在会话里跑） |
| `xtmem_distill.py` | 粗蒸馏（旧链路）：按 `(namespace, project)` 规则聚合 | 否 |
| `xtmem_timeline.py` | 时间骨架：近 N 月按月、近 48h 按日汇总 | 否 |
| `xtmem_profile.py` | 画像：按 track 聚合近期要点 | 否 |
| `xtmem_wiki.py` | 实体页：按实体共现建页（共享池/私池分开）· **实验性，默认拒绝生成** | 否 |
| `xtmem_lineage_resolve.py` | 消费 `lineage-stale:*` 标记（剔除失效源/归档空血统）· **默认只诊断，`--apply` 才写** | 否 |
| `xtmem_lineage_stale_report.py` | 只读盘点：失效源、可见性泄漏候选 | 否 |
| `xtmem_common.py` | 共用接线（库路径/引擎/CLI/namespace/时区），不是作业 | 否 |

---

## 3. 最小可用跑法

```bash
# 0) 准备：安装 wheel，或让脚本能找到仓库内引擎
export XTMEM_DB_PATH="$HOME/.xtmem/vector_store.db"
# 仓库内直跑时可选：export XTMEM_PLUGIN_ROOT=<repo>/src/hermes-plugin

# 1) 只建窗（不需要 LLM 配置）
python -m xtmem_jobs.xtmem_distill_job --units-only

# 2) 跑完整蒸馏（需要 LLM 三项配置）
export XTMEM_LLM_BASE="https://api.example.com/v1"
export XTMEM_LLM_MODEL="your-model-id"
export XTMEM_LLM_KEY="..."          # 只从环境读，不落盘、不回显
python -m xtmem_jobs.xtmem_distill_job --limit 20

# 3) 单域
python -m xtmem_jobs.xtmem_distill_job --domain my-agent

# 4) 派生层（按需）
python -m xtmem_jobs.xtmem_timeline --domain my-agent
python -m xtmem_jobs.xtmem_profile  --domain my-agent

# 5) 血缘：默认只诊断（不写库）；确认后再 --apply
python -m xtmem_jobs.xtmem_lineage_resolve
python -m xtmem_jobs.xtmem_lineage_resolve --apply --skip-domain my-judged-ns

# 6) 实体页（实验性，默认拒绝生成；必须显式开关）
python -m xtmem_jobs.xtmem_wiki --allow-experimental
```

直跑（不装包）：

```bash
python src/scripts/xtmem_distill_job.py --units-only
```

---

## 4. 配置项（全部走环境变量）

### 4.1 库与引擎

| 变量 | 默认 | 说明 |
|---|---|---|
| `XTMEM_DB_PATH` | `$XTMEM_HOME/vector_store.db` → `~/.xtmem/vector_store.db` | 最高优先，所有作业一致 |
| `XTMEM_HOME` | `~/.xtmem` | 未设 `XTMEM_DB_PATH` 时的库目录 |
| `XTMEM_PLUGIN_ROOT` | 按脚本位置推导 `<repo>/src/hermes-plugin` | 含 `memory/` 包的目录 |
| `XTMEM_CLI` | 自动发现：已装包用 `python -m xtmem`，否则仓库内 `src/cli/xtmem.py` | 落库子进程入口 |
| `XTMEM_ACTOR` | 由作业按 namespace 设置 | CLI 侧身份；手工调用时用于跨域写闸 |

### 4.2 namespace

不写死任何域名单。优先级：

1. 命令行 `--domain <ns>`
2. `XTMEM_DISTILL_NAMESPACE`（蒸馏链）/ `XTMEM_NAMESPACE`（通用）
3. 库里实际存在的 active namespace

批量作业（timeline / profile / wiki）默认遍历库里真实存在的域。

### 4.3 LLM（仅 `xtmem_distill_llm` 需要）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `XTMEM_LLM_BASE` | ✅ | — | OpenAI 兼容根地址（不含 `/chat/completions`） |
| `XTMEM_LLM_MODEL` | ✅ | — | 模型 id |
| `XTMEM_LLM_KEY` | ✅ | — | API Key；缺失时退出码 2 并写 `xtmem_events` |
| `XTMEM_LLM_EFFORT` | ❌ | `high` | 写进 `reasoning_effort`；**设为空串 = 完全不发送该参数**（网关不认时的出口） |
| `XTMEM_LLM_MAX_TOKENS` | ❌ | `8192` | 保守默认，多数网关接受；长窗口需显式调高（实测长窗 32000+ 才不被截断） |
| `XTMEM_LLM_TIMEOUT` | ❌ | `120` | 秒；慢模型/长窗口显式调高 |
| `XTMEM_DISTILL_PROMPT` | ❌ | `prompts/distill_v2_generic.md` | 提示词文件 |

⚠️ 公开仓**不带**默认网关、默认模型或内置 key；也不读取任何 `.env` 文件。
缺配置就明确报错——这是红线，不是可调项。

### 4.4 切窗与节奏

| 变量 | 默认 | 说明 |
|---|---|---|
| `XTMEM_DISTILL_DOMAIN` | 空（全部） | 调度壳的单域覆盖 |
| `XTMEM_DISTILL_GAP` | `60` | 相邻轮间隔超过该分钟数即断窗 |
| `XTMEM_DISTILL_LIMIT` | `20` | 单批窗口数 |
| `XTMEM_DISTILL_UNITS_ONLY` | `0` | `1` = 只建窗 |
| `XTMEM_DISTILL_LLM_ONLY` | `0` | `1` = 只蒸 |
| `XTMEM_SPEAKER_JSON` | 内置中英双语标记 | 见 4.5 |
| `XTMEM_TZ` | `Asia/Shanghai` | 时间骨架/画像的月日界时区 |
| `XTMEM_TZ_OFFSET` | `8` | `zoneinfo` 不可用时的固定偏移 |
| `PROFILE_CAP_CHARS` | `2000` | 画像正文上限 |

### 4.5 发言人标记

规则层靠「有没有用户发言」过滤流水。默认同时认：

- 用户：`User:` `User：` `用户:` `用户：` `Human:` `human:`
- 助手：`Assistant:` `Assistant：` `助手:` `助手：` `Bot:` `bot:`

如果自己的 agent 框架用别的前缀，覆盖：

```bash
export XTMEM_SPEAKER_JSON='{"user":["Me:","我:"],"assistant":["Agent:","它:"]}'
```

---

## 5. 数据契约

### 5.1 `distill_units` 账本

| 列 | 含义 |
|---|---|
| `unit_id` | `<namespace>:<source>:<起始epoch整数>`，天然幂等键 |
| `state` | `pending` / `processing` / `distilled` / `partial` / `failed` |
| `raw_ids` | 窗口内 raw chunk id 的 JSON 数组 |
| `refined_ids` | 本次蒸馏写入的 refined chunk id |
| `attempt` / `last_error` | 失败计数与原因；`attempt >= 3` 转 `failed` |
| `claim_token` | 并发领取令牌（`BEGIN IMMEDIATE` 内写入，防多 worker 重复蒸） |
| `claimed_at` | **本次领取**时间。僵尸回收只看它；`created_at` 是建账时间，按它判超时会把"正在蒸的旧窗口"误重置 ⇒ 重复提交 LLM |
| `has_correction` | 用户行是否含纠正信号（`不对/错了/wrong/...`） |
| `filtered_ids` | 窗口时间范围内被过滤的轮次及原因（便于回查「为什么少了几轮」） |

### 5.2 派生 chunk

| 作业 | `tier` | `derived_from` | `track` | `importance` |
|---|---|---|---|---|
| 评判式蒸馏 | refined | `distill-judge` | `procedural`/`default` | 0.50–0.85（按判定档） |
| 粗蒸馏 | refined | `distill` | `default` | 0.7 |
| 时间骨架 | refined | `timeline` | `timeline` | 1.0 |
| 画像 | refined | `profile-aggregator` | `profile` | 1.0 |
| 实体页 | refined | `wiki-entity` | `default` | 0.7 |

所有派生物都带 `source_ids`；可见性由引擎按来源读者交集继承（不会因为生成而放宽）。

### 5.3 判定档 → importance

`错误·已纠正` 0.85 > `错误·未纠正` 0.80 > `正确` 0.70 > `不适用` 0.65 >
`正确·未核验` 0.60 > `未定论` 0.50。

设计意图：**错答案最值得被记住**（防同一个坑再踩），且必须把「当时误判 + 真相」一起记下。

---

## 6. 幂等与重跑

| 作业 | 幂等口径 |
|---|---|
| units | `unit_id` 已存在即跳过（不覆盖已有 state） |
| llm | 领取即 `processing` + `attempt+1` + `claimed_at`；全部写成功回 `distilled`，**部分成功回 `partial`**，全失败回 `pending`；`attempt>=3` → `failed` |
| commit | 已 `distilled` 的窗口直接拒绝 |
| 粗蒸馏 | 水位制：只蒸产物 `source_ids` 中最大 timestamp 之后的 raw |
| timeline | 同月已有活骨架 → 跳过；`--refresh-48h` 强制产新版并 supersede 旧版 |
| profile | 每次产新版，旧画像 supersede |
| wiki | 同 `(canon, 池, 域)` 已有活页 → 跳过；`--rebuild` 先删再建 |

### 6.1 `partial` 语义（部分成功）

一个窗口可能产出多条记忆，其中几条写库失败（引擎拒绝/校验失败）：

- **已写入的条目全部保留**，`refined_ids` 记全（不会因为后面失败而丢前面的产物）；
- 账本 `state='partial'`，`last_error` 记剩余错误；**不冒充 `distilled`**；
- `partial` 不在自动领取集合（`pending`）里 ⇒ 下一轮 cron **不会整窗重蒸**（避免重复计费/重复产物）；
- 补跑：`python -m xtmem_jobs.xtmem_distill_llm --unit <unit_id>`。
  每次落库都带 `operation_id = distill:<unit指纹>:<条目指纹>`，所以重复提交同一条目
  得到**同一个记忆 id**，不会写出第二条。

批次汇总里 `units_partial` 单独计数，且只要出现 `failed` 或 `partial`，进程退出码非 0——
cron/CI 据此判红，不会把"写了一半"当成功。

重跑安全的前提：**只重跑本作业自己的派生层**，不要手工改 `distill_units.state`。

---

## 7. 原料退役（为什么 raw 会变 `superseded`）

LLM 层写成功 refined 后，会把喂它的 raw 原料置 `state='superseded'`、`superseded_by=<refined id>`，
原因记为 `retire-reason=distill-retire`。

为什么不是 `archived`：`archived` 在引擎里表示「这条没了」，会沿 `source_ids` 反向传播
`lineage-stale` 标记，把引用它的 refined 一起打成失效——蒸馏产物自己把自己毒死。
`superseded` 才是「有替代」的语义。

---

## 8. 权限与可见性

### 8.1 LLM 原料准入（蒸馏链）

`build_material()` 在**拼原料之前**逐行过 `entry_visibility_ok(row, domain)`：
目标域读不到的行**根本不进 prompt**（不是事后过滤正文——那来不及，模型可能已经把内容转述进条目）。

原因：蒸馏产物写进 `domain`，可见性由 `source_ids` 继承。若原料里混进更严可见性的行，
模型转述出的条目却只登记"可见的那几条"，私有内容就借着更宽的可见性出门。
回执里的 `acl_dropped` 是被挡掉的行数（只报数，不带内容）。

### 8.2 血缘登记范围（`lineage_scope`）

- `declared`：条目声明的轮次能**精确**对上，且整窗各行读者范围一致 ⇒ 按声明子集登记；
- `window`：声明缺失/对不上/整窗读者范围不一致 ⇒ **按整窗登记**（可见性取最严）。

历史实现用双向 `startswith` 模糊匹配，`mem-abcd` 会命中一批 `mem-abcdxxxx`，等于伪造精确来源；
现在只认精确 id，对不上就明确退回整窗并在回执标注，不假装精确。

### 8.3 取材层（时间骨架/画像）

- 取材前先过统一读闸（`memory.visibility.entry_visibility_ok`，含隔离标记），
  不可见的行**根本不进内存挑选**，不是生成后再过滤。
- 派生页的 `source_ids` 只登记**正文实际采用**的条目；登记没用到的条目等于替派生页收紧权限，
  也会让血缘查询把没进正文的条目算成依据。
- 时间骨架 / 画像不输出「另 N 条未展开」这类计数：计数本身会泄露私有材料的**存在性**。
  诊断计数只进 `run_report`（运行回执），不进页面正文。

---

## 9. 血缘与自引防护

- `xtmem_sources.select_materials()` 统一判定「这条能不能当原料」：
  本生成器旧视图、兄弟生成器页、已知聚合页、页头特征一律排除。
- `lineage_ancestors()` 沿 `source_ids` 做有限深度回溯，四类结果分开返回：
  `tainted`（链上有生成器页）/ `unknown`（悬空 id）/ `cycle`（真环）/ `unfinished`（深度用完没查完）。
  「查了 N 层没看见」不等于干净，所以 `unfinished` 也判不采用。
- `xtmem_lineage_resolve.py` 消费 `lineage-stale:*`：源失效则从 `source_ids` 剔除；
  剔空则归档该派生并连带打标。

### 9.1 默认只诊断（`--apply` 才写）

血缘修复会改 `source_ids`、甚至归档整条派生——不可逆的内容层后果。
所以默认行为是**只盘点**（打印将要 pruned/archived 的 id，零写入）；
真要执行必须显式加 `--apply`。`--skip-domain` / `$XTMEM_LINEAGE_SKIP_DOMAINS`
可让某些域只留标不代清理——给「有独立人工审档流程」的域用。

### 9.2 实体页按池隔离（实验性）

- 蒸馏产物入正文前必须按池过滤（共享页只吃 `visibility='shared'`，私池页反过来）；
- `source_ids` 必须覆盖**正文里出现的每一种原料**：实体共现源 + 蒸馏产物。
  漏登记蒸馏产物 = 派生页继承不到它的可见性，血缘也查不到这段依据。

即便如此，跨域 `shared-with` 名单、真实实体抽取质量等仍未在部署形态验证，
所以 `xtmem_wiki` **默认拒绝生成**（退出码 2），必须显式 `--allow-experimental`。

---

## 10. 子进程环境（为什么作业要"剥掉"宿主的身份变量）

`xtmem_common.store_via_cli()` 在起 CLI 子进程前会显式删除：

- `XTMEM_ADMIN` / `XTMEM_ADMIN_TOKEN` / `XTMEM_TOKEN` —— 让 CLI 以 `__admin__` 视角读写
  （读闸全开、可跨域写）。后台作业只需要本域普通身份，继承等于把宿主管理员权限漏给定时任务。
- `HERMES_HOME` / `MIMOX_HOME` / `XTMEM_HERMES_HOME` —— 会参与某些版本的库路径解析，
  与已算好的 `XTMEM_DB_PATH` 冲突时可能把写入导向另一个库。

子进程只保留：`XTMEM_DB_PATH`（绝对路径）、`XTMEM_ACTOR`（本域身份）、`PYTHONUTF8=1`。

---

## 11. 已知限制

1. **LLM 层只做单轮请求**：不重试上游 5xx（只对「空正文」重试一次）；失败靠下一轮 cron 重来。
2. **不内置向量化**：蒸馏产物写库不生成 embedding，向量腿由引擎的补算路径负责。
3. **时间骨架的月锚**是「同月存在即跳过」，改切窗口径需 `--refresh-48h` 或手工清旧版。
4. **粗蒸馏与评判式蒸馏是两条链**：同一域不要同时开两个，否则会互相 supersede。
   默认 `JUDGE_DOMAINS` 只含 `personal` 占位；自己的部署按需用 `--domain` 控制。
5. **实体页依赖 `chunk_entities` / `entity_aliases`**：未跑实体抽取时不会建页（正常空跑）。
6. **时区默认 Asia/Shanghai**：跨国部署请设 `XTMEM_TZ`。
7. 公开仓不包含任何生产库、凭据、私有提示词或私有网关地址；示例里的地址/模型都是占位。
8. **LLM 层的 `operation_id` 幂等依赖引擎的 `memory_observations` 表**；若目标库是更早的
   schema，该表可能不存在——此时 CLI 会报写失败（`ok=false`），不会被静默吞掉。
9. **`partial` 状态需要人工/显式 `--unit` 补跑**：调度器不会自动捡起它（这是有意的，
   自动重蒸整窗会重复计费）。
10. **画像/时间骨架的取材与血缘闸**在本地合成库上验过；跨域 `shared-with` 名单集合、
   隔离标记（`retire_reason` 前缀）等真实部署边界**未在本仓验证**，需在自己的库上抽验。
11. **wiki 依赖 `chunk_entities` / `entity_aliases` 表**：由实体抽取链路写入，公开仓不生成它们；
    且默认拒跑，必须 `--allow-experimental`。
12. **lineage_resolve 默认只诊断**：`--apply` 才写库（这是有意的安全默认，不是缺功能）。
13. **本仓只在合成库上验证过**：`tests/test_public_distillation.py` 覆盖切窗/ACL/血缘/
    部分成功/不泄原料/默认拒跑；**未**在真实部署形态（真实 `shared-with` 名单、
    真实实体抽取、真实 LLM 网关）上验证。

---

## 12. 目录

```
src/scripts/
  __init__.py
  xtmem_common.py          # 共用接线（库/引擎/CLI/namespace/时区/子进程环境）
  xtmem_distill_job.py
  xtmem_distill_units.py
  xtmem_distill_llm.py
  xtmem_distill_commit.py
  xtmem_distill.py
  xtmem_sources.py
  xtmem_timeline.py
  xtmem_profile.py
  xtmem_wiki.py
  xtmem_lineage_resolve.py
  xtmem_lineage_stale_report.py
  prompts/distill_v2_generic.md
```
