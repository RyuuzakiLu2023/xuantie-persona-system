# -*- coding: utf-8 -*-
"""xtmem_predict.py — XTMEM 预判层生成器（P4.2 + 增强方案，2026-09-06，assistant-c）。

步骤5（增强 2.1）：按 task_type 聚类产多条预判块（每类一条）。
  - 原料 = 该域近 7 天 refined 要点（track IN identity/preference，非流水）+ active 画像
  - 分类 = 复用 _has_task_intent 同款 6 类词表（不新建分类器）
  - <2 条/类退化单条（R2）；confidence=min(0.9, 0.5+0.1×len) 封顶；valid 7 天
  - tags=["prediction", "task_type:X", "overturn:0"]；derived_from=prediction-engine
  - source_ids=本类 point_ids 合并（血缘）
  - 簇级归档：新批次跑完 → 旧 active prediction 全 archived（daily_refresh + batch_id，O7）
  - 熔断五件套 R5：单次≤6/单域≤10/写入前 active<50/异常写失败事件/cron 依赖 distill 成功
  - ⚠️ assistant-a蒸馏域红线：JUDGE_DOMAINS 不碰——predict 生成端不涉蒸馏逻辑，只是消费 refined 产物

用法：python xtmem_predict.py [--domain <ns>] [--fallback-subprocess]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

try:  # 包内与同目录直跑两态：库路径 / CLI 统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

DEFAULT_DB = common.resolve_db_path()
PYTHON = sys.executable
DOMAINS = ["dsh", "personal", "workbuddy", "zcode"]

# R5 熔断参数
MAX_PER_BATCH = 6        # 单次最多产 6 条（6 task_type）
MAX_PER_DOMAIN_BATCH = 10  # 单域单次最多写 10 条（防异常爆库）
MAX_ACTIVE_BEFORE_WRITE = 50  # 写入前检查：本域 active prediction >= 50 → 熔断
CONFIDENCE_CAP = 0.9

# 6 类词表（复用 recall_policy._TASK_INTENT_PATTERNS 同族口径，不新建分类器）
# recall_policy 的正则是"句首动词"型；生成端按内容关键词归类，两处口径同源不混用——
# 这里用内容侧词表（原料是 refined 要点正文，不是用户 query）。
TASK_TYPE_PATTERNS = [
    ("audit", re.compile(r"审计|对账|复核|核查|audit", re.I)),
    ("deploy", re.compile(r"部署|上线|发布|安装|重启|deploy|release|install", re.I)),
    ("code", re.compile(r"改码|代码|重构|修复|bug|实现|写码|refactor|fix", re.I)),
    ("doc", re.compile(r"文档|写档|说明|报告|回执|doc|document", re.I)),
    ("debug", re.compile(r"排查|调试|定位|根因|debug|排查", re.I)),
    ("research", re.compile(r"调研|研究|评估|对比|分析|research|analyze", re.I)),
]


def _classify(content: str) -> str:
    """内容 → task_type（首命中词表；无命中 = other）。"""
    for name, pat in TASK_TYPE_PATTERNS:
        if pat.search(content or ""):
            return name
    return "other"


def _rows(conn, ns):
    """原料：近 7 天 refined 要点（identity/preference，非流水）+ active 画像。"""
    week_ago = time.time() - 7 * 86400
    # P1-10（2026-09-06 整改总单-3/5）：画像带 lineage-stale 排除——源被 forget 的画像不再喂预判（09-13 收紧：引号+冒号精确真标记，裸词=技术关键词放行——与前单主通道同口径）
    portrait = conn.execute(
        "SELECT id, content FROM chunks WHERE namespace=? AND track='profile' AND state='active' "
        "AND tags NOT LIKE '%\"lineage-stale:%' "
        "ORDER BY timestamp DESC LIMIT 1", (ns,)).fetchone()
    refined = conn.execute(
        "SELECT id, content, importance FROM chunks WHERE namespace=? AND tier='refined' AND state='active' "
        "AND track IN ('identity','preference') AND content NOT LIKE 'User: %' "
        "AND content NOT LIKE '# %' "
        "AND timestamp > ? ORDER BY importance DESC LIMIT 50",
        (ns, week_ago)).fetchall()
    # B1（2026-09-07 SupportBundle Spec）：content LIKE '# %' = 整篇进度文档/审计报告被存成要点
    # （实锤：dsh 域 mem-8af87fa9「# XTMEM · 进度与交接状态」整篇入 refined → predict 抓去产
    # 项目日报 prediction——文档型条目从源头排除，项目事实不再产预判）。
    return portrait, refined


# 选题口径治本（附录 8.4-4，2026-09-06 整改总单-6c）：预判选"行为偏好/工作习惯"，
# 不选历史拍板/项目现状——历史类要点（拍板/立项/上线/交付/现状）是"近期要闻回顾"，
# 不是"对下步行为的推论"。偏好闸词表：命中即行为类（优先做 claim 素材）。
_BEHAVIOR_PREF_HINTS = (
    "先", "习惯", "偏好", "喜欢", "一般会", "每次", "惯例", "流程", "步骤", "先做", "先看",
    "按规矩", "默认", "优先", "常用", "工作方式", "套路", "顺序",
)
_HISTORY_NOISE_HINTS = (
    "拍板", "决定", "立项", "上线", "交付", "已完成", "已落地", "现状", "当前", "定案", "合并",
    # B1（2026-09-07 SupportBundle Spec）：进度文档型词——项目状态快照不是行为推论
    "进度", "交接", "存档", "压缩", "待办", "挂账", "复盘",
)


def _is_project_fact(text: str) -> bool:
    """B1 项目事实闸（2026-09-07 SupportBundle Spec）：项目状态/进度/事故复盘类事实不产 prediction。

    判据（三占二即真）：① 命中进度/拍板/交付/复盘等状态词；
    ② 含日期戳或文件路径（项目快照的典型形态）；
    ③ 以 markdown 标题开头（整篇文档入库）。
    行为偏好/工作习惯类不受影响（词表命中前置在 _cluster_points 打分）。
    """
    t = str(text or "")
    hits = 0
    if any(h in t for h in _HISTORY_NOISE_HINTS):
        hits += 1
    if ("2026-" in t) or ("E:" in t) or ("F:" in t) or ("C:" in t) or ("/" in t and "." in t.split("/")[-1]):
        hits += 1
    if t.lstrip().startswith("#"):
        hits += 1
    return hits >= 2


def _cluster_points(refined) -> dict:
    """按 task_type 聚类 refined 要点 → {task_type: [row, ...]}。

    选题口径（6c）：行为偏好/工作习惯类优先——每簇内行为类要点排前（_build_claim 取首条
    即行为推论），历史拍板/项目现状类要点排后（只在簇内无行为类时兜底，作"近期要点"附属）。
    """
    clusters = defaultdict(list)
    for r in refined:
        ct = str(r["content"])
        _score = 0
        if any(h in ct for h in _BEHAVIOR_PREF_HINTS):
            _score += 2
        if any(h in ct for h in _HISTORY_NOISE_HINTS):
            _score -= 1
        clusters[_classify(ct)].append((_score, r))
    out = {}
    for k, v in clusters.items():
        v.sort(key=lambda x: x[0], reverse=True)
        out[k] = [r for _s, r in v]
    return out


def _build_claim(portrait, cluster_rows):
    """类 claim：画像首行（若有）+ 类内首条要点（截断）。

    附录 8.4.1 双修（2026-09-06 整改总单-5，assistant-a+workbuddy 两域同款病根）：
    ① 占位标题过滤——【反思/【会话检索/【规矩/【薰/【总线 等自动块首行不是画像内容
       （实锤：mem-12e853e4 identity 板块第 2 行是【反思 · 2026-09-04 18:00】，被抓去当核心画像）;
    ② ## preference 板块优先——行为偏好/工作习惯才是预判该抓的口径（选题治本：
       预判选"先对账再动手"这类行为推论，不选历史拍板/项目现状）。
    """
    _PLACEHOLDER_PREFIXES = ("【反思", "【会话检索", "【规矩", "【薰", "【总线", "【时间骨架", "【画像", "【预判", "【能力与公告",
                             # B1（2026-09-07）：markdown 标题行与进度/审计文档自动块前缀
                             "# ", "【压缩存档", "【SupportBundle", "【XTMEM", "【审计", "【派单", "【工单")
    def _is_placeholder(ln: str) -> bool:
        # ⚠️ 先剥 "- " 项目符号再判前缀——" - 【反思..." strip 后仍带 "- "，startswith("【反思") 会漏判
        s = ln.strip().lstrip("- ").strip()
        return any(s.startswith(p) for p in _PLACEHOLDER_PREFIXES)

    first = ""
    if portrait:
        lines = str(portrait[1]).split("\n")
        # ② preference 板块优先：定位 ## preference 标题行，只在其板块内找首个非占位 - 行
        pref_idx = next((i for i, ln in enumerate(lines) if ln.strip().lower().startswith("## preference")), None)
        # 扫描顺序：preference 板块行优先，其余行补扫（去重）
        scan_order = list(range(len(lines)))
        if pref_idx is not None:
            end = next((i for i in range(pref_idx + 1, len(lines)) if lines[i].strip().startswith("## ")), len(lines))
            scan_order = list(range(pref_idx + 1, end)) + [i for i in range(len(lines)) if not (pref_idx < i < end)]
        for i in scan_order:
            ln = lines[i]
            if not ln.startswith("- ") or _is_placeholder(ln):
                continue
            cand = ln[2:].split("[src:")[0].strip()
            if cand:
                first = cand
                break
        # ① 兜底扫全文：跳过占位标题行
        if not first:
            for ln in lines:
                if ln.startswith("- ") and not _is_placeholder(ln):
                    cand = ln[2:].split("[src:")[0].strip()
                    if cand:
                        first = cand
                        break
    pt = str(cluster_rows[0]["content"])[:100] if cluster_rows else ""
    if first and pt:
        return f"{first[:80]}；近期要点：{pt}"
    return first or pt or "（无足够画像/要点生成预判）"


def _store_via_cli(content, ns, source_ids, domain, confidence=0.6, tags="prediction,xtmem",
                   fallback_subprocess=False):
    """落库：默认走 serve（S4 已接），失败/强制回落子进程。"""
    if not fallback_subprocess:
        common.ensure_importable()
        from memory.serve_protocol import actor_token, send_request, RequestRejected
        try:
            token = actor_token(domain, os.environ.get('XTMEM_DB_PATH') or DEFAULT_DB)
            req = {"cmd": "store", "args": {
                    "agent": domain, "namespace": ns, "project": "",
                    "text": content, "importance": confidence,
                    "derived_from": "prediction-engine",
                    "source_ids": ",".join(source_ids),
                    "tier": "refined", "track": "prediction",
                    "visibility": "private", "tags": tags,
                }}
            out = send_request('127.0.0.1', int(os.environ.get('XTMEM_SERVE_PORT', '8765')), req, token, 30) if token else None
            if out and (out.get('ok') or out.get('fallback_allowed') is False):
                return out
        except RequestRejected as exc:
            return exc.response()
        except Exception:
            pass  # serve 不在 → 回落子进程
    env = dict(os.environ)
    # ⚠️ 防写穿（2026-09-06 错题本）：DB 路径取【调用时】env 优先（测试 tmp_db 隔离靠它），
    # import 期固化的 DEFAULT_DB 只作兜底——否则 pytest 里子进程回落 store 会写穿生产库。
    if not str(env.get("XTMEM_DB_PATH", "")).strip():
        env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = domain
    env["PYTHONUTF8"] = "1"
    args = common.cli_argv() + ["store", "--agent", domain, "--namespace", ns,
            "--text", content, "--importance", str(confidence),
            "--derived-from", "prediction-engine", "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "prediction", "--visibility", "private",
            "--tags", tags]
    proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def _record_event(conn, kind, ns, meta):
    """事件留痕（尽力而为）。"""
    try:
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, ns, ns, "", "", json.dumps(meta, ensure_ascii=False)))
        conn.commit()
    except Exception:
        pass


def _distill_ok_recently(conn, max_age_hours: float = 26.0) -> tuple:
    """R5 第 5 件：cron 依赖声明——最近一次 distill_backfill/distill 成功事件在 max_age_hours 内才跑。
    返回 (ok, reason)。"""
    try:
        cutoff = time.time() - max_age_hours * 3600
        row = conn.execute(
            "SELECT ts, kind FROM xtmem_events WHERE kind IN ('distill_backfill','distill') "
            "AND ts >= ? ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
        if row:
            return True, "recent-distill"
        # 2026-09-06（整改总单-6 观测口修复）：事件面无 distill 成功事件 ≠ 蒸馏没跑——
        # 蒸馏实际成功写 distill_units.state='distilled'（distilled_at 是权威成功面）。
        # 事件面（kind=distill/distill_backfill）只在部分路径写，缺失会熔断误伤
        # （实况：distill_units 最近蒸馏 19.3h 前、事件面 26h 窗空 → 误熔断）。
        # 🔴【已于 2026-09-16 单A24 修复】本句的 ORDER BY 曾被 9 条 TEXT 格式的
        #   distilled_at 永久劫持——SQLite 类型序里 TEXT > REAL，TEXT 行永远排在所有数字行之上，
        #   故 LIMIT 1 恒取到 '2026-09-05 23:30:11'（那 9 条误存成字符串的历史行），
        #   下方 isinstance(str) 分支把它解析成 09-05 的时间戳 ⇒ 永远早于 cutoff
        #   ⇒ 本函数恒返回 (False, 'no-recent-distill-success') ⇒ 预判被永久熔断。
        #   实测（09-16）：闸门取到 09-05，真实最新是当天；prediction_skipped 事件 13 次
        #   全部因此（reason 均为 no-recent-distill-success，最近 09-14）。
        # 修复内容（单A24 §二，方案 A + §一 洗数据，两件一处闭环）：
        #   ① 存量：9 条 TEXT 已归一成 REAL（单A24 §一，解析口径与本函数下方 strptime 一字不差）；
        #   ② 防复发：SQL 加 typeof 白名单，**即使将来又有脏数据混进来，这道闸也不会再被劫持**
        #      —— 不再依赖"数据格式碰巧是对的"。
        #   ③ 写入端类型契约：distill_units 时间列已加 trigger 放行 real/integer（单A23 补-4），
        #      与本条的 typeof 白名单同口径（'integer' 必须放行：xtmem_distill_commit.py 写 int(time.time())）。
        # ⚠️ 现象仍在的旁证：预判功能整体已废弃（track=prediction 的 active 记忆为 0、
        #   xtmem-predict cron 已 enabled=False、注入侧早已短路）——修闸≠开功能，
        #   cron 保持 enabled=False 不动（单A24 约束11）。
        try:
            drow = conn.execute(
                "SELECT distilled_at FROM distill_units WHERE state='distilled' AND distilled_at IS NOT NULL "
                # 单A24 §二 方案A：typeof 白名单——防"脏数据劫持 ORDER BY"复发。
                # SQLite 类型序 TEXT > REAL，哪怕只混进一条 TEXT，非数字行也会排到最前、
                # 让 LIMIT 1 取到最老那条。加白名单后这类行直接不参与排序候选。
                # ⚠️ 'integer' 必须放行：xtmem_distill_commit.py 写的是 int(time.time())。
                "AND typeof(distilled_at) IN ('real','integer') "
                "ORDER BY distilled_at DESC LIMIT 1").fetchone()
        except Exception:
            drow = None  # distill_units 表不存在（旧库/临时库）→ 无成功面可参考，落到事件面判断
        if drow:
            try:
                import datetime as _dt
                _dv = drow["distilled_at"]
                if isinstance(_dv, str):
                    _dv = _dt.datetime.strptime(_dv, "%Y-%m-%d %H:%M:%S").timestamp()
                else:
                    _dv = float(_dv)
                if _dv >= cutoff:
                    return True, "recent-distill-units"
            except Exception:
                pass
        # 兼容：无显式 distill 事件的历史库不熔断（首次运行放行，事件面缺失≠蒸馏失败）
        any_ev = conn.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind LIKE '%distill%'").fetchone()[0]
        if any_ev == 0:
            return True, "no-distill-events-yet"
        return False, "no-recent-distill-success"
    except Exception as e:
        return True, f"probe-error:{str(e)[:40]}"  # 探针失败放行（不因观测面故障停产）


def _run_domain(conn, ns, fallback_subprocess=False):
    """单域生成：聚类多条 + 簇级归档 + 熔断五件套。"""
    # R5-5：cron 依赖 distill 成功
    ok, why = _distill_ok_recently(conn)
    if not ok:
        _record_event(conn, "prediction_skipped", ns, {"reason": why})
        return {"ok": False, "ns": ns, "skipped": True, "reason": "cron-dependency: " + why}

    # R5-3：写入前检查本域 active prediction 数
    active_n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE namespace=? AND track='prediction' AND state='active'",
        (ns,)).fetchone()[0]
    if active_n >= MAX_ACTIVE_BEFORE_WRITE:
        _record_event(conn, "prediction_gen_failed", ns,
                      {"reason": "active>=50 fuse", "active": active_n})
        return {"ok": False, "ns": ns, "fused": True, "reason": "active>=50", "active": active_n}

    portrait, refined = _rows(conn, ns)
    if not refined and not portrait:
        return {"ok": True, "ns": ns, "generated": 0, "reason": "no-source"}

    clusters = _cluster_points(refined)
    batch_id = time.strftime("%Y%m%d_%H%M")
    generated = []
    stored_ids = []
    try:
        for task_type, rows in clusters.items():
            if len(generated) >= MAX_PER_BATCH:
                break  # R5-1：单次最多 6 条
            # B1 项目事实闸：整簇素材皆项目状态/进度/复盘 → 不产 prediction（跳过该簇）
            if rows and all(_is_project_fact(str(r["content"])) for r in rows):
                _record_event(conn, "prediction_skipped", ns,
                              {"reason": "project-fact-gate", "task_type": task_type, "n": len(rows)})
                continue
            n = len(rows)
            if n < 2:
                # R2：证据不足退化单条
                confidence = 0.5
            else:
                confidence = min(CONFIDENCE_CAP, 0.5 + 0.1 * n)
            claim = _build_claim(portrait, rows)
            # O1：content 不写 confidence 数值；O8 前缀含冲突证据声明
            content = ("【预判 · 可能过时，请直接纠正 · 本会话已有冲突证据则以证据为准】\n"
                       + claim + f" （类型 {task_type}）")
            source_ids = ([portrait[0]] if portrait else []) + [r["id"] for r in rows]
            tags = "prediction,task_type:%s,overturn:0" % task_type
            r = _store_via_cli(content, ns, source_ids, ns, confidence=confidence,
                               tags=tags, fallback_subprocess=fallback_subprocess)
            if r.get("ok") and r.get("id"):
                generated.append({"task_type": task_type, "id": r["id"],
                                  "confidence": confidence, "points": n})
                stored_ids.append(r["id"])
            else:
                _record_event(conn, "prediction_gen_failed", ns,
                              {"reason": "store failed", "task_type": task_type, "r": str(r)[:120]})
                return {"ok": False, "ns": ns, "reason": "store failed", "generated": generated}
        # R5-2：单域单批上限（异常保护）
        if len(stored_ids) > MAX_PER_DOMAIN_BATCH:
            _record_event(conn, "prediction_gen_failed", ns,
                          {"reason": "over max per domain", "n": len(stored_ids)})
            return {"ok": False, "ns": ns, "reason": "over-max", "generated": generated[:MAX_PER_DOMAIN_BATCH]}

        # ② 簇级归档：新批次跑完 → 旧 active prediction 全 archived（O7 字段补齐）
        # sqlite tags 是 JSON 数组文本——归档补 archive_reason/batch_id 用 JSON 合并
        old_rows = conn.execute(
            "SELECT id, tags FROM chunks WHERE namespace=? AND track='prediction' AND state='active'"
            + (" AND id NOT IN (%s)" % ",".join("?" * len(stored_ids)) if stored_ids else ""),
            (tuple([ns] + stored_ids) if stored_ids else (ns,))
        ).fetchall()
        arch_n = 0
        for orow in old_rows:
            try:
                otags = json.loads(orow[1]) if isinstance(orow[1], str) else (orow[1] or [])
            except Exception:
                otags = []
            otags = [t for t in otags if not str(t).startswith(("archive_reason:", "batch_id:"))]
            otags.append("archive_reason:daily_refresh")
            otags.append("batch_id:" + batch_id)
            conn.execute("UPDATE chunks SET state='archived', tags=? WHERE id=?",
                         (json.dumps(otags, ensure_ascii=False), orow[0]))
            arch_n += 1
        conn.commit()
        _record_event(conn, "prediction_batch_done", ns,
                      {"batch_id": batch_id, "generated": len(generated), "archived": arch_n})
        return {"ok": True, "ns": ns, "batch_id": batch_id,
                "generated": generated, "archived": arch_n}
    except Exception as e:
        # R5-4：异常熔断——写失败事件不抛出
        _record_event(conn, "prediction_gen_failed", ns, {"reason": "exception", "err": str(e)[:200]})
        return {"ok": False, "ns": ns, "reason": "exception", "err": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser(prog="xtmem_predict")
    ap.add_argument("--domain", default=None)
    ap.add_argument("--fallback-subprocess", action="store_true",
                    help="强制子进程落库（serve 保险参数）")
    args = ap.parse_args()
    import sqlite3
    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    domains = [args.domain] if args.domain else DOMAINS
    results = [_run_domain(conn, ns, fallback_subprocess=args.fallback_subprocess) for ns in domains]
    conn.close()
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
