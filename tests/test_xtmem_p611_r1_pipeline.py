# -*- coding: utf-8 -*-
"""test_xtmem_p611_r1_pipeline.py — R1 评估管线守卫（P7-B 纯减法版，2026-09-11，assistant-c）。

【P7-B 更新（判据层重写）】
  原"四刀"里有两刀断言的正是被删掉的错判据，本版处理如下：
    · test_harmful_counts_by_verdict —— 【删除】。它断言"两次兜底=2 有害注入"，
      是给病灶背书的实证（raw 回落被当有害注入）。判据没了，测它没有意义，
      留着只会让后人以为"兜底=有害"是个契约。
    · test_arm_alternates —— 【删除】。arm_for() 已删（arm 按枚举序号事后贴标，
      与检索行为零关联），函数不存在了，测试无法保留。
  新增：corrected 归因判据守卫（本节 §新1~§新4），含 24h 窗口的行为断言——
  反向验证靠它：把窗口改成 0h，§新3 必红（证明守卫真在测新判据）。
"""
import json
import os
import sqlite3
import sys as _sys
import time

import pytest

from conftest import PLUGIN_DIR

_sys.path.insert(0, str(PLUGIN_DIR))
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))

import xtmem_r1_pipeline as r1
from memory.open_db import open_db


def test_f3_gate_blocks_short_sessions():
    short = {"agent": "dsh", "verdicts": ["高置信（refined 首跳命中）"] * 3}
    g = r1.f3_gate(short)
    assert g["valid"] is False  # 3 轮 < 5
    ok = r1.f3_gate({"agent": "dsh", "verdicts": ["高置信（refined 首跳命中）"] * 5})
    assert ok["valid"] is True
    xuanti = r1.f3_gate({"agent": "personal", "verdicts": ["高置信（refined 首跳命中）"] * 9})
    assert xuanti["valid"] is False  # 样本域外（双保险）


def test_old_judgements_are_gone():
    """P7-B 纯减法：三件必须真的删干净，防"删一半留半"复活。"""
    assert not hasattr(r1, "arm_for"), "arm_for 必须已删（枚举序号贴标，与检索行为无关）"
    assert not hasattr(r1, "harmful_counts"), "harmful_counts 必须已删（raw 回落≠有害注入）"
    assert not hasattr(r1, "MAINTAINERS"), "MAINTAINERS 死代码必须已删"
    assert not hasattr(r1, "STOP_RULE_N"), "STOP_RULE_N n=100 胜负线必须已删"
    # 源码级零残留。⚠️ 首版这里翻过车：只按"行首是 #"剥注释，结果把【模块 docstring
    #   里描述历史的那句 "arm_for() / arm 字段 ... 已删"也算进代码】——正则在文档上
    #   误报。精确做法=剥掉全部注释与字符串后只扫可执行代码（下走 tokenize）。
    import io, tokenize
    src = open(r1.__file__, encoding="utf-8").read()
    code_parts = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT,):
                continue
            if tok.type == tokenize.STRING:
                continue   # 字符串（含 docstring）一律不参与判据——它们是对历史的说明
            code_parts.append(tok.string)
        code = " ".join(code_parts)
    except Exception:
        code = src   # tokenize 失败时退回全文（宁可误报，不可漏报）
    for gone in ("arm_for", "harmful_counts", "arm_layer", "MAINTAINERS", "STOP_RULE_N"):
        assert gone not in code, "%s 在可执行代码里有残留（注释/文档提及不算）" % gone


def _mk_events(con, rows):
    for ts, kind, agent, meta, target in rows:
        con.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, meta, target_chunk) VALUES (?,?,?,?,?,?)",
            (ts, kind, agent, agent, meta, target))
    con.commit()


def test_corrected_attribution_window(tmp_db):
    """corrected 归因：24h 窗口内算一次，窗口外不算（P7-B 主判据）。"""
    con = open_db(str(tmp_db))
    try:
        now = time.time()
        # 1) 注入 mem-A 后 1h 被 corrected → 应归因
        # 2) 注入 mem-B 后 30h 被 corrected → 超窗，不归因
        _mk_events(con, [
            (now - 7200, "recall", "dsh", json.dumps({"ids": ["mem-A"], "verdict": "高置信（refined 首跳命中）"}, ensure_ascii=False), None),
            (now - 3600, "corrected", "dsh", '{"via": "user-verbatim"}', "mem-A"),
            (now - 120000, "recall", "dsh", json.dumps({"ids": ["mem-B"], "verdict": "高置信（refined 首跳命中）"}, ensure_ascii=False), None),
            (now - 12000, "corrected", "dsh", '{"via": "user-verbatim"}', "mem-B"),
        ])
        r = r1.corrected_attribution(con, domains={"dsh"}, window_days=7.0)
        corr = r["corrected"]
        assert corr["signal_count"] == 2, corr
        assert corr["attributed"] == 1, "只有 1h 内那条应归因，30h 那条超窗不得归因: %s" % corr
        assert r["injected_chunk_exposures"] >= 2
    finally:
        con.close()


def test_corrected_attribution_zero_in_sample_domain(tmp_db):
    """样本域无信号时如实回 0（不是报错、不是编数）——工单 §2.2 的合格输出。"""
    con = open_db(str(tmp_db))
    try:
        now = time.time()
        _mk_events(con, [
            (now - 100, "recall", "dsh", json.dumps({"ids": ["mem-X"]}, ensure_ascii=False), None),
        ])
        r = r1.corrected_attribution(con, domains={"dsh"}, window_days=1.0)
        assert r["corrected"]["signal_count"] == 0
        assert r["corrected"]["attributed"] == 0
        assert r["confirmed"]["signal_count"] == 0
        s = r1.domain_signal_scan(con)
        for k in ("corrected", "confirmed", "session_end"):
            assert k in s, "domain_signal_scan 必须含 %s 行" % k
            assert "in_sample_domain" in s[k]
    finally:
        con.close()


def test_cross_agent_attribution_is_not_counted(tmp_db):
    """归因必须同 agent：别的域的 corrected 不得算到本域头上（防跨域串味）。"""
    con = open_db(str(tmp_db))
    try:
        now = time.time()
        _mk_events(con, [
            (now - 7200, "recall", "dsh", json.dumps({"ids": ["mem-C"]}, ensure_ascii=False), None),
            (now - 3600, "corrected", "zcode", '{"via": "user-verbatim"}', "mem-C"),
        ])
        r = r1.corrected_attribution(con, domains={"dsh", "zcode"}, window_days=7.0)
        # zcode 的 corrected 指向 mem-C，但注入是 dsh 做的 → 不得归因
        assert r["corrected"]["attributed"] == 0, r["corrected"]
    finally:
        con.close()


def test_pipeline_dry_run(tmp_db, monkeypatch):
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    con = open_db(str(tmp_db))
    try:
        now = time.time()
        for i in range(6):
            con.execute(
                "INSERT INTO xtmem_events (ts, kind, agent, namespace, meta) VALUES (?, 'recall', 'dsh', 'dsh', ?)",
                (now - i * 60, json.dumps({"verdict": "高置信（refined 首跳命中）", "ids": ["mem-Z%d" % i]}, ensure_ascii=False)),
            )
        con.commit()
        sessions = r1.fetch_sessions(con, 1.0, ["dsh"])
        assert len(sessions) >= 1
        r1.ensure_eval_table(con)
        n0 = con.execute("SELECT COUNT(*) FROM xtmem_r1_eval").fetchone()[0]
        sess = sessions[0]
        gate = r1.f3_gate(sess)
        con.execute(
            "INSERT INTO xtmem_r1_eval (ts, session_id, agent, arm, user_turns, valid, dry_run) VALUES (?,?,?,?,?,?,1)",
            (time.time(), sess["session_id"], sess["agent"], "-", gate["user_turns"], int(gate["valid"])),
        )
        con.commit()
        n1 = con.execute("SELECT COUNT(*) FROM xtmem_r1_eval").fetchone()[0]
        assert n1 == n0 + 1
        flag = con.execute("SELECT dry_run FROM xtmem_r1_eval ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert flag == 1
    finally:
        con.close()


def test_f3_gate_behavioral_observer_discrimination():
    observer_sess = {
        "agent": "dsh",
        "verdicts": [
            "lookup by id",
            "lookup by id",
            "lookup by id",
            "lookup by id",
            "refined",
        ]
    }
    g_obs = r1.f3_gate(observer_sess)
    assert g_obs["gates"]["no_selftest"] is False
    assert g_obs["has_selftest"] == 1
    assert g_obs["valid"] is False

    work_sess = {
        "agent": "zcode",
        "verdicts": ["refined"] * 5
    }
    g_work = r1.f3_gate(work_sess)
    assert g_work["gates"]["no_selftest"] is True
    assert g_work["has_selftest"] == 0
    assert g_work["valid"] is True

    dsh_work = {
        "agent": "dsh",
        "verdicts": ["refined"] * 5
    }
    g_dsh = r1.f3_gate(dsh_work)
    assert g_dsh["gates"]["no_selftest"] is True
    assert g_dsh["valid"] is True
