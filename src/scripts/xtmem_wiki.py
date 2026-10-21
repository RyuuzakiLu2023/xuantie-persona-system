# -*- coding: utf-8 -*-
"""xtmem_wiki.py — 实体页生成器（规则聚合 · v1）。

【这是什么】
把「同一实体在多条记忆里反复出现」这件事整理成一张实体页：
  - tier='refined' / derived_from='wiki-entity' / source_ids=源 chunk id
  - 按池分页：共享池（visibility='shared'）与私池（其余）各一页/（实体, 域），绝不混合聚合
  - 页内容：别名 / 活跃文档数 / 轨道分布 / 关联实体 Top / 近因 Top3 / 相关蒸馏产物 / 血缘摘要
  - 额外产出公共索引 INDEX.md（**只列共享页**——直连库产公共文件必须自滤 visibility）

【为什么要分池】
实体页是派生记忆，可见性由来源读者集合决定。把共享源与私有源混在一页里，
要么私有内容被聚合进共享页（越权），要么共享内容被私有页拖下水（失真）。
按池分页是唯一能同时保住两边语义的做法。

【幂等 / 回滚】
  - 同 (实体, 池, 域) 已有 active 页 → 跳过（不重蒸）
  - --rebuild：先删本脚本产物再重建（内容随库漂移，人工刷新用）
  - 回滚：DELETE FROM chunks WHERE derived_from='wiki-entity'

【公开化改动（相对运行正本）】
  - 库路径 / CLI 统一走 xtmem_common；索引目录默认 $XTMEM_HOME/wiki。
  - 域名单不写死：缺省遍历库里实际存在的 namespace。

【默认不生成（实验性）】
实体页涉及"按池分页 + 蒸馏产物入正文"的组合，历史上出现过私有正文混进共享页的
缺陷（已修：_related_distill 按池过滤、蒸馏产物 id 纳入 source_ids；本地合成测试覆盖）。
但跨域 shared-with 名单、真实实体抽取质量等仍未在生产形态验证，所以**默认拒绝实际生成**：
直接跑只会打印说明并以退出码 2 结束。确认要试，加 `--allow-experimental`。

用法：
  python xtmem_wiki.py --help                       # 帮助可用
  python xtmem_wiki.py --allow-experimental         # 真正生成（实验性）
  python xtmem_wiki.py --allow-experimental --rebuild --min-df 5 --domain <ns>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

# 公共索引落点（信任边界：只列共享页）。
# 默认放 $XTMEM_HOME/wiki（跟库同一个用户目录），不写死任何仓库/生产路径。
INDEX_DIR = Path(os.environ.get(
    "XTMEM_WIKI_DIR",
    str(Path(os.environ.get("XTMEM_HOME", "~/.xtmem")).expanduser() / "wiki"),
))
DERIVED = "wiki-entity"

# 库路径由 main() 解析一次后放这里：_store_via_cli 被多处调用，签名保持原样。
_DB_PATH = [None]


def _store_via_cli(content, ns, source_ids, derived_from=DERIVED):
    """走 CLI store（血缘闸 + 可见性继承 + 幂等去重全复用）。"""
    return common.store_via_cli(
        db_path=_DB_PATH[0] or common.resolve_db_path(),
        agent=ns or "default", namespace=ns or "default",
        text=content, importance=0.7, derived_from=derived_from,
        source_ids=source_ids, tier="refined", track="default",
        tags=["wiki", "xtmem"], project="",
    )


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


def _related_distill(conn, canon, ns_pool, aliases, pool, top_n=3):
    """挂蒸馏产物：同域 distill 且正文含实体别名，**且可见性属于本池**。

    ★为什么要按池过滤（安全）：实体页是按池分页的（共享页 / 私池页）。
      历史实现只按 namespace 取蒸馏产物，不判 visibility ⇒ **私有蒸馏正文会被拼进
      共享页正文**，而共享页的 source_ids 只登记了 sources（不含蒸馏产物）——
      等于私有内容借共享页出门、且血缘里查不到它。这是原料准入问题，不能靠事后过滤。
    """
    if not aliases:
        return []
    vis_cond = "visibility='shared'" if pool == "shared" else "visibility!='shared'"
    rows = conn.execute(
        f"""SELECT id, content, timestamp FROM chunks
            WHERE state='active' AND namespace=? AND derived_from='distill'
              AND {vis_cond}
              AND ({" OR ".join("content LIKE '%'||?||'%'" for _ in aliases)})
            ORDER BY timestamp DESC LIMIT ?""",
        [ns_pool] + aliases + [top_n]).fetchall()
    return rows


def _page_content(canon, aliases, df_pool, track_str, sources, related, distills, pool=""):
    """规则聚合正文（零 LLM）。全部数字 = 本池口径。

    ⚠️ `- 池:` 这一行是**幂等锚**：同实体同域要建两页（共享池 / 私池），
      没有池标记时"已存在"检查会把第二页误判成重复（实测：私池页永远 skip）。
    """
    lines = [f"# 实体 · {canon}", ""]
    if pool:
        lines.append(f"- 池: {pool}")
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
                    # ⚠️ 幂等锚必须带"池"：同实体同域有两页（shared/private）。
                    #   历史页没有 `- 池:` 行 ⇒ 按 legacy 归到共享池（不重复建、也不删除历史）。
                    if pool == "shared":
                        _pool_cond = ("(content LIKE '%池: shared%' "
                                      "OR content NOT LIKE '%池:%')")
                    else:
                        _pool_cond = "content LIKE '%池: private%'"
                    exists = conn.execute(
                        f"SELECT 1 FROM chunks WHERE derived_from=? AND namespace=? AND "
                        f"state='active' AND content LIKE ? AND {_pool_cond} LIMIT 1",
                        (DERIVED, ns_pool, f"%实体 · {canon}%")).fetchone()
                    if exists:
                        results.append({"canon": canon, "ns": ns_pool, "pool": pool, "skipped": "exists"})
                        continue
                related = _related_entities(conn, canon, ns_pool, pool)
                distills = _related_distill(conn, canon, ns_pool, aliases, pool)
                track_cnt = defaultdict(int)
                for s in sources:
                    track_cnt[s["track"] or "default"] += 1
                track_str = " / ".join(
                    f"{k}:{v}" for k, v in sorted(track_cnt.items(), key=lambda x: -x[1])[:5])
                body = _page_content(canon, aliases, len(sources), track_str,
                                     sources, related, distills, pool=pool)
                # source_ids 必须覆盖**正文里出现的每一种原料**：sources + 蒸馏产物。
                # 漏登记蒸馏产物 = 派生页继承不到它的可见性，也会让血缘查不到这段依据。
                src_ids = [s["id"] for s in sources] + [r["id"] for r in distills]
                r = _store_via_cli(body, ns_pool, src_ids)
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


EXPERIMENTAL_NOTE = (
    "xtmem_wiki 是实验性作业，默认不生成实体页。\n"
    "原因：按池分页 + 蒸馏产物入正文的组合尚未在真实部署形态下验证；\n"
    "      历史缺陷（私有蒸馏正文混进共享页）已在代码层修复并有合成测试，\n"
    "      但跨域共享名单/实体抽取质量等边界仍需你自己抽验。\n"
    "确认要试请加 --allow-experimental。"
)


def main():
    ap = argparse.ArgumentParser(prog="xtmem_wiki")
    ap.add_argument("--rebuild", action="store_true", help="先删本脚本产物再重建（刷新用）")
    ap.add_argument("--min-df", type=int, default=5)
    ap.add_argument("--domain", default=None)
    ap.add_argument("--db", default=None)
    # 已知风险默认不启：不加这个开关就只打印说明、不写库。
    ap.add_argument("--allow-experimental", action="store_true",
                    help="确认执行实验性实体页生成（会写库）")
    args = ap.parse_args()
    if not args.allow_experimental:
        print(EXPERIMENTAL_NOTE)
        return 2
    _DB_PATH[0] = common.resolve_db_path(args.db)
    conn = sqlite3.connect(_DB_PATH[0], timeout=30)
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
