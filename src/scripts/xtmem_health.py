# -*- coding: utf-8 -*-
"""xtmem_health.py — XTMEM 记忆库健康检查（assistant-a运维守门人的数据源）。

逐项查 6 项指标真实值 + 判定规则 + 事件检测（首次出现 + 持续时间）。
纯 SQL 只读（mode=ro），不写库；事件状态存 health_state.json。
输出给assistant-a：逐项真相 + 汇总状态（🟢🟡🔴）+ 异常事件列表。

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

try:  # 包内与同目录直跑两态：库路径 / 插件根统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

# 监控目标库 = DB1（双轨修复后唯一活库，assistant-a/assistant-b/zcode 共用）
# D0-0（2026-09-09，assistant-c）：支持 env / 参数覆盖，测试默认禁写生产目录（错题本 #28 铁律）
DB_DEFAULT = common.resolve_db_path()
DB = os.environ.get("XTMEM_DB_PATH", DB_DEFAULT)
STATE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_state.json")
STATE = os.environ.get("XTMEM_HEALTH_STATE", STATE_DEFAULT)
# 指标目录：默认跟库放同一个用户目录（$XTMEM_HOME/logs）；$XTMEM_METRICS_LOG 可覆盖。
METRICS_DIR_DEFAULT = str(Path(os.environ.get("XTMEM_HOME", "~/.xtmem")).expanduser() / "logs")
METRICS_DIR = os.environ.get("XTMEM_METRICS_LOG", METRICS_DIR_DEFAULT)

# 判定阈值（assistant-a v2.0 §3 默认）——P3.1-a 缺口修复（2026-09-01）：从 xtmem_tuning.json 的 health 段读，缺项用默认兜底；
# 否则改 json 对健康检查无影响（审计 5.3 前置）。
_DEFAULT_TH = {
    "vector_min": 0.90,        # 向量覆盖最低 90%
    "garbled_max": 0.005,      # 乱码率最高 0.5%
    "untagged_max": 0.20,      # 未打标率最高 20%（超此且非历史遗留才告警）
    "heartbeat_max_h": 48,     # 心跳最长 48 小时无新写入
    "backup_max_h": 30,        # R6b-1（P6 波1）：备份事件年龄 >30h 红（每日 03:00 + 容错 3h）
    "wal_warn_mb": 50,         # 贰③b（v1.3.2）：-wal 尺寸 warn 阈值（备份后 TRUNCATE 归零，健康态≈0）
    "wal_red_mb": 100,         # 贰③b：-wal 红线
    "cache_ratio_warn": 0.50,  # 叁②：embedding_cache 行数 / active chunks 比率 warn 线
    "cache_ratio_red": 0.10,   # 叁②：红线（cache 消失案监控面）
    "size_budget_mb": 100,     # 库体积预算 100MB
    "size_delta_ratio": 0.20,  # 体积环比突变 ±20%
    "annotation_min": 0.7,     # P4.3 5.3：注释覆盖率最低 70%
    "ghost_ratio_max": 0.005,       # D0-3：幽灵率 ≤0.5%（30 天事件窗）
    "ttl_expired_ratio_max": 0.05,  # D0-3：TTL 过期率 ≤5%（refined 阈值硬编码 0，不进 tuning）
}

try:
    import sys as _sys
    _plug = common.ensure_importable()
    if not _plug:
        raise ImportError("plugin root not found")
    from memory.tuning import get as _tg
    TH = {k: float(_tg("health", k, v)) for k, v in _DEFAULT_TH.items()}
except Exception:
    TH = dict(_DEFAULT_TH)


def _one(c, sql):
    return c.execute(sql).fetchone()[0]


def _backup_age_hours() -> float | None:
    """R6b-1（P6 波1）：最新 kind='backup' 事件的年龄（小时）。无备份事件 → None（视为红）。"""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # 总则 5：探查只读
        try:
            row = conn.execute(
                "SELECT MAX(ts) FROM xtmem_events WHERE kind='backup'"
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return round((time.time() - row[0]) / 3600.0, 1)
    except Exception:
        pass
    return None


def _wal_size_mb() -> float:
    """贰③b（v1.3.2）：SQLite -wal 文件尺寸 MB（TRUNCATE 后健康态≈0）。"""
    try:
        return round(os.path.getsize(DB + "-wal") / (1024 * 1024), 1)
    except OSError:
        return 0.0


def _cache_ratio() -> float:
    """叁②（防再删三层）：embedding_cache 行数 / active chunks（cache 消失案监控面）。"""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            n_cache = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE state='active'").fetchone()[0]
        finally:
            conn.close()
        return round(n_cache / n_chunks, 3) if n_chunks else 1.0
    except Exception:
        return 0.0


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
    _plug = _P(common.plugin_root() or ".")
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

    # ── D0-3（2026-09-09 SupportBundle Spec v1.2，assistant-c）：四只读指标 ──
    # 1) ghost_ratio：recall 事件命中已退役条目（ts>updated_at 且 state 退役）。
    #    ⚠️ 只有 30 天窗口——xtmem_events_purge 会清洗 recall 事件，fmt 行里注明。
    rec_total = _one(c, "SELECT count(*) FROM xtmem_events WHERE kind='recall' "
                        "AND target_chunk IS NOT NULL AND target_chunk != ''")
    ghosts = _one(c, "SELECT count(*) FROM xtmem_events e JOIN chunks ch ON e.target_chunk=ch.id "
                     "WHERE e.kind='recall' AND e.ts > ch.updated_at "
                     "AND ch.state IN ('superseded','archived')")
    ghost_ratio = round(ghosts / rec_total, 4) if rec_total else 0.0

    # 2) TTL 过期率：必须 import 同一个 _classify_ttl 保证口径一致（禁 SQL 重写——
    #    分类依赖正文关键词/tier/derived_from）。import 失败 → 指标置 None，judge 判 warn（fail-closed）。
    ttl_expired = None
    ttl_expired_refined = None
    try:
        import sys as _sys2
        _pd = common.plugin_root()
        if _pd and _pd not in _sys2.path:
            _sys2.path.insert(0, _pd)
        from memory.hybrid_retriever import _classify_ttl as _cttl
        _now_ts = time.time()
        ttl_expired = 0
        ttl_expired_refined = 0
        for _cid, _content, _tags_json, _track, _tier, _derived, _ts in c.execute(
            "SELECT id, content, tags, track, tier, derived_from, timestamp "
            "FROM chunks WHERE state='active'"
        ).fetchall():
            try:
                _tags = json.loads(_tags_json) if isinstance(_tags_json, str) else (_tags_json or [])
            except Exception:
                _tags = []
            _ttl = _cttl(_content, _tags, _track or "default",
                         tier=(_tier or "raw"), derived_from=(_derived or ""))
            if _ttl > 0 and (_now_ts - (_ts or 0)) > _ttl:
                ttl_expired += 1
                if (_tier or "raw") == "refined":
                    ttl_expired_refined += 1
    except Exception:
        ttl_expired = None
        ttl_expired_refined = None
    ttl_expired_ratio = (round(ttl_expired / total, 4) if total and ttl_expired is not None else None)

    # 3) superseded 指针完整性：Python 侧 re.fullmatch 全字正则（SQLite 无 REGEXP，禁宽 GLOB）。
    #    D1-1 判据（2026-09-09 随迁移同步升级，勿退回旧口径）——合规是二选一：
    #      (a) 真指针 ^mem-[0-9a-f]{8}$ 且目标行还在；或
    #      (b) 指针为空但 retire_reason 非空 = 「已说明原因的退役」（D1 把原因搬进新列后的正常态）。
    #    违例 = 既不知道被谁替代、也不知道为什么退；或指针形状对但目标已删（dangling）。
    #    为什么必须跟着改：旧判据只认指针，D1 之前把 332+1 条原因字串算违规（这正是 D1 要治的）；
    #    迁移后这些行的 superseded_by 变空串，沿用旧判据照样红——演练副本实测如此。判据不升级，
    #    工单里「B4 指标迁移后归零」这条验收在数学上就不可能成立。
    import re as _re_d1
    _PTR_RE = _re_d1.compile(r"^mem-[0-9a-f]{8}$")
    _all_ids = {r[0] for r in c.execute("SELECT id FROM chunks").fetchall()}
    _bad_sup = 0
    _explained = 0
    _dangling = 0
    for (_ptr, _rr) in c.execute(
        "SELECT superseded_by, retire_reason FROM chunks WHERE state='superseded'"
    ).fetchall():
        _p = str(_ptr or "").strip()
        if not _p:
            if str(_rr or "").strip():
                _explained += 1
            else:
                _bad_sup += 1
            continue
        if not _PTR_RE.fullmatch(_p):
            _bad_sup += 1          # 原因字串还塞在指针位 = 该库未做 D1 迁移
        elif _p not in _all_ids:
            _dangling += 1
            _bad_sup += 1
    _bad_arch = 0
    for (_ptr,) in c.execute(
        "SELECT superseded_by FROM chunks WHERE state='archived' "
        "AND superseded_by IS NOT NULL AND superseded_by != ''"
    ).fetchall():
        _p = str(_ptr or "").strip()
        if _p != "forget" and (not _PTR_RE.fullmatch(_p) or _p not in _all_ids):
            _bad_arch += 1
    pointer_violations = _bad_sup + _bad_arch

    # 4) shadow_edge_backlog：两端均 active 的 supersedes/replaces 影子边（B6 口径，只观测不告警）
    try:
        shadow_backlog = _one(
            c,
            "SELECT count(*) FROM memory_evolution e "
            "JOIN chunks c1 ON e.source_id=c1.id JOIN chunks c2 ON e.target_id=c2.id "
            "WHERE e.shadow_state=1 AND e.relation IN ('supersedes','replaces') "
            "AND c1.state='active' AND c2.state='active'",
        )
    except Exception:
        shadow_backlog = 0

    con.close()
    return {
        "total": total,
        "vector": {"covered": covered, "ratio": round(vector_ratio, 4)},
        "fts": {"rows": fts_rows, "ok": fts_ok},
        "garbled": {"count": garbled, "ratio": round(garbled_ratio, 4)},
        "untagged": {"count": untagged, "ratio": round(untagged_ratio, 4)},
        "heartbeat": {"last_ts": last_ts, "hours": round(hours_since, 1)},
        "size_mb": round(size_mb, 1),
        "annotation": {"covered": annot_ok, "total": annot_total, "ratio": round(annot_ratio, 4)},
        # R6b-1（P6 波1）：第七项备份新鲜度——最新 kind='backup' 事件年龄
        "backup": {"hours": _backup_age_hours()},
        # 贰③b + 叁②（v1.3.2）：-wal 尺寸 + embedding_cache/active chunks 比率
        "wal": {"mb": _wal_size_mb()},
        "cache_ratio": _cache_ratio(),
        # D0-3（2026-09-09）：四只读指标
        "ghost": {"recalls": rec_total, "ghosts": ghosts, "ratio": ghost_ratio},
        "ttl_expired": {"count": ttl_expired, "ratio": ttl_expired_ratio,
                        "refined": ttl_expired_refined},
        # D1-1：explained=已说明原因的退役（合规）、dangling=指针指向已删行（违例的一种）
        "pointer_integrity": {"violations": pointer_violations,
                              "bad_superseded": _bad_sup, "bad_archived": _bad_arch,
                              "explained": _explained, "dangling": _dangling},
        "shadow_backlog": {"edges": shadow_backlog},
    }


def judge(d: dict, prev_size: float | None) -> dict:
    """按assistant-a v2.0 §3 判定规则，逐项出状态。"""
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
    # 7 备份新鲜度（R6b-1，P6 波1）：>30h 红——备份没跑=恢复能力悬空，比多数 warn 严重
    age = d.get("backup", {}).get("hours")
    r["backup"] = "red" if (age is None or age > TH["backup_max_h"]) else "ok"
    # 8 -wal 尺寸（贰③b，v1.3.2）：>50MB warn / >100MB 红
    wal_mb = d.get("wal", {}).get("mb", 0.0)
    r["wal"] = "red" if wal_mb > TH["wal_red_mb"] else ("warn" if wal_mb > TH["wal_warn_mb"] else "ok")
    # 9 缓存比率（叁②，防再删三层）：<50% 黄 / <10% 红（cache 消失案监控面）
    ratio = d.get("cache_ratio", 1.0)
    r["cache_ratio"] = "red" if ratio < TH["cache_ratio_red"] else ("warn" if ratio < TH["cache_ratio_warn"] else "ok")
    # D0-3（2026-09-09）：四指标判定
    # ghost：>0.5% warn（30 天窗口，events purge 所致——fmt 里注明）
    _g = d.get("ghost", {}).get("ratio")
    r["ghost"] = "warn" if (_g is not None and _g > TH["ghost_ratio_max"]) else "ok"
    # TTL：指标不可得（import 失败 fail-closed）→ warn；ratio >5% → warn；refined >0 → warn（阈值=0）
    _te = d.get("ttl_expired", {})
    _tr = _te.get("ratio")
    _tref = _te.get("refined")
    r["ttl_expired"] = "ok"
    if _tr is None or _tr > TH["ttl_expired_ratio_max"]:
        r["ttl_expired"] = "warn"
    if _tref is None or _tref > 0:
        r["ttl_expired"] = "warn"
    # 指针完整性：迁移前告警即可（B4 待迁移 1226），迁移后应 =0
    _pv = d.get("pointer_integrity", {}).get("violations")
    r["pointer_integrity"] = "warn" if (_pv is None or _pv > 0) else "ok"
    # shadow_edge_backlog：只观测不告警（B6 口径）→ 恒 ok
    r["shadow_backlog"] = "ok"
    # R6b（红线5）：judge 补 red 级——red 列表单列，供下游告警直接取
    r["_red"] = [k for k, v in r.items() if not k.startswith("_") and v == "red"]
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
    """事件检测：命中即记录，跟踪首次出现时间 + 持续时间。

    ⚠️ 跳过 `_` 前缀键（2026-09-14 修）：judge_r 里带 `_red` 这类**元字段**（red 指标名
    列表，非指标本身）。原实现遍历全部 key → `[] != "ok"` 恒真 → `_red` 被当成一个永久的
    异常项写进事件表（体检输出里那条「_red 持续 120.9h」伪项即此），污染状态文件与告警面。
    """
    now = time.time()
    new_state = {}
    events = {}
    for key, status in judge_r.items():
        if key.startswith("_"):
            continue  # 元字段不是指标，不进事件/状态（见上）
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
    """拼给assistant-a看的逐项真相文本。"""
    icon = {"ok": "✓", "warn": "⚠", "red": "✗"}
    lines = []
    lines.append(f"XTMEM 记忆库体检 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"总量 {d['total']} 条 · 体积 {d['size_mb']} MB")
    lines.append("")
    lines.append(f"{icon[judge_r['vector']]} 向量覆盖 {d['vector']['ratio']*100:.1f}%（{d['vector']['covered']} 条有缓存）")
    lines.append(f"{icon[judge_r['fts']]} FTS 完整性 {d['fts']['rows']}/{d['total']}")
    lines.append(f"{icon[judge_r['garbled']]} 乱码 {d['garbled']['count']} 条（{d['garbled']['ratio']*100:.2f}%）")
    lines.append(f"{icon[judge_r['untagged']]} 未打标 {d['untagged']['count']} 条（{d['untagged']['ratio']*100:.1f}%）")
    last = time.strftime("%m-%d %H:%M", time.localtime(d['heartbeat']['last_ts'])) if d['heartbeat']['last_ts'] else "无"
    lines.append(f"{icon[judge_r['heartbeat']]} 心跳 {d['heartbeat']['hours']}h 前（{last}）")
    lines.append(f"{icon[judge_r['size']]} 体积 {d['size_mb']} MB")
    lines.append(f"{icon[judge_r['annotation']]} 注释覆盖 {d['annotation']['ratio']*100:.1f}%（{d['annotation']['covered']}/{d['annotation']['total']}）")
    # R6b-1（P6 波1）：第七项备份
    b_age = d.get('backup', {}).get('hours')
    b_txt = f"{b_age}h 前" if b_age is not None else "无备份事件"
    lines.append(f"{icon[judge_r.get('backup', 'ok')]} 备份 {b_txt}（>30h 红）")
    # 贰③b + 叁②（v1.3.2）：-wal 尺寸与缓存比率
    lines.append(f"{icon[judge_r.get('wal', 'ok')]} -wal {d.get('wal', {}).get('mb', 0)} MB（warn >{TH['wal_warn_mb']} / red >{TH['wal_red_mb']}）")
    _cr = d.get('cache_ratio', 1.0)
    lines.append(f"{icon[judge_r.get('cache_ratio', 'ok')]} 缓存比率 {_cr*100:.0f}%（warn <{TH['cache_ratio_warn']*100:.0f}% / red <{TH['cache_ratio_red']*100:.0f}%）")
    # D0-3（2026-09-09）：四指标显示行
    _gh = d.get('ghost', {})
    lines.append(f"{icon[judge_r.get('ghost', 'ok')]} 幽灵率 {_gh.get('ratio', 0)*100:.2f}%（{_gh.get('ghosts', 0)}/{_gh.get('recalls', 0)}，阈值 ≤{TH['ghost_ratio_max']*100:.1f}%；⚠️ 仅 30 天事件窗——recall 事件按期清洗，天然看不到更早的）")
    _te2 = d.get('ttl_expired', {})
    _tr2 = _te2.get('ratio')
    _tr_txt = f"{_tr2*100:.1f}%" if _tr2 is not None else "N/A"
    _tref2 = _te2.get('refined')
    _tref_txt = str(_tref2) if _tref2 is not None else "N/A"
    lines.append(f"{icon[judge_r.get('ttl_expired', 'ok')]} TTL 过期 {_tr_txt}（{_te2.get('count', 'N/A')} 条；阈值 ≤{TH['ttl_expired_ratio_max']*100:.0f}%）/ refined 隐身 {_tref_txt} 条（阈值 =0）")
    _pi = d.get('pointer_integrity', {})
    lines.append(f"{icon[judge_r.get('pointer_integrity', 'ok')]} 指针完整性 违例 {_pi.get('violations', 0)} 条（superseded {_pi.get('bad_superseded', 0)} + archived {_pi.get('bad_archived', 0)}，其中 dangling {_pi.get('dangling', 0)}；已说明原因退役 {_pi.get('explained', 0)} 条不计违例。判据 D1-1：真指针或 retire_reason 二者居一即合规）")
    _sb = d.get('shadow_backlog', {})
    lines.append(f"{icon[judge_r.get('shadow_backlog', 'ok')]} 影子边积压 {_sb.get('edges', 0)} 条（两端 active 的 supersedes/replaces，只观测不告警）")
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


def main(argv=None) -> int:
    global DB, STATE, METRICS_DIR
    import argparse
    ap = argparse.ArgumentParser(description="XTMEM 记忆库体检")
    ap.add_argument("--db", default=DB, help="数据库路径")
    ap.add_argument("--state", default=STATE, help="状态文件路径")
    ap.add_argument("--metrics-dir", default=METRICS_DIR, help="指标目录")
    args = ap.parse_args(argv)

    DB = args.db
    STATE = args.state
    METRICS_DIR = args.metrics_dir

    # D0-0 铁律防御：若指向非默认 DB 且未显式指定 state/metrics，自动在 DB 目录隔离，禁写生产目录
    if DB != DB_DEFAULT:
        db_parent = os.path.dirname(os.path.abspath(DB))
        if STATE == STATE_DEFAULT:
            STATE = os.path.join(db_parent, "health_state.json")
        if METRICS_DIR == METRICS_DIR_DEFAULT:
            METRICS_DIR = os.path.join(db_parent, "logs")

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
    vals = [s for k, s in judge_r.items() if not k.startswith("_")]
    if any(s == "warn" for s in vals):
        worst = "warn"
    if any(s == "red" for s in vals):
        worst = "red"
    summary = {"ok": "🟢 健康", "warn": "🟡 有小毛病", "red": "🔴 有红灯项"}
    print(fmt(d, judge_r, events))
    # P3.1-b 指标历史：每跑完追加一行 jsonl（时间戳 + 六指标值 + 判定状态）
    try:
        _logdir = METRICS_DIR
        os.makedirs(_logdir, exist_ok=True)
        _line = json.dumps({
            "ts": time.time(), "total": d["total"],
            "vector_ratio": d["vector"]["ratio"], "fts_ok": d["fts"]["ok"],
            "garbled_ratio": d["garbled"]["ratio"], "untagged_ratio": d["untagged"]["ratio"],
            "heartbeat_h": d["heartbeat"]["hours"], "size_mb": d["size_mb"],
            "ghost_ratio": d.get("ghost", {}).get("ratio"),
            "ttl_expired_ratio": d.get("ttl_expired", {}).get("ratio"),
            "ttl_expired_refined": d.get("ttl_expired", {}).get("refined"),
            "pointer_violations": d.get("pointer_integrity", {}).get("violations"),
            "shadow_backlog": d.get("shadow_backlog", {}).get("edges"),
            "status": worst,
        }, ensure_ascii=False)
        with open(os.path.join(_logdir, "xtmem_metrics_history.jsonl"), "a", encoding="utf-8") as _f:
            _f.write(_line + chr(10))
    except Exception:
        pass
    print(f"\n状态：{summary[worst]}")
    # ── 红灯告警落地（2026-09-14，修复方向 v3.2「忘不掉」三小件之③）──────────
    # 病灶（顶模裁定四）：红灯算出来只进 stdout/jsonl，**无推送通道**——本次 failed=4 是
    #   人工审计撞见的、不是报警报的。备份没跑=恢复能力悬空，属"忘不掉"缺口，必须可被动发现。
    # 修法：红灯（或备份项为 red）时写一条 kind='health_red' 事件到库——下游（信箱观察者 /
    #   驾驶舱 / 任何巡检）可拉；事件面是既有通道，不新增依赖、不改调度。
    # ⚠️ 只写事件不改退出码（AF-6：健康检查"发现问题"≠"脚本失败"，始终 exit 0）。
    # ⚠️ 写失败必须静默（try 包住）——告警通道故障不能反过来让体检本身失败。
    try:
        _reds = judge_r.get("_red") or []
        _prev_reds = (prev.get("_red_last") or []) if isinstance(prev.get("_red_last"), list) else []
        if _reds and _reds != _prev_reds:
            # 只在"红灯集合变化"时写（防每 5 分钟重复刷同一条红灯）
            import sqlite3 as _sq
            _c = _sq.connect(DB, timeout=10)
            try:
                _c.execute(
                    "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'health_red', ?)",
                    (time.time(), json.dumps({
                        "reds": _reds, "worst": worst,
                        "backup_age_h": d.get("backup", {}).get("hours"),
                        "size_mb": d.get("size_mb"),
                        "prev_reds": _prev_reds,
                    }, ensure_ascii=False)),
                )
                _c.commit()
            finally:
                _c.close()
            new_state["_red_last"] = _reds
            save_state(new_state)
    except Exception:
        pass
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
