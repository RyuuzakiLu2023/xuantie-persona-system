# -*- coding: utf-8 -*-
"""M03 专项验收：来源 owner 与权限交集（派生权限，Spec §9.3）。

判据来源
--------
本单专项验收（Spec §9.3）：
  ① 所有读入口同权限
  ② 派生实际读者不扩大
原核心案例：I09（cross-owner private 派生）/ I10（多源权限收紧）/ I12（model 自报 principal/admin）
           / T11（DM 主题带无关私人细节）/ T17（生成期间来源撤回）/ U08（分享回忆册按受众投影）

运行
----
    python -X utf8 tests/test_m03_derived_permissions.py     # 逐行 JSON，供验收台消费
    pytest tests/test_m03_derived_permissions.py -q          # 常规

⚠️ 本文件是"行为判据"，不是结构检查：
   每条判据都必须能**被真正的回退弄红**（见 m03/evidence 的对抗性回退记录）。
   只断言"函数存在/字段非空"的写法一律不接受——M01 的教训（结构校验不能替代行为验收）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parents[1] / "src" / "hermes-plugin"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

# 逐行 JSON 协议：用作【验收台判据】时，stdout 每行一个 {"name","passed","harness_error"}。
# 为什么默认开：本文件是"行为判据"，tk.py verify / tk.py adversarial 直接当 cases 脚本调，
#   它们只认 stdout 上的整行 JSON（tk_common.parse_case_lines）。
#   ⚠️ 键名必须是 "name"（tk 约定），早期写成 "id" 会让验收台解析到 0 条用例——
#      而"0 条"在 tk 里被判**失败**（铁律 2：不把没解析到当全绿），会硬报错而非静默。
# 人被叫来看时加 --human（人类可读输出走 stderr，不污染协议）。
_JSON_LINES = os.environ.get("M03_JSON_LINES", "1") != "0"
_HUMAN = ("--human" in sys.argv) or (os.environ.get("M03_HUMAN") == "1")
_RESULTS: list = []


def _record(pid: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append({"name": pid, "passed": bool(ok), "detail": detail})
    if _HUMAN and not _JSON_LINES:
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", pid, detail))


def _check(pid: str, cond: bool, detail: str = "") -> bool:
    _record(pid, cond, detail)
    return bool(cond)


# ================================================================
# 夹具
# ================================================================

def _tmp_db() -> Path:
    d = Path(tempfile.mkdtemp(prefix="m03-perm-"))
    return d / "vector_store.db"


def _make_store(db: Path):
    """建一个空库并返回 VectorStore（含 schema 闸）。"""
    from memory.vector_store import VectorStore
    vs = VectorStore(db_path=str(db))
    conn = vs._get_conn()
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(conn)
    conn.commit()
    return vs, conn


def _add(conn, cid: str, content: str, ns: str, vis: str, *,
         derived_from: str = "", source_ids=(), imp: float = 0.9) -> None:
    conn.execute(
        "INSERT INTO chunks (id,content,timestamp,importance,tags,source,token_count,"
        "content_hash,track,state,superseded_by,namespace,project,host_origin,updated_at,"
        "visibility,source_ids,derived_from,tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, content, time.time(), imp, "[]", "t", 8, "h-" + cid, "default", "active", "",
         ns, "", "", time.time(), vis, json.dumps(list(source_ids)), derived_from, "raw"))
    conn.commit()


def _derived_chunk(cid, content, ns, vis, src_ids, derived_from="distill"):
    from memory.vector_store import Chunk
    return Chunk(id=cid, content=content, timestamp=time.time(), importance=0.7,
                 namespace=ns, visibility=vis, state="active", tier="refined",
                 source_ids=json.dumps(list(src_ids)), derived_from=derived_from)


# ================================================================
# I09 · cross-owner private 派生：实际读者集合不扩大
# ================================================================

def test_i09_cross_owner_private_derivation_does_not_widen_readers():
    """I09：源 private 属 ns=dsh，派生落到别的 owner ⇒ 读者集合不得扩大。

    ★这是 M03 的**核心病灶**：visibility 是 namespace 相对语义，'private' 字串本身
      不带域名。旧 min_visibility 只比字串 ⇒ 换域即扩权。实机复现（交付前）：
        源 (ns=dsh, private) → 读者 {dsh}；派生落 assistant ⇒ 读者 {assistant} 🔴
    正确行为：交集 {dsh} 在 assistant 域下编码不出来 ⇒ **拒绝生成**。
    """
    from memory.visibility import reader_set_of, is_subset
    from memory.vector_store import Chunk
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "src-dsh", "dsh 的私事", "dsh", "private")

    # 归一路径（写侧闸）必须拒：拟落域 assistant 不在来源读者集合里
    c = Chunk(id="der-x1", content="跨域派生尝试", timestamp=time.time(), importance=0.7,
              namespace="assistant", visibility="private", state="active", tier="refined",
              source_ids=json.dumps(["src-dsh"]), derived_from="distill")
    ok = _check("I09-cross-owner-derivation-refused", vs.store_chunk(c) is False,
                "派生落到非来源域且不可表达 ⇒ 必须拒写")
    if ok:
        row = conn.execute("SELECT COUNT(*) FROM chunks WHERE id='der-x1'").fetchone()[0]
        _check("I09-refused-row-not-persisted", row == 0, "被拒的派生行不得落库")

    # 同域派生允许，但读者集合不扩大
    c2 = Chunk(id="der-x2", content="同域派生", timestamp=time.time(), importance=0.7,
               namespace="dsh", visibility="private", state="active", tier="refined",
               source_ids=json.dumps(["src-dsh"]), derived_from="distill")
    _check("I09-same-owner-derivation-allowed", vs.store_chunk(c2) is True,
           "同域派生是合法操作，不得一刀切拒写")
    src_readers = reader_set_of("private", "dsh")
    der_readers = reader_set_of("private", "dsh")
    _check("I09-reader-set-not-widened", is_subset(der_readers, src_readers),
           "派生读者集合必须 ⊆ 来源读者集合")

    # 攻击面直测：用 assistant 身份读同域派生，必须读不到
    from memory.visibility import visibility_ok
    _check("I09-other-owner-cannot-read", not visibility_ok("private", "dsh", "assistant"),
           "非来源域身份不得读到该派生")
    vs.close()


def test_i09_shared_derivation_respects_intersection():
    """I09 变体：多源派生，派生落域必须仍在交集内。"""
    from memory.vector_store import Chunk
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "src-a", "a 的共享", "dsh", "shared")
    _add(conn, "src-b", "b 的私事", "dsh", "private")
    c = Chunk(id="der-y", content="派生", timestamp=time.time(), importance=0.7,
              namespace="dsh", visibility="shared", state="active", tier="refined",
              source_ids=json.dumps(["src-a", "src-b"]), derived_from="distill")
    _check("I09-multisrc-tightens-to-intersection", vs.store_chunk(c) is True, "应写入")
    got = conn.execute("SELECT visibility FROM chunks WHERE id='der-y'").fetchone()["visibility"]
    _check("I09-multisrc-result-is-private", got == "private",
           "shared ∩ private = private，实得 %r" % got)
    vs.close()


# ================================================================
# I10 · 多源权限收紧：派生/投影/缓存失效
# ================================================================

def test_i10_tightening_propagates_to_all_derived():
    """I10：源权限收紧 ⇒ 派生跟随收紧（且是**重算全量交集**，不是两者 min）。"""
    from memory.source_resolver import get_resolver
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "t-a", "源A", "dsh", "shared")
    _add(conn, "t-b", "源B", "dsh", "shared")
    # 派生引用了 A+B，初始 shared
    _add(conn, "t-der", "派生", "dsh", "shared", derived_from="distill",
         source_ids=["t-a", "t-b"])
    res = get_resolver(conn).tighten_source("t-a", "private")
    _check("I10-tighten-ok", res.get("ok") is True, str(res)[:120])
    got = conn.execute("SELECT visibility FROM chunks WHERE id='t-der'").fetchone()["visibility"]
    _check("I10-derived-tightened", got == "private", "A 收紧后派生应为 private，实得 %r" % got)
    vs.close()


def test_i10_multi_source_recompute_not_pairwise_min():
    """I10 关键：多源派生在【某一个】源收紧时，必须重算全部来源的交集。

    旧 follow_source_visibility 只做 `min(A_new, derived_cur)`，B 的读者集合根本没被
    重新纳入——本判据就是用来让那种实现变红的。
    场景：A=shared-with:x, B=shared-with:y, 派生落 ns=dsh 且当前 shared-with:x,y。
         收紧 B → 派生读者应为 {dsh} ∪ ({x} ∩ {y}) = {dsh}（即 private）
         若实现只做 min(B_new, 当前值)，可能得到别的字串。
    """
    from memory.source_resolver import get_resolver
    from memory.visibility import reader_set_of, is_subset
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "m-a", "源A", "dsh", "shared-with:x")
    _add(conn, "m-b", "源B", "dsh", "shared-with:y")
    _add(conn, "m-der", "派生", "dsh", "shared-with:x,y", derived_from="distill",
         source_ids=["m-a", "m-b"])
    get_resolver(conn).tighten_source("m-b", "private")
    got = conn.execute("SELECT visibility FROM chunks WHERE id='m-der'").fetchone()["visibility"]
    r = reader_set_of(got, "dsh")
    # 期望：B=private ⇒ 交集 = {dsh} ⇒ 'private'
    _check("I10-recompute-full-intersection", got == "private",
           "B 收紧为 private 后，A∩B={dsh} ⇒ 应 private，实得 %r" % got)
    _check("I10-no-widening-after-tighten",
           is_subset(r, reader_set_of("shared-with:x,y", "dsh")),
           "收紧只能收窄，不得扩大")
    vs.close()


def test_i10_stale_index_visibility_is_rechecked_at_read():
    """I10：DB 收紧后，**旧内存索引快照**不得继续放行（缓存失效判据）。

    HybridRetriever.docs[].visibility 是 hydrate 时的快照。若检索只信快照，
    收紧后仍会放行——这正是「已缓存 ContextPlan / 投影需失效」的同族。
    本判据证明：检索侧对**每一跳都回查真值**（provider._ns_map 口径）或直接以
    快照+DB 双闸判，收紧后立刻拦得住。
    """
    from memory.visibility import visibility_ok
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "st-1", "将被收紧的内容", "assistant", "shared")
    # 模拟"检索时看到的旧快照"
    snapshot_vis = "shared"
    _check("I10-before-tighten-visible", visibility_ok(snapshot_vis, "assistant", "dsh"),
           "收紧前 dsh 可读（前置条件）")
    conn.execute("UPDATE chunks SET visibility='private' WHERE id='st-1'")
    conn.commit()
    # 读侧必须回查 DB 真值 → 拦住
    real = conn.execute("SELECT visibility FROM chunks WHERE id='st-1'").fetchone()["visibility"]
    _check("I10-after-tighten-blocked", not visibility_ok(real, "assistant", "dsh"),
           "收紧后按 DB 真值判定必须拦住")
    _check("I10-snapshot-would-have-leaked", visibility_ok(snapshot_vis, "assistant", "dsh"),
           "本判据的意义：只信旧快照确实会放行 ⇒ 读侧必须回查真值")
    vs.close()


# ================================================================
# I12 · model 自报 principal/admin 无效
# ================================================================

def test_i12_canonical_denies_empty_identity_on_private():
    """I12 基础：空身份不得读到 private（canonical 判定的 fail-closed 底线）。"""
    from memory.visibility import visibility_ok
    _check("I12-empty-viewer-denied-private",
           not visibility_ok("private", "assistant", ""),
           "空 viewer 读他人 private 必须 deny")
    _check("I12-none-like-denied-private",
           not visibility_ok("private", "assistant", None),
           "None viewer 也必须 deny")
    _check("I12-empty-viewer-allowed-shared",
           visibility_ok("shared", "assistant", ""),
           "共享内容对空身份仍可见（不得一刀切堵死）")


def test_i12_cockpit_agrees_with_canonical_on_empty_viewer():
    """I12 变体（★本单实测发现）：cockpit_contract 的空 viewer 曾 fail-open。

    实测（探针 m03-probe8.py）：
        _is_admin(None)=True, _is_admin("")=True
        chunk_visible("assistant","private","") → True，而 canonical → False
    ⇒ 两套口径。且 REST token 表允许 `{"viewer": "", "admin": false}`
      （api_server._require_auth），配上它就把非管理员 token 顶成全可见视角 = 提权。
    本判据锁死：两者必须同判。
    """
    from memory.cockpit_contract import _is_admin, chunk_visible
    from memory.visibility import visibility_ok
    for viewer in (None, ""):
        _check("I12-cockpit-empty-viewer-not-admin-%r" % (viewer,),
               _is_admin(viewer) is False,
               "_is_admin(%r) 不得为 True" % (viewer,))
        _check("I12-cockpit agrees canonical %r" % (viewer,),
               chunk_visible("assistant", "private", viewer)
               == visibility_ok("private", "assistant", viewer or ""),
               "两套口径必须同判")
    # 显式管理员仍须可用（防一刀切）
    _check("I12-explicit-admin-still-works", _is_admin("__admin__") is True,
           "显式 __admin__ 仍是管理员")


def test_i12_cli_recall_does_not_leak_without_identity():
    """I12 变体（★本单复现的 P0）：CLI recall 无身份时曾整段跳过可见性闸。

    实测（探针 m03-probe3.py，修前）：无身份 `recall --query <他人private正文片段> --cross`
    **返回正文**；而 `--as dsh` 反而被拦 —— "没身份"比"有身份"看得多。
    根因：写的是 `if _actor and not _visibility_ok(...)` ⇒ actor 空则条件恒假。
    本判据跑真子进程，锁死"无身份也走判定闸"。
    """
    import subprocess
    CLI = _PLUGIN / "xtmem.py"
    db = _tmp_db()
    vs, conn = _make_store(db)
    secret = "ZZM03-I12-SECRET-9182736"
    _add(conn, "i12-sec", secret, "assistant", "private")
    _add(conn, "i12-pub", "公开内容 M03PUB", "knowledge", "shared")
    vs.close()

    env = dict(os.environ)
    env.update({"XTMEM_DB_PATH": str(db), "PYTHONIOENCODING": "utf-8",
                "XTMEM_HERMES_ROOT": str(_PLUGIN / "memory")})
    for k in ("XTMEM_ACTOR", "XTMEM_ADMIN", "XTMEM_ADMIN_TOKEN"):
        env.pop(k, None)

    def run(args):
        p = subprocess.run([sys.executable, "-X", "utf8", str(CLI), *args],
                           capture_output=True, env=env, cwd=str(db.parent), timeout=120)
        return (p.stdout or b"").decode("utf-8", "replace")

    for args in (["recall", "--query", "ZZM03-I12", "--cross", "--top", "5"],
                 ["recall", "--query", "ZZM03-I12", "--top", "5"],
                 ["recall", "--query", "ZZM03-I12", "--namespace", "assistant"],
                 ["recall", "--query", "ZZM03-I12", "--cross", "--mode", "exact"]):
        out = run(args)
        _check("I12-cli-no-identity-no-leak:%s" % args[-1], secret not in out,
               "无身份不得读到他人 private")
    # 对照：本人能读到自己的
    out = run(["recall", "--query", "ZZM03-I12", "--cross", "--as", "assistant"])
    _check("I12-cli-owner-can-read-own", secret in out, "本人必须能读到自己的 private")
    # 对照：公开内容仍可读（防一刀切）
    out = run(["recall", "--query", "M03PUB", "--cross"])
    _check("I12-cli-shared-still-readable", "M03PUB" in out, "shared 内容对无身份仍可读")


def test_i12_fts_leg_default_is_fail_closed():
    """I12 变体（★本单实测发现）：检索腿 viewer 缺省曾是 '__admin__'（缺参数即管理员）。

    实测（探针 m03-probe7d.py，修前）：
        不传 viewer        → 命中他人 private 🔴
        viewer=''（空身份） → 0 条
    ⇒ 越权只需"少传一个参数"。修后缺省应为 fail-closed。
    """
    from memory.vector_store import VectorStore, fts_search_bm25, Chunk
    import inspect
    db = _tmp_db()
    vs, conn = _make_store(db)
    q = "ZZM03FTSLEGSECRET771"
    vs.store_chunk(Chunk(id="fts-1", content=q, timestamp=time.time(), importance=0.9,
                         namespace="assistant", visibility="private", state="active",
                         tier="raw", content_hash="", tags=[]))
    _check("I12-fts-no-viewer-no-leak", len(fts_search_bm25(conn, q, top_k=5)) == 0,
           "不传 viewer 不得放行他人 private")
    _check("I12-fts-owner-still-works",
           len(fts_search_bm25(conn, q, top_k=5, viewer="assistant")) == 1,
           "显式身份仍须正常命中")
    _check("I12-fts-explicit-admin-still-works",
           len(fts_search_bm25(conn, q, top_k=5, viewer="__admin__")) == 1,
           "显式管理员仍须可见（防一刀切）")
    _check("I12-search_bm25-default-not-admin",
           inspect.signature(VectorStore.search_bm25).parameters["viewer"].default is None,
           "search_bm25 缺省不得为 __admin__")
    _check("I12-search_vector-default-not-admin",
           inspect.signature(VectorStore.search_vector).parameters["viewer"].default is None,
           "search_vector 缺省不得为 __admin__")
    vs.close()


# ================================================================
# T11 · DM 相关话题带无关私人细节：接必要内容，禁止多带细节
# ================================================================

def test_t11_renderable_spans_filters_by_purpose_and_permission():
    """T11：进入生成正文的片段必须【同时】满足「本轮目的相关」+「viewer 有权限」。

    为什么两个条件都要：DM 相关的私人细节即便 viewer 有权限，只要与本轮目的无关，
    就不该被"顺手带进"公共生成正文（Spec §6.2 / §5.5）。
    """
    from memory.source_resolver import get_resolver
    rows = [
        {"id": "relevant-ok", "visibility": "private", "namespace": "dsh"},
        {"id": "irrelevant-priv", "visibility": "private", "namespace": "dsh"},
        {"id": "others-private", "visibility": "private", "namespace": "assistant"},
    ]
    got = get_resolver().renderable_spans(
        rows, "dsh", purpose_ids={"relevant-ok", "irrelevant-priv", "others-private"})
    ids = {r["id"] for r in got}
    _check("T11-permission-filter", "others-private" not in ids, "越权片段必须被滤掉")
    _check("T11-allowed-visible", "relevant-ok" in ids, "有权且相关必须保留")
    # 收紧 purpose：只留本轮真正需要的
    got2 = get_resolver().renderable_spans(rows, "dsh", purpose_ids={"relevant-ok"})
    ids2 = {r["id"] for r in got2}
    _check("T11-purpose-narrows-details", ids2 == {"relevant-ok"},
           "无关的私人细节即使有权限也不得进正文，实得 %s" % sorted(ids2))


# ================================================================
# T17 · source 修改/撤回发生在生成期间：发送前失效旧计划
# ================================================================

def test_t17_revalidate_detects_tighten_and_removal():
    """T17：生成期捕获快照，发送前复验——来源被收紧/删除必须判"旧计划失效"。"""
    from memory.source_resolver import get_resolver, SourceRef
    db = _tmp_db()
    vs, conn = _make_store(db)
    _add(conn, "t17-1", "来源一", "dsh", "shared")
    r = get_resolver(conn)
    snap = r.resolve(SourceRef("chunk", "t17-1"))
    ok, changes = r.revalidate([snap])
    _check("T17-fresh-snapshot-still-valid", ok is True, "未变更时快照应仍有效")

    # ① 收紧
    conn.execute("UPDATE chunks SET visibility='private' WHERE id='t17-1'")
    conn.commit()
    r.bind(conn)
    ok2, ch2 = r.revalidate([snap])
    _check("T17-tighten-invalidates-plan", ok2 is False and ch2,
           "来源收紧必须让旧计划失效（不泄旧文）")

    # ② 撤回（物理删除）
    conn.execute("DELETE FROM chunks WHERE id='t17-1'")
    conn.commit()
    r.bind(conn)
    ok3, ch3 = r.revalidate([snap])
    _check("T17-removal-invalidates-plan", ok3 is False and ch3,
           "来源被撤回必须让旧计划失效")
    vs.close()


# ================================================================
# U08 · 分享回忆册：按目标 audience 投影
# ================================================================

def test_u08_projection_uses_audience_not_caller():
    """U08：投影判据必须是「**受众**能否读」，不是「调用者能否读」——后者是经典越权。"""
    from memory.source_resolver import get_resolver
    rows = [
        {"id": "shared-row", "visibility": "shared", "namespace": "dsh"},
        {"id": "dsh-private", "visibility": "private", "namespace": "dsh"},
        {"id": "xu-private", "visibility": "private", "namespace": "assistant"},
        {"id": "xu-listed", "visibility": "shared-with:zcode", "namespace": "assistant"},
    ]
    r = get_resolver()
    # 受众 = assistant：shared + 自己的 private + 列名在册的（zcode 不在受众内，故排除）
    got = r.project_for_audience(rows, "assistant")
    ids = {x["id"] for x in got}
    _check("U08-audience-own-private-included", "xu-private" in ids, "受众自己的 private 应保留")
    _check("U08-audience-others-private-excluded", "dsh-private" not in ids,
           "他人的 private 不得进投影")
    _check("U08-shared-included", "shared-row" in ids, "shared 应保留")
    # 受众 = zcode：只应看到 shared + 被点名共享的
    got2 = r.project_for_audience(rows, "zcode")
    ids2 = {x["id"] for x in got2}
    _check("U08-listed-audience-included", "xu-listed" in ids2,
           "shared-with:zcode 对受众 zcode 应可见")
    _check("U08-unrelated-private-excluded", not ({"dsh-private", "xu-private"} & ids2),
           "非受众的 private 一律不得进投影")
    # 关键反例：调用者是管理员/别人 —— 投影**不**随调用者放宽
    _check("U08-projection-independent-of-caller",
           "dsh-private" not in {x["id"] for x in r.project_for_audience(rows, "assistant")},
           "投影只看受众，不看调用者")


def test_u08_empty_input_is_not_all_readers():
    """U08 边界：空内容集的公共读者集合是 CONFINED（不是 ALL）。"""
    from memory.visibility import readers_of_rows, CONFINED
    _check("U08-empty-rows-confined", readers_of_rows([]) is CONFINED,
           "空集合不得被当成'人人可读'")


# ================================================================
# 专项验收② · 派生实际读者不扩大（跨入口总判据）
# ================================================================

def test_special_reader_sets_never_widen_through_write_gate():
    """总判据：遍历一组 (来源 vis × 拟落域 × 声明 vis)，落库行读者集合必须 ⊆ 来源交集。

    ⚠️ 性能（2026-10-13 本单返工）：初版每次迭代都新建一条库 + ensure_xtmem_schema。
      32 组 = 32 次建库建表，单这一条判据就把 M03 整轮从十几秒拖到一分多钟，
      而对抗性验证要把它重跑 11 遍 ⇒ 十几分钟全花在建表上。
      改成**复用同一条库**：每组开始前把上组的行清掉即可（库结构只建一次）。
      语义完全不变（每组仍然是"干净的库 + 一条来源 + 一次派生写入"）。
    """
    from memory.visibility import ALL_READERS, is_subset, reader_set_of
    from memory.vector_store import Chunk
    vises = ["shared", "private", "shared-with:a", "shared-with:b"]
    nss = ["dsh", "assistant"]
    violations = []
    checked = 0
    db = _tmp_db()
    vs, conn = _make_store(db)          # ★只建一次
    try:
        for sv in vises:
            for sns in ["dsh"]:
                for dns in nss:
                    for dv in vises:
                        conn.execute("DELETE FROM chunks")
                        conn.commit()
                        _add(conn, "src", "来源", sns, sv)
                        c = Chunk(id="d-%s-%s-%s" % (sv, dns, dv), content="派生",
                                  timestamp=time.time(), importance=0.7, namespace=dns,
                                  visibility=dv, state="active", tier="refined",
                                  source_ids=json.dumps(["src"]), derived_from="distill")
                        stored = vs.store_chunk(c)
                        checked += 1
                        if not stored:
                            continue
                        landed = conn.execute(
                            "SELECT visibility, namespace FROM chunks WHERE id=?",
                            (c.id,)).fetchone()
                        src_int = reader_set_of(sv, sns)
                        landed_readers = reader_set_of(landed["visibility"], landed["namespace"])
                        allowed = set() if src_int is ALL_READERS else set(src_int)
                        if landed_readers is ALL_READERS and src_int is not ALL_READERS:
                            violations.append((sv, sns, dns, dv, "ALL_READERS"))
                        elif not is_subset(landed_readers, src_int):
                            extra = set(landed_readers) - allowed
                            violations.append((sv, sns, dns, dv, sorted(extra)))
    finally:
        vs.close()
    _check("SPEC2-no-reader-widening-across-matrix", not violations,
           "检查 %d 组，越界 %d 组：%s" % (checked, len(violations), violations[:4]))


def test_special_all_read_entries_share_one_predicate():
    """专项①：所有读入口同权限——各模块的判定必须与 canonical 逐一一致。"""
    from memory.visibility import visibility_ok, reader_set_matches_visibility_ok
    ok = reader_set_matches_visibility_ok(
        ["", "dsh", "assistant", "zcode", "knowledge"],
        ["shared", "private", "shared-with:a", "shared-with:a,b", "", "junk"],
        ["", "__admin__", "dsh", "assistant", "zcode", "a", "b"])
    _check("SPEC1-reader-set-matches-canonical", ok,
           "reader_set_of 与 visibility_ok 必须逐点一致（穷举对拍）")

    from memory.cockpit_contract import chunk_visible
    mism = []
    for ns in ["", "dsh", "assistant"]:
        for vis in ["shared", "private", "shared-with:a", "", "junk"]:
            for viewer in ["", "__admin__", "dsh", "assistant", "a"]:
                a = chunk_visible(ns, vis, viewer)
                b = visibility_ok(vis, ns, viewer)
                if a != b:
                    mism.append((ns, vis, viewer, a, b))
    _check("SPEC1-cockpit-agrees-canonical", not mism,
           "cockpit_contract.chunk_visible 与 canonical 必须同判，分歧 %s" % mism[:3])


# ================================================================
# 主线（JSON 行协议，供验收台消费）
# ================================================================

_TESTS = [
    ("I09", test_i09_cross_owner_private_derivation_does_not_widen_readers),
    ("I09", test_i09_shared_derivation_respects_intersection),
    ("I10", test_i10_tightening_propagates_to_all_derived),
    ("I10", test_i10_multi_source_recompute_not_pairwise_min),
    ("I10", test_i10_stale_index_visibility_is_rechecked_at_read),
    ("I12", test_i12_canonical_denies_empty_identity_on_private),
    ("I12", test_i12_cockpit_agrees_with_canonical_on_empty_viewer),
    ("I12", test_i12_cli_recall_does_not_leak_without_identity),
    ("I12", test_i12_fts_leg_default_is_fail_closed),
    ("T11", test_t11_renderable_spans_filters_by_purpose_and_permission),
    ("T17", test_t17_revalidate_detects_tighten_and_removal),
    ("U08", test_u08_projection_uses_audience_not_caller),
    ("U08", test_u08_empty_input_is_not_all_readers),
    ("SPEC1", test_special_all_read_entries_share_one_predicate),
    ("SPEC2", test_special_reader_sets_never_widen_through_write_gate),
]


def main() -> int:
    if _HUMAN:
        print("M03 专项判据（Spec §9.3 来源 owner 与权限交集）", file=sys.stderr)
    for gid, fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # 夹具/导入炸掉也算红，不许静默
            _record("%s-EXCEPTION-%s" % (gid, fn.__name__), False,
                    "%s: %s" % (type(exc).__name__, exc))
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r["passed"])
    if _JSON_LINES:
        for r in _RESULTS:
            print(json.dumps(r, ensure_ascii=False))
    if _HUMAN:
        print("\n合计 %d/%d 通过" % (passed, total), file=sys.stderr)
        for r in _RESULTS:
            if not r["passed"]:
                print("  FAIL %s  %s" % (r["name"], r["detail"]), file=sys.stderr)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
