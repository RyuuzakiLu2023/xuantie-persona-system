# -*- coding: utf-8 -*-
"""xtmem_prune_wiki_raw_cards.py — bge-m3 历史污染清理（2026-09-05，assistant-c,用户A放行）。

背景：09-02 13:0x wiki 首跑实体卡经 wal-replay/absorb 链错误落 raw 层（tier=raw、df 空），
全库 103 张——蒸馏器把它们当流水聚进蒸馏产物（bge-m3 混入案）、raw 兜底检索重复注入。
refined 正版（df=wiki-entity）健在，raw 版纯冗余。

处置（maintainer派单口径）：
  条件 tier=raw AND host_origin=wal-replay AND content LIKE '# 实体%' AND state=active
  → superseded，superseded_by = 同域同名 refined 正版 id（content 前 14 字符匹配）；
  无正版可指 → archived（不悬空 supersede），留内容可追溯；
  全程标准写链 open_db + 事件 kind='wiki-raw-prune'（meta 含逐条映射摘要）。

用法：python xtmem_prune_wiki_raw_cards.py [--db <路径>] [--dry-run]
幂等：只动 state=active 命中行，重跑第二遍命中 0。
"""
import argparse
import json
import os
import sys
import time

try:  # 包内与同目录直跑两态：插件根统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

common.ensure_importable()
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

DEFAULT_DB = common.resolve_db_path()


def main() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_prune_wiki_raw_cards")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = open_db(args.db, timeout=60)
    try:
        rows = conn.execute(
            "SELECT id, namespace, content FROM chunks WHERE tier='raw' AND host_origin='wal-replay' "
            "AND content LIKE '# 实体%' AND state='active'"
        ).fetchall()
        sup = []   # (id, namespace, refined_id)
        arc = []   # (id, namespace)
        for r in rows:
            head = (r["content"] or "")[:14]
            ref = conn.execute(
                "SELECT id FROM chunks WHERE namespace=? AND tier='refined' AND state='active' AND content LIKE ? LIMIT 1",
                (r["namespace"], head + "%"),
            ).fetchone()
            if ref:
                sup.append((r["id"], r["namespace"], ref["id"]))
            else:
                arc.append((r["id"], r["namespace"]))
        if args.dry_run:
            print(json.dumps({"dry_run": True, "hits": len(rows), "to_supersede": len(sup),
                              "to_archive": len(arc)}, ensure_ascii=False))
            return 0
        now = time.time()
        # D1-1 收编（2026-09-09）：supersede 走 mark_superseded（reason/by/retired_at 三列 +
        # FTS/embedding 联动）。注意口径变化：原实现顺手写 updated_at，现按列语义只写 retired_at，
        # updated_at 不再被退役动作刷新（退役不该伪装成内容更新，否则 TTL/recency 会被误导）。
        _vs = _vecstore()
        for cid, ns, rid in sup:
            _vs.mark_superseded(cid, successor_id=rid, reason="wiki-raw-prune", by="dsh-zcode-20260905")
        for cid, ns in arc:
            conn.execute(
                "UPDATE chunks SET state='archived', updated_at=? WHERE id=?",
                (now, cid),
            )
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'wiki-raw-prune', ?)",
            (now, json.dumps({"hits": len(rows), "superseded": len(sup), "archived": len(arc),
                              "by": "dsh", "dispatcher": "zcode-20260905"}),),
        )
        conn.commit()
        print(json.dumps({"ok": True, "hits": len(rows), "superseded": len(sup),
                          "archived_no_refined": len(arc)}, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())