# XTMEM · 多角色玩法增强 Spec v1.0

> 定位：XTMEM 方案 v2.3「多角色 AI 天团」的**玩法增强层**，独立于核心工程。
> 触发来源：龙崎 2026-08-25 调研多角色项目（情报局/狼人杀/剧本杀/斯坦福小镇/Whisper Town/OpenViking/Roundtable 等）后的方向确认。
> 核心洞察：从"一致性"转向"**冲突、博弈、关系演化与观察**"。
> 排期：**原定等核心做完再做**；2026-08-25 龙崎拍板：**先搭框架**（E1 visibility / E2 relations / E3 messages / E4 reflect / E6 whisper+reveal 的 schema+CLI+端侧参数已完成，永久测试绿），**人设与各角色起始关系（初始 relations/性格设定）由龙崎后续提供**后在框架上填充。
> 依赖：核心工程的共享库 + namespace 分域 + lookup --cross 全部就绪后，本层在其上**加层**（不推翻、只扩展）。

---

## 0. 诊断一句话

XTMEM v2.3 现状 = **四个角色共用一个笔记本，各写各的页**。
本层要补的三样：**各自的秘密（权限）、彼此的态度（关系）、能互相说话（互动）**。

## 1. 六项增强总览（按解锁度排序）

| # | 增强 | 参考项目 | 补的维度 | 优先级 |
|---|---|---|---|---|
| E1 | 记忆可见性分层 | AI情报局 / 剧本杀 / 狼人杀 | **权限** | P0 |
| E2 | agent-agent 关系网 | OpenViking / Roundtable / StarMoonIsland | **关系** | P0 |
| E3 | 角色互动通道 | Kallamo / 狼人杀辩论 | **互动** | P1 |
| E4 | 轻量反思层 | 斯坦福小镇（记忆→反思→计划） | **态度** | P1 |
| E5 | 观察者面板 | 斯坦福小镇 / Whisper Town | **观察** | P1 |
| E6 | whisper/secret 命令 | 主动制造信息差 | **博弈工具** | P2 |

## 2. E1 · 记忆可见性分层（P0，信息不对称的地基）

**现状**：chunks 按 namespace 分域，`lookup --cross` 全量可见。
**缺口**：没有"私有记忆"和"选择性共享"。

**设计**：
```sql
ALTER TABLE chunks ADD COLUMN visibility TEXT DEFAULT 'shared';
-- 值域：shared（全端可见）/ private（仅本 namespace）/ shared-with:agent1,agent2（指定共享）
```

**检索规则**：
- `recall`（同 namespace）：本域全部 + 他人 `shared` + 他人 `shared-with:我`
- `lookup --cross`：仅 `shared` + `shared-with:我`（`private` 对任何他者不可见）
- `whisper`（见 E6）写入即 `private`

**验收**：玄铁写一条 private，Z老师 lookup --cross 查不到；玄铁 shared-with:zcode，Z老师能查到但 D指导查不到。

## 3. E2 · agent-agent 关系网（P0，恩怨演化）

**现状**：affection 只管"玄铁↔用户"。
**缺口**：角色之间没有态度、记仇、结盟、契约。

**设计**：
```sql
CREATE TABLE relations (
  agent_a TEXT, agent_b TEXT,           -- 无序对（归一化：a<b）
  sentiment REAL DEFAULT 0,             -- -1..1 好感/敌意
  trust REAL DEFAULT 0.5,               -- 0..1 信任度
  debt TEXT DEFAULT '{}',               -- {人情/债务} 承诺账
  last_interaction REAL,
  history TEXT DEFAULT '[]',            -- 关系演化流水
  PRIMARY KEY (agent_a, agent_b)
);
```

**演化规则**（写入时触发）：
- 跨角色互动事件（谁帮了谁/谁骗了谁）→ 更新对应 relations 行的 sentiment/trust/debt
- "上次他骗了我" → 下次协作前 recall 时注入关系摘要（"你对 X 的态度：警惕，trust=0.3"）
- StarMoonIsland 式承诺：`debt` 记录"答应过 X 什么"，逾期未兑现 → trust 降

**验收**：Z老师骗了玄铁一次 → relations(玄铁,Z老师).trust 下降；玄铁下次与 Z老师互动时 recall 带出"不信任"上下文。

## 4. E3 · 角色互动通道（P1）

**现状**：四角色各自与用户对话，互不直接说话。
**设计（两条路，优先官方）**：
1. **借 hermes A2A + Bot Mode**（0.20.5 官方自带）——玄铁作为 hermes 侧 agent 已具备 agent 间通信能力；其余端经 xtmem 工具发"留言"
2. **共享留言板**（轻量兜底）：新增 `messages` 表（from/to/content/ts/read），角色可 `xtmem.py msg --to <agent> --text ...`，对方下轮 recall 时读自己的未读留言

**玩法映射**：圆桌辩论、结盟谈判、互相吐槽——用户可发起"圆桌议题"（cron 或手动），四角色各自回应，沉淀进记忆。

**验收**：江北 `msg --to 玄铁 "你的代码又出 bug 了"` → 玄铁下轮 recall 能读到并回怼。

## 5. E4 · 轻量反思层（P1）

**现状**：只有 7 天一次 AutoDream 蒸馏。
**设计**：跨角色互动事件后，触发一次"反思"——agent 生成一条 `reflection` 记忆（"这事对我意味着什么/我该改变什么态度"），写入自己 namespace，`visibility=private`（反思是自己的，不共享）。

**实现载体**：复刻 AutoDream 的 delegate 机制，但触发改为"事件驱动"（relations 变化 / 收到留言 / 博弈一轮结束），由 cron `xtmem-reflect` 或 provider 的 on_session_end 惰性触发。

**验收**：一轮博弈后，各角色生成自己的反思（立场/态度开始生长），面板可见反思流。

## 6. E5 · 观察者面板（P1）

**现状**：status_panel.html 已有生成器（单人状态）。
**设计**：升级为「天团观察面板」——
- 关系图谱：四角色 + relations 连线（粗细=亲密度，颜色=敌意/友好）
- 互动流水：messages + 跨角色事件时间线
- 记忆流：各角色最近 memory + reflection（隐私标记打码可选）
- 纯静态 HTML（复用 status_panel 生成器，无 JS 依赖）

**验收**：打开面板能看懂"这四口子最近在演什么戏"。

## 7. E6 · whisper/secret 命令（P2）

```bash
xtmem.py whisper --agent 玄铁 --text "秘密：其实..."     # 写入玄铁 private 记忆
xtmem.py reveal --id mem-xxx --to zcode                  # 选择性揭露（private→shared-with）
```
- 用途：用户主动制造信息差，旁观 B 如何从 A 套话——极限测试权限/选择性分享/检索能力
- **验收**：whisper 后除玄铁外全端查不到；reveal 给 Z老师后仅 Z老师可见

## 8. Schema 变更总汇（本层加在核心 migrate 之后）

```sql
-- E1
ALTER TABLE chunks ADD COLUMN visibility TEXT DEFAULT 'shared';
-- E2
CREATE TABLE relations (...);
-- E3（兜底留言板）
CREATE TABLE messages (id TEXT PRIMARY KEY, from_agent TEXT, to_agent TEXT, content TEXT, ts REAL, read INTEGER DEFAULT 0);
-- E4（反思作为普通 chunk，lane/visibility 标记，不单独建表）
```

> ⚠️ 全部为 `ADD COLUMN`/`CREATE TABLE`，幂等、不碰核心 450 条现有数据。

## 9. 依赖与排期

| 项 | 依赖 | 可独立做？ |
|---|---|---|
| E1 可见性 | 核心 migrate（namespace 列） | 否——等核心 |
| E2 关系网 | 核心 lookup --cross + 互动事件源 | 否 |
| E3 互动 | hermes 升级（A2A/Bot Mode） | 留言板兜底可先 |
| E4 反思 | AutoDream 挂点（cron） | 否 |
| E5 面板 | status_panel 生成器 | 半独立（可视化层） |
| E6 whisper | E1（visibility） | 否 |

**顺序**：核心工程（方案 v2.3 阶段 0-2）→ E1+E2（地基）→ E3+E4（互动与态度）→ E5+E6（观察与博弈工具）。

## 10. 与核心方案的边界（防膨胀）

- 本层**不改**核心方案的任何定稿决策（记忆全局通 / 独立人格 / 引擎插件化 / 三端接入）
- 本层**只加**：3 列/表（visibility、relations、messages）+ 2 命令（whisper、msg）+ 1 面板 + 反思触发
- 若实施中发现与核心冲突 → 以核心 spec 为准，本层调整，不反向改核心

---

*江北 · 2026-08-25 · 素材：龙崎调研的多角色项目清单 + XTMEM v2.3 现状诊断*
