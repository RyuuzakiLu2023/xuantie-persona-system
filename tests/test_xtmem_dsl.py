# -*- coding: utf-8 -*-
"""P5.0-e 修饰器 DSL 测试（2026-09-02，小D）。

覆盖：--role-valve / --mode exact / --mode associated + --rerank / 帮助文本新增参数 /
      --time-decay off 在 retriever 层的字节无扰 + 排序语义（单位测）。

所有 CLI 用例走后端子进程（xtmem.py），tmp_db 隔离；人造实体全部 zzz-* 前缀（守卫③）。
"""

import json
import sqlite3
import subprocess
import sys
import time

import pytest
import axis_predicates
import conftest

from memory.hybrid_retriever import HybridRetriever, MemoryDoc

CLI = conftest.CLI
pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用（tmp_db 拷贝源）")


def _run_cli(cli_env, *argv):
    env = dict(cli_env)
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def _seed_alias(db, canon, alias):
    con = sqlite3.connect(str(db))
    # canonical 副本可能早于 P4.9：先幂等迁移（与 CLI _ensure_schema 同源）
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(con)
    con.execute("INSERT OR IGNORE INTO entity_aliases(canon, alias) VALUES(?,?)", (canon, alias))
    con.commit()
    con.close()


def test_role_valve_filter(tmp_db, cli_env):
    hi = _run_cli(cli_env, "store", "--agent", "dsh", "--text", "DSL回归：玄铁偏好本地模型部署", "--importance", "0.95")
    lo = _run_cli(cli_env, "store", "--agent", "dsh", "--text", "DSL回归：今天天气不错", "--importance", "0.2")
    assert hi.get("ok") and lo.get("ok"), (hi, lo)
    r = _run_cli(cli_env, "recall", "--query", "DSL回归", "--cross", "--as", "dsh", "--role-valve", "0.8")
    assert r.get("ok"), r
    assert r.get("count", 0) >= 1
    assert all(it["importance"] >= 0.8 for it in r["items"]), "role-valve 必须只留 importance ≥ 0.8 的命中"
    assert r["modifiers"]["role_valve"] == 0.8


def test_mode_exact(tmp_db, cli_env):
    r0 = _run_cli(cli_env, "store", "--agent", "dsh", "--text", "精确栈回归串 zzzqzx 唯一标识")
    assert r0.get("ok"), r0
    r = _run_cli(cli_env, "recall", "--query", "zzzqzx", "--mode", "exact", "--cross", "--as", "dsh")
    assert r.get("ok"), r
    assert r.get("mode") == "exact", "exact 栈必须报告 mode=exact"
    assert r.get("count", 0) >= 1 and any("zzzqzx" in it["content"] for it in r["items"])


def _chain_setup(db):
    # 中文双字别名互不重叠：BM25/向量都无交叉命中，c3 只能经 A→B 共现被 associated 拉出（守卫③自证值）
    _seed_alias(db, "A", "甲甲")
    _seed_alias(db, "B", "乙乙")
    _seed_alias(db, "C", "丙丙")


def test_associated_and_rerank(tmp_db, cli_env):
    _chain_setup(tmp_db)
    ids = {}
    for label, text in [("c1", "甲甲 内容一"), ("c2", "甲甲 乙乙 内容二"), ("c3", "乙乙 丙丙 内容三")]:
        r = _run_cli(cli_env, "store", "--agent", "dsh", "--text", text)
        assert r.get("ok"), r
        ids[label] = r["id"]
    assert _run_cli(cli_env, "graph", "rebuild").get("ok"), "graph rebuild 必须成功（rerank/associated 数据面）"

    # associated：命中 alpha-aaa → 一跳扩展应带出仅含 beta-bbb/gamma-ccc 的 c3（A→B 链）
    ra = _run_cli(cli_env, "recall", "--query", "甲甲", "--mode", "associated", "--cross", "--as", "dsh", "--top", "10")
    assert ra.get("ok"), ra
    got = {it["id"] for it in ra["items"]}
    assert ids["c3"] in got, "associated 必须经 A→B 共现拉到 c3"
    assert any(it.get("associated") for it in ra["items"]), "扩展条目必须带 associated 标记"

    # rerank=1.0：纯传播分排序 → 种子实体 A 的文档 c1 应居首
    rr = _run_cli(cli_env, "recall", "--query", "甲甲", "--rerank", "1.0", "--cross", "--as", "dsh", "--top", "10")
    assert rr.get("ok"), rr
    assert rr["modifiers"]["reranked"] is True
    assert rr["items"][0]["id"] == ids["c1"], f"传播分排序：种子实体文档应居首，实际 {[it['id'] for it in rr['items']]}"


def test_help_includes_new_flags():
    p = subprocess.run([sys.executable, str(CLI), "recall", "--help"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    for flag in ("--mode", "--rerank", "--time-decay", "--role-valve"):
        assert flag in p.stdout, f"帮助文本缺 {flag}"


def test_time_decay_off_ordering():
    """time_decay=False 时 recency 项清零：TTL(Neural Paging) 用同一 timestamp 隔离，
    唯一变量 = 视角衰减钟（last_access）——旧钟文档在衰减开时受罚、关时不再受罚。"""
    now = time.time()
    old = MemoryDoc(id="td-old", content="time-decay 回归甲", timestamp=now,
                    importance=0.5, namespace="dsh", visibility="shared", tier="raw")
    new = MemoryDoc(id="td-new", content="time-decay 回归甲", timestamp=now,
                    importance=0.5, namespace="dsh", visibility="shared", tier="raw")
    rec = HybridRetriever()
    rec.index([old, new])

    def last_access(cid):
        return now - 200000 if cid == "td-old" else now

    r_on = [d.id for d, _ in rec.search("time-decay 回归", top_k=10, now=now, last_access_lookup=last_access)]
    r_off = [d.id for d, _ in rec.search("time-decay 回归", top_k=10, now=now,
                                         last_access_lookup=last_access, time_decay=False)]
    assert r_on[0] == "td-new", "默认（衰减开）视角钟旧文档应受罚"
    assert r_off[0] == "td-old", "衰减关（time_decay=False）旧钟文档不再被时间惩罚（同分稳定序）」"
    # 默认参数路径逐字节不变：不传 time_decay 与传 None 结果一致
    r_none = [d.id for d, _ in rec.search("time-decay 回归", top_k=10, now=now,
                                          last_access_lookup=last_access, time_decay=None)]
    assert r_none == r_on
