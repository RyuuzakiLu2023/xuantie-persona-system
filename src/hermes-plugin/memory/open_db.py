# -*- coding: utf-8 -*-
"""memory/open_db.py — XTMEM 公共连接函数（R5 schema 版本闸，P6 波1，2026-09-04，小D）。

v1.3.1 §三 R5 全文照抄执行：
- PRAGMA user_version 递增闸：不匹配 → 只读降级 + 写 schema_mismatch 事件；
- 降级路径自兜底（嘟嘟死穴一）：写 schema_mismatch 事件本身必须 try/except——
  库锁/只读态下事件写会二次抛异常，失败兜底落 stderr + 本地 jsonl
  （xtmem_degrade_fallback.jsonl，R6c 域外心跳兼读此文件）；
- 两轴关系（六修 F-6）：user_version=schema 轴（本闸），xtmem_meta.tokenizer_version=
  分词轴（FTS5 v2 已建 fail-closed 闸）——共存不互覆，各自校验各自降级；
- 守卫 2：sqlite3.connect( 只许出现在 open_db 内（XTMEM 源码树边界，tests 豁免，
  mode=ro 探针豁免）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# schema 轴版本。历史库=0（未管理态），open_db 首连幂等 seed 到 1；
# Y7 正式迁移（2026-09-04 18:27 生产执行完毕，CHECK 版 chunks 上线）→ 2。
# （2026-09-04 事故记录：曾提前改 2 → 生产库(uv=1)整体只读降级——版本号必须与生产库
# 实际迁移状态同步前进，不得先行；本次为迁移完成后正式对齐。）
EXPECTED_SCHEMA_VERSION = 3

_FALLBACK_JSONL = os.environ.get(
    "XTMEM_DEGRADE_FALLBACK",
    __import__("memory.public_runtime", fromlist=["home_file"]).home_file('logs/xtmem_degrade_fallback.jsonl'),
)


def _fallback_log(reason: str, err: str):
    """降级路径自兜底：事件写失败 → stderr + jsonl（R5 五修，唯一实锤设计洞）。"""
    line = json.dumps({
        "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "schema_mismatch_fallback", "reason": reason, "err": err,
    }, ensure_ascii=False)
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        p = Path(_FALLBACK_JSONL)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except Exception:
        pass  # 兜底自身失败也不许炸主流程（比不降级更糟）


def _try_write_mismatch_event(path: str, found: int, expected: int):
    """写 schema_mismatch 事件；任何异常 → 兜底 stderr+jsonl，不递归炸。"""
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'schema_mismatch', ?)",
                (time.time(), json.dumps({
                    "found": found, "expected": expected, "path": str(path),
                    "action": "readonly_degrade",
                })),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _fallback_log(
            f"user_version={found} != expected={expected}，事件写失败（库锁/只读态）", str(e)
        )


def _check_version(conn: sqlite3.Connection, path: str, writable: bool) -> tuple[sqlite3.Connection, bool]:
    """返回 (conn, degraded)。writable=False（探针/只读）只回报不降级。"""
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception as e:
        _fallback_log("user_version 读取失败", str(e))
        return conn, False
    if ver == EXPECTED_SCHEMA_VERSION:
        return conn, False
    if ver == 0 and writable:
        # 未管理库：幂等 seed 到 1（历史库首次收编，非 schema 变更）
        try:
            conn.execute(f"PRAGMA user_version = {EXPECTED_SCHEMA_VERSION}")
            conn.commit()
            return conn, False
        except Exception as e:
            _fallback_log("user_version seed 失败", str(e))
            return conn, False
    # 版本不匹配（高版本库遇旧代码 / 0 且不可写）
    _try_write_mismatch_event(path, ver, EXPECTED_SCHEMA_VERSION)
    if writable:
        # 只读降级：当前连接转只读语义（sqlite3 无运行时切只读，以"不再放行写"的
        # authorizer 实现——INSERT/UPDATE/DELETE/DDL 全部拒，SELECT 放行）
        conn.set_authorizer(_readonly_authorizer)
    return conn, True


def _readonly_authorizer(action, arg1, arg2, dbname, source):
    """只读降级 authorizer：放行读（SELECT/READ/FUNCTION/RECURSIVE/PRAGMA 只读面），拒一切写/DDL。"""
    import sqlite3 as _s
    deny_actions = {
        _s.SQLITE_CREATE_INDEX, _s.SQLITE_CREATE_TABLE, _s.SQLITE_CREATE_TEMP_INDEX,
        _s.SQLITE_CREATE_TEMP_TABLE, _s.SQLITE_CREATE_TEMP_TRIGGER, _s.SQLITE_CREATE_TRIGGER,
        _s.SQLITE_CREATE_VIEW, _s.SQLITE_DELETE, _s.SQLITE_DROP_INDEX, _s.SQLITE_DROP_TABLE,
        _s.SQLITE_DROP_TEMP_INDEX, _s.SQLITE_DROP_TEMP_TABLE, _s.SQLITE_DROP_TEMP_TRIGGER,
        _s.SQLITE_DROP_TRIGGER, _s.SQLITE_DROP_VIEW, _s.SQLITE_INSERT, _s.SQLITE_UPDATE,
        _s.SQLITE_ALTER_TABLE, _s.SQLITE_REINDEX, _s.SQLITE_SAVEPOINT,
    }
    if action in deny_actions:
        return _s.SQLITE_DENY
    return _s.SQLITE_OK


def open_db(path, ro: bool = False, timeout: float = 30.0, seed: bool = True, check_same_thread: bool = True, bypass_version_gate: bool = False) -> sqlite3.Connection:
    """XTMEM 全树唯一公共连接函数（R5）。

    - ro=True：mode=ro 直连（总则 5 探针面；只回报版本不降级不写事件）；
    - ro=False（默认）：校验 user_version ——
        * 匹配（或 0 且本连接可写、seed=True）→ 正常返回；
        * 不匹配 → 只读降级（authorizer 拒一切写）+ schema_mismatch 事件（自兜底）；
      任何"降级后写"会得到 sqlite3.DatabaseError，调用方按只读降级风暴语义处理。
    - bypass_version_gate=True：【维护专用】整段跳过 R5 版本闸。为什么必须有：抬 EXPECTED 常量
      与给库执行 ALTER 互为死锁——库还是 2、代码已喊 3，则迁移连接自己就被降级，DDL 写不进去。
      ⚠️ 严禁在常规读写路径传 True：绕过它等于拆掉 Y7 事故（代码先行/库滞后 → 全写链路只读降级）
      唯一的保护。合法调用方只有一个：scripts/xtmem_migrate_d1.py。
    """
    path = str(path)
    if ro:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        _check_version(conn, path, writable=False)
        return conn
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发写重试窗口——SQLite 默认 busy_timeout=0（碰锁立即 database is locked 丢写）；
    # MCP serve + api_server + cron 三路并发写时必须有重试窗口，否则非活跃写会直接失败。
    # 2026-09-16 审计发现-1 修复（Z指导）。
    conn.execute("PRAGMA busy_timeout=5000")
    if bypass_version_gate:
        # 迁移专用旁路：不校验、不降级、不写 schema_mismatch 事件（详见 docstring ⚠️）
        degraded = False
    else:
        conn, degraded = _check_version(conn, path, writable=True)
    # 贰③c（v1.3.2 工单增补）：embedding_version 轴——缓存模型/维度指纹以
    # xtmem_meta.embedding_version 为准（get_meta 读取），不匹配时调用方 fail-closed 退
    # BM25-only（open_db 不做硬闸——向量腿缺席不是一致性事故，降级策略留给调用方）
    if degraded:
        import logging
        logging.getLogger("xtmem.open_db").warning(
            "open_db: schema version mismatch on %s — read-only degrade", path
        )
    # 贰②（v1.3.2 工单增补）：maintenance flag——xtmem_meta.maintenance=1 时非 admin 连接
    # 走只读降级（停写 flag 制，点名进程降为兜底）；admin_bypass=True 的迁移连接不受限
    if not degraded and not _maintenance_bypass:
        try:
            row = conn.execute(
                "SELECT value FROM xtmem_meta WHERE key='maintenance'"
            ).fetchone()
            if row and str(row[0]) == "1":
                conn.set_authorizer(_readonly_authorizer)
                import logging
                logging.getLogger("xtmem.open_db").warning(
                    "open_db: maintenance flag set on %s — read-only degrade (admin bypass off)", path
                )
                try:
                    conn.execute(
                        "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'maintenance_degrade', ?)",
                        (time.time(), json.dumps({"path": path})),
                    )
                    conn.commit()
                except Exception as e:
                    _fallback_log("maintenance degrade event write failed", str(e))
        except sqlite3.OperationalError:
            pass  # xtmem_meta 尚未建表（全新库）
    # V1.3 stale 端探测（v1.3.2 立项批 #8，2026-09-05）：可写连接上报 {actor, code_sha, pid}
    # 到 xtmem_meta.runtime_actors（进程级去重，每次 open_db 只写一次）——health 比对磁盘 sha，
    # 旧码进程不匹配即亮红。上报失败【不】影响连接（自兜底总则：探测永不阻塞主链路）。
    _report_runtime_actor(conn, path)
    return conn


# ── V1.3 stale 端探测（v1.3.2 #8，2026-09-05）────────────────────
_RUNTIME_REPORTED_PIDS = set()  # 进程级去重：本 pid 本库只上报一次


def _runtime_code_sha():
    """本进程加载的 memory 包代码指纹：memory/*.py 按 relpath 排序逐个 sha256 再总哈希。
    只算一次存模块级缓存（代码不会运行中变——变了的就是 stale 端要抓的）。"""
    global _RUNTIME_CODE_SHA_CACHE
    try:
        return _RUNTIME_CODE_SHA_CACHE
    except NameError:
        pass
    import hashlib as _hl
    h = _hl.sha256()
    pkg_dir = Path(__file__).parent
    for f in sorted(pkg_dir.glob("*.py")):
        h.update(f.name.encode("utf-8", "ignore"))
        h.update(b"\0")
        h.update(f.read_bytes())
    _RUNTIME_CODE_SHA_CACHE = h.hexdigest()[:16]
    return _RUNTIME_CODE_SHA_CACHE


def _report_runtime_actor(conn, path):
    """上报本进程 {actor, code_sha, pid, ts} 到 xtmem_meta.runtime_actors（V1.3）。
    进程级去重 + 全 try/except 自兜底：探测失败【不】得影响 open_db 主链路。"""
    try:
        pid = os.getpid()
        marker = f"{pid}:{path}"
        if marker in _RUNTIME_REPORTED_PIDS:
            return
        _RUNTIME_REPORTED_PIDS.add(marker)
        import json as _json
        actor = (os.environ.get("XTMEM_ACTOR", "") or "local").strip() or "local"
        entry = {"actor": actor, "code_sha": _runtime_code_sha(), "ts": round(time.time(), 1)}
        # 表可能不存在（Y7 前老库/全新库）——上报面自建（IF NOT EXISTS 幂等，与 seed 面同构）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS xtmem_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute("SELECT value FROM xtmem_meta WHERE key='runtime_actors'").fetchone()
        data = _json.loads(row[0]) if row and row[0] else {}
        # 只留 24h 内活口（防表膨胀；stale 进程若还活着，其 open_db 周期性重连会刷新 ts——
        # 真长连接不重连的由 health 比对 code_sha 抓，不靠 ts）
        cutoff = time.time() - 86400
        data = {k: v for k, v in data.items() if float(v.get("ts", 0)) >= cutoff}
        data[str(pid)] = entry
        # V1.6 收敛（2026-09-05）：CLI 每命令一进程=每命令一 pid，24h 清理不够——
        # 每 (actor, code_sha) 只留最新 3 条（长连接常驻进程 ts 持续刷新天然居最新；
        # health stale 比对看的是"在场旧码进程"，最新 3 条/组足够表达）。
        if len(data) > 24:
            groups = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    gkey = (str(v.get("actor", "?")), str(v.get("code_sha", "?")))
                    groups.setdefault(gkey, []).append((float(v.get("ts", 0)), k))
            keep = set()
            for gkey, items in groups.items():
                items.sort(reverse=True)
                keep.update(k for _, k in items[:3])
            data = {k: v for k, v in data.items() if k in keep}
        conn.execute(
            "INSERT INTO xtmem_meta (key, value) VALUES ('runtime_actors', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_json.dumps(data),),
        )
        conn.commit()
    except Exception:
        # 自兜底（R5 口径）：探测任何失败静默——stale 探测永不阻塞主链路
        try:
            conn.rollback()
        except Exception:
            pass


def get_runtime_actors(conn):
    """读 xtmem_meta.runtime_actors（health 比对面）。无表/无键返回 {}。"""
    try:
        row = conn.execute("SELECT value FROM xtmem_meta WHERE key='runtime_actors'").fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return {}



# maintenance 旁路开关（进程级：迁移/维护脚本设 True 后 open_db 不降级）
_maintenance_bypass = False


def set_maintenance_bypass(on: bool):
    """迁移/维护连接旁路 maintenance flag（贰② admin 旁路）。用完必须关回。"""
    global _maintenance_bypass
    _maintenance_bypass = bool(on)


def get_meta(conn: sqlite3.Connection, key: str):
    """读 xtmem_meta 键值（无表/无键返回 None）。"""
    try:
        row = conn.execute("SELECT value FROM xtmem_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def set_meta(conn: sqlite3.Connection, key: str, value: str):
    """写 xtmem_meta 键值（upsert；无表则建）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS xtmem_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "INSERT INTO xtmem_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def write_txn(conn: sqlite3.Connection):
    """贰①（v1.3.2 工单增补）：写事务统一入口——BEGIN IMMEDIATE（禁隐式 deferred）。

    用法：with write_txn(conn): conn.execute(...); ...
    deferred 读转写在五写端并存下会间歇 BUSY_SNAPSHOT（busy handler 不救 deferred），
    全树写事务必须走本入口；守卫：源码树内禁裸 BEGIN/隐式事务写。
    """
    class _Txn:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn
        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False
    return _Txn()


def guard_bulk_cache_delete(conn: sqlite3.Connection, will_delete_rows: int, where: str = "") -> bool:
    """叁②（防再删三层，v1.3.2 工单增补）：批量删 embedding_cache 守卫。

    单语句将删 >500 行时：无 XTMEM_ALLOW_BULK_CACHE_DELETE=1 旗语 → raise RuntimeError
    （调用方回滚）并写 kind='cache_bulk_delete_blocked' 事件；有旗语则放行并写
    kind='cache_evicted' 事件（留痕）。
    """
    import os
    if will_delete_rows <= 500:
        return True
    allowed = os.environ.get("XTMEM_ALLOW_BULK_CACHE_DELETE") == "1"
    kind = "cache_evicted" if allowed else "cache_bulk_delete_blocked"
    meta = json.dumps({"rows": will_delete_rows, "where": where, "allowed": allowed})
    try:
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, ?, ?)",
            (time.time(), kind, meta),
        )
        conn.commit()
    except Exception as e:
        _fallback_log(f"bulk cache delete guard event failed ({kind})", str(e))
    if not allowed:
        raise RuntimeError(
            f"bulk embedding_cache delete blocked: {will_delete_rows} rows > 500 "
            f"(set XTMEM_ALLOW_BULK_CACHE_DELETE=1 to allow); where={where}"
        )
    return True

