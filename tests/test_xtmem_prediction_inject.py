# -*- coding: utf-8 -*-
"""test_xtmem_prediction_inject.py — 预判注入底座+增强守卫（步骤4/5，2026-09-06，assistant-c）。

守卫清单（主方案 §四 4-7 + 增强 §五）——共 7 个用例：
  ① test_prediction_top2_inject：【已反转·防复活】预判退役 → 注入恒空（原验 Top-2 注入，2026-09-15 单R1尾巴改）；
  ② test_prediction_single_degrades：【已反转·防复活】预判退役 → 注入恒空（原验 workbuddy 型退化单条，2026-09-15 单R1尾巴改）；
  ③ test_prediction_cluster_archive：同 task_type 连续 2 miss → 簇归档、异簇不动（R3/步骤5-⑤）；
  ④ test_prediction_hit_miss_meta：hit/miss 事件 meta 带 task_type（步骤4-③）；
  ⑤ test_prediction_gen_fuse：单域 60 active → 生成 0 + 失败事件（R5-3）；
  ⑥ test_prediction_cron_depends_distill：distill 无近期成功 → predict 跳过告警（R5-5）；
  ⑦ test_prediction_gen_degrades_single：workbuddy 型 <2 条/类 → 生成端退化 confidence=0.5（R2）。

⚠️ ①② 与 ③④⑤⑥⑦ 的性质分野（2026-09-15 单R1尾巴，maintainer复验判定）：
   ①② 验的是「**注入产出**」（provider._prediction_block_raw 返回值）——预判退役后该面恒空，
      故反转为防复活断言；
   ③④⑤⑥⑦ 验的是「**生成器/事件面**」（xtmem_predict._run_domain、CLI prediction hit/miss）——
      **生成器未废，仍有效，勿动**。
   本文件 2 红 5 绿的分野，正是"只切注入路径、没碰生成器"的证据（318 基线从 2 failed 恢复的来由）。

⚠️ R7：自证数据一律临时库（tmp_db 副本），禁用真实表。生产库零写。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

import conftest

CLI = conftest.CLI
PREDICT = _REPO / "scripts"/ "xtmem_predict.py"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _run_cli(cli_env, *argv):
    env = dict(cli_env)
    env.pop("XTMEM_ACTOR", None)
    env.pop("XTMEM_ADMIN", None)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def _seed_prediction(db, cid, ns, task_type, importance, overturn=0):
    """直插 prediction chunk（人造样本）。"""
    con = sqlite3.connect(str(db))
    tags = ["prediction", "task_type:" + task_type, "overturn:%d" % overturn]
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from) "
        "VALUES (?, ?, ?, ?, ?, 'prediction', 'active', ?, 'private', 'refined', 'prediction-engine')",
        (cid, "预判内容 " + cid, time.time(), importance, json.dumps(tags), ns))
    con.commit()
    con.close()


# ── ① Top-2 注入（provider._prediction_block）─────────────────────

def test_prediction_top2_inject(tmp_db):
    """【已反转·防复活】预判退役后：即便库里有分好级的多簇预判，注入也必须恒空。

    2026-09-15 单R1尾巴：预判 v3.2 定案彻底废（cron 已 pause + 注入路径已短路）。
    原断言「Top-2 注入 2 条」随之失效，本用例反转为断言退役生效。

    ⚠️ 造数【必须保留】：若把下面 4 条种子删了，断言空串会"因无数据而空"——
    将来有人摘掉退役短路（provider.__prediction_block_impl 的 return ""），
    本用例照样绿，防复活能力就废了。**有数据仍空**才是真判据。
    """
    sys.path.insert(0, _REPO / "src" / "hermes-plugin")
    import provider as P
    # 3 簇不同 confidence（dsh 域）——改前正是这批数据产出 2 行注入
    _seed_prediction(tmp_db, "mem-pa" + _uid(), "dsh", "audit", 0.9)
    _seed_prediction(tmp_db, "mem-pb" + _uid(), "dsh", "deploy", 0.7)
    _seed_prediction(tmp_db, "mem-pc" + _uid(), "dsh", "code", 0.5)
    # 跨域：zcode 域的高分条（dsh 视角绝不取）
    foreign_id = "mem-pf" + _uid()
    _seed_prediction(tmp_db, foreign_id, "zcode", "audit", 0.95)

    p = P.XtmemMemoryProvider()
    p._plugin_cfg = {"db_path": str(tmp_db)}
    p._namespace = "dsh"
    p._project = ""
    p._resolve_db_path = (lambda d=str(tmp_db): d)

    block = p._prediction_block_raw()
    # 本断言变红 = 有人把退役短路摘了（预判又往提示词里注入了）
    assert block == "", "预判已退役：注入必须恒空（红 = 有人恢复了预判）"


def test_prediction_single_degrades(tmp_db):
    """【已反转·防复活】预判退役后：单条退化路径同样不得注入（恒空）。

    2026-09-15 单R1尾巴：原断言「只 1 条退单条格式、1 行」，反转为断言退役生效。
    ⚠️ 造数同理保留——"有 1 条在库里仍空"才说明短路真的生效。
    """
    sys.path.insert(0, _REPO / "src" / "hermes-plugin")
    import provider as P
    _seed_prediction(tmp_db, "mem-p1" + _uid(), "workbuddy", "doc", 0.6)
    p = P.XtmemMemoryProvider()
    p._plugin_cfg = {"db_path": str(tmp_db)}
    p._namespace = "workbuddy"
    p._project = ""
    p._resolve_db_path = (lambda d=str(tmp_db): d)
    block = p._prediction_block_raw()
    # 本断言变红 = 有人把退役短路摘了（单条退化注入又活了）
    assert block == "", "预判已退役：注入必须恒空（红 = 有人恢复了预判）"



# ── ② 簇级归档（cmd_prediction miss 2 次）──────────────────────────

def test_prediction_cluster_archive(tmp_db, cli_env):
    """同 task_type 连续 2 miss → 该簇全归档，异簇不动（步骤5-⑤）。"""
    uid = _uid()
    a1, a2 = "mem-ca" + uid, "mem-cb" + uid
    b1 = "mem-oz" + uid
    _seed_prediction(tmp_db, a1, "dsh", "debug", 0.8)
    _seed_prediction(tmp_db, a2, "dsh", "debug", 0.7)
    _seed_prediction(tmp_db, b1, "dsh", "audit", 0.9)  # 异簇

    # 第一次 miss
    r1 = _run_cli(cli_env, "prediction", "miss", "--id", a1, "--as", "dsh")
    assert r1.get("ok"), r1
    # 第二次 miss（同簇另一条）
    r2 = _run_cli(cli_env, "prediction", "miss", "--id", a2, "--as", "dsh")
    assert r2.get("ok"), r2
    assert r2.get("cluster_archived") is True, r2

    # 簇内全归档、异簇仍 active
    con = sqlite3.connect(str(tmp_db))
    sa = con.execute("SELECT state FROM chunks WHERE id IN (?,?)", (a1, a2)).fetchall()
    sb = con.execute("SELECT state FROM chunks WHERE id=?", (b1,)).fetchone()
    con.close()
    assert all(s[0] == "archived" for s in sa), f"debug 簇应全归档，实际 {sa}"
    assert sb[0] == "active", f"audit 异簇不得受影响，实际 {sb[0]}"


def test_prediction_hit_miss_meta(tmp_db, cli_env):
    """hit/miss 事件 meta 带 task_type（步骤4-③）。"""
    uid = _uid()
    cid = "mem-hm" + uid
    _seed_prediction(tmp_db, cid, "dsh", "deploy", 0.8)
    r = _run_cli(cli_env, "prediction", "hit", "--id", cid, "--as", "dsh")
    assert r.get("ok") and r.get("task_type") == "deploy", r
    con = sqlite3.connect(str(tmp_db))
    row = con.execute(
        "SELECT meta FROM xtmem_events WHERE kind='prediction_hit' ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    meta = json.loads(row[0]) if row else {}
    assert meta.get("task_type") == "deploy", f"事件 meta 应带 task_type，实际 {meta}"


# ── ③ 生成熔断（R5-3）────────────────────────────────────────────

def test_prediction_gen_fuse(tmp_db):
    """单域 60 active → 生成 0 + prediction_gen_failed 事件（R5-3）。"""
    sys.path.insert(0, _REPO / "scripts")
    import xtmem_predict as pred
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    # 造 60 条 active prediction
    for i in range(60):
        con.execute(
            "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from) "
            "VALUES (?, ?, ?, 0.5, '[]', 'prediction', 'active', 'dsh', 'private', 'refined', 'prediction-engine')",
            (f"mem-fuse{i:03d}", "熔断测试", time.time()))
    con.commit()
    r = pred._run_domain(con, "dsh")
    assert r.get("fused") is True and r.get("generated") in (None, []), r
    ev = con.execute(
        "SELECT COUNT(*) FROM xtmem_events WHERE kind='prediction_gen_failed' AND namespace='dsh'").fetchone()[0]
    con.close()
    assert ev >= 1, "熔断必须写 prediction_gen_failed 事件"


# ── ④ cron 依赖 distill（R5-5）────────────────────────────────────

def test_prediction_cron_depends_distill(tmp_db):
    """distill 无近期成功事件（且历史有 distill 事件）→ predict 跳过+prediction_skipped。

    2026-09-06 整改总单-6 观测口修复后：成功面=distill_units.state=distilled 也放行。
    本用例要测"确实无近期成功"——必须把 canonical 副本里近 26h 的 distilled 记录清掉
    （生产最近蒸馏 19.3h 前，不清会误放行）。
    """
    sys.path.insert(0, _REPO / "scripts")
    import xtmem_predict as pred
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    # 清蒸馏成功面：distill_units 近期 distilled 置旧（模拟无近期成功）
    try:
        con.execute("UPDATE distill_units SET distilled_at=? WHERE state='distilled' AND distilled_at IS NOT NULL",
                    (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 48 * 3600)),))
    except sqlite3.OperationalError:
        pass  # 表不存在也无妨
    # 历史 distill 事件存在但都在 26h 之前 → 判定无近期成功
    old_ts = time.time() - 48 * 3600
    con.execute("INSERT INTO xtmem_events (ts, kind, agent, meta) VALUES (?, 'distill', 't', '{}')", (old_ts,))
    con.commit()
    r = pred._run_domain(con, "dsh")
    assert r.get("skipped") is True and "cron-dependency" in (r.get("reason") or ""), r
    ev = con.execute(
        "SELECT COUNT(*) FROM xtmem_events WHERE kind='prediction_skipped'").fetchone()[0]
    con.close()
    assert ev >= 1, "跳过须写 prediction_skipped 事件（告警面）"


# ── ⑤ 生成端退化单条（R2）────────────────────────────────────────

def test_prediction_gen_degrades_single(tmp_db, monkeypatch):
    """workbuddy 型：某类只 1 条要点 → confidence=0.5 退化单条（R2）。"""
    # ⚠️ 防写穿（错题本）：先设 env 再 reload predict——fallback 子进程与 serve 端口全部隔离到 tmp_db
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    monkeypatch.setenv("XTMEM_SERVE_PORT", "18799")  # 无 serve 的端口 → serve 分支快速失败回落
    sys.path.insert(0, _REPO / "scripts")
    import xtmem_predict as pred
    importlib.reload(pred)
    con = sqlite3.connect(str(tmp_db))
    con.row_factory = sqlite3.Row
    now = time.time()
    # workbuddy 域：1 条 audit 类要点（<2 退化）
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
        "VALUES (?, '审计偏好 先对账再动手', ?, 0.8, '[]', 'preference', 'active', 'workbuddy', 'private', 'refined')",
        ("mem-wb1", now))
    con.commit()
    # distill 依赖放行（空事件库 → no-distill-events-yet 放行）
    try:
        r = pred._run_domain(con, "workbuddy")
        assert r.get("ok"), r
        gens = r.get("generated") or []
        audit = [g for g in gens if g.get("task_type") == "audit"]
        assert audit and audit[0]["confidence"] == 0.5, f"单条退化 confidence=0.5，实际 {gens}"
    finally:
        con.close()
        monkeypatch.undo()
        importlib.reload(pred)  # 还原模块级 DEFAULT_DB（防泄漏给后续用例）