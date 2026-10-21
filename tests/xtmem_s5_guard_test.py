# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/xtmem_s5_guard_test.py · S5 书架幽灵 + R1 数据线守卫
#
#   ▍这是什么
#   合并单（2026-09-10）两件事的回归守卫：
#   - 任务 A（S5）：state 改非 active 必须 bump updated_at，否则 serve 书架差量拉不到 → 幽灵
#   - 任务 B（R1）：CLI recall 记事件必须带 meta.verdict，否则 R1 第二判据零数据
#   跑法：直接运行 exit 0/1；pytest 收集走 test_s5_guard()（体必须收在 _guard() 内，
#   顶层 sys.exit 会打断整个收集——见错题本 #35）。
#
#   ▍修改铁律
#   1. 本文件必须四副本同步（xtmem-project/tests、XTMEM/tests、xtmem-project 镜像、
#      XTMEM-public/tests），sha 不等即视为未交付。
#   2. 断言只用临时夹具库（tempfile），严禁连生产库。
#
#   ▍更新记录
#   *更新：2026-09-10 · 创建（S5+R1 合并工单，assistant-c）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import ast
import os
import shutil
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
CLI_PATH = _REPO / "xtmem"/ "xtmem.py"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM S5 书架幽灵 + R1 数据线守卫 ===")

    from memory.vector_store import VectorStore

    def _mk():
        tmp = tempfile.mkdtemp(prefix="xtmem-s5-guard-")
        db = os.path.join(tmp, "g.db")
        vs = VectorStore(db)
        conn = vs._get_conn()
        for i, cid in enumerate(["mem-cccc0001", "mem-cccc0002", "mem-cccc0003"]):
            conn.execute(
                "INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, "
                "content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, tier) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "guard 夹具内容 %d" % i, time.time(), 0.6, '["g"]', "guard", 3, "h-" + cid,
                 "default", "active", "", "dsh", "", "guard", time.time(), "raw"))
        conn.commit()
        return tmp, vs, conn

    # ------------------------------------------------------------
    # §1. A-3-1：mark_superseded 后 updated_at 被 bump
    # ------------------------------------------------------------
    tmp, vs, conn = _mk()
    try:
        # 基准必须在退役【之前】取（serve 的真实节奏：上次 sync 时记录 max_updated_at，
        # 之后才发生退役）。退役后再取 active 的 max，会与被退役行同刻，> 严格比较拉不到。
        # 这是本守卫首版的断言缺陷，不是产品缺陷——对照实验 s5_before_after.py 同口径。
        _base_max = conn.execute(
            "SELECT COALESCE(MAX(updated_at),0) FROM chunks WHERE state='active'").fetchone()[0]
        time.sleep(0.02)
        t0 = time.time()
        vs.mark_superseded("mem-cccc0001", reason="s5-guard", by="guard")
        row = conn.execute("SELECT state, updated_at, retired_at FROM chunks WHERE id='mem-cccc0001'").fetchone()
        check("1.1 mark_superseded 后 state=superseded", row["state"] == "superseded")
        check("1.2 updated_at 被 bump（>= 调用时刻）",
              row["updated_at"] is not None and row["updated_at"] >= t0 - 1e-6,
              "updated_at=%s t0=%s" % (row["updated_at"], t0))
        check("1.3 updated_at 与 retired_at 同刻（同一次写）",
              abs(float(row["updated_at"]) - float(row["retired_at"])) < 1e-6)

        # ------------------------------------------------------------
        # §2. A-3-2：serve 差量 SQL 能拉到这条（回归 #43）
        # ------------------------------------------------------------
        delta = conn.execute(
            "SELECT id FROM chunks WHERE COALESCE(updated_at,0) > ? LIMIT 501", (_base_max,)).fetchall()
        ids = {r[0] for r in delta}
        check("2.1 serve 差量 SQL 能拉到被退役的那条（#43 回归）",
              "mem-cccc0001" in ids, "delta=%s" % sorted(ids))
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------
    # §3. A-3-3：CLI forget / 两处归档路径同样 bump（静态核查 + 动态验证）
    # ------------------------------------------------------------
    src = open(CLI_PATH, encoding="utf-8").read()
    # 静态：三处归档语句都必须带 updated_at
    check("3.1 CLI forget 归档语句带 updated_at",
          "retire_reason='forget', " in src and "retired_by=?, retired_at=?, updated_at=? WHERE id IN (" in src)
    # 3.2 / 3.3（2026-09-16 单A23-补-3 改口径）：
    #   旧写法是"精确子串匹配整条 SQL"——单A23 给两条预测归档路径补上
    #   retire_reason/retired_by/retired_at 三列后，updated_at 前面多了一段，
    #   子串就对不上了，守卫【误红】（而 updated_at 其实照写不误）。
    #   精确字面匹配的通病：它守的是"SQL 长什么样"，不是"SQL 干了什么"。
    #   改成语义口径：把 CLI 里每条形如 .execute(...) 的调用摊开看，
    #   只要它的语句里出现 state='archived'，就要求同一条语句里也出现 updated_at。
    #   ⇒ 以后再往这些语句中间插列，不会再误红；真漏了 updated_at，照样红。
    # ⚠️ 注意仍【只】是静态核查：3.2/3.3 至今没有动态验证（3.4 只跑 forget 路径），
    #    两条预测归档路径的动态覆盖是既有缺口，本单不扩范围，留给 maintainer 定。
    _tree = ast.parse(src)
    _archive_missing = []
    _archive_total = 0
    for _n in ast.walk(_tree):
        if not (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                and _n.func.attr == "execute"):
            continue
        _lits = [a.value for a in ast.walk(_n)
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not any("state='archived'" in s for s in _lits):
            continue
        _archive_total += 1
        if not any("updated_at" in s for s in _lits):
            _archive_missing.append(_n.lineno)
    check("3.2 旧格式单块 2-miss 归档带 updated_at",
          not _archive_missing and _archive_total >= 3,
          "缺 updated_at 的归档语句行号=%s（共 %d 条归档语句）" % (_archive_missing, _archive_total))
    check("3.3 簇级 2-miss 归档带 updated_at",
          any("state='archived'" in s and "WHERE namespace=?" in s and "track='prediction'" in s
              and "updated_at" in s
              for s in [a.value for a in ast.walk(_tree)
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)]),
          "簇级归档语句（namespace+track=prediction）没带 updated_at")
    # 动态：跑一遍 forget 语义等价 SQL，确认差量可见
    tmp2, vs2, conn2 = _mk()
    try:
        # 同样：基准先取（退役前），再执行归档写入
        prev_max2 = conn2.execute(
            "SELECT COALESCE(MAX(updated_at),0) FROM chunks WHERE state='active'").fetchone()[0]
        time.sleep(0.02)
        _now = time.time()
        conn2.execute(
            "UPDATE chunks SET state='archived', superseded_by='', retire_reason='forget', "
            "retired_by=?, retired_at=?, updated_at=? WHERE id IN (?,?)",
            ("guard", _now, _now, "mem-cccc0002", "mem-cccc0003"))
        conn2.commit()
        delta2 = conn2.execute(
            "SELECT id FROM chunks WHERE COALESCE(updated_at,0) > ? LIMIT 501", (prev_max2,)).fetchall()
        ids2 = {r[0] for r in delta2}
        check("3.4 forget/归档路径退役的两条都能被差量拉到",
              {"mem-cccc0002", "mem-cccc0003"} <= ids2, "delta=%s" % sorted(ids2))
    finally:
        conn2.close()
        shutil.rmtree(tmp2, ignore_errors=True)

    # ------------------------------------------------------------
    # §4. A-3-4：端到端——N 条 active → supersede 1 条 → 书架 remove
    # ------------------------------------------------------------
    tmp3, vs3, conn3 = _mk()
    try:
        from memory.hybrid_retriever import HybridRetriever
        from memory.hybrid_retriever import MemoryDoc

        # 模拟书架：装载 3 条 active
        shelf = {}
        for cid, content in conn3.execute(
                "SELECT id, content FROM chunks WHERE state='active'").fetchall():
            shelf[cid] = content
        check("4.1 书架初始装载 3 条", len(shelf) == 3, "shelf=%d" % len(shelf))

        prev_max3 = conn3.execute(
            "SELECT COALESCE(MAX(updated_at),0) FROM chunks WHERE state='active'").fetchone()[0]
        time.sleep(0.02)
        vs3.mark_superseded("mem-cccc0002", reason="s5-e2e", by="guard")

        # serve 差量处理后：state 非 active → 应从书架 remove
        delta3 = conn3.execute(
            "SELECT id, state FROM chunks WHERE COALESCE(updated_at,0) > ? LIMIT 501", (prev_max3,)).fetchall()
        removed = []
        for dr in delta3:
            if dr["state"] in ("superseded", "archived"):
                if shelf.pop(dr["id"], None) is not None:
                    removed.append(dr["id"])
        check("4.2 端到端：被 supersede 的条目从书架移除", removed == ["mem-cccc0002"], "removed=%s" % removed)
        check("4.3 书架剩余 2 条（幽灵已清）", len(shelf) == 2, "shelf=%d" % len(shelf))
    finally:
        conn3.close()
        shutil.rmtree(tmp3, ignore_errors=True)

    # ------------------------------------------------------------
    # §5. A-2：serve 缩水阈值已收紧（任何缩水即全量重建）
    # ------------------------------------------------------------
    check("5.1 serve 缩水阈值收紧为任何缩水",
          "cur_count < prev_count:" in src and "cur_count < prev_count - 5" not in src)

    # ------------------------------------------------------------
    # §6. B-2：CLI recall 记事件带 meta.verdict，且与 verdict_of 同口径
    # ------------------------------------------------------------
    check("6.1 主路径 recall 事件带 verdict/mode",
          'meta={"verdict": _verdict, "mode": _mode}' in src)
    check("6.2 exact 栈记录点带 verdict",
          'meta={"verdict": "精确栈（LIKE 精确检索）", "mode": "exact"}' in src)
    check("6.3 B2 判空记录点带 verdict",
          'meta={"verdict": "B2 判空：寒暄/语气 query 零召回（路由前置）"' in src)
    check("6.4 lookup 记录点带 verdict",
          'meta={"verdict": "精确回查（lookup by id）", "mode": "lookup"}' in src)

    # 口径一致性：CLI 用的 _verdict 必须来自共享 verdict_of
    check("6.5 CLI 复用共享 verdict_of（防口径漂移）",
          "from provider import verdict_of as _verdict_of" in src)

    # 动态：verdict_of 三态口径验证（与 provider 同函数）
    try:
        from provider import verdict_of
        check("6.6 verdict_of(refined-first, True) = 高置信",
              verdict_of("refined-first", True) == "高置信（refined 首跳命中）")
        check("6.7 verdict_of(raw-fallback, True) = 兜底（R1 第二判据统计目标）",
              verdict_of("raw-fallback", True) == "兜底（raw 回落）")
        # ⚠️ 真实语义（实测 2026-09-10）：raw-fallback 分支【无视 has_items】恒返回「兜底」——
        # 这是共享函数的既有行为，守卫按真实口径断言，不按直觉改判据。
        check("6.8 verdict_of(raw-fallback, False) 也返回兜底（既有语义）",
              verdict_of("raw-fallback", False) == "兜底（raw 回落）")
        check("6.8b verdict_of(其他 mode, False) = 空结果",
              verdict_of("whatever", False) == "空结果")
    except Exception as e:
        check("6.6 verdict_of 可导入", False, str(e))

    # 记录顺序：_verdict 赋值行必须在 _record_cli_event_batch 调用行之前
    lines = src.splitlines()
    idx_verdict = next((i for i, l in enumerate(lines) if l.strip().startswith("_verdict = (")), -1)
    # 锚定【主路径】那处：调用跨两行（第 2 行才是 meta={"verdict": _verdict...}）——
    # exact 栈起始行形状相同，只按起始行锚会误撞到更靠前那处（首版实测 record_line=456 即此因）。
    idx_record = next((i for i, l in enumerate(lines)
                       if "_record_cli_event_batch(\"recall\", _batch_agent, [it[\"id\"] for it in items]," in l
                       and i + 1 < len(lines) and '"verdict": _verdict' in lines[i + 1]), -1)
    check("6.9 _verdict 计算在 recall 记事件之前（纯顺序修复）",
          idx_verdict > 0 and idx_record > 0 and idx_verdict < idx_record,
          "verdict_line=%d record_line=%d" % (idx_verdict, idx_record))

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== S5/R1 守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_s5_guard():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
