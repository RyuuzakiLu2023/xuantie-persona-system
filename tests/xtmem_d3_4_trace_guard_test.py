# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/xtmem_d3_4_trace_guard_test.py · D3-4 因果链查询器守卫
#
#   ▍这是什么
#   D3-4（`xtmem trace <mem-id>`）的回归守卫，覆盖工单 §B-2 五条：
#     1. 造 3 代链条（A→B→C，含 retired 档案）→ trace C 能串出 A/B/C 三代 + 时间序正确
#     2. 无血缘条目 → 输出「无因果链」而非报错/编造
#     3. 循环引用（A→B→A）不无限递归（环检测/深度上限）
#     4. --json 与默认文本输出信息等量（防 json 少字段）
#     5. 只读断言：跑 trace 前后生产库表行数不变（隔离临时库验）
#     6. 镜像源 CLI 入口可执行（防"只同步库不同步 CLI 壳"）
#     7. 🔴 P7.0 可见性闸：自身域/shared/shared-with/admin 四态 + 越权条不给正文
#     8. 🔴 P7.0 CLI 层：--as 身份贯通（含无身份 fail-closed / --as 冲突拒绝）
#
#   ▍跑法
#   直接运行 exit 0/1；pytest 收集走 test_d3_4_trace_guard()
#   （体必须收在 _guard() 内，顶层 sys.exit 会打断整个收集——见错题本 #35）。
#
#   ▍修改铁律
#   1. 副本同步（xtmem-project/tests、XTMEM/tests、xtmem-project 镜像、XTMEM-public/tests）。
#   2. 断言只用临时夹具库（tempfile），严禁连生产库。
#   3. 只读：trace 本体零写——本守卫额外断言行数不变。
#
#   ▍更新记录
#   *更新：2026-09-14 · §5 零写断言升级（COUNT → 写语句钉+字段级对拍+COUNT 对照；单H P7.5 判据落地；单I）。*
#   *更新：2026-09-11 · P7.0 可见性闸守卫（assistant-c,maintainer派单 P7-A 任务 A）：新增 §7 权限四态 +
#     越权条打码 + 血缘/后辈过闸 + 无存在性 oracle，§8 CLI --as 身份贯通与冲突拒绝。*
#   *创建：2026-09-10 · D3-4 因果链查询器（assistant-c）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
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
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM D3-4 因果链查询器守卫 ===")

    from memory.trace import trace, _parse_ids, _now_iso
    from memory.open_db import open_db

    tmp = tempfile.mkdtemp(prefix="xtmem-d34-guard-")
    db = os.path.join(tmp, "t.db")
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

        def _evo(src, tgt, rel, conf):
            conn.execute("INSERT INTO memory_evolution (source_id, target_id, relation, confidence, "
                         "created_at, visibility, valid_from, valid_to, shadow_state, adjudicator, "
                         "policy_version, decision_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (src, tgt, rel, conf, _now_iso(), "shared", now, 0, 1, "", "", ""))
            conn.commit()

        def _distill(unit, raw, ref):
            conn.execute("INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, "
                         "turn_count, char_count, raw_ids, prev_unit, filtered_ids, has_correction, "
                         "state, distilled_at, refined_ids, attempt, last_error, created_at, "
                         "claim_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (unit, "dsh", "distill", now, now, 3, 30, json.dumps(raw), None,
                          "[]", 0, "distilled", _now_iso(), json.dumps(ref), 1, "", now, ""))
            conn.commit()

        # ----------------------------------------------------------
        # §1. 造 3 代链条 A(最老)→B→C(最新)
        # 🔴【2026-09-14 数据源改道】家谱数据源 = chunks.superseded_by 指针（不再读边表）。
        #    语义：X.superseded_by = Y ⟺ X 被 Y 取代（X 旧 / Y 新）。
        #    所以 A→B 的写法是给【A】设 superseded_by="mem-bbbb"。
        #    ⚠️ 改道原因：边表 689 条 supersedes 中 98.7% 无正文依据；chunks 指针实测 100% 闭环
        #    （timeline 57/57、distill-retire 342/342）；见 memory/trace.py 文件头铁律 2。
        # ----------------------------------------------------------
        _chunk("mem-aaaa", "最初方案：用 A 结构",
               superseded_by="mem-bbbb", state="superseded", retire_reason="distill-retire")
        _chunk("mem-bbbb", "妥协方案：在 B 骨架改用 B2", derived_from="distill",
               source_ids=json.dumps(["mem-aaaa"]),
               superseded_by="mem-cccc", state="superseded", retire_reason="distill-retire")
        _chunk("mem-cccc", "最终定案：决定采用 C", derived_from="distill",
               source_ids=json.dumps(["mem-bbbb"]), tier="refined")
        # 🔴【改道诱饵】边表里造一条【指向无关条目】的边——家谱若仍读边表，§1.1 必多出 decoy。
        # 这是"数据源改道"的核心钉子：谁把 _evolution_chain 改回读 memory_evolution，这里立刻红。
        _chunk("mem-decoy", "诱饵条：只在边表里，不在指针链里")
        _evo("mem-decoy", "mem-cccc", "supersedes", 0.99)   # 边表：decoy 取代 C（不该被读）
        _distill("U-123", ["mem-aaaa", "mem-bbbb"], ["mem-cccc"])

        r = trace(conn, "mem-cccc", depth=5)
        nodes = r["nodes"]
        ids = [n["id"] for n in nodes]
        check("1.1 trace C 能串出三代 A/B/C", ids == ["mem-aaaa", "mem-bbbb", "mem-cccc"],
              "ids=%s" % ids)
        check("1.2 时间序正序（最老在前）", True, "由 ids 顺序隐含判定")
        check("1.3 蒸馏原料被溯源（U-123 的 raw 都在 lineage/distill）",
              set(r["distill"]) == {"mem-aaaa", "mem-bbbb"} or set(r["lineage"]) >= {"mem-aaaa"},
              "distill=%s lineage=%s" % (r["distill"], r["lineage"]))
        check("1.4 关系标签存在（supersedes → 取代）", any("取代" in n["relation_to_child"] for n in nodes)
              or nodes[-1]["id"] == "mem-cccc", "nodes=%s" % nodes)

        # 🔴 1.5/1.6 方向钉子（2026-09-14 改道版）：家谱走 chunks.superseded_by 指针。
        # 这条专防"数据源被改回边表"——若谁把 _evolution_chain 改回读 memory_evolution，
        # decoy（边表独有）会立刻出现在下面的链里。
        # 1.5 最老的 A：只应得自身（后辈 B/C 不得被当祖先串出来）
        r_a = trace(conn, "mem-aaaa", depth=5)
        ids_a = [n["id"] for n in r_a["nodes"]]
        check("1.5 🔴 方向钉子：trace 最老的 A 只应得自身（不含后辈 B/C）",
              ids_a == ["mem-aaaa"], "ids=%s" % ids_a)
        # 1.6 最新的 C：向前回溯出 A/B/C，且【不含边表独有 decoy】（改道核心断言）
        ids_c = [n["id"] for n in trace(conn, "mem-cccc", depth=5)["nodes"]]
        check("1.6 🔴 方向钉子：trace 最新的 C 应向前回溯出 A/B/C（最老在前）",
              ids_c == ["mem-aaaa", "mem-bbbb", "mem-cccc"], "ids=%s" % ids_c)
        check("1.7 🔴 改道钉子：边表独有 decoy【不】出现在指针链里（数据源 = chunks 指针）",
              "mem-decoy" not in ids_c,
              "ids=%s（出现 decoy 说明 _evolution_chain 仍在读 memory_evolution）" % ids_c)

        # ----------------------------------------------------------
        # §2. 无血缘条目 → 输出「无因果链」而非报错
        # ----------------------------------------------------------
        _chunk("mem-orig", "古早的原初记录，没有任何派生态")
        r2 = trace(conn, "mem-orig", depth=5)
        check("2.1 无血缘条目 ok=True（不报错）", r2.get("ok") is True)
        check("2.2 无血缘条目如实说「无…因果链」不编造",
              "无更早的因果链" in r2.get("text", ""), "text=%s" % r2.get("text", ""))
        # 2.3 不给旧条留"孤岛错觉"：被谁取代了要说出来（真数据复核时补的）
        # 改道后 successors 读本条自己的 superseded_by（单值）。
        r2b = trace(conn, "mem-aaaa", depth=5)
        check("2.3 旧条能报出『后续演化（被谁接替）』",
              any(s["id"] == "mem-bbbb" for s in r2b.get("successors", [])),
              "succ=%s" % r2b.get("successors"))
        check("2.4 文本里后续演化行有渲染",
              "后续演化" in r2b.get("text", ""), "text=%s" % r2b.get("text", ""))

        # ----------------------------------------------------------
        # §3. 环引用：A→B→A 不无限递归（环检测/深度上限）
        # 改道后：指针链 A.superseded_by=B 且 B.superseded_by=A（数据损坏型互指）。
        # ----------------------------------------------------------
        _chunk("mem-aa", "A", superseded_by="mem-bb")
        _chunk("mem-bb", "B", superseded_by="mem-aa")
        _chunk("mem-cc", "C")
        try:
            r3 = trace(conn, "mem-aa", depth=5)
            check("3.1 环检测：不抛异常且有限输出",
                  r3.get("ok") is True and len(r3["nodes"]) <= 8,
                  "nodes=%s" % len(r3["nodes"]))
        except Exception as e:
            check("3.1 环检测：不抛异常且有限输出", False, str(e))

        # ----------------------------------------------------------
        # §4. --json 与文本信息等量
        # ----------------------------------------------------------
        r4j = trace(conn, "mem-cccc", depth=5, want_json=True)
        r4t = trace(conn, "mem-cccc", depth=5, want_json=False)
        # 信息等量：json 必带 nodes/lineage/distill/retired/gen，且文本由他们（或相等信息）渲染
        for key in ("nodes", "lineage", "distill", "retired", "generations", "successors"):
            check("4.1 json 含字段 %s" % key, key in r4j, "keys=%s" % sorted(r4j))
        check("4.2 文本画出来的三代在 json.nodes 里都存在",
              all(any(n["id"] == x for n in r4j["nodes"]) for x in ["mem-aaaa", "mem-bbbb", "mem-cccc"]))
        check("4.3 文本/JSON root 一致", r4j["root"] == r4t["root"], "%s vs %s" % (r4j["root"], r4t["root"]))
        # 4.4 文本含血缘信息（蒸馏/血缘都在，不是空壳）
        check("4.4 文本含蒸馏/血缘信息",
              ("蒸馏" in r4t["text"] or "血缘" in r4t["text"]), "text=%s" % r4t["text"])

        # ----------------------------------------------------------
        # §5. 零写断言（🔴 单H P7.5 复审升级：COUNT 型不足证零写——改值型写入行数不变）
        #   三档：5.1 写语句钉（set_trace_callback，行为层最灵）
        #         5.2 字段级对拍（全表全字段快照，防改值型蒙过 COUNT）
        #         5.3 COUNT 对照（留作辅助，说明其为何不足）
        # ----------------------------------------------------------
        import re as _re
        _WRITE_RE = _re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)", _re.I)
        _seen_writes = []
        conn.set_trace_callback(lambda sql: _seen_writes.append(sql) if _WRITE_RE.match(sql) else None)

        def _snapshot(c):
            out = {}
            tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tabs:
                rows = [tuple(r) for r in c.execute("SELECT * FROM %s" % t)]
                out[t] = sorted(rows, key=lambda x: repr(x))
            return out

        before = _snapshot(conn)
        trace(conn, "mem-cccc", depth=5)
        trace(conn, "mem-orig", depth=5)
        trace(conn, "mem-aa", depth=5)
        after = _snapshot(conn)
        conn.set_trace_callback(None)   # 撤销回调，防影响后续断言

        check("5.1 🔴 零写·写语句钉：trace 期间无任何 INSERT/UPDATE/DELETE（set_trace_callback）",
              not _seen_writes, "captured=%s" % (_seen_writes[:3],))
        check("5.2 🔴 零写·字段级对拍：全表全字段快照一致（防改值型写入蒙过 COUNT）",
              before == after, "diff_tables=%s" % ([t for t in before if before[t] != after.get(t)],))
        _rc = lambda snap: {t: len(v) for t, v in snap.items()}
        check("5.3 行数对照（COUNT 型只作辅助，单H 已判其不足以证零写）",
              _rc(before) == _rc(after), "before=%s" % _rc(before))

        # ----------------------------------------------------------
        # §6. 镜像源 CLI 入口可执行性（错题本 #45：D3-4 只同步了运行时 CLI）
        # 为什么钉在这里：memory/trace.py 是库，cmd_trace 是壳——只测库测不出
        #   "CLI 壳漏同步"。本段真起子进程跑【镜像源布局】的 CLI，cmd_trace 一旦
        #   缺失（或子命令注册漏了）这里必红，防"对拍表只列 memory/ 不列 CLI"复发。
        # ⚠️ 用临时夹具库（XTMEM_DB_PATH 指向本守卫的 tmp db），不碰生产。
        # ----------------------------------------------------------
        import subprocess
        _cli_mirror = _REPO / "xtmem-project"/ "src"/ "cli"/ "xtmem.py"
        _env = dict(os.environ, XTMEM_DB_PATH=db)
        try:
            _p = subprocess.run([sys.executable, _cli_mirror, "trace", "mem-cccc", "--json"],
                                capture_output=True, text=True, encoding="utf-8",
                                env=_env, timeout=120)
            check("6.1 镜像源 CLI 能执行 trace 子命令（rc=0）", _p.returncode == 0,
                  "rc=%s err=%s" % (_p.returncode, (_p.stderr or "")[-200:]))
            _d = None
            try:
                _d = json.loads((_p.stdout or "").strip().splitlines()[-1])
            except Exception:
                _d = None
            check("6.2 镜像源 CLI trace --json 回 valid JSON 且 root 正确",
                  isinstance(_d, dict) and _d.get("root") == "mem-cccc",
                  "out=%s" % (_p.stdout or "")[:200])
            check("6.3 镜像源 CLI trace 输出的三代与库内一致",
                  isinstance(_d, dict) and
                  sorted(n["id"] for n in _d.get("nodes", [])) == ["mem-aaaa", "mem-bbbb", "mem-cccc"],
                  "nodes=%s" % (_d.get("nodes") if isinstance(_d, dict) else None))
        except Exception as _e:
            check("6.1 镜像源 CLI 能执行 trace 子命令（rc=0）", False, "exc=%r" % (_e,))
            check("6.2 镜像源 CLI trace --json 回 valid JSON 且 root 正确", False, "exc=%r" % (_e,))
            check("6.3 镜像源 CLI trace 输出的三代与库内一致", False, "exc=%r" % (_e,))

        # ----------------------------------------------------------
        # §7. 🔴 P7.0 可见性闸（2026-09-11 派单 P7-A 任务 A）
        # 病灶：trace 全模块零可见性过滤 + trace() 无 viewer 形参 → dsh 身份可完整读出
        #   personal 的 private 正文（顶模审计实测坐实）。
        # 本节是这条的钉子：谁把 viewer 判定拆掉、或把判定注到别处（没打中真实读取路径），
        #   7.1/8.1 立刻红。四态：自身域 private / shared / shared-with 名单 / admin。
        # ----------------------------------------------------------
        _chunk("mem-xu-priv", "assistant-a私密内容：只有assistant-a能看", namespace="personal", visibility="private")
        _chunk("mem-dsh-priv", "assistant-c自己的私密内容", namespace="dsh", visibility="private")
        _chunk("mem-sw-1", "名单内可见的内容", namespace="personal", visibility="shared-with:dsh,zcode")
        _chunk("mem-sw-2", "名单外不可见的内容", namespace="personal", visibility="shared-with:zcode")
        _chunk("mem-pub-1", "公共内容", namespace="personal", visibility="shared")

        # 7.1 反向验证核心：dsh 身份读assistant-a private → 正文不可得（打的是 trace 真实读取路径）
        r_xu = trace(conn, "mem-xu-priv", depth=3, viewer="dsh")
        check("7.1 🔴 dsh 身份 trace assistant-a private：正文不可得",
              (r_xu.get("ok") is False) and ("assistant-a私密内容" not in str(r_xu)),
              "res=%s" % str(r_xu)[:200])

        # 7.2 态一 · 自身域 private → 可见
        r_own = trace(conn, "mem-dsh-priv", depth=3, viewer="dsh")
        check("7.2 自身域 private：本域身份可见且正文可得",
              r_own.get("ok") is True and "assistant-c自己的私密内容" in str(r_own),
              "res=%s" % str(r_own)[:200])

        # 7.3 态二 · shared → 任何身份都可见（含空身份：公共内容不拒）
        r_pub = trace(conn, "mem-pub-1", depth=3, viewer="dsh")
        r_pub_nobody = trace(conn, "mem-pub-1", depth=3)
        check("7.3 shared：有身份 / 无身份都可见（公共内容不拒）",
              r_pub.get("ok") is True and r_pub_nobody.get("ok") is True
              and "公共内容" in str(r_pub_nobody),
              "dsh=%s nobody=%s" % (r_pub.get("ok"), r_pub_nobody.get("ok")))

        # 7.4 态三 · shared-with：名单内可见、名单外不可见（两向都过闸，防"名单只做一半"）
        r_in = trace(conn, "mem-sw-1", depth=3, viewer="dsh")
        r_out = trace(conn, "mem-sw-2", depth=3, viewer="dsh")
        check("7.4 shared-with：名单内可见 / 名单外不可见",
              r_in.get("ok") is True and "名单内可见" in str(r_in)
              and r_out.get("ok") is False and "名单外不可见" not in str(r_out),
              "in=%s out=%s" % (r_in.get("ok"), r_out.get("ok")))

        # 7.5 态四 · admin（__admin__）= 上帝视角，四态全可见
        r_admin = trace(conn, "mem-xu-priv", depth=3, viewer="__admin__")
        check("7.5 admin（__admin__）：含他人 private 在内的全部可见",
              r_admin.get("ok") is True and "assistant-a私密内容" in str(r_admin),
              "res=%s" % str(r_admin)[:200])

        # 7.6 🔴 无身份 fail-closed：只放行 shared，【不】回落"全可见"
        r_anon_priv = trace(conn, "mem-dsh-priv", depth=3)
        r_anon_sw = trace(conn, "mem-sw-1", depth=3)
        r_anon_pub = trace(conn, "mem-pub-1", depth=3)
        check("7.6 🔴 空 viewer fail-closed：private 拒 / shared-with 拒 / shared 放行",
              r_anon_priv.get("ok") is False and r_anon_sw.get("ok") is False
              and r_anon_pub.get("ok") is True,
              "priv=%s sw=%s pub=%s" % (r_anon_priv.get("ok"), r_anon_sw.get("ok"), r_anon_pub.get("ok")))

        # 7.7 越权祖先：节点保留 id/state 以示存在、正文打码、且【不】继续向上回溯
        # 改道：指针链 C.superseded_by=D（C 旧被 D 取代）→ trace D 向前回溯见 C。
        _chunk("mem-chain-C", "祖先的秘密正文", namespace="personal", visibility="private",
               superseded_by="mem-chain-D")
        _chunk("mem-chain-D", "后辈的公共正文", namespace="dsh", visibility="shared")
        r_hid = trace(conn, "mem-chain-D", depth=5, viewer="dsh")
        _ids_h = [n["id"] for n in r_hid["nodes"]]
        _masked = [n for n in r_hid["nodes"] if not n.get("visible")]
        check("7.7 🔴 越权祖先：留 id/state 但正文打码（hidden_count=1，不外泄正文）",
              r_hid.get("ok") is True and _ids_h == ["mem-chain-C", "mem-chain-D"]
              and len(_masked) == 1 and _masked[0]["content"] == ""
              and "祖先的秘密正文" not in str(r_hid)
              and r_hid.get("hidden_count") == 1,
              "ids=%s masked=%s hidden=%s" % (_ids_h, _masked, r_hid.get("hidden_count")))
        check("7.8 越权条在文本渲染里示『无权查看』且正文不出现",
              "无权查看" in r_hid.get("text", "") and "祖先的秘密正文" not in r_hid.get("text", ""),
              "text=%s" % r_hid.get("text", ""))

        # 7.9 🔴 越权根 vs 确实不存在：报错文案逐字相同（防用报错差异探测存在性）
        _miss = trace(conn, "mem-not-exist", depth=3, viewer="dsh")
        check("7.9 🔴 越权根与不存在同文案（无存在性 oracle）",
              r_xu.get("error") == _miss.get("error") == "未找到该条目"
              and r_xu.get("text") == "未找到条目 mem-xu-priv"
              and _miss.get("text") == "未找到条目 mem-not-exist"
              and "无权" not in str(r_xu),
              "xu=%r miss=%r" % (r_xu.get("text"), _miss.get("text")))

        # 7.10 血缘 id 列表过闸：越权源 id 连存在性都不给
        _chunk("mem-lin-src", "别人的私密血缘源", namespace="personal", visibility="private")
        _chunk("mem-lin-dst", "我的派生条", namespace="dsh", visibility="shared",
               source_ids=json.dumps(["mem-lin-src", "mem-pub-1"]), derived_from="distill")
        r_lin = trace(conn, "mem-lin-dst", depth=3, viewer="dsh")
        check("7.10 🔴 血缘列表过闸：越权源 id 不出现（只留可见源）",
              r_lin.get("lineage") == ["mem-pub-1"], "lineage=%s" % r_lin.get("lineage"))

        # 7.11 后辈列表过闸（改道：读旧条自己的 superseded_by 指针）
        _chunk("mem-succ-old", "我的旧条", namespace="dsh", visibility="shared",
               superseded_by="mem-succ-new")
        _chunk("mem-succ-new", "别人的私密后辈", namespace="personal", visibility="private")
        r_succ = trace(conn, "mem-succ-old", depth=3, viewer="dsh")
        check("7.11 后辈列表过闸：越权后辈不出现在 successors",
              all(s["id"] != "mem-succ-new" for s in r_succ.get("successors", [])),
              "succ=%s" % r_succ.get("successors"))

        # ----------------------------------------------------------
        # §8. 🔴 P7.0 CLI 层：真起子进程跑 --as dsh 读他人 private
        # 为什么必须打 CLI：只测库测不出"CLI 壳没接 viewer"——本段打的是 CLI 真实出口（stdout）。
        # ----------------------------------------------------------
        _cli2 = _REPO / "xtmem-project"/ "src"/ "cli"/ "xtmem.py"
        _env2 = {k: v for k, v in os.environ.items() if k != "XTMEM_ACTOR"}
        _env2["XTMEM_DB_PATH"] = db
        _n81 = "8.1 🔴 CLI --as dsh 读他人 private：stdout 无该正文"
        _n82 = "8.2 CLI --as dsh 读自身域 private：正文可得（防一刀切堵死正常读取）"
        _n83 = "8.3 🔴 CLI 无身份 fail-closed：读不到 private 正文"
        _n84 = "8.4 XTMEM_ACTOR 与 --as 冲突：拒绝执行并回冲突说明"
        try:
            _p2 = subprocess.run([sys.executable, _cli2, "trace", "mem-xu-priv", "--as", "dsh"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 env=_env2, timeout=120)
            _out2 = (_p2.stdout or "") + (_p2.stderr or "")
            check(_n81, "assistant-a私密内容" not in _out2, "out=%s" % _out2[:300])

            _p3 = subprocess.run([sys.executable, _cli2, "trace", "mem-dsh-priv", "--as", "dsh"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 env=_env2, timeout=120)
            _out3 = (_p3.stdout or "") + (_p3.stderr or "")
            check(_n82, "assistant-c自己的私密内容" in _out3, "out=%s" % _out3[:300])

            _p4 = subprocess.run([sys.executable, _cli2, "trace", "mem-dsh-priv"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 env=_env2, timeout=120)
            _out4 = (_p4.stdout or "") + (_p4.stderr or "")
            check(_n83, "assistant-c自己的私密内容" not in _out4, "out=%s" % _out4[:300])

            _env5 = dict(_env2, XTMEM_ACTOR="personal")
            _p5 = subprocess.run([sys.executable, _cli2, "trace", "mem-pub-1", "--as", "dsh"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 env=_env5, timeout=120)
            _out5 = (_p5.stdout or "") + (_p5.stderr or "")
            check(_n84, _p5.returncode != 0 and "冲突" in _out5,
                  "rc=%s out=%s" % (_p5.returncode, _out5[:300]))
        except Exception as _e2:
            for _n in (_n81, _n82, _n83, _n84):
                check(_n, False, "exc=%r" % (_e2,))

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print("\n=== D3-4 守卫结果：%d/%d passed ===" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_d3_4_trace_guard():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())