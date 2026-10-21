# -*- coding: utf-8 -*-
"""xtmem_distill.py — XTMEM 蒸馏器（P4.8 T6，2026-09-01，assistant-c）。

把 raw 层（对话流水）按 (namespace, project) 聚合成 refined 蒸馏产物：
  - tier='refined' / derived_from='distill' / source_ids=[组内 raw chunk id...] / 血缘闸 P0.1
  - 每条后缀 [src:xxxxxxxx]（句级溯源）
  - 可见性走 CLI store 的 min_visibility 继承最严源（部分源 private → 产物 private，绝不越源）
  - 幂等：同 (ns, project) 已有 active 蒸馏产物则跳过（不重蒸；改判据/回填用独立标记 distill-backfill）

回填分批（AF-8）后续做：每晚 50 组 importance DESC、首批 20 组人工验精度、独立标记 distill-backfill 可整批 DELETE。
用法：python xtmem_distill.py [--domain <ns>] [--project <p>] [--site <raw|all>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
# ⚠️ Path 必须显式导入（2026-10-16 公开仓修复）：_store_via_serve() 里用了
#    `Path(DEFAULT_CLI).resolve().parents[1]`，但本文件此前没有导入 pathlib。
#    后果：`python xtmem_distill.py` 一跑就 NameError: name 'Path' is not defined，
#    整条蒸馏链路第一步就死。私有树里能跑是因为其它模块先导入了 Path——
#    那种"靠邻居的 import 活着"的写法一旦单独执行/被别处调用就炸。
from pathlib import Path

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"~/.xtmem/vector_store.db")

# ⚠️ CLI 路径自动发现（2026-10-16 公开仓修复）：脱敏时这行被替换成了字面量
#    `<XT_ROOT>/xtmem/xtmem.py` 占位符，但公开仓没有那个目录 ⇒ 蒸馏产物子进程回写
#    永远 "store failed"。这里按「环境变量 → 仓库内相对位置」依次探测，都是相对
#    本文件定位（不写死盘符），放进 pip 安装后的任意位置也能用。
def _discover_cli() -> str:
    env = os.environ.get("XTMEM_CLI")
    if env and Path(env).exists():
        return env
    _here = Path(__file__).resolve()
    for _cand in (
        _here.parents[1] / "cli" / "xtmem.py",          # <repo>/src/scripts → <repo>/src/cli
        _here.parents[2] / "src" / "cli" / "xtmem.py",  # 从 <repo>/src/scripts 再上一层
    ):
        if _cand.exists():
            return str(_cand)
    return "xtmem"  # 最后退回 PATH 里的 console script

DEFAULT_CLI = _discover_cli()
PYTHON = sys.executable

# 走评判式蒸馏新链路的域（xtmem_distill_units.py 建窗 + xtmem_distill_llm.py 评判提炼）。
# 本粗蒸馏器按 (namespace, project) 整域一锅，承载不住"判assistant-a答得对不对/分发言人"的口径，
# 2026-09-05 把personal 域摘出。这些域在无 --domain 的全库遍历里跳过。
JUDGE_DOMAINS = {"personal"}

# R5（P6 波1）：写连接收编 open_db（schema 版本闸+降级自兜底）。
# ⚠️ 插件路径自动发现（2026-10-16 公开仓修复）：原行是脱敏占位符
#    `sys.path.insert(0, r"<XT_ROOT>/plugins/xtmem")`，公开仓里不存在 ⇒ 下面这行
#    `from memory.open_db import open_db` 直接 ImportError，整个脚本起不来。
#    pip install -e . 之后 memory 已是可导入的包，故只在"仓库内直跑"时才需要补路径。
try:
    from memory.open_db import open_db  # pip 安装 / 已在 path 上
except ImportError:
    for _p in (
        Path(__file__).resolve().parents[1] / "hermes-plugin",   # <repo>/src/hermes-plugin
        Path(__file__).resolve().parents[1],                      # <repo>/src
    ):
        if (_p / "memory").is_dir() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    from memory.open_db import open_db
_VS = None
def _vecstore():
    """D1-1（2026-09-09）收编：supersede 的唯一写路径 = VectorStore.mark_superseded。

    为什么不许再写把 state 直接置 'superseded' 的裸 UPDATE 语句：那条路不写 retire_reason/retired_by/
    retired_at 三列、不删 chunks_fts 行（=superseded 幽灵命中，R2.3 的老坑）、也不清
    embedding_cache（向量腿幽灵源头），成因字串还会污染 superseded_by 指针列（D1-1 存量 1226 条
    就是这么来的）。收编后这些联动只有一份实现。
    脚本自带 conn 与本连接并存是安全的：mark_superseded 内部 commit，本进程后续读走新快照。
    """
    global _VS
    if _VS is None:
        from memory.vector_store import VectorStore
        _VS = VectorStore(DEFAULT_DB)
    return _VS


def _rows(conn, ns=None, project=None):
    sql = ("SELECT id, namespace, project, content, importance, track, timestamp FROM chunks "
           "WHERE state='active' AND tier='raw'")
    params = []
    if ns:
        sql += " AND namespace=?"
        params.append(ns)
    if project:
        sql += " AND project=?"
        params.append(project)
    sql += " ORDER BY importance DESC, timestamp DESC"
    return conn.execute(sql, params).fetchall()


_SERVE_PORT = int(os.environ.get("XTMEM_SERVE_PORT", "8765") or "8765")
_SERVE_TIMEOUT = 30.0


def _store_via_serve(content, ns, project, source_ids, domain, derived_from="distill") -> Optional[dict]:
    """S4（B方案常驻书架，2026-09-06，assistant-c）：经 serve 常驻服务回写蒸馏产物。

    省掉每窗一次的子进程全量水化（178 窗 × 2~3s 纯浪费）。协议一行 JSON：
      {"cmd":"store","args":{agent,namespace,project,text,importance,
       derived-from,source-ids,tier,track,tags}}
    失败/超时返回 None（调用方回落子进程）。"""
    sys.path.insert(0, str(Path(DEFAULT_CLI).resolve().parents[1] / 'plugins/xtmem'))
    from memory.serve_protocol import actor_token, send_request, RequestRejected
    try:
        token = actor_token(domain, os.environ.get('XTMEM_DB_PATH') or DEFAULT_DB)
        if not token:
            return None  # Unprovisioned host uses its local CLI; no anonymous TCP attempt.
        req = {
                "cmd": "store",
                "args": {
                    "agent": domain,
                    "namespace": ns,
                    "project": project or "",
                    "text": content,
                    "importance": 0.7,
                    "derived_from": derived_from,
                    "source_ids": ",".join(source_ids),
                    "tier": "refined",
                    "track": "default",
                    "tags": "distill,xtmem",
                },
            }
        return send_request('127.0.0.1', _SERVE_PORT, req, token, _SERVE_TIMEOUT)
    except RequestRejected as exc:
        return exc.response()
    except Exception:
        return None  # serve 不在 → 回落子进程


def _store_via_cli(content, ns, project, source_ids, domain, derived_from="distill",
                   fallback_subprocess: bool = False):
    """蒸馏产物回写：默认先试 serve（S4），失败回落子进程（保险参数 --fallback-subprocess 强制子进程）。"""
    if not fallback_subprocess:
        r = _store_via_serve(content, ns, project, source_ids, domain, derived_from=derived_from)
        if r and r.get("ok"):
            return r
        if r and r.get('fallback_allowed') is False:
            return r
        # serve 失败：记一次告警后走子进程兜底（功能不丢）
        import logging
        logging.getLogger("xtmem_distill").warning(
            "serve store failed (%s) — fallback subprocess", (r or {}).get("error", "no serve")
        )
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = domain
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", domain, "--namespace", ns,
            "--project", project, "--text", content, "--importance", "0.7",
            "--derived-from", derived_from, "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "default", "--tags", "distill,xtmem"]
    proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def _distill_group(rows):
    """规则聚合：按 track 分组，抽高 importance 预览，带 [src] 句级溯源。"""
    by_track = defaultdict(list)
    for r in rows:
        by_track[r["track"]].append(r)
    lines = []
    for track in ("preference", "identity", "procedural", "default", "emotion"):
        items = by_track.get(track, [])
        if not items:
            continue
        lines.append(f"## {track}")
        for r in sorted(items, key=lambda x: -x["importance"])[:30]:
            lines.append(f"- {str(r['content'])[:160]} [src:{r['id'][:8]}]")
    if not lines:
        return None
    return "\n".join(lines)


def _group_watermark(conn, ns, project):
    """该组现有 active distill 产物已蒸到的 raw 水位：产物 source_ids 所指 raw 的 MAX(timestamp)。
    无产物/无存活源返回 0.0。幂等 bug 修复（2026-09-05，assistant-c）：旧逻辑"同组已有产物即永久跳过"，
    导致全库蒸馏锁死在首次蒸馏点（assistant-a 09-02 后零产出实证）——水位制让新增 raw 能触发重蒸。"""
    rows = conn.execute(
        "SELECT source_ids FROM chunks WHERE namespace=? AND project=? AND derived_from='distill' AND state='active'",
        (ns, project or ""),
    ).fetchall()
    all_src = []
    for r in rows:
        try:
            ids = json.loads(r["source_ids"] or "[]")
            if isinstance(ids, list):
                all_src.extend(str(x) for x in ids)
        except Exception:
            pass
    if not all_src:
        return 0.0
    ph = ",".join("?" * len(all_src))
    try:
        row = conn.execute(f"SELECT MAX(timestamp) m FROM chunks WHERE id IN ({ph})", all_src).fetchone()
        return float(row["m"] or 0.0)
    except Exception:
        return 0.0


def _distill_group_capped(body: str, cap: int = 4000) -> str:
    """蒸馏产物字符帽（2026-09-05）：旧产物失控到 17-20KB（assistant-a实证）——超注入预算，
    两段路由命中也不被引用。按行截断保 [src:] 不截半（对齐 O3-a 画像帽口径）。"""
    if not body or len(body) <= cap:
        return body
    lines = body.split("\n")
    out = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > cap:
            break
        out.append(ln)
        used += len(ln) + 1
    out.append("(cap 4000 截断——组内更高 importance 的 raw 见下一蒸馏产物)")
    return "\n".join(out)


def _run_group(conn, ns, project, fallback_subprocess=False):
    rows = _rows(conn, ns, project)
    if len(rows) < 3:
        return {"ok": False, "ns": ns, "project": project or "", "reason": "raw<3", "n": len(rows)}
    # 幂等修复（2026-09-05）：水位制——只蒸水位后新增 raw（旧产物 supersede），
    # 【不】再"已有产物即永久跳过"（旧逻辑 = 全库蒸馏锁死 bug，assistant-a 09-02 后零产出实证）
    wm = _group_watermark(conn, ns, project)
    new_rows = [r for r in rows if float(r["timestamp"]) > wm] if wm > 0 else rows
    if not new_rows:
        return {"ok": True, "ns": ns, "project": project or "", "skipped": "up-to-date", "n": len(rows), "new_since_wm": 0}
    if len(new_rows) < 3:
        return {"ok": True, "ns": ns, "project": project or "", "skipped": "new-raw<3 (pending)", "n": len(new_rows)}
    source_ids = [r["id"] for r in new_rows]
    body = _distill_group(new_rows)
    if body is None:
        return {"ok": False, "ns": ns, "project": project or "", "reason": "empty-distill"}
    body = _distill_group_capped(body)
    content = f"# 蒸馏 · {ns} · {project or '—'}\n" + body
    r = _store_via_cli(content, ns, project, source_ids, ns,
                       fallback_subprocess=fallback_subprocess)
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "project": project or "", "reason": "store failed", "r": r}
    # 旧 active distill 置 superseded + 回指新版（保留历史，检索只见新产物）
    # D1-1 收编：先查目标 id 再逐条走 mark_superseded；reason/by 落新三列，指针列只留真指针。
    new_id = r.get("id")
    try:
        _old = conn.execute(
            "SELECT id FROM chunks WHERE namespace=? AND project=? AND derived_from='distill' "
            "AND state='active' AND id!=?",
            (ns, project or "", new_id),
        ).fetchall()
        _vs = _vecstore()
        for _rr in _old:
            _vs.mark_superseded(_rr["id"], successor_id=new_id,
                               reason="distill-reaggregate", by="distill")
    except Exception:
        pass  # supersede 失败不否定新产物（下一轮水位含多产物仍增量）
    return {"ok": True, "ns": ns, "project": project or "", "id": new_id, "n": len(new_rows), "wm": round(wm, 0)}

def main():
    ap = argparse.ArgumentParser(prog="xtmem_distill")
    ap.add_argument("--domain", default=None)
    ap.add_argument("--project", default=None)
    ap.add_argument("--fallback-subprocess", action="store_true",
                    help="S4：强制走子进程回写（serve 保险参数；蒸馏窗口重跑保险）")
    args = ap.parse_args()
    import sqlite3
    conn = open_db(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    # 分组：按 (namespace, project) 遍历 raw 活跃 chunk
    groups = conn.execute(
        "SELECT DISTINCT namespace, project FROM chunks WHERE state='active' AND tier='raw'"
    ).fetchall()
    results = []
    for g in groups:
        ns = g["namespace"]
        project = g["project"]
        if args.domain and ns != args.domain:
            continue
        if args.project and project != args.project:
            continue
        # personal 域走评判式蒸馏新链路，本粗蒸馏器不再碰（--domain 显式指定时不拦，留调试口）
        if ns in JUDGE_DOMAINS and not args.domain:
            results.append({"ok": True, "ns": ns, "project": project or "",
                            "skipped": "judge-pipeline (xtmem_distill_units/llm)"})
            continue
        results.append(_run_group(conn, ns, project, fallback_subprocess=args.fallback_subprocess))
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
