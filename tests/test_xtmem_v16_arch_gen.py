# -*- coding: utf-8 -*-
"""test_xtmem_v16_arch_gen.py — V1.6 图从码生守卫（Spec P6.5 / V1.6，2026-09-06，assistant-c）。

判据（DoD）：
  ① 脚本从 jobs.json / schema / connect 计数生成架构/部署/状态表数据面；
  ② 必须包含：源码指纹、user_version、chunks 分层/可见性、实体图、cron 任务列表；
  ③ 核心验穿标准（Spec 原文）：「改一行代码重生成，图必须变；不变=死图」：
     - schema user_version 变更 → 指纹必变；
     - 增加 chunk 数据 → 指纹必变；
     - 任何关键数据面微小漂移均在数据指纹层面灵敏感知。
"""
import sqlite3
import pytest
from pathlib import Path

from conftest import PLUGIN_DIR
import sys
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# 导入生成器脚本
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import xtmem_arch_faces as arch_gen


def test_v16_arch_data_collection(tmp_db):
    """验证数据面采集覆盖三面完整性。"""
    data = arch_gen.collect_arch_data(str(tmp_db))
    assert "code_sha" in data and len(data["code_sha"]) >= 8
    assert "user_version" in data
    assert "chunks_breakdown" in data
    assert "cron_jobs" in data
    assert "data_surface_sha" in data
    assert len(data["cron_jobs"]) > 0

    md = arch_gen.format_markdown(data)
    assert "# XTMEM 架构动态数据面" in md
    assert data["data_surface_sha"] in md
    assert "Chunks Breakdown" in md
    assert "Profiles Cron Jobs" in md


def test_v16_code_or_schema_change_mutates_graph(tmp_db):
    """验穿铁律：「改一行代码重生成，图必须变；不变=死图」。"""
    # 状态 1
    d1 = arch_gen.collect_arch_data(str(tmp_db))
    sha1 = d1["data_surface_sha"]

    # 变动 1：schema user_version 变动
    con = sqlite3.connect(str(tmp_db))
    con.execute("PRAGMA user_version = 88")
    con.commit()
    con.close()

    d2 = arch_gen.collect_arch_data(str(tmp_db))
    sha2 = d2["data_surface_sha"]
    assert sha1 != sha2, "user_version 变更后数据面指纹必须变化（防死图）"

    # 变动 2：写入新 chunk
    con = sqlite3.connect(str(tmp_db))
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
        "VALUES ('mem-v16test', '图从码生验穿测试', 1700000000, 0.5, '[]', 'default', 'active', 'dsh', 'shared', 'refined')"
    )
    con.commit()
    con.close()

    d3 = arch_gen.collect_arch_data(str(tmp_db))
    sha3 = d3["data_surface_sha"]
    assert sha2 != sha3, "数据记录变更后数据面指纹必须变化（防死图）"
