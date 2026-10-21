# -*- coding: utf-8 -*-
"""xtmem_r1_pipeline.py — R1 注入质量纵向监测管线（P7-B 纯减法版，2026-09-11）。

【P7-B 重写，取代 P6.11 版本】
  本脚本经历了判据层重写（顶模定量核查 + 维护者拍板 + 顶模终裁），要点：

  ▍删掉三件错的东西（纯减法，不留"观察性"标注的中间态）
    1. harmful_counts() 的 harmful=sum(v=="兜底（raw 回落）") ——
       raw 回落是【检索降级】不是【有害注入】，语义错位。留着它 = 让两组并排数字
       早晚被人拿去算胜率（与"守卫给病灶背书"同病）。
    2. arm_for() / arm 字段 / arm_layer 分层输出 / STOP_RULE_N 胜负判定 ——
       arm 按枚举序号事后贴标，与检索行为零关联。结构本身在暗示不存在的对照，删干净。
    3. MAINTAINERS 死代码 —— {"owner","assistant_c","maintainer_z"} 从未命中 dsh/zcode 域
       （域过滤在前先挡）；且按字面执行会让样本域直接为空（dsh/zcode 就是维护者 agent）。
       本单只删死代码防误活；F-3 整体重写归 P7.8①。

  ▍换上 corrected 归因判据（主判据）
    信号源：已落库的 corrected / confirmed 事件（带 target_chunk，via="user-verbatim"
            = 用户用自己的话判定记忆对错）。
    归因口径：一条 chunk 被 recall 注入（该 id 出现在 recall 事件的 meta.ids 里）
              → 之后【同 agent】产生 corrected(target=该 chunk) → 记一次"注入后被纠正"。
    归因上限 24h（时间窗口裁决，顶模拍；不设轮次窗口——库里没有轮次序列）。
    率 = corrected 归因次数 / 有注入的 chunk-曝光数；confirmed 作对照信号同法统计。
    为什么强于词表：按【记忆条目归因】（target_chunk 精确指认），不是按轮次窗口
    猜"用户下一句在骂哪条"——天然绕开误归因，也不需要录用户对话入库。

  ▍消费者（写死，防孤儿指标）
    R1 已从 P5.0 波2 前置【摘除】（解耦：R1 管注入质量、P5.0 管检索排序，当初设成
    前置就是错配）。摘除后用途明确写死：**注入质量的纵向回归监测**——等 P7.8 数据线
    接通后开始积累（P7.8 立项本身说明 R1 不暂停维护）。不留"没人看又半维护"的中间态。

  ▍当前状态：样本域 {dsh,zcode} 内 corrected/confirmed/session_end 信号数 = 0
    这不是失败，是【合格输出】——如实说明"数据线没接（P7.8）"。
    唯一信号源在 personal_lark（correction/confirmed/session_end 全在该域），
    而 personal 是 R1 明令排除的域（assistant-a红线：只读统计可引用其数字，不并入样本）。

  ▍写库纪律（P7-B 修订，2026-09-11）
    【--dry-run = 真只读】。旧版 dry-run 也往 xtmem_r1_eval 写 dry_run=1 的行；
    P7-B 工单硬项是"生产库零写"，故 dry-run 路径改为：连 open_db(ro=True) 只读连接、
    且完全跳过 INSERT——只读统计 + 打印。
    ⚠️ 我第一次跑 dry-run 时【确实写了 4 行到生产库】（id 258~261），已备份库后精确删除
       还原到 257 行基线（详见回执"自查纠错"段）。此处改动就是为了让同类误写不可能再发生：
       只读不靠人的记性，靠连接模式与代码分支。
    正式批（不带 --dry-run）才写库，用于纵向留痕。

用法：
  python xtmem_r1_pipeline.py --dry-run --sessions 4        # 空转（不产生正式结论）
  python xtmem_r1_pipeline.py --sessions 50 --window-days 7  # 正式批
"""
import argparse
import json
import os
import sys
import time
import sqlite3

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "hermes-plugin"))
from memory.open_db import open_db

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
SAMPLE_DOMAINS = {"dsh", "zcode"}
MIN_USER_TURNS = 5
MIN_TASK_REQUESTS = 1
# P7-B：MAINTAINERS 死代码已删（见文件头 §删 3）。F-3 整体重写归 P7.8①。
ATTRIBUTION_WINDOW_SEC = 24 * 3600   # corrected 归因上限 24h（顶模拍）


def ensure_eval_table(conn):
    """建/复用评估流水表。

    ⚠️ 表结构沿用 P6.11（arm 列保留为列、但不写入有义值）——理由：该表已存在于
    生产库（257 行历史），本单明令"不改 schema"。删列会破坏既有行。
    脚本侧不再产生 arm 语义：写入固定 "-"（列非空约束），并在此注释说明它已作废，
    防后人看到列名又去算 arm 对照。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS xtmem_r1_eval ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts REAL NOT NULL,"
        " session_id TEXT NOT NULL,"
        " agent TEXT NOT NULL,"
        " arm TEXT NOT NULL,"
        " user_turns INTEGER,"
        " task_requests INTEGER,"
        " has_selftest INTEGER,"
        " has_recall_injection INTEGER,"
        " valid INTEGER,"
        " verdict TEXT,"
        " harmful_injections INTEGER,"
        " injections INTEGER,"
        " dry_run INTEGER DEFAULT 0,"
        " meta TEXT DEFAULT '{}')"
    )
    conn.commit()


def fetch_sessions(conn, window_days, domains):
    """按 agent@小时 聚合 recall 事件成"会话桶"（沿用 P6.11 口径，未改）。"""
    since = time.time() - window_days * 86400
    ph = ",".join("?" * len(domains))
    sql = "SELECT ts, agent, meta FROM xtmem_events WHERE kind='recall' AND ts>=? AND agent IN (" + ph + ")"
    rows = conn.execute(sql, (since, *domains)).fetchall()
    buckets = {}
    for ts, agent, meta in rows:
        try:
            v = (json.loads(meta) or {}).get("verdict") or "无标记"
        except Exception:
            v = "无标记"
        key = agent + "@" + time.strftime("%Y%m%d%H", time.localtime(ts))
        b = buckets.setdefault(key, {"session_id": key, "agent": agent, "verdicts": [], "ts0": ts, "ts1": ts})
        b["verdicts"].append(v)
        b["ts1"] = max(b["ts1"], ts)
    return list(buckets.values())


OBSERVER_LOOKUP_RATIO_THRESHOLD = 0.50

def f3_gate(sess):
    turns = len(sess.get("verdicts", []))
    agent = sess.get("agent", "")
    in_domain = agent in SAMPLE_DOMAINS
    lookups = sum(1 for v in sess.get("verdicts", []) if "lookup" in str(v))
    lookup_ratio = (lookups / turns) if turns > 0 else 0.0
    is_observer = (lookup_ratio >= OBSERVER_LOOKUP_RATIO_THRESHOLD) if turns >= MIN_USER_TURNS else False
    g = {
        "in_sample_domain": in_domain,
        "user_turns_ge_5": turns >= MIN_USER_TURNS,
        "task_requests_ge_1": turns >= MIN_TASK_REQUESTS,
        "no_selftest": not is_observer,
        "has_recall_injection": turns >= 1,
    }
    return {
        "gates": g,
        "valid": all(g.values()),
        "user_turns": turns,
        "task_requests": turns,
        "has_selftest": int(is_observer),
        "lookup_ratio": round(lookup_ratio, 4),
        "has_recall_injection": int(g["has_recall_injection"]),
    }


def _injected_chunk_ids(conn, since, until, domains):
    """取窗口内 recall 事件实际注入过的 chunk id 集合（按 agent 分组）。

    数据来源：xtmem_events.meta.ids（CLI/provider 记录的实际命中条目列表）。
    返回 {agent: {chunk_id: [注入时间戳, ...]}}——保留每次注入时间，供 24h 归因窗口判定。
    """
    ph = ",".join("?" * len(domains))
    rows = conn.execute(
        "SELECT ts, agent, meta FROM xtmem_events WHERE kind='recall' AND ts>=? AND ts<=? AND agent IN (" + ph + ")",
        (since, until, *domains)
    ).fetchall()
    out = {}
    for ts, agent, meta in rows:
        if not meta:
            continue
        try:
            ids = (json.loads(meta) or {}).get("ids") or []
        except Exception:
            continue
        if not ids:
            continue
        b = out.setdefault(agent, {})
        for cid in ids:
            if cid:
                b.setdefault(str(cid), []).append(float(ts))
    return out


def corrected_attribution(conn, domains=SAMPLE_DOMAINS, window_days=7.0):
    """【主判据】corrected 归因率 + confirmed 对照信号（P7-B 新判据）。

    口径（工单 §2.1）：
      · 一条 chunk 被 recall 注入（该 id 出现在 recall 事件的 meta.ids）
        → 之后【同 agent】产生 corrected(target=该 chunk) → 记一次"注入后被纠正"；
      · 归因上限 24h：注入→纠正超过 24h 不归因（时间窗口裁决；不设轮次窗口，
        因为库里没有轮次序列）；
      · 率 = corrected 归因次数 / 有注入的 chunk-曝光数；
      · confirmed 作对照信号（记忆被确认有用）同法统计。

    只读：本函数不写任何表。
    """
    now = time.time()
    since = now - window_days * 86400
    ph = ",".join("?" * len(domains))

    injected = _injected_chunk_ids(conn, since, now, sorted(domains))

    # chunk-曝光数 = 所有 (agent, chunk_id) 注入对的总数（同一 chunk 被注入多次算多次曝光）
    exposures = 0
    for agent, cmap in injected.items():
        exposures += sum(len(v) for v in cmap.values())

    results = {}
    for kind in ("corrected", "confirmed"):
        rows = conn.execute(
            "SELECT ts, agent, target_chunk FROM xtmem_events WHERE kind=? AND ts>=? AND agent IN (" + ph + ")",
            (kind, since, *sorted(domains))
        ).fetchall()
        total_sig = len(rows)
        attributed = 0
        detail = []
        for ts, agent, tc in rows:
            if not tc:
                continue
            hits = (injected.get(agent) or {}).get(str(tc)) or []
            # 24h 归因窗口：存在一次注入 t 满足 0 <= ts - t <= 24h
            ok = any(0 <= (ts - t) <= ATTRIBUTION_WINDOW_SEC for t in hits)
            if ok:
                attributed += 1
            detail.append({"chunk": tc, "attributed": bool(ok)})
        results[kind] = {
            "signal_count": total_sig,
            "attributed": attributed,
            "rate": (round(attributed / exposures, 4) if exposures else None),
            "detail": detail[:20],
        }

    return {
        "attribution_window_hours": ATTRIBUTION_WINDOW_SEC / 3600.0,
        "window_days": window_days,
        "sample_domains": sorted(domains),
        "injected_chunk_exposures": exposures,
        "corrected": results["corrected"],
        "confirmed": results["confirmed"],
    }


def domain_signal_scan(conn):
    """如实输出各信号在【样本域】与【域外】的条数——含 session_end。

    工单 §2.2：如实输出"样本域 {dsh,zcode} 内 corrected/confirmed/session_end
    信号数 = 0"——这就是合格交付，不是失败。
    """
    out = {}
    for kind in ("corrected", "confirmed", "session_end"):
        tot = conn.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind=?", (kind,)).fetchone()[0]
        sd = conn.execute(
            "SELECT COUNT(*) FROM xtmem_events WHERE kind=? AND agent IN ('dsh','zcode')", (kind,)
        ).fetchone()[0]
        out[kind] = {"total": tot, "in_sample_domain": sd, "out_of_domain": tot - sd}
    return out


def main():
    ap = argparse.ArgumentParser(prog="xtmem_r1_pipeline")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--window-days", type=float, default=7.0)
    args = ap.parse_args()
    # 🔴 P7-B：dry-run = 真只读（工单硬项"生产库零写"）。
    #   两道保险：① ro=True 的连接本身拒绝写；② 下面 dry-run 分支整段跳过 INSERT。
    #   只靠"人别跑正式批"是记性，不是纪律——用连接模式和分支把误写堵死。
    conn = open_db(args.db, ro=bool(args.dry_run), timeout=60)
    try:
        if not args.dry_run:
            ensure_eval_table(conn)   # 只读连接下建表会抛错，故仅正式批建
        domains = sorted(SAMPLE_DOMAINS)
        sessions = fetch_sessions(conn, args.window_days, domains)[: max(args.sessions, 0)]
        rows_out = []
        for sess in sessions:
            gate = f3_gate(sess)
            inj = len(sess["verdicts"])
            # P7-B：不再写 arm 语义（列保留，固定占位 "-"）；harmful 不再计算（判据已废）
            if not args.dry_run:   # 🔴 dry-run 零写：整段跳过 INSERT（见文件头写库纪律）
                conn.execute(
                    "INSERT INTO xtmem_r1_eval (ts, session_id, agent, arm, user_turns, task_requests,"
                    " has_selftest, has_recall_injection, valid, verdict, harmful_injections, injections, dry_run, meta)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), sess["session_id"], sess["agent"], "-", gate["user_turns"],
                     gate["task_requests"], gate["has_selftest"], gate["has_recall_injection"],
                     int(gate["valid"]), sess["verdicts"][0] if sess["verdicts"] else "空结果",
                     None, inj, 0,
                     json.dumps({"gates": gate["gates"], "verdicts_head": sess["verdicts"][:5]}, ensure_ascii=False)),
                )
            rows_out.append({"session": sess["session_id"], "agent": sess["agent"],
                             "valid": gate["valid"], "turns": gate["user_turns"], "inj": inj})
        if not args.dry_run:
            conn.commit()
        valid_rows = [r for r in rows_out if r["valid"]]
        n_valid = len(valid_rows)

        signals = domain_signal_scan(conn)
        attribution = corrected_attribution(conn, domains=set(domains), window_days=args.window_days)

        report = {
            "mode": "dry-run 空转（不产生正式结论）" if args.dry_run else "正式批",
            "note": "P7-B 纯减法版：旧判据三件已删；主判据换为 corrected 归因（24h 窗口）",
            "candidates": len(rows_out), "valid": n_valid,
            "sample_domain_signal_counts": signals,
            "corrected_attribution": attribution,
            "consumer": "注入质量纵向回归监测（R1 已从 P5.0 波2 前置摘除；数据线接通归 P7.8）",
        }
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print("-- 会话明细（无 arm/无 harmful：旧判据已删）--")
        for r in rows_out:
            print("  %-26s %-8s valid=%s turns=%s inj=%s" % (r["session"][:24], r["agent"], r["valid"], r["turns"], r["inj"]))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
