# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_p74_trace_shadow.py · 血缘数据源改道守卫
#
#   ▍这是什么
#   原为 Spec [S2] Task [T-P7.4] 的"影子边打标"守卫（assistant-c,2026-09-13）。
#   🔴【2026-09-14 改道重写】家谱数据源从 memory_evolution 边表切到 chunks.superseded_by 指针
#   （修复方向 v3.2 §不记错-3 + 顶模裁定二），本守卫随之重写，覆盖三件事：
#     1. 家谱走【指针】：祖先链/后辈链只认 chunks.superseded_by，边表独有条目不得串进家谱；
#     2. 指针语义恒量：relation="supersedes" / edge_state="confirmed"（指针=既成退役事实，无候选态）；
#     3. 血缘【批次约束】（原病灶 3 保留）：同 derived_from 但 source_ids 不同的条目不许互相串。
#
#   ▍为什么改道（旧病灶危害）
#   边表 689 条 supersedes(shadow=0) 中 680 条（98.7%）无正文依据，是平行旧账本；chunks 指针
#   实测 100% 闭环（timeline 57/57、distill-retire 342/342、悬空 0、断链 0）。原守卫守的
#   "影子边候选标"随数据源废弃而失去意义——候选边概念只存在于边表，指针无语义细分。
#   本守卫现在的钉子：谁把 _evolution_chain / _successors 改回读边表，§1.4 的 decoy 断言立刻红。
#
#   ▍跑法
#   直接运行 exit 0/1；pytest 收集走 test_p74_trace_shadow()。
#   （体必须收在 _guard() 内，顶层 sys.exit 会打断整个收集——见错题本 #35。）
#
#   ▍修改铁律
#   1. 断言只用临时夹具库（tempfile），【严禁】连生产库；全程 XTMEM_DB_PATH 指向临时库。
#   2. 反向验证配套：把 memory/trace.py 的 _evolution_chain 改回读 memory_evolution → §1.4 必红。
#   3. 本文件曾属单G；2026-09-14 改道重写属修复方向 v3.2 自理件（maintainer）。
#
#   ▍更新记录
#   *更新：2026-09-14 · 【改道重写】数据源 边表→chunks 指针：§1/§3 夹具改设 superseded_by；
#     删除候选/已确认对照段（§0/§1.1-1.3/§3.1-3.3 影子边断言），改为指针语义恒量 + decoy 钉子。*
#   *更新：2026-09-13 · §6 零写断言按单H P7.5 复审口径升级（COUNT 型 → set_trace_callback 写语句钉
#     + 全表全字段快照对拍）：单H 点名"仅 COUNT 三表 = 弱断言"（UPDATE 改值/写旁表照绿），
#     并给协同要求"单G 须同步升级 trace 零写断言"。既有 d3_4 §5 未动（属另一单）。*
#   *创建：2026-09-13 · P7.4 影子边打标 + 血缘批次约束守卫（maintainer派单单G）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import re
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

# 读 master（deploy 主树）——与其他 trace 守卫同款路径约定
MASTER = _REPO / "src" / "hermes-plugin"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM P7.4 影子边打标守卫 ===")

    from memory.trace import trace
    from memory.open_db import open_db

    tmp = tempfile.mkdtemp(prefix="xtmem-p74-guard-")
    db = os.path.join(tmp, "t.db")
    # 隔离闸：把环境指到临时库，任何误连生产的可能都掐死（工单 §四.5）。
    # ⚠️ 必须"存原值→finally 还原"：直接 os.environ[...]= 会在本用例结束后留下 XTMEM_DB_PATH，
    #    conftest 的环境漂移断言（tests/conftest.py 收尾检查）会把后续 10 个用例全判 ERROR。
    _prior_db_path = os.environ.get("XTMEM_DB_PATH")
    os.environ["XTMEM_DB_PATH"] = db
    try:
        conn = open_db(db, seed=True)
        conn.execute("""CREATE TABLE chunks (id TEXT PRIMARY KEY, content TEXT, timestamp REAL,
                        importance REAL, tags TEXT, source TEXT, token_count INT, content_hash TEXT,
                        track TEXT, state TEXT, superseded_by TEXT, namespace TEXT, project TEXT,
                        host_origin TEXT, updated_at REAL, visibility TEXT, source_ids TEXT,
                        derived_from TEXT, tier TEXT, content_time REAL, retire_reason TEXT,
                        retired_by TEXT, retired_at REAL)""")
        conn.execute("""CREATE TABLE memory_evolution (id INTEGER PRIMARY KEY, source_id TEXT,
                        target_id TEXT, relation TEXT, confidence REAL, created_at TEXT,
                        visibility TEXT, valid_from REAL, valid_to REAL, shadow_state INT,
                        adjudicator TEXT, policy_version TEXT, decision_reason TEXT)""")
        conn.execute("""CREATE TABLE distill_units (unit_id TEXT PRIMARY KEY, namespace TEXT,
                        source TEXT, ts_start REAL, ts_end REAL, turn_count INT, char_count INT,
                        raw_ids TEXT, prev_unit TEXT, filtered_ids TEXT, has_correction INT,
                        state TEXT, distilled_at TEXT, refined_ids TEXT, attempt INT,
                        last_error TEXT, created_at TEXT, claim_token TEXT)""")
        conn.commit()
        now = time.time()

        def _chunk(cid, content, **kw):
            vals = dict(id=cid, content=content, timestamp=now, importance=0.5, tags='[]',
                        source="guard", token_count=1, content_hash="h", track="default",
                        state="active", superseded_by="", namespace="dsh", project="",
                        host_origin="", updated_at=now, visibility="shared", source_ids="",
                        derived_from="", tier="raw", content_time=0, retire_reason="",
                        retired_by="", retired_at=0)
            vals.update(kw)
            cols = ",".join(vals.keys())
            marks = ",".join("?" * len(vals))
            conn.execute(f"INSERT INTO chunks ({cols}) VALUES ({marks})", list(vals.values()))
            conn.commit()

        def _evo(src, tgt, rel, conf, shadow=0):
            """造演化边。shadow=1 → 影子边（候选），0 → 已确认边。方向 source=新/target=旧。"""
            conn.execute("INSERT INTO memory_evolution (source_id, target_id, relation, confidence, "
                         "created_at, visibility, valid_from, valid_to, shadow_state, adjudicator, "
                         "policy_version, decision_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (src, tgt, rel, conf, now, "shared", now, 0, int(shadow), "", "", ""))
            conn.commit()

        # ----------------------------------------------------------
        # §1. 祖先链：只认 chunks.superseded_by 指针（改道核心）
        # 造两条链：
        #   链一（指针）：a1(旧) superseded_by=b1(新) —— 应被读到
        #   诱饵（边表独有）：边表造 decoy 取代 b1 ——【不】应被读到
        # ----------------------------------------------------------
        _chunk("mem-p74-a1", "旧方案（被 b1 取代）", superseded_by="mem-p74-b1",
               state="superseded", retire_reason="distill-retire")
        _chunk("mem-p74-b1", "新方案")
        # 🔴【改道钉子】边表独有诱饵——家谱若仍读边表，1.4 必红
        _chunk("mem-p74-decoy", "诱饵条：只在边表里，不在指针链里")
        _evo("mem-p74-decoy", "mem-p74-b1", "supersedes", 0.99, shadow=0)

        r_cand = trace(conn, "mem-p74-b1", depth=5)

        # 1.1 祖先节点来自指针
        n_anc = [n for n in r_cand["nodes"] if n["id"] == "mem-p74-a1"]
        check("1.1 祖先链读到指针前任（chunks.superseded_by）",
              len(n_anc) == 1, "nodes=%s" % r_cand["nodes"])
        # 1.2 指针边语义恒量：relation=supersedes / edge_state=confirmed
        # ⚠️ relation_to_child 存【原始 relation 值】（英文），中文"取代"是渲染层 _REL_LABEL 转的
        check("1.2 指针边语义恒量（relation=supersedes, edge_state=confirmed）",
              n_anc and n_anc[0].get("edge_state") == "confirmed"
              and n_anc[0].get("relation_to_child") == "supersedes",
              "node=%s" % (n_anc[0] if n_anc else None))
        # 1.3 链为 [旧, 自身] 两代
        check("1.3 祖先链两代（旧在前，自身在末）",
              [n["id"] for n in r_cand["nodes"]] == ["mem-p74-a1", "mem-p74-b1"],
              "ids=%s" % [n["id"] for n in r_cand["nodes"]])
        # 1.4 🔴 改道钉子：边表独有 decoy 不得出现在链里
        _ids_cand = [n["id"] for n in r_cand["nodes"]]
        check("1.4 🔴 改道钉子：边表独有 decoy【不】出现在指针链里",
              "mem-p74-decoy" not in _ids_cand,
              "ids=%s（出现 decoy = 仍在读 memory_evolution）" % _ids_cand)
        # 1.5 端点自身 edge_state 为空——它不是边，别给它贴假标签
        check("1.5 端点自身 edge_state 为空串（无被指向的边）",
              r_cand["nodes"][-1].get("edge_state") == "" and r_cand["nodes"][-1]["id"] == "mem-p74-b1",
              "last=%s" % r_cand["nodes"][-1])

        # ----------------------------------------------------------
        # §2. 文本渲染 + json 两路（指针链无语义候选标，文本不得出现「候选」字样）
        # ----------------------------------------------------------
        check("2.1 文本渲染祖先链正常（含 id）",
              "mem-p74-a1" in r_cand.get("text", ""), "text=%s" % r_cand.get("text", ""))
        check("2.2 🔴 指针链【不】出现「候选」字样（指针=既成事实，无候选态）",
              "[候选]" not in r_cand.get("text", ""), "text=%s" % r_cand.get("text", ""))
        check("2.3 json 输出带结构化 edge_state 字段（机读）",
              all("edge_state" in n for n in r_cand["nodes"]),
              "nodes=%s" % r_cand["nodes"])

        # ----------------------------------------------------------
        # §3. 后辈链：读本条自己的 superseded_by 指针
        # ----------------------------------------------------------
        _chunk("mem-p74-old", "我的旧条", superseded_by="mem-p74-newf")
        _chunk("mem-p74-newf", "我的接替者")
        r_old = trace(conn, "mem-p74-old", depth=3)
        succ = {s["id"]: s for s in r_old.get("successors", [])}
        check("3.1 后辈来自本条自己的 superseded_by 指针",
              succ.get("mem-p74-newf", {}).get("edge_state") == "confirmed",
              "succ=%s" % r_old.get("successors"))
        check("3.2 🔴 文本「后续演化」行渲染（指针链路）",
              "mem-p74-newf(取代)" in r_old.get("text", ""),
              "text=%s" % r_old.get("text", ""))
        check("3.3 无 superseded_by 的条（active 无后继）：successors 为空（如实不编）",
              trace(conn, "mem-p74-newf", depth=3).get("successors") == [],
              "succ=%s" % trace(conn, "mem-p74-newf", depth=3).get("successors"))

        # ----------------------------------------------------------
        # §4. 指针链的取舍：只走指针，条数由指针决定（不因边表多变多）
        # ----------------------------------------------------------
        check("4.1 指针链份数正确（旧+自身=2，decoy 未混入故仍 2）",
              len(r_cand["nodes"]) == 2, "nodes=%d" % len(r_cand["nodes"]))

        # ----------------------------------------------------------
        # §5. 血缘批次约束（病灶 3 保留）：同 derived_from 不同 source_ids → 不互相串
        # 修复前：同 derived_from 的兄弟条目 source_ids 会被并集进对方血缘（口径错）；
        # 修复后：只认【本条目自己】的 source_ids，兄弟的源不出现在我的血缘里。
        # ----------------------------------------------------------
        _chunk("mem-p74-src1", "我自己的来源1")
        _chunk("mem-p74-src2", "别人的来源2（不该串进来）")
        _chunk("mem-p74-lin", "我的派生条（无 source_ids）", derived_from="distill-judge")
        _chunk("mem-p74-sib", "同生成器的兄弟条（有 source_ids）", derived_from="distill-judge",
               source_ids=json.dumps(["mem-p74-src2"]))
        r_lin = trace(conn, "mem-p74-lin", depth=3)
        # 5.1 我的 source_ids 为空 + 无蒸馏 → 血缘必须为空（修前会串进 mem-p74-src2）
        check("5.1 🔴 同 derived_from 但无 source_ids：血缘为空（不串兄弟的源）",
              r_lin.get("lineage") == [],
              "lineage=%s（修前会错串成 ['mem-p74-src2']）" % r_lin.get("lineage"))
        # 5.2 兄弟条自己的血缘仍正常（约束只砍跨条目并集，不砍本条目 source_ids）
        r_sib = trace(conn, "mem-p74-sib", depth=3)
        check("5.2 兄弟条自身 source_ids 仍正常输出（约束不误伤本条目血缘）",
              r_sib.get("lineage") == ["mem-p74-src2"],
              "lineage=%s" % r_sib.get("lineage"))
        # 5.3 我自己有 source_ids 时照常出（正例：本条目血缘不受影响）
        _chunk("mem-p74-lin2", "我的派生条2（有 source_ids）", derived_from="distill-judge",
               source_ids=json.dumps(["mem-p74-src1"]))
        r_lin2 = trace(conn, "mem-p74-lin2", depth=3)
        check("5.3 本条目 source_ids 非空 → 血缘正常输出",
              r_lin2.get("lineage") == ["mem-p74-src1"], "lineage=%s" % r_lin2.get("lineage"))

        # ----------------------------------------------------------
        # §6. 零写断言（🔴 按单H P7.5 复审口径升级：COUNT 不算数）
        # 为什么升级：单H 复审（2026-09-13）点名"只 COUNT 三表 = 弱断言"——UPDATE 改值、
        #   或往【另一张表】写入，行数照样不变，COUNT 型断言既失明又误绿。单H 给的协同要求是
        #   "须同步升级 trace 零写断言为字段级对拍或 set_trace_callback 钉"。本段两条都上：
        #     ① sqlite3 set_trace_callback 捕获执行过的每条 SQL，凡写语句（INSERT/UPDATE/
        #        DELETE/REPLACE/DROP/ALTER/CREATE…）出现即红——这是"行为钉"，改值也逃不掉；
        #     ② 全表【全字段】快照对拍（每行每列），防有旁路写入绕过 trace 回调（如触发器）。
        #   ⚠️ 既有 xtmem_d3_4_trace_guard_test.py §5 仍是 COUNT 型（单H 判 🔴），本单【未改】
        #      它（工单 §三 要求"别破坏既有断言"，且属另一单范围）——本段是 trace 影子路径的补强。
        # ----------------------------------------------------------
        _WRITE_RE = re.compile(
            r"^\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|VACUUM|REINDEX|TRUNCATE)\b",
            re.IGNORECASE)

        def _snapshot(c):
            """全表全字段快照：{表: (列名元组, 行数据元组)}。用于逐字段对拍。"""
            out = {}
            for t in ("chunks", "memory_evolution", "distill_units"):
                cur = c.execute("SELECT * FROM %s ORDER BY rowid" % t)
                cols = tuple(d[0] for d in cur.description)
                out[t] = (cols, tuple(tuple(r) for r in cur.fetchall()))
            return out

        _seen_writes = []
        conn.set_trace_callback(lambda sql: _seen_writes.append(sql) if _WRITE_RE.match(sql) else None)
        before = _snapshot(conn)
        try:
            # 把本守卫覆盖的所有血缘读取路径都跑一遍（祖先链/后辈链/血缘/可见性）
            trace(conn, "mem-p74-b1", depth=5)          # 祖先链（指针）
            trace(conn, "mem-p74-old", depth=3)         # 后辈链（指针）
            trace(conn, "mem-p74-lin", depth=3)         # 血缘（病灶3 路径）
            trace(conn, "mem-p74-pv", depth=3, viewer="dsh")  # 可见性闸
        finally:
            conn.set_trace_callback(None)   # 撤销回调，防影响后续断言
        after = _snapshot(conn)
        check("6.1 🔴 零写·写语句钉：trace 期间无任何 INSERT/UPDATE/DELETE（set_trace_callback）",
              not _seen_writes, "捕获到写语句=%s" % _seen_writes[:3])
        check("6.2 🔴 零写·字段级对拍：全表全字段快照一致（防改值型写入蒙过 COUNT）",
              before == after, "before!=after（表/字段有变）")
        # 6.3 旧 COUNT 型判据保留一行做对照（说明它为何不够：即使 6.2 绿，6.1 才是行为钉）
        _cnt = {t: len(after[t][1]) for t in after}
        check("6.3 行数对照（COUNT 型只作辅助，单H 已判其不足以证零写）",
              _cnt == {t: len(before[t][1]) for t in before}, "cnt=%s" % _cnt)

        # ----------------------------------------------------------
        # §7. 回归不破既有行为：无血缘/环检测/可见性闸在打标后仍正常
        # ----------------------------------------------------------
        _chunk("mem-p74-solo", "孤立原初记录")
        r_solo = trace(conn, "mem-p74-solo", depth=3)
        check("7.1 无血缘条目仍如实说「无更早的因果链」",
              "无更早的因果链" in r_solo.get("text", ""), "text=%s" % r_solo.get("text", ""))
        # 7.2 可见性闸仍生效：越权根 fail-closed（打标没把 identity 逻辑带歪）
        _chunk("mem-p74-pv", "他人私密", namespace="personal", visibility="private")
        r_pv = trace(conn, "mem-p74-pv", depth=3, viewer="dsh")
        check("7.2 越权根仍 fail-closed（打标未破坏 P7.0 可见性闸）",
              r_pv.get("ok") is False and "他人私密" not in str(r_pv), "res=%s" % str(r_pv)[:200])

        conn.close()
    finally:
        # 还原 XTMEM_DB_PATH（见上方注释：不还原会让后续用例的 conftest 环境闸集体 ERROR）
        if _prior_db_path is None:
            os.environ.pop("XTMEM_DB_PATH", None)
        else:
            os.environ["XTMEM_DB_PATH"] = _prior_db_path
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print("\n=== P7.4 守卫结果：%d/%d passed ===" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_p74_trace_shadow():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
