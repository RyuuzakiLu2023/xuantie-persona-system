# -*- coding: utf-8 -*-
"""xtmem_profile.py — 画像层聚合器。

【这是什么】
把每个 namespace 的琐碎记忆聚合成一条「画像」chunk：
  - track='profile' / tier='refined' / importance=1.0 / derived_from='profile-aggregator'
  - 每域恰好 1 条活画像；旧画像 supersede 到新版（保留历史）
  - 每条后缀 [类型|src:xxxxxxxx]（句级溯源）
  - 正文固定"track 段 → 条目"两级，每段 ≤25 条且总量 ≤PROFILE_CAP_CHARS

【为什么画像要单独一层】
零散要点直接注入会把预算吃光，而且「这个人/这个 agent 是谁」需要跨条目归纳。
画像是**可回退的压缩视图**：原料一条不删，只是另存一份摘要。

【取材纪律（M04 契约，改前必读）】
  1. 派生原料不能冒充第一手证据：取材统一走 xtmem_sources.select_materials，
     本生成器旧视图 / 兄弟生成器页 / 聚合页一律不入料；
     distill 类要点**保留**可用——一刀切会把整个域砍空，那是另一种失真。
  2. 类型不混写：每行带类型标签（原话/事实/推断/未标注）。
  3. 权限前置：不可见的行在取材前就剔除，不是生成后靠 min_visibility 兜。
  4. 深度写死两级：不读上一版画像做递归汇总，所以连续刷新不会自引。
  5. source_ids 只登记**正文实际采用**的条目（登记没用到 = 顺手改权限）。

【公开化改动（相对运行正本）】
  - 库路径/CLI/引擎统一走 xtmem_common；不写死生产路径。
  - 默认 namespace = 库里实际存在的域（不预设私人域）。
  - 时区走 xtmem_common.tz()（默认 Asia/Shanghai，$XTMEM_TZ 可覆盖）。
  - use_llm 形参一直未被使用，本版如实说明：正文是纯规则生成，没有 LLM 通道。

用法：python xtmem_profile.py [--domain <ns>] [--days 7] [--db <path>] [--no-llm]
输出：JSON 一行
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
    from .xtmem_sources import (KIND_LABEL, actual_source_ids, ingested_time,
                                occurrence_time, quarantine_ok, select_materials)
except ImportError:  # pragma: no cover
    import xtmem_common as common
    from xtmem_sources import (KIND_LABEL, actual_source_ids, ingested_time,
                               occurrence_time, quarantine_ok, select_materials)

# 与 xtmem_timeline 同口径：库内 timestamp 是 UTC epoch，但"哪天发生/哪天入库"是给人看的
# 本地语义，一律按同一时区划。两脚本必须同一个时区，否则同一批材料在两边显示成不同日期。
BJ = common.tz()

TRACK_WEIGHT = {"identity": 3.0, "preference": 2.0, "emotion": 0.5, "default": 1.0}
CHUNK_PREVIEW = 200  # 每条截断 200 字符

_VS = None


def _vecstore(db_path: str):
    """supersede 的唯一写路径 = VectorStore.mark_superseded（联动 retire 三列 + FTS + 缓存）。"""
    global _VS
    if _VS is None:
        _VS = common.vector_store(db_path)
    return _VS


def _select_candidates(conn, ns, days):
    """取本域候选原料；**先**过可见性、**再**过取材闸，两关都在进正文之前。

    ⚠️【不】把 visible 过滤删掉只留 SQL：viewer 是域身份，SQL 里写不出
      shared-with 的名单交集合语义（memory.visibility 是唯一实现）。
    ⚠️ 画像只用「非流水」原料：raw 流水会被 _infer_track 误标 preference/default，
      再经画像 + importance=1.0 被「洗白」成永久要点（TTL 对画像不生效）。
    """
    cutoff = time.time() - days * 86400
    has_ct = any(r[1] == "content_time"
                 for r in conn.execute("PRAGMA table_info(chunks)"))
    # retire_reason → 统一读闸的隔离判据；source_ids → 血缘深度解析。缺列只会静默少一层保护。
    cols = ("id, project, content, importance, track, timestamp, derived_from, tier, "
            "visibility, namespace, speaker, provenance_status, confirmation_type, "
            "retire_reason, source_ids")
    if has_ct:
        cols += ", content_time"
    rs = conn.execute(
        f"SELECT {cols} FROM chunks "
        "WHERE state='active' AND namespace=? AND timestamp>=? "
        "AND track IN ('identity','preference','emotion','default') "
        "AND (tier IS NULL OR tier != 'raw') "
        "AND content NOT LIKE 'User: %' "
        "ORDER BY importance DESC, timestamp DESC LIMIT 150",
        (ns, cutoff),
    ).fetchall()

    visible = [r for r in rs if quarantine_ok(r, ns)]
    kept, rejected = select_materials(visible, "profile", conn=conn)
    _select_candidates.last_rejected = rejected
    return kept


def _preview(content):
    return str(content or "")[:CHUNK_PREVIEW]


def _rule_build(kinded_rows):
    """纯规则画像：按 track 分组 + 重要度摘录，每条带 [类型|src:xxxxxxxx]。

    O3-a 加帽：出料 ≤PROFILE_CAP_CHARS（默认 2000 字符），按行切且 [src:] 在行尾不截半；
    无总量帽时历史画像曾溢出 5 倍（注入预算吃光）。句序 = track 优先级 + importance 降序。

    返回 (body, used_kinded)：used_kinded 是**真正写进正文**的那些行。
    ⚠️【不】从正文反解 [src:xxxxxxxx] 再回查候选：库里 id 是 mem-<8位>、正文只显示前 8 字符，
      前缀撞车会把没写进正文的条目也算成已采用。
    """
    groups = {"identity": [], "preference": [], "emotion": [], "default": []}
    for row, kind in kinded_rows:
        trk = str(_col_val(row, "track") or "default")
        if trk not in groups:
            trk = "default"
        groups[trk].append((row, kind))
    out, used = [], []
    used_chars = 0
    cap = int(os.environ.get("PROFILE_CAP_CHARS", "2000"))
    # identity / preference 优先（画像核心原料），emotion 只做趋势不做结论，default 兜底
    for track in ("identity", "preference", "default", "emotion"):
        items = sorted(groups.get(track, []),
                       key=lambda rk: -(_col_val(rk[0], "importance") or 0))
        if not items:
            continue
        out.append(f"## {track}")
        used_chars += len(f"## {track}") + 1
        for row, kind in items[:25]:
            _tag = "src-raw" if (_col_val(row, "tier") == "raw") else "src"
            # 每行带"发生/入库"时间依据，和 timeline 同口径。
            # ⚠️ _time_txt 自带方括号，这里**不再套一层**（否则出现 "[[入库:...]]"）。
            line = (f"- [{kind}|{_tag}:{str(_col_val(row, 'id'))[:8]}] "
                    f"{_time_txt(row)} {_preview(_col_val(row, 'content'))}")
            if used_chars + len(line) > cap:
                break  # 已超帽：停止本 track（更低 importance 的句更无必要）
            out.append(line)
            used.append((row, kind))
            used_chars += len(line) + 1
    return "\n".join(out), used


def _time_txt(row):
    """一条材料的"发生 / 入库"时间串。

    与 xtmem_timeline._bucket 同一口径，**统一带方括号**（调用方直接拼，不再套一层）：
      - 有 content_time → "[发生YYYY-MM-DD]"，若入库与之相差 >1 天再补 "/入库YYYY-MM-DD"
      - 无 content_time → "[入库:YYYY-MM-DD]"（照实说只有入库时间）
      - 两个都没有     → "[时间未知]"（不编、不猜、不拿生成时间顶替）
    """
    occ = occurrence_time(row)
    ing = ingested_time(row)
    if occ is not None:
        txt = "发生" + datetime.fromtimestamp(occ, tz=BJ).strftime("%Y-%m-%d")
        if ing is not None and abs(ing - occ) > 86400:
            txt += "/入库" + datetime.fromtimestamp(ing, tz=BJ).strftime("%Y-%m-%d")
        return "[" + txt + "]"
    if ing is not None:
        return "[入库:" + datetime.fromtimestamp(ing, tz=BJ).strftime("%Y-%m-%d") + "]"
    return "[时间未知]"


def _col_val(row, name):
    """sqlite3.Row 没有 .get()；dict 有。两种都能读。"""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def _supersede_old(conn, db_path, ns, new_id):
    """把该域现有活画像 supersede，并回指新版。

    收编到 mark_superseded 后 retire_reason/profile-reaggregate、retired_by/profile、
    retired_at 三列有值，FTS 行与 embedding_cache 也随写路径联动清理。
    """
    rows = conn.execute(
        "SELECT id FROM chunks WHERE namespace=? AND state='active' AND track='profile' AND id!=?",
        (ns, new_id),
    ).fetchall()
    vs = _vecstore(db_path)
    n = 0
    for rr in rows:
        if vs.mark_superseded(rr["id"], successor_id=new_id,
                              reason="profile-reaggregate", by="profile"):
            n += 1
    return n


def _run_domain(conn, db_path, ns, days, use_llm):
    rows = _select_candidates(conn, ns, days)
    # O3-a 回落：有机 refined（derived_from 空）<12 行 → 按 utility 补 raw 标 [src-raw]。
    # ⚠️ 补料支路必须与主路**同列、同闸、同解析、同时间**：
    #   历史上这里是一处独立 SELECT + 独立过滤，主路修好的保护在侧门同时丢失
    #   （漏 retire_reason → 隔离正文进画像；不传 conn → 两跳血缘不查；漏 content_time）。
    if len([rk for rk in rows if not (_col_val(rk[0], "derived_from") or "")]) < 12:
        has_ct = any(r[1] == "content_time"
                     for r in conn.execute("PRAGMA table_info(chunks)"))
        fb_cols = ("id, project, content, importance, track, timestamp, derived_from, tier, "
                   "visibility, namespace, speaker, provenance_status, confirmation_type, "
                   "retire_reason, source_ids")
        if has_ct:
            fb_cols += ", content_time"
        fall = conn.execute(
            f"SELECT {fb_cols} FROM chunks "
            "WHERE state='active' AND namespace=? AND timestamp>=? "
            "AND track IN ('identity','preference','emotion','default') "
            "AND tier='raw' AND content NOT LIKE 'User: %' "
            "ORDER BY importance DESC, timestamp DESC LIMIT ?",
            (ns, time.time() - days * 86400, 12),
        ).fetchall()
        if fall:
            fb_vis = [r for r in fall if quarantine_ok(r, ns)]
            fb_kept, fb_rej = select_materials(fb_vis, "profile", conn=conn)
            rows = rows + list(fb_kept)
            _run_domain.fallback_rejected = len(fb_rej) + (len(fall) - len(fb_vis))
    if len(rows) < 5:
        return {"ok": False, "ns": ns, "reason": "candidates<5", "n": len(rows)}
    stamp = datetime.now(BJ).strftime("%Y%m%d")
    body, used_rows = _rule_build(rows)
    if not used_rows:
        # 候选够但一条都没写进正文（例如全被 cap 挡掉）：不落库、不假报成功。
        return {"ok": False, "ns": ns, "reason": "nothing-used", "n": len(rows)}
    source_ids = actual_source_ids(used_rows)
    kinds = {}
    for _row, _kind in used_rows:
        kinds[_kind] = kinds.get(_kind, 0) + 1
    kind_txt = "、".join(f"{k}{v}" for k, v in sorted(kinds.items())) or "无"
    generated_at = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    # 尾行不写"已排除 N 条"——计数是从被排除材料推出来的，写进公开页等于泄露"存在性"；
    # 诊断改走 run_report（受控回执，不落库）。
    content = (
        f"# 画像 · {ns} · {stamp}\n"
        + body
        + "\n---\n"
        + f"本版输入：{len(used_rows)} 条记忆（取数窗口近 {days} 天，按入库时间；"
        + f"类型：{kind_txt}）\n"
        + f"生成时间：{generated_at}（不参与任何一条的时间判定）"
    )
    r = common.store_via_cli(
        db_path=db_path, agent=ns or "default", namespace=ns or "default",
        text=content, importance=1.0, derived_from="profile-aggregator",
        source_ids=source_ids, tier="refined", track="profile",
        tags=["profile", "xtmem"],
    )
    if not r.get("ok"):
        return {"ok": False, "ns": ns, "reason": "store failed", "r": r}
    new_id = r.get("id") or (r.get("chunk") or {}).get("id")
    sup = _supersede_old(conn, db_path, ns, new_id) if new_id else 0
    run_report = {
        "rejected_by_reason": _count_reasons(getattr(_select_candidates, "last_rejected", [])),
        "candidates": len(rows),
        "used": len(used_rows),
    }
    return {"ok": True, "ns": ns, "n": len(rows), "used": len(used_rows),
            "id": new_id, "superseded": sup, "run_report": run_report}


def _count_reasons(rejected):
    """把排除原因收成计数表（只进回执，不进正文）。"""
    out = {}
    for _rid, reason in rejected:
        out[reason] = out.get(reason, 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser(prog="xtmem_profile")
    ap.add_argument("--domain", default=None,
                    help="只处理该 namespace；缺省=库内全部")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=None)
    # --no-llm 保留为兼容位：本实现是纯规则画像，没有 LLM 通道（历史形参 use_llm 从未使用）。
    ap.add_argument("--no-llm", action="store_true", help="兼容位：本版画像本来就是纯规则生成")
    args = ap.parse_args()

    db_path = common.resolve_db_path(args.db)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        domains = ([args.domain] if args.domain
                   else common.resolve_namespaces(conn))
        results = [_run_domain(conn, db_path, ns, args.days, use_llm=not args.no_llm)
                   for ns in domains]
        print(json.dumps({"ok": True, "db": db_path, "results": results},
                         ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        # 兜底 UTF-8 输出（GBK 控制台下 print 中文会 UnicodeEncodeError 崩）
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
