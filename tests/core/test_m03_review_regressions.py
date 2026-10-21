# -*- coding: utf-8 -*-
"""M03 返修判据：独立验收 12 个阻断项 + 2 个限制项的逐条回归。

为什么单独一份（不并进 test_m03_derived_permissions.py）：
  ① 那份是**交付时的自测**，判据口径已经独立验收核对过；本轮返修要能单独证明
     "12 条阻断项各自被修掉了"，混在一起会让"哪条红了"变难读；
  ② 独立验收的脚本（m03-review/review_boundaries.py / review_integration.py）
     不可能在本单里被改动——它们是审查方资产。这里**复刻它们的判据**，
     并额外加"修复后必须成立"的正向断言（原判据只断言失败形态）。

★本文件最重要的一条纪律（R3-1 的教训）：
  判据必须走**真实入口**，禁止"手工读库 + 直接调底层函数"充当验证。
  · I10 缓存失效 → 必须调 HybridRetriever.search()，不能自己 visibility_ok 一遍；
  · follow 传播   → 必须调 vector_store.follow_source_visibility()，
                    不能直接调 resolver.tighten_source()（后者是内部件）；
  · FTS 限额      → 必须调 fts_search_bm25() 这个真入口；
  · REST 身份     → 必须走 api_server._parse_role_tokens → _require_auth 全链。
  自测用假入口 = 自测是假的绿（M03 第一轮就是这么漏掉 R3 的）。

输出协议与 test_m03 一致：M03_JSON_LINES=1 时逐行 JSON，供验收台消费。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

_JSON_LINES = os.environ.get("M03_JSON_LINES") == "1"
_HUMAN = os.environ.get("M03_HUMAN", "1") == "1"

__file__ = os.path.abspath(__file__)

_ROOT = Path(__file__).resolve().parents[2] / "src" / "hermes-plugin"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_RESULTS: list = []


def _record(pid: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append({"name": pid, "passed": bool(ok), "detail": detail})
    if _JSON_LINES:
        print(json.dumps({"name": pid, "passed": bool(ok), "detail": detail},
                         ensure_ascii=False), flush=True)
    elif _HUMAN:
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", pid, detail))


def _check(pid: str, cond: bool, detail: str = "") -> bool:
    _record(pid, cond, detail)
    return bool(cond)


def _case(name, fn):
    """跑一条判据；自身异常记为 EXCEPTION（不静默）。"""
    try:
        fn()
    except Exception:
        _record(name + "-EXCEPTION", False, traceback.format_exc()[-1200:])


# ================================================================
# 夹具（与 test_m03 同款，保持可比性）
# ================================================================

def _tmp_db() -> Path:
    d = Path(tempfile.mkdtemp(prefix="m03fix-"))
    return d / "vector_store.db"


def _make_store(db: Path):
    from memory.vector_store import VectorStore
    vs = VectorStore(db_path=str(db))
    conn = vs._get_conn()
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(conn)
    conn.commit()
    return vs, conn


def _add(conn, cid, content, ns, vis, *, derived_from="", source_ids=(), imp=0.9, state="active"):
    conn.execute(
        "INSERT INTO chunks (id,content,timestamp,importance,tags,source,token_count,"
        "content_hash,track,state,superseded_by,namespace,project,host_origin,updated_at,"
        "visibility,source_ids,derived_from,tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, content, time.time(), imp, "[]", "t", 8, "h-" + cid, "default", state, "",
         ns, "", "", time.time(), vis, json.dumps(list(source_ids)), derived_from, "raw"))
    conn.commit()


def _derived(cid, ns, src_ids, declared="shared", derived_from="distill"):
    from memory.vector_store import Chunk
    return Chunk(id=cid, content="派生 " + cid, timestamp=time.time(), importance=0.7,
                 namespace=ns, visibility=declared, state="active", tier="refined",
                 source_ids=json.dumps(list(src_ids)), derived_from=derived_from)


def _row(conn, cid):
    r = conn.execute("SELECT namespace, visibility, state FROM chunks WHERE id=?",
                     (cid,)).fetchone()
    return dict(r) if r else None


def _can_read(conn, cid, viewer):
    from memory.visibility import visibility_ok
    r = _row(conn, cid)
    return bool(r and r["state"] == "active"
                and visibility_ok(r["visibility"], r["namespace"], viewer))


# ================================================================
# R1 · 派生写入不得放宽声明权限，也不得因缺失来源忽略已知限制
# ================================================================

def r1_write_keeps_declared_restriction():
    """R1-1：来源 shared + 调用方**明确声明** private ⇒ 落库不得变成 shared。

    独立验收原判据（m03-review/review_boundaries.py::keep_declared_private）实测：
      accepted=True, actual=dsh/shared, readable_by_zcode=True  🔴
    正解：落库读者 = 来源交集 ∩ 声明读者集合 ⇒ 这里必须仍是 private。
    """
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        accepted = vs.store_chunk(_derived("b", "dsh", ["a"], declared="private"))
        got = _row(conn, "b")
        _check("R1-declared-private-kept",
               accepted and got is not None and got["visibility"] == "private",
               "声明 private 必须保住，实得 %r（accepted=%r）" % (got, accepted))
        _check("R1-declared-private-not-readable-by-others",
               not _can_read(conn, "b", "zcode"),
               "调用方收窄到 private 后，他人不得可读")
        # 反向：声明 shared 而来源是 private ⇒ 仍按交集收紧成 private（不得放宽）
        _add(conn, "c", "私来源", "dsh", "private")
        vs.store_chunk(_derived("d", "dsh", ["c"], declared="shared"))
        got2 = _row(conn, "d")
        _check("R1-intersection-still-tightens",
               got2 is not None and got2["visibility"] == "private",
               "来源 private ⇒ 即使声明 shared 也必须收紧，实得 %r" % (got2,))
    finally:
        vs.close()


def r1_missing_source_must_not_grant_known_private():
    """R1-2：已知来源 dsh/private + 一条不存在的来源 ⇒ 派生落 zcode **必须拒写**。

    独立验收原判据实测：
      accepted=True, derived=zcode/private, derived_readable_by_zcode=True  🔴
    病灶：旧保守档直接落"拟落域 private"= 把这条白送给 zcode，而 zcode 根本
          不在已知来源的读者集合里。**加一条不存在的来源就能绕过已知 private 来源的限制。**
    正解：用 known_readers（仅可解析来源的交集）当上界；与保守值取交后编不出来 ⇒ 拒。
    """
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "dsh 的私事", "dsh", "private")
        # 先证明"只有已知来源"时确实被拒（这是基线的正确行为）
        ok_single = vs.store_chunk(_derived("b", "zcode", ["a"], declared="private"))
        _check("R1-known-private-alone-denied", ok_single is False,
               "单引已知 private 来源的跨域派生必须拒写")
        # 关键反例：追加一个不存在的来源 id
        ok_paired = vs.store_chunk(_derived("c", "zcode", ["a", "does-not-exist"],
                                            declared="private"))
        leak = _can_read(conn, "c", "zcode")
        _check("R1-missing-source-does-not-grant-known-private", not leak,
               "追加不存在来源不得解除已知 private 来源的约束（accepted=%r row=%r）"
               % (ok_paired, _row(conn, "c")))
        # 缺失来源**不得顺手把整批打死**：同域自引用仍应能落库（数据保全）
        _add(conn, "d", "同域私来源", "dsh", "private")
        ok_same = vs.store_chunk(_derived("e", "dsh", ["d", "also-missing"],
                                          declared="private"))
        _check("R1-missing-source-keeps-same-domain-writable",
               ok_same is True and _row(conn, "e") is not None,
               "缺失来源 + 同域已知来源 ⇒ 保守落 private 且不丢数据")
    finally:
        vs.close()


# ================================================================
# R2 · 权限收紧必须经既有入口传播，覆盖跨域与多级，并拒绝放宽
# ================================================================

def r2_existing_follow_entry_propagates():
    """R2-1a：**既有入口** follow_source_visibility(src,'private') 必须真的传播。

    独立验收实测：reported_changed=0，派生仍 shared，zcode 可读 🔴
    病灶：包装器先把源 UPDATE+commit，resolver 再看"值已相等"提前返回 ⇒ 传播空转。
    """
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        vs.store_chunk(_derived("b", "dsh", ["a"], declared="shared"))
        count = vs.follow_source_visibility("a", "private")
        got = _row(conn, "b")
        _check("R2-follow-entry-propagates",
               got is not None and got["visibility"] == "private",
               "既有入口调用后派生必须收紧（reported=%r row=%r）" % (count, got))
        _check("R2-follow-entry-no-leak-after",
               not _can_read(conn, "b", "zcode"),
               "收紧后 zcode 不得再读到派生")
    finally:
        vs.close()


def r2_follow_after_source_update_propagates():
    """R2-1b：来源**先被调用方改掉**、再调 follow ⇒ 仍必须传播。

    这是 schema.quarantine_bad_visibility 的真实调用序（它先 UPDATE 再 follow）。
    独立验收实测：reported_changed=0，派生仍 shared 🔴
    """
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        vs.store_chunk(_derived("b", "dsh", ["a"], declared="shared"))
        # 模拟 quarantine 的调用序：调用方先把源改掉并提交
        conn.execute("UPDATE chunks SET visibility='private' WHERE id='a'")
        conn.commit()
        count = vs.follow_source_visibility("a", "private")
        got = _row(conn, "b")
        _check("R2-follow-after-source-update-propagates",
               got is not None and got["visibility"] == "private",
               "源已被调用方改过，follow 仍须重算派生（reported=%r row=%r）" % (count, got))
    finally:
        vs.close()


def r2_cross_owner_tightening_keeps_reader_subset():
    """R2-2：跨域收紧后，派生**不得**落到"拟落域 private"。

    独立验收实测：dsh/shared 源 + zcode/shared 派生，源收紧为 dsh/private 后，
    派生变成 **zcode/private**，zcode 仍可读 🔴 —— 用拟落域 private 顶替不可表达的
    交集 = 凭空把阅读权送给那个域。
    正解：隔离/失效（state≠active），谁也读不到，证据仍在。
    """
    from memory.source_resolver import get_resolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        vs.store_chunk(_derived("b", "zcode", ["a"], declared="shared"))
        _check("R2-cross-owner-derived-initially-readable",
               _can_read(conn, "b", "zcode"), "前置：收紧前 zcode 可读")
        res = get_resolver(conn).tighten_source("a", "private")
        leak = _can_read(conn, "b", "zcode")
        _check("R2-cross-owner-tightening-keeps-reader-subset", not leak,
               "收紧后 zcode 不得读到（outcome=%s row=%r）"
               % (json.dumps(res.get("detail", {}), ensure_ascii=False), _row(conn, "b")))
        # 关键：不得留下 zcode/private 这种"看着像收紧了、实际是送给 zcode"的值
        got = _row(conn, "b")
        bad = bool(got and got["namespace"] == "zcode"
                   and got["visibility"] == "private" and got["state"] == "active")
        _check("R2-cross-owner-not-confined-to-target-ns-private", not bad,
               "不可表达的交集不得用拟落域 private 顶替，实得 %r" % (got,))
    finally:
        vs.close()


def r2_transitive_derivation_propagates():
    """R2-3：A→B→C 同域链上收紧 A ⇒ **C 也必须**被收紧。

    独立验收实测：只更新 B，C 仍 shared，其他域可读 🔴（只收紧一级）
    """
    from memory.source_resolver import get_resolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "源A", "dsh", "shared")
        vs.store_chunk(_derived("b", "dsh", ["a"], declared="shared"))
        vs.store_chunk(_derived("c", "dsh", ["b"], declared="shared"))
        _check("R2-multihop-precondition",
               _can_read(conn, "c", "zcode"), "前置：收紧前 C 对 zcode 可读")
        get_resolver(conn).tighten_source("a", "private")
        _check("R2-transitive-derivation-propagates", not _can_read(conn, "c", "zcode"),
               "A 收紧后第二级派生 C 也必须收紧，实得 C=%r B=%r"
               % (_row(conn, "c"), _row(conn, "b")))
    finally:
        vs.close()


def r2_tighten_must_not_widen_source():
    """R2-4：tighten_source 对 dsh/private 传 shared 必须**拒绝**（不是照单全收）。

    独立验收实测：ok=True，来源真的变成所有域可读 🔴
    病灶：docstring 白纸黑字写着"【不】允许放宽"，**代码里没有任何子集校验**。
    """
    from memory.source_resolver import get_resolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "私来源", "dsh", "private")
        res = get_resolver(conn).tighten_source("a", "shared")
        widened = _can_read(conn, "a", "zcode")
        _check("R2-tighten-must-not-widen-source", not widened,
               "收紧入口不得放宽（outcome_ok=%r row=%r）" % (res.get("ok"), _row(conn, "a")))
        _check("R2-tighten-widening-reports-failure",
               res.get("ok") is False and res.get("error") == "widening_not_allowed",
               "放宽必须显式报错，实得 %r" % (res.get("error"),))
    finally:
        vs.close()


def r2_follow_wrapper_is_atomic():
    """R2-1c：follow 包装器**不得先把源单独落库再传播**（原子性）。

    为什么单列这条（2026-10-14 返修中自己发现的缺口）：
      R2-1a 修掉"值未变即提前返回"之后，预提交**不再导致传播空转**——
      于是"传播是否发生"已经测不出预提交的问题了。
      但预提交还有一个**独立且更隐蔽**的危害：**非原子**。
      源先单独 UPDATE+commit，之后传播若失败/进程崩溃，库里就留下
      「源已收紧为 private、派生仍是 shared」——**崩溃窗口里是明文越源**，
      正是这个函数存在的理由（见它的 docstring：漏洞面=源 private 派生仍 shared）。
      正解是把源与派生放进**同一个原子操作**（resolver 统一写、一次性提交），
      让"源收紧"与"派生跟随"要么都成、要么都不成。
    判据：崩溃点注入——让传播阶段抛异常，源也不许留下已收紧的痕迹。
    """
    from memory.source_resolver import SourceResolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        vs.store_chunk(_derived("b", "dsh", ["a"], declared="shared"))
        # 注入：让 resolver 在传播阶段炸掉（模拟崩溃/断电/磁盘满）
        orig = SourceResolver.tighten_source

        def boom(self, *a, **kw):
            raise RuntimeError("injected crash during propagation")

        SourceResolver.tighten_source = boom
        try:
            vs.follow_source_visibility("a", "private")
        except Exception:
            pass          # 异常是注入出来的，预期之内
        finally:
            SourceResolver.tighten_source = orig
        src = _row(conn, "a")
        _check("R2-follow-wrapper-is-atomic",
               src is not None and src["visibility"] == "shared",
               "传播崩溃时源不得已被单独收紧（否则派生仍 shared = 明文越源窗口）；"
               "实得 source=%r derived=%r" % (src, _row(conn, "b")))
    finally:
        vs.close()


# ================================================================
# R3 · 读路径必须用当前权限；权限过滤先于候选限额
# ================================================================

def r3_actual_retriever_rechecks_database():
    """R3-1：**真实 HybridRetriever.search()** 必须回查 DB，不得返回已撤销权限的缓存文档。

    独立验收实测：index 时 shared → resolver 收紧为 private →
    同一 retriever.search(viewer='zcode') 仍返回它 🔴
    ★本判据必须走 retriever 真入口（施工方原 I10-stale 是手工读 DB 调 visibility_ok，
      根本没走检索，所以证明不了这条——这是本轮最重要的教训）。
    """
    from memory.hybrid_retriever import HybridRetriever, MemoryDoc
    from memory.source_resolver import get_resolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "cached", "将被撤销的内容", "dsh", "shared")
        retriever = HybridRetriever(db_path=vs.db_path, fts5_mode="off",
                                    relevance_threshold=0, semantic_dedup_enabled=False)
        doc = MemoryDoc(id="cached", content="reviewtoken 内容", timestamp=time.time(),
                        namespace="dsh", visibility="shared", importance=0.9)
        retriever.index([doc], embeddings=[[1.0, 0.0]])
        before = [d.id for d, _ in retriever.search("reviewtoken", viewer="zcode",
                                                    query_vec=[1.0, 0.0])]
        _check("R3-retriever-precondition", "cached" in before,
               "前置：收紧前 retriever 应返回该文档")
        get_resolver(conn).tighten_source("cached", "private")
        after = [d.id for d, _ in retriever.search("reviewtoken", viewer="zcode",
                                                   query_vec=[1.0, 0.0])]
        _check("R3-actual-retriever-rechecks-database", "cached" not in after,
               "DB 已收紧为 private，真实检索不得再返回（实得 %r，库中 %r）"
               % (after, _row(conn, "cached")))
        # 反向：有权的人仍必须拿得到（别把闸做成谁都读不到）
        owner = [d.id for d, _ in retriever.search("reviewtoken", viewer="dsh",
                                                   query_vec=[1.0, 0.0])]
        _check("R3-retriever-still-serves-owner", "cached" in owner,
               "收紧为 dsh/private 后，dsh 本人仍应读得到")
    finally:
        vs.close()


def r3_fts_permissions_before_limit():
    """R3-2：FTS 必须先按权限过滤、再取限额，不得让不可读行挤掉可读行。

    独立验收实测：60 条他域 private（排名更高）+ 1 条可读 shared、top_k=1 ⇒ 返回空 🔴
    违反 §16「FTS 限额不能在权限过滤前取 top-k」。
    """
    from memory.vector_store import fts_search_bm25, _tokenize_for_fts5
    vs, conn = _make_store(_tmp_db())
    try:
        for i in range(60):
            cid = "private-%02d" % i
            _add(conn, cid, "噪音", "dsh", "private")
            conn.execute("INSERT INTO chunks_fts(id,content) VALUES (?,?)",
                         (cid, _tokenize_for_fts5("reviewtoken")))
        _add(conn, "allowed", "可读内容", "zcode", "shared")
        conn.execute("INSERT INTO chunks_fts(id,content) VALUES (?,?)",
                     ("allowed", _tokenize_for_fts5("reviewtoken " + ("padding " * 300))))
        conn.commit()
        result = fts_search_bm25(conn, "reviewtoken", top_k=1, viewer="zcode")
        ids = [r[0] for r in result]
        _check("SPEC16-fts-permissions-before-limit", "allowed" in ids,
               "可读行不得被不可读行挤掉（eligible=%r 实得 %r）"
               % (_can_read(conn, "allowed", "zcode"), ids))
        # 反向：不可读的行一条都不得漏出来
        _check("SPEC16-fts-still-hides-private",
               not any(str(x).startswith("private-") for x in ids),
               "他域 private 不得出现在结果里，实得 %r" % (ids,))
        # 无身份（缺省）仍须 fail-closed
        _check("SPEC16-fts-empty-viewer-fail-closed",
               fts_search_bm25(conn, "reviewtoken", top_k=5, viewer=None) == []
               or not any(str(x[0]).startswith("private-")
                          for x in fts_search_bm25(conn, "reviewtoken", top_k=5, viewer=None)),
               "缺省 viewer 不得命中他域 private")
    finally:
        vs.close()


# ================================================================
# R4 · 权限检查与提交同一事务；不得偷提交调用方事务
# ================================================================

def r4_source_change_between_gate_and_insert():
    """R4-1：闸通过后、INSERT 前来源被改 ⇒ 旧授权结果**不得**落库。

    独立验收实测：另一条真实 SQLite 连接把来源 shared 改成 private，
    原写操作继续成功，跨域派生保持 shared 🔴
    正解：提交前复验来源版本指纹（检出即拒）。
    """
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "source", "来源", "dsh", "shared")
        other = sqlite3.connect(vs.db_path)
        gate = vs._apply_derived_readers_gate

        def gate_then_tighten(c, chunk):
            ok = gate(c, chunk)
            if ok:
                other.execute("UPDATE chunks SET visibility='private' WHERE id='source'")
                other.commit()
            return ok

        vs._apply_derived_readers_gate = gate_then_tighten
        accepted = vs.store_chunk(_derived("derived", "zcode", ["source"], declared="shared"))
        leak = _can_read(conn, "derived", "zcode")
        _check("R4-source-change-between-gate-and-insert", not leak,
               "闸后来源被改 ⇒ 不得落旧授权派生（accepted=%r row=%r）"
               % (accepted, _row(conn, "derived")))
        other.close()
    finally:
        vs.close()


def r4_preserve_caller_transaction():
    """R4-2：tighten_source 不得偷提交调用方已开启的事务。

    独立验收实测：调用方 BEGIN 后写一条无关审计记录，再调 tighten_source，
    外层 in_transaction 变 false；调用方 rollback 后那条记录仍在 🔴
    """
    from memory.source_resolver import SourceResolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "来源", "dsh", "shared")
        conn.execute("BEGIN")
        marker = "m03fix-tx-marker"
        conn.execute("INSERT INTO xtmem_events(ts,kind,meta) VALUES (?,?,?)",
                     (time.time(), marker, "{}"))
        SourceResolver(conn).tighten_source("a", "private")
        still_open = conn.in_transaction
        conn.rollback()
        survived = conn.execute("SELECT COUNT(*) FROM xtmem_events WHERE kind=?",
                                (marker,)).fetchone()[0]
        _check("R4-preserve-caller-transaction", still_open and survived == 0,
               "调用方事务不得被偷提交（still_open=%r 幸存的无关记录=%r）"
               % (still_open, survived))
        # 反向：本函数自己开的事务仍必须提交（别修成"永不提交"）
        conn.execute("SELECT 1")
        res = SourceResolver(conn).tighten_source("a", "private")
        landed = conn.execute("SELECT visibility FROM chunks WHERE id='a'").fetchone()[0]
        _check("R4-own-transaction-still-commits",
               res.get("ok") is True and landed == "private",
               "自持事务必须提交，实得 ok=%r 库中=%r" % (res.get("ok"), landed))
    finally:
        vs.close()


# ================================================================
# R5 · 来源版本指纹覆盖血缘变化
# ================================================================

def r5_lineage_edit_invalidates():
    """R5：派生来源由 shared A 改成 private B ⇒ revalidate 必须判定失效。

    独立验收实测：still_valid=True, changes=[] 🔴
    病灶：_CHUNK_VERSION_COLS 不含 source_ids ⇒ 血缘改写（授权语义变了）不算版本变化。
    正解：指纹必须绑定来源引用本身。
    """
    from memory.source_resolver import SourceResolver
    vs, conn = _make_store(_tmp_db())
    try:
        _add(conn, "a", "共享来源", "dsh", "shared")
        _add(conn, "b", "私来源", "dsh", "private")
        _add(conn, "der", "派生", "dsh", "shared", derived_from="distill", source_ids=["a"])
        r = SourceResolver(conn)
        snap = r.resolve("der")
        conn.execute("UPDATE chunks SET source_ids=? WHERE id='der'",
                     (json.dumps(["b"]),))
        conn.commit()
        valid, changes = r.revalidate([snap])
        _check("R5-lineage-edit-invalidates", not valid,
               "血缘从 A 改成 B 必须让旧计划失效（valid=%r changes=%r）" % (valid, changes))
        # 反向：内容/权限真的没变时不得误报失效（别把指纹做成"永远失效"）
        r2 = SourceResolver(conn)
        snap2 = r2.resolve("a")
        valid2, changes2 = r2.revalidate([snap2])
        _check("R5-unchanged-source-still-valid", valid2 is True and not changes2,
               "没改动的来源必须仍判有效，实得 valid=%r changes=%r" % (valid2, changes2))
    finally:
        vs.close()


# ================================================================
# 非阻断限制项（接受修复，但要留判据防止回退）
# ================================================================

def limit_resolver_does_not_rebind_another_db():
    """R-限制2：同进程两个库的 resolver 互不重绑（旧实现会串库）。

    独立验收实测：取 DB1 的 resolver，再 get_resolver(DB2)，
    r1.resolve() 读回来变成 DB2 的内容 🔴（登记为 P2 条件性限制，本轮一并收口）
    """
    from memory.source_resolver import get_resolver
    v1, c1 = _make_store(_tmp_db())
    v2, c2 = _make_store(_tmp_db())
    try:
        _add(c1, "same-id", "DB1", "dsh", "private")
        _add(c2, "same-id", "DB2", "zcode", "shared")
        r1 = get_resolver(c1)
        before = r1.resolve("same-id").as_dict()
        get_resolver(c2)
        after = r1.resolve("same-id").as_dict()
        _check("LIMIT-resolver-does-not-rebind-another-db",
               after["namespace"] == "dsh" and after["visibility"] == "private",
               "DB1 的 resolver 不得被绑到 DB2（before=%r after=%r）" % (before, after))
    finally:
        v1.close()
        v2.close()


def limit_empty_viewer_does_not_promote_admin():
    """R-限制3：REST token 声明 viewer='' 不得被提升为 admin。

    独立验收实测：{'viewer':'','admin':False} 解析后变 {'viewer':'admin','admin':True} 🔴
    并在报告中修正了归属：提升发生在 api_server._parse_role_tokens 的 `meta.get('viewer') or 'admin'`
    （**既有未修改代码**）。生产 token 文件已核对无触发形态 ⇒ 列为非阻断条件性风险。
    本判据锁住"解析链上不得静默提升"，防止回退。
    """
    from memory import api_server
    old = os.environ.get("XTMEM_REST_TOKENS")
    os.environ["XTMEM_REST_TOKENS"] = json.dumps(
        {"synthetic-fix-token": {"viewer": "", "admin": False}})
    try:
        parsed = api_server._parse_role_tokens(
            os.environ["XTMEM_REST_TOKENS"])["synthetic-fix-token"]
        auth = api_server._require_auth("synthetic-fix-token")
        # ⚠️ 这条**目前仍会红**是预期的：parser 是既有未修改代码（本轮不动它，
        #    归属已在验收报告中修正）。判据留在库里做"已知形态"登记，
        #    不让它悄悄变成"没人记得这回事"。
        _check("LIMIT-empty-viewer-does-not-promote-admin",
               not auth["admin"] and auth["viewer"] != "__admin__",
               "【已知既有缺陷·非本单引入】parser 把空 viewer 提升为 admin："
               "parsed=%r auth=%r（生产 token 文件已核对无此形态）" % (parsed, auth))
    finally:
        if old is None:
            os.environ.pop("XTMEM_REST_TOKENS", None)
        else:
            os.environ["XTMEM_REST_TOKENS"] = old


# ================================================================
# 主线
# ================================================================

_CASES = [
    ("R1-write-keeps-declared-restriction", r1_write_keeps_declared_restriction),
    ("R1-missing-source-must-not-grant-known-private",
     r1_missing_source_must_not_grant_known_private),
    ("R2-existing-follow-entry-propagates", r2_existing_follow_entry_propagates),
    ("R2-follow-after-source-update-propagates", r2_follow_after_source_update_propagates),
    ("R2-follow-wrapper-is-atomic", r2_follow_wrapper_is_atomic),
    ("R2-cross-owner-tightening-keeps-reader-subset",
     r2_cross_owner_tightening_keeps_reader_subset),
    ("R2-transitive-derivation-propagates", r2_transitive_derivation_propagates),
    ("R2-tighten-must-not-widen-source", r2_tighten_must_not_widen_source),
    ("R3-actual-retriever-rechecks-database", r3_actual_retriever_rechecks_database),
    ("SPEC16-fts-permissions-before-limit", r3_fts_permissions_before_limit),
    ("R4-source-change-between-gate-and-insert", r4_source_change_between_gate_and_insert),
    ("R4-preserve-caller-transaction", r4_preserve_caller_transaction),
    ("R5-lineage-edit-invalidates", r5_lineage_edit_invalidates),
    ("R-limit-resolver-rebind", limit_resolver_does_not_rebind_another_db),
    ("R-limit-rest-empty-viewer", limit_empty_viewer_does_not_promote_admin),
]


def main() -> int:
    for name, fn in _CASES:
        _case(name, fn)
    _ALLOWED_KNOWN_RED = {"LIMIT-empty-viewer-does-not-promote-admin"}
    hard_red = [r for r in _RESULTS
                if not r["passed"] and r["name"] not in _ALLOWED_KNOWN_RED]
    if _JSON_LINES:
        print(json.dumps({"summary": {
            "total": len(_RESULTS),
            "passed": sum(1 for r in _RESULTS if r["passed"]),
            "failed": sum(1 for r in _RESULTS if not r["passed"]),
            "hard_failed": len(hard_red),
            "known_red": sorted(_ALLOWED_KNOWN_RED),
        }}, ensure_ascii=False), flush=True)
    else:
        print("\n=== m03-fix 行为判据：%d/%d 通过（硬红 %d）==="
              % (sum(1 for r in _RESULTS if r["passed"]), len(_RESULTS), len(hard_red)))
    return 0 if not hard_red else 1


if __name__ == "__main__":
    sys.exit(main())
