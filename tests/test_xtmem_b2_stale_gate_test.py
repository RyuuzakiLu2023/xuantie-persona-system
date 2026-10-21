# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_b2_stale_gate_test.py · B2 锚点补召 lineage-stale 闸守卫
#
#   ▍这是什么
#   检索面修复二合一（2026-09-13 工单）件②的钉子：CLI recall 的 B2 锚点补召段
#   （score=0.35 补位尾巴）曾绕开 hybrid_retriever.search() 合并循环的
#   lineage-stale fail-closed 闸（deploy:799），带病条经补召外溢
#   （466c 发现项 2，mem-10c009c4 实锤：探针 search 被挡、CLI recall 漏出）。
#   本守卫用临时夹具库验证：带标条双通道双挡（主通道 search + CLI 补召）、
#   正常条补位不受伤、补召通道本体仍活着（防闸加错把补召全杀）。
#
#   ▍怎么跑
#   python test_xtmem_b2_stale_gate_test.py   （或 pytest 本文件）
#   全程 XTMEM_DB_PATH 指向 tempfile 临时库 + XTMEM_ACTOR=zcode，零生产写。
#
#   ▍文件从上到下的结构
#   常量区（夹具 id / 锚点 query） → _guard()：建临时库 → 灌夹具三条
#   → importlib 加载生产 CLI（mimox-data/xtmem/xtmem.py，MCP 回落同款路径）
#   → 主通道直查 → cmd_recall 全链路 → 断言。
#
#   ▍修改铁律
#   1. 本文件四副本同步（mimox-data/xtmem-project/tests、XTMEM/tests、
#      xtmem-project/tests、XTMEM-public/tests），sha 不等即视为未交付。
#   2. 夹具锚点 token 用 "b2alpha-gate-probe" 系（罕见 ASCII 串），改夹具
#      正文必须同步改 QUERY——extract_anchors 靠它触发补召，改岔了守卫空转。
#   3. 断言只用临时夹具库，严禁连生产库；禁改 CLI/engine 源码来"喂"本守卫。
#
#   ▍更新记录
#   *更新：2026-09-13 · 创建（检索面修复二合一 件②守卫，maintainer代执）*
#   *更新：2026-09-13 · stale判据收紧单：夹具病条由裸词改真标记后缀式（判据已收紧
#   为只认带冒号真标记），新增裸词条钉子断言 1.3/2.6（裸词是技术标签不得屏蔽），
#   top 3→4（夹具 3→4）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import argparse
import importlib.util
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

# 生产 CLI = MCP 回落子进程同款路径（xtmem_mcp.py XTMEM_PY 绝对候选）；引擎 = deploy 树
MASTER = _REPO / "src" / "hermes-plugin"
CLI_PATH = _REPO / "xtmem"/ "xtmem.py"

STALE_ID = "mem-b2s1"   # 带 lineage-stale 真标记（后缀式）的夹具条（病条）
N1_ID = "mem-b2n1"      # 正常条甲（锚点与病条相同，考"同锚不同命"）
N2_ID = "mem-b2n2"      # 正常条乙（独立锚点）
N3_ID = "mem-b2n3"      # 裸词条（tags 含裸词 lineage-stale）——2026-09-13 收紧后
                        # 判据只认真标记，裸词条必须照常召回（防收紧变"一刀切全屏蔽"）
# 夹具四条全 refined：分数低于 refined 首跳阈值（实测 mode=raw-fallback 空手）→
# raw 池为空、主通道结构性零召回，锚点命中条全走 0.35 补位尾巴——补召必然开火。
# top=4 必须大于夹具总数：rerank_with_anchors 会按 top 截断，top 小了正常条被砍。
QUERY = "b2alpha-gate-probe b2beta-gate-probe 巡检"

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM B2 锚点补召 lineage-stale 闸守卫 ===")

    if MASTER not in sys.path:
        sys.path.insert(0, MASTER)
    from memory.vector_store import VectorStore

    tmp_dir = tempfile.mkdtemp(prefix="xtmem-b2-gate-")
    db_path = os.path.join(tmp_dir, "guard.db")
    # ⚠️ 顺序契约：XTMEM_DB_PATH/XTMEM_ACTOR 必须在 CLI 模块 import 前设好——
    # CLI 在模块顶层读环境变量定 DEFAULT_DB，import 后再设就是空设（会打到生产库）。
    # 2026-09-13 二合二件②：先存原值再覆盖，finally 里还原（原无则 pop）——
    # 直写不清理会连坐同进程其后所有用例（466d 实测 64 errors 全因此）。
    _old_env = {k: os.environ.get(k) for k in ("XTMEM_DB_PATH", "XTMEM_ACTOR")}
    os.environ["XTMEM_DB_PATH"] = db_path
    os.environ["XTMEM_ACTOR"] = "zcode"

    try:
        conn = None
        vs = VectorStore(db_path)
        conn = vs._get_conn()
        now = time.time()
        fixtures = [
            # (id, content, tags)——病条 tags 带真标记后缀式（2026-09-13 收紧后病条
            # 定义=真标记；裸词是技术关键词不是病条，由 N3 钉住反向语义）
            (STALE_ID, "b2alpha-gate-probe 带病条部署在主机丙", '["lineage-stale:b2gate01"]'),
            (N1_ID, "b2alpha-gate-probe 正常条甲部署在主机甲", '["xtmem"]'),
            (N2_ID, "b2beta-gate-probe 正常条乙部署在主机乙", '["xtmem"]'),
            (N3_ID, "b2alpha-gate-probe 裸词条部署在主机丁", '["lineage-stale"]'),
        ]
        for cid, content, tags in fixtures:
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, content, timestamp, state, tier, "
                "namespace, visibility, updated_at, tags) "
                "VALUES (?, ?, ?, 'active', 'refined', 'zcode', 'shared', ?, ?)",
                (cid, content, now, now, tags),
            )
        conn.commit()
        conn.close()

        spec = importlib.util.spec_from_file_location("xtmem_cli_b2gate", CLI_PATH)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        builder = cli.get_builder()

        # ---- 1. 主通道（hybrid search 合并循环，799 行闸所在地）----
        hits = builder._retriever.search("b2alpha-gate-probe", top_k=10)
        hit_ids = [d.id for d, _ in hits]
        check("1.1 主通道：带标条被 799 闸挡住（search 直查无病条）",
              STALE_ID not in hit_ids, str(hit_ids))
        check("1.2 主通道：正常条甲可命中（夹具/分词自检，防守卫空转假绿）",
              N1_ID in hit_ids, str(hit_ids))
        check("1.3 主通道：裸词技术标签不屏蔽（收紧反证钉子，松判据回潮即红）",
              N3_ID in hit_ids, str(hit_ids))

        # ---- 2. CLI recall 全链路（主循环 + B2 锚点补召尾巴）----
        args = argparse.Namespace(
            query=QUERY, top=4, cross=True, namespace=None, project=None,
            entity=None, mode=None, rerank=None, time_decay=None,
            role_valve=None, as_agent=None,
        )
        res = cli.cmd_recall(args)
        items = res.get("items", [])
        ids = [it["id"] for it in items]
        print("  [evidence] mode=%s items=%s" % (
            res.get("mode"),
            [(it["id"], it.get("score"), it.get("b2_like", False)) for it in items]))
        check("2.1 补召通道：带标条不外溢（本单核心断言，修复前补位漏=红）",
              STALE_ID not in ids, str(ids))
        check("2.2 正常条不受伤：甲在结果里", N1_ID in ids, str(ids))
        check("2.3 正常条不受伤：乙在结果里", N2_ID in ids, str(ids))
        check("2.6 补召通道：裸词条不屏蔽（收紧反证钉子，松判据回潮即红）",
              N3_ID in ids, str(ids))
        b2_items = [it for it in items if it.get("b2_like")]
        check("2.4 锚点命中条至少一条存活（防闸加错把补召全杀）",
              len(items) >= 1,
              str([(it["id"], it.get("score")) for it in items]))
        check("2.5 补位分数口径：补位条 score=0.35（改动不许顺手改分）",
              all(abs(float(it.get("score", 0)) - 0.35) < 1e-6 for it in b2_items),
              str([it.get("score") for it in b2_items]))

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # env 还原（二合二件②）：原无值的 pop 掉、原有的写回；
        # 上面的"import 前设值"顺序契约不动。
        for _k, _v in _old_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== B2 补召闸守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_b2_stale_gate():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
