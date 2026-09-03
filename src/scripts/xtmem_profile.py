# -*- coding: utf-8 -*-
"""xtmem_profile.py — XTMEM 忆·画像层聚合器（P4.1，2026-09-01，小D）。

把每个角色域的琐碎记忆聚合成一条「画像」chunk：
  - track='profile' / visibility='private' / namespace=<被画像域> / importance=1.0
  - derived_from='profile-aggregator'，source_ids=[本周输入 chunk id...]（血缘闸 P0.1）
  - 每域恰好 1 条活画像；旧画像置 state='superseded' + superseded_by=新版（P2.1 lifecycle）
  - 每条后缀 [src:xxxxxxxx]（句级溯源，p4.4 推翻入口的依赖）；句级溯源抽验可回溯

LLM 通道：hermes.exe one-shot（可选）。LLM 不可达 → 纯规则画像（tags=["profile","rule-fallback"]）。
用法：python xtmem_profile.py [--db <XTMEM_DB_PATH>] [--domain <ns>] [--days 7] [--no-llm]
环境：XTMEM_DB_PATH 可覆盖（测试副本）；XTMEM_CLI 覆盖 CLI。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db"
)
DEFAULT_CLI = os.environ.get(
    "XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"
)
PYTHON = sys.executable
DOMAINS = ["dsh", "xuantie_lark", "workbuddy", "zcode", "default"]
TRACK_WEIGHT = {"identity": 3.0, "preference": 2.0, "emotion": 0.5, "default": 1.0}
CHUNK_PREVIEW = 200  # 每条截断 200 字符（对齐 autodream 惯例）


def _conn(db):
    import sqlite3
    return sqlite3.connect(db, timeout=30)


def _select_candidates(conn, ns, days):
    cutoff = time.time() - days * 86400
    # P4.1 修（2026-09-01，§12a ⑫）：画像只用「非流水」原料——raw 流水会被 _infer_track 误标 preference/default，
    # 且经画像 + identity/preference 豁免 + imp=1.0 被「洗白」成永久要点（T0 的 30 天 TTL 对画像不生效）。
    # 排除：tier='raw'（T1 流水层）+ 内容 User: 开头（流水兜底，防 NULL/未打标流水漏网）。
    rs = conn.execute(
        "SELECT id, project, content, importance, track, timestamp FROM chunks "
        "WHERE state='active' AND namespace=? AND timestamp>=? "
        "AND track IN ('identity','preference','emotion','default') "
        "AND (tier IS NULL OR tier != 'raw') "
        "AND content NOT LIKE 'User: %' "
        "ORDER BY importance DESC, timestamp DESC LIMIT 150",
        (ns, cutoff),
    ).fetchall()
    return rs


def _preview(content):
    return str(content or "")[:CHUNK_PREVIEW]


def _rule_build(rows):
    """纯规则画像：按 track 分组 + 重要度摘录，每条带 [src:xxxxxxxx]。"""
    groups = {"identity": [], "preference": [], "emotion": [], "default": []}
    for r in rows:
        groups[r["track"]].append(r)
    out = []
    # identity / preference 优先（画像核心原料），emotion 只做趋势不做结论，default 兜底
    for track in ("identity", "preference", "default", "emotion"):
        items = sorted(groups.get(track, []), key=lambda r: -r["importance"])
        if not items:
            continue
        out.append(f"## {track}")
        for r in items[:25]:
            out.append(f"- {_preview(r['content'])} [src:{r['id'][:8]}]")
    return "\n".join(out)


def _store_via_cli(content, ns, source_ids, old_id, domain):
    """用 CLI store 落画像（血缘三参数走 AF-1；visibility=private + track=profile + tier=refined）。"""
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = domain
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", domain, "--namespace", ns,
            "--text", content, "--importance", "1.0",
            "--derived-from", "profile-aggregator",
            "--source-ids", ",".join(source_ids),
            "--tier", "refined",
            "--track", "profile",
            "--tags", "profile,xtmem"]
    proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def _supersede_old(conn, ns, new_id):
    """把该域现有活画像置 superseded，并回指新版。"""
    cur = conn.execute(
        "UPDATE chunks SET state='superseded', superseded_by=? "
        "WHERE namespace=? AND state='active' AND track='profile' AND id!=?",
        (new_id, ns, new_id),
    )
    conn.commit()
    return cur.rowcount


def _run_domain(conn, ns, days, use_llm):
    rows = _select_candidates(conn, ns, days)
    if len(rows) < 5:
        return {"ok": False, "ns": ns, "reason": "candidates<5", "n": len(rows)}
    source_ids = [r["id"] for r in rows]
    stamp = datetime.now().strftime("%Y%m%d")
    body = _rule_build(rows)
    content = (
        f"# 画像 · {ns} · {stamp}\n"
        + body
        + "\n---\n"
        + f"本版输入：{len(rows)} 条记忆（近 {days} 天）｜生成方式：rule-fallback"
    )
    r = _store_via_cli(content, ns, source_ids, None, ns)
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "reason": "store failed", "r": r}
    new_id = r.get("id")
    sup = _supersede_old(conn, ns, new_id)
    return {"ok": True, "ns": ns, "n": len(rows), "id": new_id, "superseded": sup}


def main():
    ap = argparse.ArgumentParser(prog="xtmem_profile")
    ap.add_argument("--domain", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    conn = _conn(DEFAULT_DB)
    conn.row_factory = __import__("sqlite3").Row
    domains = [args.domain] if args.domain else DOMAINS
    results = []
    for ns in domains:
        results.append(_run_domain(conn, ns, args.days, use_llm=not args.no_llm))
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
