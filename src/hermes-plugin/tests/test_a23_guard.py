# ══════════════════════════════════════════════════════════════════════════
# 单A23 守卫 · assistant-a raw 永久豁免 + 退役哑账闸 + 时间字段类型闸（2026-09-16 · assistant-c）
# ══════════════════════════════════════════════════════════════════════════
# 管什么：单A23 三件的"不变量"。跑法：
#     pytest tests/test_a23_guard.py -v
#     或 python tests/test_a23_guard.py      （无 pytest 也能跑）
#
# ⚠️ 本守卫的两条设计纪律（都来自本单错题）：
#   1. 【判参数取值一律走 AST，绝不用文本匹配】——A16/A20 两次"守卫假绿"都是因为
#      assert 'state="recalled"' in block 匹配到了【我自己写的注释】，把真参数删了守卫还绿。
#      所以这里所有"代码里传了什么"的断言都用 ast 取实参值，注释里的字串一律不算数。
#   2. 【反向验证必做】——每个"加了闸"的地方都要先证明"改坏后它真能红"。
#      实现与夹具共享同一个错误假设时，全绿毫无意义（自证循环陷阱）。
# ══════════════════════════════════════════════════════════════════════════
import ast
import json
import os
import re
import sqlite3
import sys
import tempfile
import textwrap
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve()
PLUGINS = HERE.parent.parent                      # .../plugins/xtmem
MEM = PLUGINS / "memory"
PROVIDER = PLUGINS / "provider.py"
VSTORE = MEM / "vector_store.py"
SCHEMA = MEM / "schema.py"
XTMEM_CLI = HERE.parent.parent.parent.parent / "xtmem" / "xtmem.py"   # 爬四层到 mimox-data
for p in (PROVIDER, VSTORE, SCHEMA, XTMEM_CLI):
    assert p.exists(), "路径没找到: %s" % p

sys.path.insert(0, str(PLUGINS))


# ────────────────────────────────────────────────────────────────────────────
# 【一】写入端：assistant-a raw 自动带「永久」
# ────────────────────────────────────────────────────────────────────────────
def _sync_turn_tags_node():
    """从 provider.py AST 里抠出 sync_turn() 里 task["meta"]["tags"] 的那个表达式节点。
    不 import 引擎（会拉起 sqlite/embedding 初始化），只解析源码——理由见文件头。
    ⚠️ 踩过一次：dict 的键值对是 ast.Dict（keys/values 两个平行列表），
       【不是】ast.Call 的 ast.keyword —— 按 keyword 找必然返回 None。"""
    tree = ast.parse(PROVIDER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "sync_turn":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Dict):
                continue
            for k, v in zip(sub.keys, sub.values):
                if isinstance(k, ast.Constant) and k.value == "tags":
                    return v
    return None


def _eval_tags_expr(ns):
    """把源码里那个真实表达式拿去 eval —— 用假 self 喂不同 namespace。
    这是"跑真代码"而不是"匹配字串"，注释里写什么都骗不过它。"""
    node = _sync_turn_tags_node()
    assert node is not None, "sync_turn 里找不到 task[meta][tags] 表达式"
    code = compile(ast.Expression(node), "<tags>", "eval")

    class _FakeSelf:
        pass

    fake = _FakeSelf()
    fake._namespace = ns
    return eval(code, {"__builtins__": {}}, {"self": fake})


def test_a23_1_write_side_tags_by_namespace():
    """【一】验收5：新造assistant-a raw 自动带「永久」；dsh / zcode 不带（用户A明确只豁免assistant-a）。"""
    got = {ns: _eval_tags_expr(ns) for ns in ("personal", "dsh", "zcode", "hermes-webui", "knowledge")}
    assert "永久" in got["personal"], "assistant-a raw 应带「永久」，实得 %s" % got["personal"]
    for ns in ("dsh", "zcode", "hermes-webui", "knowledge"):
        assert "永久" not in got[ns], "%s 不该被豁免（用户A：该淘汰淘汰），实得 %s" % (ns, got[ns])
    # 原有两个标签不能被顶掉（追加，不是覆盖）
    for ns, tags in got.items():
        assert "hermes" in tags and ns in tags, "%s 的原标签被顶掉了: %s" % (ns, tags)
    assert got["personal"] == ["hermes", "personal", "永久"], "标签顺序/内容变了: %s" % got["personal"]
    assert got["dsh"] == ["hermes", "dsh"], "dsh 分支内容变了: %s" % got["dsh"]


def test_a23_1_write_side_reverse_proof():
    """【一】反向验证：把判据改成恒真（= 三端全豁免），守卫必须红。
    这条证明上一条不是"自证循环"——它真的看得见 namespace 条件。"""
    node = _sync_turn_tags_node()
    assert isinstance(node, ast.IfExp), "tags 必须是按 namespace 分支的条件表达式，实得 %s" % type(node).__name__
    # 判据必须比对 personal 这个具体值
    src = ast.dump(node.test)
    assert "personal" in src, "分支判据里没有 personal: %s" % src
    assert "永久" in ast.dump(node.body), "真分支里没有「永久」"
    assert "永久" not in ast.dump(node.orelse), "假分支里不该有「永久」"
    # 反证：把测试条件换成恒真，dsh 也会带上「永久」 ⇒ 说明这条断言确实依赖 namespace
    forged = ast.Expression(ast.IfExp(test=ast.Constant(value=True),
                                      body=node.body, orelse=node.orelse))
    ast.fix_missing_locations(forged)
    fake = eval(compile(forged, "<forged>", "eval"), {"__builtins__": {}},
                {"self": type("F", (), {"_namespace": "dsh"})()})
    assert "永久" in fake, "反证失败：把判据换成恒真后 dsh 也带永久了吗？实得 %s" % fake
    assert _eval_tags_expr("dsh") != fake, "反证失败：正常判据与恒真判据结果相同 = 判据没起作用"


# ────────────────────────────────────────────────────────────────────────────
# 【二】退役哑账闸
# ────────────────────────────────────────────────────────────────────────────
# ⚠️ 夹具落点用 <TMP>\\DSH，【不】用系统 TEMP —— 按 AGENTS.md 第五节第 7 条，
#    所有临时产物一律进 <TMP>\\DSH。也【不】用 tempfile.TemporaryDirectory：
#    Windows 下 sqlite 连接没关干净就删目录会抛 WinError 32（今天踩过）。
#    这里退出时不删目录，留着当现场证据。
GUARD_TMP = Path(r"<TMP>\DSH\a23-guard")


class _tmpdir(object):
    """with _tmpdir('tag') as td:  —— 落在 <TMP>\\DSH 下，退出不删。"""

    def __init__(self, tag):
        self.dir = GUARD_TMP / ("%s-%d-%s" % (tag, os.getpid(), _tmpdir._seq))
        _tmpdir._seq += 1

    _seq = 0

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        return str(self.dir)

    def __exit__(self, *a):
        return False


def _fresh_store(tmpdir):
    from memory.vector_store import VectorStore
    return VectorStore(db_path=os.path.join(tmpdir, "guard.db"))


def _close_store(vs):
    """把 VectorStore 持有的连接关掉，免得句柄泄漏（Windows 下还会锁文件）。"""
    try:
        for attr in ("_conn", "conn", "_connection"):
            c = getattr(vs, attr, None)
            if c is not None and hasattr(c, "close"):
                c.close()
                return
        vs._get_conn().close()
    except Exception:
        pass


def _mk_chunk(vs, cid, state="active", **kw):
    conn = vs._get_conn()
    conn.execute(
        "INSERT INTO chunks (id, content, timestamp, tags, track, state, superseded_by, "
        "namespace, retire_reason, retired_by, retired_at, tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, kw.get("content", "c"), kw.get("timestamp", 1.0), kw.get("tags", "[]"),
         kw.get("track", "default"), state, kw.get("superseded_by", ""),
         kw.get("namespace", "dsh"), kw.get("retire_reason", ""), kw.get("retired_by", ""),
         kw.get("retired_at"), kw.get("tier", "raw")))
    conn.commit()


def _reason_of(vs, cid):
    return vs._get_conn().execute("SELECT retire_reason FROM chunks WHERE id=?", (cid,)).fetchone()[0]


def test_a23_2_mark_superseded_derives_reason_from_pointer():
    """【二】①：只传继任指针 → retire_reason 必须派生出 superseded-by:<ptr>，不许留空。"""
    with _tmpdir("a23") as td:
        vs = _fresh_store(td)
        _mk_chunk(vs, "mem-aaaaaaaa")
        ok = vs.mark_superseded("mem-aaaaaaaa", successor_id="mem-bbbbbbbb", by="guard")
        assert ok is not False, "带指针的退役不该被拒"
        got = _reason_of(vs, "mem-aaaaaaaa")
        assert got == "superseded-by:mem-bbbbbbbb", "reason 未派生，实得 %r" % got


def test_a23_2_mark_superseded_rejects_bare_retirement():
    """【二】①：既无指针又无 reason → 必须拒绝写入（不许留哑账）。"""
    with _tmpdir("a23") as td:
        vs = _fresh_store(td)
        _mk_chunk(vs, "mem-aaaaaaaa")
        res = vs.mark_superseded("mem-aaaaaaaa")
        conn = vs._get_conn()
        state = conn.execute("SELECT state FROM chunks WHERE id=?", ("mem-aaaaaaaa",)).fetchone()[0]
        assert res is False, "裸调用（无指针无 reason）应当被拒，实得 %r" % (res,)
        assert state == "active", "被拒了却还是写了 state=%s —— 拒写不彻底" % state
        assert _reason_of(vs, "mem-aaaaaaaa") in ("", None), "被拒了却写了 reason"


def test_a23_2_mark_superseded_keeps_explicit_reason():
    """【二】①：显式传了 reason 的老调用方必须一字不差照常工作（全树 6 个调用点依赖）。"""
    with _tmpdir("a23") as td:
        vs = _fresh_store(td)
        _mk_chunk(vs, "mem-aaaaaaaa")
        vs.mark_superseded("mem-aaaaaaaa", successor_id="mem-bbbbbbbb",
                           reason="wiki-raw-prune", by="dsh-zcode")
        assert _reason_of(vs, "mem-aaaaaaaa") == "wiki-raw-prune", "显式 reason 被覆盖了"


def _predict_archive_updates():
    """从 xtmem.py AST 里抠出所有会把 state 改成 archived 的 UPDATE 语句（字符串字面量拼接后的全文）。"""
    tree = ast.parse(XTMEM_CLI.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                try:
                    s = eval(compile(ast.Expression(arg), "<s>", "eval"), {"__builtins__": {}})
                except Exception:
                    continue
                if isinstance(s, str) and "state='archived'" in s:
                    out.append((node.lineno, s))
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and "state='archived'" in arg.value:
                out.append((node.lineno, arg.value))
    return out


def test_a23_2_prediction_archive_writes_retire_reason():
    """【二】①：xtmem.py 两条预测归档路径必须写 retire_reason（原本一条都不写 = 哑账入口）。"""
    ups = _predict_archive_updates()
    assert len(ups) >= 2, "至少应找到 2 条归档 UPDATE，实得 %d: %s" % (len(ups), [l for l, _ in ups])
    for lineno, sql in ups:
        assert "retire_reason" in sql, "第 %d 行的归档 UPDATE 没写 retire_reason:\n%s" % (lineno, sql)
        assert "retired_by" in sql and "retired_at" in sql, \
            "第 %d 行的归档 UPDATE 退役三列不全" % lineno


# ────────────────────────────────────────────────────────────────────────────
# 【三】时间字段类型闸
# ────────────────────────────────────────────────────────────────────────────
DU_DDL = """
CREATE TABLE distill_units (
  unit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source TEXT NOT NULL,
  ts_start REAL NOT NULL, ts_end REAL NOT NULL, turn_count INTEGER NOT NULL,
  char_count INTEGER NOT NULL, raw_ids TEXT NOT NULL, prev_unit TEXT,
  filtered_ids TEXT, has_correction INTEGER DEFAULT 0, state TEXT NOT NULL,
  distilled_at REAL, refined_ids TEXT, claim_token TEXT, attempt INTEGER DEFAULT 0,
  last_error TEXT, created_at REAL NOT NULL
);
"""
# ⚠️ ensure_xtmem_schema 开头就会 PRAGMA table_info(chunks)，库里没有 chunks 表会直接炸
#    （第一版漏了这个，4 条守卫全报 no such table: chunks）。
_CHUNKS_DDL = """
CREATE TABLE chunks (
  id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp REAL NOT NULL,
  tags TEXT DEFAULT '[]', track TEXT DEFAULT 'default', state TEXT DEFAULT 'active',
  superseded_by TEXT DEFAULT '', namespace TEXT DEFAULT 'dsh', retire_reason TEXT DEFAULT '',
  retired_by TEXT DEFAULT '', retired_at REAL DEFAULT NULL, tier TEXT DEFAULT 'raw'
);
"""


def _guarded_db(tmpdir):
    """造一个带 distill_units 的库，并跑 ensure_xtmem_schema 把闸建起来。"""
    from memory.schema import ensure_xtmem_schema
    p = os.path.join(tmpdir, "du.db")
    con = sqlite3.connect(p)
    con.executescript(_CHUNKS_DDL + DU_DDL)
    ensure_xtmem_schema(con)
    con.commit()
    return p


def _insert_du(con, uid, distilled_at):
    con.execute(
        "INSERT INTO distill_units (unit_id,namespace,source,ts_start,ts_end,turn_count,char_count,"
        "raw_ids,state,distilled_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "personal", "s", 1.0, 2.0, 1, 1, "[]", "x", distilled_at, 3.0))


def test_a23_3_time_type_gate_blocks_text():
    """【三】验收1：写入文字格式时间必须被拒（反向验证）。"""
    with _tmpdir("a23") as td:
        con = sqlite3.connect(_guarded_db(td))
        for bad in ("2026-09-05 21:50:47", "not-a-time", ""):
            try:
                _insert_du(con, "u-%s" % abs(hash(bad)), bad)
                con.rollback()
                raise AssertionError("distilled_at=%r 居然写进去了" % bad)
            except AssertionError:
                raise
            except Exception:
                con.rollback()
        # UPDATE 路径也要拦（病根正是历史 UPDATE 写脏的）
        _insert_du(con, "u-ok", 1757000000.0)
        con.commit()
        try:
            con.execute("UPDATE distill_units SET distilled_at=? WHERE unit_id='u-ok'",
                        ("2026-09-05 21:50:47",))
            con.rollback()
            raise AssertionError("UPDATE 把 distilled_at 改成 TEXT 居然成功了")
        except AssertionError:
            raise
        except Exception:
            con.rollback()
        con.close()


def test_a23_3_time_type_gate_allows_real_integer_null():
    """【三】验收2/防误伤：real / integer / NULL 必须放行。
    ⚠️ integer 这条不是凑数——xtmem_distill_commit.py 写的是 int(time.time())，
       只认 'real' 会当场误伤那个现存写端。"""
    with _tmpdir("a23") as td:
        con = sqlite3.connect(_guarded_db(td))
        for uid, val in (("u-real", 1757000000.0), ("u-int", 1757000000), ("u-null", None)):
            _insert_du(con, uid, val)
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM distill_units").fetchone()[0]
        assert n == 3, "应写入 3 条，实得 %d（闸误伤了正常写入）" % n
        con.close()


def test_a23_3_time_type_gate_covers_four_columns():
    """【三】：闸必须真正覆盖 ts_start/ts_end/created_at/distilled_at 四列。
    ⚠️ 这里刻意【不】做"源码里找 'ts_start' 字串"的文本断言 —— 本鱼第一版就是这么写的，
       结果源码用双引号、断言找单引号，白报了一条红（引号风格不该影响判据）。
       改成真去写脏数据：哪一列没闸，哪一列就写得进去，立刻红。"""
    with _tmpdir("a23") as td:
        con = sqlite3.connect(_guarded_db(td))
        _insert_du(con, "u-base", 1757000000.0)
        con.commit()
        for col in ("ts_start", "ts_end", "created_at", "distilled_at"):
            try:
                con.execute("UPDATE distill_units SET %s=? WHERE unit_id='u-base'" % col,
                            ("2026-09-05 21:50:47",))
                con.rollback()
                raise AssertionError("列 %s 上没闸：写脏成功了" % col)
            except AssertionError:
                raise
            except Exception:
                con.rollback()      # 被闸拒了才是对的
        con.close()


def test_a23_3_gate_present_in_both_files():
    """【三】：schema.py（老库那条路）与 xtmem_distill_units.py（新建表那条路）都必须有闸。
    漏一处 = 另一条路的新库/老库没锁。用 AST 取 tuple 实参，不看引号风格、不看注释。"""
    def _gate_cols(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cols = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Tuple):
                try:
                    v = ast.literal_eval(node)
                except Exception:
                    continue
                # ⚠️ 源码里有很多 tuple 元素不是标量（嵌套列表），直接 set(v) 会
                #    TypeError: unhashable type: 'list' —— 先筛成纯字符串元组再比。
                if not isinstance(v, tuple) or not all(isinstance(x, str) for x in v):
                    continue
                if set(v) >= {"ts_start", "ts_end", "created_at", "distilled_at"}:
                    cols |= set(v)
        return cols

    script = PLUGINS.parent.parent / "scripts" / "xtmem_distill_units.py"
    for path, label in ((SCHEMA, "schema.py"), (script, "xtmem_distill_units.py")):
        assert path.exists(), "%s 不存在: %s" % (label, path)
        cols = _gate_cols(path)
        missing = {"ts_start", "ts_end", "created_at", "distilled_at"} - cols
        assert not missing, "%s 的闸没覆盖列: %s" % (label, sorted(missing))


def test_a23_3_time_type_gate_reverse_proof():
    """【三】反向验证：把闸 DROP 掉，TEXT 就能写进去 —— 证明上一条守卫真的在起作用。"""
    with _tmpdir("a23") as td:
        p = _guarded_db(td)
        con = sqlite3.connect(p)
        con.execute("DROP TRIGGER trg_distill_units_time_type_ins")
        con.commit()
        _insert_du(con, "u-bypass", "2026-09-05 21:50:47")   # 闸没了，必然写得进去
        con.commit()
        got = con.execute("SELECT typeof(distilled_at) FROM distill_units WHERE unit_id='u-bypass'").fetchone()[0]
        assert got == "text", "反证失败：没闸也写不进 TEXT？实得 %s" % got
        con.close()


# ────────────────────────────────────────────────────────────────────────────
# 自跑入口（无 pytest 也能跑）
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_a23_") and callable(f)]
    npass = nfail = 0
    for name, fn in fns:
        try:
            fn()
            print("  PASS  %s" % name)
            npass += 1
        except Exception as e:
            print("  FAIL  %s -> %s: %s" % (name, type(e).__name__, e))
            nfail += 1
    print("\nA23 守卫: %d passed / %d failed" % (npass, nfail))
    raise SystemExit(1 if nfail else 0)
