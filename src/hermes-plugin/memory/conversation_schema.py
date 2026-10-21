# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · conversation_schema.py（CST01 · 跨会话 feature schema + 版本闸）
#
# ▍这是什么
# ① 干什么：把 Spec v2.1 §14.1 的 additive feature schema 落成**幂等迁移**，
#    并给出“这个库到底能不能用 cross_session 功能”的**唯一判定**（feature 轴）。
# ② 技术栈：纯 Python + sqlite3；DDL 与 flag 写在**同一个事务**里。
# ③ 依赖：只依赖 sqlite3 与 memory.open_db（取公共连接语义的调用方自己传连接）。
# ④ 给谁看：CST01 施工者、CST02/CST03 后续消费者、O02 诊断维护者。
#
# ▍两条轴（改前必读，血泪）
#   · 核心 schema 轴 = PRAGMA user_version（memory/open_db.py 的 EXPECTED_SCHEMA_VERSION）
#   · feature 轴     = xtmem_meta['feature_schema.cross_session']
# 本模块**只**动 feature 轴，**绝不**改 user_version：
#   §14.1 明令「纯新增表不先把全库 EXPECTED_SCHEMA_VERSION 改成 4」。
#   提前改核心版本号 = 所有旧代码连接整库只读降级（2026-09-04 事故同款形态）。
#
# ▍为什么 flag 不能单独代表就绪（R06 抓过的形态）
#   “只有 flag=1、表却缺列”的库会被旧口径判成 enabled=true，于是一路写不存在的结构。
#   本模块的就绪判定 = flag 值正确 **且** 必需表/列/索引逐项齐备；
#   任一缺失 → ready=False 并给出**可归因**的 reason（缺哪个表/列/索引）。
#
# ▍修改铁律
# 1. 【不】把 DDL 与 flag 拆成两个事务：半建表 + flag 已开 = 谎报就绪，且没有回滚点。
# 2. 【不】把 set_feature_flag 单独暴露成“先开旗再建表”的用法——迁移只有 ensure_feature_schema 一个入口。
# 3. 【不】在文本比较里忽略大小写或空白去凑 flag（'1 ' 不是 1）。
# 4. 表名以 Spec 参考 DDL（spec-v2.1/conversation_schema.sql）为准，【不】另起别名。
#    ★F03 已收口（2026-10-16）：doctor 不再自己另算一套 feature 规则，而是**直接消费**
#      本模块的 feature_state()（含 FTS/触发器/唯一约束的定义核验）。旧病是“同一座库一边
#      拒写、一边亮绿灯”——两处各维护一份部分规则必然漂移；现在只剩**一个**判定实现。
#      ⚠️【不】再往 doctor 里抄表名列名或重写完整性规则：抄一份 = 又造一个漂移点。
#
# ▍外号表
# "feature 轴" → xtmem_meta['feature_schema.cross_session']
# "就绪闸"     → feature_state()['ready']（capture 写入前唯一要问的东西）
#
# ▍R06 返修（2026-10-16 · 独立验收实测，改前必读）
#   病根：就绪闸只核对象**名字**在不在，不核它**是不是那么回事**。三条实测假就绪：
#     ① 删 cs_events.host_locator_json → 仍 ready，而 capture 的 INSERT 明确要它；
#     ② 删 cs_events_fts              → 仍 ready，原话搜索已经没索引；
#     ③ 把不变量触发器换成同名 `SELECT 1` 空壳 → 仍 ready，随后能原地 UPDATE 原话。
#   修法：必需列补 host_locator_json；FTS/同步触发器/不可变触发器改为**核 DDL 定义**；
#        唯一约束按**列集**核（名字可以改，列集才是语义）。
#   ⚠️【不】靠“后面 INSERT 会炸”或“search 返回空”兜底：那两种表现会被当成
#      “没有历史”或“偶发写失败”，正是 I18 要消灭的不可归因形态。
#   ⚠️ 也【不】自动破坏性重建未知旧表：结构不对就报不齐 + 给 reason，由部署侧决定处置。
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-R06）：就绪判定改为核定义（FTS/同步/不可变触发器）+
#   唯一约束列集 + 补 host_locator_json；同名空壳不再算就绪*
# *创建：2026-10-16 · 小D（CST01）：additive DDL + flag 同事务迁移 + 可归因就绪判定*
# ================================================================
"""CST01 跨会话 feature schema（additive）+ feature 版本闸。"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

FEATURE_FLAG_KEY = "feature_schema.cross_session"
FEATURE_FLAG_VALUE = "1"


class FeatureSchemaMismatch(RuntimeError):
    """feature 轴不可用（未迁移 / flag 关 / 结构不齐）。对应 Spec §12 的 SCHEMA_MISMATCH。"""

    code = "SCHEMA_MISMATCH"

    def __init__(self, reason: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("SCHEMA_MISMATCH: " + reason)
        self.reason = reason
        self.detail = detail or {}


# 参考 DDL 里与 CST01（capture + 来源范围）直接相关的表。
# ⚠️【不】顺手把 cs_topics/cs_topic_* / cs_routes 也建出来：那些是 CST02（路由/话题）的范围，
#    本单建了却不用 = 给下游预填“已具备”的假象（派单卡明令不预填下游能力）。
_DDL_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS cs_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    format_version INTEGER NOT NULL CHECK(format_version = 1),
    profile TEXT NOT NULL,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL CHECK(chat_type IN ('dm','group','thread')),
    sender_principal_id TEXT NOT NULL,
    sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human','agent','tool','platform')),
    identity_status TEXT NOT NULL DEFAULT 'verified' CHECK(identity_status IN ('verified','partial','unknown')),
    source_message_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
    session_id TEXT,
    reply_to_message_id TEXT,
    root_message_id TEXT,
    thread_id TEXT,
    source_timestamp REAL,
    source_time_status TEXT NOT NULL CHECK(source_time_status IN ('verified','observed_only','unknown')),
    observed_at REAL NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('inbound','assistant_published','message_edited','message_retracted','imported','media_transcript','platform_notice')),
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    search_text TEXT NOT NULL DEFAULT '',
    reader_scope_kind TEXT NOT NULL CHECK(reader_scope_kind IN ('dm_owner','group','explicit','agent_private','knowledge_shared','unknown')),
    reader_scope_id TEXT NOT NULL,
    disclosure_class TEXT NOT NULL CHECK(disclosure_class IN ('normal','personal','restricted','unknown')),
    host_locator_json TEXT,
    observe_allowed INTEGER NOT NULL CHECK(observe_allowed IN (0,1)),
    reply_allowed INTEGER NOT NULL CHECK(reply_allowed IN (0,1)),
    CHECK(source_time_status <> 'verified' OR source_timestamp IS NOT NULL),
    UNIQUE(profile,platform,tenant_id,bot_id,chat_id,source_message_id,source_revision,kind)
)""",
    "CREATE INDEX IF NOT EXISTS cs_events_scope_seq ON cs_events(profile,reader_scope_kind,reader_scope_id,event_seq)",
    "CREATE INDEX IF NOT EXISTS cs_events_sender_time ON cs_events(profile,sender_principal_id,source_timestamp,event_seq)",
    "CREATE INDEX IF NOT EXISTS cs_events_reply ON cs_events(profile,platform,chat_id,reply_to_message_id)",
    # 原文不可原地更新：证据只许**追加 revision**，不许 UPDATE 改写（Spec §5.2/§5.3）。
    # ⚠️【不】把这条触发器删掉去换“好改”。它是 T08“隔天原话仍可找”的物证：
    #    任何改写原话的路径都会撞 RAISE(ABORT)，而不是悄悄把历史洗白。
    """CREATE TRIGGER IF NOT EXISTS cs_events_immutable
BEFORE UPDATE OF evidence_id,operation_id,format_version,profile,platform,tenant_id,bot_id,chat_id,chat_type,
sender_principal_id,sender_kind,source_message_id,source_revision,source_timestamp,observed_at,kind,text,content_hash,
reader_scope_kind,reader_scope_id,disclosure_class ON cs_events
BEGIN SELECT RAISE(ABORT,'immutable conversation evidence; append a revision'); END""",
    # 来源“最新态”不rewrite证据：撤回/编辑/权限收紧都往 heads 里记状态。
    """CREATE TABLE IF NOT EXISTS cs_source_heads (
    profile TEXT NOT NULL, platform TEXT NOT NULL, tenant_id TEXT NOT NULL,
    bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, source_message_id TEXT NOT NULL,
    event_seq INTEGER REFERENCES cs_events(event_seq) ON DELETE SET NULL,
    source_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('intact','retracted','redacted','revision_uncertain')),
    acl_revision INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY(profile,platform,tenant_id,bot_id,chat_id,source_message_id)
)""",
    """CREATE TABLE IF NOT EXISTS cs_redactions (
    source_key TEXT PRIMARY KEY,
    redacted_at REAL NOT NULL,
    authorization_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL
)""",
    # 受众授权：只由可信授权侧写（本单只提供写入入口，不在 adapter 里调用）。
    # ⚠️【不】靠显示名/自然语言补授权（Spec §5.3）。
    """CREATE TABLE IF NOT EXISTS cs_event_audiences (
    event_seq INTEGER NOT NULL REFERENCES cs_events(event_seq) ON DELETE CASCADE,
    audience_kind TEXT NOT NULL CHECK(audience_kind IN ('principal','group','capability')),
    audience_id TEXT NOT NULL,
    authorization_ref TEXT NOT NULL,
    grant_revision INTEGER NOT NULL,
    revoked_at REAL,
    PRIMARY KEY(event_seq,audience_kind,audience_id)
)""",
    "CREATE INDEX IF NOT EXISTS cs_audiences_subject ON cs_event_audiences(audience_kind,audience_id,event_seq)",
    # FTS 外挂索引（content=cs_events）。本单的薄读服务不依赖它（见 conversation_capture.search_events），
    # 但结构先建好，等 CST03 的有权限检索接入时不用再动迁移。
    """CREATE VIRTUAL TABLE IF NOT EXISTS cs_events_fts USING fts5(
    search_text, content='cs_events', content_rowid='event_seq'
)""",
    """CREATE TRIGGER IF NOT EXISTS cs_events_fts_insert AFTER INSERT ON cs_events BEGIN
    INSERT INTO cs_events_fts(rowid,search_text) VALUES(new.event_seq,new.search_text);
END""",
    """CREATE TRIGGER IF NOT EXISTS cs_events_fts_delete AFTER DELETE ON cs_events BEGIN
    INSERT INTO cs_events_fts(cs_events_fts,rowid,search_text) VALUES('delete',old.event_seq,old.search_text);
END""",
    """CREATE TRIGGER IF NOT EXISTS cs_events_fts_search_update AFTER UPDATE OF search_text ON cs_events BEGIN
    INSERT INTO cs_events_fts(cs_events_fts,rowid,search_text) VALUES('delete',old.event_seq,old.search_text);
    INSERT INTO cs_events_fts(rowid,search_text) VALUES(new.event_seq,new.search_text);
END""",
)

# 就绪判定要逐项核对的必需结构（缺一即不 ready）。
FEATURE_REQUIRED_TABLES: Dict[str, Tuple[str, ...]] = {
    "cs_events": (
        "event_seq", "evidence_id", "operation_id", "format_version", "profile",
        "platform", "tenant_id", "bot_id", "chat_id", "chat_type",
        "sender_principal_id", "sender_kind", "identity_status",
        "source_message_id", "source_revision", "session_id",
        "reply_to_message_id", "root_message_id",
        # F03（2026-10-16）：INSERT 明确要 thread_id，而清单漏了它 —— 实测删掉该列
        # 仍 ready=True，写的时候才炸。缺列必须在**启用前**拒绝并归因。
        "thread_id",
        "source_timestamp", "source_time_status", "observed_at", "kind",
        "text", "content_hash", "search_text",
        "reader_scope_kind", "reader_scope_id", "disclosure_class",
        "host_locator_json", "observe_allowed", "reply_allowed",
    ),
    "cs_source_heads": (
        "profile", "platform", "tenant_id", "bot_id", "chat_id",
        "source_message_id", "event_seq", "source_revision", "state", "acl_revision",
    ),
    "cs_event_audiences": (
        "event_seq", "audience_kind", "audience_id",
        "authorization_ref", "grant_revision", "revoked_at",
    ),
}
FEATURE_REQUIRED_INDEXES: Tuple[str, ...] = (
    "cs_events_scope_seq", "cs_events_sender_time", "cs_events_reply", "cs_audiences_subject",
)

# 唯一约束（按**索引列集**核，不按索引名核 —— 名字可以随便改，列集才是语义）。
# 为什么必须核：I05 的幂等承诺最终靠这两条唯一约束兜底；把它拆掉后
#   “重复投递只一次效果”就只能靠 Python 分支，而并发下那个分支必然会被穿过。
FEATURE_REQUIRED_UNIQUE_SETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("cs_events", ("operation_id",)),
    ("cs_events", ("evidence_id",)),
    ("cs_events", ("profile", "platform", "tenant_id", "bot_id", "chat_id",
                   "source_message_id", "source_revision", "kind")),
)

# 定义核验表 (type, name, 必须出现在 sqlite_master.sql 里的正则)。
# ⚠️ 必须配 1（R06）：**存在**是最便宜的伪装，任何空壳都能叫 cs_events_immutable；
#    实测过把不变量触发器换成同名 `SELECT 1`，就绪闸照样放行，然后原话被 UPDATE 掉了。
FEATURE_REQUIRED_DEFINITIONS: Tuple[Tuple[str, str, str], ...] = (
    ("table", "cs_events_fts", r"\bfts5\b"),
    ("table", "cs_events_fts", r"content\s*=\s*'cs_events'"),
    ("trigger", "cs_events_immutable", r"RAISE\s*\(\s*ABORT"),
    ("trigger", "cs_events_fts_insert", r"INSERT\s+INTO\s+cs_events_fts"),
    ("trigger", "cs_events_fts_delete", r"INSERT\s+INTO\s+cs_events_fts"),
    ("trigger", "cs_events_fts_search_update", r"INSERT\s+INTO\s+cs_events_fts"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return set()


def _object_names(conn: sqlite3.Connection, kind: str) -> set:
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)).fetchall()}
    except sqlite3.Error:
        return set()


def _object_sql(conn: sqlite3.Connection, kind: str, name: str) -> Optional[str]:
    """取对象的原始 DDL 文本；对象不存在返回 None。

    R06 的支点：名字在 ≠ 东西是那么回事。同名空壳只能骗过“存在性核验”，
    骗不过“读它自己的 DDL”。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row[0] or "")


def _unique_column_sets(conn: sqlite3.Connection, table: str) -> set:
    """表上的唯一索引列集（frozenset 的集合）。用于核“唯一约束还在不在”。"""
    out = set()
    try:
        indexes = conn.execute("PRAGMA index_list(%s)" % table).fetchall()
    except sqlite3.Error:
        return out
    for row in indexes:
        # PRAGMA index_list 行格式：seq | name | unique | origin | partial
        if len(row) < 3 or not int(row[2]):
            continue
        name = str(row[1])
        try:
            cols = [str(r[2]) for r in conn.execute("PRAGMA index_info(%s)" % name).fetchall()]
        except sqlite3.Error:
            continue
        if cols:
            out.add(frozenset(cols))
    return out


def feature_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """feature 轴的**唯一**就绪判定。返回 ready/flag/reason/missing 明细。

    契约：
    入参：conn（任意连接；本函数只读）
    返回：{ready, flag, reason, missing_tables, missing_columns, missing_indexes,
           missing_definitions, missing_uniques}
    谁调用：conversation_capture 写入口（写前必问）、诊断/医生侧（可选用）
    改动影响：这是“能不能写”的闸门。放宽 = 往不存在的结构里写（R06 形态）；收紧 = 误停写。
    """
    tables = _object_names(conn, "table")
    indexes = _object_names(conn, "index")

    missing_tables: List[str] = []
    missing_columns: Dict[str, List[str]] = {}
    for table, columns in FEATURE_REQUIRED_TABLES.items():
        if table not in tables:
            missing_tables.append(table)
            continue
        actual = _table_columns(conn, table)
        missing = [c for c in columns if c not in actual]
        if missing:
            missing_columns[table] = missing
    missing_indexes = [n for n in FEATURE_REQUIRED_INDEXES if n not in indexes]

    # ── 定义核验（R06）：对象必须“是那么回事”，不只是“名字在” ──────────
    missing_definitions: List[str] = []
    for kind, name, pattern in FEATURE_REQUIRED_DEFINITIONS:
        sql = _object_sql(conn, kind, name)
        if sql is None:
            missing_definitions.append("%s:%s:absent" % (kind, name))
            continue
        if not re.search(pattern, sql, re.IGNORECASE):
            missing_definitions.append("%s:%s:definition_mismatch(%s)" % (kind, name, pattern))

    # ── 唯一约束核验（R06）：按列集核，防“靠吞重复来伪装幂等” ──────────
    missing_uniques: List[str] = []
    for table, columns in FEATURE_REQUIRED_UNIQUE_SETS:
        if table not in tables:
            continue  # 表都不在，已由 missing_tables 归因，不重复报
        if frozenset(columns) not in _unique_column_sets(conn, table):
            missing_uniques.append("%s:(%s)" % (table, ",".join(columns)))

    out: Dict[str, Any] = {
        "ready": False, "flag": None, "reason": "",
        "missing_tables": missing_tables, "missing_columns": missing_columns,
        "missing_indexes": missing_indexes, "missing_definitions": missing_definitions,
        "missing_uniques": missing_uniques,
    }

    if "xtmem_meta" not in tables:
        out["reason"] = "xtmem_meta_table_absent"
        return out
    try:
        row = conn.execute(
            "SELECT value FROM xtmem_meta WHERE key=?", (FEATURE_FLAG_KEY,)).fetchone()
    except sqlite3.Error as exc:
        out["reason"] = "feature_meta_unreadable:" + type(exc).__name__
        return out
    if row is None:
        out["reason"] = "feature_flag_absent"
        return out
    flag = str(row[0])
    out["flag"] = flag
    # ⚠️【不】strip()/lower() 去凑：契约值就是字面量 '1'，其余一律不认。
    if flag != FEATURE_FLAG_VALUE:
        out["reason"] = "feature_flag_not_enabled:%s" % flag
        return out

    incomplete = (missing_tables or missing_columns or missing_indexes
                  or missing_definitions or missing_uniques)
    if incomplete:
        parts = []
        if missing_tables:
            parts.append("tables=" + ",".join(missing_tables))
        if missing_columns:
            parts.append("columns=" + ";".join(
                "%s:%s" % (t, ",".join(c)) for t, c in missing_columns.items()))
        if missing_indexes:
            parts.append("indexes=" + ",".join(missing_indexes))
        if missing_definitions:
            parts.append("definitions=" + ",".join(missing_definitions))
        if missing_uniques:
            parts.append("uniques=" + ",".join(missing_uniques))
        out["reason"] = "feature_flag_on_but_structure_incomplete(" + "|".join(parts) + ")"
        return out

    out["ready"] = True
    return out


def require_feature_schema(conn: sqlite3.Connection) -> Dict[str, Any]:
    """就绪闸：不 ready 就抛 FeatureSchemaMismatch（可归因）。"""
    state = feature_state(conn)
    if not state["ready"]:
        raise FeatureSchemaMismatch(str(state["reason"]), state)
    return state


def ensure_feature_schema(conn: sqlite3.Connection, *, default_namespace_unused: Optional[str] = None) -> Dict[str, Any]:
    """幂等迁移：DDL + feature flag **同一个事务**；失败整体回滚，绝不留半建表。

    契约：
    入参：conn（可写连接；本函数自持事务，若调用方已在事务里就走 SAVEPOINT）
    返回：迁移后的 feature_state()
    副作用：建 additive 表/索引/触发器 + 写 xtmem_meta flag；**不动 user_version**
    谁调用：CST01 部署步骤（§14.2 第 4 步）；测试与本地初始化
    改动影响：DDL 与 flag 拆开即“谎报就绪”，见文件头铁律 1。
    """
    owns_transaction = not conn.in_transaction
    savepoint = "cst01_feature_schema"
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT " + savepoint)
    try:
        if "xtmem_meta" not in _object_names(conn, "table"):
            conn.execute("CREATE TABLE IF NOT EXISTS xtmem_meta (key TEXT PRIMARY KEY, value TEXT)")
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO xtmem_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (FEATURE_FLAG_KEY, FEATURE_FLAG_VALUE))
        state = feature_state(conn)
        if not state["ready"]:
            # DDL 全成功却仍不 ready = 参考 DDL 与必需清单漂移，必须整体回滚而不是留个半吊子。
            raise FeatureSchemaMismatch("migration_incomplete:" + str(state["reason"]), state)
        if owns_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT " + savepoint)
        return state
    except BaseException:
        try:
            if owns_transaction:
                if conn.in_transaction:
                    conn.rollback()
            elif conn.in_transaction:
                conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                conn.execute("RELEASE SAVEPOINT " + savepoint)
        except BaseException:
            # 事务状态未知时不许静默——调用方不能把半迁移连接当 ready（见错题本 #2 精神）。
            raise
        raise