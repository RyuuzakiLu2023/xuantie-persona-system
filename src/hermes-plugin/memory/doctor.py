# ================================================================
# O02 doctor · 总导游图（改代码前必读，读完再动手）
#
# ▍这是什么
# ① 干什么：只读诊断「这个角色连的端点到底对不对、库到底是谁、协议和
#    schema 就绪没有」，每项给 expected/observed/status/reason，
#    并按权限脱敏。逻辑契约对应 Spec 的 GET /v1/doctor。
# ② 技术栈：Python 3.11 标准库 + memory.endpoint_config + memory.open_db。
# ③ 依赖：endpoint_config（唯一解析口径）、open_db（只读连接）。
# ④ 给谁看：CLI `xtmem doctor`、运维排障、独立验收（O02-A09/A10）。
#
# ▍文件从上到下的结构
# 退出码与状态词表    EXIT_* / STATUS_*（判据直接读这些值，改名会破判据）
# 脱敏层              _redact() ★核心★ —— 普通/运维两种视图
# 单项检查器          _check_credential / _check_database / _check_live
# 主入口              run_doctor() ★核心★ —— 组装 + 判总状态 + 定退出码
#
# ▍修改铁律（都是血泪教训）
# 1. **只读铁律**：绝不用 CLI 的 _get_conn()——它会跑 _ensure_schema()，
#    那会在「只看一眼」时建表/迁移/写事件（O02-A09 明令禁止）。
#    必要 SQLite 检查一律 open_db(path, ro=True) 或 mode=ro URI。
# 2. **异常不许吞成成功**：诊断自身出错 → status=unknown + reason，
#    绝不落成 ok；但也不许把异常原文（可能含路径/凭据）带进普通视图。
# 3. **普通视图零路径**：db_path / pid_path / 凭据目录 / 原始异常文本
#    一律只在 operator 视图出现。加字段前先问「这会不会泄路径」。
# 4. **feature 缺失要归因**，不要笼统降级：核心 schema 与 feature_schema
#    是两条轴（见 open_db 的说明），doctor 也要分开报。
# 5. **不自动修复**：doctor 不建库、不迁移、不签发凭据、不起服务、
#    不杀进程。发现 stale PID 也只报告（Spec §4.2 明令）。
#
# ▍外号表
# "三态哈希" → code_hash 的 disk_candidate / process_startup / live_verified
# "两条轴"   → 核心 schema（PRAGMA user_version）与 feature_schema（xtmem_meta）
#
# ▍已知问题
# · doctor 不验证「进程名」，只验证「PID 活着 + 启动时刻对得上」；
#   按名匹配会被同名无关进程骗过（Spec §4.2：进程名不能代替应用层核验）。
# · 无 TCP 面的客户端（DSH mjs 走 spawn CLI）没有 hello 可谈，
#   其端点就绪只能由 CLI 侧 doctor 覆盖，见交付报告「客户端可用面」。
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-F03）：feature 子项改为直接消费 conversation_schema.
#   feature_state()（含 FTS 定义/触发器/唯一约束核验），不再自维护第二份部分规则；
#   依赖缺失（模块没装）单独归因 conversation_schema_unavailable，不混成“结构不齐”*
# *创建：2026-10-14 · O02 只读脱敏 doctor（端点/凭据/库/schema/feature/活体）*
# ================================================================
"""O02: read-only, permission-scoped diagnostics with attributable reasons.

Hard rule: a plain (non-admin) doctor response never contains a local absolute
path, another role's private database, a credential, or raw exception text.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, Optional

from memory import endpoint_config as ec

# ── 退出码：命令失败/身份拒绝/schema mismatch/unready/不支持/unknown 不报 healthy ──
EXIT_HEALTHY = 0
EXIT_FAILURE = 1
EXIT_UNREADY = 2
EXIT_AUTH_DENIED = 3
EXIT_SCHEMA_MISMATCH = 4
EXIT_UNKNOWN = 5

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_NOT_READY = "not_ready"
STATUS_MISMATCH = "mismatch"
STATUS_DENIED = "denied"
STATUS_UNKNOWN = "unknown"
STATUS_UNSUPPORTED = "unsupported"

OVERALL_HEALTHY = "healthy"
OVERALL_DEGRADED = "degraded"
OVERALL_UNREADY = "unready"
OVERALL_SCHEMA_MISMATCH = "schema_mismatch"
OVERALL_AUTH_DENIED = "auth_denied"
OVERALL_UNKNOWN = "unknown"

# 普通视图允许出现的字段（白名单式，新增字段必须显式登记，防手滑泄路径）
_PUBLIC_SAFE_KEYS = frozenset({
    "actor", "host", "port", "port_source", "db_id", "db_source",
    "protocol_version", "sampled_at", "pid", "pid_state", "process_start",
    "ready", "ready_reason", "code_hash", "descriptor_kind", "legacy_pid_kind",
})


def _item(expected, observed, status: str, reason: str = "") -> Dict[str, Any]:
    """统一的 expected/observed/status/reason 四件套。"""
    return {"expected": expected, "observed": observed, "status": status, "reason": reason}


# ── G7 递归脱敏：不可信字段一个都不许原样外传 ──────────────────
# 病灶（验收 R15 实测）：无关 TCP 服务在 `actor` 字段里塞本地绝对路径，
# doctor 虽报 mismatch，却把那个路径**原样**回显在
# `checks.live.observed.checks.actor.observed`；`_redact` 只作用于 resolved/endpoint
# 外层键，白名单也没净化嵌套 observed/reason。
# 修法：定义"什么能出网"的类型/范围白名单，**递归**走一遍整棵响应树。
_MAX_STRING = 200          # 单值长度上限（防超长正文/异常文本外泄）
_SAFE_PATH_FREE_KEYS = frozenset({
    "status", "reason", "verdict", "descriptor_kind", "legacy_pid_kind",
    "pid_state", "db_source", "port_source", "hermes_home_source",
})
# reason 是"我们自己的机器可读码"，但可能被拼进对端给的 error_code。
# 因此 reason 也要过一遍净化：只留安全字符集的短串。
_REASON_SAFE = __import__("re").compile(r"^[A-Za-z0-9_.:\-|(),=;]*$")


def _looks_like_path(value: str) -> bool:
    """判断一个字符串是否是本地绝对路径（Windows 盘符 / UNC / POSIX 根）。"""
    if len(value) < 3:
        return False
    if value[1:3] in (":\\", ":/"):
        return True
    if value.startswith("\\\\") or value.startswith("//"):
        return True
    if value.startswith("/") and "/" in value[1:]:
        return True
    return False


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    """递归净化：路径→指纹、超长截断、非法类型→类型名。

    ⚠️ 为什么必须递归且对**所有**嵌套格生效：对端是完全不可信的。
    我们只能保证"从我们这里出去的，没有本地路径、没有超长正文、没有任意对象"。
    """
    if depth > 8:
        return "<depth-limit>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if _looks_like_path(value):
            return "path#" + ec.db_id_for(value)[:16]
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + "<truncated>"
        return value
    if isinstance(value, dict):
        return {str(k)[:64]: _sanitize_value(v, depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v, depth + 1) for v in list(value)[:64]]
    # 任意对象（异常实例、自定义类）一律只留类型名——绝不 str() 它
    return "<" + type(value).__name__ + ">"


def _sanitize_reason(reason: Any) -> str:
    """reason 只允许机器可读码；任何"像路径/像正文"的内容一律替换。"""
    text = str(reason or "")
    if not text:
        return ""
    if _looks_like_path(text) or not _REASON_SAFE.match(text):
        # 保留可归因性但去掉不可信内容：原样丢弃 + 换成语义明确的标注
        return "rejected_untrusted_reason_text"
    return text[:_MAX_STRING]


def _redact_tree(node: Any, *, operator: bool, key: str = "") -> Any:
    """把一棵响应树净化成"可以出网"的形态。

    - 普通视图（operator=False）：路径→指纹、正文截断、reason 净化、未知对象只留类型名；
    - 运维视图（operator=True）：**保留本地路径**（管理员已获授权看路径），
      但仍限制长度/类型/reason 字符集——"不可信输入"这个性质不因授权而改变。

    ⚠️ 运维分支**必须继续递归**（operator=True 往下传），不能改用 _sanitize_value：
    后者会把路径一律换成指纹，于是"授权看路径"的管理员反而看不到 db_path
    （本函数第一版就踩了这个：A10 的自检当场抓到 admin_has_db_path=false）。
    授权改变的是"路径是否可读"，不是"还要不要递归"。
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            k = str(k)
            if k == "reason":
                out[k] = _sanitize_reason(v)
            elif operator:
                out[k] = _redact_tree(v, operator=True, key=k)
            else:
                out[k] = _redact_tree(v, operator=False, key=k)
        return out
    if isinstance(node, list):
        return [_redact_tree(v, operator=operator, key=key) for v in node]
    if isinstance(node, str):
        if operator:
            # 授权视图保留路径，但仍截断超长（防把对端的整段正文/异常文本带出去）
            return node[:_MAX_STRING] + ("<truncated>" if len(node) > _MAX_STRING else "")
        if _looks_like_path(node):
            return "path#" + ec.db_id_for(node)[:16]
        return node[:_MAX_STRING] + ("<truncated>" if len(node) > _MAX_STRING else "")
    if node is None or isinstance(node, (bool, int, float)):
        return node
    return "<" + type(node).__name__ + ">"


def _redact(value: Any, *, operator: bool) -> Any:
    """普通视图里把绝对路径换成稳定指纹（保留可对账性，去掉可读路径）。"""
    if operator or not isinstance(value, str) or not value:
        return value
    if len(value) >= 2 and value[1] == ":":
        return "sha256:" + ec.db_id_for(value)[:16]
    return value


# ================================================================
# 单项检查器
# ================================================================
def _check_credential(config: ec.EndpointConfig) -> Dict[str, Any]:
    """核验凭据**绑定**，绝不返回 token 本身。

    ⚠️ 只报「有没有 / 绑没绑对 / 协议版本对不对」，token 值永不出现在
    expected/observed/reason 任何一格——O02-A10 的硬要求。
    """
    path = Path(config.credential_dir) / (config.actor + ".json")
    expected = {"actor": config.actor, "protocol_version": config.protocol_version,
                "credential_present": True}
    if not config.actor:
        return _item(expected, {"credential_present": False}, STATUS_DENIED,
                     "actor_missing_or_invalid")
    if not path.exists():
        # 环境变量注入形态（S01 既有能力）也算凭据就位
        if os.environ.get("XTMEM_SERVE_TOKEN"):
            return _item(expected, {"credential_present": True, "source": "env"},
                         STATUS_OK, "credential_supplied_by_host_environment")
        return _item(expected, {"credential_present": False}, STATUS_NOT_READY,
                     "credential_file_absent")
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _item(expected, {"credential_present": True, "readable": False},
                     STATUS_MISMATCH, "credential_file_unreadable_or_invalid")
    if not isinstance(data, dict):
        return _item(expected, {"credential_present": True, "shape": "not_an_object"},
                     STATUS_MISMATCH, "credential_file_shape_invalid")
    bound_actor = data.get("actor")
    bound_protocol = data.get("protocol_version")
    token_ok = isinstance(data.get("token"), str) and len(data["token"]) >= 32
    observed = {"credential_present": True, "bound_actor_matches": bound_actor == config.actor,
                "bound_protocol_matches": bound_protocol == config.protocol_version,
                "token_shape_valid": token_ok}
    if bound_actor != config.actor:
        return _item(expected, observed, STATUS_MISMATCH,
                     "credential_bound_to_different_actor")
    if bound_protocol != config.protocol_version:
        return _item(expected, observed, STATUS_MISMATCH, "credential_protocol_version_mismatch")
    if not token_ok:
        return _item(expected, observed, STATUS_MISMATCH, "credential_token_shape_invalid")
    return _item(expected, observed, STATUS_OK, "")


def _ro_connect(db_path: str) -> sqlite3.Connection:
    """只读连接。**真正零副作用**——本函数是本单 R04 的唯一落点。

    ⚠️【不】用 open_db(ro=True) ！它在 ro 分支里仍调 `_check_version(conn, path,
    writable=False)`，而该函数在版本不匹配时会走 `_try_write_mismatch_event`——
    **另开一条可写连接写 xtmem_events(kind='schema_mismatch')**。验收实测：
    隔离库 user_version=99 跑一次 doctor，schema_mismatch 事件从 0 → 1，
    返回却仍声明 read_only=true（那就是"自称只读、实际写库"）。
    该 helper 是既有代码，但**不能拿"旧 helper 存在"给自己免责**：
    本单的 doctor 必须自己保证零副作用。故这里只用 mode=ro URI：
    只读连接下任何 INSERT/DDL 都会被 SQLite 直接拒（"attempt to write a readonly database"），
    连"想写"都不可能发生——比"约定不写"强一个量级。

    常规**写**连接的版本闸保护原样保留（open_db 一行未改），doctor 只是不走它。
    """
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# ================================================================
# G5 结构与 feature 实际核验 —— 版本号/feature flag 都不能代替结构存在
# ================================================================
# 核心必需结构：user_version=3 的**语义**是"这些列/索引都在"。
# 只报一个数字 3、却不看列在不在，等于用版本号冒充结构（R06 的病灶）。
CORE_REQUIRED_COLUMNS = {
    "chunks": ("id", "content", "namespace", "project", "visibility", "state",
               "tier", "timestamp", "updated_at", "source_ids", "derived_from"),
    "xtmem_meta": ("key", "value"),
}
CORE_REQUIRED_INDEXES = ("idx_chunks_ns", "idx_chunks_vis", "idx_chunks_tier")
# feature 轴（cross_session）的就绪判定**只有一个实现**：
#   memory.conversation_schema.feature_state()
#
# ★CST01-F03 对齐（2026-10-16，两步走完）：
#   ① R01 先把旧别名（conversation_events/… ）换成参考 DDL 的 cs_* 清单 ——
#      治的是“表名对不上”，同一座库一边可用一边不可用；
#   ② 但只 import 清单仍然**只是名字**：doctor 自己另算完整性，不核 FTS 定义、
#      触发器与唯一约束。独立验收实测：同一座库删掉 cs_events_fts，
#      feature_state 正确 not-ready，doctor 的 feature.enabled 却是 True。
#   ⇒ 修法：doctor **直接消费** feature_state() 的结论（含定义/约束核验），
#      不再另维护第二份部分规则。清单 import 只用于把“缺什么”原样透出。
# ⚠️【不】在这里再抄一遍表名列名或重写完整性规则：抄一份 = 又造一个漂移点（M03 的教训）。
# ⚠️【不】把“import 失败”当成“结构不齐”：那是旁路模块缺失，必须单独归因，
#    否则插件没装好会被读成“库坏了”，排障方向直接跑偏。
# ⚠️ personal_items / conversation_topics 属于 CST02/UX 的后续范围，**本单不建也不诊断**；
#    它们缺了不该让 cross_session（本单已实现的 capture/来源范围）报不可用。
try:
    from memory.conversation_schema import (
        FEATURE_REQUIRED_TABLES as _CS_TABLES,
        FEATURE_REQUIRED_INDEXES as _CS_INDEXES,
        FEATURE_FLAG_KEY as _CS_FLAG_KEY,
        feature_state as _cs_feature_state,
    )
    FEATURE_REQUIRED_TABLES = dict(_CS_TABLES)
    FEATURE_REQUIRED_INDEXES = tuple(_CS_INDEXES)
    FEATURE_FLAG_KEY = _CS_FLAG_KEY
except Exception:  # 插件未装/导入失败：退回空清单并在诊断里明确归因，不假装有结构
    FEATURE_REQUIRED_TABLES = {}
    FEATURE_REQUIRED_INDEXES = ()
    FEATURE_FLAG_KEY = "feature_schema.cross_session"
    _cs_feature_state = None


def _table_columns(conn, table: str) -> set:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return set()


def _index_names(conn) -> set:
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    except sqlite3.Error:
        return set()


def _check_structure(conn, tables: set) -> Dict[str, Any]:
    """核对核心必需列/索引；feature 只做**名字级预览**（判定权不在本函数）。

    返回 {"core": {...}, "feature": {...}}；每格都带 missing_* 明细，
    这样 reason 能直接说"缺哪一列"，而不是笼统一句"结构不符"。

    ⚠️★F03（2026-10-16）：`feature` 这一格**只回答“表/列/索引名字在不在”**，
       它**不是** feature 就绪判定。就绪（含 FTS 定义、不可变触发器、唯一约束）
       由 conversation_schema.feature_state() 给，doctor 直接消费——
       见 _check_database 里的 feature 块。
    ⚠️【不】把这里的 feature.complete 当成 enabled：名字对齐时它对 FTS 缺失
       一无所知，正是“capture 拒写、doctor 亮绿灯”那个假绿的来源。
    """
    indexes = _index_names(conn)
    core_missing_tables = [t for t in CORE_REQUIRED_COLUMNS if t not in tables]
    core_missing_columns, core_missing_indexes = {}, []
    for table, columns in CORE_REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        actual = _table_columns(conn, table)
        missing = [c for c in columns if c not in actual]
        if missing:
            core_missing_columns[table] = missing
    for name in CORE_REQUIRED_INDEXES:
        if name not in indexes:
            core_missing_indexes.append(name)

    feature_missing_tables = [t for t in FEATURE_REQUIRED_TABLES if t not in tables]
    feature_missing_columns, feature_missing_indexes = {}, []
    for table, columns in FEATURE_REQUIRED_TABLES.items():
        if table not in tables:
            continue
        actual = _table_columns(conn, table)
        missing = [c for c in columns if c not in actual]
        if missing:
            feature_missing_columns[table] = missing
    for name in FEATURE_REQUIRED_INDEXES:
        if name not in indexes:
            feature_missing_indexes.append(name)

    return {
        "core": {"missing_tables": core_missing_tables,
                 "missing_columns": core_missing_columns,
                 "missing_indexes": core_missing_indexes,
                 "complete": not (core_missing_tables or core_missing_columns
                                  or core_missing_indexes)},
        "feature": {"missing_tables": feature_missing_tables,
                    "missing_columns": feature_missing_columns,
                    "missing_indexes": feature_missing_indexes,
                    "complete": not (feature_missing_tables or feature_missing_columns
                                     or feature_missing_indexes)},
    }


def _check_database(config: ec.EndpointConfig) -> Dict[str, Any]:
    """库身份 + 核心 schema 轴 + feature 轴，一次**真只读**连接全查完。

    G5 修正（2026-10-14，R06 返修）：原实现只报 `user_version` 与
    `feature_schema.cross_session` 标记，于是"只有 chunks(id) + xtmem_meta 两张表、
    但 flag=1"的库被判 `ok / feature enabled=true`——**版本号与 flag 都不能代替
    结构存在**。现改为逐项核对必需表/列/索引（详见 _check_structure）。

    ★F03（2026-10-16）：feature 子项的 `enabled` 改为**直接消费**
    conversation_schema.feature_state() 的 ready —— 与 capture 写入口同一个判定。
    只共用表名清单不够：实测同一座库删掉 cs_events_fts，capture 拒写而 doctor
    仍报 feature enabled=true（因为 doctor 那份规则压根不看 FTS 定义）。
    """
    path = Path(config.db_path)
    expected = {"db_id": config.db_id, "core_schema_version": None,
                "core_structure_complete": True}
    if not path.exists():
        return _item(expected, {"exists": False}, STATUS_NOT_READY, "database_absent")
    try:
        expected_core = __import__("memory.open_db", fromlist=["EXPECTED_SCHEMA_VERSION"]).EXPECTED_SCHEMA_VERSION
    except Exception:
        expected_core = None
    expected["core_schema_version"] = expected_core

    conn = None
    try:
        conn = _ro_connect(config.db_path)
        observed_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        structure = _check_structure(conn, tables)
        observed: Dict[str, Any] = {
            "exists": True, "core_schema_version": observed_version,
            "chunks_table_present": "chunks" in tables,
            "tables_count": len(tables),
            "core_structure": structure["core"],
        }
        # feature 轴独立成格：核心版本对不代表 feature 就绪（两条轴，别混）。
        # ★F03：`enabled` **不再**由 doctor 自己拼规则，而是**直接消费**
        #    conversation_schema.feature_state() 的 ready —— 那是 capture 写入口
        #    同一个判定。理由：两处各维护一份“部分规则”必然漂移，实测过
        #    “capture 拒写、doctor 亮绿灯”（同一座库、同一条 FTS 缺失）。
        feature: Dict[str, Any] = {"enabled": False, "reason": "", "flag": None,
                                   "structure": structure["feature"]}
        if _cs_feature_state is None:
            # 旁路模块没装：这是**依赖缺失**，不是库结构问题。分开归因，别混。
            feature["reason"] = "conversation_schema_unavailable"
        else:
            try:
                # ⚠️ 只读调用：feature_state 全是 SELECT/PRAGMA，且本连接是 mode=ro，
                #    真写了也会被 SQLite 直接拒（见 _ro_connect 的说明）。
                fstate = _cs_feature_state(conn)
            except Exception as exc:  # noqa: BLE001 —— 诊断自身出错必须归 unknown 而不是假装 ok
                fstate = None
                feature["reason"] = "feature_state_unreadable:" + type(exc).__name__
            if fstate is not None:
                feature["flag"] = fstate.get("flag")
                feature["enabled"] = bool(fstate.get("ready"))
                # 结构明细原样透出（缺什么就说什么，不合并成一句“结构不符”）
                feature["missing_tables"] = list(fstate.get("missing_tables") or [])
                feature["missing_columns"] = dict(fstate.get("missing_columns") or {})
                feature["missing_indexes"] = list(fstate.get("missing_indexes") or [])
                feature["missing_definitions"] = list(fstate.get("missing_definitions") or [])
                feature["missing_uniques"] = list(fstate.get("missing_uniques") or [])
                if not feature["enabled"]:
                    # reason 直接用**同一个**判定给出的话术（含 FTS/触发器/唯一约束），
                    # 不要再翻译成人话——翻译一次就多一个漂移点。
                    feature["reason"] = str(fstate.get("reason") or "feature_not_ready")
        observed["feature_schema"] = feature

        if "chunks" not in tables:
            return _item(expected, observed, STATUS_NOT_READY, "core_table_chunks_absent")
        # 结构优先于版本号：版本对但列缺，仍然不是就绪
        if not structure["core"]["complete"]:
            parts = []
            if structure["core"]["missing_tables"]:
                parts.append("tables=" + ",".join(structure["core"]["missing_tables"]))
            if structure["core"]["missing_columns"]:
                parts.append("columns=" + ";".join(
                    "%s:%s" % (t, ",".join(c)) for t, c
                    in structure["core"]["missing_columns"].items()))
            if structure["core"]["missing_indexes"]:
                parts.append("indexes=" + ",".join(structure["core"]["missing_indexes"]))
            return _item(expected, observed, STATUS_NOT_READY,
                         "core_structure_incomplete(" + "|".join(parts) + ")")
        if expected_core is not None and observed_version != expected_core:
            # ⚠️ 只报告，绝不改版本、绝不迁移、绝不写 schema_mismatch 事件（R04）
            return _item(expected, observed, STATUS_MISMATCH,
                         "core_schema_version_mismatch_read_only")
        return _item(expected, observed, STATUS_OK, "")
    except Exception as exc:
        # 异常类型可以报，原始文本不报（可能含路径）
        return _item(expected, {"exists": True, "readable": False}, STATUS_UNKNOWN,
                     "database_open_failed:" + type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _check_live(config: ec.EndpointConfig, token: Optional[str],
                *, allow_probe: bool, timeout: float = 5.0) -> Dict[str, Any]:
    """活体核验：TCP 之上再验 actor/db_id/protocol/ready（委托 endpoint_config）。

    allow_probe=False 时不做任何网络动作，明确报 unsupported——
    「没验」和「验过是好的」必须能区分（这是最容易造假的一格）。

    ⚠️ G4 返修（R08）：observed 里必须**带上活体自述的 startup_cli_sha256**。
    原实现把 hello 的返回整个丢掉，于是 doctor 的 `process_startup` /
    `live_verified` 两格恒为 null、reason 还错写成"未探测"——哈希链在 doctor
    这一跳断掉（A12 的手工传参判据因此掩盖了真实链路）。本函数是那条链的唯一入口。
    同样把活体自述的 **pid/actor/db_id/protocol_version/ready** 原样带出，
    供 _check_process 做交叉核验（不是给普通视图看的——那些值会被递归脱敏）。
    """
    expected = {"actor": config.actor, "db_id": config.db_id,
                "protocol_version": config.protocol_version, "ready": True}
    if not allow_probe:
        return _item(expected, None, STATUS_UNSUPPORTED, "live_probe_disabled_by_invocation")
    result = ec.verify_remote_endpoint(config, token, timeout=timeout)
    observed: Dict[str, Any] = {
        "reachable": result["reachable"],
        "checks": result["checks"],
        "verdict": result["verdict"],
    }
    # hello 自述字段（能拿到才填；拿不到就留 None，绝不编）
    report = result.get("hello") or {}
    for key in ("startup_cli_sha256", "pid", "actor", "db_id",
                "protocol_version", "ready", "db_source", "port", "host"):
        if key in report:
            observed[key] = report[key]
    mapping = {
        ec.VERIFY_OK: (STATUS_OK, ""),
        ec.VERIFY_UNREACHABLE: (STATUS_NOT_READY, "transport_failure"),
        ec.VERIFY_REJECTED: (STATUS_DENIED, result["reason"]),
        ec.VERIFY_MISMATCH: (STATUS_MISMATCH, result["reason"]),
        ec.VERIFY_UNVERIFIED: (STATUS_UNKNOWN, result["reason"]),
    }
    status, reason = mapping.get(result["verdict"], (STATUS_UNKNOWN, "unmapped_verdict"))
    return _item(expected, observed, status, reason)


def _check_process(config: ec.EndpointConfig,
                   live_observed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """PID/描述/活体三重交叉核验（G4 返修，R07/R09/R10）。

    ⚠️ 原实现的病灶（验收实测）：只看"描述文件里有个活着且启动时刻对得上的 PID"
    就报 ok，于是——
      · 结构化文件**只有 `pid`**、缺 process_start/actor/db/protocol/ready ⇒ 报 ok
        （只证明存在某进程，不能证明它就是本端点）；
      · 描述被改成**另一个活进程**的 PID + actor=dsh + 协议 999 + 错 db/port +
        ready=false，而真实 serve 健康 ⇒ doctor 仍 healthy，且显示的 PID 与真正
        响应 hello 的 PID **不是同一个**。
    修法：把「描述事实」与「活体自述」逐格比对，任一格冲突即 mismatch/degraded；
    活体没探测（live_observed 为 None）时**不得**因为描述自洽就报 ok。
    """
    descriptor = ec.read_descriptor(config.descriptor_path)
    legacy = ec.read_descriptor(config.pid_path)
    primary = descriptor if descriptor["pid"] is not None else legacy
    state = ec.classify_pid_state(primary)
    expected = {"descriptor_kind": ec.DESC_STRUCTURED, "pid_state": ec.PID_ACTIVE,
                "descriptor_identity_complete": True, "matches_live": True}
    record = descriptor.get("record") or {}
    observed = {"descriptor_kind": descriptor["kind"], "legacy_pid_kind": legacy["kind"],
                "pid": state["pid"], "pid_state": state["state"],
                "reason": state["reason"],
                "recorded_keys": sorted(record.keys()) if record else []}

    # ① 描述身份完整性：只有 pid 不算身份（R07）
    #    ⚠️ G4 二次返修（验收 N07）：**host/port 也要算身份**。
    #    旧实现只核 actor/db_id/protocol/ready 四格，实测"只把描述里的 port
    #    改成别的值、其余全对"仍报 healthy——描述端口与实际监听端口不一致，
    #    客户端照着描述去连必然连到别处（或连不上），这必须是 mismatch。
    #    port 是端点身份的一部分，不是可有可无的展示字段。
    required = ("actor", "db_id", "protocol_version", "ready", "port")
    missing = [k for k in required if k not in record]
    observed["descriptor_missing_identity_fields"] = missing

    if state["state"] == ec.PID_REUSED:
        return _item(expected, observed, STATUS_MISMATCH, state["reason"])
    if state["state"] == ec.PID_STALE:
        return _item(expected, observed, STATUS_NOT_READY, state["reason"])
    if state["state"] == ec.PID_UNKNOWN:
        return _item(expected, observed, STATUS_UNKNOWN, state["reason"])

    # ② 旧整数形态：只证明一个 PID 值（无元数据可交叉核验）
    if descriptor["kind"] != ec.DESC_STRUCTURED:
        return _item(expected, observed, STATUS_DEGRADED,
                     "live_pid_without_structured_endpoint_metadata")

    if missing:
        return _item(expected, observed, STATUS_DEGRADED,
                     "structured_descriptor_missing_identity:" + ",".join(missing))

    # ③ 与活体自述交叉核验（R09）——这是"描述说的是不是真的"的唯一判据
    if live_observed is None:
        # 没探测就没资格说"描述与真实一致"；明确报未核验，不许报 ok
        return _item(expected, observed, STATUS_UNKNOWN,
                     "descriptor_self_consistent_but_live_not_probed")

    mismatches = []
    checks = {
        "pid": (record.get("pid"), live_observed.get("pid")),
        "actor": (record.get("actor"), live_observed.get("actor")),
        "db_id": (record.get("db_id"), live_observed.get("db_id")),
        "protocol_version": (record.get("protocol_version"), live_observed.get("protocol_version")),
        # ⚠️ G4 二次返修（N07）：port 必须参与交叉核验——"只改描述端口"是最容易被
        #    忽略的一类假描述：其余身份全对，客户端却会照着它连到错误的端口。
        "port": (record.get("port"), live_observed.get("port")),
        "host": (record.get("host"), live_observed.get("host")),
    }
    for name, (claimed, actual) in checks.items():
        observed["descriptor_" + name] = claimed
        observed["live_" + name] = actual
        if actual is None:
            # ⚠️ host 的特例：对端若没报 host（旧版 serve），那是"该格无法核验"，
            #    不是"描述错了"。报 unknown 由总状态兜底，不冤枉成 mismatch——
            #    否则任何未升级的对端都会被判假描述（那是误杀，不是安全）。
            if name == "host":
                observed["host_check"] = "live_did_not_report_host"
                return _item(expected, observed, STATUS_UNKNOWN,
                             "live_host_evidence_missing")
            mismatches.append(name + ":live_absent")
        elif claimed != actual:
            mismatches.append(name + ":descriptor_vs_live")
    # ready 必须描述与活体都为真
    observed["descriptor_ready"] = record.get("ready")
    observed["live_ready"] = live_observed.get("ready")
    if record.get("ready") is not True:
        mismatches.append("ready:descriptor_false")
    if live_observed.get("ready") is not True:
        mismatches.append("ready:live_false")

    if mismatches:
        return _item(expected, observed, STATUS_MISMATCH,
                     "descriptor_conflicts_with_live(" + ",".join(mismatches) + ")")
    return _item(expected, observed, STATUS_OK, "")


# ================================================================
# 主入口
# ================================================================
def run_doctor(
    actor: str,
    *,
    admin: bool = False,
    explicit_db: Optional[str] = None,
    hermes_home: Optional[str] = None,
    token: Optional[str] = None,
    allow_probe: bool = True,
    cli_path=None,
) -> Dict[str, Any]:
    """run_doctor()：契约
    入参：actor（可信身份）、admin（是否具备管理能力→决定视图与详情）
          explicit_db / hermes_home / token / allow_probe / cli_path
    返回：{"ok", "overall", "exit_code", "actor", "admin_view", "endpoint", "checks", "sampled_at"}
    副作用：**只读**。一次只读库连接 +（可选）一次只读 TCP 往返。
    谁调用：xtmem.cmd_doctor
    改动影响：checks 的键名与 status 词表被 O02 判据直接消费，改名要同步判据。

    ⚠️ 权限切分：admin=True 才返回 operator 视图（含本地绝对路径）。
    普通角色拿到的是脱敏摘要——**不是**"少报几项"，而是"报告同一批项、
    但路径等敏感值以指纹替代"，这样普通角色仍能自查端点是否被换。
    """
    config = ec.resolve_endpoint(actor, explicit_db=explicit_db, hermes_home=hermes_home)

    checks: Dict[str, Any] = {}
    if not config.actor:
        checks["identity"] = _item({"actor": "valid non-admin host actor"},
                                   {"actor": None}, STATUS_DENIED,
                                   "actor_missing_or_invalid_refused")
    else:
        checks["identity"] = _item({"actor": config.actor}, {"actor": config.actor},
                                   STATUS_OK, "")

    checks["endpoint"] = _item(
        {"host": config.host, "port": config.port, "db_id": config.db_id,
         "protocol_version": config.protocol_version},
        {"host": config.host, "port": config.port, "port_source": config.port_source,
         "db_id": config.db_id, "db_source": config.db_source,
         "protocol_version": config.protocol_version},
        STATUS_OK, "")

    if config.actor:
        checks["credential"] = _check_credential(config)
        checks["database"] = _check_database(config)
        # ⚠️ 顺序要紧：先探测活体，再把活体自述喂给进程检查做交叉核验（G4）。
        checks["live"] = _check_live(config, token, allow_probe=allow_probe)
        live_observed = checks["live"].get("observed")
        checks["process"] = _check_process(
            config, live_observed if isinstance(live_observed, dict) else None)
    else:
        for name in ("credential", "database", "process", "live"):
            checks[name] = _item(None, None, STATUS_DENIED, "identity_refused_before_check")

    # 总状态优先级。排序理由（每一条都对应一个必须不被更大的问题掩盖的判据）：
    #   ① 身份拒绝最优先：身份都没立住，后面的检查根本没跑，报别的会误导。
    #   ② schema 不符**高于**未就绪：版本不兼容是硬闸（open_db 会整条只读降级），
    #      且它可归因、可动作；若被"凭据文件还没生成"这类**暂时态**盖住，
    #      排障者会去补凭据而错过真正的墙（工单 §4.3 要求"可区分"）。
    #   ③ 其余 mismatch（如活体 actor/db 不符）归入 unready——它们同属
    #      "端点还没对上"，细节由 checks[] 的 reason 区分。
    #   ④ 降级/未知排在最后两档，都不得报 healthy。
    statuses = [c["status"] for c in checks.values()]
    schema_mismatch = any(
        c["status"] == STATUS_MISMATCH and "schema" in str(c.get("reason", ""))
        for c in checks.values())
    if STATUS_DENIED in statuses:
        overall, code = OVERALL_AUTH_DENIED, EXIT_AUTH_DENIED
    elif schema_mismatch:
        overall, code = OVERALL_SCHEMA_MISMATCH, EXIT_SCHEMA_MISMATCH
    elif STATUS_NOT_READY in statuses or STATUS_MISMATCH in statuses:
        overall, code = OVERALL_UNREADY, EXIT_UNREADY
    elif STATUS_DEGRADED in statuses:
        overall, code = OVERALL_DEGRADED, EXIT_UNREADY
    elif STATUS_UNKNOWN in statuses or STATUS_UNSUPPORTED in statuses:
        overall, code = OVERALL_UNKNOWN, EXIT_UNKNOWN
    else:
        overall, code = OVERALL_HEALTHY, EXIT_HEALTHY

    # 端点描述用三段式哈希，区分「磁盘候选」「进程启动记录」「活体已验」。
    # ⚠️ G4 返修（R08）：upstream 那条链必须真的接通——
    #    原始写法只把 live 哈希传给 describe，`process_startup` 恒为
    #    "record_unavailable"，而**活体自述里本来就有同一个值**。
    #    ⇒ 活体报回的 startup 哈希同时充当「进程启动记录」（它就是进程启动时记下的）
    #      和「活体已验」两格；只有当活体报告里确实没这个字段时才留 null+reason。
    live_check = checks.get("live", {})
    live_dict = live_check.get("observed") if isinstance(live_check.get("observed"), dict) else {}
    live_hash = live_dict.get("startup_cli_sha256")
    process_record = {"startup_cli_sha256": live_hash} if live_hash else None
    endpoint_desc = ec.describe(config, cli_path=cli_path, process_record=process_record,
                                live_report=live_hash,
                                ready=(overall == OVERALL_HEALTHY),
                                ready_reason="" if overall == OVERALL_HEALTHY else overall)

    view = config.as_operator() if admin else config.as_public()
    endpoint_public = {k: v for k, v in endpoint_desc.items() if k in _PUBLIC_SAFE_KEYS}
    if admin:
        endpoint_public.update({k: v for k, v in endpoint_desc.items()
                                if k not in _PUBLIC_SAFE_KEYS})

    response = {
        "ok": overall == OVERALL_HEALTHY,
        "overall": overall,
        "exit_code": code,
        "actor": config.actor,
        "admin_view": bool(admin),
        "endpoint": endpoint_public,
        "resolved": view,
        "checks": checks,
        "sampled_at": time.time(),
        "read_only": True,
    }
    # G7 返修：**整棵响应树**递归净化后再出网。
    # 为什么不是"只清 resolved/endpoint"：observed/reason 里装的是**对端的原话**，
    # 完全不可信（无关服务能往里塞任何东西）。白名单漏一格就是一条外泄通道。
    return _redact_tree(response, operator=bool(admin))
