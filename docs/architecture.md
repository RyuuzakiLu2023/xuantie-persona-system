# XTMEM 通用架构

## 一份引擎，多端身份

```text
Hermes provider ──────────────────┐
DSH 接头 → 已安装 CLI ────────────┤
ZCode / MCP / hooks → CLI ────────┤
脚本 / 使用者 → CLI ──────────────┘
                   │
          ContextBuilder / VectorStore
          ├── 正文校验、来源与读者交集
          ├── 操作/观察身份与重试判断
          ├── 应用 WAL、回执与遗忘存根
          └── SQLite：正文、全文索引、向量缓存
                   │
     raw 原料 → 窗口账本 → LLM 提炼 → refined 卡片
                   │
      refined 达门槛就用，否则回落 raw
                   │
          各端自行组装当轮上下文
```

端接头固定 actor，决定怎么接宿主事件。共用代码不等于共用会话状态；私有与共享读取由权限模块执行，不能靠目录名推断身份。

## 写入与来源

CLI `store` 调用观察/写入链；Hermes 自动保存先落 outbox，再由 worker 处理。收到、持久落盘、入索引和抽取是不同阶段。SQLite WAL 与应用级寄存 WAL 是两套机制，恢复不能只复制主库文件。

派生产物必须登记实际来源，读者集合取来源交集并受声明范围约束。`source_ids` 是引用记录，不是不可伪造的密码学证明；内容正确性仍取决于来源与提炼过程。

## 自动蒸馏

raw 是材料，refined 是手工记录或提炼卡片。窗口账本记录哪些材料等待处理，LLM 层提炼和提交；粗蒸馏提供不依赖 LLM 的聚合路径。调度由使用者设置，不附带维护者私人 cron。

不同通用整理脚本及限制见 distillation.md；不能以库里存在 refined 列就声称自动蒸馏已启用。

## 检索与注入

全文检索/BM25提供关键词腿，Ollama `/api/embed`提供向量腿。向量不可用时降级；raw-fallback仅表示精选层未达到门槛，不等于向量故障。

CLI工具返回、Hermes自动注入、DSH组装、ZCode hooks是不同出口。曝光五态区分找到/展示/引用/确认/纠正，但字段存在不证明每端反馈可靠，也不等于自动判断事实真伪。

## 公开边界

通用引擎、端接头与整理源码保留；私人库、角色配置、密钥、飞书桥、真实夹具和部署自动化不提供。HTTP和高级消息接口不默认开放，已知问题见 known-issues.md。

## 布局

- `src/cli/xtmem.py`：完整CLI，wheel安装为xtmem模块。
- `src/hermes-plugin/memory`：共用引擎，wheel安装为memory包。
- `src/hermes-plugin`：Hermes宿主接头，wheel另提供xtmem_hermes包。
- `src/scripts`：通用整理模块，wheel安装为xtmem_jobs包。
- `src/zcode`、`src/dsh`：宿主安装资源与适配。

源码与wheel均须使用目标目录的实现验证，不从维护者生产目录借包。
