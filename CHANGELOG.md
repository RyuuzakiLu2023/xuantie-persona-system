# XTMEM · 公开版发布 Changelog

> 目标仓库：`https://github.com/RyuuzakiLu2023/xuantie-persona-system`
> 许可证：MIT License  
> 规范基线：XTMEM 共享底座架构规范

---

## [1.1.0.dev0] — 未发布整备

- 从现役正本恢复完整34命令和共用引擎，不再以五命令裁剪代表多端记忆产品。
- 补齐自动蒸馏的窗口、评判提炼、提交、通用调度和配套来源整理模块；模型地址与密钥由使用者显式提供。
- 保留Hermes、DSH、ZCode/MCP接头，公共安装共用一份引擎与CLI；不带私人角色/端口/库/会话夹具。
- 公开路径默认改用户目录，wheel按已安装memory包定位；console入口返回整数退出码。
- 最终冻结后公开安装、核心、端接头与蒸馏合成组合132项通过；wheel源码外安装3项及raw到refined整链通过。未验边界见docs/public-validation.md。
- 本地工作树未提交、未推送、未打标签；历史v1.0.0远端裁剪版不被覆盖。

## [Unreleased]

### 发布范围整理（2026-10-21，尚未封版）
- 开始按运行源码核对公开范围；未执行批量同步，未跑测试、构建、提交、推送或创建 Release。
- 保留现有 README 的介绍与结构，纠正“备份即拷贝文件”的恢复承诺；撤下尚未匹配原始记录的测试通过断言，不把失败当成跳过。
- 已确认公开 CLI 的引擎发现依赖源码相邻目录；wheel 的实际安装布局不在该候选列表中。安装可用性尚未有对应证据，本轮不修产品，不将 wheel 标为可用制品。
- 暂缓范围：依赖未齐或尚未核当前修复的客户端、后台自动整理、远程管理、消息/房间授权和完整恢复能力；最终清单尚在整理，不表示相关能力全部不可用。
- 后续：先完成架构、能力与文件清单，再按现有版本规范确定本版；已知问题修复后更新本日志并使用新版本，不覆盖旧标签。

### Changed
- 新增版本号规范，明确日常开发与发布批次、Python/预发布标签格式，以及产品、数据库、协议和文档版本的区别。
- 根据既有长期迭代、版本号未维护的实际情况，建议首个规范正式发布以 1.0.0 建立基线；旧 0.1.0 历史记录保留。

## [0.1.0] - 2026-09-08

### 核心亮点
- **开源合规与治理标准化**：补齐 MIT LICENSE、SECURITY.md、CONTRIBUTING.md 与 GitHub Actions CI 流水线。
- **开箱即用工程化**：提供标准 `pyproject.toml`、`requirements.txt` 与 `requirements-dev.txt`，支持纯 Python 干净环境直接安装 `pip install -e .`。
- **文档分层与收口**：将内部历史施工记录与 Spec 归档至 `docs/history/`，新增面对社区开发者的 `docs/getting-started.md` 与精简版 `README.md`。

### 架构与核心组件（已同步核验）
- `src/cli/xtmem.py`：独立通用 CLI，支持 `store` / `recall` / `lookup` / `msg` / `bulletin` 等指令。
- `src/hermes-plugin/memory/`：Hermes 平台原生内存提供者实现（支持双层检索、曝光五态记账与 Support Bundle 组装）。
- `src/dsh/xtmem-tools/`：DeepSeek Harness (DSH) 工具层插件与端侧记忆注入适配。
- `src/zcode/mcp/`：zcode MCP 服务端与 Stdio/Hooks 协议实现。
- `src/skills-shared/`：四端共享的技能定义与同步母本。
- `src/scripts/`：后台异步维护脚本（健康巡检、记忆代谢、蒸馏清洗等）。
- `tests/`：单元测试与隔离安全断言集。

### 治理与修正
- 修复早期 Changelog 中错误的相对路径前缀（如 `srcdsh/` → `src/dsh/`）。
- 明确前端控制台（照渊台 Cockpit）独立演进，暂不耦合于当前轻量发布包。
- 确立全量发布前自动化脱敏与盘符路径阻断门禁（`scripts/scan_drive_letters.py`）。
