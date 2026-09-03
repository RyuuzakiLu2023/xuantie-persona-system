# -*- coding: utf-8 -*-
"""xtmem_wiki.py — P5.0-g 实体页生成器（LLM Wiki 借鉴 · rule-fallback v1）（2026-09-02，小D）。

定位（图谱方案 §二.1 同构 + 红线内化）：
  LLM Wiki 的「wiki 层 ≈ refined 层」——实体页 = refined 层产物的一种形态：
    tier='refined' / derived_from='wiki-entity' / source_ids=源 chunk id / 血缘闸 P0.1
  红线（单用户→多角色）：每页 = derived 实体，必须带 source_ids + min_visibility 继承
  （CLI store 自带最严源继承）；**agent 私页与共享页物理分开**（按池分页，绝不混合聚合）。

v1 范围（对总 Spec P5.0-g：49 个 df≥5 实体建页，挂 T6 蒸馏器产物）：
  - 规则聚合、零 LLM（与 distill/profile/predict 同纪律；LLM 通道待蒸馏器 LLM 化后随附）
  - 分池：共享池（visibility='shared' 源）与私池（visibility!='shared' 源）各一页/（canon, 域）
  - 页内容：别名 / df / 域分布 / 轨道分布 / 关联实体 Top10（tag_edges 边权）/ 近因 Top3 /
    相关蒸馏产物 Top3（derived_from='distill' 且含实体别名）/ 血缘摘要
  - 产物：chunks（wiki-entity）+ 公共索引 memories/xtmem-wiki/INDEX.md（**只列共享页**——
    直连 db 产公共文件必须自滤 visibility，信任边界纪律）

幂等 / 回滚：
  - 同 (canon, 池, 域) 已有 active wiki-entity 页 → 跳过（不重蒸）
  - --rebuild：先 DELETE 本脚本产物再重建（内容随库漂移，人工刷新用）
  - 回滚：DELETE FROM chunks WHERE derived_from='wiki-entity'

用法：python xtmem_wiki.py [--rebuild] [--min-df 5] [--domain <ns>]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")
DEFAULT_CLI = os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py")
PYTHON = sys.executable
# 公共索引落点（信任边界：只列共享页）
INDEX_DIR = Path(os.environ.get("XTMEM_WIKI_DIR", r"<XT_ROOT>/mimox-data/memories/xtmem-wiki"))
DERIVED = "wiki-entity"


def _store_via_cli(content, ns, source_ids, derived_from=DERIVED):
    """走 CLI store（血缘闸 + min_visibility 继承 + 幂等去重全复用）。"""
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = DEFAULT_DB
    env["XTMEM_ACTOR"] = ns or "dsh"          # 域闸：页面写进自己域（自己记自己的）
    env["PYTHONUTF8"] = "1"
    args = [DEFAULT_CLI, "store", "--agent", ns or "dsh", "--namespace", ns or "dsh",
            "--project", "", "--text", content, "--importance", "0.7",
            "--derived-from", derived_from, "--source-ids", ",".join(source_ids),
            "--tier", "refined", "--track", "default", "--tags", "wiki,xtmem"]
    try:
        proc = subprocess.run([PYTHON] + args, capture_output=True, text=True,
                              encoding="utf-8", timeout=180, env=env)
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _aliases(conn, canon):
    return [r[0] for r in conn.execute(
        "SELECT alias FROM entity_aliases WHERE canon=?", (canon,)).fetchall()]


def _df_stats(conn):
    """canon → (total_df, 域分布, 轨道分布)（以 chunk_entities 现况为准，防 entities.count 滞后）。"""
    rows = conn.execute(
        """SELECT ce.canon, ch.namespace, ch.track
           FROM chunk_entities ce JOIN chunks ch ON ch.id = ce.chunk_id
           WHERE ch.state='active'"""
    ).fetchall()
    df_total, ns_dist, tr_dist = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    for canon, ns, track in rows:
        df_total[canon] += 1
        ns_dist[canon][ns or "?"] += 1
        tr_dist[canon][track or "default"] += 1
    return df_total, ns_dist, tr_dist


def _pool_sources(conn, canon, ns_pool, pool):
    """(canon, 域, 池) 的源 chunk 列表：active + namespace + visibility 匹配（页内一切统计仅本池）。"""
    if pool == "shared":
        vis_cond = "ch.visibility='shared'"
    else:
        vis_cond = "ch.visibility!='shared'"
    return conn.execute(
        f"""SELECT ch.id, ch.content, ch.importance, ch.timestamp, ch.derived_from, ch.track
            FROM chunk_entities ce JOIN chunks ch ON ch.id = ce.chunk_id
            WHERE ce.canon=? AND ch.namespace=? AND ch.state='active' AND {vis_cond}
              AND COALESCE(ch.derived_from, '') = ''
            ORDER BY ch.importance DESC, ch.timestamp DESC""",
        (canon, ns_pool)).fetchall()


def _related_entities(conn, canon, ns_pool, pool, top_n=10):
    """池内共现 Top：与 canon 同现于本池 active chunk 的其他 canon（跨域/私图边不引用）。"""
    if pool == "shared":
        vis_cond = "ch.visibility='shared'"
    else:
        vis_cond = "ch.visibility!='shared'"
    rows = conn.execute(
        f"""SELECT ce2.canon AS other, COUNT(DISTINCT ce1.chunk_id) AS cnt
            FROM chunk_entities ce1
            JOIN chunk_entities ce2 ON ce1.chunk_id = ce2.chunk_id
                AND ce1.canon=? AND ce2.canon<>?
            JOIN chunks ch ON ch.id = ce1.chunk_id
            WHERE ch.namespace=? AND ch.state='active' AND {vis_cond}
              AND COALESCE(ch.derived_from, '') = ''
            GROUP BY ce2.canon ORDER BY cnt DESC LIMIT ?""",
        (canon, canon, ns_pool, top_n)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _related_distill(conn, canon, ns_pool, aliases, top_n=3):
    """挂 T6 蒸馏器产物：同域 distill 且正文含实体别名的，按时间取 TopN。"""
    if not aliases:
        return []
    ph = ",".join("?" * len(aliases))
    rows = conn.execute(
        f"""SELECT id, content, timestamp FROM chunks
            WHERE state='active' AND namespace=? AND derived_from='distill'
              AND ({" OR ".join("content LIKE '%'||?||'%'" for _ in aliases)})
            ORDER BY timestamp DESC LIMIT ?""",
        [ns_pool] + aliases + [top_n]).fetchall()
    return rows


def _page_content(canon, aliases, df_pool, track_str, sources, related, distills):
    """规则聚合正文（零 LLM；蒸馏 LLM 化后此函数换 LLM 分支，结构不变）。全部数字 = 本池口径。"""
    lines = [f"# 实体 · {canon}", ""]
    lines.append(f"- 别名: {' / '.join(aliases[:10]) or '—'}")
    lines.append(f"- 活跃文档数 (df): {df_pool}")
    if track_str:
        lines.append(f"- 轨道分布: {track_str}")
    if related:
        lines.append("- 关联实体 Top:")
        lines += [f"  - {o}（共现 {c}）" for o, c in related]
    if sources:
        lines.append("- 近因 Top3:")
        lines += [f"  - [{r['id'][:8]}] {str(r['content'])[:120]}" for r in sources[:3]]
    if distills:
        lines.append("- 相关蒸馏产物:")
        lines += [f"  - {r[0]} {str(r[1])[:120]}" for r in distills]
    lines.append(f"- 血缘源数: {len(sources)}（{', '.join(r['id'][:8] for r in sources[:12])}…）")
    return chr(10).join(lines)


def build_pages(conn, min_df=5, domain=None, rebuild=False):
    """遍历 (canon, 域, 池) 建页；返回结果清单。"""
    if rebuild:
        conn.execute("DELETE FROM chunks WHERE derived_from=?", (DERIVED,))
        conn.commit()
    df_total, _, _ = _df_stats(conn)  # 全局 df≥5 决定建页与否；页内数字一律池内口径
    canons = [c for c, n in sorted(df_total.items(), key=lambda x: -x[1]) if n >= min_df]
    results = []
    domains = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT namespace FROM chunks WHERE state='active'").fetchall()} | {""})
    for canon in canons:
        aliases = _aliases(conn, canon)
        for ns_pool in domains:
            if domain and ns_pool != domain:
                continue
            for pool in ("shared", "private"):
                sources = _pool_sources(conn, canon, ns_pool, pool)
                if not sources:
                    continue
                if not rebuild:
                    exists = conn.execute(
                        "SELECT 1 FROM chunks WHERE derived_from=? AND namespace=? AND "
                        "state='active' AND content LIKE ? LIMIT 1",
                        (DERIVED, ns_pool, f"%实体 · {canon}%")).fetchone()
                    if exists:
                        results.append({"canon": canon, "ns": ns_pool, "pool": pool, "skipped": "exists"})
                        continue
                related = _related_entities(conn, canon, ns_pool, pool)
                distills = _related_distill(conn, canon, ns_pool, aliases)
                track_cnt = defaultdict(int)
                for s in sources:
                    track_cnt[s["track"] or "default"] += 1
                track_str = " / ".join(
                    f"{k}:{v}" for k, v in sorted(track_cnt.items(), key=lambda x: -x[1])[:5])
                body = _page_content(canon, aliases, len(sources), track_str, sources, related, distills)
                r = _store_via_cli(body, ns_pool, [s["id"] for s in sources])
                results.append({"canon": canon, "ns": ns_pool, "pool": pool,
                                "ok": r.get("ok", False), "id": r.get("id"),
                                "skipped": r.get("skipped"),
                                "err": r.get("error") if not r.get("ok") else None,
                                "n_src": len(sources)})
    return results


def write_index(conn):
    """公共索引：只列共享池 wiki 页（直连 db 产公共文件自滤 visibility——信任边界）。"""
    rows = conn.execute(
        """SELECT id, content, namespace, derived_from, updated_at FROM chunks
           WHERE derived_from=? AND visibility='shared' AND state='active' ORDER BY content""",
        (DERIVED,)).fetchall()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# XTMEM 实体页索引（共享池）", "",
             "> 由 xtmem_wiki.py 生成 · 只列 visibility=shared 页；私域页不入公共索引。", ""]
    for r in rows:
        canon = r["content"].split(chr(10), 1)[0].replace("# 实体 · ", "").strip()
        lines.append(f"- {canon} · [{r['id']}] · ns={r['namespace'] or '—'} · {r['derived_from']}")
    (INDEX_DIR / "INDEX.md").write_text(chr(10).join(lines), encoding="utf-8")
    return len(rows)


def main():
    ap = argparse.ArgumentParser(prog="xtmem_wiki")
    ap.add_argument("--rebuild", action="store_true", help="先删本脚本产物再重建（刷新用）")
    ap.add_argument("--min-df", type=int, default=5)
    ap.add_argument("--domain", default=None)
    args = ap.parse_args()
    conn = sqlite3.connect(DEFAULT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    results = build_pages(conn, min_df=args.min_df, domain=args.domain, rebuild=args.rebuild)
    n_index = write_index(conn)
    conn.close()
    ok = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    print(json.dumps({"ok": True, "built": ok, "skipped": skipped, "index_pages": n_index,
                      "results": results[:60]}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
