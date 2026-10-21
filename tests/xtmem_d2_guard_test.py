# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/xtmem_d2_guard_test.py · D2 矛盾检测与影子态治理守卫
#
#   ▍这是什么
#   D1.5 活雷收编与 D2 全量任务的专用回归守卫：
#   - D1.5: lineage_resolve:116 写入新列且指针为空
#   - D2-1: conflict_regression 约束与隔离切分
#   - D2-2: 显式版本下沉、双向打分影子态、两表幂等写入、分歧提示
#   - P7.1: 落库三闸（方向=新取代旧 / visibility=min_visibility / 重判清旧影子边）
#   - 红线: 严禁自动归档任何一方（双活保持）
#
#   ▍修改铁律
#   1. 本文件必须四副本同步（xtmem-project/tests、XTMEM/tests、xtmem-project 镜像、
#      XTMEM-public/tests），sha 不等即视为未交付。
#   2. 断言只用临时夹具库（tempfile），严禁连生产库。
#
#   ▍更新记录
#   *更新：2026-09-11 · P7.1 落库闭环守卫（assistant-c,maintainer派单 P7-A 任务 B）：新增 §8 方向/顺序无关/
#     幂等/可见性两态/重判闭边/error 不闭/已执行边不误删/无 details 时按时间与显式版本序取向。*
#   *更新：2026-09-09 · 创建（D2 全量正式工单，assistant-c）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import importlib.util
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

MASTER = _REPO / "src" / "hermes-plugin"
SCRIPTS = _REPO / "scripts"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}

def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))

def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM D2 事实冲突治理守卫 ===")

    from memory.schema import ensure_xtmem_schema
    from memory.conflict import (
        pair_key_of,
        extract_explicit_version,
        judge_explicit_conflict,
        record_conflict_result,
        get_conflict_warnings_for
    )
    from memory.vector_store import VectorStore

    tmp_dir = tempfile.mkdtemp(prefix="xtmem-d2-guard-")
    db_path = os.path.join(tmp_dir, "guard.db")

    try:
        vs = VectorStore(db_path)
        conn = vs._get_conn()

        # ------------------------------------------------------------
        # §1. 表结构与约束检查
        # ------------------------------------------------------------
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        check("1.1 conflict_regression 表已存在", "conflict_regression" in tables)
        check("1.2 conflict_candidate 表已存在", "conflict_candidate" in tables)

        # 约束测试：非法 label
        bad_label_caught = False
        try:
            conn.execute("""
                INSERT INTO conflict_regression
                (pair_id, a_id, b_id, label, direction, adjudicator, ts, split)
                VALUES ('p1', 'a', 'b', 'invalid_label', 'none', 'guard', 0.0, 'train')
            """)
        except sqlite3.IntegrityError:
            bad_label_caught = True
        check("1.3 conflict_regression label CHECK 拦截非法枚举", bad_label_caught)

        # 约束测试：非法 split
        bad_split_caught = False
        try:
            conn.execute("""
                INSERT INTO conflict_regression
                (pair_id, a_id, b_id, label, direction, adjudicator, ts, split)
                VALUES ('p2', 'a', 'b', 'supersedes', 'none', 'guard', 0.0, 'bad_split')
            """)
        except sqlite3.IntegrityError:
            bad_split_caught = True
        check("1.4 conflict_regression split CHECK 拦截非法划分", bad_split_caught)

        # ------------------------------------------------------------
        # §2. D1.5 lineage_resolve 活雷收编与指针列清洁
        # ------------------------------------------------------------
        now = time.time()
        conn.execute("""
            INSERT INTO chunks (id, content, timestamp, state, tier, derived_from, source_ids, tags, namespace, updated_at)
            VALUES ('mem-s1', 'dead_src', ?, 'archived', 'raw', '', '[]', '[]', 'dsh', ?)
        """, (now, now))
        conn.execute("""
            INSERT INTO chunks (id, content, timestamp, state, tier, derived_from, source_ids, tags, namespace, updated_at)
            VALUES ('mem-der1', 'stale_derived', ?, 'active', 'refined', 'distill', '["mem-s1"]', '["lineage-stale:mem-s1"]', 'dsh', ?)
        """, (now, now))
        conn.commit()

        spec = importlib.util.spec_from_file_location("lin", os.path.join(SCRIPTS, "xtmem_lineage_resolve.py"))
        lin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lin)

        out_lin = lin.resolve_stale(conn, dry_run=False)
        r_der = conn.execute("SELECT state, superseded_by, retire_reason, retired_by, retired_at FROM chunks WHERE id='mem-der1'").fetchone()
        
        check("2.1 lineage_resolve 执行后派生转 archived", r_der["state"] == "archived")
        check("2.2 superseded_by 必须为空串（严禁字串再污染）", r_der["superseded_by"] == "")
        check("2.3 retire_reason 写入 lineage-stale-resolve", r_der["retire_reason"] == "lineage-stale-resolve")
        check("2.4 retired_by 写入 lineage_resolve_cron", r_der["retired_by"] == "lineage_resolve_cron")
        check("2.5 retired_at 记录有效时间戳", r_der["retired_at"] and r_der["retired_at"] > 0)

        # ------------------------------------------------------------
        # §3. 显式冲突下沉规则与优先级 (病灶1：全量版本测试矩阵)
        # ------------------------------------------------------------
        # ⚠️ 3.1/3.2 旧断言于 2026-09-10 D2-p3 修复中被【反转】——
        #   原文断言「content_time 显式冲突规则下沉生效 / 显式新版本胜出」，
        #   那是在断言病灶本身（把时间戳当版本序）。改后语义：内容冲突但无版本语义，
        #   规则层【不得】下沉，应交给 LLM 语义层判——这正是修 bug 的意图。
        c1 = {"id": "m1", "content": "预算为 600 tokens", "content_time": 100.0}
        c2 = {"id": "m2", "content": "预算调整为 400 字符", "content_time": 200.0}
        exp = judge_explicit_conflict(c1, c2)
        check("3.1 🔴 仅时间不同+无版本语义：规则层不得下沉（交 LLM 层）", exp is None)
        check("3.2 🔴 修复后不再有 content_time 版本序赢家",
              exp is None or exp.get("rule") != "explicit_version_priority_3")

        # 优先级校验：外部版本号对比
        c3 = {"id": "m3", "content": "升级到 v1.2 补丁", "content_time": None}
        c4 = {"id": "m4", "content": "升级到 v2.0 补丁", "content_time": None}
        exp_v = judge_explicit_conflict(c3, c4)
        check("3.3 外部版本号比较规则生效", exp_v is not None and exp_v["newer_id"] == "m4")

        # 严禁裸 updated_at 充当版本序
        c5 = {"id": "m5", "content": "普通文本A", "content_time": None, "updated_at": 100.0}
        c6 = {"id": "m6", "content": "普通文本B", "content_time": None, "updated_at": 200.0}
        exp_bare = judge_explicit_conflict(c5, c6)
        check("3.4 严禁裸 updated_at 触发显式版本下沉", exp_bare is None)

        # ----------------------------------------------------------
        # §3.6 D2-p3 回归钉子（2026-09-10，错题本 #46）
        # 病灶：priority 3 曾定义为 content_time>0，把【时间戳】当【版本序】用，
        #   致任何两条 content_time 不同且都>0 的记忆恒判 supersedes（取值空间=
        #   "两条不同的记忆"，恒真）。生产实测：回归集 18 次触发 0 真阳性，
        #   候选表 466 行 supersedes 决策由此产出。
        # 这三条断言是钉子：谁把 content_time 短路放回来，这里立刻红。
        # ----------------------------------------------------------
        # 3.6a 构造性反例（无关记忆 + 不同时间戳，必须不触发）
        x1 = {"id": "x1", "content": "我喜欢桃子汽水", "content_time": 1000.0}
        x2 = {"id": "x2", "content": "今天下雨了", "content_time": 900.0}
        check("3.6a 🔴 无关记忆+不同content_time 不得判 supersedes（p3 已废用）",
              judge_explicit_conflict(x1, x2) is None)
        # 3.6b 同主题但无版本语义的两条，同样不得触发
        x3 = {"id": "x3", "content": "XTMEM 部署偏好是 E 盘", "content_time": 1788000000.0}
        x4 = {"id": "x4", "content": "personal 域判定逻辑保持不变", "content_time": 1787000000.0}
        check("3.6b 🔴 无版本语义的真记忆对不得触发规则下沉",
              judge_explicit_conflict(x3, x4) is None)
        # 3.6c content_time 不得再产生 priority 3（直接钉住提取层）
        p_ct, _ = extract_explicit_version("随便一段话", 1234567890.0)
        check("3.6c 🔴 content_time 不得返回 priority 3", p_ct != 3, "got p=%s" % p_ct)
        # 3.6d 反向：p1/p2 真版本语义必须仍然工作（防修 p3 时误伤）
        y1 = {"id": "y1", "content": "升级到 v1.2 补丁", "content_time": None}
        y2 = {"id": "y2", "content": "升级到 v2.0 补丁", "content_time": None}
        e_y = judge_explicit_conflict(y1, y2)
        check("3.6d p2 真版本语义仍生效（防误伤）", e_y is not None and e_y["newer_id"] == "y2")
        z1 = {"id": "z1", "content": "开启 D1 事实治理批次"}
        z2 = {"id": "z2", "content": "开启 D2 事实治理批次"}
        e_z = judge_explicit_conflict(z1, z2)
        check("3.6e p1 阶段标注仍生效（防误伤）", e_z is not None and e_z["newer_id"] == "z2")

        # 11 样本病灶1前置排除矩阵全覆盖
        matrix = [
            ("宿主机 IP 100.121.34.56 配置完成", 0),
            ("创建于 2026-07-27 日志记录", 0),
            ("模型准确率达到 99.9%", 0),
            ("年份 2026 计划", 0),
            ("监听端口 8080 服务正常", 0),
            ("发布了真版本 v1.2 补丁", 2),
            ("系统运行在 Python 裸 3.11 环境", 0),
            ("将系统版本 3.11 写入文档", 2),
            ("最新版本为 V2.1.3 稳定版", 2),
            ("进入 Phase 2 演练阶段", 1),
            ("开启 D1 事实治理批次", 1)
        ]
        mat_pass = True
        for m_txt, m_exp in matrix:
            p_val, _ = extract_explicit_version(m_txt)
            if p_val != m_exp:
                mat_pass = False
                break
        check("3.5 病灶1版本提取11样本矩阵全部断言通过", mat_pass)

        # ------------------------------------------------------------
        # §4. 写入幂等与 pair_key 一致性
        # ------------------------------------------------------------
        k1 = pair_key_of("mem-b2222222", "mem-a1111111")
        k2 = pair_key_of("mem-a1111111", "mem-b2222222")
        check("4.1 pair_key 对称一致性", k1 == k2 and k1 == "mem-a1111111|mem-b2222222")

        res_mock = {
            "is_conflict": True,
            "candidate_score": 0.88,
            "llm_score": 0.88,
            "decision": "supersedes",
            "policy_version": "D2.2-v1",
            "model": "mock",
            "prompt_hash": "hash"
        }
        # 写入两次
        record_conflict_result(conn, {"id": "mem-a1111111", "namespace": "dsh"}, {"id": "mem-b2222222", "namespace": "dsh"}, res_mock)
        record_conflict_result(conn, {"id": "mem-a1111111", "namespace": "dsh"}, {"id": "mem-b2222222", "namespace": "dsh"}, res_mock)
        
        c_count = conn.execute("SELECT count(*) FROM conflict_candidate WHERE pair_key=?", (k1,)).fetchone()[0]
        check("4.2 conflict_candidate 重复写入幂等（行数=1）", c_count == 1)

        # ------------------------------------------------------------
        # §5. 影子态红线：双活保持，严禁自动归档
        # ------------------------------------------------------------
        conn.execute("""
            INSERT INTO chunks (id, content, timestamp, state, tier, updated_at)
            VALUES ('mem-c1', '活事实1', ?, 'active', 'refined', ?),
                   ('mem-c2', '活事实2', ?, 'active', 'refined', ?)
        """, (now, now, now, now))
        conn.commit()

        record_conflict_result(conn, {"id": "mem-c1", "namespace": "dsh"}, {"id": "mem-c2", "namespace": "dsh"}, res_mock)

        st1 = conn.execute("SELECT state FROM chunks WHERE id='mem-c1'").fetchone()[0]
        st2 = conn.execute("SELECT state FROM chunks WHERE id='mem-c2'").fetchone()[0]
        check("5.1 判定冲突后 chunk A 保持 active", st1 == "active")
        check("5.2 判定冲突后 chunk B 保持 active（红线：禁自动归档）", st2 == "active")

        # 检查 memory_evolution 写入了 shadow_state=1 的边
        evo_row = conn.execute("""
            SELECT shadow_state, relation, adjudicator FROM memory_evolution
            WHERE source_id='mem-c1' AND target_id='mem-c2'
        """).fetchone()
        check("5.3 memory_evolution 写入影子边且 shadow_state=1", evo_row is not None and evo_row["shadow_state"] == 1)

        # ------------------------------------------------------------
        # §6. 分歧警告装配测试
        # ------------------------------------------------------------
        warn_map = get_conflict_warnings_for(conn, ["mem-c1", "mem-other"])
        check("6.1 分歧提示查询正确命中对手条目", "mem-c1" in warn_map and "mem-c2" in warn_map["mem-c1"])
        check("6.2 无冲突条目不产生分歧提示", "mem-other" not in warn_map)

        # ------------------------------------------------------------
        # §7. 静态扫描：全树 live 脚本禁字串污染再现
        # ------------------------------------------------------------
        scan_scripts = [
            os.path.join(SCRIPTS, "xtmem_lineage_resolve.py"),
            os.path.join(SCRIPTS, "xtmem_conflict_scan.py"),
            os.path.join(SCRIPTS, "xtmem_shadow_rejudge.py")
        ]
        has_dirty_ptr = False
        for s_path in scan_scripts:
            if os.path.exists(s_path):
                txt = open(s_path, "r", encoding="utf-8").read()
                if "superseded_by='lineage-stale-resolve'" in txt:
                    has_dirty_ptr = True
        check("7.1 核心脚本无 'lineage-stale-resolve' 字串残留", not has_dirty_ptr)

        # ------------------------------------------------------------
        # §8. 🔴 P7.1 D2 落库闭环（2026-09-11 派单 P7-A 任务 B）
        # 病灶：judge_explicit_conflict 已算好 newer_id/older_id，但 record_conflict_result 不用——
        #   ① 方向由扫描顺序决定 ② 恒可见 shared ③ 重判后旧 supersedes 影子边永久残留。
        # 本节是这条的钉子：谁把方向逻辑改回去（或把判定注到别处没打中真实写入路径），
        #   8.1/8.2 立刻红；谁把 visibility 写回 'shared'，8.3/8.4 立刻红。
        # ------------------------------------------------------------
        from memory.conflict import evaluate_pair_bidirectional, load_tuning_config
        _cfg = load_tuning_config()

        def _edge_of(x, y):
            """取该 pair（任一方向）的影子边 → [(source, target, vis, shadow), ...]。"""
            return [tuple(r) for r in conn.execute(
                "SELECT source_id, target_id, visibility, shadow_state FROM memory_evolution "
                "WHERE (source_id=? AND target_id=?) OR (source_id=? AND target_id=?)",
                (x, y, y, x)).fetchall()]

        def _mkchunk(cid, content, vis, ns="dsh"):
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, content, timestamp, state, tier, namespace, "
                "visibility, updated_at) VALUES (?, ?, ?, 'active', 'refined', ?, ?, ?)",
                (cid, content, now, ns, vis, now),
            )
            conn.commit()

        # 8.1 规则路径方向：A=v1.2（旧）/ B=v2.0（新）→ 边必须 source=B(新) → target=A(旧)
        # ⚠️ 两条夹具时间戳完全相同且无 content_time → 方向【只能】来自 details.newer_id/older_id；
        #    若实现退回传参顺序，这里必然得到 A→B 而变红（真正的反向验证钉子）。
        conn.execute("DELETE FROM memory_evolution WHERE source_id LIKE 'mem-p7%' OR target_id LIKE 'mem-p7%'")
        conn.commit()
        _mkchunk("mem-p7A", "项目版本 v1.2", "shared")
        _mkchunk("mem-p7B", "项目版本 v2.0", "shared")
        _pa = {"id": "mem-p7A", "content": "项目版本 v1.2", "namespace": "dsh"}
        _pb = {"id": "mem-p7B", "content": "项目版本 v2.0", "namespace": "dsh"}
        _r81 = evaluate_pair_bidirectional(_pa, _pb, _cfg)
        record_conflict_result(conn, _pa, _pb, _r81)
        _e81 = _edge_of("mem-p7A", "mem-p7B")
        check("8.1 🔴 方向：A=v1.2 旧 / B=v2.0 新 → 边 source=B(新) → target=A(旧)",
              _r81.get("model") == "rule_explicit_version"
              and len(_e81) == 1 and _e81[0][0] == "mem-p7B" and _e81[0][1] == "mem-p7A",
              "model=%s edges=%s" % (_r81.get("model"), _e81))

        # 8.2 传参顺序无关 + 幂等：swap 传参重写，仍是同一条边且不产生第二条
        _r82 = evaluate_pair_bidirectional(_pb, _pa, _cfg)
        record_conflict_result(conn, _pb, _pa, _r82)
        _e82 = _edge_of("mem-p7A", "mem-p7B")
        check("8.2 🔴 传参顺序无关 + 幂等：swap 后仍只有 source=B → target=A 一条边",
              len(_e82) == 1 and _e82[0][0] == "mem-p7B" and _e82[0][1] == "mem-p7A",
              "edges=%s" % (_e82,))

        # 8.3 可见性 = min_visibility（取最严者），不再恒 shared
        _mkchunk("mem-p7C", "私密版本 v1.0", "private", ns="zcode")
        _mkchunk("mem-p7D", "名单版本 v2.1", "shared-with:dsh", ns="dsh")
        _pc = {"id": "mem-p7C", "content": "私密版本 v1.0", "namespace": "zcode"}
        _pd = {"id": "mem-p7D", "content": "名单版本 v2.1", "namespace": "dsh"}
        _r83 = evaluate_pair_bidirectional(_pc, _pd, _cfg)
        record_conflict_result(conn, _pc, _pd, _r83)
        _e83 = _edge_of("mem-p7C", "mem-p7D")
        check("8.3 🔴 可见性 = min_visibility（private × shared-with → private，不能恒 shared）",
              len(_e83) == 1 and _e83[0][2] == "private", "edges=%s" % (_e83,))

        # 8.4 可见性不放大：shared × shared-with → shared-with（取更严那侧）
        _mkchunk("mem-p7E", "公共版本 v3.0", "shared")
        _mkchunk("mem-p7F", "名单版本 v3.1", "shared-with:dsh,zcode")
        _pe = {"id": "mem-p7E", "content": "公共版本 v3.0", "namespace": "dsh"}
        _pf = {"id": "mem-p7F", "content": "名单版本 v3.1", "namespace": "dsh"}
        _r84 = evaluate_pair_bidirectional(_pe, _pf, _cfg)
        record_conflict_result(conn, _pe, _pf, _r84)
        _e84 = _edge_of("mem-p7E", "mem-p7F")
        check("8.4 可见性不放大：shared × shared-with → shared-with",
              len(_e84) == 1 and str(_e84[0][2]).startswith("shared-with:"), "edges=%s" % (_e84,))

        # 8.5 重判 enriches → 该 pair 旧 supersedes 影子边 0 残留
        record_conflict_result(conn, _pa, _pb, {
            "is_conflict": False, "candidate_score": 0.1, "llm_score": 0.1,
            "decision": "enriches", "model": "mock", "policy_version": _cfg["policy_version"],
            "prompt_hash": ""})
        _e85 = _edge_of("mem-p7A", "mem-p7B")
        check("8.5 🔴 重判 enriches：该 pair 旧 supersedes 影子边 0 残留",
              len(_e85) == 0, "edges=%s" % (_e85,))

        # 8.6 decision='error'（没判成，如 LLM 抛异常）不删既有影子边——网络抖动不得抹掉判定
        record_conflict_result(conn, _pa, _pb, evaluate_pair_bidirectional(_pa, _pb, _cfg))
        _before_err = len(_edge_of("mem-p7A", "mem-p7B"))
        record_conflict_result(conn, _pa, _pb, {"is_conflict": False, "decision": "error",
                                                "model": "m", "policy_version": _cfg["policy_version"]})
        _after_err = len(_edge_of("mem-p7A", "mem-p7B"))
        check("8.6 decision='error' 不删既有影子边（只重判才覆盖）",
              _before_err == 1 and _after_err == 1,
              "before=%s after=%s" % (_before_err, _after_err))

        # 8.7 🔴 闭边只清影子边：已执行边（shadow_state=0）不被误删
        conn.execute("DELETE FROM memory_evolution WHERE source_id='mem-p7G' OR target_id='mem-p7G'")
        conn.commit()
        _mkchunk("mem-p7G", "已执行旧条", "shared")
        _mkchunk("mem-p7H", "已执行新条", "shared")
        conn.execute(
            "INSERT INTO memory_evolution (source_id, target_id, relation, confidence, visibility, "
            "valid_from, valid_to, shadow_state) VALUES ('mem-p7H','mem-p7G','supersedes',0.9,'shared',?,0.0,0)",
            (now,))
        conn.commit()
        record_conflict_result(conn, {"id": "mem-p7G", "content": "已执行旧条"},
                               {"id": "mem-p7H", "content": "已执行新条"},
                               {"is_conflict": False, "decision": "enriches", "model": "m",
                                "policy_version": _cfg["policy_version"]})
        _exe = conn.execute(
            "SELECT COUNT(*) FROM memory_evolution WHERE shadow_state=0 AND "
            "((source_id='mem-p7G' AND target_id='mem-p7H') OR "
            "(source_id='mem-p7H' AND target_id='mem-p7G'))").fetchone()[0]
        check("8.7 🔴 闭边只清影子边：已执行边（shadow_state=0）不被误删", _exe == 1, "count=%s" % _exe)

        # 8.8 LLM 路径（无 details）按时间信号显式取向：时间更新的那条当 source
        conn.execute("DELETE FROM memory_evolution WHERE source_id LIKE 'mem-p7t%' OR target_id LIKE 'mem-p7t%'")
        conn.commit()
        _pt_a = {"id": "mem-p7ta", "content": "旧内容无版本语义", "timestamp": 100.0}
        _pt_b = {"id": "mem-p7tb", "content": "新内容无版本语义", "timestamp": 200.0}
        record_conflict_result(conn, _pt_a, _pt_b, {
            "is_conflict": True, "llm_score": 0.9, "decision": "supersedes",
            "model": "llm_bidirectional", "policy_version": _cfg["policy_version"]})
        _e88 = _edge_of("mem-p7ta", "mem-p7tb")
        check("8.8 LLM 路径按时间取向：B(t=200) 更新 → source=B（不再由传参顺序决定）",
              len(_e88) == 1 and _e88[0][0] == "mem-p7tb" and _e88[0][1] == "mem-p7ta",
              "edges=%s" % (_e88,))

        # 8.9 🔴 手工造的 res（无 details、无时间），但内容带显式版本序 → 方向仍须 source=B(新)。
        #     这一档专打"审计复现脚本的调用约定"：res 是手搓的、不走 evaluate_pair_bidirectional。
        #     若落库方只会读 details，这里会退回传参顺序 → A→B，红。
        conn.execute("DELETE FROM memory_evolution WHERE source_id LIKE 'mem-p7v%' OR target_id LIKE 'mem-p7v%'")
        conn.commit()
        _mkchunk("mem-p7vA", "项目 A 客户端 v1.2", "shared")
        _mkchunk("mem-p7vB", "项目 A 客户端 v2.0", "shared")
        record_conflict_result(conn, {"id": "mem-p7vA", "content": "项目 A 客户端 v1.2"},
                               {"id": "mem-p7vB", "content": "项目 A 客户端 v2.0"},
                               {"is_conflict": True, "llm_score": 1.0, "decision": "supersedes",
                                "model": "rule_explicit_version",
                                "policy_version": _cfg["policy_version"]})
        _e89 = _edge_of("mem-p7vA", "mem-p7vB")
        check("8.9 🔴 手工 res 无 details：内容显式版本序仍定方向 source=B(新) → target=A(旧)",
              len(_e89) == 1 and _e89[0][0] == "mem-p7vB" and _e89[0][1] == "mem-p7vA",
              "edges=%s" % (_e89,))

    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== D2 守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0

def test_d2_guard():
    assert _guard() == 0

if __name__ == "__main__":
    sys.exit(_guard())
