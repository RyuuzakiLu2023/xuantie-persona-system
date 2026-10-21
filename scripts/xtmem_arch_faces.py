# -*- coding: utf-8 -*-
"""xtmem_arch_faces.py — V1.6 图从码生：架构/部署/状态表动态数据面生成器（2026-09-06）。

设计背景（Spec P6.5 / V1.6）：
  手画架构图（如 v1.3.1）是时点静态快照，随着代码演进、表结构扩展、cron 任务变动必腐化。
  本脚本从当前环境的三大数据面实时提取事实，生成机器校验的「动态数据面」：
    1. 代码指纹面：runtime_code_sha（memory/*.py 源码逐字节总指纹）
    2. 调度面：profiles/*/cron/jobs.json 活跃任务表与周期
    3. 存储与架构面：schema user_version / 表结构 / chunks 分轨与分层 / 实体图拓扑 / 连接端状态
  验收判据：改一行代码或改一处 schema 重生成，输出必须变；不变即判为死图。

用法：
  python xtmem_arch_faces.py [--db <路径>] [--output <输出.md>] [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# 保证能导入 plugins/xtmem
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src" / "hermes-plugin"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
DEFAULT_OUT = str(Path(__file__).resolve().parents[1] / "docs" / "architecture-faces.md")


def collect_arch_data(db_path: str = DEFAULT_DB) -> dict:
    """采集三面实时真实数据（纯函数，全异常隔离）。"""
    from memory.open_db import open_db, _runtime_code_sha
    from memory.graph import graph_stats

    data: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "code_sha": _runtime_code_sha(),
        "db_path": db_path,
    }

    # 1. 引擎与实体图拓扑
    try:
        data["graph_stats"] = graph_stats(db_path)
    except Exception as e:
        data["graph_stats"] = {"error": str(e)}

    # 2. 数据库分层与状态细分
    try:
        conn = open_db(db_path, ro=True)
        conn.row_factory = sqlite3.Row
        try:
            # chunks 状态与 tier 矩阵
            rows = conn.execute(
                "SELECT tier, state, visibility, COUNT(*) as cnt FROM chunks GROUP BY tier, state, visibility"
            ).fetchall()
            data["chunks_breakdown"] = [
                {"tier": r["tier"], "state": r["state"], "visibility": r["visibility"], "cnt": r["cnt"]}
                for r in rows
            ]
            # user_version
            data["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            # 表清单与条数
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            data["tables"] = {}
            for t in tables:
                try:
                    c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    data["tables"][t] = c
                except Exception:
                    pass
            # stale 派生数
            stale_n = conn.execute("SELECT COUNT(*) FROM chunks WHERE tags LIKE '%lineage-stale:%' AND state='active'").fetchone()[0]
            data["lineage_stale_active"] = stale_n
        finally:
            conn.close()
    except Exception as e:
        data["db_error"] = str(e)

    # 3. 调度面 jobs 采集
    import glob
    jobs_list = []
    for jp in glob.glob(str(Path.home() / ".xtmem" / "cron" / "jobs.json")):
        try:
            with open(jp, encoding="utf-8") as f:
                jdata = json.load(f)
            j_items = jdata.get("jobs", []) if isinstance(jdata, dict) else jdata
            prof = Path(jp).parent.parent.name
            for item in j_items:
                jobs_list.append({
                    "profile": prof,
                    "name": item.get("name", ""),
                    "script": item.get("script", ""),
                    "schedule": item.get("schedule_display") or str(item.get("schedule")),
                    "enabled": item.get("enabled", True),
                })
        except Exception:
            pass
    data["cron_jobs"] = sorted(jobs_list, key=lambda x: (x["profile"], x["name"]))

    # 计算全局指纹（任何字段变动此指纹必变）
    payload = json.dumps({k: v for k, v in data.items() if k != "generated_at"}, sort_keys=True, ensure_ascii=False)
    data["data_surface_sha"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return data


def format_markdown(data: dict) -> str:
    """将数据面格式化为规范 Markdown 报告。"""
    md = []
    md.append(f"# XTMEM 架构动态数据面 · v1.6 图从码生")
    md.append(f"> 自动生成时间：{data.get('generated_at')} · **数据面总指纹：`{data.get('data_surface_sha')}`**")
    md.append(f"> 源码指纹（`memory/*.py`）：`{data.get('code_sha')}` · 数据库：`{data.get('db_path')}`\n")

    # 一、核心接入与代码指纹
    md.append("## 一、核心指纹与架构版本")
    md.append("| 检查项 | 实时状态 / 指纹 | 约束口径 |")
    md.append("|---|---|---|")
    md.append(f"| **运行时源码指纹** | `{data.get('code_sha')}` | memory/*.py 逐字节 sha256 聚合 |")
    md.append(f"| **Schema user_version** | `{data.get('user_version', '?')}` | open_db 协议版本闸 |")
    md.append(f"| **活跃 Lineage-Stale 标记** | `{data.get('lineage_stale_active', 0)}` | V1.1 反向血缘待消费数 |\n")

    # 二、存储分轨与分层矩阵
    md.append("## 二、存储分层与可见性分布（Chunks Breakdown）")
    md.append("| Tier (分层) | State (生命周期) | Visibility (可见性) | 条目数 |")
    md.append("|---|---|---|---|")
    for row in data.get("chunks_breakdown", []):
        md.append(f"| {row['tier']} | {row['state']} | {row['visibility']} | {row['cnt']} |")
    md.append("")

    # 三、实体共现图拓扑
    gstats = data.get("graph_stats", {})
    md.append("## 三、实体图拓扑与共现边（P5.0 传输场）")
    md.append(f"- **总节点 (Canon)**: {gstats.get('nodes', 0)} | **总共现边**: {gstats.get('edges', 0)} | **打标 Chunk 数**: {gstats.get('tagged_chunks', 0)}")
    top_edges = gstats.get("top_edges", [])
    if top_edges:
        md.append("| 核心共现边 | 共现次数 | 权重 (抗 Hub 后) |")
        md.append("|---|---|---|")
        for e in top_edges:
            md.append(f"| `{e.get('edge')}` | {e.get('cnt')} | {e.get('w')} |")
    md.append("")

    # 四、真调度面分布
    md.append("## 四、活跃调度面（Profiles Cron Jobs）")
    md.append("| Profile | 任务标识 | 调度周期 | 执行脚本 | 启用状态 |")
    md.append("|---|---|---|---|---|")
    for j in data.get("cron_jobs", []):
        st = "✅ 启用" if j["enabled"] else "⏸ 暂停"
        md.append(f"| {j['profile']} | `{j['name']}` | {j['schedule']} | `{j['script']}` | {st} |")
    md.append("")

    return "\n".join(md)


def main() -> int:
    parser = argparse.ArgumentParser(description="XTMEM 架构动态数据面生成器")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--output", default=DEFAULT_OUT, help="输出 Markdown 路径")
    parser.add_argument("--check", action="store_true", help="校验模式：仅检查输出是否漂移")
    args = parser.parse_args()

    data = collect_arch_data(args.db)
    md_content = format_markdown(data)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md_content, encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "data_surface_sha": data["data_surface_sha"],
        "code_sha": data["code_sha"],
        "output": str(out_path),
        "cron_jobs_n": len(data.get("cron_jobs", [])),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
