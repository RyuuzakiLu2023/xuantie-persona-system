# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/xtmem_d1_guard_test.py · D1 事实态治理守卫
#
#   ▍这是什么
#   D1-1/D1-2/D1-3 三件事的回归守卫：schema 六列、迁移幂等与存量回填、open_db 版本闸、
#   极性检测关闭、蒸馏退役红线、裸 UPDATE 收编静态核查。
#   跑法：直接运行 exit 0/1；pytest 收集走 test_d1_guard()（体必须收在 _guard() 内，
#   顶层 sys.exit 会打断整个收集——见 错题本 #35）。
#   ▍修改铁律
#   1. 本文件必须四副本同步（xtmem-project/tests、XTMEM/tests、xtmem-project 镜像、
#      XTMEM-public/tests），sha 不等即视为未交付。
#   2. 断言只用临时夹具库（tempfile），严禁连生产库。
#   3. 版本闸断言写成「EXPECTED 可连 / EXPECTED±1 降级」而非硬编码数字 3——本批因不重启
#      未翻转常量，翻转后同一断言自动变成对 3 的校验，不需要改守卫。
#   ▍更新记录
#   *更新：2026-09-09 · 创建（D1 正式工单，assistant-c）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import importlib.util
import json
import os
import re
import shutil
import tempfile
import time

MASTER = _REPO / "src" / "hermes-plugin"
SCRIPTS = _REPO / "scripts"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


def _new_vs(tmp, name="g.db"):
    from memory.vector_store import VectorStore
    return VectorStore(os.path.join(tmp, name))


def _add_chunk(vs, cid, content, state="active", tier="raw", superseded_by="",
                  derived_from="", source_ids="[]", track="default", tags=None):
    """直插一行 chunks（守卫夹具，刻意绕开 store_chunk 以便精确造 D1 前遗留形态）。"""
    conn = vs._get_conn()
    conn.execute("DELETE FROM chunks WHERE id=?", (cid,))
    conn.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, "
        "content_hash, track, state, superseded_by, namespace, project, host_origin, "
        "updated_at, tier, derived_from, source_ids) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, content, time.time(), 0.6, json.dumps(tags or ["xtmem"]), "guard", 3,
         "h-" + cid, track, state, superseded_by, "dsh", "", "guard", time.time(),
         tier, derived_from, source_ids),
    )
    conn.commit()


def _guard() -> int:
    """脚本体收进函数：pytest 收集期只导入不执行。"""
    CHECKS.clear()
    tmp = tempfile.mkdtemp(prefix="xtmem-d1-guard-")
    try:
        # ── 1. schema 六列 ──
        vs = _new_vs(tmp)
        conn = vs._get_conn()
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")};
        ecol = {r[1] for r in conn.execute("PRAGMA table_info(memory_evolution)")};
        from memory.schema import D1_CHUNK_COLS, D1_EVO_COLS
        miss_c = [c for c, _, _ in D1_CHUNK_COLS if c not in ccols]
        miss_e = [c for c, _, _ in D1_EVO_COLS if c not in ecol]
        check("1.1 chunks 三列齐备", not miss_c, miss_c)
        check("1.2 memory_evolution 三列齐备", not miss_e, miss_e)
        check("1.3 retired_at 是 REAL（可存 epoch）",
              any(r[1] == "retired_at" and str(r[2]).upper() == "REAL"
                  for r in conn.execute("PRAGMA table_info(chunks)")))

        # ── 2. 迁移幂等 + 存量 1226 类形态回填正确（夹具复刻三类）──
        mig = _load(os.path.join(SCRIPTS, "xtmem_migrate_d1.py"), "d1_mig")
        _add_chunk(vs, "mem-aaaaaaaa", "被 0827 清理批退役的提示残留",
                   state="superseded", superseded_by="cleanup-20260827-prompt-residue")
        _add_chunk(vs, "mem-aaaaaaab", "第二条清理批",
                   state="superseded", superseded_by="cleanup-20260827-prompt-residue")
        _add_chunk(vs, "mem-aaaaaaac", "p41 流程污染行",
                   state="superseded", superseded_by="p41fix-flow-polluted")
        _add_chunk(vs, "mem-aaaaaaad", "被 forget 归档 有事件",
                   state="archived", superseded_by="forget")
        _add_chunk(vs, "mem-aaaaaaae", "被 forget 归档 无事件",
                   state="archived", superseded_by="forget")
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) "
            "VALUES (?, 'forget', 'zcode', 'zcode', '', 'mem-aaaaaaad', '{}')",
            (time.time(),),
        )
        conn.commit()
        _add_chunk(vs, "mem-11111111", "合法后继源", state="superseded",
                   superseded_by="mem-22222222")
        _add_chunk(vs, "mem-22222222", "合法后继目标")

        s1 = mig.migrate(conn)
        check("2.1 首遍回填三类计数 2+1+2",
              s1["backfill"]["cleanup_20260827"] == 2 and s1["backfill"]["p41fix"] == 1
              and s1["backfill"]["forget"] == 2, s1["backfill"])
        s2 = mig.migrate(conn)
        check("2.2 二遍零更新（幂等硬契约）",
              s2["backfill"]["total"] == 0, s2["backfill"])
        row = lambda cid: conn.execute(
            "SELECT superseded_by, retire_reason, retired_by, retired_at, state FROM chunks "
            "WHERE id=?", (cid,)).fetchone()
        a = row("mem-aaaaaaaa")
        check("2.3 清理批：指针清空 + 原因进 retire_reason",
              a["superseded_by"] == "" and a["retire_reason"] == "cleanup-20260827-prompt-residue", dict(a))
        check("2.4 清理批：retired_by=手工批 + retired_at 有值",
              a["retired_by"] == "manual-batch-20260827" and a["retired_at"], dict(a))
        check("2.5 清理批：state 未被改成 archived", a["state"] == "superseded", a["state"])
        b = row("mem-aaaaaaac")
        check("2.6 p41fix 归一写成 p41fix",
              b["retire_reason"] == "p41fix" and b["superseded_by"] == "", dict(b))
        c1 = row("mem-aaaaaaad")
        check("2.7 forget 发起者取自 xtmem_events.agent",
              c1["retired_by"] == "zcode" and c1["retire_reason"] == "forget", dict(c1))
        c2 = row("mem-aaaaaaae")
        check("2.8 无事件时兜底 cli-forget-unknown",
              c2["retired_by"] == "cli-forget-unknown", dict(c2))
        check("2.9 forget 类保持 archived 不强改 state", c2["state"] == "archived", c2["state"])
        vp = row("mem-11111111")["superseded_by"]
        check("2.10 真指针一条都不许动", vp == "mem-22222222", vp)
        pc = mig.verify_pointer(conn)
        check("2.11 迁移后违规指针 = 0", pc["illegal_pointers"] == 0, pc)
        check("2.12 真指针仍计数（夹具 1 条）", pc["valid_pointers"] == 1, pc)
        check("2.13 迁移写 d1-migration 事件留痕",
              conn.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind='d1-migration'"
              ).fetchone()[0] >= 1)
        vs.close()

        # ── 3. open_db 版本闸（写成 EXPECTED 相对断言，翻转前后都成立）──
        from memory import open_db as odb
        exp = odb.EXPECTED_SCHEMA_VERSION
        db2 = os.path.join(tmp, "gate.db")
        vc = _new_vs(tmp, "gate.db")
        vc.close()
        cn = odb.open_db(db2)
        cn.execute("PRAGMA user_version = %d" % exp)
        cn.commit()
        cn.close()
        c_ok = odb.open_db(db2)
        c_ok.execute("UPDATE xtmem_events SET meta=meta WHERE 1=0")
        check("3.1 user_version==EXPECTED 可写（不降级）", True)
        c_ok.close()
        c_hi = odb.open_db(db2)
        c_hi.execute("PRAGMA user_version = %d" % (exp + 1))
        c_hi.commit()
        c_hi.close()
        degraded = False
        c3 = odb.open_db(db2)
        try:
            c3.execute("INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'x', '{}')",
                      (time.time(),))
        except Exception:
            degraded = True
        c3.close()
        check("3.2 高版本库（EXPECTED+1）触发只读降级", degraded)
        c_lo = odb.open_db(db2)
        c_lo.execute("PRAGMA user_version = %d" % (exp - 1))
        c_lo.commit()
        c_lo.close()
        degraded2 = False
        c4 = odb.open_db(db2)
        try:
            c4.execute("INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'x', '{}')",
                      (time.time(),))
        except Exception:
            degraded2 = True
        c4.close()
        check("3.3 低版本库（EXPECTED-1）同样降级", degraded2)
        check("3.4 当前 EXPECTED_SCHEMA_VERSION 值（翻转后应为 3）", exp >= 2, exp)
        c5 = odb.open_db(db2, bypass_version_gate=True)
        c5.execute("PRAGMA user_version = %d" % exp)
        c5.commit()
        c5.close()
        check("3.5 迁移旁路可在错配态下写 DDL（bypass 生效）", True)

        # ── 4. D1-2 极性检测关闭 ──
        vs4 = _new_vs(tmp, "pol.db")
        from memory.context_builder import ContextBuilder
        _add_chunk(vs4, "mem-33333333", "用户偏好使用 A 编辑器进行开发工作")
        class _Stub:
            def __init__(self, v):
                self._vector_store = v
        out = ContextBuilder._detect_evolution(
            _Stub(vs4), "用户不再使用 A 编辑器，改为使用 B 编辑器", "mem-44444444", "dsh")
        rels = [r[1] for r in out]
        check("4.1 极性句不再产 replaces", "replaces" not in rels, out)
        check("4.2 极性句不再产 supersedes", "supersedes" not in rels, out)
        src = open(os.path.join(MASTER, "memory", "context_builder.py"), encoding="utf-8").read()
        check("4.3 polarity_flip 显式写死 False（防空词表恒真）",
              "polarity_flip = False" in src)
        check("4.4 调用侧硬闸在位（supersedes/replaces 直接 continue）",
              'if relation in ("supersedes", "replaces"):' in src)
        tun = json.load(open(_REPO / "xtmem_tuning.json", encoding="utf-8"))
        check("4.5 tuning polarity.words 已清空", tun["polarity"]["words"] == [],
              tun["polarity"]["words"])
        vs4.close()

        # ── 5. D1-3 蒸馏退役红线 ──
        vs5 = _new_vs(tmp, "dist.db")
        dl = _load(os.path.join(SCRIPTS, "xtmem_distill_llm.py"), "d1_dl")
        db5 = vs5.db_path
        _add_chunk(vs5, "mem-55555555", "原始流水一", tier="raw", state="active")
        _add_chunk(vs5, "mem-66666666", "原始流水二", tier="raw", state="active")
        _add_chunk(vs5, "mem-77777777", "蒸馏产物 refined", tier="refined", state="active",
                   derived_from="distill-judge", source_ids=json.dumps(["mem-55555555", "mem-66666666"]))
        st5 = dl.retire_source_raws(db5, [("mem-77777777", ["mem-55555555", "mem-66666666"])])
        r5 = vs5._get_conn().execute(
            "SELECT state, superseded_by, retire_reason, retired_by FROM chunks "
            "WHERE id='mem-55555555'").fetchone()
        check("5.1 蒸馏后 raw 置 superseded 而非 archived",
              r5["state"] == "superseded", r5["state"])
        check("5.2 superseded_by 指向 refined 真指针", r5["superseded_by"] == "mem-77777777", dict(r5))
        check("5.3 reason/by 落新列",
              r5["retire_reason"] == "distill-retire" and r5["retired_by"] == "distill_llm", dict(r5))
        check("5.4 退役统计计数正确", st5["retired"] == 2, st5)
        tags_ref = vs5._get_conn().execute(
            "SELECT tags FROM chunks WHERE id='mem-77777777'").fetchone()["tags"] or "[]"
        check("5.5 refined 未被血缘传播打成 lineage-stale（红线）",
              "lineage-stale" not in tags_ref, tags_ref)
        # 对照组（红线不是想当然）：raw 置 archived + 血缘传播 → refined 立刻被打 stale
        _add_chunk(vs5, "mem-88888888", "对照 raw 三", tier="raw", state="active")
        _add_chunk(vs5, "mem-99999999", "对照 refined 二", tier="refined", state="active",
                   derived_from="distill-judge", source_ids=json.dumps(["mem-88888888"]))
        vs5._get_conn().execute("UPDATE chunks SET state=" + "'archived'" + " WHERE id=" + "'mem-88888888'")
        vs5._get_conn().commit()
        vs5.propagate_source_archived("mem-88888888", reason="guard-control")
        tags_ctl = vs5._get_conn().execute(
            "SELECT tags FROM chunks WHERE id=" + "'mem-99999999'").fetchone()["tags"] or "[]"
        check("5.6 对照组：raw 置 archived 确实毒死 refined（红线依据）",
              "lineage-stale" in tags_ctl, tags_ctl)
        st5b = dl.retire_source_raws(db5, [("mem-77777777", ["mem-55555555"])])
        check("5.7 重复退役幂等：已 superseded 的原料只 skip 不再动",
              st5b["retired"] == 0 and st5b["skipped"] == 1, st5b)
        st5c = dl.retire_source_raws(db5, [("mem-77777777", ["mem-77777777"])])
        check("5.8 refined 不会被当原料退役（tier/自指过滤）", st5c["candidates"] == 0, st5c)
        vs5.close()

        # ── 6. 裸 UPDATE 收编静态核查（live + 镜像一起查，副本没同步就是没交付）──
        # 按目录遍历（少写一条路径 = 漏检；副本没同步就是没交付，红着别过）
        SCRIPT_DIRS = [
            _REPO / "scripts",
            _REPO / "profiles"/ "personal"/ "scripts",
            _REPO / "xtmem-project"/ "src"/ "scripts",
            _REPO / "XTMEM-public"/ "src"/ "scripts",
            _REPO / "XTMEM"/ "src"/ "scripts",
            _REPO / "xtmem-project"/ "src"/ "scripts",
        ]
        RETIRED_SCRIPTS = ["xtmem_distill.py", "xtmem_profile.py", "xtmem_timeline.py",
                           "xtmem_prune_wiki_raw_cards.py"]
        BARE = "UPDATE chunks SET state=" + "'superseded'"
        hits = []
        scanned = 0
        for root in SCRIPT_DIRS:
            for name in RETIRED_SCRIPTS:
                pth = os.path.join(root, name)
                if not os.path.exists(pth):
                    continue
                scanned += 1
                if BARE in open(pth, encoding="utf-8").read():
                    hits.append(pth.replace("<XT_ROOT>/", ""))
        check("6.0 扫描到副本数（<4 说明路径漂移）", scanned >= 4, scanned)
        check("6.1 收编范围内无裸 superseded UPDATE（含全部镜像）", not hits, hits)
        live = [os.path.join(SCRIPT_DIRS[0], n) for n in RETIRED_SCRIPTS
                if os.path.exists(os.path.join(SCRIPT_DIRS[0], n))]
        no_call = [os.path.basename(x) for x in live
                   if "mark_superseded(" not in open(x, encoding="utf-8").read()]
        check("6.2 live 四脚本全部改走 mark_superseded", not no_call, no_call)
        cb = open(os.path.join(MASTER, "memory", "context_builder.py"), encoding="utf-8").read()
        check("6.3 hydrate 不再用 10_000 硬上限",
              "list_chunks(limit=10_000)" not in cb and "iter_chunks_all" in cb)
        vsr = open(os.path.join(MASTER, "memory", "vector_store.py"), encoding="utf-8").read()
        check("6.4 embedding_cache 清理带同 hash 判据（不盲删）", "_dup_active" in vsr)

        # ── 7. mark_superseded 路由 + 缓存条件清理 ──
        vs7 = _new_vs(tmp, "ms.db")
        c7 = vs7._get_conn()
        _add_chunk(vs7, "mem-bbbbbbbb", "将被退役的一条", state="active")
        _add_chunk(vs7, "mem-cccccccc", "后继条目", state="active")
        from memory.utils import compute_embedding_cache_key

        def _cache_row(cid):
            content = c7.execute("SELECT content FROM chunks WHERE id=?", (cid,)).fetchone()["content"]
            key = compute_embedding_cache_key(content)
            c7.execute(
                "INSERT OR REPLACE INTO embedding_cache "
                "(text_hash, text, chunk_id, embedding, dim, created_at) VALUES (?,?,?,?,?,?)",
                (key, content, cid, b"\x00\x01", 2, time.time()),
            )
            c7.commit()
            return key

        k7 = _cache_row("mem-bbbbbbbb")
        vs7.mark_superseded("mem-bbbbbbbb", "cleanup-x-prompt-residue")  # 老口径：原因塞在 source_id
        d7 = c7.execute(
            "SELECT superseded_by, retire_reason, retired_at FROM chunks WHERE id=" + "'mem-bbbbbbbb'"
        ).fetchone()
        check("7.1 非指针入参改道 retire_reason，指针列不被污染",
              d7["superseded_by"] == "" and d7["retire_reason"] == "cleanup-x-prompt-residue", dict(d7))
        check("7.2 retired_at 落时间戳", bool(d7["retired_at"]), dict(d7))
        left7 = c7.execute("SELECT COUNT(*) FROM embedding_cache WHERE text_hash=?", (k7,)).fetchone()[0]
        check("7.3 同 hash 无其他 active 时缓存行照删（S3 幽灵治理不回退）", left7 == 0, left7)
        _add_chunk(vs7, "mem-dddddddd", "重复文本", state="active")
        _add_chunk(vs7, "mem-ddddddd2", "重复文本", state="active")
        c7.execute("UPDATE chunks SET content_hash=? WHERE id IN (" + "'mem-dddddddd','mem-ddddddd2'" + ")",
                   ("dup-hash",))
        c7.commit()
        k8 = _cache_row("mem-dddddddd")
        vs7.mark_superseded("mem-dddddddd", "mem-ddddddd2")
        left8 = c7.execute("SELECT COUNT(*) FROM embedding_cache WHERE text_hash=?", (k8,)).fetchone()[0]
        check("7.4 同 hash 仍有 active 时缓存行保留（不盲删致性能回退）", left8 == 1, left8)
        d8 = c7.execute("SELECT superseded_by FROM chunks WHERE id=" + "'mem-dddddddd'").fetchone()
        check("7.5 真指针正常写入", d8["superseded_by"] == "mem-ddddddd2", d8["superseded_by"])
        vs7.close()


        failed = [k for k, v in CHECKS.items() if not v]
        print("")
        print("=== D1 守卫结果：%d/%d passed ===" % (len(CHECKS) - len(failed), len(CHECKS)))
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_d1_guard():
    """pytest 入口：跑完整守卫并断言全绿。"""
    assert _guard() == 0, "见上方 FAIL 明细（D1）"


if __name__ == "__main__":
    sys.exit(_guard())
