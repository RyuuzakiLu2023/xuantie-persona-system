# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · xtmem_migrate_d1.py · 总导游图（改代码前必读，读完再动手）
#
#   ▍这是什么
#   ① D1 事实态治理的 schema 迁移器：chunks/memory_evolution 补 6 列 + 存量 1226 条
#      原因字串迁进新列 + user_version 2→3（可分离）。② 技术栈：纯 sqlite3 + open_db。
#   ③ 依赖：memory.schema.ensure_xtmem_schema（建列唯一实现）、memory.open_db（连接与
#      版本闸，本脚本是唯一合法走 bypass_version_gate 的调用方）。
#   ④ 给谁看：执行迁移的人（maintainer/用户A）+ D1 守卫 tests/xtmem_d1_guard_test.py。
#
#   ▍文件从上到下的结构
#   常量 → _forget_initiators() → migrate()（建列 → 回填 → 版本 → 事件）→ verify_pointer()
#        → _cli() → __main__
#   流程：读版本 → ensure_xtmem_schema → 三类回填 → 写 d1-migration 事件 → 指针体检
#
#   ▍修改铁律（血泪教训，详见 错题本 / Spec v1.2 §D1）
#   1. user_version 只在 --bump-version 时写，默认不碰。原因：9900 网关 / 20000 api_server /
#      8766 serve / MCP 都是长驻进程，内存里仍是 EXPECTED_SCHEMA_VERSION=2，而 api_server
#      每请求调一次 open_db()——库先抬到 3 而进程不重启 = 每次请求立刻只读降级 + 刷
#      schema_mismatch 事件，正是 Y7 事故（2026-09-04，代码先行库滞后 → 全写链路降级）的
#      镜像重演。真翻转必须与「改代码常量 + 重启」同窗。
#   2. 回填三段的 WHERE 必须靠「把 superseded_by 置空」自我失效，重跑第二遍必须 0 更新。
#      幂等是硬契约：生产环全在演练副本跑过才允许上真库，第二遍非 0 = 契约破，别去改断言。
#   3. 绝不把原因字串写回 superseded_by（D1 的全部意义就是这两者分离）；也绝不把 state
#      写成 archived（触发血缘传播 → refined 被打 lineage-stale → 蒸馏产物自杀）。
#   4. 迁移连接走 open_db(bypass_version_gate=True)。别在别处传这个参数，也别自己
#      sqlite3.connect——守卫 2 规定全源码树只许 open_db 内有 connect。
#
#   ▍存量口径（本脚本只认这三类，别扩大）
#   332 条 superseded + superseded_by='cleanup-20260827-prompt-residue' → 2026-08-27 清理批
#     手工退役，retired_by=manual-batch-20260827，retired_at=该行 updated_at
#   1 条 superseded + 'p41fix-flow-polluted' → p41fix 脚本，retire_reason 归一 'p41fix'
#   893 条 archived + 'forget' → 发起者从 xtmem_events(kind=forget,target_chunk=id).agent 查，
#     查不到落 cli-forget-unknown（实测 892 可连 / 1 兜底）
#   合计 1226 条，与工单口径一致；163 条真指针（mem-xxxxxxxx）一条都不许动。
#
#   ▍更新记录
#   *更新：2026-09-09 · 创建（D1 正式工单，assistant-c）*
# ================================================================

import argparse
import json
import os
import re
import sys
import time

try:  # 包内与同目录直跑两态：插件根统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

common.ensure_importable()

from memory.open_db import open_db, EXPECTED_SCHEMA_VERSION
from memory.schema import ensure_xtmem_schema, D1_CHUNK_COLS, D1_EVO_COLS

# 指针形状唯一合法口径（与 VectorStore._PTR_RE 同一份语义）：真后继才配进 superseded_by
PTR = re.compile(r"^mem-[0-9a-f]{8}$")
DEFAULT_DB = common.resolve_db_path()


def _forget_initiators(conn) -> dict:
    """chunk id -> forget 发起者。同一 chunk 多条取最后一次（重试场景以最终生效者为准）。"""
    out = {}
    for r in conn.execute(
        "SELECT target_chunk, agent, MAX(ts) AS ts FROM xtmem_events "
        "WHERE kind='forget' AND target_chunk IS NOT NULL AND target_chunk!='' "
        "GROUP BY target_chunk"
    ):
        out[r["target_chunk"]] = (r["agent"] or "cli-forget-unknown", r["ts"])
    return out


def migrate(conn, dry_run: bool = False, bump_version: bool = False, to_version=None) -> dict:
    """执行 D1-1 迁移。返回结构化统计（守卫断言与回执都读它）。

    dry_run=True 只算不落；bump_version=False 时 user_version 原样保留（见铁律 1）。
    """
    ver0 = int(conn.execute("PRAGMA user_version").fetchone()[0])
    st = {"user_version_from": ver0, "columns_added": [], "backfill": {},
          "events_unmatched": 0, "dry_run": bool(dry_run)}

    have = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    have_evo = {r[1] for r in conn.execute("PRAGMA table_info(memory_evolution)")}
    st["columns_added"] = [c for c, _, _ in D1_CHUNK_COLS if c not in have]
    st["columns_added_evolution"] = [c for c, _, _ in D1_EVO_COLS if c not in have_evo]
    if dry_run:
        # ⚠️ dry-run 绝不能调 ensure_xtmem_schema：Python<3.12 的 sqlite3 只在 DML 前开隐式
        #    事务，DDL 是立即提交的——那句 rollback() 骗人，对生产库跑「预览」会把列真加上去
        #    （2026-09-09 首版实测踩中，演练副本被这么污染过一次）。预览一律只读比对。
        pass
    else:
        # 建列唯一实现在 memory/schema.py（引擎与 CLI 共用，杜绝两套 schema 漂移）
        ensure_xtmem_schema(conn)
        conn.commit()

    # 前两类一条 SQL 搞定（原因字串→retire_reason，裁决者→retired_by，时间→updated_at）
    _bf = {}
    _sql = ("UPDATE chunks SET superseded_by='', retire_reason=?, retired_by=?, "
            "retired_at=COALESCE(updated_at, timestamp) "
            "WHERE state='superseded' AND superseded_by=?")
    for key, reason, by, legacy_val in (
        ("cleanup_20260827", "cleanup-20260827-prompt-residue", "manual-batch-20260827",
         "cleanup-20260827-prompt-residue"),
        ("p41fix", "p41fix", "script-p41fix", "p41fix-flow-polluted"),
    ):
        # p41fix 口径：库里存的是 'p41fix-flow-polluted'，工单要求归一写成 'p41fix'
        if dry_run:
            _bf[key] = int(conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE state='superseded' AND superseded_by=?",
                (legacy_val,)).fetchone()[0])
        else:
            _bf[key] = conn.execute(_sql, (reason, by, legacy_val)).rowcount
    st["backfill"] = _bf

    # 第三类逐行：retired_by 要查事件发起者，SQL 一条写不出这个映射
    _ini = _forget_initiators(conn)
    rows = conn.execute(
        "SELECT id, updated_at, timestamp FROM chunks "
        "WHERE state='archived' AND superseded_by='forget'"
    ).fetchall()
    _n = 0
    for r in rows:
        agent, ts = _ini.get(r["id"], (None, None))
        if agent is None:
            agent = "cli-forget-unknown"
            st["events_unmatched"] += 1
        _rt = ts if ts else (r["updated_at"] or r["timestamp"])
        if not dry_run:
            conn.execute(
                "UPDATE chunks SET superseded_by='', retire_reason='forget', "
                "retired_by=?, retired_at=? WHERE id=?", (agent, _rt, r["id"])
            )
        _n += 1
    st["backfill"]["forget"] = _n
    st["backfill"]["total"] = sum(st["backfill"].values())

    if not dry_run:
        conn.commit()

    if bump_version:
        # 默认目标=代码常量 EXPECTED_SCHEMA_VERSION（改常量即改目标，避免两处硬编码各说各话）。
        # 首版这里踩过坑：常量还是 2 时 --bump-version 静默写成 2，看着像成功实则没动——
        # 所以现在目标 <= 当前版本就直接拒绝，宁可报错退出。
        target = int(to_version) if to_version is not None else int(EXPECTED_SCHEMA_VERSION)
        if target <= ver0 and not dry_run:
            raise SystemExit(
                "拒绝抬版本：目标 %d <= 当前 %d。要么代码常量还没改（open_db.EXPECTED_SCHEMA_VERSION），"
                % (target, ver0) + "要么本库已迁过。生产翻转必须与「改常量 + 重启长驻进程」同窗，见文件头铁律 1。")
        if to_version is not None and int(to_version) != int(EXPECTED_SCHEMA_VERSION):
            st["version_warning"] = (
                "--to=%s 与代码常量 EXPECTED_SCHEMA_VERSION=%s 不一致：抬完库后所有进程都会只读降级，"
                % (to_version, EXPECTED_SCHEMA_VERSION) + "除非同窗改常量并重启")
        st["user_version_to"] = target if not dry_run else ver0
        if not dry_run:
            conn.execute("PRAGMA user_version = %d" % target)
            conn.commit()
    else:
        st["user_version_to"] = ver0

    if not dry_run:
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, meta) VALUES (?, 'd1-migration', "
            "'xiaod', ?)",
            (time.time(), json.dumps(st, ensure_ascii=False)[:1800])
        )
        conn.commit()
    return st


def verify_pointer(conn) -> dict:
    """指针体检：superseded_by 只许真指针或空；旧原因字串必须 0 遗留。"""
    bad = []
    legacy = 0; good = 0
    for r in conn.execute(
        "SELECT superseded_by FROM chunks WHERE superseded_by IS NOT NULL AND superseded_by!=''"
    ):
        v = r["superseded_by"]
        if PTR.match(v):
            good += 1
        else:
            legacy += 1
            if len(bad) < 5:
                bad.append(v)
    filled = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE retire_reason!='' OR retired_by!=''"
    ).fetchone()[0]
    return {"illegal_pointers": legacy, "valid_pointers": good,
            "samples": bad, "new_columns_filled": int(filled)}


def _cli() -> int:
    ap = argparse.ArgumentParser(prog="xtmem_migrate_d1")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true", help="只统计不落库")
    ap.add_argument("--bump-version", action="store_true",
                  help="同时把 user_version 抬到 EXPECTED_SCHEMA_VERSION（必须与改代码常量+重启同窗）")
    ap.add_argument("--to", dest="to_version", type=int, default=None,
                  help="显式目标 user_version（默认取 open_db.EXPECTED_SCHEMA_VERSION）")
    ap.add_argument("--check", action="store_true", help="只跑指针体检不迁移")
    a = ap.parse_args()
    conn = open_db(a.db, timeout=60, bypass_version_gate=True)
    try:
        if a.check:
            print(json.dumps(verify_pointer(conn), ensure_ascii=False, indent=2))
            return 0
        out = migrate(conn, dry_run=a.dry_run, bump_version=a.bump_version,
                    to_version=a.to_version)
        out["pointer_check"] = verify_pointer(conn)
        out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_cli())
