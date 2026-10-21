# -*- coding: utf-8 -*-
"""xtmem_distill_commit.py — 会话直跑蒸馏的落库助手。

【这是什么】
当蒸馏产物是「人在会话里直跑模型、把结果存成 md 文件」而不是 cron 调的 LLM 层时，
用本脚本把产物 md 一次落库并把 distill_units 账本推到 distilled：
  读产物文件 → CLI store（derived_from='distill-judge'）→ 更新 distill_units
  （state='distilled' + refined_ids）。

【为什么要单独一个脚本】
直接手敲多步（store → 记 id → UPDATE 账本）半途断了就丢产物，而且账本和库容易对不上。
这里一次完成，失败只返回 ok=False，不改账本。

【公开化改动】
  - 库路径 / CLI 统一走 xtmem_common；不再写死生产路径与生产库。
  - namespace 从账本行取（unit.namespace），不写死私人域。

用法：
  python xtmem_distill_commit.py --unit <unit_id> --file <产物.md> [--dry-run] [--db <path>]

⚠️ 幂等：已 distilled 的窗口会直接拒绝（防止同一窗重复记两条）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def main() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_distill_commit")
    ap.add_argument("--unit", required=True, help="distill_units.unit_id")
    ap.add_argument("--file", required=True, help="产物 md 文件路径")
    ap.add_argument("--db", default=None)
    ap.add_argument("--importance", type=float, default=0.7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = common.resolve_db_path(args.db)

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot read product file: {exc}"},
                         ensure_ascii=False))
        return 1
    if not content.strip():
        print(json.dumps({"ok": False, "error": "empty product file"}, ensure_ascii=False))
        return 1

    conn = common.open_db(db_path)
    try:
        conn.row_factory = sqlite3.Row
        unit = conn.execute("SELECT * FROM distill_units WHERE unit_id=?",
                            (args.unit,)).fetchone()
        if unit is None:
            print(json.dumps({"ok": False, "error": "unit not found"}, ensure_ascii=False))
            return 1
        if unit["state"] == "distilled":
            print(json.dumps({"ok": False, "error": "already distilled",
                              "refined_ids": unit["refined_ids"]}, ensure_ascii=False))
            return 1

        raw_ids = json.loads(unit["raw_ids"] or "[]")
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "unit": args.unit,
                              "chars": len(content), "source_ids": len(raw_ids),
                              "content_hash": _short_hash(content)},
                             ensure_ascii=False))
            return 0

        ns = unit["namespace"]
        r = common.store_via_cli(
            db_path=db_path, agent=ns, namespace=ns,
            text=content, importance=args.importance,
            derived_from="distill-judge", source_ids=raw_ids,
            tier="refined", track="default",
            tags=["distill-judge", "xtmem"],
        )
        if not r.get("ok"):
            print(json.dumps({"ok": False, "stage": "store", "r": r}, ensure_ascii=False))
            return 1
        new_id = r.get("id") or (r.get("chunk") or {}).get("id")
        conn.execute(
            "UPDATE distill_units SET state='distilled', refined_ids=?, distilled_at=?,"
            " last_error=NULL, claim_token=NULL WHERE unit_id=?",
            (json.dumps([new_id], ensure_ascii=False), time.time(), args.unit),
        )
        conn.commit()
        print(json.dumps({"ok": True, "unit": args.unit, "id": new_id,
                          "chars": len(content), "source_ids": len(raw_ids),
                          "content_hash": _short_hash(content)}, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
