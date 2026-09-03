# -*- coding: utf-8 -*-
"""xtmem_health.py — XTMEM 记忆库健康检查（玄铁运维守门人的数据源）。

逐项查 6 项指标真实值 + 判定规则 + 事件检测（首次出现 + 持续时间）。
纯 SQL 只读（mode=ro），不写库；事件状态存 health_state.json。
输出给玄铁：逐项真相 + 汇总状态（🟢🟡🔴）+ 异常事件列表。

用法：python xtmem_health.py
退出码：0=脚本正常跑完（🟢/🟡 均为正常完成，问题状态写入 stdout/health_state.json）
        2=脚本自身异常（异常才算失败，cron 不会再因"发现小毛病"记 error）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# 监控目标库 = DB1（双轨修复后唯一活库，玄铁/江北/zcode 共用）
DB = r"<XT_ROOT>\mimox-data\.hermes\memory\vector_store.db"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_state.json")

# 判定阈值（玄铁 v2.0 §3 默认）——P3.1-a 缺口修复（2026-09-01）：从 xtmem_tuning.json 的 health 段读，缺项用默认兜底；
# 否则改 json 对健康检查无影响（审计 5.3 前置）。
_DEFAULT_TH = {
    "vector_min": 0.90,        # 向量覆盖最低 90%
    "garbled_max": 0.005,      # 乱码率最高 0.5%
    "untagged_max": 0.20,      # 未打标率最高 20%（超此且非历史遗留才告警）
    "heartbeat_max_h": 48,     # 心跳最长 48 小时无新写入
    "size_budget_mb": 100,     # 库体积预算 100MB
    "size_delta_ratio": 0.20,  # 体积环比突变 ±20%
    "annotation_min": 0.7,     # P4.3 5.3：注释覆盖率最低 70%
}

try:
    import sys as _sys
    _sys.path.insert(0, os.environ.get("XTMEM_PLUGIN_DIR", r"<XT_ROOT>/mimox-data/plugins/xtmem"))
    from memory.tuning import get as _tg
    TH = {k: float(_tg("health", k, v)) for k, v in _DEFAULT_TH.items()}
except Exception:
    TH = dict(_DEFAULT_TH)


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


def collect() -> dict:
    """只读采集 6 项指标真实值。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c = con.cursor()
    total = _one(c, "SELECT COUNT(*) FROM chunks WHERE state='active'")
    # 1 向量覆盖：cache_embedding() 不写 chunk_id（恒 NULL），须用
    #    sha256(content).hexdigest() 关联 embedding_cache.text_hash
    cached_hashes = {r[0] for r in c.execute("SELECT text_hash FROM embedding_cache")}
    covered = 0
    for (content,) in c.execute("SELECT content FROM chunks WHERE state='active'"):
        if hashlib.sha256(content.encode()).hexdigest() in cached_hashes:
            covered += 1
    vector_ratio = covered / total if total else 1.0
    # 2 FTS 完整性：active chunk 的 FTS 行数对上 + 能正常 MATCH（坏索引会抛错）。
    #    P4.3 ⑯ 修复：superseded/archived chunk 遗留 FTS 行是卫生问题（检索按 state 过滤不受影响），
    #    不应让全表 fts_rows 与 active 对比而误报——改按「active chunk 在 FTS 的行数」对齐。
    fts_rows = _one(c, "SELECT COUNT(*) FROM chunks_fts")
    fts_active = _one(c, "SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON f.rowid=c.rowid WHERE c.state='active'")
    try:
        _one(c, "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '记忆'")
        fts_queryable = True
    except Exception:
        fts_queryable = False
    fts_ok = (fts_active == total) and fts_queryable
    # 3 乱码：content 含 U+FFFD 的条数
    garbled = _one(c, "SELECT COUNT(*) FROM chunks WHERE content LIKE '%\ufffd%'")
    garbled_ratio = garbled / total if total else 0.0
    # 4 未打标：updated_at IS NULL 条数
    untagged = _one(c, "SELECT COUNT(*) FROM chunks WHERE updated_at IS NULL")
    untagged_ratio = untagged / total if total else 0.0
    # 5 心跳：最新 chunk 距今小时数
    last_ts = _one(c, "SELECT MAX(timestamp) FROM chunks")
    hours_since = (time.time() - last_ts) / 3600.0 if last_ts else 999.0
    # 6 体积
    size_mb = os.path.getsize(DB) / 1024.0 / 1024.0
    # 7 注释覆盖率（P4.3 5.3）：现役插件 py 文件含文件头导游图（=60 等号）或模块 docstring 的比例
    from pathlib import Path as _P
    _plug = _P(os.environ.get("XTMEM_PLUGIN_DIR", r"<XT_ROOT>/mimox-data/plugins/xtmem"))
    annot_ok = annot_total = 0
    for _p in _plug.rglob("*.py"):
        if "__pycache__" in str(_p) or _p.name.endswith(".cbak"):
            continue
        annot_total += 1
        try:
            _head = _p.read_text(encoding="utf-8")[:2000]
            if "总导游图" in _head or (chr(34)*3) in _head[:400]:
                annot_ok += 1
        except Exception:
            pass
    annot_ratio = annot_ok / annot_total if annot_total else 1.0
    con.close()
    return {
        "total": total,
        "vector": {"covered": covered, "ratio": round(vector_ratio, 4)},
        "fts": {"rows": fts_rows, "active": fts_active, "ok": fts_ok},
        "garbled": {"count": garbled, "ratio": round(garbled_ratio, 4)},
        "untagged": {"count": untagged, "ratio": round(untagged_ratio, 4)},
        "heartbeat": {"last_ts": last_ts, "hours": round(hours_since, 1)},
        "size_mb": round(size_mb, 1),
        "annotation": {"covered": annot_ok, "total": annot_total, "ratio": round(annot_ratio, 4)},
    }


def judge(d: dict, prev_size: float | None) -> dict:
    """按玄铁 v2.0 §3 判定规则，逐项出状态。"""
    r = {}
    # 1 向量覆盖
    r["vector"] = "ok" if d["vector"]["ratio"] >= TH["vector_min"] else "warn"
    # 2 FTS
    r["fts"] = "ok" if d["fts"]["ok"] else "warn"
    # 3 乱码
    r["garbled"] = "ok" if d["garbled"]["ratio"] <= TH["garbled_max"] else "warn"
    # 4 未打标：历史遗留豁免，只认"新增"（新增检测在 main 里对比上次 count，
    #    符合 v2.0 §4「超豁免基线且非历史遗留才告警」——450 条历史遗留不算）
    r["untagged"] = "ok"
    # 5 心跳
    r["heartbeat"] = "ok" if d["heartbeat"]["hours"] <= TH["heartbeat_max_h"] else "warn"
    # 7 注释覆盖率（P4.3 5.3）
    r["annotation"] = "ok" if d["annotation"]["ratio"] >= TH["annotation_min"] else "warn"
    # 6 体积：超预算或环比突变
    r["size"] = "ok"
    if d["size_mb"] > TH["size_budget_mb"]:
        r["size"] = "warn"
    if prev_size is not None and prev_size > 0:
        delta = abs(d["size_mb"] - prev_size) / prev_size
        if delta > TH["size_delta_ratio"]:
            r["size"] = "warn"
    return r


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def build_events(judge_r: dict, prev: dict) -> tuple[dict, dict]:
    """事件检测：命中即记录，跟踪首次出现时间 + 持续时间。"""
    now = time.time()
    new_state = {}
    events = {}
    for key, status in judge_r.items():
        entry = prev.get(key, {}) if isinstance(prev.get(key), dict) else {}
        if status != "ok":
            first = entry.get("first_seen") or now
            new_state[key] = {"status": status, "first_seen": first, "last_seen": now}
            events[key] = {
                "status": status,
                "first_seen": first,
                "duration_h": round((now - first) / 3600.0, 1),
            }
        else:
            new_state[key] = {"status": "ok", "first_seen": None}
    return new_state, events


def fmt(d: dict, judge_r: dict, events: dict) -> str:
    """拼给玄铁看的逐项真相文本。"""
    icon = {"ok": "✓", "warn": "⚠"}
    lines = []
    lines.append(f"XTMEM 记忆库体检 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"总量 {d['total']} 条 · 体积 {d['size_mb']} MB")
    lines.append("")
    lines.append(f"{icon[judge_r['vector']]} 向量覆盖 {d['vector']['ratio']*100:.1f}%（{d['vector']['covered']} 条有缓存）")
    # F-4（2026-09-03 审计整改）：显示用 active 对齐（判据即 fts_active==total），
    # 不再用全表 fts.rows（含 superseded 残余）当分子——避免比值 >100% 观感误导
    lines.append(f"{icon[judge_r['fts']]} FTS 完整性 {d['fts']['active']}/{d['total']}")
    lines.append(f"{icon[judge_r['garbled']]} 乱码 {d['garbled']['count']} 条（{d['garbled']['ratio']*100:.2f}%）")
    lines.append(f"{icon[judge_r['untagged']]} 未打标 {d['untagged']['count']} 条（{d['untagged']['ratio']*100:.1f}%）")
    last = time.strftime("%m-%d %H:%M", time.localtime(d['heartbeat']['last_ts'])) if d['heartbeat']['last_ts'] else "无"
    lines.append(f"{icon[judge_r['heartbeat']]} 心跳 {d['heartbeat']['hours']}h 前（{last}）")
    lines.append(f"{icon[judge_r['size']]} 体积 {d['size_mb']} MB")
    lines.append(f"{icon[judge_r['annotation']]} 注释覆盖 {d['annotation']['ratio']*100:.1f}%（{d['annotation']['covered']}/{d['annotation']['total']}）")
    if events:
        lines.append("")
        lines.append("异常事件：")
        for k, e in events.items():
            first = time.strftime("%m-%d %H:%M", time.localtime(e['first_seen']))
            lines.append(f"  ⚠ {k}：首次 {first}，持续 {e['duration_h']}h")
    else:
        lines.append("")
        lines.append("异常事件：无")
    return "\n".join(lines)


def main() -> int:
    d = collect()
    prev = load_state()
    judge_r = judge(d, prev.get("size_mb") if isinstance(prev.get("size_mb"), (int, float)) else None)
    # 未打标"新增"检测（v2.0 §4：历史遗留不算，新增才告警）
    prev_untagged = prev.get("untagged_count")
    if prev_untagged is not None and d["untagged"]["count"] > prev_untagged:
        judge_r["untagged"] = "warn"
    new_state, events = build_events(judge_r, prev)
    new_state["size_mb"] = d["size_mb"]
    new_state["untagged_count"] = d["untagged"]["count"]
    new_state["updated_at"] = time.time()
    save_state(new_state)
    # 汇总状态
    worst = "ok"
    if any(s == "warn" for s in judge_r.values()):
        worst = "warn"
    summary = {"ok": "🟢 健康", "warn": "🟡 有小毛病"}
    print(fmt(d, judge_r, events))
    # P3.1-b 指标历史：每跑完追加一行 jsonl（时间戳 + 六指标值 + 判定状态）
    try:
        _logdir = os.environ.get("XTMEM_METRICS_LOG", "<XT_ROOT>/mimox-data/logs")
        os.makedirs(_logdir, exist_ok=True)
        _line = json.dumps({
            "ts": time.time(), "total": d["total"],
            "vector_ratio": d["vector"]["ratio"], "fts_ok": d["fts"]["ok"],
            "garbled_ratio": d["garbled"]["ratio"], "untagged_ratio": d["untagged"]["ratio"],
            "heartbeat_h": d["heartbeat"]["hours"], "size_mb": d["size_mb"],
            "status": worst,
        }, ensure_ascii=False)
        with open(os.path.join(_logdir, "xtmem_metrics_history.jsonl"), "a", encoding="utf-8") as _f:
            _f.write(_line + chr(10))
    except Exception:
        pass
    print(f"\n状态：{summary[worst]}")
    # AF-6（2026-08-31，P4 第 1 波）：健康检查"发现问题"≠"脚本失败"。
    # 始终 exit 0，状态放 stdout/health_state.json（下游要告警读状态字段）；
    # 只有脚本自身异常（__main__ 的 except → exit 2）才算失败。
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(2)