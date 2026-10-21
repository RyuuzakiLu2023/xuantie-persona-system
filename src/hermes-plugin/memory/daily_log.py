"""每日日志记录模块 — session 启动/结束时写日志到 memories/ 目录。

数据目录：~/.hermes/memories/
    working-buffer.md   — 当前任务 + 临时笔记
    knowledge-index.md  — 知识索引表格
    YYYY-MM-DD.md       — 每日工作记录
"""

import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_memories_dir(hermes_home: str = None) -> Path:
    if not hermes_home:
        hermes_home = os.path.expanduser("~/.hermes")
    return Path(hermes_home) / "memories"


def ensure_files(hermes_home: str = None) -> dict:
    """确保 memories/ 目录和基础文件存在。返回文件路径字典。"""
    mem_dir = _get_memories_dir(hermes_home)
    mem_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "working_buffer": mem_dir / "working-buffer.md",
        "knowledge_index": mem_dir / "knowledge-index.md",
        "daily_log": mem_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md",
    }

    # working-buffer.md
    if not paths["working_buffer"].exists():
        paths["working_buffer"].write_text(
            "# Working Buffer\n\n## 当前任务\n- 无\n\n## 临时笔记\n- 无\n",
            encoding="utf-8",
        )

    # knowledge-index.md
    if not paths["knowledge_index"].exists():
        paths["knowledge_index"].write_text(
            "# Knowledge Index\n\n| 文件 | 主题 | 效用 | 日期 |\n|------|------|------|------|\n",
            encoding="utf-8",
        )

    # 每日记录
    if not paths["daily_log"].exists():
        paths["daily_log"].write_text(
            f"# {datetime.now().strftime('%Y-%m-%d')}\n\n"
            "## 工作记录\n- 无\n\n"
            "## 学习笔记\n- 无\n",
            encoding="utf-8",
        )

    return {k: str(v) for k, v in paths.items()}


def log_session_start(hermes_home: str = None, session_id: str = None, model: str = None) -> None:
    """记录 session 开始到每日日志。"""
    try:
        paths = ensure_files(hermes_home)
        now = datetime.now().strftime("%H:%M:%S")
        entry = f"- {now} Session 开始"
        if session_id:
            entry += f" [{session_id[:8]}]"
        if model:
            entry += f" model={model}"

        with open(paths["daily_log"], "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception as e:
        logger.warning("Daily log write failed: %s", e)


def log_session_end(hermes_home: str = None, turn_count: int = 0) -> None:
    """记录 session 结束到每日日志。"""
    try:
        paths = ensure_files(hermes_home)
        now = datetime.now().strftime("%H:%M:%S")
        with open(paths["daily_log"], "a", encoding="utf-8") as f:
            f.write(f"- {now} Session 结束 ({turn_count} turns)\n")
    except Exception as e:
        logger.warning("Daily log write failed: %s", e)


def update_working_buffer(hermes_home: str = None, task: str = None, note: str = None) -> None:
    """更新工作缓冲区（当前任务或临时笔记）。"""
    try:
        paths = ensure_files(hermes_home)
        wb_path = Path(paths["working_buffer"])
        content = wb_path.read_text(encoding="utf-8")

        if task:
            content = _replace_section(content, "## 当前任务", f"- {task}\n")

        if note:
            content = _replace_section(content, "## 临时笔记", f"- {note}\n")

        wb_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.warning("Working buffer update failed: %s", e)


def _replace_section(content: str, section_header: str, new_line: str) -> str:
    """替换 content 中对应 section 的内容（追加新行）。"""
    if section_header not in content:
        return content + f"\n{section_header}\n{new_line}"

    idx = content.index(section_header)
    end = content.find("##", idx + len(section_header))
    if end == -1:
        end = len(content)

    after_header = content.index("\n", idx) + 1
    existing = content[after_header:end].strip()
    if existing == "- 无":
        # 替换 "无"
        return content[:after_header] + new_line + content[end:]
    else:
        # 追加
        return content[:idx] + content[idx:end].rstrip() + "\n" + new_line + "\n" + content[end:]
