# -*- coding: utf-8 -*-
"""test_xtmem_p612_entity_gate.py — P6.12 实体成熟度四门专属守卫（W5，W 批，2026-09-05，assistant-c）。

波2 复核欠账：实体成熟度四门（xtmem_entity_maturity.py）至今无专属守卫测试文件。
本文件补上——四算式各一条断言（人造样本，不依赖热库数字——错题本 #22 热库漂移教训，
DoD 数字一律时点快照或人造样本；故用 empty_db + ensure_xtmem_schema 造干净库，绝不用
canonical 副本污染分母）。

  ① canon ≥50 下限：人造 50 个 canon（count>0）→ ok；49 → 不 ok；
  ② 覆盖率 = 挂载实体的 primary chunks 占比 ≥60%（派生不计分母）；
  ③ hub 占比（P7 重标口径：排除 viewer 自身+用户本人实体后）≤20%——人造畸变 hub 验证排除后达标；
  ④ 长尾 = 挂载 ≥5 条的实体数 ≥30。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from pathlib import Path

from memory.schema import ensure_xtmem_schema

SCRIPT = Path(_REPO / "scripts"/ "xtmem_entity_maturity.py")


def _run_script(db):
    """子进程跑实体成熟度脚本（隔离 env），返回 (exit_code, json)。

    Windows 中文管道三坑（AGENTS.md 附则五）：脚本 print ensure_ascii=False 的 JSON，
    子进程 stdout 默认 cp936 → 以 PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 锁子进程编码，
    测试侧 utf-8 读（否则 0xbf UnicodeDecodeError，out 落空）。"""
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = str(db)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db)],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=120, env=env)
    try:
        return p.returncode, json.loads(p.stdout.strip() or "{}")
    except Exception:
        return p.returncode, {"raw": (p.stdout or "")[:300], "err": (p.stderr or "")[:200]}


@pytest.fixture()
def clean_db(empty_db):
    """empty_db + 全 schema（chunks/chunk_entities/entities/xtmem_events）——人造干净库。

    chunks 基础表由 VectorStore._init_db 建（ensure_xtmem_schema 只增列/建附加表，
    假定 chunks 已存在）；先用 VectorStore 初始化全库 schema，再关掉用裸连接造样本。"""
    from memory.vector_store import VectorStore
    vs = VectorStore(db_path=str(empty_db))
    vs.close()
    con = sqlite3.connect(str(empty_db))
    con.row_factory = sqlite3.Row
    ensure_xtmem_schema(con)
    con.commit()
    con.close()
    return empty_db


def _seed_chunk(con, cid, canon_list, derived=False, ns="dsh", raw=True):
    """插一个 primary/派生 chunk + 挂 canon。"""
    df = "distill" if derived else ""
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier, derived_from) "
        "VALUES (?, '内容', 1700000000, 0.5, '[]', 'default', 'active', ?, 'shared', 'raw', ?)",
        (cid, ns, df),
    )
    for canon in canon_list:
        con.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES (?, ?)", (cid, canon))
        con.execute(
            "INSERT INTO entities (canon, count, first_seen, last_seen) "
            "VALUES (?, 1, 1700000000, 1700000000) ON CONFLICT(canon) DO UPDATE SET count=count+1",
            (canon,),
        )


def test_p612_gate_canon_floor_50(clean_db):
    """canon ≥50 下限：49 个 canon → 不过；补到 50 → 过。"""
    con = sqlite3.connect(str(clean_db))
    for i in range(49):
        con.execute("INSERT INTO entities (canon, count, first_seen, last_seen) VALUES (?, 1, 1.0, 1.0)",
                    (f"canon-{i:03d}",))
    con.commit(); con.close()
    code, out = _run_script(clean_db)
    assert code in (0, 1), out  # all_ok=False 时脚本退 1，属预期
    g = out.get("gates", {})
    assert g["canon_ge_50"]["got"] == 49 and g["canon_ge_50"]["ok"] is False, out

    con = sqlite3.connect(str(clean_db))
    con.execute("INSERT INTO entities (canon, count, first_seen, last_seen) VALUES ('canon-050', 1, 1.0, 1.0)")
    con.commit(); con.close()
    _, out2 = _run_script(clean_db)
    assert out2["gates"]["canon_ge_50"]["got"] == 50 and out2["gates"]["canon_ge_50"]["ok"] is True, out2


def test_p612_gate_coverage_60(clean_db):
    """覆盖率 = 挂载实体的 primary chunks 占比 ≥60%（派生 chunk 不计入分母）。"""
    con = sqlite3.connect(str(clean_db))
    # 10 个 primary：8 个挂实体、2 个不挂 → 覆盖率 80%（≥60 过）
    for i in range(8):
        _seed_chunk(con, f"mem-cov{i:02d}", ["用户A"])
    for i in range(8, 10):
        _seed_chunk(con, f"mem-cov{i:02d}", [])
    # 派生 chunk 不计入分母但挂了实体
    _seed_chunk(con, "mem-covder", ["assistant-a"], derived=True)
    con.commit(); con.close()
    _, out = _run_script(clean_db)
    cov = out["coverage_pct"]
    assert 75 <= cov <= 85, f"覆盖率应为 80%±5，实际 {cov}（派生挂实体不应稀释分母）"
    # 反例：把 8 挂实体的去掉 6 → 剩 2/10 = 20% 不过
    con = sqlite3.connect(str(clean_db))
    for i in range(6):
        con.execute("DELETE FROM chunk_entities WHERE chunk_id=?", (f"mem-cov{i:02d}",))
    con.commit(); con.close()
    _, out2 = _run_script(clean_db)
    assert out2["gates"]["coverage_ge_60"]["ok"] is False, out2


def test_p612_gate_hub_p7_exclusion(clean_db):
    """hub 占比 P7 重标口径：排除 viewer 自身实体 + 用户本人（用户A）后 ≤20%。

    人造畸变：30 个 primary 全挂「用户A」（用户本人）→ raw 口径 hub=100%，
    但 P7 排除后用户A被剔除 → top 回落普通实体 → 门过（防"用户被记得多"被打压）。"""
    con = sqlite3.connect(str(clean_db))
    for i in range(30):
        _seed_chunk(con, f"mem-hub{i:02d}", ["用户A", f"普通实体{i % 5}"])
    # 普通实体 0-4 各被 6 个 chunk 挂 → 排除后 top=普通实体*，pct=6/30=20%（边界达标）
    con.commit(); con.close()
    _, out = _run_script(clean_db)
    sens = out.get("hub_sensitivity", {})
    raw = sens.get("raw_原口径", {})
    hub_excl = sens.get("excl_viewer_user_P7重标口径", {})
    assert raw["top"] == "用户A", out
    assert hub_excl["top"] != "用户A", out
    assert hub_excl["pct"] <= 20, f"P7 排除后 hub 应 ≤20%，实际 {hub_excl}"


def test_p612_gate_self_prove_sensitivity(clean_db, tmp_path):
    """守卫自证（W5 波2 债）：故意调坏脚本阈值 → 断言必红——证明守卫真在守，
    不是恒绿摆设（阈值算式被改坏时测试必须能抓到）。

    做法：把 xtmem_entity_maturity.py 复制到临时目录，替换 coverage 阈值
    "coverage_pct >= 60" → "coverage_pct >= 160"（永远不过），跑坏副本 →
    断言 all_ok=False 且 coverage_ge_60.ok=False（守卫红=证明测试对脚本敏感）。
    人造样本：10 primary 8 挂实体 → 覆盖率 80%，好阈值过、坏阈值红。
    """
    import shutil
    con = sqlite3.connect(str(clean_db))
    for i in range(8):
        _seed_chunk(con, f"mem-sp{i:02d}", ["用户A"])
    for i in range(8, 10):
        _seed_chunk(con, f"mem-sp{i:02d}", [])
    con.commit(); con.close()
    # 好阈值副本先跑 → 过
    _, good = _run_script(clean_db)
    assert good["gates"]["coverage_ge_60"]["ok"] is True, "人造 80% 覆盖率好阈值应过"
    # 坏阈值副本 → 必红
    bad = tmp_path / "xtmem_entity_maturity_bad.py"
    shutil.copyfile(SCRIPT, bad)
    src = bad.read_text(encoding="utf-8")
    assert "coverage_pct >= 60" in src, "阈值算式定位失败（脚本已改？）"
    src = src.replace("coverage_pct >= 60", "coverage_pct >= 160")
    bad.write_text(src, encoding="utf-8")
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = str(clean_db)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("XTMEM_ACTOR", None)
    p = subprocess.run([sys.executable, str(bad), "--db", str(clean_db)],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=120, env=env)
    try:
        out_bad = json.loads(p.stdout.strip() or "{}")
    except Exception:
        pytest.fail("坏副本输出解析失败: " + (p.stdout or "")[:200])
    assert out_bad.get("all_ok") is False, "调坏阈值后 all_ok 必须 False（守卫自证红）"
    assert out_bad["gates"]["coverage_ge_60"]["ok"] is False, "coverage 门必须红（80% 实际 < 坏阈值 160）"
    # gate 标签是硬编码显示值（与算式分离）——校验算式行为已红即可，不断言标签数字


def test_p612_gate_tail_ge30(clean_db):
    """长尾 = 挂载 ≥5 条的实体数 ≥30。"""
    con = sqlite3.connect(str(clean_db))
    # 造 150 个 primary chunk（互不冲突 id），先给 60 个各挂一个 tail canon（各 1 挂，不达标）
    for i in range(60):
        _seed_chunk(con, f"mem-t{i:03d}", [f"tail-{i:03d}"])
    # 再 30 个实体各挂 5 个不同 chunk（挂在已存在的 mem-t 上，5 个 DISTINCT chunk/实体）
    for e in range(30):
        for c in range(5):
            cid = f"mem-t{(e * 5 + c) % 60:03d}"
            con.execute("INSERT INTO chunk_entities (chunk_id, canon) VALUES (?, ?)",
                        (cid, f"ge5-{e:03d}"))
            con.execute(
                "INSERT INTO entities (canon, count, first_seen, last_seen) "
                "VALUES (?, 1, 1.0, 1.0) ON CONFLICT(canon) DO UPDATE SET count=count+1",
                (f"ge5-{e:03d}",),
            )
    con.commit(); con.close()
    _, out = _run_script(clean_db)
    got = out["tail_ge5"]
    assert got >= 30, f"长尾应 ≥30（ge5-* 30 个各 5 挂），实际 {got}"
