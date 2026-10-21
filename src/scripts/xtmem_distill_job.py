# -*- coding: utf-8 -*-
"""xtmem_distill_job.py — 通用蒸馏调度壳（cron / 定时任务唯一入口）。

【这是什么】
把「规则层建窗 → LLM 层评判蒸馏」串成一次可重复执行的作业，靠**环境变量**参数化，
不再为每个域各写一个包装脚本。

【为什么要这个壳】
历史做法是「一个域一个包装文件」（如只蒸 A 域、只蒸 B 域各一份），原因只是
调度器不支持给脚本传参数。文件一多就有三个病：
  - 新增一个域要新建文件，容易漏、容易漂；
  - 六个包装里的 limit/域名各写一份，没人记得哪个是准的；
  - 域名单写死在文件名里，公开仓没法直接跑。
现在统一成「一个壳 + 环境变量」，调度器那边只填一条命令。

【执行链】
  ① xtmem_distill_units.py --domain D --gap G     → 规则层建窗（幂等）
  ② xtmem_distill_llm.py   --domain D --limit N   → LLM 层评判蒸馏（幂等，可重试）
  （--units-only / --llm-only 可只跑其中一段）

【环境变量】
  XTMEM_DISTILL_DOMAIN     只处理该 namespace；缺省=库内全部 namespace
  XTMEM_DISTILL_GAP        规则层切窗间隔分钟（默认 60）
  XTMEM_DISTILL_LIMIT      LLM 层单批窗口数（默认 20）
  XTMEM_DISTILL_UNITS_ONLY  "1" = 只建窗（不需要 LLM 配置）
  XTMEM_DISTILL_LLM_ONLY    "1" = 只蒸（前提是账本里已有 pending 窗口）
  XTMEM_DB_PATH 等         见 xtmem_common

用法：
  python -m xtmem_jobs.xtmem_distill_job
  python xtmem_distill_job.py --units-only
输出：JSON 一行（含两段各自的退出码与摘要；任一段失败整体退出码 1）

⚠️ 本壳不吞失败：规则层失败就停（没账本，LLM 层无从下手）；
   LLM 层失败照实返回，由调用方（cron/CI）判红。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

_HERE = os.path.dirname(os.path.abspath(__file__))


def _env_flag(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _run(script: str, argv: list[str], env: dict) -> dict:
    path = os.path.join(_HERE, script)
    if not os.path.exists(path):
        return {"ok": False, "script": script, "error": "script missing: " + path}
    proc = subprocess.run([sys.executable, path, *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    try:
        body = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        body = {"ok": False,
                "error": "bad output: " + (proc.stdout or proc.stderr or "")[:300]}
    body["_exit_code"] = proc.returncode
    if proc.returncode != 0 and proc.stderr.strip():
        body.setdefault("_stderr", proc.stderr.strip()[:500])
    return body


def main() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_distill_job")
    ap.add_argument("--domain", default=None,
                    help="只处理该 namespace；缺省=库内全部（或 $XTMEM_DISTILL_DOMAIN）")
    ap.add_argument("--gap", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--units-only", action="store_true")
    ap.add_argument("--llm-only", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    domain = args.domain or (os.environ.get("XTMEM_DISTILL_DOMAIN", "").strip() or None)
    gap = args.gap if args.gap is not None else int(os.environ.get("XTMEM_DISTILL_GAP", "60") or 60)
    limit = (args.limit if args.limit is not None
             else int(os.environ.get("XTMEM_DISTILL_LIMIT", "20") or 20))
    units_only = args.units_only or _env_flag("XTMEM_DISTILL_UNITS_ONLY")
    llm_only = args.llm_only or _env_flag("XTMEM_DISTILL_LLM_ONLY")

    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = common.resolve_db_path(args.db)
    env["PYTHONUTF8"] = "1"

    out = {"ok": True, "db": env["XTMEM_DB_PATH"], "domain": domain or "*",
           "gap": gap, "limit": limit}

    if not llm_only:
        argv = ["--gap", str(gap)]
        if domain:
            argv += ["--domain", domain]
        units = _run("xtmem_distill_units.py", argv, env)
        out["units"] = units
        if units.get("_exit_code") != 0 or not units.get("ok", False):
            out["ok"] = False
            print(json.dumps(out, ensure_ascii=False))
            return 1

    if not units_only:
        argv = ["--limit", str(limit)]
        if domain:
            argv += ["--domain", domain]
        llm = _run("xtmem_distill_llm.py", argv, env)
        out["llm"] = llm
        if llm.get("_exit_code") != 0 or not llm.get("ok", False):
            out["ok"] = False
            print(json.dumps(out, ensure_ascii=False))
            return 1

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
