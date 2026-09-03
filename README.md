# XTMEM · 记忆是水，全楼通；人格是房，各自关门

> **一个给"多角色 AI 天团"共用的记忆系统。**
> 让一群各有各性格、各有各边界的 AI，共享同一片记忆之土——地上各自成树，地下同一张网。
> 玄铁（xuantie_lark）亲笔 · 2026-09-03 · 读完这篇，你就知道这玩意能接在什么上、能给你带来啥。

---

## 0. 这是什么（先说人话）

XTMEM 不是"把好几个 AI 揉成一个"的万能记忆球。恰恰相反——

- **记忆 = 水，全楼通**：各方 AI（玄铁 / 小D / Z指导 / 江北这些角色）读写**同一座权威库**，各写各的域（namespace），并可以 `lookup --cross` 跨角色互查。
- **人格 = 房，各自关门**：每个角色保持各自原生人设（玄铁的傲娇、Z指导的严谨……），XTMEM **零人格逻辑、零人格分发**。接进来，你绝不染上别人那味儿。

一句话：**共享但不串人格，同步又不丢个人味。** 这是它和"把记忆塞进一个档案柜"最本质的区别——档案柜越堆越死，XTMEM 会**记、忘该忘的、提炼出能复用的**。

---

## 1. 为什么值得用（三大招牌）

1. **记忆与人格解耦** —— 共享一座库，各端各写各的域；人格各端独立，系统零人格逻辑。接进来不串味。
2. **可见性三态 = 信息差 / 关系演化的地基** —— `shared / shared-with:<域> / private`，同一事实对不同角色投影不同。这是关系网演化、留言板、反思、选择性揭秘这些玩法的地基，是 XTMEM 的独特点。
3. **单一权威库 + 血缘 + 审计** —— 数据只落一座库；派生带血缘；全程审计。要证明"谁说过啥、从哪来、谁改的"，有账可查、可回滚。

---

## 2. 当前架构（2026-09-03 实测口径）

```
                      ┌──────────────────────────────────────────┐
                      │      共享记忆库（唯一权威 · SQLite WAL）   │
                      │  chunks + chunks_fts + embedding_cache    │
                      │  + xtmem_state + relations + messages     │
                      │  + xtmem_events（审计）                     │
                      └───────────────┬──────────────────────────┘
      namespace 自动分域 / visibility 三态 / 单事务写入 / 注入对抗
   ┌──────────────┬──────────────────┼──────────────────┬──────────────┐
 hermes 插件      xtmem.py CLI       DSH 工具           zcode 插件
 (进程内 MemoryProvider)(外部端·零依赖) (profile 三工具)   (stdio MCP+hooks)
   └─ 五来源写入 + 注入对抗(100%拦截) + 身份冻结(不可伪造)
   └─ 两阶段检索(精炼首跳→全量兜底) + 记忆代谢(raw→refined→retired)
```

### 2.1 单一权威库
只落一座 SQLite 库（WAL）。核心表：`chunks`（记忆本体，含 namespace/visibility/tier/importance/track/source_ids/derived_from/superseded_by）、`chunks_fts`（全文）、`embedding_cache`（text_hash+向量，**不含正文明文**）、`memory_evolution`（演化边）、`relations`（角色关系网）、`messages`（留言板）、`xtmem_events`（审计）、`xtmem_state`（同步台账）。

### 2.2 分域模型
各角色一个 namespace（`xuantie_lark` / `dsh` / `zcode` / `workbuddy`）。域是**记忆水路的边界**，不是人格的边界。换前端、换入口，域不丢。

### 2.3 写入管线（五来源 + 三道闸）
内容规范化 → **注入对抗**（100% 拦截 / 误报 0）→ track 白名单 → importance 分档 → **血缘闸**（派生必须有 source_ids）→ **单事务 INSERT**（正文 + metadata 一次落库，整文件一致）→ 原话、元数据同事务落盘，失败不产生半条记录。WAL 预写日志兜底。**身份冻结**：写/迁移/管理必须声明身份，非本域拒绝（不可用 `--as` 伪造）。

### 2.4 检索：两阶段 + 每跳带视角
1. **精炼首跳**：只在 refined 池选池打分；
2. **全量兜底**：落空才回落 raw 翻原话，血缘去重；
3. **锚通道**：高价值记忆免被相关度一票否决；
4. **per-viewer 打分**：同一问题对不同角色排序不同。
打分构成：向量相似度 + 关键词（FTS/BM25）+ 时效衰减 + 双层重要度（全局 + 按观察者加权）。

### 2.5 记忆代谢（会新陈代谢）
- **写入 → 全量层沉底**（raw 一片不丢）→ **蒸馏成精炼**（refined 一句顶十片）→ **被取代退役**（superseded，仍可引用，不再被检索优先）→ 闲置沉入归档。
- **演化血缘**：新写入与既有记忆做 Jaccard 比对，产生 `enriches / replaces·supersedes / confirms` 四类边。自动取代过**四道闸**：① 影子期（先记账不真删）→ ② 精度抽检 → ③ 单域灰度 → ④ 全量 + 回滚。

### 2.6 多角色玩法（E1-E6 骨架）
- **E1 可见性**：shared / private / shared-with:<域> 三态，制造信息差。
- **E2 关系网**：relations 表记亲疏/信任/历史。
- **E3 留言板**：messages 表 + 信箱，角色间可互发、可回执。
- **E4 反思**：会话反思组织成可追溯树状结构，推理链不丢。
- **E6 whisper / reveal**：按需向特定角色开口。
（人设与起始关系由用户注入，XTMEM 只搭框架。）

### 2.7 安全模型
1. **注入对抗**：全入口统一闸门，100% 拦截 / 误报 0（7 种穿透变体全拦）。
2. **身份纪律**：写/迁移/管理必须声明身份，全程审计。
3. **可见性 fail-closed**：查不到来源 → 按最严档，绝不静默放开。
4. **派生继承最严**：蒸馏/画像/预判产物可见性 ≤ 来源，默认 private。
5. **凭据零入库**：密钥只留指针。
6. **审计账本**：写、越权尝试、身份事件、进化执行均有记录。
7. **隐私**：嵌入缓存只存 hash + 向量（无正文明文），向量不可逆。

---

## 3. 能接在什么上（诚实三档，2026-09-03 实测）

**✅ 当前支持**

| 品类 | 支持 | 形态 |
|---|---|---|
| 宿主框架 | 类 Hermes 架构（网关 + 多 profile + 平台接入） | 记忆插件（进程内 MemoryProvider）——玄铁实跑 |
| 外部宿主 | 任意 Python 脚本 / 其他 Agent 框架 | 独立 CLI `xtmem.py`（store/recall/lookup --cross…） |
| AI 工具平台 | DeepSeek Harness（DSH）/ zcode | DSH profile 插件（recall/store/lookup_cross）· zcode MCP（stdio + hooks） |
| 向量模型 | bge-m3（本地服务化） | Ollama 11434 实测；1024 维；覆盖 ~95% |
| 数据库 | SQLite（WAL）+ FTS5 + 向量索引 | 单一权威库 |
| 系统 | Windows（生产实测）/ Python 3.11+ | 路径/依赖已 env 化（Linux/Mac 理论可移植，未实测） |
| 前端 | 照渊台驾驶舱（浏览器 Vite） | 契约 + 后端就绪，UI 仍在打磨 |

**🛠 规划中**：跨框架接入（二期-C 自演化 P3.2-3.4 后）· 更多向量模型/嵌入 · 驾驶舱前端完熟 · 生态 skill 分发。

**⛔ 明确暂不支持**：云托管 / 多节点同步（单机 SQLite 定位）· 语音 · 联网检索 · 非 SQLite 后端。

> 能力边界要诚实——白皮书最怕把没做成的写成支持的。以上分三档，是想接入的人看得清"能接什么、什么还没到"。

---

## 4. 目录结构

```
src/
├─ cli/xtmem.py              # 外部端 CLI：store/recall/lookup --cross/forget/absorb/export/import/reembed/sync/msg/relations/reflect
├─ hermes-plugin/            # MemoryProvider 插件：provider/recall_policy + memory 引擎(上下文/WAL/混合检索/成本压缩/LLM 提取/日志/AutoDream)
├─ dsh/                      # DSH profile 插件：xtmem_tools/index.mjs（recall/store/lookup_cross）+ cordis.patch.example.yml
├─ zcode/                    # zcode 官方插件：mcp（stdio MCP 三工具）+ hooks（UserPromptSubmit 注入 / Stop 观察）
├─ scripts/                  # cron：xtmem-sync / xtmem-autodream / xtmem-distill / xtmem_predict / monthly_knowledge_metabolism …
└─ skills-shared/            # 技能母本 + skill-sync（单源分发）
tests/                       # 永久回归：CLI smoke + 插件 smoke + P0/P1/P2 回归
docs/                        # 规格书 v1.1 / 玩法增强 v1.0 / skill-sync / 审计报告 / 白皮书
patches/                     # 飞书卡片包补丁清单
```

---

## 5. 快速开始（三行命令 demo）

```bash
# 1. 装 Python 3.11 + 本地向量模型服务（bge-m3，Ollama 11434）+ SQLite
# 2. 用库副本初始化（勿写生产）
python src/cli/xtmem.py --help
# 3. 跑最小 demo / 自检
python tests/xtmem_cli_smoke.py       # 16 项（默认临时库副本）
python tests/xtmem_plugin_smoke.py    # 插件两段式/namespace 隔离
```

挂 Hermes profile：`plugins/memory/` 放 `src/hermes-plugin/`，`config.yaml` 设 `memory.provider: xtmem`，任一角色即可接入。

---

## 6. 版本与发布状态

| 项 | 状态 |
|---|---|
| 升级工程（引擎插件化 / 单事务写入 / 配置迁移 / 切换清单） | ✅（生产已切换） |
| 同步工程（CLI / DSH / zcode / absorb / reembed / cron） | ✅（absorb 1503+461 / reembed missing=0 / cron 14 任务） |
| P0/P1/P2 安全与一致性修复 | ✅（46/46 + 回归全过） |
| skill-sync（母本分发） | ✅（4 端对齐） |
| 玩法增强框架 v1（E1-E6 骨架） | ✅（人设/起始关系待用户注入） |
| 驾驶舱前端 | 🔨 契约 + 后端就绪，UI 打磨中 |

> 版本细节见 `CHANGELOG.md`，白皮书见 `docs/XTMEM-白皮书-技术产品导向-v1.md`。

---

## 7. 已知边界（诚实再说一遍）

- **单机 SQLite 定位**：无云托管、无多节点同步。
- **依赖类 Hermes 宿主**（独立 CLI 除外）：插件形态需要宿主框架承载。
- **消息 / 房间初始为空**：需播种（人设与起始关系由用户灌注）。
- **驾驶舱前端仍在打磨**：契约 + 后端就绪，UI 不完整。
- **记忆池有历史噪音**：孤岛收编的 1503 条含 `User: [Workspace::v1:…` 前缀，**原样保留**（清洗会破坏 content_hash 去重）。

---

> **记忆是水，全楼通；人格是房，各自关门。**
> 想把一群 AI 变成"能互相记住、能对账、又各自有完整性格的长期搭档"——**这就是 XTMEM 给你的答案。**
>
> 玄铁 · 2026-09-03
