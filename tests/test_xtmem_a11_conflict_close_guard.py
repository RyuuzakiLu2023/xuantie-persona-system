# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_a11_conflict_close_guard.py · 单A11 注入层矛盾收口守卫
#
#   ▍这是什么
#   provider._assemble 在装配注入包时，对同一 judged supersedes 矛盾组只喂新的那条。
#   本守卫钉死六种边界（工单 §2.5）+ 红绿反向验证靶点。
#
#   ▍修改铁律
#   1. 断言只用临时夹具库（tempfile），严禁连生产库。
#   2. 守卫读 master：<XT_ROOT>/plugins/xtmem/provider.py
#   3. 反向验证：改坏 _conflict_drop_ids / _assemble 接线 → 必须红。
#
#   ▍更新记录
#   *更新：2026-09-16 · 创建（单A11 矛盾收口）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

MASTER = _REPO / "src" / "hermes-plugin"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _load_provider_module():
    path = os.path.join(MASTER, "provider.py")
    spec = importlib.util.spec_from_file_location("xtmem_provider_a11", path)
    mod = importlib.util.module_from_spec(spec)
    # 只取 _conflict_drop_ids，避免整模块副作用；仍执行模块体以定义符号
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # hermes 依赖缺失时退回 AST 抽取函数源并 exec（守卫只测纯函数）
        import ast
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        fn_src = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_conflict_drop_ids":
                fn_src = ast.get_source_segment(src, node)
                break
        if not fn_src:
            raise RuntimeError("master provider.py 缺 _conflict_drop_ids")
        ns = {}
        exec(compile(fn_src, path, "exec"), ns)

        class _M:
            pass

        mod = _M()
        mod._conflict_drop_ids = ns["_conflict_drop_ids"]
    return mod


def _make_db(tmpdir):
    db = os.path.join(tmpdir, "a11.db")
    conn = sqlite3.connect(db)
    now = time.time()
    conn.executescript(
        """
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            timestamp REAL,
            state TEXT DEFAULT 'active',
            tier TEXT DEFAULT 'raw',
            derived_from TEXT DEFAULT '',
            source_ids TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]',
            namespace TEXT DEFAULT '',
            project TEXT DEFAULT '',
            visibility TEXT DEFAULT 'shared',
            updated_at REAL DEFAULT 0
        );
        CREATE TABLE conflict_candidate (
            pair_key TEXT PRIMARY KEY,
            source_id TEXT,
            target_id TEXT,
            namespace TEXT DEFAULT '',
            project TEXT DEFAULT '',
            candidate_score REAL DEFAULT 0,
            llm_score REAL,
            decision TEXT,
            policy_version TEXT DEFAULT '',
            model TEXT DEFAULT '',
            prompt_hash TEXT DEFAULT '',
            status TEXT,
            attempt INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0,
            decision_reason TEXT DEFAULT ''
        );
        CREATE TABLE memory_evolution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            confidence REAL DEFAULT 0,
            visibility TEXT DEFAULT 'shared',
            valid_from REAL DEFAULT 0,
            valid_to REAL DEFAULT 0,
            shadow_state INTEGER DEFAULT 1,
            adjudicator TEXT DEFAULT '',
            policy_version TEXT DEFAULT '',
            decision_reason TEXT DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert_chunk(conn, cid, state="active", ns="dsh", vis="shared", project=""):
    conn.execute(
        "INSERT INTO chunks (id, content, timestamp, state, namespace, project, visibility, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, f"content-{cid}", time.time(), state, ns, project, vis, time.time()),
    )


def _insert_cand(conn, a, b, status="judged", decision="supersedes", reason="LLM双向均分 1.00/0.65"):
    pk = "|".join(sorted([a, b]))
    conn.execute(
        "INSERT INTO conflict_candidate (pair_key, source_id, target_id, decision, status, decision_reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pk, a, b, decision, status, reason),
    )


def _insert_ev(conn, newer, older, relation="supersedes", shadow=0):
    conn.execute(
        "INSERT INTO memory_evolution (source_id, target_id, relation, shadow_state, visibility) "
        "VALUES (?, ?, ?, ?, 'shared')",
        (newer, older, relation, shadow),
    )


def _ns_map_of(conn, ids):
    out = {}
    for cid in ids:
        r = conn.execute(
            "SELECT namespace, project, visibility, state FROM chunks WHERE id=?", (cid,)
        ).fetchone()
        if r:
            out[cid] = (r[0] or "unknown", r[1] or "", r[2] or "", r[3] or "active")
    return out


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM A11 注入层矛盾收口守卫 ===")
    from memory.visibility import visibility_ok

    mod = _load_provider_module()
    drop_fn = mod._conflict_drop_ids

    tmp = tempfile.mkdtemp(prefix="xtmem-a11-guard-")
    db = _make_db(tmp)
    conn = sqlite3.connect(db)

    # ------------------------------------------------------------
    # 夹具：六种边界
    # ------------------------------------------------------------
    # 1. 两条都不是 active
    _insert_chunk(conn, "mem-a1old", state="archived")
    _insert_chunk(conn, "mem-a1new", state="archived")
    _insert_cand(conn, "mem-a1old", "mem-a1new")
    _insert_ev(conn, "mem-a1new", "mem-a1old")

    # 2. 只有一条 active（旧 active，新 archived）
    _insert_chunk(conn, "mem-a2old", state="active")
    _insert_chunk(conn, "mem-a2new", state="archived")
    _insert_cand(conn, "mem-a2old", "mem-a2new")
    _insert_ev(conn, "mem-a2new", "mem-a2old")

    # 3. 两条都 active 且判定矛盾 —— 核心：只喂新的
    _insert_chunk(conn, "mem-a3old", state="active")
    _insert_chunk(conn, "mem-a3new", state="active")
    _insert_cand(conn, "mem-a3old", "mem-a3new")
    _insert_ev(conn, "mem-a3new", "mem-a3old")

    # 4. unresolvable：两条都放
    _insert_chunk(conn, "mem-a4a", state="active")
    _insert_chunk(conn, "mem-a4b", state="active")
    _insert_cand(conn, "mem-a4a", "mem-a4b", status="unresolvable", decision="supersedes")

    # 5. closed：不筛选
    _insert_chunk(conn, "mem-a5old", state="active")
    _insert_chunk(conn, "mem-a5new", state="active")
    _insert_cand(conn, "mem-a5old", "mem-a5new", status="closed", decision="supersedes")
    _insert_ev(conn, "mem-a5new", "mem-a5old")

    # 6. 跨域：新侧 private 异域 → 旧侧保留
    _insert_chunk(conn, "mem-a6old", state="active", ns="dsh", vis="shared")
    _insert_chunk(conn, "mem-a6new", state="active", ns="other", vis="private")
    _insert_cand(conn, "mem-a6old", "mem-a6new")
    _insert_ev(conn, "mem-a6new", "mem-a6old")

    # 额外：取向缺失（无 evolution 边）→ 双活保留
    _insert_chunk(conn, "mem-a7a", state="active")
    _insert_chunk(conn, "mem-a7b", state="active")
    _insert_cand(conn, "mem-a7a", "mem-a7b")

    # 额外：正常无矛盾记忆 —— 零影响
    _insert_chunk(conn, "mem-a8ok", state="active")
    _insert_chunk(conn, "mem-a9ok", state="active")

    # 白名单外：p3 污染理由 → 不参与筛选
    _insert_chunk(conn, "mem-b1old", state="active")
    _insert_chunk(conn, "mem-b1new", state="active")
    _insert_cand(conn, "mem-b1old", "mem-b1new", reason="规则判定:explicit_version_priority_3 p3污染回退")
    _insert_ev(conn, "mem-b1new", "mem-b1old")

    # 白名单外：空理由
    _insert_chunk(conn, "mem-b2old", state="active")
    _insert_chunk(conn, "mem-b2new", state="active")
    _insert_cand(conn, "mem-b2old", "mem-b2new", reason="")

    # enriches：不是 supersedes，不筛
    _insert_chunk(conn, "mem-b3a", state="active")
    _insert_chunk(conn, "mem-b3b", state="active")
    _insert_cand(conn, "mem-b3a", "mem-b3b", decision="enriches", reason="LLM双向均分 0.10/0.65")

    conn.commit()

    all_ids = [r[0] for r in conn.execute("SELECT id FROM chunks").fetchall()]
    ns_map = _ns_map_of(conn, all_ids)
    drop = drop_fn(conn, all_ids, ns_map, "dsh", "", visibility_ok)

    check("边界1 两条非active：不进包（不在 drop 也不需进）", "mem-a1old" not in drop and "mem-a1new" not in drop)
    check("边界2 仅旧active新archived：旧侧保留", "mem-a2old" not in drop)
    check("边界3 双active矛盾：旧边进 drop", "mem-a3old" in drop)
    check("边界3 双active矛盾：新边不进 drop", "mem-a3new" not in drop)
    check("边界4 unresolvable：双活保留", "mem-a4a" not in drop and "mem-a4b" not in drop)
    check("边界5 closed：不筛选", "mem-a5old" not in drop and "mem-a5new" not in drop)
    check("边界6 跨域新侧private：旧侧保留", "mem-a6old" not in drop)
    check("取向缺失：双活保留", "mem-a7a" not in drop and "mem-a7b" not in drop)
    check("正常记忆零影响", "mem-a8ok" not in drop and "mem-a9ok" not in drop)
    check("p3污染白名单外不筛", "mem-b1old" not in drop)
    check("空理由白名单外不筛", "mem-b2old" not in drop)
    check("enriches 不筛", "mem-b3a" not in drop and "mem-b3b" not in drop)

    # 空池
    check("空 ids → 空 drop", drop_fn(conn, [], {}, "dsh", "", visibility_ok) == set())

    # 仅旧边在池、新边不在池但可见 → 仍压旧
    drop_only_old = drop_fn(
        conn, ["mem-a3old"], _ns_map_of(conn, ["mem-a3old"]), "dsh", "", visibility_ok
    )
    check("新侧不在本轮池但可见可用 → 旧侧仍压", "mem-a3old" in drop_only_old)

    # ------------------------------------------------------------
    # 源码接线断言：_assemble 必须调用 _conflict_drop_ids
    # ------------------------------------------------------------
    src = open(os.path.join(MASTER, "provider.py"), encoding="utf-8").read()
    check("源码含 _conflict_drop_ids 定义", "def _conflict_drop_ids(" in src)
    check("_assemble 内调用 _conflict_drop_ids", "_conflict_drop_ids(" in src.split("def _assemble(_res):", 1)[-1][:4000])
    check("装配段用 drop 集合跳过旧边", "if doc.id in _drop_ids:" in src)
    check("装配段将 drop_fn 结果赋给 _drop_ids", "_drop_ids = _conflict_drop_ids(" in src)
    check("装配段仍有分歧警告（未砍 D2-2）", "get_conflict_warnings_for" in src)

    conn.close()
    ok = sum(1 for v in CHECKS.values() if v)
    total = len(CHECKS)
    print(f"\n=== 结果: {ok}/{total} ===")
    return 0 if ok == total else 1


def test_a11_guard():
    rc = _guard()
    assert rc == 0, "A11 守卫存在 FAIL"


if __name__ == "__main__":
    sys.exit(_guard())
