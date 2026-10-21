# -*- coding: utf-8 -*-
"""发现30 食尾蛇守卫测试（2026-09-02，assistant-c）。

守住「派生产物不被打标/入图」这一判据修改（总则 10）：
  - 普通 chunk（derived_from 空）→ 被 NER 打标（chunk_entities 有行）
  - 派生 chunk（derived_from 非空）→ 不被打标（chunk_entities 无行），且不污染 tag_edges / wiki 池
"""

from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import sqlite3
import subprocess
import sys

import pytest
import axis_predicates
import conftest

from memory import graph as G
from memory import ner_tagger  # 引入但仅用于基准路径一致性；实际断言走库

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


def _q(db, sql, args=()):
    con = sqlite3.connect(str(db))
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def test_derived_not_tagged_or_in_graph(tmp_db, cli_env):
    # ① 普通 chunk（derived 空）→ 打标
    raw_r = _run_cli(cli_env, "store", "--agent", "dsh", "--text", "assistant-a 用户A 测试语料")
    assert raw_r.get("ok"), raw_r
    raw_id = raw_r["id"]
    assert _q(tmp_db, "SELECT 1 FROM chunk_entities WHERE chunk_id=?", (raw_id,)), "普通 chunk 必须被打标（基准）"

    # ② 派生 chunk（derived_from=wiki-entity）→ 不打标
    der_r = _run_cli(cli_env, "store", "--agent", "dsh", "--text", "assistant-a 用户A 派生页面正文",
                     "--derived-from", "wiki-entity", "--source-ids", raw_id, "--tier", "refined")
    assert der_r.get("ok"), der_r
    der_id = der_r["id"]
    assert not _q(tmp_db, "SELECT 1 FROM chunk_entities WHERE chunk_id=?", (der_id,)), "派生 chunk 不得被打标（发现30）"

    # ③ graph rebuild 排除派生：边只有普通 chunk 贡献（assistant-a~用户A = 1）
    assert G.rebuild_edges(db_path=str(tmp_db)), "rebuild 应成功"
    row = _q(tmp_db, "SELECT cnt FROM tag_edges WHERE a='assistant-a' AND b='用户A'")
    assert row and row[0][0] == 1, f"派生页不得灌边权，assistant-a~用户A应=1，实际 {row}"

    # ④ wiki _pool_sources 排除派生：assistant-a共享池只含 raw_id
    import sys as _s
    _s.path.insert(0, _REPO / "scripts")
    import xtmem_wiki as W
    con = sqlite3.connect(str(tmp_db)); con.row_factory = sqlite3.Row
    srcs = W._pool_sources(con, "assistant-a", "dsh", "shared")
    con.close()
    ids = {r[0] for r in srcs}
    assert raw_id in ids and der_id not in ids, f"wiki 池须排除派生页，实际 {ids}"
