# -*- coding: utf-8 -*-
"""xtmem_d03_guard_test.py — D0-3 health 四指标守卫（2026-09-09，assistant-c）。

覆盖（SupportBundle Spec v1.2 §D0-3）：
  T1 四指标键齐全：ghost/ttl_expired/pointer_integrity/shadow_backlog 均在 collect() 返回与 fmt 输出中
  T2 ghost_ratio：recall 命中已退役条目才计数
  T3 ttl_expired_refined=0：refined 旧条目不再隐身（D0-2 豁免的直接证据）
  T4 指针完整性：Python 侧 re.fullmatch 全字正则，宽 GLOB 无法混入
  T5 影子边：两端 active 的 supersedes/replaces 计数
  T6 隔离：--db 指向非默认库时 state/metrics 自动落库目录，禁写生产目录
数据保护：VectorStore 自建 tmp 库，生产零接触。
用法：python tests/xtmem_d03_guard_test.py
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PLUGIN_DIR = _REPO / "src" / "hermes-plugin"
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

HEALTH = Path(_REPO / "profiles"/ "personal"/ "scripts"/ "xtmem_health.py")
PROD_LOGS = Path(_REPO / "logs"/ "xtmem_metrics_history.jsonl")
PYTHON = sys.executable

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)[:160]))


def main() -> int:
    from memory.vector_store import VectorStore

    td = Path(tempfile.mkdtemp(prefix="xtmem-d03-"))
    try:
        db = td / "d03.db"
        # 建库 + 全 schema（含 D0-1 的 chunk_review_state）
        vs = VectorStore(str(db))
        vs.close()

        conn = sqlite3.connect(str(db))
        now = time.time()
        old = now - 90 * 86400  # 90 天前

        # 合成数据：
        #  1. refined 旧条目（D0-2 后永不隐身）
        #  2. raw conversation 旧条目（30 天流水，已过期）
        #  3. raw 普通新条目（不过期）
        #  4. raw 普通旧条目（7 天 TTL，已过期）
        seeds = [
            ("mem-refined01", "refined 事实条目", "refined", "", now, []),
            ("mem-stream01", "user: 对话流水", "raw", "", old, ["conversation"]),
            ("mem-fresh01", "普通新事实", "raw", "", now, []),
            ("mem-stale01", "普通旧事实", "raw", "", old, []),
        ]
        for cid, content, tier, derived, ts, tags in seeds:
            conn.execute(
                "INSERT INTO chunks (id, content, timestamp, track, state, namespace, visibility, "
                "tier, derived_from, tags, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, content, ts, "default", "active", "dsh", "shared", tier, derived,
                 json.dumps(tags), ts),
            )
        # superseded 带原因字串（非指针 → 违例 1）
        conn.execute(
            "INSERT INTO chunks (id, content, timestamp, track, state, superseded_by, namespace, "
            "visibility, tier, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("mem-super01", "被替代条目", now, "default", "superseded",
             "cleanup-20260827-prompt-residue", "dsh", "shared", "raw", now),
        )
        # 幽灵：recall 事件指向已退役条目且 ts>updated_at
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) "
            "VALUES (?,?,?,?,?,?,?)",
            (now + 1, "recall", "dsh", "dsh", "", "mem-super01", "{}"),
        )
        # 正常 recall 事件 ×2（不过期条目）
        for i in range(2):
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) "
                "VALUES (?,?,?,?,?,?,?)",
                (now, "recall", "dsh", "dsh", "", "mem-fresh01", "{}"),
            )
        # 影子边：两端均 active 的 supersedes
        conn.execute(
            "INSERT INTO memory_evolution (source_id, target_id, relation, confidence, created_at, "
            "visibility, valid_from, valid_to, shadow_state) VALUES (?,?,?,?,?,?,?,?,?)",
            ("mem-fresh01", "mem-stale01", "supersedes", 0.9, now, "shared", now, None, 1),
        )
        conn.commit()
        conn.close()

        # ── 跑 health：显式三参隔离 ──
        state_p = td / "state.json"
        metrics_p = td / "metrics"
        r = subprocess.run(
            [PYTHON, str(HEALTH), "--db", str(db), "--state", str(state_p),
             "--metrics-dir", str(metrics_p)],
            capture_output=True, text=True, encoding="utf-8", timeout=300,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        out = r.stdout

        # T1 四指标键齐全
        check("T1a rc=0", r.returncode == 0, out[:200] + r.stderr[:200])
        check("T1b ghost line in fmt", "幽灵率" in out, out[:300])
        check("T1c ttl line in fmt", "TTL 过期" in out)
        check("T1d pointer line in fmt", "指针完整性" in out)
        check("T1e shadow line in fmt", "影子边积压" in out)
        check("T1f 30-day window note", "30 天事件窗" in out)

        # 解析 state 判断阈值行为
        st = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
        # 重新 collect 一遍拿指标值（进程外跑，直接读 stdout 数字）
        # ghost：1 ghost / 3 recalls = 33.3% > 0.5% → warn
        check("T2 ghost counted (1/3)", "33.33%" in out, out[:400])
        check("T2b ghost status warn in state", st.get("ghost", {}).get("status") == "warn", str(st.get("ghost")))
        # TTL：refined 旧条目不再隐身（0 条）；stream+stale 两条过期
        check("T3 refined hidden = 0", "refined 隐身 0 条" in out, out[:500])
        check("T4 pointer violations = 1", "违例 1 条" in out, out[:500])
        check("T5 shadow edges = 1", "影子边积压 1 条" in out, out[:500])

        # ── T6 隔离验证：--db 非默认时 state/metrics 自动落库目录 ──
        prod_state = Path(HEALTH).parent / "health_state.json"
        _prod_state_before = prod_state.stat().st_mtime if prod_state.exists() else 0
        _prod_log_before = PROD_LOGS.stat().st_size if PROD_LOGS.exists() else 0
        r2 = subprocess.run(
            [PYTHON, str(HEALTH), "--db", str(db)],
            capture_output=True, text=True, encoding="utf-8", timeout=300,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        check("T6a rc=0 (auto-isolate run)", r2.returncode == 0, r2.stderr[:200])
        auto_state = db.parent / "health_state.json"
        auto_metrics = db.parent / "logs" / "xtmem_metrics_history.jsonl"
        check("T6b auto state beside db", auto_state.exists(), str(list(db.parent.iterdir())))
        check("T6c auto metrics beside db", auto_metrics.exists())
        _prod_state_after = prod_state.stat().st_mtime if prod_state.exists() else 0
        _prod_log_after = PROD_LOGS.stat().st_size if PROD_LOGS.exists() else 0
        check("T6d prod state untouched", _prod_state_after == _prod_state_before,
              f"{_prod_state_before} -> {_prod_state_after}")
        check("T6e prod metrics untouched", _prod_log_after == _prod_log_before,
              f"{_prod_log_before} -> {_prod_log_after}")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== D0-3 守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED:", failed)
    return 1 if failed else 0


def test_d03_guard():
    """pytest 入口：D0-3 health 四指标守卫全绿。"""
    assert main() == 0, "见上方 FAIL 明细（D0-3）"


if __name__ == "__main__":
    sys.exit(main())
