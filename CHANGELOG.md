# XTMEM · 公开版发布 Changelog

> 目标仓库：`https://github.com/RyuuzakiLu2023/xuantie-persona-system`  
> 许可证：MIT License  
> 规范基线：XTMEM 共享底座架构规范

---

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
