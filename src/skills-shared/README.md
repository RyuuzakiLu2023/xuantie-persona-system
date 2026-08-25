# Skills-Shared · 跨端技能母本库

> 单一权威来源（single source of truth）：**只放"跨端通用技能"**。
> 分发方向：母本 → 各端单向（skill-sync），各端改动不回写母本。
> 关联方案：《XTMEM-skill-sync方案-v1.0.md》（F:\Ryuuzaki\Documents\）

## 投放目标（实测落点）

| 端 | 落点 | 状态 |
|---|---|---|
| WorkBuddy（江北） | `C:\Users\Ryuuzaki\.workbuddy\skills\<name>\` | ✅ 已投 |
| hermes | `E:\2222\3333\mimox-data\skills\<name>\`（**active 目录**） | ⏳ 待投 |
| DSH | `C:\Users\Ryuuzaki\.dsh\skills\<name>\` | ⏳ 待投 |
| zcode | 插件 skills 组件（v2） | ⏳ v2 |

## hermes 侧注意事项（2026-08-25 实测）

- hermes 技能分**三目录**：active = `mimox-data\skills`（39 个）/ disabled = `E:\2222\skills-disabled`（23 个）/ archived = `E:\2222\3333\skills-archived`（46 个）
- `skills-catalog.md` 由 `mimox-data\scripts\skills_catalog_update.py` 扫描三目录生成（有 SKILL.md 即算技能，**不去重**）
- `skills-lock.json` 记录来源锁定 + hash（安全机制）
- **投放动作 = 复制进 active 目录 + 重新生成 catalog**（否则玄铁 skill_view 看不到）
- 同名冲突：active 已存在同名 → 跳过警告，人工裁决

## 母本 skill 清单

| 名称 | 版本 | 说明 | 母本状态 |
|---|---|---|---|
| ai-code-audit-handbook | 1.0.0 | 实战代码审计手册（文档对账/patch 靶点/注入实跑） | ✅ 已入库（2026-08-25） |
