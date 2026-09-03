# -*- coding: utf-8 -*-
"""xtmem_timeline.py — P5.0-f 时间骨架生成器（月度骨架 + 近48h 摘要）（2026-09-02，小D）。

Z指导 裁决（2026-09-02）：月度骨架走 chunk 路线，不建独立月报文件——注入面只读库；
  血缘/回滚/幂等白拿 + 天然在 visibility 体系内（月度报告已有 xtmem_monthly.py 文件版，职责分开）。

产出（每域一条）：
  - track='timeline' / tier='refined' / importance=1.0 / derived_from='timeline'
  - source_ids=[用到的 chunk id...]（血缘闸 P0.1 + min_visibility 继承，跨可见性源自动最严）
  - 内容 = 近12个月月度骨架（每月一行：条数+要点[src]）+ 近48h 摘要（按日一行）+ 尾行（生成方式）
  - 旧活时间骨架置 state='superseded' + superseded_by=新版（P2.1 生命周期，画像层同款）

幂等口径（docstring 写死——Z指导 2026-09-02 要求）：
  - 月度锚 = 标题 YYYYMM（粒度月不用天）：同月已存在活时间骨架 → 默认跳过（月内重跑幂等，
    不产新版不翻版——48h 段随运行变化，纯 content_hash 会月内翻版，故锚=月存在性判定）；
  - --refresh-48h：同月强制产新版（近48h 刷新），旧版置 superseded（与画像层跨天同款）；
  - 整批回滚：DELETE FROM chunks WHERE derived_from='timeline'。

用法：python xtmem_timeline.py [--domain <ns>] [--months 12] [--refresh-48h]
环境：XTMEM_DB_PATH 可覆盖（测试副本）；XTMEM_CLI 覆盖 CLI。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from calendar import monthrange
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
DEFAULT_CLI = os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py")
PYTHON = sys.executable
DOMAINS = ["dsh", "xuantie_lark", "workbuddy", "zcode", "default"]
DERIVED = "timeline"
PREVIEW = 140  # 每行要点截断


def _conn(db):
    import sqlite3
    return sqlite3.connect(db, timeout=30)


def _month_bounds(ym: str):
    """'YYYYMM' → (start_ts, end_ts)。"""
    y, m = int(ym[:4]), int(ym[4:6])
    days = monthrange(y, m)[1]
    start = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(y, m, days, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    return start, end


def _recent_months(n):
    now = datetime.now(timezone.utc)
    keys = []
    y, m = now.year, now.month
    for _ in range(n):
        keys.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(keys))


def _pick(rows):
    """行列表 → (条数, 要点行)（按 importance 抽最好的那条）。"""
    if not rows:
        return 0, ""
    top = max(rows, key=lambda r: (r["importance"] or 0, r["timestamp"] or 0))
    return len(rows), f"{str(top['content'])[:PREVIEW]} [src:{top['id'][:8]}]"


def _store_via_cli(content, ns, source_ids):
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = ns or "dsh"
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", ns or "dsh", "--namespace", ns or "dsh",
            "--text", content, "--importance", "1.0",
            "--derived-from", DERIVED, "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "timeline", "--tags", "timeline,xtmem"]
    try:
        proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                              encoding="utf-8", timeout=180, env=env)
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _supersede_old(conn, ns, new_id):
    cur = conn.execute(
        "UPDATE chunks SET state='superseded', superseded_by=? "
        "WHERE namespace=? AND state='active' AND track='timeline' AND id!=?",
        (new_id, ns, new_id))
    conn.commit()
    return cur.rowcount


def _month_exists(conn, ns, stamp):
    return conn.execute(
        "SELECT 1 FROM chunks WHERE namespace=? AND track='timeline' AND state='active' "
        "AND content LIKE '%' || ? || '%' LIMIT 1",
        (ns, stamp)).fetchone() is not None


def _run_domain(conn, ns, months, refresh):
    now = time.time()
    # 月度骨架：近 N 月，每月一行
    month_lines = []
    src_ids = []
    for ym in _recent_months(months):
        s, e = _month_bounds(ym)
        rows = conn.execute(
            "SELECT id, content, importance, timestamp FROM chunks WHERE state='active' "
            "AND namespace=? AND timestamp>=? AND timestamp<? ORDER BY importance DESC LIMIT 1",
            (ns, s, e)).fetchall()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE state='active' AND namespace=? AND timestamp>=? AND timestamp<?",
            (ns, s, e)).fetchone()[0]
        n, pick = _pick(rows)
        if pick:
            month_lines.append(f"- {ym}: {cnt} 条 · {pick}")
        else:
            month_lines.append(f"- {ym}: 无记录（不报错，空位留白）")
        src_ids += [r["id"] for r in rows]
    # 近 48h：按日一行
    cutoff = now - 48 * 3600
    rows48 = conn.execute(
        "SELECT id, content, importance, timestamp FROM chunks WHERE state='active' "
        "AND namespace=? AND timestamp>=? ORDER BY importance DESC LIMIT 200",
        (ns, cutoff)).fetchall()
    day_lines = []
    by_day = {}
    for r in rows48:
        day = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(r)
    for day in sorted(by_day):
        n, pick = _pick(by_day[day])
        day_lines.append(f"- {day}: {n} 条 · {pick}")
    src_ids += [r["id"] for r in rows48]
    if not month_lines and not day_lines:
        return {"ok": False, "ns": ns, "reason": "empty-domain"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m")
    # 幂等：同月已有活时间骨架 → 默认跳过（月内重跑不翻版）；--refresh-48h 才产新版
    if _month_exists(conn, ns, stamp) and not refresh:
        return {"ok": True, "ns": ns, "skipped": "same-month-exists", "stamp": stamp}
    lines = [f"# 时间骨架 · {ns} · {stamp}", "", "## 月度摘要（近 12 个月）"] + month_lines
    lines += ["", "## 近 48h"] + (day_lines or ["- 近48h 无记录"])
    lines += ["", "---", "生成方式：rule + 时间窗（月度锚 = YYYYMM；同月重跑默认跳过，--refresh-48h 强制新版并 supersede 旧版）"]
    content = chr(10).join(lines)
    if not src_ids:
        return {"ok": False, "ns": ns, "reason": "no-sources"}
    r = _store_via_cli(content, ns, src_ids[:800])
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "reason": "store failed", "r": r}
    new_id = r.get("id")
    sup = _supersede_old(conn, ns, new_id) if new_id else 0
    return {"ok": True, "ns": ns, "stamp": stamp, "id": new_id, "superseded": sup,
            "month_lines": len(month_lines), "day_lines": len(day_lines)}


def main():
    ap = argparse.ArgumentParser(prog="xtmem_timeline")
    ap.add_argument("--domain", default=None)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--refresh-48h", action="store_true")
    args = ap.parse_args()
    conn = _conn(DEFAULT_DB)
    conn.row_factory = __import__("sqlite3").Row
    domains = [args.domain] if args.domain else DOMAINS
    results = [_run_domain(conn, ns, args.months, args.refresh_48h) for ns in domains]
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
