# -*- coding: utf-8 -*-
# D0-1 专用守卫测试：拆 overturn 归档 + chunk_review_state 幂等状态表 + 归因收窄 + utility 0.8
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import sys, os, sqlite3, tempfile, json, time, shutil
from pathlib import Path

MASTER = _REPO / "src" / "hermes-plugin"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

from memory.vector_store import VectorStore
from provider import XtmemMemoryProvider

CHECKS = {}
def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    """脚本体收进函数：pytest 收集期只导入不执行，避免顶层 sys.exit 打断整个收集。"""
    CHECKS.clear()
    td = tempfile.mkdtemp(prefix="test-d01-")
    try:
        db_path = str(Path(td) / "d01.db")
        vs = VectorStore(db_path)
        vs.close()

        # 1. 构造 20 条 active chunks
        conn = sqlite3.connect(db_path)
        now = time.time()
        cids = [f"mem-test{i:04x}" for i in range(20)]
        for i, cid in enumerate(cids):
            conn.execute(
                "INSERT INTO chunks (id, content, timestamp, track, state, namespace, visibility, updated_at) "
                "VALUES (?, ?, ?, 'default', 'active', 'dsh', 'shared', ?)",
                (cid, f"test content {cid} anchor_target_{i%3}", now, now)
            )
        conn.commit()
        conn.close()

        # 构造 provider 实例
        p = XtmemMemoryProvider()
        p._db_path = db_path
        p._actor = "dsh"
        p._namespace = "dsh"
        p._recent_exposed.extend(cids)

        # ── 校验 1：第一次 overturn_bump ──
        for cid in cids:
            p._overturn_bump(cid)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        arch1 = conn.execute("SELECT count(*) FROM chunks WHERE state='archived'").fetchone()[0]
        st1 = conn.execute("SELECT count(*), max(overturn_count), min(overturn_count), max(review_status) FROM chunk_review_state").fetchone()
        ev1 = conn.execute("SELECT count(*) FROM xtmem_events WHERE kind='review_required'").fetchone()[0]
        check("first bump: 0 archived", arch1 == 0, f"archived={arch1}")
        check("first bump: 20 rows in state table with count=1", st1[0] == 20 and st1[1] == 1 and st1[2] == 1, str(dict(st1)))
        check("first bump: status is none", st1[3] == "none", st1[3])
        check("first bump: 0 review_required events", ev1 == 0, f"ev={ev1}")

        # ── 校验 2：第二次 overturn_bump（连续 2 次负面）──
        for cid in cids:
            p._overturn_bump(cid)

        arch2 = conn.execute("SELECT count(*) FROM chunks WHERE state='archived'").fetchone()[0]
        st2 = conn.execute("SELECT count(*), max(overturn_count), min(overturn_count), min(review_status) FROM chunk_review_state").fetchone()
        ev2 = conn.execute("SELECT count(*) FROM xtmem_events WHERE kind='review_required'").fetchone()[0]
        check("second bump: 0 archived (NO auto-archive)", arch2 == 0, f"archived={arch2}")
        check("second bump: 20 rows in state table with count=2", st2[0] == 20 and st2[1] == 2 and st2[2] == 2, str(dict(st2)))
        check("second bump: status is required", st2[3] == "required", st2[3])
        check("second bump: 20 review_required events emitted", ev2 == 20, f"ev={ev2}")

        # ── 校验 3：第三次 overturn_bump（幂等测试：状态表 count=3，行数不变，不新增事件）──
        for cid in cids:
            p._overturn_bump(cid)

        st3 = conn.execute("SELECT count(*), max(overturn_count), min(overturn_count) FROM chunk_review_state").fetchone()
        ev3 = conn.execute("SELECT count(*) FROM xtmem_events WHERE kind='review_required'").fetchone()[0]
        check("third bump: 20 rows in state table with count=3", st3[0] == 20 and st3[1] == 3 and st3[2] == 3, str(dict(st3)))
        check("third bump: idempotent - events count unchanged (still 20)", ev3 == 20, f"ev={ev3}")

        # ── 校验 4：increment_utility 对 corrected 不加分 ──
        vs = VectorStore(db_path)
        cid0 = cids[0]
        # 先记一次 exposed (+0.1)
        vs.increment_utility(cid0, viewer="dsh", state="exposed")
        u1 = vs.get_utility(cid0, viewer="dsh")
        init_score = u1["utility_score"]
        init_corr = u1.get("corrected_n", 0)

        # 两次 corrected
        vs.increment_utility(cid0, viewer="dsh", state="corrected")
        vs.increment_utility(cid0, viewer="dsh", state="corrected")
        u2 = vs.get_utility(cid0, viewer="dsh")

        check("utility_score NO +1.6 accumulation on corrected", abs(u2["utility_score"] - init_score) < 1e-6,
              f"score before={init_score}, after={u2['utility_score']}")
        row_corr = vs._get_conn().execute("SELECT corrected_n FROM chunk_utility WHERE chunk_id=? AND viewer_agent='dsh'", (cid0,)).fetchone()[0]
        check("corrected_n incremented by 2", row_corr == 2,
              f"corrected_n={u2.get('corrected_n')}")
        vs.close()

        # ── 校验 5：归因收窄（_resolve_corrected_targets）──
        # 5a. 显式 [m:id]
        r_mid = p._resolve_corrected_targets(f"不对，看这里 [m:{cids[5]}]", cids)
        check("attribution: [m:id] matches single target", r_mid == [cids[5]], str(r_mid))

        # 5b. 锚点命中（包含 anchor_target_1 的 chunks）
        r_anc = p._resolve_corrected_targets("不对，那个 anchor_target_1 错了", cids)
        expected_anc = [cid for i, cid in enumerate(cids) if i % 3 == 1]
        check("attribution: anchor matches only relevant chunks", sorted(r_anc) == sorted(expected_anc), f"got {len(r_anc)}, exp {len(expected_anc)}")

        # 5c. 无 [m:id] 且无锚点（普通'不对'）→ 空列表，整批兜底已彻底删除
        r_empty = p._resolve_corrected_targets("不对，这不对啊", cids)
        check("attribution: generic '不对' returns empty (whole-batch deleted)", r_empty == [], str(r_empty))

        conn.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== D0-1 守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    return 1 if failed else 0


def test_d01_guard():
    """pytest 入口：跑完整守卫并断言全绿。"""
    assert _guard() == 0, "见上方 FAIL 明细（D0-1）"


if __name__ == "__main__":
    sys.exit(_guard())
