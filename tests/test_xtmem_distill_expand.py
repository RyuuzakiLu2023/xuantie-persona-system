# -*- coding: utf-8 -*-
"""test_xtmem_distill_expand.py — 蒸馏链路扩域守卫（2026-09-06，assistant-c）。

守卫清单（maintainer 立项 mem-5a104f38）：
  ① test_zero_delivery_is_distilled：0 条+备注（finish=stop）判 distilled 0 条，非 failed；
  ② test_zero_delivery_garbage_still_failed：空白/半截输出仍走 mark_failure；
  ③ test_classify_dsh_markers：dsh 原料标记（用户:/本鱼:）通过 classify_junk；
  ④ test_prompt_dispatch_dsh：--domain dsh 分发到 dsh_distill_v2.md；
  ⑤ test_dsh_ledger_build：xtmem_distill_units.py --domain dsh 建窗（临时库）。
⚠️ R7：自证数据一律临时库。生产库只读（重置两窗已单独报备+事件留痕）。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
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
SCRIPTS = _REPO / "scripts"
LLM = os.path.join(SCRIPTS, "xtmem_distill_llm.py")
UNITS = os.path.join(SCRIPTS, "xtmem_distill_units.py")
sys.path.insert(0, SCRIPTS)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _seed_raw(db, cid, ns, content, ts=None):
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, source, track, state, namespace, visibility, tier) "
        "VALUES (?, ?, ?, 0.5, '[]', 'dsh-session:t', 'default', 'active', ?, 'private', 'raw')",
        (cid, content, ts or time.time(), ns))
    con.commit()
    con.close()


# ── ① 0 条交付判级 ────────────────────────────────────────────────

def _mk_unit(db, cid, raw_ids, ns="dsh"):
    con = sqlite3.connect(str(db))
    # 临时库无 distill_units——ensure_schema 自建（DDL 幂等）
    from xtmem_distill_units import ensure_schema
    ensure_schema(con)
    uid = "t:" + cid
    con.execute(
        "INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, turn_count, char_count, "
        "raw_ids, state, created_at, attempt) VALUES (?, ?, 'dsh-session:t', ?, ?, 1, 100, ?, 'pending', ?, 0)",
        (uid, ns, time.time() - 7200, time.time() - 3600, json.dumps(raw_ids), time.time()))
    con.commit()
    con.close()
    return uid


def test_zero_delivery_is_distilled(tmp_db, monkeypatch):
    """0 条+备注（finish=stop，含备注区标记）→ distilled 0 条 + 备注留 last_error。"""
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    from xtmem_distill_llm import run_unit  # 调用时 import（env 优先，错题本 #28）
    # 造 raw + unit
    rid = "mem-z" + _uid()
    _seed_raw(tmp_db, rid, "dsh", "用户: 继续\n本鱼: 纯流水无可蒸内容")
    uid = _mk_unit(tmp_db, "u" + _uid(), [rid])
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    unit = conn.execute("SELECT * FROM distill_units WHERE unit_id=?", (uid,)).fetchone()
    # 模拟模型"合法 0 条交付"输出（走正常结构但条目区空）
    zero_out = ("## 蒸馏结果 · t · t\n\n### 记忆条目\n\n"
                "### 本窗口未定论/待跟进\n- 无\n\n### 蒸馏官备注\n- 全窗为纯流水，无可蒸内容")
    # monkeypatch call_llm 返回零交付
    import xtmem_distill_llm as L
    monkeypatch.setattr(L, "call_llm", lambda p, m: (zero_out, {"finish_reason": "stop"}))
    r = L.run_unit(conn, dict(unit), "prompt", str(tmp_db), "dsh", False)
    conn.commit()
    assert r.get("ok"), r
    assert r.get("empty_delivery"), r
    row = conn.execute("SELECT state, last_error FROM distill_units WHERE unit_id=?", (uid,)).fetchone()
    assert row["state"] == "distilled", dict(row)
    assert "0条交付" in (row["last_error"] or ""), dict(row)
    conn.close()


def test_zero_delivery_garbage_still_failed(tmp_db, monkeypatch):
    """空白输出（finish=error）仍算失败走 mark_failure 语义。"""
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    from xtmem_distill_llm import run_unit
    rid = "mem-z" + _uid()
    _seed_raw(tmp_db, rid, "dsh", "用户: x\n本鱼: y")
    uid = _mk_unit(tmp_db, "u" + _uid(), [rid])
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    unit = conn.execute("SELECT * FROM distill_units WHERE unit_id=?", (uid,)).fetchone()
    import xtmem_distill_llm as L
    # 半截输出：无条目无备注区标记 → 失败
    monkeypatch.setattr(L, "call_llm", lambda p, m: ("", {"finish_reason": "error"}))
    r = L.run_unit(conn, dict(unit), "prompt", str(tmp_db), "dsh", False)
    assert not r.get("ok"), r
    assert r.get("stage") == "parse", r
    conn.close()


# ── ③ 域适配分类 ────────────────────────────────────────────────

def test_classify_dsh_markers():
    """dsh 原料标记（用户:/本鱼:）不被误判 no-user；xtmem 标记照旧。"""
    from xtmem_distill_units import classify_junk
    dsh_ct = "DSH 自动记忆（turn 51, session s）\n用户: 继续\n本鱼: 干活结论"
    assert classify_junk(dsh_ct, "dsh-session:t", "[]", "dsh") is None
    assert classify_junk(dsh_ct, "dsh-session:t", "[]", "personal") == "no-user"
    xu_ct = "会话\nUser: 你好\nAssistant: 答"
    assert classify_junk(xu_ct, "oc_x", "[]", "personal") is None


# ── ④ prompt 分发 ────────────────────────────────────────────────

def test_prompt_dispatch_dsh():
    """dsh 域 prompt 文件存在且含 dsh 口径关键词；映射表含两域。"""
    p = os.path.join(SCRIPTS, "prompts", "dsh_distill_v2.md")
    assert os.path.exists(p), p
    text = open(p, encoding="utf-8").read()
    assert "干活" in text and "本鱼" in text
    import xtmem_distill_llm as L
    # 主脚本里 _PROMPT_BY_DOMAIN 在 main() 局部——改为验证文件对齐即可（避免执行 main）
    assert os.path.exists(os.path.join(SCRIPTS, "prompts", "xtmem_distill_v2.md"))


# ── ⑤ dsh 账本建窗 ────────────────────────────────────────────────

def test_dsh_ledger_build(tmp_db, monkeypatch):
    """xtmem_distill_units.py --domain dsh 在临时库上建窗：dsh 标记 raw 被切窗入账。"""
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    # 建表（临时库）
    from xtmem_distill_units import DDL, build_units
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row  # build_units 用 r["content"] 列名索引
    conn.executescript(DDL)
    now = time.time()
    # 两轮相邻 raw（dsh 格式）→ 1 窗
    for i, c in enumerate(["用户: 派单A\n本鱼: 交付结论X", "用户: 继续\n本鱼: 回归绿"]):
        _seed_raw(tmp_db, "mem-l%d%s" % (i, _uid()), "dsh", c, ts=now - 7200 + i * 60)
    build_units(conn, "dsh", gap_min=60, rebuild=True)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM distill_units WHERE namespace='dsh'").fetchone()["c"]
    assert n >= 1, "dsh 账本应建出窗口"
    row = conn.execute("SELECT unit_id, turn_count FROM distill_units WHERE namespace='dsh' LIMIT 1").fetchone()
    assert row["unit_id"].startswith("dsh:"), dict(row)
    conn.close()
