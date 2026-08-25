# XTMEM · Skill-Sync 方案 v1.0

> 定位：XTMEM「资产单源分发」三兄弟之一——soul --sync（人格）/ agents.md --sync（指令，v2.1）/ **skill --sync（技能，本文）**。三者同用「母本单源 → 多端投放」模式。
> 关键特性：**独立于记忆引擎**——纯文件分发，不依赖 vector_store.db，可先于 XTMEM 主体落地（今天就投）。
> 日期：2026-08-25
> ⚠️ 2026-08-25 拍板更新：① **skill --sync = ✅ 已实施 v1**（母本库+脚本+4 端投放见进度文件）；② **soul --sync = ❌ 已随 v2.3 删除**（独立人格，零分发）；③ **agents.md --sync = ✅ 要做**，但**版本待龙崎完善后启动**（本文档对应部分届时按新版实现）。

---

## 1. 现状勘查（实测）

| 端 | skills 落点 | 现状 | 格式 |
|---|---|---|---|
| WorkBuddy（江北） | `C:\Users\Ryuuzaki\.workbuddy\skills\<name>\SKILL.md` | 已装 ai-code-audit-handbook（本次新建） | SKILL.md（frontmatter name/description/version） |
| hermes | `E:\2222\3333\mimox-data\skills\<name>\SKILL.md` | 全局库 **39 个 skill**（anysearch 等，标准格式+scripts/） | 同格式 ✅ |
| DSH | `C:\Users\Ryuuzaki\.dsh\skills\<name>\SKILL.md` | 现有 j-space 先例 | 同格式 ✅（原生吃 Anthropic Skill） |
| zcode | 无全局 skills 目录 | 走插件 skills 组件（browser-use 插件带 skills/） | 同格式 ✅（兼容 Claude 生态） |
| 玄铁多 agent 镜像 | `mimox-data\.claude\skills\` `.codebuddy\skills\` 等几十个 `.agent名\skills\` | 已有内容（byted-web-search 等），是给各 agent 喂技能的现成体系 | 同格式 |

**结论**：四端格式完全一致（SKILL.md 已成事实标准），母本单源多端投放零格式转换成本。

## 2. 架构：母本单源 + 单向分发 + 台账

```
E:\2222\3333\skills-shared\          ← 母本库（git 管理，唯一权威，只放"跨端通用技能"）
  ├── ai-code-audit-handbook\
  │     ├── SKILL.md
  │     └── references\（可选）
  └── <future-skills>\
        │
        ↓ skill-sync.py（单向：母本 → 各端，分身永不写回母本）
  ├── WorkBuddy   ~/.workbuddy/skills/<name>/
  ├── hermes      mimox-data/skills/<name>/
  ├── DSH         ~/.dsh/skills/<name>/
  └── zcode       （v2：做成 xtmem-skills 小插件的 skills/ 组件）
```

- **方向**：单向分发（同 soul --sync）；母本改动 → 投放；各端本地改动不回写
- **台账**：`skills-shared/.sync-state.json` 记录每端每 skill 最后投放的 version/mtime，增量投放，无变更不写
- **版本**：母本 git 管理，每次改 SKILL.md 提交；投放记录版本号

## 3. 投放目标表

| # | 端 | 落点 | v1 支持 |
|---|---|---|---|
| 1 | WorkBuddy | `~/.workbuddy/skills/<name>/SKILL.md` | ✅ 复制 |
| 2 | hermes | `mimox-data/skills/<name>/SKILL.md` | ✅ 复制 |
| 3 | DSH | `~/.dsh/skills/<name>/SKILL.md` | ✅ 复制 |
| 4 | zcode | 小插件 `xtmem-skills` 的 `skills/` 组件 | ⏳ v2（需先立插件壳；临时可先手动放） |
| 5 | （可选）玄铁多 agent 镜像 | `mimox-data\.<agent>\skills\` | ⏳ 按需——那是给 Claude/Augment 等第三方 agent 喂技能的现成体系，skill-sync 可接管投喂 |

## 4. 命令设计

```bash
# 独立脚本（零依赖，Python 3.11，先于 XTMEM 主体落地）
python skill-sync.py sync [--target workbuddy,hermes,dsh]   # 全量/指定端投放
python skill-sync.py status                                  # 台账：每端每 skill 版本对齐情况
python skill-sync.py add <name>                              # 从母本新 skill 登记进台账
python skill-sync.py pull <端> <name>                        # 单 skill 单端手动补投

# XTMEM 主体落地后并入：xtmem.py skill --sync（同语义）
```

## 5. 保护规则（防覆盖他人资产）

1. **只投放母本里声明的 skill**——绝不扫各端目录反推，避免把 hermes 自带的 39 个 skill 拖进母本或误覆盖。
2. **同名冲突处理**：投放目标已存在同名目录且**不在台账管理内**（非 skill-sync 投放的）→ 跳过 + 警告（如 hermes 已有自己的 `ai-code-audit` 就先不盖）。
3. 台账管理内的（本工具投放过的）→ 按母本覆盖（skill 是知识资产非用户数据，覆盖安全）。
4. 首次投放前先备份目标端同名目录（`.sync-bak-<date>`）。

## 6. 落地步骤

| 步 | 动作 | 验收 |
|---|---|---|
| 1 | 建母本库 `skills-shared/` + git init；迁入 ai-code-audit-handbook 作为首个母本 skill | 母本 git log 有 baseline |
| 2 | 写 skill-sync.py（sync/status/add/pull + 台账 + 保护规则） | status 显示三端对齐 |
| 3 | 首次投放 ai-code-audit-handbook → WorkBuddy/hermes/DSH | 三端 `ls skills/ai-code-audit-handbook/SKILL.md` 都在且与母本一致 |
| 4 | （可选）hermes cron `xtmem-skill-sync`（每日）或手动 | 母本改动次日各端自动对齐 |
| 5 | v2：zcode 插件壳 + 并入 xtmem.py | zcode 端技能可用 |

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 误覆盖 hermes 39 个自带 skill | 只投母本声明的 + 同名冲突跳过警告（§5） |
| 各端 SKILL.md frontmatter 字段差异 | 统一以母本为准；各端对未知字段的容忍度 v2 实测（当前三端已知都认 name/description/version） |
| 母本与某端已有同名不同内容 | 首次备份 + 冲突跳过，人工裁决 |
| zcode 端暂无落点 | v2 立插件壳，v1 先三端 |

## 8. 与 XTMEM 的关系

- skill-sync **不依赖记忆引擎**，可独立先行（今天投三端审计 skill）
- 列入 XTMEM v2.1 议题，与 agents.md --sync 一并做自动同步（cron）
- 后续任何"跨端通用技能"（审计/写作/翻译等）都走母本分发，一处维护四处可用

---

*江北 · 2026-08-25 · 依据：四端 skills 目录实地勘查（hermes 39 先例 / DSH j-space 先例 / zcode 插件 skills / WorkBuddy 本次新建）*
