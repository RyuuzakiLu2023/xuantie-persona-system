# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_e_associated_ns_gate.py · 尾批二单E守卫
#
#   ▍这是什么
#   `recall --mode associated` 的**涟漪联想**段（associations 一跳扩展）此前
#   只有 visibility + role_valve 两道闸，缺主循环那道 **ns 等值闸**。盲标实测：
#   associated 注入行 54.07%（438/810）是跨域污染——viewer=zcode/dsh 却拉进
#   ns=personal 的内容（22 条 distinct 记忆撑起 438 行）。438 条全为
#   visibility=shared，所以 visibility 闸拦不住，只能靠 ns 闸。
#
#   本守卫验证：ns 闸补上后，涟漪段不再跨域外溢；--cross 显式跨域语义保留；
#   自己域的 associated 条不被误杀。
#
#   ▍怎么跑
#   python test_xtmem_e_associated_ns_gate.py   （或 pytest 本文件）
#   全程 XTMEM_DB_PATH 指向 tempfile 临时库 + XTMEM_ACTOR=zcode，零生产写。
#
#   ▍文件从上到下的结构
#   常量区 → _guard()：建临时库灌三条夹具 → 非 cross 跑 recall（核心断言：
#   0 条跨域）→ 加 --cross 复跑（跨域条必须回来）→ 静态断言两份 CLI 源码
#   涟漪段都含 ns 闸特征。
#
#   ▍修改铁律
#   1. 夹具 canon 用 "testcanon-e"（工单指定），改 canon 必须同步改下面三条
#      夹具的 INSERT 与 QUERY 里对应的 token，否则涟漪段空转成假绿。
#   2. 断言只用临时夹具库，严禁连生产库；禁改 CLI 源码来"喂"本守卫。
#   3. 两份 CLI（deploy / 源仓）都要静态断言——只查一份等于放跑分叉。
#
#   ▍更新记录
#   *更新：2026-09-13 · 创建（尾批二单E，maintainer代执）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import argparse
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 引擎 = deploy 树（MCP 回落子进程同款）；CLI = 采集实测用的那份
MASTER = _REPO / "src" / "hermes-plugin"
CLI_PATH = _REPO / "xtmem"/ "xtmem.py"
# 两份 CLI 都要静态断言涟漪段含 ns 闸（源仓副本历史上与 deploy 有分叉，都改过）
CLI_COPIES = [
    _REPO / "xtmem"/ "xtmem.py",
    _REPO / "XTMEM"/ "src"/ "cli"/ "xtmem.py",
]

CANON = "testcanon-e"          # 工单指定：涟漪靠 canon 扩展，三条夹具同 canon
Z_MAIN = "mem-e-zmain"         # zcode 主条（唯一被主通道命中的条）
Z_ASSOC = "mem-e-zassoc"       # zcode 涟漪条（同 canon，只该经涟漪段进来）
X_ASSOC = "mem-e-xassoc"       # personal 跨域条（同 canon，本单要拦的就是它）
# 主通道查询只命中 Z_MAIN 的独有 token；另两条不带它，只能走涟漪段——
# 这样"0 条跨域"才真的在考涟漪段，而不是被主通道提前筛掉后空转。
QUERY_MAIN = "egateprobe-main-token"

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _mk_args(**over):
    base = dict(query=QUERY_MAIN, top=6, cross=False, namespace=None, project=None,
                entity=None, mode="associated", rerank=None, time_decay=None,
                role_valve=None, as_agent=None)
    base.update(over)
    return argparse.Namespace(**base)


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM 单E · associated 涟漪段 ns 闸守卫 ===")

    if MASTER not in sys.path:
        sys.path.insert(0, MASTER)
    from memory.vector_store import VectorStore

    tmp_dir = tempfile.mkdtemp(prefix="xtmem-e-gate-")
    db_path = os.path.join(tmp_dir, "guard.db")
    # ⚠️ 顺序契约：XTMEM_DB_PATH/XTMEM_ACTOR 必须在 CLI 模块 import 前设好——
    # CLI 顶层读环境变量定 DEFAULT_DB，import 后再设就是空设（会打到生产库）。
    _old_env = {k: os.environ.get(k) for k in ("XTMEM_DB_PATH", "XTMEM_ACTOR")}
    os.environ["XTMEM_DB_PATH"] = db_path
    os.environ["XTMEM_ACTOR"] = "zcode"

    conn = None
    try:
        vs = VectorStore(db_path)
        conn = vs._get_conn()
        now = time.time()
        fixtures = [
            # (id, content, namespace)——三条同 canon，涟漪必然把它们互相带出来；
            # 差异只在 namespace，考的就是涟漪段有没有 ns 闸。
            (Z_MAIN, f"{QUERY_MAIN} 主条体 zcode", "zcode"),
            (Z_ASSOC, "egateprobe-assoc-token 联想条体 zcode", "zcode"),
            (X_ASSOC, "egateprobe-xassoc-token 跨域条体assistant-a", "personal"),
        ]
        for cid, content, ns in fixtures:
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, content, timestamp, state, tier, "
                "namespace, visibility, updated_at, tags) "
                "VALUES (?, ?, ?, 'active', 'raw', ?, 'shared', ?, '[]')",
                (cid, content, now, ns, now),
            )
            # 打标：涟漪段从 chunk_entities 按 canon 反查同实体 chunk
            conn.execute(
                "INSERT OR REPLACE INTO chunk_entities (chunk_id, canon) VALUES (?, ?)",
                (cid, CANON),
            )
        conn.commit()
        conn.close()
        conn = None

        spec = importlib.util.spec_from_file_location("xtmem_cli_egate", CLI_PATH)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        # ---- 1. 非 cross：涟漪段不得跨域（本单核心）----
        res = cli.cmd_recall(_mk_args(cross=False))
        items = res.get("items", [])
        ids = [it["id"] for it in items]
        print("  [evidence] cross=False mode=%s items=%s" % (
            res.get("mode"), [(it["id"], it.get("namespace"), it.get("associated", False))
                              for it in items]))
        check("1.1 非 cross：0 条 ns=personal（本单核心，修复前 54.07% 全漏）",
              all(it.get("namespace") != "personal" for it in items), str(ids))
        check("1.2 非 cross：跨域条本体不在结果里", X_ASSOC not in ids, str(ids))
        check("1.3 夹具自检：主条被召回（防主通道空转导致守卫假绿）",
              Z_MAIN in ids, str(ids))
        check("1.4 防误杀：本域涟漪条仍返回（ns 闸不许把自己人一刀切）",
              Z_ASSOC in ids, str(ids))

        # ---- 2. cross：显式跨域语义保留（不许顺手改 cross 行为）----
        res_x = cli.cmd_recall(_mk_args(cross=True))
        ids_x = [it["id"] for it in res_x.get("items", [])]
        print("  [evidence] cross=True mode=%s items=%s" % (
            res_x.get("mode"), [(it["id"], it.get("namespace")) for it in res_x.get("items", [])]))
        check("2.1 cross：跨域条仍可见（cross 语义不回归）", X_ASSOC in ids_x, str(ids_x))
        check("2.2 cross：本域两条也还在", Z_MAIN in ids_x and Z_ASSOC in ids_x, str(ids_x))

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for _k, _v in _old_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # ---- 3. 静态断言：两份 CLI 涟漪段都含 ns 闸特征 ----
    for p in CLI_COPIES:
        with open(p, encoding="utf-8") as f:
            src = f.read()
        i = src.index("# P5.0-e 修饰器：associated = 涟漪联想")
        j = src.index("items += _extra[:int(args.top or 6)]", i)
        seg = src[i:j]
        check("3.x 涟漪段含 ns 闸特征 %s" % os.path.basename(os.path.dirname(os.path.dirname(p))) + "/" + os.path.basename(p),
              "_auth_ns" in seg and "cross" in seg,
              "涟漪段未出现 _auth_ns/cross，ns 闸可能没补上")

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== 单E 涟漪 ns 闸守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_e_associated_ns_gate():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
