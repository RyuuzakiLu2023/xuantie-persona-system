# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · xtmem_d1_version_flip.py · 总导游图（改代码前必读）
#
#   ▍这是什么
#   D1-1 的【后半段】：把 user_version 2→3 与代码常量 EXPECTED_SCHEMA_VERSION 2→3 在同一条
#   命令里完成，并提示必须重启的长驻进程。为什么单独一个脚本：这两步不能分开做（见铁律）。
#
#   ▍文件从上到下的结构
#   常量 OPEN_DB_COPIES → _precheck() → _edit_constants() → _bump_db() → _cli()
#   流程：体检（列齐+回填完） → 改 open_db.py 常量 → 抬 PRAGMA user_version → 打印重启清单
#
#   ▍修改铁律（血泪教训）
#   1. 跑完必须立刻重启长驻进程，一个都不能留：9900 网关 / 20000 api_server / 8766 serve /
#      MCP。原因：api_server 每个请求都调一次 open_db()，进程内存里仍是旧常量 EXPECTED=2，
#      库抬到 3 而进程不换码 = 每次请求都命中「版本不匹配 → 只读降级 + schema_mismatch 事件」。
#      这是 Y7 事故（2026-09-04，代码先行/库滞后 → 全写链路降级）的镜像版，方向不同病灶相同。
#   2. 【勿】单独抬库或单独改常量：任一边先行都会打开降级窗口。本脚本存在的意义就是把两步
#      焊成一步，别把它拆成两个命令分开跑。
#   3. 前置检查不过就别抬：列不齐=迁移没跑；illegal_pointers>0=回填没做。抬完 D1-1 白干。
#
#   ▍更新记录
#   *更新：2026-09-09 · 创建（D1 正式工单，assistant-c）*
# ================================================================
import argparse
import json
import re
import sys
import time

try:  # 包内与同目录直跑两态：插件根统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
common.ensure_importable()

from memory.open_db import open_db          # noqa: E402
from xtmem_migrate_d1 import verify_pointer  # noqa: E402

# 需要同步"期望 schema 版本"的 open_db.py 副本。
# ⚠️ 默认只列**本次运行实际加载的那一份**（由 xtmem_common 解析）；
#    多副本部署（插件目录 / 另一棵树 / 另一个 profile）请用
#    $XTMEM_OPEN_DB_COPIES（逗号分隔）显式列出——公开仓不知道你的部署布局，
#    写死路径只会让下一台机器在错误的文件上改版本号。
_loaded_open_db = os.path.join(common.plugin_root(), "memory", "open_db.py")     if common.plugin_root() else ""
OPEN_DB_COPIES = common.env_list("XTMEM_OPEN_DB_COPIES") or (
    [_loaded_open_db] if _loaded_open_db and os.path.exists(_loaded_open_db) else [])
CONST_RE = re.compile(r"(EXPECTED_SCHEMA_VERSION\s*=\s*)(\d+)")
MUST_RESTART = [
    "hermes gateway run --profile personal（9900）",
    "memory.api_server（20000）",
    "xtmem.py serve（8766）",
    "xtmem_mcp（MCP 服务进程）",
    "DSH/zcode 侧 hook 常驻进程（如有）",
]


def _precheck(conn) -> dict:
    """抬版本的前置：D1 六列齐 + 违规指针归零 + 新列已有数据。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(memory_evolution)")}
    need_c = {"retire_reason", "retired_by", "retired_at"}
    need_e = {"adjudicator", "policy_version", "decision_reason"}
    pc = verify_pointer(conn)
    filled = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE retire_reason != ''"
    ).fetchone()[0]
    return {
        "chunks_cols_ok": need_c <= cols,
        "evo_cols_ok": need_e <= ecols,
        "illegal_pointers": pc["illegal_pointers"],
        "backfilled_rows": int(filled),
    }


def _edit_constants(to_version: int, dry: bool):
    """把每处 open_db.py 的 EXPECTED_SCHEMA_VERSION 改成 to_version（幂等：已是目标值则跳过）。"""
    out = []
    for path in OPEN_DB_COPIES:
        try:
            with open(path, encoding="utf-8") as f:
                t = f.read()
        except FileNotFoundError:
            out.append({"path": path, "status": "absent（该树无运行位 open_db，跳过）"})
            continue
        m = CONST_RE.search(t)
        if not m:
            out.append({"path": path, "status": "❌ 没找到 EXPECTED_SCHEMA_VERSION，人工确认"})
            continue
        cur = int(m.group(2))
        if cur == to_version:
            out.append({"path": path, "status": "已是 %d，跳过" % to_version})
            continue
        new_t = CONST_RE.sub(lambda mm: mm.group(1) + str(to_version), t, count=1)
        if not dry:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_t)
        out.append({"path": path, "status": "%d -> %d%s" % (cur, to_version, "（dry-run 未写）" if dry else "")})
    return out


def _cli() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_d1_version_flip")
    ap.add_argument("--db", default=r"~/.xtmem/vector_store.db")
    ap.add_argument("--to", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    # 迁移旁路：抬版本这件事本身就发生在「常量与库暂时不一致」的窗口里，必须绕过版本闸
    conn = open_db(a.db, timeout=60, bypass_version_gate=True)
    try:
        pre = _precheck(conn)
        ver0 = int(conn.execute("PRAGMA user_version").fetchone()[0])
        print("=== 前置体检 ===")
        print(json.dumps(pre, ensure_ascii=False, indent=2))
        print("当前 user_version =", ver0)
        if not (pre["chunks_cols_ok"] and pre["evo_cols_ok"]):
            print("❌ D1 六列不齐——先跑 xtmem_migrate_d1.py，别抬版本")
            return 2
        if pre["illegal_pointers"] > 0 or pre["backfilled_rows"] == 0:
            print("❌ 回填未完成（违规指针 %d / 已回填 %d）——先跑 xtmem_migrate_d1.py"
                  % (pre["illegal_pointers"], pre["backfilled_rows"]))
            return 2
        if ver0 == a.to:
            print("库已是 %d，只核常量" % a.to)
        else:
            if not a.dry_run:
                conn.execute("PRAGMA user_version = %d" % a.to)
                conn.execute(
                    "INSERT INTO xtmem_events (ts, kind, agent, meta) VALUES (?, 'd1-version-flip', 'xiaod', ?)",
                    (time.time(), json.dumps({"from": ver0, "to": a.to, "dry_run": a.dry_run})),
                )
                conn.commit()
            print("库 user_version: %d -> %d%s" % (ver0, a.to, "（dry-run 未写）" if a.dry_run else ""))
        print("=== 代码常量 ===")
        for line in _edit_constants(a.to, a.dry_run):
            print("  ", line["status"], "|", line["path"])
        print("=== 必做：立刻重启以下进程（铁律 1）===")
        for p in MUST_RESTART:
            print("   -", p)
        print("=== 重启后自检 ===")
        print("   python xtmem_health.py --db <库>   # 不应出现 schema_mismatch / 只读降级")
        print("   SELECT COUNT(*) FROM xtmem_events WHERE kind='schema_mismatch' AND ts>?  # 应为 0")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_cli())
