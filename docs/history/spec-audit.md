# XTMEM 实施规格书 v1.0 · 审计报告

> 审计对象：`<DOCS>/XTMEM-实施规格书-v1.0.md`
> 方法：ai-code-audit-handbook（文档对账模式：声称 vs 实况，全部结论带行号/实测证据）
> 审计人：江北（WorkBuddy）· 2026-08-25 15:2x
> **一句话结论：spec 骨架扎实（事实表大头全对），但 v2.3 改版留下两处决策级残留矛盾，provider 接口认知漏了 6 个方法，且环境是活的（审计期间 mimox.py 已被并行会话修改）——修完 5 处可放心开工。评分 ★★★☆☆（3.5/5）**

---

## 一、文档对账（声称 vs 真实）

### ✅ 真实（实测通过，抽验 13 项全过）

| 声称（行号） | 实测证据 |
|---|---|
| 共享库 450 条（L22） | sqlite COUNT 实测 450 ✅ |
| 孤岛 1503 条（L23） | sqlite COUNT 实测 1503 ✅ |
| cron 13 任务（L31） | jobs.json len=13 ✅ |
| 常量 2200/1375（L32） | memory_tool.py:263（`memory_char_limit: int = 2200, user_char_limit: int = 1375`）✅ |
| on_pre_compress 在 memory_provider.py:305（L94） | grep 实测 :305 ✅ |
| 注册器 2466→6782 行（L133） | wc -l 实测 ✅ |
| hermes_state 7822→14637+四卫星（L132） | wc -l + ls 实测 ✅ |
| 引擎 458K/19 文件（L46） | du + ls 实测 ✅ |
| ~2300 行官方文件改动（L24） | 双树全量 diff（CRLF 修正）实测 ✅ |
| xuantie-webui 1142 行（L29） | wc -l 实测 ✅ |
| 引用文档 6/6 存在（附录 C） | 逐个 ls 实测 ✅ |
| <HERMES_HOME>/plugins 与 profile plugins 双目录并存（L83） | ls 实测 ✅ |
| zcode 插件体系/本地 marketplace（L30） | marketplace.json source=directory 实测 ✅ |

### 🟠 严重（2 项——决策级矛盾，新会话按 spec 执行会走错方向）

**S1 · §4.4 与步骤 2.2 残留"注入层"，与 D5 直接矛盾**
- 证据：spec L107「注入层 | SOUL/USER/MEMORY 摘要 systemPrompt 注入，仅玄铁会话触发 | 只有玄铁」+ L171「2.2 DSH profile 插件层挂载（工具+注入两层）」
- 对照：D5（L43）「**不注入任何人格**（各 preset 保持各自人设）」、D1（L39）「XTMEM 零人格逻辑」、§4.6（L124）「不做任何同步」
- 判定：v2.3 改版时 §4.4 表格与步骤 2.2 漏改——**新会话按字面执行会给 DSH 做人格注入，直接违反 v2.3 定稿**。方案文档 §7.1 已改（"人格（v2.3 独立）：不做注入层"），spec 未同步。
- 修法：L107 行改为"人格（v2.3 独立）：不注入，各 preset 各自人设"；L171 删"（工具+注入两层）"。

**S2 · MemoryProvider 接口认知不全：声称 19 方法，实测类内 21 个，职责清单漏 6 个**
- 证据：实测 `class MemoryProvider` 内 21 个方法：name/is_available/initialize/unavailable_reason/system_prompt_block/prefetch/queue_prefetch/recall_status/sync_turn/get_tool_schemas/handle_tool_call/**shutdown/on_turn_start/on_session_switch/on_delegation/on_memory_write**/on_pre_compress/get_config_schema/save_config/**on_memory_write 已列**/backup_paths（全文件含模块级 def 共 22）
- 对照：spec L131 声称"19 方法"（转引自用户侧评估，未亲验）；§4.3 职责三栏（L87-97）未提 **on_memory_write / on_delegation / on_turn_start / on_session_switch / shutdown / backup_paths**
- 判定：其中 **`on_memory_write`（写入事件回调）与 `on_delegation`（委派事件）可能是"钩子漏网"的新挂点**——直接影响 L99"三条漏网已认领"的完备性（第四条漏网候选？）；`backup_paths` 关系 provider 数据备份约定。职责清单自称"定稿"但接口面不全。
- 修法：L131 改"类内 21 方法"；§4.3 补注"另有 on_memory_write/on_delegation/on_turn_start/on_session_switch/shutdown/backup_paths 六个可选挂点，1.3 实施前逐个评估是否承接漏网行为（尤其 on_memory_write）"。

### 🟡 缺陷（6 项）

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| F1 | L126-127 | `absorb：阶段 2.7 执行…` **整行重复两次**（编辑事故） | 删一行 |
| F2 | L4 | 裁决顺序写"方案 v2.2"——方案实际标题 v2.3（含 v2.4 记录） | 更新为 v2.3（v2.4 记录） |
| F3 | L213 | 附录 C 同样写"（v2.2，本 spec 母本）"；且裁决链缺《多角色玩法增强 spec》《skill-sync 方案》 | 同步版本+补两份 |
| F4 | L204 | 待拍板项 2"娱乐性五项/好感度跨端计分"已过时（v2.3 升级为多角色互动，v2.4 又立玩法增强 spec 且已拍板"等做完再改"） | 刷新为"阶段 3 多角色互动；玩法增强 E1-E6 见独立 spec（核心完成后启动）" |
| F5 | L12-15 | "拆成两层"下列三条要点（记忆/人格/羁绊）——措辞与内容不符 | "两层"改"三层"或删羁绊行 |
| F6 | L3 | "所有路径与数值均经实地勘查验证"声称过强——含未亲验的转引数据（19 方法来自用户评估、hash16 公式来自用户侧破解、部分常量转自架构文档） | 改"经 2026-08-25 勘查；部分数值转引自用户侧勘验（已标注）" |

### ⚠️ 活环境警告（审计期间的意外发现）

- **mimox.py 已于今天 14:06 被修改**（mtime 实锤）：原"硬编码 v1 死路径"（`<PROJECT_ROOT>/hermes-agent-main\hermes-agent-main`——实测该目录**不存在**，原 mimox.py 本已损坏）被改为 `XTMEM_HERMES_ROOT` 环境变量 + 默认指 v2 生产路径，注释标"XTMEM 阶段1.5"。
- 判定：**存在并行执行会话在按 spec 干活**（改法与 spec §4.2/阶段 1.5 设计吻合）。spec L26"硬编码 v1 老路径待盘点"**已过时**（实况自愈）。
- 含义：① spec 声称的"实测"带时间戳效力，环境会演进；② 建议 spec 头加一行「勘查时点 2026-08-25；环境由并行会话推进，冲突时以实况为准并回写本 spec」；③ 0.1 步骤的 git init 变得**更紧迫**（改动已在发生，再不收编基线就飘了）。

### 🪦 死引用/死代码核查

- 引用文档 6/6 存在 ✅；引用路径（test_p1_features.py、mem0 _backend.py、marketplace.json 等）实测存在 ✅；无死引用。

## 二、数值对账总表

| 声称 | 实测 | 判定 |
|---|---|---|
| MemoryProvider 19 方法 | 类内 21（全文件 def 22） | 🟠 S2 |
| mimox.py 硬编码 v1 待盘点 | 14:06 已被改为 XTMEM_HERMES_ROOT+v2 默认 | ⚠️ 过时（活环境） |
| 方案文档 v2.2 | v2.3（含 v2.4 记录） | 🟡 F2 |
| 450 / 1503 / 13 / 2200 / 1375 / 2466→6782 / 7822→14637 / 458K / ~2300 行 / 1142 行 / :305 | 全部吻合 | ✅ |

## 三、修复路线图（ROI 排序）

| 优先 | 动作 | 工作量 |
|---|---|---|
| 1 | S1：删 §4.4/步骤 2.2 的"注入层"残留 | 1 分钟 |
| 2 | S2：方法数改 21 + 职责清单补 6 挂点注记 | 5 分钟 |
| 3 | F1-F4：删重复行 / 版本号刷新 / 待拍板刷新 | 3 分钟 |
| 4 | F5-F6 + 活环境警告落 spec 头 | 2 分钟 |
| 5 | （建议）风险表补一条"孤岛 1503 条含 'User: [Workspace::v1:…' 噪音前缀，absorb 前需清洗策略" | 1 分钟 |

## 四、审计范围与方法

- 对象：XTMEM-实施规格书 v1.0 全文 216 行，逐行通读 + 30+ 项声称清单 + 13 项抽样实测（sqlite COUNT / wc -l / grep / ls / python re 解析类方法）+ 跨文档一致性（方案 v2.3/玩法 spec/验尸 v1.3）。
- 局限：未覆盖——① 母本手册全流程的阶段 3-9（架构臃肿/攻击样本实跑等对纯文档对象不适用，仅取文档对账模式）；② spec §5 三大接口点的深层源码复核（此前验尸 v1.3 §六已做过一轮，本次仅复核数值）；③ 并行会话的其他改动面（仅发现 mimox.py 一处，建议 0.1 git init 前全面 diff 一遍在用版源码）。
- 实测记录：见上文各表"实测证据"列；所有 sqlite/wc/grep 命令于 2026-08-25 15:2x 实跑。

---

*江北 · ai-code-audit-handbook 首战 · 2026-08-25*
