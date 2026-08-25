# XTMEM 阶段 1.7 · config 迁移清单（准备版，未落生产）

> 原则（D12）：切换完成前**不修改生产 config.yaml**；本清单由龙崎在切换时执行（或由 AI 出完整切换清单后一起执行）。
> 激活键：0.20.5 的 \`memory.provider\`（空 = 仅内建；只允许一个外部 provider）。

## 1. 需要改的文件

| 文件 | 现状 | 改后 |
|---|---|---|
| mimox-data/config.yaml（根 default） | 无 memory 段 | 加 \`memory.provider: xtmem\` |
| mimox-data/profiles/xuantie_lark/config.yaml | \`memory.provider: ''\`（347-353 行附近） | \`memory.provider: xtmem\` |
| mimox-data/profiles/novel/config.yaml | \`memory.provider: ''\` | \`memory.provider: xtmem\`（novel 也装同一插件，namespace 自动=novel） |
| $HERMES_HOME/xtmem.yaml | 不存在 | 按需放（见插件内 \`xtmem.yaml.example\`）；不建=全默认 |

## 2. memory 段建议值（各 profile）

\`\`\`yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200     # 规格书硬顶：2200
  user_char_limit: 1375       # 规格书硬顶：1375
  nudge_interval: 10          # 0.20.5 默认 10；如需旧行为再拍板
  provider: xtmem             # ★ 激活 XTMEM（单选）
\`\`\`

## 3. memory_advanced 段处理

- 0.19 的自建 \`memory_advanced\` 段（embedding/retrieval/compression）仍在 config.yaml 里，0.20.5 不再由官方引擎读取；
- XTMEM provider 会自动从 \`config.yaml\` 读取 \`memory_advanced\`，并用 \`$HERMES_HOME/xtmem.yaml\` 覆盖；
- 因此**保持 memory_advanced 段不动即可兼容**；想迁走的可复制到 xtmem.yaml（见示例）。

## 4. 验证命令（切换后跑）

\`\`\`bash
# 确认 provider 被发现
hermes memory status   # 0.20.5 应有 xtmem / active
# 确认库 schema 迁移幂等
python -c "import sqlite3; c=sqlite3.connect(r'E:\2222\3333\mimox-data\.hermes\memory\vector_store.db'); print([r[1] for r in c.execute('PRAGMA table_info(chunks)')])"
# 应看到 namespace/project/host_origin/updated_at；跑两次无报错
\`\`\`


## 5. v1.1 核实补充（2026-08-25，恢复推进后实测）

- **实测行号**：mimox-data/config.yaml 无 memory 段（memory_advanced 在 31-32 行）；xuantie_lark 的 memory 段 347-353（provider: 空），memory_advanced 957-975；novel 的 memory 段 346-352（provider: 空），memory_advanced 828-846。清单与实际一致。
- **✅ memory_char_limit（已拍板 2026-08-25）**：**保留 4000**，切换时不动（xuantie_lark/novel 现状）；USER.md 1375 不动。0.20.5 官方默认 2200 仅作参考。
- **memory.provider 放两层**：规格书说全局 config 加即可，但 **profile config 显式 provider: 空 会覆盖/禁掉根配置**，因此切换时需同时改：根 config.yaml 新增 memory 段（provider: xtmem）+ xuantie_lark/novel 两 profile 的 provider 改为 xtmem，三者一致。
- **插件落点**（全局可见）：mimox-data/plugins/xtmem/ + profiles/xuantie_lark/plugins/xtmem、profiles/novel/plugins/xtmem 两个 junction（已存在，切换不受源码替换影响，直接保留）。

*江北（WorkBuddy）整理 · 2026-08-25*
