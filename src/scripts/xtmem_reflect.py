# -*- coding: utf-8 -*-
"""xtmem_reflect.py — XTMEM E4 轻量反思层·事件驱动版 v1.0（2026-08-27 assistant-b）

对齐 xtmem_autodream.py 风格（零 LLM / 标准库 / XTMEM_DB_PATH 可覆盖测试副本）。

反思写入走 xtmem.py CLI 的 cmd_reflect（builder.store 全链路：向量化 + FTS + content_hash
去重 + visibility=private 仅本角色可见），不裸插 SQL。

触发的事件源（水位控制在 xtmem_state.side='reflect'）：
  1) messages 新留言    -> 收件角色生成"收到留言"反思（带关系上下文 sentiment/trust）
  2) relations 演化     -> 关系发生变化的双方各生成一条态度更新反思
     （history 数组新增条目 ts > 水位 即视为演化事件）

用法：
  python xtmem_reflect.py             # 正式执行
  python xtmem_reflect.py --dry-run   # 只演算打印，不写不推进水位

挂载建议（mimox-data/cron/jobs.json）：
  {"id":"xtmem-reflect","script":"xtmem_reflect.py","no_agent":true,
   "schedule":{"kind":"cron","expr":"0 */6 * * *"},"enabled":true}
"""
from __future__ import annotations

import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timedelta, timezone

try:  # 包内与同目录直跑两态：库路径 / CLI 统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

# ⚠️ 反思正文标题时间按显式时区转——naive fromtimestamp 跟随机器时区，
# 机器时区不同时正文标题与同条 chunk 的 epoch timestamp 会自相矛盾（2026-09-05 统一）。
# 默认 Asia/Shanghai（与 timeline/profile 同口径），$XTMEM_TZ 可覆盖。
_BJ = common.tz()

# 复用 CLI 的 cmd_reflect（它自己处理引擎路径/编码）；CLI 落点由 xtmem_common 发现，
# 【不】再写死某个宿主目录。
DEFAULT_DB = common.resolve_db_path()
# 反思对象：$XTMEM_AGENTS（逗号分隔）可覆盖；默认只放通用示例域，不含私人角色名。
AGENTS = common.env_list("XTMEM_AGENTS") or ["default"]
REFLECT_SIDE = "reflect"


def _load_wm(conn) -> float:
    row = conn.execute("SELECT last_sync FROM xtmem_state WHERE side=?", (REFLECT_SIDE,)).fetchone()
    return float(row[0]) if row and row[0] else 0.0


def _save_wm(conn, wm: float):
    conn.execute(
        "INSERT INTO xtmem_state(side,last_sync) VALUES(?,?) "
        "ON CONFLICT(side) DO UPDATE SET last_sync=excluded.last_sync",
        (REFLECT_SIDE, wm),
    )


def _rel(conn, a: str, b: str):
    """无向取关系行（agent_a<=agent_b 不保证，双向查）。"""
    return (
        conn.execute("SELECT * FROM relations WHERE (agent_a=? AND agent_b=?) OR (agent_a=? AND agent_b=?)",
                     (a, b, b, a)).fetchone()
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    x = common.import_cli()

    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    now = time.time()
    wm = _load_wm(conn)
    produced = []
    latest_ts = wm

    for agent in AGENTS:
        blocks = []

        # --- 1) 留言事件 ---
        msgs = conn.execute(
            "SELECT from_agent, content, ts FROM messages WHERE to_agent=? AND ts>? ORDER BY ts",
            (agent, wm),
        ).fetchall()
        if msgs:
            names = {}
            for m in msgs:
                fr = m["from_agent"]
                rel = _rel(conn, agent, fr)
                s = f"{float(rel['sentiment']):.2f}" if rel else "n/a"
                t = f"{float(rel['trust']):.2f}" if rel else "n/a"
                names[fr] = names.get(fr, 0) + 1
                preview = str(m["content"])[:80].replace("\n", " ")
                blocks.append(f"{fr} 说：『{preview}…』（当前关系好感 {s} / 信任 {t}）")
            lst = "、".join(names)
            blocks.insert(0, f"共收到 {len(msgs)} 条来自 {lst} 的留言")
            latest_ts = max(latest_ts, max(m["ts"] for m in msgs))

        # --- 2) 关系演化事件 ---
        rels = conn.execute("SELECT * FROM relations WHERE agent_a=? OR agent_b=?", (agent, agent)).fetchall()
        for rel in rels:
            if agent not in (rel["agent_a"], rel["agent_b"]):
                continue
            other = rel["agent_b"] if rel["agent_a"] == agent else rel["agent_a"]
            try:
                hist = json.loads(rel["history"] or "[]")
            except Exception:
                continue
            fresh = [h for h in hist if float(h.get("ts") or 0) > wm]
            if fresh:
                notes = "; ".join(str(h.get("note") or "")[:60] for h in fresh[-2:])
                s = float(rel["sentiment"])
                t = float(rel["trust"])
                tone = "更亲近了些" if ds_ok(s, t) else "保持观察"
                blocks.append(
                    f"和 {other} 的关系有新变化：{notes}。当前好感 {s:.2f}/信任 {t:.2f}——{tone}"
                )
                latest_ts = max(latest_ts, max(float(h.get("ts") or 0) for h in fresh))

        if not blocks:
            continue
        text = (
            f"【反思 · {datetime.fromtimestamp(now, tz=_BJ):%Y-%m-%d %H:%M}】\n"
            + "\n".join("- " + b for b in blocks)
            + "\n这条反思是私有的，别人看不见；下次和他们打交道时记得带着这层态度。"
        )
        produced.append({"agent": agent, "chars": len(text), "events": len(blocks)})
        if not args.dry_run:
            ns = argparse.Namespace(agent=agent, text=text)
            r = x.cmd_reflect(ns)
            if not r.get("ok"):
                print(json.dumps({"ok": False, "agent": agent, "resp": r}, ensure_ascii=False))
                return 1

    if not args.dry_run:
        _save_wm(conn, latest_ts if latest_ts > wm else now)
        conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "dry_run": args.dry_run,
                      "watermark": {"old": wm, "new": latest_ts},
                      "reflections": produced}, ensure_ascii=False))
    return 0


def ds_ok(s: float, t: float) -> bool:
    """粗判关系走向：好感信任双高算'更亲近'。阈值让模板口吻有点分寸感。"""
    return s >= 0.5 and t >= 0.5


if __name__ == "__main__":
    sys.exit(main())
