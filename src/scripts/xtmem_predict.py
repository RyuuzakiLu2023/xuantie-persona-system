# -*- coding: utf-8 -*-
"""xtmem_predict.py — XTMEM 预判层生成器（P4.2，2026-09-01，小D）。

为每个活跃角色域产出一条预判块（track='prediction'）：
  - 原料 = 该域 active 画像（track='profile'）+ 最近 refined 要点
  - claim = 从画像 identity/preference 首行提炼（rule）
  - derived_from='prediction-engine' / source_ids=[画像+要点 id] / visibility='private' / 本域
  - tags=['prediction','overturn:N']；confidence 用 importance 复用
  - 内容前缀含「可能过时，请纠正」（P4.4 护栏，验收 4）
  - 旧预判块置 state='archived'（P2.1：检索排除、保留引用，不删）

用法：python xtmem_predict.py [--domain <ns>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
DEFAULT_CLI = os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py")
PYTHON = sys.executable
DOMAINS = ["dsh", "xuantie_lark", "workbuddy", "zcode"]


def _rows(conn, ns):
    # 原料 = active 画像 + 最近 refined（非流水）
    portrait = conn.execute(
        "SELECT id, content FROM chunks WHERE namespace=? AND track='profile' AND state='active' "
        "ORDER BY timestamp DESC LIMIT 1", (ns,)).fetchone()
    refined = conn.execute(
        "SELECT id, content, importance FROM chunks WHERE namespace=? AND tier='refined' AND state='active' "
        "AND track IN ('identity','preference') AND content NOT LIKE 'User: %' ORDER BY importance DESC LIMIT 5",
        (ns,)).fetchall()
    return portrait, refined


def _build_claim(portrait, refined):
    """rule 提炼：从画像 identity/preference 首行取，或 top refined。"""
    text = ""
    if portrait:
        for ln in str(portrait[1]).split("\n"):
            if ln.startswith("- "):
                text = ln[2:].split("[src:")[0].strip()
                if text:
                    break
    if not text and refined:
        text = str(refined[0][1])[:80]
    return text or "（无足够画像/要点生成预判）"


def _store_via_cli(content, ns, source_ids, domain, confidence=0.6):
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = domain
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", domain, "--namespace", ns,
            "--text", content, "--importance", str(confidence),
            "--derived-from", "prediction-engine", "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "prediction", "--visibility", "private",
            "--tags", "prediction,xtmem"]
    proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def _archive_old(conn, ns, new_id):
    cur = conn.execute(
        "UPDATE chunks SET state='archived' WHERE namespace=? AND track='prediction' AND state='active' AND id!=?",
        (ns, new_id))
    conn.commit()
    return cur.rowcount


def _run_domain(conn, ns):
    portrait, refined = _rows(conn, ns)
    source_ids = ([portrait[0]] if portrait else []) + [r[0] for r in refined]
    if not source_ids:
        return {"ok": False, "ns": ns, "reason": "no-source"}
    claim = _build_claim(portrait, refined)
    content = "【预判 · 基于历史画像生成，可能过时，请纠正】\n" + claim
    r = _store_via_cli(content, ns, source_ids, ns)
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "reason": "store failed", "r": r}
    arch = _archive_old(conn, ns, r.get("id"))
    return {"ok": True, "ns": ns, "id": r.get("id"), "source_ids": len(source_ids), "archived": arch}


def main():
    ap = argparse.ArgumentParser(prog="xtmem_predict")
    ap.add_argument("--domain", default=None)
    args = ap.parse_args()
    import sqlite3
    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    domains = [args.domain] if args.domain else DOMAINS
    results = [_run_domain(conn, ns) for ns in domains]
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
