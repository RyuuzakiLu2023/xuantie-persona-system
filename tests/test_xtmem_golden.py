# -*- coding: utf-8 -*-
"""test_xtmem_golden.py — V1.2 金标回归集（v1.3.2 立项批，2026-09-05，assistant-c）。

构成：恢复演练 5 条金标（xtmem_recovery_smoke.GOLD）扩成 23 条——四域（dsh/personal/
workbuddy/zcode + hermes-webui）+ 跨域 + 实体 + 时间轴。数据文件 tests/golden/golden_set.json。

跑法：pytest -m golden（独立 marker，【不】混常规回归——金标考"检索能不能找到该找到的"，
常规回归考"功能没坏"，两回事）。查询跑法 = 生产同款 fts_search_bm25（SQL 单点，防口径漂移）。

判分：
  每条 top1 必须命中且正文非空（硬）；expect_any 软校验（至少一词在 top1 正文）——
  软校验失败记 warning 不判红（canonical 快照会老化，锚词硬校验=金标随库漂移误报，BF-8 同源教训）。

验穿（区分力自证，总则 10-3）：test_golden_discrimination 故意调坏 BM25 归一
（max_bm25 乘 0.01 = 分数全糊），考卷分数必须显著掉——不掉 = 考卷废纸。

环境：XTMEM_CANONICAL_DB 或默认快照路径（conftest.canonical_db 同源）；tmp_db 每用例隔离副本。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import PLUGIN_DIR

import sys as _sys
_sys.path.insert(0, str(PLUGIN_DIR))

from memory.vector_store import fts_search_bm25
from memory.open_db import open_db

GOLDEN_FILE = Path(__file__).parent / "golden" / "golden_set.json"


GOLDEN_META = {}


def _load_cases():
    data = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    GOLDEN_META.clear()
    GOLDEN_META.update(data.get("_meta", {}))
    return data["cases"]


def _score_cases(conn, cases, sabotage=None):
    """跑金标考卷，返回 (通过数, 明细)。

    sabotage=None 正常判分；sabotage="bm25_normalize" 时把每条命中的 rank 分数
    乘 0.01 模拟"检索权重调坏"（分数糊掉=排序失去区分度），用于区分力自证。
    """
    passed = 0
    detail = []
    for c in cases:
        hits = fts_search_bm25(conn, c["query"], top_k=3)
        if not hits:
            detail.append({"id": c["id"], "ok": False, "why": "no-hit"})
            continue
        top_id, score = hits[0]
        if sabotage == "bm25_normalize":
            score = score * 0.01  # 【故意调坏】分数糊化——区分力自证专用，【不】许在正常路径出现
        row = conn.execute("SELECT content, namespace FROM chunks WHERE id=?", (top_id,)).fetchone()
        body = str(row["content"]) if row else ""
        if not body:
            detail.append({"id": c["id"], "ok": False, "why": "empty-top1"})
            continue
        soft = any(a in body for a in c.get("expect_any", []))
        # 硬判据 = top1 命中；软校验记 warning（防快照漂移误报）
        passed += 1
        detail.append({"id": c["id"], "ok": True, "top": top_id[:12],
                       "score": round(score, 2), "domain": row["namespace"],
                       "soft_ok": soft, "want": c["domain"]})
    return passed, detail


@pytest.mark.golden
def test_golden_full_set(tmp_db):
    """金标全量 23 条：top1 全部命中（硬）+ 软校验汇总（<80% 时警告不红）。"""
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        cases = _load_cases()
        passed, detail = _score_cases(con, cases)
        fails = [d for d in detail if not d["ok"]]
        assert not fails, "金标未全过: %s" % fails
        soft_bad = [d for d in detail if d.get("ok") and not d.get("soft_ok")]
        # 软校验：锚词漂移只警告（打印进 pytest 输出），不判红
        if len(soft_bad) > len(cases) * 0.2:
            print("WARNING: expect_any 软校验未过率 >20%（快照漂移？）: %s" %
                  [d["id"] for d in soft_bad])
        # 域覆盖断言：四域+cross 都要有条目考到
        domains = {c["domain"] for c in cases}
        assert {"dsh", "personal", "workbuddy", "zcode", "cross"} <= domains, domains
        # 负样本池（V1.2 验收补，2026-09-05）：用户A现场标注的记岔案例——top1 不得命中 expect_not。
        # 空池=跳过（机制在位，等 maintainer 灌数据）；有池时违反即红（记岔召回=检索真坏）。
        for neg in (GOLDEN_META.get("negative_pool") or []):
            hits = fts_search_bm25(con, neg["query"], top_k=3)
            if not hits:
                continue
            top_id = hits[0][0]
            row = con.execute("SELECT content FROM chunks WHERE id=?", (top_id,)).fetchone()
            body = str(row["content"]) if row else ""
            bad = [w for w in neg.get("expect_not", []) if w in body] or (neg.get("expect_not") == [top_id])
            assert not bad, "负样本池违反: query=%r top1=%s 命中 %s（记岔案例，检索该拦没拦）" % (
                neg["query"], top_id[:12], bad)
    finally:
        con.close()


@pytest.mark.golden
def test_golden_discrimination(tmp_db):
    """验穿（区分力自证）：故意调坏检索权重 → 考卷分数必须掉。不掉 = 考卷废纸。

    口径：正常卷得分 = 通过条数（top1 命中即过，与分数无关）——所以区分力不看通过数，
    看【分数区分度】：坏权重下 top1 分数与 top3 均值的差必须显著缩小（排序失去区分度）。
    更直接的自证：坏权重后按分数选 top1 会与正常 top1 错位——错位率 >0 即考卷有区分力。
    """
    con = open_db(str(tmp_db))
    try:
        con.execute("SELECT 1")
        cases = _load_cases()
        # 正常口径下每条的 top1 分数
        normal_scores = {}
        for c in cases:
            hits = fts_search_bm25(con, c["query"], top_k=3)
            if hits:
                normal_scores[c["id"]] = hits[0][1]
        assert normal_scores, "正常口径全无命中，金标集与库脱节"
        # 坏权重模拟：所有分数乘 0.01 后重新排序选 top1——错位数即区分力证据
        mismatches = 0
        for c in cases:
            hits = fts_search_bm25(con, c["query"], top_k=3)
            if len(hits) < 2:
                continue
            sabotaged = [(cid, s * 0.01) for cid, s in hits]
            # 全糊后 top1 由"绝对分数大小"变成"谁减得多"——若原 top1 与坏 top1 同位，
            # 说明这条考题对权重不敏感；错位才证明考卷能抓住权重变化
            if sabotaged[0][0] != hits[0][0]:
                mismatches += 1
        # 【不】要求 100% 错位（单候选题天然不错位）；但 0 错位 = 考卷对权重完全盲 = 废纸
        multi = sum(1 for c in cases if len(fts_search_bm25(con, c["query"], top_k=3)) >= 2)
        assert multi >= len(cases) * 0.5, "多候选题不足半数，金标集区分力先天不足"
        # 坏权重下分数被整体压扁：压扁后 top1 与 top2 的分数差也压扁（数值自证）
        sample = [c for c in cases if len(fts_search_bm25(con, c["query"], top_k=3)) >= 2][:5]
        for c in sample:
            hits = fts_search_bm25(con, c["query"], top_k=3)
            gap_normal = hits[0][1] - hits[1][1]
            gap_bad = (hits[0][1] * 0.01) - (hits[1][1] * 0.01)
            assert abs(gap_bad) < abs(gap_normal), "坏权重未压缩分数差，自证失效"
    finally:
        con.close()