# -*- coding: utf-8 -*-
"""xtmem.py — XTMEM 外部端 CLI（零依赖单文件，Python 3.11）。

规格书 §4.2 / 阶段 2.1：
- sys.path 指向引擎：优先 XTMEM_HERMES_ROOT（生产源码或插件 engine），
  默认插件内 plugins/xtmem/memory。
- 共享库默认 mimox-data/.hermes/memory/vector_store.db（用户拍板基准），
  可用 XTMEM_DB_PATH 覆盖（测试副本）。
- 单事务写入：ContextBuilder.store() 一次 INSERT 落正文+namespace/project/host_origin/visibility/track/updated_at（P0-4）。
- 命令：migrate / store / observe / recall / lookup / forget / absorb / export(→zcode 文件轨) / import(←zcode) / sync / agents / agents.md。
- v2.3：无 soul 命令（独立人格，零人格分发）。
- 2026-10-12 O01：诊断按关联 ID 记录关键阶段，stats/diagnostics 只读安全元数据。

用法示例：
    python xtmem.py migrate --namespace assistant
    python xtmem.py store --agent dsh --project X --text "..." --importance .8
    python xtmem.py recall --cross --query "部署偏好" --top 6
    python xtmem.py absorb --db "<ARCHIVE>/.hermes/memory/vector_store.db" --namespace hermes-webui
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import logging
import os
import sqlite3
import sys
import time
from contextvars import ContextVar

logger = logging.getLogger(__name__)
from pathlib import Path

# 统一输出编码：Windows 管道默认 GBK，桥接方（DSH/zcode）按 UTF-8 解码
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------------------------------------------------------------------------
# 引擎与库路径
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
# P1-H / R-12：引擎解析候选——
#   1) XTMEM_HERMES_ROOT 环境变量
#   2) 镜像布局 <repo>/src/hermes-plugin/memory（从 src/cli/xtmem.py 出发 = ../hermes-plugin/memory）
#   3) 运行时布局 <HERMES_HOME>/plugins/xtmem/memory（从 <HERMES_HOME>/xtmem/xtmem.py 出发 = ../plugins/xtmem/memory）
# 全找不到 → 打印明确指引并退出（不再抛 No module named 'memory'）
_engine_candidates = []
_env_root = os.environ.get("XTMEM_HERMES_ROOT", "").strip()
if _env_root:
    _engine_candidates.append(_env_root)
_engine_candidates.append(str(Path(HERE) / ".." / "hermes-plugin" / "memory"))
_engine_candidates.append(str(Path(HERE) / ".." / "plugins" / "xtmem" / "memory"))
DEFAULT_ENGINE = next((c for c in _engine_candidates if Path(c).exists()), "")
if not DEFAULT_ENGINE:
    # wheel 安装后引擎位于 site-packages，不能只沿源码目录猜路径。
    import importlib.util as _import_util
    _memory_spec = _import_util.find_spec("memory")
    if _memory_spec and _memory_spec.submodule_search_locations:
        DEFAULT_ENGINE = str(next(iter(_memory_spec.submodule_search_locations)))
if not DEFAULT_ENGINE:
    _msg = (
        "xtmem: 找不到 memory 引擎目录。已尝试:\n  "
        + "\n  ".join(_engine_candidates)
        + "\n请设 XTMEM_HERMES_ROOT 指向 src/hermes-plugin/memory（镜像）"
          "或 plugins/xtmem/memory（运行时），或从仓库正确位置运行。"
    )
    if __name__ == "__main__":
        print(_msg, file=sys.stderr)
        sys.exit(2)
    raise RuntimeError(_msg)
ENGINE_ROOT = DEFAULT_ENGINE
# 若指向插件内 memory/（名字=memory），父目录才是 import memory 的搜索根
_PARENT = str(Path(ENGINE_ROOT).resolve().parent)
if Path(ENGINE_ROOT).name == "memory":
    sys.path.insert(0, _PARENT)
else:
    sys.path.insert(0, ENGINE_ROOT)

from memory.diagnostics import diagnosed, emit

DEFAULT_CLI = os.path.abspath(__file__)
# ── G1 返修（2026-10-14，R01）：库路径接到**唯一解析口径** ──────────────
# 旧写法 `os.environ.get("XTMEM_DB_PATH", <硬编码>)` 只认 env + 常量，
# **压根不读 profile 的 config.yaml/xtmem.yaml** ⇒ 隔离 novel profile 时，
# 解析器能算对、而真实 CLI 仍连默认共享库（验收实测：CLI/live/doctor 全读 guard.db，
# 只有显式传 hermes_home 的解析器算对 novel.db）。那等于"解析器是个没人用的平行模块"。
# 现改为走 endpoint_config.resolve_db_path —— CLI/serve/doctor/引擎同一条链。
# ⚠️ 语义保持：XTMEM_DB_PATH 仍是最高优先的显式覆盖（env 层），
#    测试显式重定向照旧有效、且不写穿生产（解析结果只读，不建库不迁移）。
def _cli_resolve_db() -> str:
    """CLI 的库路径解析入口（唯一口径）。失败时回落旧常量语义，绝不抛。"""
    try:
        from memory import endpoint_config as _epc
        return _epc.resolve_db_path()["db_path"]
    except Exception:
        return os.environ.get("XTMEM_DB_PATH",
                              __import__("memory.public_runtime", fromlist=["db_default"]).db_default())


DEFAULT_DB = _cli_resolve_db()
DEFAULT_NAMESPACE = "default"

_builder = None
_serve_auth = ContextVar('xtmem_serve_auth', default=None)
# ⚠️ G4 二次返修（N08）：本次请求**实际挂上来的 builder**，供 hello 现算
#    "我到底连的哪个库 / 初始化完成没有"。用 ContextVar 而非模块全局——
#    一个进程可能有多个连接线程（S01 的 LoopbackServer 就是多线程），
#    模块全局会被并发请求互相覆盖，报出别人那条连接的库。
_serve_builder = ContextVar('xtmem_serve_builder', default=None)

# 命令自己已经把【人读文本】打到 stdout 了的旗语（当前只有 trace 默认模式用）。
# 为什么需要：本 CLI 的 __main__ 统一 print(json.dumps(main()))，而 trace 的产品形态是
# 时间线叙事——两者同时输出会让 stdout 出现"文本 + JSON"两段（管道/截屏都难看）。
# 命令置 True → __main__ 不再补打 JSON，stdout 保持单一形态。
_CLI_TEXT_EMITTED = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等迁移：统一走 memory.schema 模块（P0-4/P1-6：provider/CLI/引擎同一份）。"""
    # N3（2026-08-31）：全新空库直接 migrate 会 no such table: chunks——
    # chunks/chunks_fts 由引擎 VectorStore 建。缺表时先借引擎建基础表。
    try:
        has_chunks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
        if not has_chunks:
            from memory.vector_store import VectorStore
            _vs = VectorStore(db_path=DEFAULT_DB)
            _vs.close()
    except Exception as e:
        logger.warning("_ensure_schema: borrow engine to build base tables failed: %s", e)
    from memory.schema import ensure_xtmem_schema
    ensure_xtmem_schema(conn)
    conn.commit()



def _get_conn() -> sqlite3.Connection:
    # R5（P6 波1）：CLI 启动连接收编 open_db——schema 版本闸+降级自兜底
    from memory.open_db import open_db
    conn = open_db(DEFAULT_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn



def _resolve_actor(args) -> Optional[str]:
    """P0-1+R-03（2026-09-03）：身份固定。可信来源 = XTMEM_ACTOR（宿主注入）。

    --as 只在无环境身份时可用（本地显式上下文）；一旦 XTMEM_ACTOR 与 --as
    同时出现且不一致 → 抛错拒绝（宿主身份不可被调用方自报覆盖）。
    """
    trusted = _serve_auth.get()
    env_actor = trusted.actor if trusted is not None else os.environ.get("XTMEM_ACTOR", "").strip()
    as_agent = getattr(args, "as_agent", None)
    as_agent = str(as_agent).strip() if as_agent else None
    if env_actor and as_agent and as_agent != env_actor:
        raise RuntimeError(
            "--as %s 与 XTMEM_ACTOR=%s 冲突：宿主注入身份不可被自报 --as 覆盖"
            % (as_agent, env_actor)
        )
    if env_actor:
        if env_actor == '__admin__' and not _resolve_admin():
            raise RuntimeError('administrator viewer requires trusted administrator credentials')
        return env_actor
    if as_agent == '__admin__' and not _resolve_admin():
        raise RuntimeError('administrator viewer requires trusted administrator credentials')
    return as_agent


# R-03（2026-09-03）：admin 只认 1/true/yes/on；XTMEM_ADMIN=0/maybe 一律拒绝，
# 不再 bool() 把 "0" 当真。promotion/越权命令的管理员凭证优先读宿主注入的
# XTMEM_ADMIN_TOKEN（非空即管理员），XTMEM_ADMIN=1 仅限本地调试。
_ADMIN_TRUE = {"1", "true", "yes", "on"}


def _resolve_admin() -> bool:
    """严格解析管理员凭证。非法 XTMEM_ADMIN 值抛错（fail-fast，绝不 fallback）。"""
    trusted = _serve_auth.get()
    if trusted is not None:
        return trusted.admin
    token = os.environ.get("XTMEM_ADMIN_TOKEN", "").strip()
    if token:
        return True
    v = os.environ.get("XTMEM_ADMIN", "").strip().lower()
    if not v:
        return False
    if v not in _ADMIN_TRUE:
        raise RuntimeError(
            "XTMEM_ADMIN 仅接受 1/true/yes/on，非法值拒绝执行（当前=%r）" % v
        )
    return True


def _stats_enabled():
    """读 mimox-data/xtmem.yaml 的 stats.record_events（与 provider 同源）。"""
    try:
        import yaml
        path = os.environ.get("XTMEM_STATS_YAML", __import__("memory.public_runtime", fromlist=["home_file"]).home_file('xtmem.yaml'))
        raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
        return bool((raw.get("stats") or {}).get("record_events", False))
    except Exception:
        return False


def _record_cli_event(kind, agent, target_chunk="", meta=None):
    """CLI 活性事件：store/recall/lookup 均记 xtmem_events（stats.record_events 默认关）。"""
    if not _stats_enabled():
        return
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, agent or DEFAULT_NAMESPACE, agent or DEFAULT_NAMESPACE, "",
             target_chunk, json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass



def _record_cli_event_batch(kind, agent, ids, meta=None):
    """P4.0-d（2026-08-31）：一次 recall/lookup 记一行事件，ids 放 meta，避免每命中写一行导致事件表爆炸。"""
    if not _stats_enabled():
        return
    if not ids:
        return
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, agent or DEFAULT_NAMESPACE, agent or DEFAULT_NAMESPACE, "", "",
             json.dumps((meta or {}) | {"ids": list(ids), "count": len(ids)}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _builder_workspace() -> str:
    """N1（2026-08-31）：从 DB 路径反推 ContextBuilder 期望的 workspace。

    引擎 WAL 固定落在 <workspace>/.hermes/memory/wal/current.wal；
    期望 DB 布局为 <workspace>/.hermes/memory/vector_store.db（生产即如此），
    此时 workspace = db 的第三级父目录。非标准布局退回 db 所在目录，
    避免把 workspace 误传成 db 目录而写出 .hermes 双嵌套的孤儿 WAL。
    """
    db = Path(DEFAULT_DB).resolve()
    try:
        if db.parents[1].name == ".hermes" and db.parent.name == "memory":
            return str(db.parents[2])
    except IndexError:
        pass
    return str(db.parent)


def get_builder():
    global _builder
    if _builder is not None:
        return _builder
    if ENGINE_ROOT not in sys.path and Path(ENGINE_ROOT).name != "memory":
        sys.path.insert(0, ENGINE_ROOT)
    from memory.context_builder import ContextBuilder

    _builder = ContextBuilder(
        workspace_dir=_builder_workspace(),
        context_window=128_000,
        config={
            "db_path": DEFAULT_DB,
            "prefetch_only": True,
            "compression_enabled": False,
            "rest_api": False,
        },
    )
    return _builder



def cmd_migrate(args):
    conn = _get_conn()
    ns = args.namespace or DEFAULT_NAMESPACE
    conn.execute(
        "UPDATE chunks SET namespace=? WHERE namespace IS NULL OR namespace=''",
        (ns,),
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    return {"ok": True, "chunks": n, "namespace_default": ns}


def _parse_store_json_field(raw, flag):
    """入参：CLI JSON字符串与参数名；返回：解析对象或None；副作用：无；谁调用：cmd_store；改动影响：坏结构载荷明确拒绝，不漏出解析异常。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must contain valid JSON" % flag) from exc


def cmd_store(args):
    text = (args.text or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    builder = get_builder()
    ns = args.namespace or args.agent or DEFAULT_NAMESPACE
    vis = getattr(args, "visibility", "shared") or "shared"
    # P1-E / R-09：未知/空 visibility 落库即拒（fail-closed），合法值为 shared|private|shared-with:...
    from memory.visibility import normalize_visibility
    nv = normalize_visibility(vis)
    if nv is None:
        return {"ok": False, "error": "invalid --visibility: shared | private | shared-with:<list>"}
    vis = nv
    # N2（2026-08-31）：有身份时禁止写入他人域（XTMEM_ACTOR/--as = 本端身份）
    actor = _resolve_actor(args)
    # Knowledge 豁免（2026-09-06，Knowledge 升级方案 §3.1——全方案唯一代码侵入点）：
    #   ns=="knowledge" 且 visibility=="shared" 时放行任意 actor 写入共享知识域；
    #   真实 actor 溯源记 _record_cli_event meta.real_actor（下方）。豁免只开这一个组合，
    #   knowledge+private 与其他 namespace 的身份闸一字不动（私有域闸不松一寸）。
    _kx_exempt = (ns == "knowledge" and vis == "shared")
    if actor and ns != actor and not _kx_exempt:
        return {"ok": False, "error": f"本端记忆只能写入本域 {actor}（自己记自己的）"}
    if not actor and args.agent and ns != args.agent and not _kx_exempt:
        return {"ok": False, "error": "无身份时 namespace 必须与 agent 一致；正式操作请设 XTMEM_ACTOR"}
    # AF-1（2026-08-31，P4 第 1 波）：CLI store 血缘三参数——蒸馏器/画像聚合器的派生记忆
    # 必须能传进 derived_from/source_ids/tier，否则 P0.1 血缘闸会拒写。
    derived_from = (getattr(args, "derived_from", "") or "").strip()
    raw_src = (getattr(args, "source_ids", "") or "").strip()
    source_ids = []
    if raw_src:
        raw_src = raw_src.rstrip(",")
        if raw_src.startswith("["):
            try:
                parsed = json.loads(raw_src)
                source_ids = [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                return {"ok": False, "error": "--source-ids 不是合法 JSON 数组"}
        else:
            source_ids = [x.strip() for x in raw_src.split(",") if x.strip()]
    if derived_from and not source_ids:
        return {"ok": False, "error": "derived_from 非空时 --source-ids 必须非空（血缘闸 P0.1）"}
    tier = (getattr(args, "tier", "refined") or "refined").strip() or "refined"
    try:
        # P0-2/P0-3/P0-4：安全校验由引擎统一做；正文+metadata 单事务落库。
        # 拒绝时 builder.store 抛 ValueError → 明确返回失败，不再谎报成功。
        write_result = builder.store_observation(
            text,
            metadata={
                "importance": max(0.0, min(1.0, float(getattr(args, "importance", 0.5)))),
                "tags": (args.tags.split(",") if args.tags else ["xtmem", ns]),
                "session_id": args.agent or "cli",
                "type": "conversation",
                "namespace": ns,
                "project": args.project or "",
                "host_origin": f"cli:{args.agent or 'unknown'}",
                "visibility": vis,
                "derived_from": derived_from,
                "source_ids": source_ids,
                "tier": tier,
                "track": getattr(args, "track", "") or "",
                "operation_id": getattr(args, "operation_id", None),
                "observation_id": getattr(args, "observation_id", None),
                "speaker": getattr(args, "speaker", None),
                "subject": getattr(args, "subject", None),
                "source_refs": _parse_store_json_field(getattr(args, "source_refs_json", ""), "--source-refs-json"),
                "scope": _parse_store_json_field(getattr(args, "scope_json", ""), "--scope-json"),
                "valid_from": getattr(args, "valid_from", None),
                "valid_until": getattr(args, "valid_until", None),
                "confirmation_type": getattr(args, "confirmation_type", None),
            },
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "reason": "rejected"}
    if not write_result.get("ok"):
        return {"ok": False, "action": write_result.get("action"),
                "error_code": write_result.get("error_code"),
                "error": write_result.get("error", "memory write was rejected")}
    mid = write_result["memory_id"]
    # real_actor 溯源：Knowledge 豁免通道下 ns != actor（跨域写），事件必须能回答"谁写的"
    _record_cli_event("write", ns, target_chunk=mid, meta={"agent": args.agent or "dsh", "real_actor": actor or args.agent or "cli"})
    return {"ok": True, "action": write_result["action"], "id": mid,
            "memory_id": mid, "observation_id": write_result["observation_id"],
            "namespace": ns, "visibility": vis}



def cmd_observe(args):
    # 尾批一 · 件2（2026-09-08）：observe 兼事件记账口——hook 侧 assemble_bundle 后
    # 调 CLI observe --kind bundle_injected --meta-json {...} 直落事件（hook 对生产库
    # 只读铁律不破：observe 是 CLI 侧已在的唯一写口，事件落 xtmem_events 零 memory 写入）。
    # --kind 缺省=旧行为（原文直存 store）；寒暄轮 hook 侧不调本命令（没注入没账）。
    _kind = str(getattr(args, "kind", "") or "").strip()
    _meta_raw = str(getattr(args, "meta_json", "") or "").strip()
    if _kind:
        try:
            _meta = json.loads(_meta_raw) if _meta_raw else {}
        except Exception:
            return {"ok": False, "error": "bad --meta-json"}
        _record_cli_event(_kind, getattr(args, "agent", None) or "cli", meta=_meta)
        return {"ok": True, "kind": _kind, "meta": _meta}
    text = (args.text or "").strip()
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        return {"ok": False, "error": "empty input"}
    # 简化：原文直存（LLM 提取留给 memory_bridge/长驻链路）
    return cmd_store(argparse.Namespace(
        text=text, agent=args.agent, project=args.project, importance=args.importance,
        namespace=args.namespace, tags=args.tags,
    ))


def _visibility_ok(entry: dict, actor: str) -> bool:
    """E1 记忆可见性判定：shared 全可见；private 仅本 namespace；
    shared-with:a,b 名单内或本 namespace 可见。

    ⚠️★M03（2026-10-13）更正：**无 actor 不是"上帝视角"**。
      本函数一路走到 memory.visibility.visibility_ok(vis, ns, "")，
      空 viewer 的语义是 **fail-closed：只放行 shared**，private/shared-with/未知一律 deny。
      旧 docstring 写「无 actor（CLI 上帝视角）恒 True」——那是**错的**，
      上帝视角必须显式传 '__admin__'（由 _resolve_admin 的凭证闸把关，见上）。
      这句错注释直接养出了调用方的 `if _actor and not _visibility_ok(...)` 写法：
      actor 为空时整段跳过 ⇒ 无身份反而比有身份看得更多（I12 变体，实机复现）。

    ★★F2-1（2026-10-14 第二轮返修）：**升级为统一闸**——改调
      memory.visibility.entry_visibility_ok，从而**同时消费权限隔离态**。
      病灶（独立验收实测）：收紧来源后派生被标 state='archived' + 退役原因带
      'm03-acl-unexpressible:'，但 visibility 仍是 shared ⇒ 本函数只看 visibility
      ⇒ `cmd_lookup(id=隔离行)` 在 zcode 普通身份下 count=1 且返回**完整正文**。
      隔离的语义是"该给谁读算不出来" ⇒ 除管理员外一律不可读。
      ⚠️ 判据是**退役原因前缀**而不是 state=='archived'（见 visibility.is_quarantined）：
        把正常知识退役也拦掉属过度收紧（历史行仍应按 visibility 可读）。

    R1.3（2026-08-31）：薄封装——真正判定收敛到 memory.visibility.visibility_ok。
    """
    from memory.visibility import entry_visibility_ok
    # ★F2-1：entry 里若带 retire_reason（lookup 的 SELECT 已带），统一闸会一并判定隔离态；
    #   没带的调用方（只传 visibility/namespace 的旧调用点）行为与旧版完全一致。
    return entry_visibility_ok(entry, actor or "")


def cmd_recall(args):
    query = (args.query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    builder = get_builder()
    # P0.3（2026-08-31）：检索按 viewer（本端域）过滤可见性；actor 从 --as/XTMEM_ACTOR 取
    # P1.1（2026-08-31）：per-viewer utility 加权
    _actor = _resolve_actor(args)
    # B2（2026-09-07，小D）：Bundle Selector 规则版——路由分类 + 判空前置 + route 透传引擎。
    #   判空：纯寒暄零召回（负样本 N1-N4/N6-N8 治本位；N5 边界观察不计分不拦）。
    #   namespace_hint：非 --cross 才传目标域（跨域题 gold 在异域，域内加权会反杀）。
    #   尾批一（2026-09-08）：本段曾因主树/运行位覆盖事故丢失，按 B5 侦察笔记原文复原。
    _b2_route = ""
    _b2_hint = ""
    try:
        from memory.bundle_selector import classify_query_route, is_chitchat
        if is_chitchat(query):
            _ba = _actor or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
            try:
                # R1 数据线（合并单 B-1）：带上本路径已知 verdict（与下面返回体同串，防口径漂移）。
                # 注：本路径 ids 恒为空，_record_cli_event_batch 对空 ids 直接 return（不落库），
                # 此处 meta 属"口径对齐"性质，实际不影响 R1 计数。
                _record_cli_event_batch("recall", _ba, [],
                                        meta={"verdict": "B2 判空：寒暄/语气 query 零召回（路由前置）",
                                              "mode": "chitchat"})
            except Exception:
                pass
            return {"ok": True, "count": 0, "items": [], "route": "chitchat",
                    "verdict": "B2 判空：寒暄/语气 query 零召回（路由前置）",
                    "basis": "bundle_selector.is_chitchat",
                    "modifiers": {"mode": getattr(args, "mode", None), "rerank": None,
                                  "time_decay": None, "role_valve": None}}
        _b2_route = classify_query_route(query)
    except Exception:
        _b2_route = ""
    if not getattr(args, "cross", False):
        _b2_hint = (getattr(args, "namespace", None) or _actor or "") or ""
    # P5.0-e（2026-09-02，v1.13 波3）：修饰器 DSL——全部默认 None/现状，与 --cross/--namespace/--as/--top 正交
    _mode = getattr(args, "mode", None)
    _rerank_w = getattr(args, "rerank", None)
    _tdecay = getattr(args, "time_decay", None)
    _role_valve = getattr(args, "role_valve", None)
    if _mode == "exact":
        # 精确栈：SQL LIKE 精确检索（翻笔记本），importance 排序——与 hybrid 免疫（要精确时不走"KNN 相似"）
        _conn = _get_conn()
        _where = "state='active' AND (content LIKE ? OR content LIKE ?)"
        _params = [f"%{query}%", f"%{query.lower()}%"]
        if not getattr(args, "cross", False):
            # P1-F / R-10：非 cross 检索——有 actor 用 actor 域，无 actor 须显式 --namespace；
            # 二者皆无 → 拒绝（不得回退 DEFAULT_NAMESPACE 全库扫）
            _auth_ns = args.namespace or _actor
            if not _auth_ns:
                return {"ok": False, "error": "非 cross 检索需 --namespace 或 --as/XTMEM_ACTOR（P1-F）"}
            _where += " AND namespace=?"
            _params.append(_auth_ns)
        if getattr(args, "project", ""):
            _where += " AND project=?"
            _params.append(args.project)
        _sql = (f"SELECT id, content, namespace, project, visibility, tier, importance "
                f"FROM chunks WHERE {_where} ORDER BY importance DESC, timestamp DESC LIMIT ?")
        _params.append(int(args.top or 6))
        _rows = _conn.execute(_sql, _params).fetchall()
        _conn.close()
        items = []
        for r in _rows:
            # ★M03（2026-10-13）读入口 fail-closed 收口（原写法见下方"M03 修的是什么"）
            # M03 修的是什么：旧写法 `if _actor and not _visibility_ok(...)`。
            #   _actor 为空（无 --as / 无 XTMEM_ACTOR）时该条件**恒假** ⇒ 可见性闸整段被跳过，
            #   配合 --cross（或 --namespace）就能把他人 private 正文原样返回。
            #   实测（<TEMP>\DSH\m03-probe3.py）：无身份 `recall <他人private正文片段> --cross`
            #   **返回正文**；而 `--as dsh` 反而被拦——"没身份"比"有身份"看得多。
            #   根因是 _visibility_ok 的旧 docstring 写着「无 actor（CLI 上帝视角）恒 True」，
            #   但那句是**错的**（见 _visibility_ok 的更正说明）：
            #   空 viewer 在 memory.visibility.visibility_ok 里是 fail-closed，只放行 shared。
            #   修法：**去掉 `_actor and`**，让空身份也走判定 ⇒ 只可能看到 shared。
            # ⚠️【不】把 `_actor and` 加回来（那是把 I12 变体请回来）。
            # ⚠️ namespace 空值一律用 "unknown" 哨兵，【不】回落 DEFAULT_NAMESPACE：
            #   回落会把"无处归属"的行送给默认域的人读（provider 侧 P1-F/R-10 已收口，
            #   这里是 CLI 侧对齐——"所有读入口同权限"）。
            if not _visibility_ok({"namespace": r["namespace"] or "unknown",
                                   "visibility": r["visibility"] or "shared"}, _actor):
                continue
            if _role_valve is not None and float(r["importance"] or 0) < _role_valve:
                continue
            items.append({"id": r["id"], "content": str(r["content"]),
                          "score": round(float(r["importance"] or 0.5), 4),
                          "namespace": r["namespace"] or "", "project": r["project"] or "",
                          "visibility": r["visibility"] or "shared", "tier": r["tier"] or "raw",
                          "importance": float(r["importance"] or 0)})
        if getattr(args, "entity", None):
            try:
                _ec = _get_conn()
                # 发现30（2026-09-02）：--entity 检索同样排除派生页（食尾蛇：不要把 wiki/timeline 页当普通记忆过滤出来）
                _ent_ids = {r[0] for r in _ec.execute(
                    "SELECT ce.chunk_id FROM chunk_entities ce JOIN chunks ch ON ch.id = ce.chunk_id "
                    "WHERE ce.canon=? AND COALESCE(ch.derived_from, '') = ''", (args.entity,))}
                _ec.close()
                items = [it for it in items if it["id"] in _ent_ids]
            except Exception:
                pass
        _batch_agent = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
        # R1 数据线（合并单 B-1）：exact 栈固定口径 verdict（与下方返回体同串）。
        _record_cli_event_batch("recall", _batch_agent, [it["id"] for it in items],
                                meta={"verdict": "精确栈（LIKE 精确检索）", "mode": "exact"})
        try:
            for it in items:
                # A16（2026-09-16 Z指导裁定）：state="recalled" = 进了【候选池】。
                # ⚠️ 这里【不】是 exposed——CLI 返回的是候选（dsh 取 top12/zcode 取 topN），
                #    记成 exposed 会虚高、假绿。exposed 由真正拼装注入文本的那层记
                #    （dsh 侧=xtmem-tools 注入层扫最终行；zcode 侧=emit_context 之后）。
                builder._vector_store.increment_utility(it["id"], viewer=_batch_agent, state="recalled")
        except Exception:
            pass
        return {"ok": True, "count": len(items), "items": items, "mode": "exact",
                "verdict": "精确栈（LIKE 精确检索）", "basis": "SQL LIKE + importance 排序，未走 hybrid",
                "modifiers": {"mode": _mode, "rerank": None, "time_decay": None, "role_valve": _role_valve}}
    _top = int(args.top or 6)
    # P4.8 T4（2026-09-01）：两阶段检索路由——refined 首跳高分即返，miss 回落 raw 兜底（天然不相交去重）
    _t_tt = time.perf_counter()
    # ══════════════════════════════════════════════════════════════════
    # R01·RQ06（2026-10-21 小D）：域范围在【候选限额之前】生效
    #
    # 病灶（核查结论.md §一.5，实测）：
    #   旧代码不传 namespace_filter ⇒ 检索先按全库取 top_k，之后才在 L598-605 按
    #   namespace/project 等值过滤。外域可读候选先把 top_k 槽位占满，
    #   域内正确答案被挤掉，筛完只剩空槽。
    #   ⚠️ 关键边界（报告原话）：域过滤前移【只能】保证范围与名额正确，
    #     它【不能】冒充相关性修复——隔离对照里前移后负例反而从 1 条变 4 条。
    #      所以本改动与 RQ02 的证据准入必须同时在场，缺一不可。
    #
    # 修法：非 --cross 时把授权域作为 namespace_filter 传进两跳检索（引擎侧
    #   已有该通道，见 hybrid_retriever.search 参数），让"选候选"和"限额"同域。
    #   --cross 是显式跨域请求，原授权语义保持不变（不传 filter）。
    #   knowledge 域：M03 之后共享知识仍按既有可见性规则处理，这里【不】偷偷扩域——
    #   扩域属于权限变更，不在本单范围（§9.7.3 明确）。
    # ══════════════════════════════════════════════════════════════════
    # ★R01-FIX1（2026-10-21，独立验收 F2-B）：**project 也要在限额前生效**。
    #   上一版只前传了 namespace，project 仍在主路（限额之后）才筛，
    #   于是 --project right 时 wrong 项目已占掉名额；更糟的是补召 SQL 根本没带
    #   project（独立验收实测返回体 project=wrong、b2_like=true、admission=backfill:content-term）。
    #   修法：
    #     ① namespace_filter 之外，把 project 也交给引擎，让"选候选"时就已经同域同项目
    #        （引擎侧在 sort/MMR/top_k 之前就有 project 过滤位）；
    #     ② 补召 SQL 追加 project=? —— 与主路同范围；
    #     ③ 后面的 Python 侧筛保留：它是**双保险**，不是主判据。
    #   ⚠️【不】靠"把 LIMIT 调大"糊过去：名额被占与被占多少无关，范围必须在限额前成立。
    #   ⚠️ --cross 显式跨域按原契约：不传范围过滤（授权就是授权）。
    _ns_filter = None
    _project_filter = ""
    if not getattr(args, "cross", False):
        _scope_ns = args.namespace or _actor
        if _scope_ns:
            _ns_filter = (_scope_ns,)
        _project_filter = getattr(args, "project", "") or ""
    _search_kw = {}
    if _ns_filter:
        _search_kw["namespace_filter"] = _ns_filter
    if _project_filter:
        _search_kw["project_filter"] = _project_filter
    results = builder._retriever.search_two_tier(
        query, top_k=int(args.top or 6), viewer=_actor,
        time_decay=False if _tdecay == "off" else None,
        utility_lookup=lambda cid: (builder._vector_store.get_utility(cid, viewer=_actor or "") or {}).get("utility_score", 0.0),
        # R01·RQ05 + R01-FIX1（F3）：新鲜度只认**真实有用时间**（cited/confirmed 才写入）。
        #   上一版在 useful 为空时回落 last_access ——那正好把"展示续命"放回来：
        #   旧账被 exposed 刷新过的 last_access 会冒充"最近有用"。
        #   现在：没有 last_access_useful 就返回 None（= 未知），由上层按"无新鲜度"
        #   处理；【不】拿展示时刻补造。
        #   ⚠️ 单次查询只取一次账（上一版写了两遍 get_utility，热路径双查库）。
        last_access_lookup=lambda cid: (builder._vector_store.get_utility(cid, viewer=_actor or "") or {}).get("last_access_useful"),
        route=_b2_route or None, namespace_hint=_b2_hint or None,
        **_search_kw,
    )
    _perf_mark("serve", "recall_two_tier", _t_tt)
    _t_nsmap = time.perf_counter()
    conn = _get_conn()
    ns_map = {
        r["id"]: (r["namespace"] or "unknown", r["project"] or "", r["visibility"] or "", r["tier"] or "raw")
        for r in conn.execute("SELECT id, namespace, project, visibility, tier FROM chunks WHERE state='active'")
    }
    conn.close()
    _perf_mark("serve", "recall_ns_map_scan", _t_nsmap)
    items = []
    for doc, score in results:
        if score < 0.01:
            continue
        entry = ns_map.get(doc.id)
        actor = _resolve_actor(args)
        # P5.0-e 修饰器：--role-valve X = 命中块 importance ≥ X 才返回（默认 None 不筛）
        if _role_valve is not None and float(getattr(doc, "importance", 0.5) or 0.5) < _role_valve:
            continue
        # ★M03：去掉 `actor and`——空身份也要过可见性闸（见上面 exact 栈同款说明）
        # ⚠️ entry 为 None（id 不在库/内存索引比库新）⇒ 无法回查真值 ⇒ 不放行（fail-closed）。
        if entry is None:
            continue
        if not _visibility_ok({"namespace": entry[0] or "unknown",
                               "visibility": entry[2] if entry else "shared"}, actor):
            continue
        if args.cross:
            ns_ok = project_ok = True
        else:
            _auth_ns = args.namespace or actor
            ns_ok = (_auth_ns is not None) and (entry is not None) and (entry[0] == _auth_ns)
            project_ok = (not args.project) or (entry is not None and entry[1] == args.project)
        if not (ns_ok and project_ok):
            continue
        _adm_note = (getattr(builder._retriever, "_last_admission_notes", None) or {}).get(str(doc.id))
        items.append({
            "id": doc.id,
            "content": str(doc.content),
            "score": round(float(score), 4),
            "namespace": entry[0] if entry else "",
            "project": entry[1] if entry else "",
            "visibility": entry[2] if entry else "shared",
            "tier": entry[3] if entry else "raw",  # P4.4 ⑱：层级透明（refined/raw）
            "importance": float(doc.importance),
            # R01-FIX1（F4）：主路条目也带准入凭据——出口据此区分
            #   「新规则验过」与「旧来源没检查」。没有凭据的旧候选不得当已核。
            "admission": "main:evidence",
            "admission_basis": (_adm_note or {}).get("basis") or "",
        })
    # P4.9-波1 H（2026-09-02，v1.12.1）：--entity 只做 where 过滤器（不进排序权重，防 hub）
    if getattr(args, "entity", None):
        try:
            _ec = _get_conn()
            _ent_ids = {r[0] for r in _ec.execute("SELECT chunk_id FROM chunk_entities WHERE canon=?", (args.entity,))}
            _ec.close()
            items = [it for it in items if it["id"] in _ent_ids]
        except Exception:
            pass
    # B2 锚点补召（2026-09-07，小D）：只对稀有 token 做 LIKE 补召——检索双腿（BM25+向量）
    # 漏掉的字面锚点（agents.md/354,469/专名）走 SQL LIKE 直捞，visibility 同阀。
    # 尾批一（2026-09-08）：本段曾因主树/运行位覆盖事故丢失，按 B2 验收版原文复原；
    # 补召 append 带 role_valve 同阀（#31/dsl 漏网根因：主循环 468 阀拦不住补召路）。
    try:
        from memory.bundle_selector import extract_anchors, rerank_with_anchors
        # R01-FIX1（F4）：统一准入核心——主路/补召/关联扩展/最终出口共用同一份判据
        from memory.evidence_admission import assess as _assess_evidence
        _vec_adm_min = float(getattr(builder._retriever, "vector_admission_min", 0.5))
        _anchors = extract_anchors(query)
        # R01·RQ04（2026-10-21）：补召被拒条数——失败要有声音，别静默吞（错题本常客）
        _recall_rejected = 0
        if _anchors:
            _lc = _get_conn()
            _seen = {it["id"] for it in items}
            _w = ("state='active' AND (" + " OR ".join(["content LIKE ?"] * len(_anchors)) + ")"
                  " AND content NOT LIKE '# 实体 ·%' "
                  "AND content NOT LIKE '# 时间骨架%'"
                  # P1-10 同闸·补召版（2026-09-13 检索面二合一件②）：补位候选与主通道
                  #   同守 lineage-stale fail-closed（hybrid search 的 799 同款闸）——
                  #   带病条摘标前不得经补召外溢（466c 发现项 2，mem-10c009c4 实锤）。
                  #   COALESCE 防 tags 为 NULL 的行被 NOT LIKE 整行误杀。
                  #   2026-09-13 收紧（stale判据收紧单）：'%"lineage-stale:%' 引号+冒号
                  #   精确匹配 JSON 元素开头（tags 为 json.dumps 文本，元素内引号必转义，
                  #   裸引号只会出现在元素边界）；裸词技术标签不算病条，防闭环自复制。
                  " AND COALESCE(tags,'') NOT LIKE '%\"lineage-stale:%'")
            _p = [f"%{_a}%" for _a in _anchors]
            if not getattr(args, "cross", False):
                _auth_ns2 = args.namespace or _actor
                if _auth_ns2:
                    _w += " AND namespace=?"
                    _p.append(_auth_ns2)
                # ★R01-FIX1（2026-10-21，独立验收 F2-B）：补召 SQL 原来只加 namespace，
                #   **没有 project** —— 于是 `--project right` 时补召仍会把 wrong 项目的
                #   记录捞回来（实测 mem-60000001，project=wrong、b2_like=true、
                #   admission=backfill:content-term）。主路筛掉的，侧门又端回来。
                #   修法：补召与主路同范围——project 一并进 WHERE。
                #   ⚠️【不】只在 Python 侧再筛一次就算修好：那样名额与 LIMIT 48 已经被
                #      错项目的记录占掉了（少捞几条正确的），范围要在 SQL 里就成立。
                _auth_proj2 = getattr(args, "project", "") or ""
                if _auth_proj2:
                    _w += " AND project=?"
                    _p.append(_auth_proj2)
            _p.append(48)
            for _r in _lc.execute(
                f"SELECT id, content, namespace, project, visibility, tier, importance "
                f"FROM chunks WHERE {_w} ORDER BY timestamp DESC, importance DESC LIMIT ?",
                _p,
            ).fetchall():
                if _r["id"] in _seen:
                    continue
                # ★M03：去掉 `_actor and`（B2 锚点补召路径与主通道同闸；空身份 fail-closed）
                if not _visibility_ok(
                    {"namespace": _r["namespace"] or "unknown",
                     "visibility": _r["visibility"] or "shared"}, _actor):
                    continue
                if _role_valve is not None and float(_r["importance"] or 0) < _role_valve:
                    continue
                # ══════════════════════════════════════════════════════════
                # R01·RQ04（2026-10-21 小D）：补召必须过【同一套证据准入】
                #
                # 病灶（核查结论.md §一.4，生产只读副本实测）：
                #   补召按 content LIKE 直接捞，捞到就 append 并【固定赋分 0.35】，
                #   之后 rerank_with_anchors 按 0.35 把它们与主路候选混排——
                #   没有重新过主路的任何一道筛选。
                #   问「书房 C03 数字 NULL 裁决进展」时补召回 4 条本域旧记录，
                #   4 条都【不含 C03】，却都含 null（agent 失败返回null / IS NULL / R3=null）；
                #   仅停掉补召就让最终 4 条变 0 条 ⇒ 侧门实锤。
                #   另一实测：mem-4b6dec18 在十条 case 里从未进过 hops.selected，
                #   只以 score=0.35/b2_like=true 出现在 rerank 之后 —— 同一形态。
                #
                # 修法：补召候选进门之前，先回答"它凭什么是这条问题的证据"：
                #   ① 必须命中查询的【内容词】（>=1 个多字词/标识符）；
                #   ② 且命中的不能【只有】本次锚点词（例如只含 "NULL"）——
                #      NULL 是通用词，不能单独替代主题（RQ04 原话）。
                #   ③ 赋分不再凭空 0.35：按命中数分档 0.25/0.30/0.35，上限仍 0.35，
                #      但先经过①②，不再"先 append 就排到正例前面"
                #      （实测 case0 顺序是 [0.35,0.35,0.35,0.6125,...]，真阳性被埋）。
                #   ⚠️【不】直接删掉补召：它治的是"字面锚点检索双腿都漏"的真问题
                #     （agents.md / 版本号 / 千分位数字），删了就退回漏查。
                #   ⚠️ 判据必须复用 memory.hybrid_retriever 的同一份实现，
                #     不许在 CLI 里另抄一套阈值 —— §9.7.3「端侧只薄适配」。
                # ══════════════════════════════════════════════════════════
                # ★R01-FIX1（2026-10-21，独立验收 F4）：补召**不再自己写一套判据**。
                #   上一版在这里另做「非锚内容词命中 + 0.25~0.35 分档」，与主路
                #   hybrid_retriever 的规则不是同一份 —— 正门与侧门各盖各的章。
                #   现在统一问 memory.evidence_admission.assess()：
                #     · 补召走的是字面 LIKE 命中，等价「词法有据」，故传 raw_bm=1.0；
                #     · 严格标识符硬约束同样生效（问 ZXQ731920 不会捞回 ABC998877）；
                #     · 通用词（null/项目/部署/进展）在抽词阶段就被剔除，不可能单独当证据。
                #   ⚠️【不】在这里再写一份阈值：§9.7.3 明确端侧只薄适配/检查结果。
                _content = str(_r["content"] or "")
                _bk_verdict = _assess_evidence(
                    query, _content, raw_bm=1.0, raw_vec=0.0, ttl_decay=1.0,
                    importance=float(_r["importance"] or 0.0),
                    relevance_threshold=0.15,
                    vector_admission_min=_vec_adm_min,
                )
                if not _bk_verdict["admitted"]:
                    _recall_rejected += 1
                    logger.debug("R01 补召拒绝：%s id=%s", _bk_verdict["reason"], _r["id"])
                    continue
                # 赋分仍按证据强度分档，但**上限低于主路**，且必须先过准入
                _hit_n = len(_bk_verdict.get("term_hits") or []) + len(_bk_verdict.get("id_hits") or [])
                _bk_score = min(0.35, 0.20 + 0.05 * max(1, _hit_n))
                items.append({
                    "id": _r["id"], "content": _content,
                    "score": round(_bk_score, 4),
                    "namespace": _r["namespace"] or "",
                    "project": _r["project"] or "",
                    "visibility": _r["visibility"] or "shared",
                    "tier": _r["tier"] or "raw",
                    "importance": float(_r["importance"] or 0),
                    "b2_like": True,
                    "admission": "backfill:content-term",
                    # R01-FIX1（F4）：「可追溯」——返回体要说明这条凭什么进门
                    "admission_basis": _bk_verdict.get("basis") or "",
                })
                _seen.add(_r["id"])
            _lc.close()
            _ul = lambda cid: (builder._vector_store.get_utility(cid, viewer=_actor or "") or {}).get("utility_score", 0.0)
            items = rerank_with_anchors(items, _anchors, _top, utility_lookup=_ul, query=query)
        else:
            items = items[:_top]
    except Exception:
        items = items[:_top]
    # P5.0-e 修饰器：associated = 涟漪联想（associations 一跳扩展，吃实体表/图）
    if _mode == "associated" and items:
        try:
            _ac = _get_conn()
            _ids = [it["id"] for it in items]
            _ph = ",".join("?" * len(_ids))
            _canons = {r[0] for r in _ac.execute(
                f"SELECT canon FROM chunk_entities WHERE chunk_id IN ({_ph})", _ids)}
            _extra = []
            if _canons:
                _ph2 = ",".join("?" * len(_canons))
                _xrows = _ac.execute(
                    f"""SELECT ch.id, ch.content, ch.namespace, ch.project, ch.visibility, ch.tier, ch.importance
                        FROM chunk_entities ce JOIN chunks ch ON ch.id = ce.chunk_id
                        WHERE ce.canon IN ({_ph2}) AND ch.state='active' AND COALESCE(ch.derived_from, '') = ''""",
                    list(_canons)).fetchall()
                _seen = {it["id"] for it in items}
                for r in _xrows:
                    if r["id"] in _seen:
                        continue
                    # ★M03：去掉 `_actor and`（associated 涟漪扩展与主通道同闸）
                    if not _visibility_ok({"namespace": r["namespace"] or "unknown",
                                           "visibility": r["visibility"] or "shared"}, _actor):
                        continue
                    if _role_valve is not None and float(r["importance"] or 0) < _role_valve:
                        continue
                    # 尾批二单E（2026-09-13）：本段此前只有 visibility + role_valve 两道闸，缺主循环
                    #   那道 ns 等值闸——盲标实测 associated 注入行 54.07%（438/810）是跨域污染：
                    #   viewer=zcode/dsh 却拉进 ns=assistant 的内容（22 条 distinct 撑起 438 行），
                    #   全为 visibility=shared 所以 visibility 闸拦不住。此处镜像主循环闸（上面
                    #   `ns_ok = project_ok = True` 那段）。--cross 显式跨域请求语义保留，不拦。
                    if not getattr(args, "cross", False):
                        _auth_ns = getattr(args, "namespace", None) or _actor
                        if _auth_ns is not None and (r["namespace"] or DEFAULT_NAMESPACE) != _auth_ns:
                            continue
                        if getattr(args, "project", "") and (r["project"] or "") != args.project:
                            continue
                    _extra.append({"id": r["id"], "content": str(r["content"]),
                                   "score": round(0.5 * max([it["score"] for it in items], default=0.5), 4),
                                   "namespace": r["namespace"] or "", "project": r["project"] or "",
                                   "visibility": r["visibility"] or "shared", "tier": r["tier"] or "raw",
                                   "importance": float(r["importance"] or 0), "associated": True})
                    _seen.add(r["id"])
            _ac.close()
            items += _extra[:int(args.top or 6)]
        except Exception:
            pass
    # P5.0-e 修饰器：--rerank w = RRF 融合传播分与 BM25（0=纯 BM25，1=纯传播；默认 None 不启用）
    _reranked = False
    if _rerank_w is not None and items:
        try:
            _w = max(0.0, min(1.0, float(_rerank_w)))
            from memory.graph import doc_boosts
            _bg = doc_boosts(query, [it["id"] for it in items]) or {}
            _bgmax = max(_bg.values(), default=0.0)
            if _bgmax > 0:
                _bm = {}
                try:
                    _raw = builder._retriever.bm25.search(query, top_k=len(items) * 3)
                    _bm_raw = {}
                    for _i, _s in _raw:
                        if _i < len(builder._retriever.docs):
                            _bm_raw[builder._retriever.docs[_i].id] = _s
                    _bmmax = max(_bm_raw.values(), default=0.0) or 1.0
                    _bm = {k: v / _bmmax for k, v in _bm_raw.items()}
                except Exception:
                    _bm = {}
                def _blend(it):
                    _b = _bm.get(it["id"], it["score"])
                    _g = _bg.get(it["id"], 0.0) / _bgmax
                    return (1.0 - _w) * _b + _w * _g
                items = sorted(items, key=_blend, reverse=True)
                for it in items:
                    it["score"] = round(_blend(it), 4)
                _reranked = True
        except Exception:
            _reranked = False
    _batch_agent = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
    # P4.4 ⑱：诚实输出——告诉调用方这次结果怎么来的（模式/置信/依据），防谄媚。
    # R1 数据线（2026-09-10 合并单 B-1）：_mode/_verdict 计算【必须】提到下面记事件之前——
    # 原来是"先记事件(631)后算 verdict(640)"，导致 CLI recall 路径（zcode/dsh 走的）写进
    # xtmem_events.meta 的事件【没有 verdict 字段】→ R1 第二判据（有害注入率）在样本域零数据、
    # 出不了结论。纯计算顺序问题，不新增 import。
    # ══════════════════════════════════════════════════════════════
    # R01·RQ07（2026-10-21 小D）：mode/verdict/basis 必须描述【最终来源】
    #
    # 病灶（核查结论.md §一.4 末，实测）：
    #   _mode 读的是 builder._retriever._last_two_tier_mode —— 那是【补召之前】两跳检索的模式。
    #   补召段随后往 items 里 append 了 4 条 0.35 的货，mode 却还是 "refined-first"，
    #   于是返回体对着一盘补召货贴"高置信（refined 首跳命中）"。
    #   "正门查验盖了章、侧门换了一盘菜、餐牌没换"——这是 RQ07 的原话。
    #
    # 修法：先看【最终 items 的构成】再决定标签：
    #   · 全部来自补召（都有 b2_like）      → mode=backfill-only，低置信
    #   · 主路与补召混排                    → mode=<主路模式>+backfill，中置信
    #   · 全是主路                          → 沿用主路模式（与改前一致）
    #   并且 basis 必须点名"最终是谁进的包"。
    #   ⚠️【不】把 refined-first 一律降级：主路真的命中时它就是高置信，
    #     冤枉正常路径同样是"标签不反映事实"。
    # ══════════════════════════════════════════════════════════════
    _tier_mode = getattr(builder._retriever, "_last_two_tier_mode", None) or "unknown"
    _n_total = len(items)
    _n_backfill = sum(1 for it in items if it.get("b2_like"))
    if _n_total and _n_backfill == _n_total:
        _mode = "backfill-only"
    elif _n_backfill:
        _mode = "%s+backfill" % _tier_mode
    else:
        _mode = _tier_mode
    _max_score = max([it["score"] for it in items], default=0.0)
    try:
        # R01（2026-10-21 小D）：原来的写法是 sys.path.insert(0, r"<XTMEM_HOME>/plugins/xtmem")
        #   ——【硬编码生产路径】。后果不只是难看：
        #   ① 在隔离树里跑本 CLI，verdict_of 仍从【生产】provider 导入 ⇒
        #      改隔离树的 provider 永远测不出效果（本端实测踩到：backfill 新态
        #      在隔离树里跑出 "未知"，因为它问的是生产的那份 verdict_of）。
        #   ② 换机/换盘即崩，且崩在 except 里静默降级——静默降级正是错题本常客。
        #   改成与文件头同一套 ENGINE_ROOT 推导：provider.py 就住在引擎包的上一级。
        _prov_root = str(Path(ENGINE_ROOT).resolve().parent)
        if _prov_root not in sys.path:
            sys.path.insert(0, _prov_root)
        from provider import verdict_of as _verdict_of
    except Exception:
        _verdict_of = None
    _verdict = (_verdict_of(_mode, bool(items)) if _verdict_of
                else ("高置信（refined 首跳命中）" if _mode == "refined-first" else ("兜底（raw 回落）" if _mode == "raw-fallback" else ("空结果" if not items else "未知"))))
    # R01·RQ07：basis 必须点名"最终是谁进的包"，而不是只描述检索阶段。
    if _mode == "backfill-only":
        _basis = "主检索无合格证据，最终条目全部来自关键词补召（已过内容词准入）"
    elif _mode.endswith("+backfill"):
        _basis = ("%s 为主，另有 %d 条来自关键词补召（同准入）"
                  % ("query + 画像/要点（refined 池）匹配" if _mode.startswith("refined-first")
                     else "refined 无高分，回落 raw 流水兜底", _n_backfill))
    else:
        _basis = ("query + 画像/要点（refined 池）匹配" if _mode == "refined-first" else "refined 无高分，回落 raw 流水兜底")
    # P4.0-d（2026-08-31）：recall 改批量一行事件（ids 放 meta），不再每命中写一行。
    # 与 provider 侧同口径（verdict_of 是共享函数），R1 第二判据据此统计「兜底（raw 回落）」。
    _record_cli_event_batch("recall", _batch_agent, [it["id"] for it in items],
                            meta={"verdict": _verdict, "mode": _mode})
    for it in items:
        # P4.0-b（2026-08-31，P4 第 2 波）：CLI recall 回写 utility——打通 zcode/DSH 端 per-viewer 数据。
        # （已存在同段代码的 utility_lookup 是读取侧，这里补写入侧，P1.1/P1.2 才对这端生效。）
        # A16（2026-09-16）：补 state="recalled"（候选池口径，非 exposed——理由见精确栈段注释）
        try:
            builder._vector_store.increment_utility(it["id"], viewer=_batch_agent, state="recalled")
        except Exception:
            pass
    return {"ok": True, "count": len(items), "items": items, "mode": _mode, "verdict": _verdict, "basis": _basis,
            "max_score": round(_max_score, 4), "route": _b2_route or "",
            "modifiers": {"mode": _mode, "rerank": _rerank_w, "reranked": _reranked,
                          "time_decay": _tdecay, "role_valve": _role_valve}}


def cmd_bulletin(args):
    """Knowledge 升级方案阶段二-②（2026-09-06）：能力与公告——共享底座能力，四端注入同口径。

    knowledge 域 tags 含「能力总账/更新通告」的 active 最新 1 条（updated_at 优先），
    截 1000 字符。只读零写；dsh 端 xtmem-tools 公告段 spawn 本命令（zcode hook
    knowledge_bulletin_lines 与玄铁 provider._knowledge_block 三处同口径，改口径三处同步）。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, content, namespace, visibility, updated_at, timestamp FROM chunks "
            "WHERE namespace='knowledge' AND state='active' "
            "AND (tags LIKE '%能力总账%' OR tags LIKE '%更新通告%') "
            "ORDER BY updated_at DESC, timestamp DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    viewer = '__admin__' if _resolve_admin() else (_resolve_actor(args) or '')
    if not row or not _visibility_ok(dict(row), viewer) or not str(row["content"] or "").strip():
        return {"ok": True, "count": 0, "bulletin": ""}
    return {"ok": True, "count": 1, "id": row["id"], "bulletin": str(row["content"]).strip()[:1000]}


def cmd_lookup(args):
    # ID 是定位信息，不授予读取权限；拒绝时不暴露正文或进行记账。
    actor = _resolve_actor(args)
    viewer = '__admin__' if _resolve_admin() else (actor or '')
    if getattr(args, "id", None):
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, content, namespace, project, host_origin, track, importance, timestamp, visibility, state, superseded_by, source_ids, derived_from, tier, content_time, updated_at, content_hash, tags, source, token_count, retire_reason, retired_by, retired_at FROM chunks WHERE id = ?",
            (args.id,)
        ).fetchone()
        conn.close()
        if not row or not _visibility_ok(dict(row), viewer):
            return {"ok": True, "count": 0, "items": []}
        # ── A20 靶点2（2026-09-16，小D 承接 / Z指导 合并单）· --id 分支补记账 ──────────
        # 病灶：本分支原来的【两个 return 都在记账段之前】⇒ 按 id 精确回查整段零记账——
        #   连 access_count 都不涨，于是"谁在按 id 取这条"永远是黑的。
        # 受影响面（Z指导 独立核实）：zcode MCP 的 xtmem_lookup_id（xtmem_mcp.py 传 --id）。
        #   ⚠️ dsh 的 xtmem_lookup_cross 【不】走这里（它传 --cross 无 --id，走下面有记账
        #      的那条）——那条没错，别去动它（A16 回执在这点上说错了，本单已更正）。
        # 口径：state="recalled"（候选池）。lookup 返回的是【候选】不是注入面 ⇒ 【不】记 exposed：
        #   记了就是把候选冒充"真看见"，尺子虚高、假绿（A16 裁定口径，与下方 query 路径同态）。
        # 仅已授权返回计入 recalled；被拒读取不记访问或候选事件。
        _id_agent = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
        _record_cli_event_batch("recall", _id_agent, [row["id"]],
                                meta={"verdict": "精确回查（lookup by id）", "mode": "lookup"})
        try:
            _id_builder = get_builder()
            _id_builder._vector_store.increment_utility(row["id"], viewer=_id_agent, state="recalled")
        except Exception:
            pass
        return {
            "ok": True, "count": 1,
            "items": [{
                "id": row["id"], "content": str(row["content"]),
                "namespace": row["namespace"] or "unknown",
                "project": row["project"] or "", "host_origin": row["host_origin"] or "",
                "track": row["track"] or "default", "importance": row["importance"] or 0,
                "timestamp": row["timestamp"] or 0, "visibility": row["visibility"] or "",
                "state": row["state"] or "active", "superseded_by": row["superseded_by"] or "",
                "source_ids": row["source_ids"] or "", "derived_from": row["derived_from"] or "",
                "tier": row["tier"] or "refined", "content_time": row["content_time"] or 0,
                "updated_at": row["updated_at"] or 0, "content_hash": row["content_hash"] or "",
                "tags": row["tags"] or "[]", "source": row["source"] or "", "token_count": row["token_count"] or 0,
            }]
        }

    query = (args.query or "").strip()
    conn = _get_conn()
    where = "state='active'"
    params = []
    if query:
        where += " AND (content LIKE ? OR content LIKE ?)"
        params = [f"%{query}%", f"%{query.lower()}%"]
    # R2.4（2026-08-31）：lookup 默认只回本域；--cross 才全库（与 recall 对齐）
    if not getattr(args, "cross", False):
        # P1-F / R-10：非 cross 检索——有 actor/--namespace 才限定域，否则拒绝（不回退 DEFAULT_NAMESPACE）
        _auth_ns = args.namespace or _resolve_actor(args)
        if not _auth_ns:
            conn.close()
            return {"ok": False, "error": "非 cross 检索需 --namespace 或 --as/XTMEM_ACTOR（P1-F）"}
        where += " AND namespace = ?"
        params.append(_auth_ns)
    # ★F2-1：SELECT 一并带出 retire_reason——_visibility_ok 现在消费隔离态，
    #   不带它就判不出"这行被权限隔离了"（本条 where 默认 state='active' 已排除 archived，
    #   但把判据依赖写在**判定函数**里、而不是靠上层恰好过滤，才不会被人改上层时静默破防）。
    sql = (f"SELECT id, content, namespace, project, host_origin, track, importance, timestamp, visibility, state, retire_reason "
           f"FROM chunks WHERE {where} ORDER BY importance DESC, timestamp DESC LIMIT ?")
    params.append(int(args.top or 50))
    rows = conn.execute(sql, params).fetchall()
    rows = [r for r in rows if _visibility_ok(dict(r), viewer)]
    conn.close()
    # P4.0-b（2026-08-31，P4 第 2 波）：CLI lookup 回写 utility，同 recall（get_builder 仅取 vector_store）。
    # P4.0-d（2026-08-31）：lookup 改批量一行事件。
    try:
        _builder = get_builder()
    except Exception:
        _builder = None
    _batch_agent = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
    # R1 数据线（合并单 B-1）：lookup 是「按 id 精确回查」，不走 hybrid 两段式阈值，
    # 因此没有 refined-first/raw-fallback 之分——填 lookup 专属口径，避免混入 R1 的
    # "兜底（raw 回落）" 计数（那会把 lookup 误算成有害注入）。依据：本路径上方是
    # WHERE id IN (...) 精确查询 + visibility 过滤，无 tier 回落判定。
    _record_cli_event_batch("recall", _batch_agent, [r["id"] for r in rows],
                            meta={"verdict": "精确回查（lookup by id）", "mode": "lookup"})
    for r in rows:
        if _builder is not None:
            try:
                # A16（2026-09-16 Z指导 裁定"lookup 也记 recalled"）：lookup 是按 id 精确回查，
                # 返回的【同样是候选不是注入面】——记 exposed 一样会虚高。故与 recall 两段同态。
                _builder._vector_store.increment_utility(r["id"], viewer=_batch_agent, state="recalled")
            except Exception:
                pass
    return {
        "ok": True,
        "count": len(rows),
        "items": [{
            "id": r["id"], "content": str(r["content"]),
            "namespace": r["namespace"] or "unknown",
            "project": r["project"] or "", "host_origin": r["host_origin"] or "",
            "track": r["track"] or "default", "importance": r["importance"] or 0,
            "timestamp": r["timestamp"] or 0, "visibility": r["visibility"] or "",
        } for r in rows],
    }


def _wal_path():
    """引擎真实 WAL 路径（<ws>/.hermes/memory/wal/current.wal），与 _purge 共用一处推导。"""
    return os.path.join(_builder_workspace(), ".hermes", "memory", "wal", "current.wal")


def _purge_wal_entries(ids, bak):
    """R-04（2026-09-03）：强锁整文件重写删除一批 entry。

    先把 current.wal 快照备份到 bak（拒绝路径零副作用；副作用路径失败可恢复），
    再走 _locked_wal(required=True) 原子重写——绝不再 lock-free 降级。
    成功返回 True（bak 保留，DB 提交成功后由调用方删除；DB 失败时由调用方恢复）。
    """
    wal = _wal_path()
    if not os.path.exists(wal):
        return True
    try:
        import shutil
        shutil.copy2(wal, bak)
    except Exception:
        logger.warning("_purge_wal_entries: backup %s failed", bak)
        return False
    try:
        from memory.wal_engine import _locked_wal
        with _locked_wal(os.path.dirname(wal), required=True, blocking=True):
            lines = open(wal, encoding="utf-8").read().splitlines()
            id_set = set(ids)
            kept = []
            for l in lines:
                ls = l.strip()
                if not ls:
                    continue
                try:
                    if json.loads(ls).get("id") in id_set:
                        continue
                except Exception:
                    pass  # 损坏行保留（不是本次目标，留给读侧告警）
                kept.append(l)
            tmp = wal + ".r04.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(kept))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, wal)
        return True
    except Exception as e:
        logger.warning("_purge_wal_entries failed, restoring backup: %r", e)
        try:
            import shutil
            if os.path.exists(bak):
                shutil.copy2(bak, wal)
        except Exception:
            pass
        return False


def _drop_wal_backup(bak):
    try:
        if os.path.exists(bak):
            os.remove(bak)
    except Exception:
        logger.warning("_drop_wal_backup: remove %s failed", bak)


def _restore_wal_backup(bak):
    """DB 侧失败时把 WAL 回滚到备份（副作用补偿）。"""
    try:
        import shutil
        if os.path.exists(bak):
            shutil.copy2(bak, _wal_path())
            os.remove(bak)
    except Exception as e:
        logger.warning("_restore_wal_backup failed: %r", e)


# ---------------------------------------------------------------------------
# M01 · R6（2026-10-13 · D 路）：forget 的 durable 存根写链与失败补偿
# ---------------------------------------------------------------------------
# 【为什么单开一段】旧 cmd_forget 是「先 purge WAL 再改 DB」，全程没有任何东西阻止
#   WAL 重放把刚"忘掉"的条目又写回库里——验收 v2 的 R6 反例正是这么复现的。
#   修法 = 把副作用顺序焊死成「①存根先落 → ②WAL purge → ③DB 事务」：
#   存根是最先落盘的，后面任何一步炸了，最坏也只是"这条还没删干净"，
#   绝不会是"已经忘掉的条目又活了"（遗忘方向必须 fail-safe）。
# 【改了会怎样】这段只负责存根的写与撤回；DB 事务、WAL purge 的既有语义一个字没动。
# ---------------------------------------------------------------------------

# 兜底用的存根文件名：优先从引擎 import（单一来源），这个常量只在引擎 import 失败时顶上。
_TOMBSTONE_FILE_NAME_FALLBACK = "tombstones.jsonl"


def _tombstone_file_path():
    """存根文件路径 = <ws>/.hermes/memory/wal/tombstones.jsonl（与引擎同一处推导）。

    【为什么不直接问 WALEngine 要路径】拒绝路径（缺 --confirm / 越域）必须零副作用，
    而 WALEngine.__init__ 会顺带 mkdir 整个 WAL 目录——只算路径、不建目录才叫零副作用。
    """
    try:
        from memory.wal_engine import TOMBSTONE_FILE_NAME as _name
    except Exception:
        _name = _TOMBSTONE_FILE_NAME_FALLBACK
    return os.path.join(os.path.dirname(_wal_path()), _name)


def _forget_engine():
    """构造与 CLI 工作区同源的 WALEngine（只用来读写存根，不碰 DB）。"""
    from memory.wal_engine import WALEngine
    return WALEngine(os.path.dirname(_wal_path()))


def _compensate_tombstones(ids):
    """撤回本批**由本次新登记**的存根（DB/WAL 回滚后的补偿动作）。

    调用 A 路的正式接口 `WALEngine.remove_tombstone`（M01 第三轮 A 路交付，
    memory/wal_engine.py）。语义：删掉该 id 的全部存根行并 fsync；
    True = 删掉了/本来就没有，False = 写失败/存根文件读不动（fail-closed）。
    返回"没删掉"的 id 列表（空 = 补偿干净）；调用方负责 emit + 在返回值里显式标注。

    ★合并记录（2026-10-13 小D，合并人）：D 路施工时 A 路接口尚未落地，
    故 D 路曾带一个 `_remove_tombstone_fallback()` 本地兜底 + getattr 分支；
    合并时已**整段删除兜底函数**，此处直接调用正式接口。
    ⚠️【不】要再退回 getattr 探测式降级：接口缺失属于"装配错误"，必须响亮地失败
      （返回 None → 调用方记补偿失败并如实上报），而不是悄悄走一条语义可能不同的旁路。
    """
    if not ids:
        return []
    try:
        engine = _forget_engine()
    except Exception as exc:
        logger.warning("_compensate_tombstones: engine unavailable: %r", exc)
        engine = None
    remover = getattr(engine, "remove_tombstone", None) if engine is not None else None
    if not callable(remover):
        # 装配错误（引擎旧版/导入到别处的 wal_engine）：整批补偿失败，如实上报，绝不假装成功。
        logger.error("_compensate_tombstones: WALEngine.remove_tombstone 不可用 —— "
                     "补偿未执行，%d 个存根可能残留（装配错误，见 M01 契约 §8）", len(ids))
        return list(ids)
    failed = []
    for cid in ids:
        try:
            ok = bool(remover(cid))
        except Exception as exc:
            logger.warning("_compensate_tombstones: %s failed: %r", cid, exc)
            ok = False
        if not ok:
            failed.append(cid)
    return failed


# ---------------------------------------------------------------------------
# 多角色玩法命令（E1-E4，2026-08-25 补齐实现——此前仅 argparse 注册）
# ---------------------------------------------------------------------------

def cmd_whisper(args):
    """E1：写入 private 记忆（仅本 namespace 可见，lookup --cross 不可达）。"""
    setattr(args, "visibility", "private")
    r = cmd_store(args)
    if r.get("ok"):
        r["visibility"] = "private"
    return r


def cmd_reveal(args):
    """E1：把 private 记忆选择性揭露（--to 某角色 / --all 全公开）。

    R1.5（2026-08-31）：身份收口——reveal 需要身份（--as/XTMEM_ACTOR）或管理员
    （XTMEM_ADMIN=1）；非管理员只能操作本域记忆；成功记账 xtmem_events。
    """
    conn = _get_conn()
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if not actor and not admin:
        conn.close()
        return {"ok": False, "error": "reveal 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    if args.all:
        vis = "shared"
    elif args.to:
        vis = "shared-with:" + args.to
    else:
        row = conn.execute("SELECT visibility FROM chunks WHERE id=?", (args.id,)).fetchone()
        conn.close()
        return {"ok": False, "error": "--to <agent> or --all required",
                "current": row["visibility"] if row else None}
    row = conn.execute("SELECT namespace FROM chunks WHERE id=?", (args.id,)).fetchone()
    if actor and row and (row["namespace"] or DEFAULT_NAMESPACE) != actor:
        conn.close()
        return {"ok": False, "error": f"只能操作本域记忆（{actor}）"}
    conn.execute("UPDATE chunks SET visibility=?, updated_at=? WHERE id=?",
                 (vis, time.time(), args.id))
    conn.commit()
    _record_cli_event("reveal", actor or "admin", target_chunk=args.id, meta={"new_visibility": vis})
    conn.close()
    return {"ok": True, "id": args.id, "visibility": vis}


def cmd_promote(args):
    """P2.5（2026-08-31）：private->shared 显式审批门禁——需 XTMEM_ADMIN=1 + --reason；留痕 promotion。"""
    conn = _get_conn()
    admin = _resolve_admin()
    if not admin:
        conn.close()
        return {"ok": False, "error": "promote 需管理员（XTMEM_ADMIN=1）显式审批"}
    if not getattr(args, "reason", None):
        conn.close()
        return {"ok": False, "error": "promote 需 --reason（脱敏通过/多角色收益/冲突已解决/审计留痕）"}
    row = conn.execute("SELECT visibility, namespace FROM chunks WHERE id=?", (args.id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "chunk not found"}
    if row["visibility"] != "private":
        conn.close()
        return {"ok": False, "error": f"仅支持 private->shared，当前 {row['visibility']}"}
    conn.execute("UPDATE chunks SET visibility='shared', updated_at=? WHERE id=?", (time.time(), args.id))
    conn.commit()
    _record_cli_event("promotion", "admin", target_chunk=args.id,
                      meta={"from": "private", "to": "shared", "reason": args.reason})
    conn.close()
    return {"ok": True, "id": args.id, "visibility": "shared"}


def _wake_zcode_observer_if_needed(to_agent: str, from_agent: str) -> None:
    """zcode 来信 → 拨动 ZCode 观察者 automation 的 next_run_at（2026-09-03）。

    用户要求"观察者智能化"：没信不触发、来信立即触发。机制 =
    ZCode 调度器每 2s 扫 automations 表（claimDue），next_run_at <= now 就派发；
    这里在 msg send 落库后，把 zcode 观察者那行的 next_run_at 拨到当前时间，
    调度器下一 tick（≤2s）就会醒来处理——真·事件驱动，不再靠 15 分钟轮询空转。

    只对 to_agent=zcode 生效（其他端的信不拨 zcode 的观察者）；失败静默（不影响发信主链路）。
    公开版默认关闭：只有显式设置 XTMEM_OBSERVER_DB 与精确的 XTMEM_OBSERVER_TITLE
    才允许接入使用者自己的自动化库；不探测宿主私有路径，不模糊修改多条任务。
    """
    # 私人桌面自动化不随通用消息总线启用；明确配置文件和精确标题后才接入。
    configured_db = os.environ.get("XTMEM_OBSERVER_DB", "").strip()
    configured_title = os.environ.get("XTMEM_OBSERVER_TITLE", "").strip()
    if not configured_db or not configured_title or to_agent != "zcode" or from_agent == "zcode":
        return
    try:
        zcode_db = Path(configured_db).expanduser()
        if not zcode_db.exists():
            return
        import sqlite3 as _sq
        _conn = _sq.connect(str(zcode_db), timeout=3)
        try:
            _conn.execute(
                "UPDATE automations SET next_run_at=?, updated_at=? "
                "WHERE title = ? AND enabled=1 AND lifecycle_status='active'",
                (int(time.time() * 1000), int(time.time() * 1000), configured_title),
            )
            _conn.commit()
        finally:
            _conn.close()
    except Exception:
        # 观察者拨表失败绝不影响发信主链路——静默
        pass


def _looks_like_ack_text(text):
    """判别文本是否为 A2A「入站确认/唤醒占位」而非对端真回复。

    与 memory/api_server.py 的 _looks_like_ack 同口径（同一批关键词），
    因为两处都在回答同一个问题："这句话是答案，还是管道在响？"
    本函数只用于 R04 签收册门的准入判断——判成 ack 就走原 INSERT（仍落库、只是不签收）。
    """
    r = str(text or "")
    return any(k in r for k in ("[pending]", "已注入", "入站", "A2A 已", "正在唤醒", "轮询", "等待其回复"))


def _parse_shared_with(raw):
    """tolerant 解析 shared_with 列（JSON 数组 / 逗号串 / 空）→ set[str]。

    CLI 侧写入是 json.dumps(list)，历史行可能是逗号串；两种都得认，否则"名单比对"
    会把合法子集误判成扩权（假红）或把扩权误判成子集（漏放）。
    """
    r = str(raw or "").strip()
    if not r:
        return set()
    try:
        v = json.loads(r)
        if isinstance(v, list):
            return {str(x).strip() for x in v if str(x).strip()}
        if isinstance(v, str):
            return {x.strip() for x in v.split(",") if x.strip()}
    except Exception:
        pass
    return {x.strip() for x in r.split(",") if x.strip()}


# 可见性宽窄序：private（最紧） < shared_with < shared（最宽）。
_VIS_RANK = {"private": 0, "shared_with": 1, "shared": 2}


def _clamp_reply_permissions(source_row, requested_vis, requested_sw):
    """F01（2026-10-15）：回程可见性【不得宽于源】。返回 (visibility, shared_with)。

    规则（四档，全部保守）：
      1. 无显式 --visibility → 继承源；
      2. 显式宽于源（private→shared 等）→ 收回到源（扩权请求被拒，不留痕于权限列）；
      3. 显式窄于源（shared→private 等）→ 也保持源——否则"源请求者仍可读"这条会破；
      4. 同级 shared_with → 名单必须是源的子集，多出接收者就收回到源。

    为什么要"宽窄都收回源"而不是允许收紧：本函数的调用场景是**同一次外发的回程**，
    它的读者集合在源消息发出时就已确定；回程是替源消息带答案回来，不是一次新的投递决策。
    要换权限就发一条新消息，不要在回程上改锚点。
    """
    svis = str((source_row or {}).get("visibility") or "")
    if not svis:
        # 源不可知（无源行/老库空值）→ 不擅自放大：退回最保守的 private 会误伤正常回程，
        # 这里保持调用方给的值（缺省 shared 由上层决定），但显式放宽同样不适用。
        return requested_vis, requested_sw
    ssw = str((source_row or {}).get("shared_with") or "")
    rvis = str(requested_vis or "")
    if not rvis:
        return svis, ssw                      # 规则 1：默认继承
    if _VIS_RANK.get(rvis, -1) != _VIS_RANK.get(svis, -2):
        return svis, ssw                      # 规则 2/3：不同级一律以源为准
    if rvis == "shared_with":
        if _parse_shared_with(requested_sw) - _parse_shared_with(ssw):
            return svis, ssw                  # 规则 4：名单越界 → 收回
    return rvis, (requested_sw if rvis == "shared_with" else ssw if svis == "shared_with" else "")


def _legal_identities_of(agent):
    """某 agent/namespace 的合法身份别名集合（F02 资格判定的唯一换算处）。

    ⚠️ G01（2026-10-15 返修）：实现已收进签收册 `delivery_jobs.legal_identities_of`
    ——同一套绑定规则现在有 CLI / worker / poller / 恢复四扇门在共用，若这里另存一份
    别名换算，四扇门就会各自漂移（G01 的病根正是"同一条规则写了两遍，只改了一处"）。
    本函数保留名字只因既有守卫按它取用；逻辑只此一份，插件不可用时才退化成"只有自己"。
    """
    a = str(agent or "").strip()
    if not a:
        return set()
    try:
        from memory import delivery_jobs
        return delivery_jobs.legal_identities_of(a)
    except Exception:
        # 独立跑 CLI / 无插件上下文：宁可判窄不可判宽（旧实现同口径）。
        return {a}


def _reply_binding_ok(source_row, job, actor, to_agent):
    """F02/G01（2026-10-15）：签收资格 = 原投递目标 + 正确回程接收者。返回 (ok, reason)。

    为什么光有 actor 不够：`cmd_msg` 只保证"from == 宿主注入的 actor"，那证明的是
    "签字的人是本端"，不是"这张单允许他签"。新签收门拿到了改完成态的能力，必须把资格
    绑回原投递目标，否则任何合法 actor 都能替别人的单签收、抢占唯一回程名额。

    ⚠️ G01：判定本体在签收册 `delivery_jobs.reply_binding_ok`（唯一实现）。此前本例
    与 api_server 的查重各写一套，于是"拒绝签收"与"查重认可"对同一条帖给出相反结论——
    错收件人的普通帖被后续当成了有效回程。本函数只做转调，不复制规则。
    """
    try:
        from memory import delivery_jobs
        src = dict(source_row) if not isinstance(source_row, dict) and source_row is not None else source_row
        return delivery_jobs.reply_binding_ok(src, job, actor, to_agent)
    except Exception:
        # 兜底保持原语义（宁可判窄）：源缺失/无单/actor 不属目标/接收者不符 一律拒绝。
        if not source_row:
            return False, "missing_source"
        if not job:
            return False, "no_job"
        if not _legal_identities_of(actor) & _legal_identities_of(job.get("target_agent")):
            return False, "actor_not_target"
        src_from = str((source_row or {}).get("from_agent") or "")
        if src_from and str(to_agent or "") not in _legal_identities_of(src_from):
            return False, "recipient_mismatch"
        return True, ""


def _delivery_reply_via_ledger(conn, mid, args, kind, state, reply_to, room_id, topic_id,
                               visibility, shared_with, delegation_id, persist, inject,
                               wake_prompt, chunk_id, deadline_s, actor=""):
    """R04（2026-10-15）：CLI 直接回帖走 delivery_jobs 同一本签收册，重复回程只留一条。

    入参：已打开的 CLI 连接 conn（本函数【不】commit，由调用方决定）、以及 cmd_msg
          已解析好的各列取值；actor = 宿主可信身份（F02 资格判定用）。
    返回：成功接管则返回与正常 INSERT 同形状的 {"ok":True,"id":...,...} 字典；
          未接管（reply_to 不命中任何已登记投递行 / 资格不符）返回 None，调用方走原 INSERT 路径。
    副作用：命中时在 delivery_jobs.record_reply 的唯一操作行锁内插入消息表行并登记完成态；
          不 commit（事务归属仍归调用方，避免嵌套事务把外层写一起带走）。

    为什么不直接全文去重：签收册的键是 operation_id（同一次外发），不是正文。
    只有该 reply_to 确实对应一条登记的投递时才算"同一次外发的回程"；
    人工发的普通新消息、或不同 operation 的同文正文都必须照常落库。

    ⚠️【不】把内部 notice / 入站占位也接进来（2026-10-15 实测踩到的自噬 bug）：
    api_server 发完消息会自己写一条 `已唤醒X，等待其回复……` 的 notice（kind=notice，带
    --reply-to 源消息）。若签收册门把它当业务回程，就会在真答案到达前先把 job 置成
    completed —— poller 拿到真 FINAL-ANSWER 时 record_reply 见 completed 判 duplicate，
    正文永远进不了消息表（正常对照直接空回）。判据：notice 类与 _looks_like_ack 文本
    一律不接管，走原 INSERT 落库（它们是管道占位，不代表"对方答复了"）。
    """
    # 内部占位不是业务回程：kind=notice，或文本形如 A2A 入站确认/唤醒提示。
    if str(kind or "") == "notice" or _looks_like_ack_text(str(getattr(args, "text", "") or "")):
        return None
    try:
        from memory import delivery_jobs
    except Exception:
        return None
    db_path = _cli_resolve_db()
    op_id = "a2a-" + str(reply_to)
    job = delivery_jobs.get_job(db_path, op_id)
    if not job:
        return None

    # F02：资格绑定在【消费名额之前】判 —— 不合格一律不接管、不写完成态。
    source_row = conn.execute(
        "SELECT from_agent, visibility, shared_with, room_id, topic_id FROM messages WHERE id=?",
        (reply_to,)).fetchone()
    source = dict(zip(("from_agent", "visibility", "shared_with", "room_id", "topic_id"), source_row)) if source_row else None
    ok, reason = _reply_binding_ok(source, job, actor or str(getattr(args, "from_agent", "") or ""),
                                   getattr(args, "to", ""))
    if not ok:
        _record_cli_event("msg_reply_binding_rejected", actor or str(getattr(args, "from_agent", "") or "cli"),
                          meta={"to": str(getattr(args, "to", "") or ""), "reason": reason})
        return None

    if job.get("status") in ("completed", "acked"):
        # 已经收过回程：CLI 这次是迟到/重复的那扇门，直接回成功但不新增行。
        return {"ok": True, "id": job.get("operation_id") or op_id, "from": args.from_agent,
                "to": args.to, "kind": kind, "room": room_id, "deduplicated": True}

    # F01（2026-10-15）：已关联投递的回程，可见性一律向源消息看齐——显式参数不得扩权。
    # ⚠️ 读 args.visibility 的【原始值】（可能是空=没传），不能用 cmd_msg 里那个
    # `or "shared"` 之后的变量：那个默认值本身就是病灶（不传就变 shared，盖过源 private）。
    visibility, shared_with = _clamp_reply_permissions(
        source, str(getattr(args, "visibility", "") or ""), shared_with)

    text = str(args.text)
    reply_id = ("reply-" + hashlib.sha256(
        (op_id + "\0" + hashlib.sha256(text.encode("utf-8")).hexdigest()).encode("utf-8")).hexdigest()[:24])
    captured = {}

    def _insert(c, job_row, reply, digest):
        """在签收册事务里落回程行；可见性继承源消息（R08/F01 同口径）。

        ⚠️ G01（2026-10-15 返修）：判重不能只比"from + reply_to + 正文"。
        本函数所在门已按 (actor=目标, 接收者=原请求人) 通过资格判定，故这里的同文行
        本身已是本单的合法回程；但仍统一走签收册唯一的绑定判据，避免将来某扇门
        把这行删改后"查到同文就 return"又把名额占住。返回 {reply_present,...} 供
        record_reply 决定能否盖 completed（见 delivery_jobs.record_reply 的三态契约）。
        """
        existed = delivery_jobs.existing_bound_reply_id(
            c, args.from_agent, reply_to, str(reply), source, job_row.get("target_agent"))
        if existed:
            captured["id"] = existed
            return {"reply_present": True, "inserted": False, "id": existed}
        # ⚠️【不】用 c.total_changes 直接判本条是否插进去：它是**连接累计**值，
        # 本连接在 _connect 里跑过建表/补列，累计值天然非 0，拿它当"插了"会把
        # "源行不存在所以没插"也判成插成功（假完成）。取前后差值才是本条语句的效果。
        before = c.total_changes
        c.execute(
            "INSERT OR IGNORE INTO messages"
            " (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind, state, reply_to, delegation_id, persist, inject, wake_prompt, visibility, shared_with, chunk_id, woke_at, deadline_s)"
            " SELECT ?,?,?,?, ?,0,COALESCE(NULLIF(?,''),room_id),COALESCE(NULLIF(?,''),topic_id),?,?,?,?,?,?,?,"
            " COALESCE(NULLIF(?,''),visibility),COALESCE(NULLIF(?,''),shared_with),?,NULL,? FROM messages WHERE id=?",
            (reply_id, args.from_agent, args.to, str(reply), time.time(), room_id, topic_id,
             kind, state, reply_to, delegation_id, persist, inject, wake_prompt,
             visibility, shared_with, chunk_id, deadline_s, reply_to),
        )
        captured["id"] = reply_id
        # 源行不存在时 INSERT...SELECT 无行 → 差值为 0：那不算有效回程，不许记完成。
        inserted = (c.total_changes - before) > 0
        return {"reply_present": inserted, "inserted": inserted, "id": reply_id}

    result = delivery_jobs.record_reply(db_path, op_id, job.get("peer_task_id") or "",
                                        job.get("peer_context_id") or "", text, _insert)
    if result.get("inserted"):
        return {"ok": True, "id": captured.get("id", reply_id), "from": args.from_agent,
                "to": args.to, "kind": kind, "room": room_id}
    if result.get("code") == "duplicate":
        return {"ok": True, "id": captured.get("id") or (job.get("operation_id") or op_id),
                "from": args.from_agent, "to": args.to, "kind": kind, "room": room_id,
                "deduplicated": True}
    if result.get("code") == "no_valid_reply":
        # G01：本单名额不许被"没有有效回程"的这次调用消费掉，也不许盖完成章。
        # 回 None = 交回调用方走普通 INSERT —— 让这条帖按普通评论落库（短单：
        # "普通合法评论保留"），而 job 保持未完成，留给真正给原请求人的回程。
        _record_cli_event("msg_reply_no_valid_reply", args.from_agent or "cli",
                          meta={"to": str(getattr(args, "to", "") or ""), "reply_to": reply_to})
        return None
    # 任务/上下文不匹配等：交回调用方走普通路径，不吞掉这次发送意图。
    return None


def cmd_msg(args):
    """E3：角色间留言板（send / inbox / recent）+ P5.1-1 房间/话题/kind/回执/委派扩展。

    修订 7（2026-09-02，方案 v2.4）：msg send/inbox 补 `_record_cli_event`（kind=msg_send/msg_read），
    消息活性从此有数（v1 全表 2914 行零 kind=msg 的坑）。
    """
    conn = _get_conn()
    if args.action == "send":
        if not (args.to and args.text):
            conn.close()
            return {"ok": False, "error": "--to and --text required"}
        from memory.vector_store import VectorStore
        from memory.schema import KINDS
        if VectorStore._validate_content(args.text) is None:
            conn.close()
            return {"ok": False, "error": "message rejected: failed safety validation (injection/empty)"}
        kind = str(getattr(args, "kind", "msg") or "msg")
        if kind not in KINDS:
            conn.close()
            return {"ok": False, "error": "invalid --kind: must be one of " + ",".join(sorted(KINDS))}
        # R-03（2026-09-03）：宿主身份（XTMEM_ACTOR）在场时 --from 必须等于 actor，
        # 伪造 from 直接拒绝（不落库、不留痕于消息体）。
        actor_send = _resolve_actor(args)
        sender = str(args.from_agent or "cli")
        if actor_send and sender != actor_send:
            conn.close()
            return {"ok": False, "error": "msg send 只能以本端身份 %s 发送（--from %s 伪造被拒）" % (actor_send, sender)}
        mid = "msg-" + hashlib.sha1(
            (args.from_agent + ":" + args.to + ":" + args.text + ":" + str(time.time())).encode("utf-8")
        ).hexdigest()[:8]
        room_id = str(getattr(args, "room", "") or "")
        topic_id = str(getattr(args, "topic", "") or "")
        state = str(getattr(args, "state", "") or "")
        reply_to = str(getattr(args, "reply_to", "") or "")
        # 对端回帖自动归房（2026-09-03 小D）：带 --reply-to 但没显式 --room/--topic 时，
        # 从原帖补 room_id/topic_id——否则回复 room_id='' 落不进同一间房，前端气泡不显示回复。
        if reply_to and (not room_id or not topic_id):
            _orig = conn.execute("SELECT room_id, topic_id FROM messages WHERE id=?", (reply_to,)).fetchone()
            if _orig:
                if not room_id:
                    room_id = str(_orig[0] or "")
                if not topic_id:
                    topic_id = str(_orig[1] or "")
        delegation_id = str(getattr(args, "delegation_id", "") or "")
        persist = 1 if getattr(args, "persist", False) else 0
        inject = 0 if getattr(args, "no_inject", False) else 1
        wake_prompt = str(getattr(args, "wake_prompt", "") or "")
        visibility = str(getattr(args, "visibility", "") or "shared")
        if visibility not in ("shared", "private", "shared_with"):
            conn.close()
            return {"ok": False, "error": "invalid --visibility: shared|private|shared_with"}
        shared_with = str(getattr(args, "shared_with", "") or "")
        chunk_id = str(getattr(args, "chunk_id", "") or "")
        _dl = getattr(args, "deadline_s", None)
        try:
            deadline_s = int(_dl) if _dl not in (None, "") else None
        except Exception:
            conn.close()
            return {"ok": False, "error": "invalid --deadline-s"}

        # R04（2026-10-15 返修）：带 --reply-to 的直接回帖必须过【与 poller 同一本签收册】。
        # 原先这里无条件 INSERT（消息 ID 还掺了 time.time()，天然不可能撞重），于是
        # "poller 先写 + 对端 CLI 迟到再回同一结果" = 消息表两条完全相同的回程。
        # 走 delivery_jobs.record_reply：唯一操作行锁内判重、写正文与完成态同事务。
        #
        # 【不】把这套去重放大成全文去重：只有当 reply_to 命中一个【已登记的投递行】时才走
        # 签收册（那才是同一次外发的回程）；人工发的普通新消息、或不同 operation 的同文正文，
        # 照旧走原 INSERT，不被误吞。
        if reply_to:
            try:
                _adopt = _delivery_reply_via_ledger(
                    conn, mid, args, kind, state, reply_to, room_id, topic_id, visibility,
                    shared_with, delegation_id, persist, inject, wake_prompt, chunk_id, deadline_s,
                    actor=actor_send or sender)
            except Exception as _e:
                _adopt = None
                _record_cli_event("msg_send_ledger_error", args.from_agent or "cli",
                                  meta={"to": args.to, "error_type": type(_e).__name__})
            if _adopt is not None:
                conn.commit()
                conn.close()
                _record_cli_event("msg_send", args.from_agent or "cli",
                                  meta={"to": args.to, "kind": kind, "room": room_id, "id": _adopt["id"]})
                _wake_zcode_observer_if_needed(args.to, args.from_agent or "cli")
                return _adopt

        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind, state, reply_to, delegation_id, persist, inject, wake_prompt, visibility, shared_with, chunk_id, woke_at, deadline_s)"
            " VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (mid, args.from_agent, args.to, args.text, time.time(), room_id, topic_id, kind, state, reply_to, delegation_id, persist, inject, wake_prompt, visibility, shared_with, chunk_id, deadline_s),
        )
        conn.commit()
        conn.close()
        _record_cli_event("msg_send", args.from_agent or "cli", meta={"to": args.to, "kind": kind, "room": room_id, "id": mid})
        _wake_zcode_observer_if_needed(args.to, args.from_agent or "cli")
        return {"ok": True, "id": mid, "from": args.from_agent, "to": args.to, "kind": kind, "room": room_id}
    who = _resolve_actor(args) or args.from_agent or "cli"
    room = str(getattr(args, "room", "") or "")
    kindf = str(getattr(args, "kind", "") or "")
    if args.action == "inbox":
        sql = "SELECT id, from_agent, content, ts, room_id, kind FROM messages WHERE to_agent=? AND read=0"
        params = [who]
        if room:
            sql += " AND room_id=?"
            params.append(room)
        if kindf:
            sql += " AND kind=?"
            params.append(kindf)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(args.top)
        rows = list(conn.execute(sql, params))
        # P1-J / R-14：只标记本次 SELECT 命中的 id（与查询同 room/kind/top），
        # 空结果不更新任何行——room-a 的 inbox 不再误刷 room-b/task。
        _ids = [r["id"] for r in rows]
        _n_read = 0
        if _ids:
            _marks = ",".join("?" * len(_ids))
            _upd = conn.execute(
                "UPDATE messages SET read=1 WHERE to_agent=? AND read=0 AND id IN (%s)" % _marks,
                [who, *_ids],
            )
            _n_read = int(_upd.rowcount or 0)
        conn.commit()
    else:  # recent
        sql = "SELECT id, from_agent, to_agent, content, ts, read, room_id, kind FROM messages"
        params = []
        if room:
            sql += " WHERE room_id=?"
            params.append(room)
        if kindf:
            sql += (" AND kind=?" if room else " WHERE kind=?")
            params.append(kindf)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(args.top)
        rows = list(conn.execute(sql, params))
    conn.close()
    # P5.1-2b 尾巴①（2026-09-02）：inbox 标记已读即 msg_read 事件（消息活性有数）
    if args.action == "inbox":
        _record_cli_event("msg_read", who, meta={"count": _n_read, "room": room, "kind": kindf})
    return {"ok": True, "count": len(rows), "items": [dict(r) for r in rows]}


def _parse_members_arg(raw):
    """--members 宽容解析：JSON 数组串 / 逗号名单 → list（防 double-encode 历史坑）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if s.startswith("[") or s.startswith("{"):
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else [str(v)]
        except Exception:
            pass
    return [x.strip() for x in s.split(",") if x.strip()]


def cmd_rooms(args):
    """P5.1-1（2026-09-02）：房间子命令（create / list / get / set）。

    rooms 表 = 驾驶舱群聊房间元数据（members JSON / mode / invite_prompt / current_round / pending_speakers）。
    """
    conn = _get_conn()
    if args.action == "create":
        room_id = str(args.id or "").strip()
        if not room_id:
            conn.close()
            return {"ok": False, "error": "--id required"}
        try:
            members = json.dumps(_parse_members_arg(args.members), ensure_ascii=False)
        except Exception:
            members = "[]"
        conn.execute(
            "INSERT OR REPLACE INTO rooms (id, name, members, mode, invite_prompt, current_round, pending_speakers, ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (room_id, args.name or "", members, args.mode or "invite_only", args.invite_prompt or "", 0, "[]", time.time()),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "id": room_id}
    if args.action == "set":
        """P5.1-2b（2026-09-02）：rooms set——驾驶舱 schedule 切 mode 的 CLI 单入口。"""
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (args.id,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "room not found: " + str(args.id)}
        sets, vals = [], []
        for _col in ("name", "mode", "invite_prompt"):
            _v = getattr(args, _col, None)
            if _v is not None:
                sets.append(_col + "=?")
                vals.append(_v)
        if args.members is not None:
            sets.append("members=?")
            vals.append(json.dumps(_parse_members_arg(args.members), ensure_ascii=False))
        if not sets:
            conn.close()
            return {"ok": False, "error": "nothing to set (--name/--mode/--invite-prompt/--members)"}
        sets.append("ts=?")
        vals.append(time.time())
        vals.append(args.id)
        conn.execute("UPDATE rooms SET " + ", ".join(sets) + " WHERE id=?", vals)
        conn.commit()
        conn.close()
        return {"ok": True, "id": args.id}
    if args.action == "get":
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (args.id,)).fetchone()
        conn.close()
        if not row:
            return {"ok": False, "error": "room not found: " + str(args.id)}
        d = dict(row)
        d["members"] = json.loads(d.get("members") or "[]")
        d["pending_speakers"] = json.loads(d.get("pending_speakers") or "[]")
        return {"ok": True, "room": d}
    # list
    rows = list(conn.execute("SELECT id, name, mode, current_round, ts FROM rooms ORDER BY ts DESC"))
    conn.close()
    return {"ok": True, "count": len(rows), "rooms": [dict(r) for r in rows]}


def cmd_relations(args):
    """E2：agent-agent 关系网（set / status / summary）。

    R2.4（2026-08-31）：status/summary 收窄到本角色——有 actor 时 SQL 加
    WHERE agent_a=? OR agent_b=?；无 identity（且非管理员）拒绝，防窥探全库关系。
    """
    conn = _get_conn()
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if args.action == "set":
        a, b = args.agent_a, args.agent_b
        if not (a and b):
            conn.close()
            return {"ok": False, "error": "--agent-a and --agent-b required"}
        if b < a:
            a, b = b, a  # 归一化：a < b
        row = conn.execute(
            "SELECT sentiment, trust, debt, history FROM relations WHERE agent_a=? AND agent_b=?",
            (a, b),
        ).fetchone()
        hist = json.loads(row["history"]) if row and row["history"] else []
        upd = {
            "sentiment": args.sentiment if args.sentiment is not None else (row["sentiment"] if row else 0.0),
            "trust": args.trust if args.trust is not None else (row["trust"] if row else 0.5),
            "debt": args.debt if args.debt is not None else (row["debt"] if row else "{}"),
        }
        if args.note:
            hist.append({"ts": time.time(), "note": args.note,
                         "sentiment": upd["sentiment"], "trust": upd["trust"]})
        conn.execute(
            "INSERT OR REPLACE INTO relations"
            " (agent_a, agent_b, sentiment, trust, debt, last_interaction, history)"
            " VALUES (?,?,?,?,?,?,?)",
            (a, b, upd["sentiment"], upd["trust"], upd["debt"],
             time.time(), json.dumps(hist, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "pair": [a, b], "history_count": len(hist), **upd}
    if args.action == "status":
        who = actor or args.agent
        if not who and not admin:
            conn.close()
            return {"ok": False, "error": "relations status 需要身份（--as 或 XTMEM_ACTOR）；管理员设 XTMEM_ADMIN=1"}
        if who:
            rows = list(conn.execute(
                "SELECT * FROM relations WHERE agent_a=? OR agent_b=?", (who, who)))
        else:
            rows = list(conn.execute("SELECT * FROM relations"))
        conn.close()
        return {"ok": True, "count": len(rows), "items": [dict(r) for r in rows]}
    # summary
    if not actor and not admin:
        conn.close()
        return {"ok": False, "error": "relations summary 需要身份（--as 或 XTMEM_ACTOR）；管理员设 XTMEM_ADMIN=1"}
    if actor:
        rows = list(conn.execute(
            "SELECT * FROM relations WHERE agent_a=? OR agent_b=? ORDER BY last_interaction DESC",
            (actor, actor)))
    else:
        rows = list(conn.execute("SELECT * FROM relations ORDER BY last_interaction DESC"))
    conn.close()
    return {"ok": True, "count": len(rows),
            "pairs": [[r["agent_a"], r["agent_b"],
                       round(r["sentiment"], 2), round(r["trust"], 2)] for r in rows]}


def cmd_reflect(args):
    """E4：轻量反思——生成一条 reflection 私有记忆（仅本角色可见）。"""
    who = args.agent or "cli"
    if not hasattr(args, "namespace"):
        setattr(args, "namespace", who)
    args.namespace = who
    args.agent = who
    if not hasattr(args, "project"):
        setattr(args, "project", "")
    args.tags = "reflection"
    setattr(args, "visibility", "private")
    r = cmd_store(args)
    if r.get("ok"):
        r["reflection"] = True
        r["agent"] = who
    return r


def cmd_forget(args):
    """R1.5+R-04（2026-09-03）：forget 三段式——① 只读解析目标 → ② 身份/域鉴权 →
    ③ 副作用（DB 事务 + WAL 可恢复备份）。任何越域/无权拒绝路径零副作用：
    混合域 query 被拒后 current.wal 字节级不变（实机复现的 R-04 已修）。

    ★M01-R6（2026-10-13 · D 路）：③ 的副作用顺序焊死为
      ① durable 存根登记（add_tombstone，失败即拒）→ ② WAL purge（带备份）→ ③ DB 单事务；
      ③ 失败 = DB rollback + WAL 备份回滚 + 补偿删存根（remove_tombstone）。
    为什么存根在第一：遗忘必须 fail-safe——存根先落，后面任何失败都不会让被遗忘条目
    从 WAL 重放复活（旧顺序 purge 先行、无存根，重启即"忘了又活"，见验收 v2 R6 反例）。
    成功路径返回值带 "tombstone_registered": True。
    """
    conn = _get_conn()
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if not actor and not admin:
        conn.close()
        return {"ok": False, "error": "forget 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    # ① 只读解析目标（仅 SELECT，不写任何东西）
    if args.id:
        row = conn.execute("SELECT id, namespace FROM chunks WHERE id=?", (args.id,)).fetchone()
        ids = [row["id"]] if row else []
        ns_of = {args.id: (row["namespace"] or DEFAULT_NAMESPACE)} if row else {}
    elif args.query:
        q = f"%{args.query}%"
        rows = conn.execute("SELECT id, namespace FROM chunks WHERE content LIKE ?", (q,)).fetchall()
        ids = [r["id"] for r in rows]
        ns_of = {r["id"]: (r["namespace"] or DEFAULT_NAMESPACE) for r in rows}
    else:
        conn.close()
        return {"ok": False, "error": "--id or --query required"}
    # ② 权限/域校验（任何越域命中 → 拒绝，到此为止没有任何副作用）
    if actor and not admin:
        bad = [(cid, ns_of[cid]) for cid in ids if ns_of[cid] != actor]
        if bad:
            conn.close()
            return {"ok": False, "error": f"只能操作本域记忆（{actor}）；越域命中 {len(bad)} 条，已拒绝",
                    "cross_domain": bad[:10]}
    if not ids:
        conn.close()
        return {"ok": True, "removed": 0}
    # ②b 【确认闸 · 2026-09-14（顶模裁定 / 对撞裁定 A-R2）】——缺 --confirm 只回清单，零副作用。
    #    为什么必须补：三份 CLI 全无 --confirm、引擎 ContextBuilder.forget 的 confirm 闸又不在本路径上
    #    （CLI 自写 SQL 直删），等于"记忆的删除动作没有二次确认"。对「忘不掉」目标这是最大单点风险。
    #    ⚠️ 与引擎闸语义对齐：confirm=False → 只返回将命中的 id 清单（供人工复核），不删一行。
    #    【勿动】别把这段挪到 ③ 副作用之后——那等于闸设了个摆设（写都写完了才问）。
    if not getattr(args, "confirm", False):
        conn.close()
        return {
            "ok": True, "removed": 0, "confirm": False, "pending": len(ids),
            "preview": ids[:20],
            "hint": f"将退役 {len(ids)} 条（state→archived，内容保留）；确认请加 --confirm",
        }
    # ③ 副作用按【契约 §8 焊死的顺序】：①存根登记 → ②WAL purge → ③DB 事务。
    #    【勿动】别把存根登记挪到 purge/DB 之后——旧顺序（purge 先行、无存根）的后果是：
    #    purge 只从 current.wal 删行、DB 只是把 state 改 archived，重启重放照样把条目写回库里
    #    = "忘了又活"。存根先落，后续任何一步失败最坏是"还没删干净"，绝不会"复活"。
    #    ① 存根登记：每个 id 一条 durable 存根。任一条登记不上就【拒绝继续】——
    #    零副作用返回（此时 ② ③ 都还没跑，WAL/DB 原样）。理由：一条没登记的 id
    #    若放行继续删，重启后它会从 WAL 复活，等于"部分遗忘"这种最坏形态。
    _tombstoned = []
    try:
        _engine = _forget_engine()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": "forget rejected: 存根登记失败（WAL 引擎不可用）: " + str(e)}
    # ⚠️ 登记前先看一眼存量存根：只对【本次新登记】的 id 承担补偿责任。
    #    为什么：重复 forget 同一条时，旧存根可能是"上一次真的遗忘"留下的——失败补偿若把它
    #    一并删掉，等于亲手把已遗忘条目放回可复活状态（fail-safe 方向反向踩雷）。
    #    read_tombstones() 三态：None=fail-closed（文件整体读不动）。
    # ★v3 返修（2026-10-13，验收 V3-R6 上半）：存量 = None 时【不】再按"新登记"算。
    #    旧版口径是"宁可重不漏——无法区分新旧就都当新的"，出发点是想把存根清干净；
    #    但方向反了：Unknown 时 add_tombstone 自己会照常落新行（幂等短路被跳过），
    #    而补偿阶段 remove_tombstone 是拿**能力**动刀（只要文件可读就真删行）——
    #    一旦文件在"读存量那一刻"短暂不可读、"补偿那一刻"又恢复了（本反例就是这么演的），
    #    旧口径就会把**上一次真遗忘留下的旧存根**连同本次新登记一起删掉：
    #    DB 里那条还是 active，存根却没了 —— 下次重放它就复活，"忘不掉"红线当场破。
    #    正确口径（契约 §8 原话"未知新旧时不删" + 血泪教训 5"拿不准就别删"）：
    #    存量未知 ⇒ 该 id **不进入可撤销新登记集合**（_tombstoned 不收），补偿自然不碰它；
    #    代价是"本次新登记的存根可能残留"，这是**安全方向**的残留（最坏是忘多了，不是复活），
    #    且必须在返回值里显式点名 + emit，【不】许静默（契约 §8 硬要求）。
    try:
        _pre_existing = _engine.read_tombstones()
    except Exception:
        _pre_existing = None
    # 存量可信（拿到了 set）才谈得上"新旧之分"；None = 拿不准 = 整批不撤销。
    _membership_known = _pre_existing is not None
    if not _membership_known:
        logger.warning("cmd_forget: 存量存根不可读（read_tombstones=None）—— 本次登记的存根"
                       "不纳入补偿集合（未知新旧时不删，契约 §8）")
        emit('wal', 'recovery.replay', 'degraded', fallback_reason='deferred_recovery')
    for _cid in ids:
        try:
            _ok = bool(_engine.add_tombstone(_cid, reason="forget", by=actor or "cli-forget"))
        except Exception as e:
            _ok = False
            logger.warning("cmd_forget: add_tombstone %s raised: %r", _cid, e)
        if not _ok:
            # ⚠️ 已登记的兄弟 id 也不能留着：本路径对外承诺"零副作用"，
            #    留一半存根 = 下次 forget 这批时幂等短路，用户以为删了、其实一条没删。
            _leftover = _compensate_tombstones(_tombstoned)
            conn.close()
            r = {"ok": False,
                 "error": "forget rejected: 存根登记失败（未写入 durable tombstone，已零副作用返回）",
                 "failed_id": _cid}
            if _leftover:
                emit('wal', 'recovery.replay', 'failed', fallback_reason='deferred_recovery')
                r["tombstone_compensation_failed"] = _leftover
            if not _membership_known:
                r["tombstone_compensation_skipped"] = "unknown_membership"
                r["tombstone_state"] = "unknown"
            return r
        if _membership_known and _cid not in _pre_existing:
            _tombstoned.append(_cid)
    # ② WAL purge（沿用既有实现，带备份；失败自己会回滚 WAL 并返回 False）
    bak = _wal_path() + ".bak-forget-" + time.strftime("%Y%m%d%H%M%S")
    if not _purge_wal_entries(ids, bak):
        # 与 ① 失败同款：本路径零副作用，故把刚落的存根撤回，别留幽灵遗忘记录。
        _leftover = _compensate_tombstones(_tombstoned)
        conn.close()
        r = {"ok": False, "error": "forget rejected: WAL purge 失败（已保持原状）"}
        if _leftover:
            emit('wal', 'recovery.replay', 'failed', fallback_reason='deferred_recovery')
            r["tombstone_compensation_failed"] = _leftover
        if not _membership_known:
            # ★v3：存量未知 ⇒ 本次登记的存根**根本没进补偿集合**，上面的清空动作拿不到它们。
            #   必须显式标注，否则调用方会误读成"补偿干净了"——那是最危险的静默。
            r["tombstone_compensation_skipped"] = "unknown_membership"
            r["tombstone_state"] = "unknown"
        return r
    try:
        # R1.4：正文同步从 embedding_cache 删掉（向量腿不留已 forget 的正文）
        from memory.utils import compute_embedding_cache_key
        for cid in ids:
            crow = conn.execute("SELECT content FROM chunks WHERE id=?", (cid,)).fetchone()
            if crow:
                conn.execute(
                    "DELETE FROM embedding_cache WHERE text_hash=?",
                    (compute_embedding_cache_key(str(crow["content"] or "")),),
                )
        placeholders = ",".join("?" * len(ids))
        # D1.5（2026-09-09）：forget 的退役原因走 retire_reason 三列——superseded_by 只放真指针。
        # 【勿动】往 superseded_by 塞 'forget' 字串 = 把 D1 刚迁走的原因字串再写回去（B4 立刻回红）。
        # S5（2026-09-10 合并单 A-1）：同写 updated_at——serve 书架差量靠它单调拉取，
        # 只改 state 不 bump = 退役条留在书架里成幽灵（错题本 #43）。
        _forget_now = time.time()
        conn.execute(
            "UPDATE chunks SET state='archived', superseded_by='', retire_reason='forget', "
            "retired_by=?, retired_at=?, updated_at=? WHERE id IN (" + placeholders + ")",
            tuple([actor or "cli-forget", _forget_now, _forget_now] + ids),
        )
        # P0-5：归档必须同步 FTS（经 chunks rowid 映射删，FTS5 的 id 列是 UNINDEXED）
        for cid in ids:
            conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
                (cid,),
            )
        # V1.1（#6 反向血缘，2026-09-05，小D）：源 forget/archive → 指向它的 active 派生
        # 打 stale 标记（同事务）。只在源 archived（主动删）时触发；superseded 演化覆盖不触发。
        for cid in ids:
            _derived = conn.execute(
                "SELECT id, tags FROM chunks WHERE derived_from != '' AND state='active' "
                "AND source_ids LIKE ?",
                ('%"' + cid + '"%',),
            ).fetchall()
            for _d in _derived:
                try:
                    _tags = json.loads(_d["tags"] or "[]")
                    if not isinstance(_tags, list):
                        _tags = []
                except Exception:
                    _tags = []
                _tag = "lineage-stale:" + str(cid)[-8:]
                if _tag not in _tags:
                    _tags.append(_tag)
                conn.execute(
                    "UPDATE chunks SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps(_tags, ensure_ascii=False), time.time(), _d["id"]),
                )
        # V1.1 事件留痕（源归档 → 派生 stale 清单）
        _stale_map = {}
        for cid in ids:
            _sd = conn.execute(
                "SELECT id FROM chunks WHERE derived_from != '' AND state='active' "
                "AND source_ids LIKE ?",
                ('%"' + cid + '"%',),
            ).fetchall()
            if _sd:
                _stale_map[cid] = [x["id"] for x in _sd]
        if _stale_map:
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-stale', ?)",
                (time.time(), json.dumps({
                    "sources": _stale_map, "n": sum(len(v) for v in _stale_map.values()),
                    "reason": "forget", "by": "dsh-cli",
                }, ensure_ascii=False)),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        _restore_wal_backup(bak)
        # ③ 失败补偿：DB 回滚 + WAL 回滚之后，把 ① 落的存根也撤回——
        # 否则库里条目还在 active，存根却已宣称"这条被遗忘过" = 幽灵遗忘状态。
        # 【不】静默：补偿没删成的 id 必须 emit + 在返回值里点名，让人能手工收拾。
        _leftover = _compensate_tombstones(_tombstoned)
        r = {"ok": False, "error": "forget 失败，DB 与 WAL 已回滚: " + str(e)}
        if _leftover:
            emit('wal', 'recovery.replay', 'failed', fallback_reason='deferred_recovery')
            r["tombstone_compensation_failed"] = _leftover
        if not _membership_known:
            # ★v3：存量未知时本次存的那些存根不在可撤销集合里 ⇒ 它们**确定残留**。
            #   判据（R6-unknown-membership-does-not-remove-old-tombstone）正是断言
            #   "旧存根仍在" + "不得宣称补偿成功"。这里给的是"状态未知/补偿未执行"，
            #   既不谎报成功，也提示人工去查残留（安全的残留方向，见 ① 注释）。
            r["tombstone_compensation_skipped"] = "unknown_membership"
            r["tombstone_state"] = "unknown"
            logger.warning("cmd_forget: DB 失败且存量存根未知 —— 本次登记存根未补偿（可能残留），"
                           "状态未知，请人工核查 %s", _tombstone_file_path())
        return r
    _drop_wal_backup(bak)
    for cid in ids:
        _record_cli_event("forget", actor or "admin", target_chunk=cid, meta={"query": args.query})
    conn.close()
    # 同进程内复用 builder 时，重灌内存检索器（CLI 单次进程也兜底）
    if _builder is not None:
        try:
            _builder._hydrate_retriever()
        except Exception:
            pass
    # 成功路径显式回报"存根已登记"：调用方（serve/桥接层）能据此区分
    # "删干净了且重启不会复活" 与 "只是 archive 了"（R6 判据直接断言这一项）。
    return {"ok": True, "removed": len(ids), "tombstone_registered": True}



def _copy_chunk_row(src_conn, dst_conn, row, namespace, host_origin):
    """把源库一行 chunks 复制到共享库（content_hash 去重，含 FTS）。"""
    # P1-K / R-15：先转 dict——sqlite3.Row 不支持赋值（空 hash 补算此前会炸并被误吞进 skipped）
    row = dict(row)
    existing = dst_conn.execute(
        "SELECT 1 FROM chunks WHERE content_hash=?", (row["content_hash"],)
    ).fetchone()
    if existing:
        return 0
    if not row["content_hash"]:
        from memory.utils import compute_content_hash
        row["content_hash"] = compute_content_hash(str(row["content"] or ""))
    project = row["project"] if "project" in row.keys() else ""
    # R1.6（2026-08-31）：absorb 补 visibility 列——源库无该列/值非法 → 最严兜底 private，禁止回落 shared
    src_keys = row.keys()
    vis = row["visibility"] if "visibility" in src_keys else None
    if not vis or not (vis == "shared" or vis == "private" or str(vis).startswith("shared-with:")):
        vis = "private"
    rows = dst_conn.execute(
        """INSERT OR IGNORE INTO chunks
           (id, content, timestamp, importance, tags, source, token_count, content_hash,
            track, state, superseded_by, namespace, project, host_origin, updated_at, visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["id"], row["content"], row["timestamp"], row["importance"],
            row["tags"] or "", row["source"] or "", row["token_count"] or 0, row["content_hash"],
            row["track"] or "default", "active", "", namespace, project,
            f"absorb:{host_origin}", time.time(), vis,
        ),
    )
    # FTS 更新
    from memory.vector_store import _tokenize_for_fts5
    dest_rowid = dst_conn.execute("SELECT rowid FROM chunks WHERE id=?", (row["id"],)).fetchone()
    if dest_rowid:
        dst_conn.execute(
            "INSERT OR REPLACE INTO chunks_fts(rowid, id, content, tags, source) VALUES (?,?,?,?,?)",
            (dest_rowid[0], row["id"], _tokenize_for_fts5(row["content"] or ""), row["tags"], row["source"]),
        )
    return rows.rowcount


def cmd_absorb(args):
    # R2.7（2026-08-31）：absorb 身份收口——必须 --as/XTMEM_ACTOR 或 XTMEM_ADMIN
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if not actor and not admin:
        return {"ok": False, "error": "absorb 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    if actor and args.namespace and args.namespace != actor:
        return {"ok": False, "error": f"只能吸收到本域（{actor}）；指定域 {args.namespace} 不一致"}
    src = args.db
    if not os.path.exists(src):
        return {"ok": False, "error": f"source db not found: {src}"}
    from memory.open_db import open_db
    src_conn = open_db(src, ro=True)
    src_conn.row_factory = sqlite3.Row
    dst_conn = _get_conn()
    from memory.vector_store import VectorStore
    rows = src_conn.execute("SELECT * FROM chunks WHERE state='active'").fetchall()
    # P1-K / R-15：added / duplicate / rejected / error 四类计数 + 逐行错误清单；
    # 整个 absorb 显式事务，意外失败整体回滚；结束时核对 source_total == 四类之和。
    from memory.utils import compute_content_hash as _achash
    added = duplicate = rejected = error = 0
    errors: list = []
    try:
        for row in rows:
            rid = str(row["id"]) if "id" in row.keys() else ""
            try:
                r = dict(row)
                content = str(r.get("content") or "")
                # P0-2：所有写入路径（含 absorb）统一安全校验；注入内容不收入编
                if VectorStore._validate_content(content) is None:
                    rejected += 1
                    errors.append({"id": rid, "reason": "rejected:safety_validation"})
                    continue
                chash = str(r.get("content_hash") or "")
                if not chash:
                    chash = _achash(content)
                    r["content_hash"] = chash
                if dst_conn.execute(
                    "SELECT 1 FROM chunks WHERE content_hash=?", (chash,)
                ).fetchone():
                    duplicate += 1
                    continue
                got = _copy_chunk_row(
                    src_conn, dst_conn, r, args.namespace,
                    args.host_origin or "hermes-webui",
                )
                if got:
                    added += 1
                else:
                    duplicate += 1
            except Exception as exc:
                error += 1
                _reason = repr(exc)[:200]
                errors.append({"id": rid, "reason": _reason})
                _record_cli_event(
                    "absorb_error", actor or "admin",
                    target_chunk=rid,
                    meta={"source": src, "reason": _reason},
                )
        dst_conn.commit()
    except Exception as exc:
        dst_conn.rollback()
        src_conn.close()
        dst_conn.close()
        return {"ok": False, "error": "absorb aborted & rolled back: %r" % (exc,)}
    finally:
        try:
            src_conn.close()
        except Exception:
            pass
        try:
            dst_conn.close()
        except Exception:
            pass
    _record_cli_event("absorb", actor or "admin", meta={"source": src, "added": added,
                                                        "duplicates": duplicate,
                                                        "rejected": rejected, "error": error})
    result = {"ok": True, "source": src, "source_total": len(rows),
              "added": added, "duplicates": duplicate, "rejected": rejected, "error": error}
    if errors:
        result["errors"] = errors
    if len(rows) != added + duplicate + rejected + error:
        return {"ok": False, "error": "absorb count mismatch",
                "source_total": len(rows), "added": added, "duplicates": duplicate,
                "rejected": rejected, "error": error}
    return result


ZCODE_MEMORIES_ROOT = os.environ.get(
    "XTMEM_ZCODE_MEMORIES",
    str(Path.home() / ".zcode" / "cli" / "memories" / "projects"),
)


def _zcode_dir_hash16(name: str) -> str:
    """hash16 公式（用户侧已破解）：sha256(path.lower())[:16]

    R2.1（2026-08-31）：改名——表达更精确，避免与 64 位 sha256 内容哈希混淆。
    """
    return hashlib.sha256(name.lower().encode("utf-8", "ignore")).hexdigest()[:16]


def _zcode_projects():
    root = Path(ZCODE_MEMORIES_ROOT)
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) == 16 and parts[1].isalnum():
            out.append({"dir": d, "name": parts[0], "hash16": parts[1], "valid": _zcode_dir_hash16(parts[0]) == parts[1]})
    return out


def _parse_front_matter(text: str):
    if not text.startswith("---"):
        return {}, text.rstrip("\n")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.rstrip("\n")
    meta: dict = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("'").strip('"')
    body = parts[2]
    body = body.lstrip("\n")
    return meta, body.rstrip("\n")


def cmd_export(args):
    """DB → zcode 文件轨（mem- 分区）：只写 <name>-<hash16>/memory/mem-*.md，非 mem- 文件整行不动。"""
    projects = _zcode_projects()
    if not projects:
        return {"ok": False, "error": f"no zcode project dirs under {ZCODE_MEMORIES_ROOT}"}
    conn = _get_conn()
    # R1.8（2026-08-31）：export 只出 shared——private 默认不出库，要出走显式 reveal
    rows = conn.execute(
        "SELECT * FROM chunks WHERE state='active' AND visibility='shared' "
        "AND (namespace='zcode' OR host_origin LIKE 'zcode:%')"
    ).fetchall()
    exported = 0
    for proj in projects:
        memory_dir = proj["dir"] / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            project = row["project"] or ""
            if project and project != proj["name"]:
                continue
            mid = row["id"]
            fp = memory_dir / f"mem-{mid}.md"
            header = "\n".join([
                "---",
                f"id: {mid}",
                f"namespace: {row['namespace'] or 'zcode'}",
                f"project: {proj['name']}",
                f"host_origin: {row['host_origin'] or 'zcode:export'}",
                f"importance: {row['importance'] or 0}",
                f"timestamp: {row['timestamp'] or 0}",
                f"tags: {row['tags'] or ''}",
                f"track: {row['track'] or 'default'}",
                f"visibility: {row['visibility'] or 'shared'}",
                "---",
                "",
            ])
            fp.write_text(header + str(row["content"] or "") + "\n", encoding="utf-8")
            exported += 1
        last_files = {p.name: str(p.stat().st_mtime) for p in memory_dir.glob("mem-*.md")}
        conn.execute(
            "INSERT INTO xtmem_state(side,last_files,last_sync) VALUES(?,?,?) "
            "ON CONFLICT(side) DO UPDATE SET last_files=excluded.last_files,last_sync=excluded.last_sync",
            (f"zcode:{proj['name']}", json.dumps(last_files, ensure_ascii=False), time.time()),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "exported": exported, "projects": [p["name"] for p in projects]}


def cmd_import(args):
    # R2.7（2026-08-31）：import 身份收口——必须 --as/XTMEM_ACTOR 或 XTMEM_ADMIN
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if not actor and not admin:
        return {"ok": False, "error": "import 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    """zcode 文件轨 → DB（mem- 分区增量；content_hash 去重=自痕迹跳过；防循环三件套之一）。"""
    projects = _zcode_projects()
    conn = _get_conn()
    from memory.vector_store import VectorStore, _tokenize_for_fts5
    added = 0
    duplicates = 0
    rejected = 0
    for proj in projects:
        memory_dir = proj["dir"] / "memory"
        if not memory_dir.exists():
            continue
        for fp in sorted(memory_dir.glob("mem-*.md")):
            raw = fp.read_text(encoding="utf-8")
            meta, body = _parse_front_matter(raw)
            content = body if meta else raw
            if not content.strip():
                continue
            # P0-2：import 同走统一安全校验（此前直写 chunks 绕过了注入检测）
            if VectorStore._validate_content(content) is None:
                rejected += 1
                continue
            # P2-11（2026-08-31）：去重 hash 统一 MD5（compute_content_hash），
            # 此前 import 用 sha256、引擎 store 用 MD5 → 同内容两边去重失败
            from memory.utils import compute_content_hash
            content_hash = compute_content_hash(content)
            if conn.execute("SELECT 1 FROM chunks WHERE content_hash=?", (content_hash,)).fetchone():
                duplicates += 1
                continue
            # Y7 F-2 改铸（P6 波1，2026-09-04）：mem-zc- 前缀不合 CHECK mem-[0-9a-f]{8}（zc 非 hex），CHECK 上线即拒——改铸 mem-{content_hash[:8]}（存量 0 行，纯预防）
            mid = meta.get("id") or f"mem-{content_hash[:8]}"
            ts = float(meta.get("timestamp") or time.time())
            imp = float(meta.get("importance") or 0.5)
            # R1.8（2026-08-31）：import 保住 visibility——缺字段按最严兜底 private，禁止回落 shared
            vis = meta.get("visibility") or "private"
            cur = conn.execute(
                "INSERT OR IGNORE INTO chunks "
                "(id, content, timestamp, importance, tags, source, token_count, content_hash, "
                " track, state, superseded_by, namespace, project, host_origin, updated_at, visibility) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, content, ts, imp, meta.get("tags") or "", "zcode:import", 0, content_hash,
                 meta.get("track") or "default", "active", "", meta.get("namespace") or "zcode",
                 proj["name"], "zcode:import", time.time(), vis),
            )
            if not cur.rowcount:
                duplicates += 1
                continue
            rowid = conn.execute("SELECT rowid FROM chunks WHERE id=?", (mid,)).fetchone()
            if rowid:
                conn.execute(
                    "INSERT OR REPLACE INTO chunks_fts(rowid,id,content,tags,source) VALUES (?,?,?,?,?)",
                    (rowid[0], mid, _tokenize_for_fts5(content), meta.get("tags") or "", "zcode:import"),
                )
            added += 1
        conn.execute(
            "INSERT INTO xtmem_state(side,last_files,last_sync) VALUES(?,?,?) "
            "ON CONFLICT(side) DO UPDATE SET last_files=excluded.last_files,last_sync=excluded.last_sync",
            (f"zcode:{proj['name']}", "{}", time.time()),
        )
    conn.commit()
    conn.close()
    _record_cli_event("import", actor or "admin", meta={"added": added, "duplicates": duplicates, "rejected": rejected})
    return {"ok": True, "added": added, "duplicates": duplicates, "rejected": rejected,
            "projects": [p["name"] for p in projects]}


def cmd_agents_md(args):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT namespace, COUNT(*) n FROM chunks WHERE state='active' GROUP BY namespace ORDER BY n DESC"
    ).fetchall()
    conn.close()
    out = Path(getattr(args, "out", None) or "agents.md")
    lines = ["# XTMEM agents 一览", "", "| namespace | chunks |", "|---|---|"]
    for r in rows:
        lines.append(f"| {r['namespace']} | {r['n']} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(out), "namespaces": len(rows)}


def cmd_agents(args):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT namespace, COUNT(*) n FROM chunks WHERE state='active' GROUP BY namespace ORDER BY n DESC"
    ).fetchall()
    conn.close()
    return {"ok": True, "agents": [{"namespace": r["namespace"], "chunks": r["n"]} for r in rows]}


def cmd_reembed(args):
    """为 embedding_cache 缺失的 active chunks 批量重建 bge-m3 向量（正确口径）。

    缓存键与引擎一致：text_hash = sha256(全文)（P2-12 修订，不再截前 2000 字）；
    "缺失"按该键判定，每次最多 --limit 条；--limit 0=全部（分批自调）。
    返回 remaining=处理后缺失数。
    """
    from memory.utils import compute_embedding_cache_key as _cek  # noqa: F401
    from memory.embedding_provider import OllamaEmbedding
    from memory.vector_store import VectorStore

    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, content FROM chunks c WHERE state='active'"
    ).fetchall()
    cached = {r[0] for r in conn.execute("SELECT text_hash FROM embedding_cache")}
    pending = []
    for r in rows:
        # P2-12（2026-08-31）：缓存键用全文 hash（compute_embedding_cache_key），
        # 与 VectorStore/hydrate 一致；长文本不再因"前 2000 字键"错位而重复计算。
        text = str(r["content"] or "")
        th = _cek(text)
        if th not in cached:
            pending.append((r["id"], text, th))
    limit = int(getattr(args, "limit", 0) or 0)
    batch = pending[:limit] if limit else pending
    if not batch:
        conn.close()
        return {"ok": True, "done": 0, "remaining": 0}
    emb = OllamaEmbedding(
        model="bge-m3", base_url="http://127.0.0.1:11434",
        timeout=float(getattr(args, "timeout", 120) or 120),
    )
    try:
        vecs = emb.embed([x[1] for x in batch])
    except Exception as exc:
        conn.close()
        return {"ok": False, "error": f"embedding failed: {exc}"}
    now = time.time()
    done = 0
    for (mid, text, th), vec in zip(batch, vecs):
        blob = VectorStore._serialize_embedding(vec)
        # P0-2 收口（2026-09-06 外部审计整改总单-2）：text 列写空占位不写正文——
        # 与 F-5 现行口径一致（cache 靠 text_hash 键回查，正文列是明文泄漏面；
        # cmd_reembed 曾是漏网之鱼，跑一次就重新灌满明文）。hydrate/反查走 hash 键不受影响。
        conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, text, chunk_id, embedding, dim, model, created_at) VALUES (?,?,?,?,?,?,?)",
            (th, "", mid, blob, len(vec), "bge-m3", now),
        )
        done += 1
    conn.commit()
    cached2 = {r[0] for r in conn.execute("SELECT text_hash FROM embedding_cache")}
    remaining = sum(1 for x in pending if x[2] not in cached2)
    conn.close()
    return {"ok": True, "done": done, "remaining": remaining, "total_pending_before": len(pending)}

def cmd_sync(args):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO xtmem_state(side,last_sync) VALUES(?,?) "
        "ON CONFLICT(side) DO UPDATE SET last_sync=excluded.last_sync",
        (args.side or "manual", time.time()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "synced_at": time.time()}


def cmd_maintain(args):
    """P4.0-d（2026-08-31）：xtmem_events 事件表治理——按 kind 分级保留，超龄删除。

    高频类（recall/write）按 args.retention_days（默认 30 天）短保留；
    身份/敏感类（forget/reveal/promote/import/absorb/prediction_*/fingerprint）长保留 180 天。
    """
    import time as _t
    retention_days = int(getattr(args, "retention_days", None) or 30)
    LONGLIVE = {"forget", "reveal", "promote", "import", "absorb", "distill",
                "prediction_hit", "prediction_miss", "prediction_retire", "fingerprint"}
    now = _t.time()
    conn = _get_conn()
    total_before = conn.execute("SELECT COUNT(*) FROM xtmem_events").fetchone()[0]
    ll_list = list(LONGLIVE)
    qmarks = ",".join("?" for _ in ll_list)
    # 短保留（非 longlive）
    cur = conn.execute(
        f"DELETE FROM xtmem_events WHERE ts < ? AND kind NOT IN ({qmarks})",
        [now - retention_days * 86400] + ll_list,
    )
    short_del = cur.rowcount
    # 长保留
    cur2 = conn.execute(
        f"DELETE FROM xtmem_events WHERE ts < ? AND kind IN ({qmarks})",
        [now - 180 * 86400] + ll_list,
    )
    long_del = cur2.rowcount
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM xtmem_events").fetchone()[0]
    conn.close()
    return {"ok": True, "retention_days": retention_days, "long_retain_days": 180,
            "before": total_before, "deleted_short": short_del, "deleted_long": long_del, "remaining": remaining}


def cmd_prediction(args):
    """P4.2 验证回路 + 步骤4-③/5-⑤（2026-09-06）：prediction hit/miss 记账，统计单位=task_type 簇。

    - hit/miss 事件 meta 带 task_type（步骤4-③：tags json_extract 取簇键）；
    - miss 递增该 chunk tags overturn:N（单块被推翻计数保留，兼容旧格式）；
    - 步骤5-⑤（R3 核心修法）：同 task_type 连续 2 miss → 该簇【全部】active prediction 归档，
      异簇不受影响（连续判定=查该簇最近 2 条 prediction_miss 事件都无 intervening hit）。
    """
    action = args.action
    cid = args.id
    actor = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
    conn = _get_conn()
    try:
        # 步骤4-③：取簇键（task_type 从 chunk tags 解析）
        task_type = ""
        row0 = conn.execute("SELECT tags, namespace FROM chunks WHERE id=?", (cid,)).fetchone()
        if row0:
            try:
                _tags0 = json.loads(row0[0]) if isinstance(row0[0], str) else (row0[0] or [])
                for _t0 in _tags0:
                    if str(_t0).startswith("task_type:"):
                        task_type = str(_t0).split(":", 1)[1]
                        break
            except Exception:
                pass
        # 事件留痕：meta 带 task_type（步骤4-③）。
        # ⚠️ 不走 _record_cli_event——那受 _stats_enabled 闸控（默认关），而 hit/miss 事件
        # 是簇级归档（步骤5-⑤）的判定依据，必须恒写（尽力而为 try/except 兜底）。
        try:
            conn.execute(
                "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) VALUES (?,?,?,?,?,?,?)",
                (time.time(), "prediction_" + action, actor, actor, "", cid,
                 json.dumps({"task_type": task_type}, ensure_ascii=False)),
            )
            conn.commit()
        except Exception:
            pass
        if action != "miss":
            return {"ok": True, "action": action, "task_type": task_type}

        row = conn.execute("SELECT tags, state FROM chunks WHERE id=?", (cid,)).fetchone()
        if not row:
            return {"ok": False, "action": action, "error": "chunk not found"}
        try:
            tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        except Exception:
            tags = []
        # 单块 overturn 计数（兼容旧格式：第 1 次 miss→overturn:1，第 2 次→2）
        n = 1
        for t in list(tags):
            if str(t).startswith("overturn:"):
                n = int(str(t).split(":")[1]) + 1
                tags.remove(t)
        tags.append("overturn:%d" % n)
        conn.execute("UPDATE chunks SET tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), cid))
        conn.commit()

        # 步骤5-⑤（簇级统计）：同 task_type 连续 2 miss → 该簇全归档
        if not task_type:
            # 旧格式单块（无 task_type 标）沿用单块语义：2 miss 归档自己
            if n >= 2:
                # S5（2026-09-10 合并单 A-1）：补 updated_at，否则 serve 书架差量拉不到（错题本 #43）
                # 单A23-补-3①（2026-09-16 小D）：补 retire_reason/retired_by/retired_at 三列。
                # 这条路径原本【一个都不写】——归档完库里查不出"为啥退"，是哑账入口之一。
                # reason 是事实陈述（本条正是被连续 2 次预测未命中触发的），不是编的。
                _arc_now = time.time()
                conn.execute(
                    "UPDATE chunks SET state='archived', retire_reason='prediction-miss-archive', "
                    "retired_by=?, retired_at=?, updated_at=? WHERE id=?",
                    (actor or "cli-predict", _arc_now, _arc_now, cid))
                conn.commit()
                return {"ok": True, "action": action, "archived": True, "overturn": n}
            return {"ok": True, "action": action, "overturn": n}

        # 连续判定：该簇最近 3 条事件按时间倒序，前 2 条都是 miss（无 hit 打断）→ 簇归档
        ev = conn.execute(
            "SELECT kind FROM xtmem_events WHERE kind IN ('prediction_hit','prediction_miss') "
            "AND meta LIKE ? ORDER BY id DESC LIMIT 2",
            ('%"task_type": "' + task_type + '"%',),
        ).fetchall()
        kinds = [str(e[0]) for e in ev]
        if len(kinds) >= 2 and kinds[0] == "prediction_miss" and kinds[1] == "prediction_miss":
            # S5（2026-09-10 合并单 A-1）：补 updated_at，否则 serve 书架差量拉不到（错题本 #43）
            # 单A23-补-3①（2026-09-16 小D）：同上，簇级归档也要写退役三列——
            # 这批是"同 task_type 连续 2 次 miss"整簇退场，不写原因的话整簇几十条
            # 一起变哑账，将来更没法归因。
            _arc_now = time.time()
            cur = conn.execute(
                "UPDATE chunks SET state='archived', retire_reason='prediction-miss-archive', "
                "retired_by=?, retired_at=?, updated_at=? WHERE namespace=? AND track='prediction' "
                "AND state='active' AND tags LIKE ?",
                (actor or "cli-predict", _arc_now, _arc_now,
                 row0[1] if len(row0) > 1 else actor, '%task_type:' + task_type + '%'),
            )
            conn.commit()
            return {"ok": True, "action": action, "cluster_archived": True,
                    "task_type": task_type, "archived_n": cur.rowcount, "overturn": n}
        return {"ok": True, "action": action, "overturn": n, "task_type": task_type}
    finally:
        conn.close()


def cmd_session_end(args):
    actor = _resolve_actor(args) or getattr(args, "agent", None) or "dsh"
    messages_count = int(getattr(args, "messages", 0) or 0)
    _record_cli_event("session_end", actor, meta={"messages": messages_count})
    return {"ok": True, "event": "session_end", "agent": actor, "messages": messages_count}

def cmd_feedback(args):
    actor = _resolve_actor(args) or getattr(args, "agent", None) or "dsh"
    kind = str(getattr(args, "kind", "")).strip()
    target_chunk = str(getattr(args, "target", "")).strip()
    via = str(getattr(args, "via", "user-verbatim")).strip() or "user-verbatim"
    if kind not in ("corrected", "confirmed"):
        return {"ok": False, "error": f"Invalid feedback kind: {kind}"}
    if not target_chunk:
        return {"ok": False, "error": "target chunk required"}
    _record_cli_event(kind, actor, target_chunk=target_chunk, meta={"via": via})
    return {"ok": True, "event": kind, "agent": actor, "target_chunk": target_chunk, "via": via}

def cmd_mark(args):
    """P4.3 5.1：记录 memory_used 事件（记忆被 agent 显式引用 [m:xxxxxxxx] 命中）。

    命中率 = memory_used 去重 chunk / recall 去重 chunk（per-viewer）。
    """
    cid = args.id
    actor = _resolve_actor(args) or getattr(args, "as_agent", None) or DEFAULT_NAMESPACE
    if not cid:
        return {"ok": False, "error": "--id required"}
    _record_cli_event("memory_used", actor, target_chunk=cid)
    return {"ok": True, "action": "memory_used", "id": cid, "agent": actor}


def cmd_expose(args):
    """A16（2026-09-16，小D 提出 / Z指导 裁定）：批量记 exposed——"真进了注入文本"的那半边。

    为什么要单开一条命令（而不是复用 mark）：mark 记的是 `memory_used`（模型在回答里
    主动引用了 [m:id] = cited 那一态），exposed 是"这轮它被塞进提示词了"——两件事、两个态，
    混在一条命令里会让 cited/exposed 的口径互相污染。

    谁调用：dsh 侧 xtmem-tools 的 system-prompt/assemble —— 两段注入文本
    （xtmem:memory 固定摘要段 + xtmem:memory-now 现捞段）都拼完之后，扫最终行的
    [m:mem-xxxxxxxx]，去重后【一次 spawn 把本轮全部暴露落账】。
    ⚠️ 为什么必须在"拼完之后"而不是渲染函数内部：memory-now.mjs 明写"本体保持纯拼接
       便于单测"，在纯函数里写库会破坏可测性；且 exposed 的定义就是"最终注入面"，
       在渲染中途记会把"生成了一半"的东西也记进去。

    与 provider.py 的差别：玄铁端在进程内直接 increment_utility，且额外调了
    _record_event_force("exposed", ...)。CLI 侧没有那个轻量事件入口，故本命令只落
    chunk_utility 五态计数；事件线仍由现有 recall/mark 事件链负责——【不】在这里
    重复造事件（重复 = 事件表被双倍污染）。

    失败处理：单条失败记入 failed 并 warning，不中断整批、不静默吞（工单 §四 第 4 条
    "记账异常不得中断注入/答话，且不得静默吞成新 bug"）。
    """
    ids_raw = str(getattr(args, "ids", "") or "").strip()
    one = str(getattr(args, "id", "") or "").strip()
    if one and not ids_raw:
        ids_raw = one
    # 兼容分号/空格分隔（调用方是 shell 拼串，逗号最容易被转义搞坏，多留两条退路）
    ids = [s.strip() for s in ids_raw.replace(";", ",").replace(" ", ",").split(",") if s.strip()]
    ids = list(dict.fromkeys(ids))  # 去重保序：同轮同 chunk 记一次，重复=计数虚高（防假绿线）
    if not ids:
        return {"ok": False, "error": "--ids (comma separated) or --id required"}
    actor = _resolve_actor(args) or getattr(args, "as_agent", None) or DEFAULT_NAMESPACE
    try:
        builder = get_builder()
    except Exception as e:
        return {"ok": False, "error": "builder init failed: " + str(e)}
    done = 0
    failed = []
    for cid in ids:
        try:
            builder._vector_store.increment_utility(cid, viewer=actor, state="exposed")
            done += 1
        except Exception as e:
            failed.append(cid)
            logger.warning("A16 expose utility failed for chunk %s: %s", cid, e)
    # ── C-0 小件④（2026-09-16，用户批 / Z指导 补充）· 可选事件写入 ────────────────
    # 为什么需要它：A20 靶点 3 就是被"没有事件"卡住的活例——要清探针假账却定位不到
    #   （xtmem_events 里 dsh 的 exposed 事件 = 0 条），根因正是本命令只写 utility、不写事件。
    # ⚠️ 默认【不】写：不传 --record-event 时行为与改前逐字节一致、事件表零新增。
    # ⚠️ 不自造写入路径：复用 _record_cli_event_batch（与 recall/lookup 同一张表、同一格式），
    #   因此它同样受 stats.record_events 门控——返回体里回 stats_enabled，
    #   免得"开了开关却静默没写"又要靠猜（今天已经被这种静默坑过两次）。
    _want_event = bool(getattr(args, "record_event", False))
    if _want_event and done:
        _record_cli_event_batch("exposed", actor, [cid for cid in ids if cid not in failed],
                                meta={"via": "cli-expose", "recorded": done, "failed": len(failed)})
    return {"ok": True, "action": "exposed", "agent": actor, "requested": len(ids),
            "recorded": done, "failed": failed,
            "event_requested": _want_event,
            "event_written": bool(_want_event and done and _stats_enabled()),
            "stats_enabled": _stats_enabled()}


_TASK_RULES = [
    ("审计", ["审计", "审", "对账", "验收", "盘点"]),
    ("部署", ["部署", "上线", "发版", "发布", "放生"]),
    ("改代码", ["改代", "修", "bug", "bug", "实现", "写代码", "补"]),
    ("写文档", ["文档", "md", "方案", "报告", "说明"]),
    ("调试", ["调试", "排查", "定位", "报错", "崩溃"]),
    ("研究", ["研究", "调研", "查", "搜索", "对比"]),
]


def _detect_task_type(text):
    import re as _re
    lo = str(text or "").lower()
    best, best_hits = "研究", 0
    for t, kws in _TASK_RULES:
        hits = sum(1 for k in kws if k.lower() in lo)
        if hits > best_hits:
            best, best_hits = t, hits
    return best


def _upsert_mode(ns, actor, ttype, sig_id, sig_chunk_id, hit):
    """P4.2 4.2：签名 hit_count>=3 升格为「模式」——落 track=procedural + derived_from=task-mode + source_ids=[签名]。

    模式内容 = 任务类型/域 + 命中次数（真实的「被引用记忆/工作流」由后续模式库聚合 chunk_utility/recall 补，此处为升格骨架）。
    """
    content = "任务模式 · %s · %s\n该签名 %s 已命中 %d 次，升格为任务模式。" % (ns, ttype, sig_id, hit)
    builder = get_builder()
    mid = builder.store(content, metadata={
        "importance": 0.7, "tags": ["mode", "procedural", "hit:" + str(hit)],
        "session_id": actor, "type": "conversation", "namespace": ns, "project": "",
        "host_origin": "cli:"+actor, "visibility": "private", "tier": "refined",
        "track": "procedural", "derived_from": "task-mode", "source_ids": [sig_chunk_id],
    })
    return mid


def cmd_sign(args):
    """P4.2 4.1 任务签名（零 LLM）：从会话首轮 prompt 提炼 task_type/domain/keywords，落 track=='procedural' 签名块。

    字段：sig_id（sha1(task_type+domain+keywords)前8）/ task_type / domain / keywords / agent / first_seen / hit_count。
    hit_count 存 tags 的 hit:N；重复签名（同 ns+sig_id）累加。
    """
    import subprocess as _subprocess
    import hashlib as _hash
    text = str(args.text or "").strip()
    if not text:
        return {"ok": False, "error": "--text required"}
    ns = args.namespace or args.agent or DEFAULT_NAMESPACE
    actor = _resolve_actor(args) or ns
    ttype = _detect_task_type(text)
    import re as _re
    keywords = [w for w in _re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", text)][:8]
    sig_id = "sig-" + _hash.sha1((ttype + ns + "|".join(keywords)).encode()).hexdigest()[:8]
    conn = _get_conn()
    # 已有同 ns+sig_id 的 procedural 签名块？
    row = conn.execute("SELECT id, tags FROM chunks WHERE namespace=? AND track='procedural' AND content LIKE ?", (ns, "%" + sig_id + "%")).fetchone()
    if row:
        try:
            tags = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or [])
        except Exception:
            tags = []
        n = 0
        for t in list(tags):
            if str(t).startswith("hit:"):
                n = int(str(t).split(":")[1]) + 1
                tags.remove(t)
        tags.append("hit:%d" % max(n, 1))
        conn.execute("UPDATE chunks SET tags=?, updated_at=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), time.time(), row[0]))
        conn.commit()
        conn.close()
        _hit = max(n, 1)
        if _hit >= 3:
            _mode = _upsert_mode(ns, actor, ttype, sig_id, row[0], _hit)
        else:
            _mode = None
        _record_cli_event("sign", actor, target_chunk=row[0], meta={"sig_id": sig_id, "task_type": ttype, "hit": _hit})  # ⑰
        return {"ok": True, "sig_id": sig_id, "task_type": ttype, "hit": _hit, "id": row[0], "escalated": _hit >= 3, "mode_id": _mode}
    conn.close()
    content = json.dumps({"sig_id": sig_id, "task_type": ttype, "domain": ns, "keywords": keywords, "agent": actor, "first_seen": time.time(), "hit_count": 1}, ensure_ascii=False)
    # 用 builder.store 落（track=procedural, tier=refined, 无 derived_from 所以不需要 source_ids）——避免 argv 中文打码
    builder = get_builder()
    mid = builder.store(content, metadata={
        "importance": 0.5, "tags": ["procedural", "sig", "hit:1"], "session_id": actor,
        "type": "conversation", "namespace": ns, "project": "", "host_origin": "cli:"+actor,
        "visibility": "private", "tier": "refined", "track": "procedural",
    })
    if not mid:
        return {"ok": True, "sig_id": sig_id, "task_type": ttype, "skipped": "duplicate"}
    _record_cli_event("sign", actor, target_chunk=mid, meta={"sig_id": sig_id, "task_type": ttype, "hit": 1})  # ⑰
    return {"ok": True, "sig_id": sig_id, "task_type": ttype, "id": mid, "hit": 1}


def cmd_graph(args):
    """P5.0-a（2026-09-02）：canon 共现图管理——rebuild 全量重建 tag_edges / stats 只读统计。

    触发键 = chunk_entities（chunks.id 语义），不依赖 content_hash（联合报告判断4）。
    rebuild 幂等（DELETE+重算，唯一真值来源 chunk_entities），stats 附边权 Top5 供人工核验防 hub。
    """
    from memory.graph import graph_stats, rebuild_edges
    if args.action == "rebuild":
        return {"ok": True, "action": "rebuild", **rebuild_edges()}
    return {"ok": True, "action": "stats", **graph_stats()}


def cmd_trace(args):
    """D3-4（2026-09-10 派单工单）：因果链查询器。

    给一条记忆查"家谱"——把历代版本串成时间线，并溯源蒸馏原料。
    只读（除 --fix-source-ids 这个独立小修外）。
    输出：默认打【纯文本时间线叙事】（人读）；--json 打结构化（机读，信息等量）。

    P7.0（2026-09-11，Z指导派单 P7-A 任务 A）：身份收口——viewer 从 XTMEM_ACTOR/--as 取
    （解析照抄 recall 款：宿主注入身份优先、与 --as 冲突即拒），透传给 memory.trace.trace
    做可见性过滤；无身份时 fail-closed，只看得到 shared。
    ⚠️ 修前病灶：本命令既不读身份也不传 viewer，任何身份都能 trace 出他人 private 正文。
    """
    from memory.trace import trace

    mem_id = str(getattr(args, "mem_id", "") or "").strip()
    if not mem_id:
        return {"ok": False, "error": "需要 mem_id"}
    depth = int(getattr(args, "depth", 3) or 3)
    _actor = _resolve_actor(args) or ""  # P7.0：空=无身份 → trace 内 fail-closed 只放行 shared

    # B-0 漏记小修：--fix-source-ids 是独立的单条补写（把漏记的 source_ids 补上）。
    # ⚠️ 这【不】是 trace 本体，是工单 B-0 的附带补丁——写前必须校验该条确实漏记
    #   （有 derived_from 且 source_ids 为空），防误覆盖正常条目。
    fix_ids = getattr(args, "fix_source_ids", None) or ""
    if fix_ids.strip():
        ids = [x.strip() for x in fix_ids.split(",") if x.strip()]
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT source_ids, derived_from FROM chunks WHERE id=?",
                (mem_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "未找到条目 %s" % mem_id}
            cur = row["source_ids"] or ""
            from memory.trace import _parse_ids as _pi
            # 只补"确实漏记"的：已有 source_ids 就拒绝（防覆盖）
            if _pi(cur):
                return {"ok": False,
                        "error": "该条已有 source_ids=%r，拒绝覆盖（--fix-source-ids 只服务漏记小修）" % cur}
            new_json = json.dumps(ids, ensure_ascii=False)
            conn.execute(
                "UPDATE chunks SET source_ids=? WHERE id=?",
                (new_json, mem_id),
            )
            conn.commit()
            return {"ok": True, "action": "fix_source_ids", "mem_id": mem_id,
                    "source_ids": new_json, "note": "B-0 漏记补写（独立小修，非 trace 本体）"}
        finally:
            conn.close()

    conn = _get_conn()
    try:
        result = trace(conn, mem_id, depth=depth, viewer=_actor)
        # JSON 模式：信息等量（nodes/lineage/distill/successors/retired 都有），只差排版层
        if getattr(args, "json", False):
            return result
        # 默认模式：直接给人读的时间线叙事（本 CLI 其余命令是 JSON 出口，但 trace 的产品形态
        # 按工单 B-1 就是"一条时间线叙事"——所以这里打纯文本，并置 _CLI_TEXT_EMITTED
        # 让 __main__ 不再补打一遍 JSON，stdout 保持单一形态、可管道可截屏）。
        global _CLI_TEXT_EMITTED
        print(result.get("text") or "无因果链")
        _CLI_TEXT_EMITTED = True
        return {"ok": result.get("ok", False), "root": mem_id, "rendered": "text"}
    finally:
        conn.close()


def cmd_rest_token(args):
    """R-02（2026-09-03）：角色自助签发 REST token（写 xtmem_rest_auth.json）。

    门禁：admin/owner 只能由管理员签发（XTMEM_ADMIN_TOKEN/1）；
    其余角色只能签自己（XTMEM_ACTOR=<角色>）；无身份不允许签发。
    产物 token 只在本命令输出里出现一次，请调用方立即落盘保存。
    """
    import secrets as _secrets
    role = str(getattr(args, "role", "") or "").strip()
    import re as _role_re
    if not _role_re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", role):
        return {"ok": False, "error": "--role 必须是1至64位字母、数字、下划线或连字符"}
    actor = _resolve_actor(args)
    admin = _resolve_admin()
    if role == "admin" or role == "owner":
        if not admin:
            return {"ok": False, "error": "签发 %s 需要管理员凭证（XTMEM_ADMIN_TOKEN 或 XTMEM_ADMIN=1）" % role}
    elif not admin:
        if not actor:
            return {"ok": False, "error": "签发角色 token 需要身份：XTMEM_ACTOR=<角色> 或管理员凭证"}
        if actor != role:
            return {"ok": False, "error": "角色只能签自己的 token（XTMEM_ACTOR=%s 想签 %s，拒绝）" % (actor, role)}
    token = _secrets.token_urlsafe(24)
    from memory.public_runtime import home_file
    path = os.environ.get("XTMEM_REST_AUTH_FILE") or home_file("xtmem_rest_auth.json")
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    store = {}
    try:
        if os.path.exists(path):
            raw = open(path, encoding="utf-8").read()
            store = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return {"ok": False, "error": "读取 token 配置失败: %s" % e}
    store[token] = {"viewer": role, "admin": role in ("admin", "owner")}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        return {"ok": False, "error": "写 token 配置失败: %s" % e}
    return {"ok": True, "role": role, "file": path, "token": token,
            "warning": "token 仅本次显示，请立即保存（存后删屏）"}


# ===================================================================
# S2（B方案常驻书架，2026-09-06，小D）：xtmem serve —— 常驻 TCP 服务
# 协议：127.0.0.1 TCP 一行 JSON 请求 = 一行 JSON 响应（行分隔 \n）。
#   请求: {"cmd": "<cmd>", "args": {<argname>: <value>, ...}}
#   响应: {"ok": ..., ...}（cmd_* 原样结果）
# 特性：单例 PID 文件 + stale 检测；懒启动幂等（端口被占即退出）；
#       降级（连接失败方回落子进程单命令）；检索前脏检查增量水化。
# 复用：命令处理直接调 cmd_recall/cmd_store/... ——同一代码路径，
#       杜绝子进程/常驻两套行为（Spec S2）。
# ===================================================================

_SERVE_DEFAULT_PORT = int(os.environ.get("XTMEM_SERVE_PORT", "8765") or "8765")
# ⚠️ G1 二次返修（验收 N04）：PID 路径必须落在**已解析的库**旁边。
#    旧写法 `Path(os.environ.get("XTMEM_DB_PATH", DEFAULT_DB)).parent` 有两个病：
#      ① 优先取 env——而 env 可能只是"这条命令的临时重定向"，不是本端该连的库；
#      ② 一旦取 env，就与 DEFAULT_DB（走 profile 解析、可能指到 profile 目录）
#         分了家：CLI 把锁写在 guard 目录、doctor 却在 profile 目录找锁，
#         **同一个端点两条单实例锁互不相识**（实测 cli_singleton_path 与
#         doctor_singleton_path 不同目录）。
#    统一口径：PID 与描述/凭据三条路径同源，都从 resolved DB 派生
#    （与 endpoint_config._resolve_paths 逐字同规则）。显式 XTMEM_SERVE_PID 仍最高。
_SERVE_PID_PATH = os.environ.get("XTMEM_SERVE_PID") or str(
    Path(DEFAULT_DB).resolve().parent / ".xtmem_serve.pid"
)
# S2 书架脏检查水位（模块级，serve 进程内生命周期持有）
_shelf_wm = {"key": None, "at": 0.0}
_SERVE_MAX_DELTA = int(os.environ.get("XTMEM_SERVE_MAX_DELTA", "500") or "500")
_SERVE_DIRTY_CACHE_SECS = 0.2  # O-3：200ms 微节流


# ===================================================================
# 8766 性能埋点（2026-09-13，工单：new/XTMEM-派单工单-8766检索延迟专项-埋点诊断-20260913.md）
# ⚠️ 零行为改动铁律：只计时不改逻辑——retriever/builder 方法在 serve 启动时
#   用运行时包裹装壳（engine 文件零改动，包裹≠改写），_PERF 进程内持有【不】落库。
# 读法：serve 协议发 {"cmd":"stats"} → cmd_stats 回读分段耗时 + 运行时模块
#   真身路径/sha16（排查「加载了旧码/另一份副本」）。CLI 裸跑恒空（统计只在
#   serve 进程内存活），stats 不注册进 argparse 子命令 = 仅 TCP 可读。
# 【不】让埋点伤主链路：装壳/打点全程 try/except 静默，宁缺埋点不添故障。
# ===================================================================
_PERF = {"serve": {}, "retriever": {}, "meta": {"started": None}}
_PERF_WRAP_FLAG = "_perf_wrapped_8766"


def _perf_mark(bucket, stage, t0):
    """计时打点：耗时 ms 记入 _PERF[bucket][stage]（n/total/last/max 四件套）。永不抛异常。"""
    try:
        ms = (time.perf_counter() - t0) * 1000.0
        s = _PERF.setdefault(bucket, {}).setdefault(
            stage, {"n": 0, "total_ms": 0.0, "last_ms": 0.0, "max_ms": 0.0})
        s["n"] += 1
        s["total_ms"] += ms
        s["last_ms"] = round(ms, 1)
        s["max_ms"] = round(max(s["max_ms"], ms), 1)
    except Exception:
        pass


def _perf_wrap_method(obj, attr, stage, stage_of=None):
    """给实例方法装计时壳（包裹不改写；幂等，重复装壳直接跳过）。失败静默。

    stage_of(kw) 可选：按调用 kwargs 动态命名 stage（如 _search_locked 按 tier_filter 分桶）。
    """
    try:
        import functools
        orig = getattr(obj, attr, None)
        if orig is None or getattr(orig, _PERF_WRAP_FLAG, False):
            return

        @functools.wraps(orig)
        def _wrapped(*a, **kw):
            _t0 = time.perf_counter()
            try:
                return orig(*a, **kw)
            finally:
                try:
                    _perf_mark("retriever", stage_of(kw) if stage_of else stage, _t0)
                except Exception:
                    _perf_mark("retriever", stage, _t0)

        setattr(_wrapped, _PERF_WRAP_FLAG, True)
        setattr(obj, attr, _wrapped)
    except Exception:
        pass


def cmd_stats(args):
    """8766 埋点回读（2026-09-13 诊断单件①）：进程内分段耗时 + 运行时加载真身。

    【零写】承诺：不连库、不落盘、不落事件。serve TCP 发 {"cmd":"stats"} 读取；
    件①.3 的 modules/module_sha16 即「运行进程加载的代码路径与 sha」核对数据。
    """
    out = {"ok": True, "pid": os.getpid(), "ts": time.time(),
           "uptime_s": round(time.time() - (_PERF["meta"].get("started") or time.time()), 1)}
    # 上线核对要认准角色、库和协议；这里用启动快照，不为 stats 再开库连接。
    out['endpoint'] = {key: _PERF['meta'].get(key) for key in
                       ('actor', 'db_id', 'protocol_version', 'startup_cli_sha256', 'ready')}
    from memory.diagnostics import snapshot
    out['diagnostics'] = snapshot()
    out["serve"] = {k: dict(v) for k, v in _PERF.get("serve", {}).items()}
    out["retriever"] = {k: dict(v) for k, v in _PERF.get("retriever", {}).items()}
    mods = {}
    try:
        mods["xtmem.py"] = str(Path(__file__).resolve())
        import memory.hybrid_retriever as _hr
        import memory.vector_store as _vs
        mods["hybrid_retriever"] = str(Path(getattr(_hr, "__file__", "")).resolve())
        mods["vector_store"] = str(Path(getattr(_vs, "__file__", "")).resolve())
        try:
            import memory.context_builder as _cb
            mods["context_builder"] = str(Path(getattr(_cb, "__file__", "")).resolve())
        except Exception:
            pass
        def _sha16(p):
            try:
                import hashlib as _hashlib
                return _hashlib.sha256(open(p, "rb").read()).hexdigest()[:16].upper()
            except Exception:
                return ""
        out["module_sha16"] = {k: _sha16(v) for k, v in mods.items()}
    except Exception as e:
        out["modules_err"] = str(e)[:120]
    # O02-A10（2026-10-14）：stats 是可被**普通角色**经 serve 调到的诊断入口，
    # 而 modules 里装的是本地绝对路径——普通视图不得外泄路径（Spec §4.3）。
    # 处置：普通视图只留**基名 + sha16**（够做「加载了哪一份码」对账），
    #      绝对路径仅在可信管理上下文（_resolve_admin）下返回。
    # ⚠️ 不删字段只换值：S01/e2e 探针靠 modules_err/module_sha16 判「加载了旧码」，
    #    删字段会让它们从「对账」变成「缺字段」，那是另一种静默失真。
    try:
        _is_admin = bool(_resolve_admin())
    except Exception:
        _is_admin = False
    if _is_admin:
        out["modules"] = mods
    else:
        out["modules"] = {k: os.path.basename(v) for k, v in mods.items()}
        out["modules_redacted"] = True
    return out


def _serve_actual_db_path():
    """本次连接**真的挂上来的**库路径；没有（未初始化）返回 None。

    ⚠️ G4 二次返修（验收 N08）——为什么不能用"解析出来的期望路径"顶替：
      hello 要回答的是"**我**连的哪个库"，属于**服务端自述**。用
      endpoint_config.resolve_db_path() 算出来的是**客户端也能算的期望值**，
      它反映"配置说该连哪个库"，不反映"这个进程实际连上了没有"。
      库文件不存在、builder 没建起来时，期望路径照样算得出来——
      旧实现就是这么在"根本没库"的场景下报出一个 db_id 的（实测 N08）。
      所以只认**进程内真实对象**：由本次已初始化的 builder 反查它的库路径。
    """
    try:
        builder = _serve_builder.get()
        if builder is None:
            return None
        vs = getattr(builder, '_vector_store', None)
        db_path = getattr(vs, 'db_path', None)
        return str(db_path) if db_path else None
    except Exception:
        return None


def cmd_hello(args):
    """O02 应用层握手：一条只读往返即回答「你是谁、连的哪个库、协议几、就绪没」。

    为什么不能只看 TCP 通：TCP 只证明**某个**进程在听端口。无关服务、旧版
    serve、连错库的 serve 都能让 connect() 成功（O02-A05）。本命令把
    actor/db_id/protocol_version/ready 四件事一次性报出来，客户端据此四验。

    ⚠️ 严格零参数：任何 args 都被 _serve_prepare_args 拒（args_invalid）。
    为什么：四验的期望值必须来自**客户端自己的可信配置**，若允许客户端把
    期望值塞进来，四验就退化成"自己跟自己比"——那是 I12 的变体。

    ⚠️ 数据来源两段式（G4 返修补）：
      ① 首选 `_PERF['meta']`——由 cmd_serve 在 listen 成功后写入的**启动事实**
         （进程启动哈希、实际绑定端口等只有启动时才知道的东西）；
      ② meta 缺项时按**当前上下文**现算——因为 `_serve_handle_connection` 也可能
         被别的入口驱动（S01 的 LoopbackServer 就是直接调它、从不跑 cmd_serve）。
         那种情况下 meta 是空的，若只认 meta 就会把 actor/db_id 全报成 null ⇒
         四验永远不过 ⇒ 既有 S01 回归被打红（实测踩到）。
         现算**不等于**编造：actor 取自本次已认证的 AuthContext，db_id 取自
         现解析出的库路径，都是当下可核的事实。
      ③ `ready` 语义仍然是"初始化完成、可以服务"：应答本身证明通道在服务，
         故 meta 无值时按 True；meta 有值时以启动时写入的为准。
    """
    meta = _PERF.get('meta', {})
    if not meta.get('actor'):
        # ② 现算：actor 只能来自本次已认证上下文，绝不从请求参数取
        trusted = _serve_auth.get()
        actor = trusted.actor if trusted is not None else os.environ.get('XTMEM_ACTOR', '').strip()
    else:
        actor = meta.get('actor')
    db_id = meta.get('db_id')
    ready_reason = ''
    if not db_id:
        # ⚠️ G4 二次返修（验收 N08）：**不许拿"期望路径的 hash"当实际库**。
        #    旧实现调 endpoint_config.resolve_db_path() 取 db_id —— 那是**期望值**，
        #    客户端侧同样算得出来。服务端报期望值自述 = 四验退化成自己跟自己比。
        #    只认 meta（cmd_serve 写的启动事实）或本次真的挂上来的 builder。
        actual_db = _serve_actual_db_path()
        if actual_db:
            db_id = hashlib.sha256(str(actual_db).encode('utf-8')).hexdigest()
        else:
            db_id = None
            ready_reason = "no_initialized_builder_no_actual_database"
    from memory.serve_protocol import PROTOCOL_VERSION as _PV
    ready = meta.get('ready')
    if ready is None:
        # ⚠️ G4 二次返修（验收 N08）：**"能答 hello"不证明"初始化完成"**。
        #    旧实现写"应答本身证明通道在服务，故按 True"——实测：库文件不存在、
        #    builder=None、启动记录无 ready，hello 仍报 ready=true。那是编造。
        #    只认正面证据：本次连接确实挂上了一个已初始化的书架（builder）。
        ready = _serve_actual_db_path() is not None
        if not ready and not ready_reason:
            ready_reason = "hello_alone_does_not_prove_initialization"
    return {
        "ok": True,
        "hello": True,
        "pid": os.getpid(),
        "actor": actor,
        "db_id": db_id,
        "protocol_version": meta.get("protocol_version") or _PV,
        "startup_cli_sha256": meta.get("startup_cli_sha256"),
        "ready": bool(ready),
        "ready_reason": meta.get("ready_reason") or ready_reason,
        "port": meta.get("port"),
        "host": meta.get("host"),
        "db_source": meta.get("db_source"),
    }


def _shelf_watermark(conn) -> str:
    """S2 脏检查探针：单行 SELECT 出 (active数, max_updated_at, max_timestamp)。"""
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(MAX(updated_at),0) mu, "
        "COALESCE(MAX(timestamp),0) mt, COALESCE(MAX(content_hash),'') mh "
        "FROM chunks WHERE state='active'"
    ).fetchone()
    return "|".join(str(x) for x in (row["c"], row["mu"], row["mt"], row["mh"]))


def _pid_alive_serve(pid: int) -> bool:
    """进程存活探测（Windows OpenProcess / POSIX kill-0）。"""
    try:
        import os as _os
        if _os.name == "nt":
            import ctypes
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        _os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _serve_take_singleton() -> bool:
    """单例 PID 锁。占用成功返回 True；已有活进程返回 False。

    8766 修复单件②（2026-09-13）：读锁异常不再无条件放行——旧兜底 `except: return True`
    （"宁可并跑"）是双 serve 之根：pid 文件被瞬时占用/IO 抖动读炸一次就放行第二个 serve，
    两个 serve 同写一库才是真事故。现改两段式：读炸 → 0.2s 后重试一次 → 仍炸则拒绝启动
    （False；MCP 探活失败会回落子进程模式，业务不断——宁不起，勿并跑）。
    """
    for _attempt in (1, 2):
        try:
            if os.path.exists(_SERVE_PID_PATH):
                raw = open(_SERVE_PID_PATH, encoding="utf-8").read().strip()
                if raw.isdigit() and _pid_alive_serve(int(raw)):
                    return False  # 已有活 serve
                # stale PID：删除旧锁
                try:
                    os.remove(_SERVE_PID_PATH)
                except OSError:
                    pass
            Path(_SERVE_PID_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(_SERVE_PID_PATH, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except Exception:
            if _attempt == 1:
                time.sleep(0.2)
                continue
            logger.warning("serve singleton: pid file unreadable twice — refuse to start (anti double-serve)")
            return False
    return False


def _serve_release_singleton():
    try:
        if os.path.exists(_SERVE_PID_PATH):
            raw = open(_SERVE_PID_PATH, encoding="utf-8").read().strip()
            if raw == str(os.getpid()):
                os.remove(_SERVE_PID_PATH)
    except Exception:
        pass


def _serve_live_probe(actor: str, host: str, port: int, timeout: float = 2.0,
                      expected_db_id: str = ""):
    """应用层探活：本端口上是不是**本角色、本库、同协议、已就绪**的 serve 在答。

    做什么：拿宿主凭据发一条只读 `hello`，**完整四验**（actor/db_id/protocol/ready）。
    返回 None = 没答上来或不匹配；返回 dict = 对端自述。

    ⚠️ G4 二次返修（验收 N09）：**旧实现只验 actor 一格**，于是
      "actor 对、但 db_id=wrong-db、protocol=999、ready=false" 的被测端点
      照样被判 `ok=true,already_running=true`。只核身份不核库/协议/就绪，
      等于把"端口上有个认得本角色的进程"当成了"本端点已在正常服务"——
      这正是 O02 要堵的假就绪。现改为逐格核验，任一格不符即返回 None。
    ⚠️ 期望 db_id 的默认取值（不传时）：用**本次进程自己的解析结果**
      （serve 启动路径已解析出实际库）。调用方要覆盖时可显式传入。
    ⚠️ 不误杀、不代修复：探不通就如实返回 None，绝不杀进程、绝不改 pid 文件。
    副作用：一次只读 TCP 往返；不写任何状态、不杀任何进程。
    """
    try:
        from memory.serve_protocol import (send_request, actor_token,
                                           PROTOCOL_VERSION as _PV)
        token = os.environ.get("XTMEM_SERVE_TOKEN", "").strip() or None
        if not token and actor:
            token = actor_token(actor, DEFAULT_DB, create=False)
        if not token:
            return None
        report = send_request(host, int(port), {"cmd": "hello", "args": {}}, token, timeout)
        if not isinstance(report, dict) or report.get("ok") is not True:
            return None
        # ① actor 必须严格匹配
        if report.get("actor") != actor:
            return None
        # ② 协议版本必须是真 int 且相符（bool 不算——Python True==1 会骗过判等）
        protocol = report.get("protocol_version")
        if type(protocol) is not int or protocol != _PV:
            return None
        # ③ 就绪必须是真布尔 True（"能答 hello" 不等于"初始化完成"，见 cmd_hello）
        if report.get("ready") is not True:
            return None
        # ④ 库标识必须与期望一致（同角色同 token 也可能连到另一个库）
        expected = expected_db_id or _serve_expected_db_id()
        observed_db = report.get("db_id")
        if not isinstance(observed_db, str) or not observed_db:
            return None
        if expected and observed_db != expected:
            return None
        return {"actor": report.get("actor"), "db_id": observed_db,
                "protocol_version": protocol, "ready": report.get("ready"),
                "pid": report.get("pid")}
    except Exception:
        return None


def _serve_expected_db_id() -> str:
    """本进程期望的库标识（解析器口径）。失败返回空串（此时不拿它当判据）。"""
    try:
        from memory import endpoint_config as _epc
        return str(_epc.resolve_db_path()["db_id"])
    except Exception:
        return ""


def _serve_shelf_embedding(builder, conn, doc, cid):
    """_serve_shelf_embedding()：契约
    入参：builder（serve 的 ContextBuilder）、conn（书架库连接，Row factory）、
          doc（待 apply 的 MemoryDoc）、cid（chunk id）
    返回：(vec, src)；src ∈ "shelf-reuse" | "cache" | "computed"
    副作用：仅 computed 分支 best-effort 写回 embedding_cache（带 chunk_id，照 hydrate 补写同款）
    谁调用：_serve_sync_shelf 差量 apply 循环（8766 修复单件①，2026-09-13）
    改动影响：只动向量"供给端"，append/patch 签名零改动——引擎热路径不碰，与并行单
      （检索面修复二合一，改过滤/补位区）天然无碰撞。

    根因背景（诊断单 2026-09-13）：差量 apply 原来裸 append/patch(doc) 不传 embedding，
    每条现算 979ms（Ollama 往返），20 条欠账 = 19.6s；缓存读 0.04ms/条，差 2.4 万倍，
    且现网 20/20 缓存全有却从不查。

    ⚠️ 硬闸（宁慢勿毒，D3-2 同源雷）：embedding_cache 主键是纯 text_hash 不含 model，
    命中行必须双校验——dim == builder._expected_dim()（1024=bge-m3 系）且 model 列 ==
    当前配置 embedding_model；任一不过/unpack 炸一律按 miss 现算。B0-10 实锤库里存在
    dim=128 毒行（SimpleEmbedder 回落产物），无闸即毒向量腿。
    """
    ret = builder._retriever
    try:
        _exp_dim = int(builder._expected_dim())
    except Exception:
        _exp_dim = 1024
    try:
        _model = str((getattr(builder, "_cfg", {}) or {}).get("embedding_model", "bge-m3"))
    except Exception:
        _model = "bge-m3"

    # ① patch 同文短路：书架已有同 id 且内容未变 → 复用内存旧向量（零查询零现算）。
    #    摘标/记账类更新只 bump updated_at 不动 content，走这条最多。
    #    全零向量是 hydrate 降级模式（miss>500）的填充物，复用它等于毒腿，any() 挡掉。
    idx = ret._id_to_idx.get(cid)
    if idx is not None and idx < len(ret.docs):
        old = ret.docs[idx]
        if old is not None and old.content == doc.content:
            old_vec = ret._embeddings[idx] if idx < len(ret._embeddings) else None
            if old_vec and len(old_vec) == _exp_dim and any(old_vec):
                return old_vec, "shelf-reuse"

    # ② 查 embedding_cache（按内容指纹；0.04ms/条）
    try:
        from memory.utils import compute_embedding_cache_key
        row = conn.execute(
            "SELECT embedding, dim, model FROM embedding_cache WHERE text_hash=?",
            (compute_embedding_cache_key(doc.content),),
        ).fetchone()
    except Exception:
        row = None
    if row is not None:
        try:
            from memory.vector_store import VectorStore as _VS
            vec = _VS._deserialize_embedding(row["embedding"], int(row["dim"]))
            if len(vec) == _exp_dim and str(row["model"] or "") == _model:
                return vec, "cache"
        except Exception:
            pass  # blob 尺寸与 dim 不符/毒行 → 按 miss 现算

    # ③ miss 现算 + best-effort 写回缓存（防同文反复 miss）
    vec = ret._embed(doc.content)
    try:
        builder._vector_store.cache_embedding(
            text=doc.content, embedding=vec, model=_model, chunk_id=cid)
    except Exception:
        pass
    return vec, "computed"


def _serve_sync_shelf(builder):
    """S2 脏检查 + 增量水化：检索前轻量 SELECT，水位变了 → 拉差量 patch/append/remove。
    异常/超阈值 → 全量重建（I2 宁慢勿错）。返回 {"action": ...}。"""
    global _shelf_wm
    now = time.time()
    # O-3 微节流：200ms 内的连续检索不打 SQLite（读内存书架即可）
    if now - _shelf_wm["at"] < _SERVE_DIRTY_CACHE_SECS and _shelf_wm["key"] is not None:
        return {"action": "cached"}
    try:
        conn = _get_conn()
        try:
            cur_key = _shelf_watermark(conn)
            if _shelf_wm["key"] == cur_key:
                _shelf_wm["at"] = now
                return {"action": "clean"}

            # 水位变了：拉差量（updated_at > 上次 max_updated_at）
            prev_parts = (_shelf_wm["key"] or "0|0|0|").split("|")
            prev_max_upd = float(prev_parts[1] if len(prev_parts) > 1 else 0)
            prev_count = int(prev_parts[0]) if prev_parts[0].isdigit() else 0
            cur_count = int(cur_key.split("|")[0])

            # 异常：active 数缩水 → 全量重建。
            # S5 加固（2026-09-10 合并单 A-2）：阈值从 prev_count-5 收紧为【任何缩水即重建】。
            # 为什么敢收到 0：水位第 1 段是 state='active' 的行数——正常写入只可能让它增或持平，
            # 缩水一定意味着有条目退出了检索池（supersede/archive/物理删）。A-1 已让 5 处写者
            # 都 bump updated_at，这条兜底专防「未来再有只减不 bump 的写者」：粗但安全，
            # 代价仅在真减条时多一次全量水化（宁慢勿错，见错题本 #43）。
            if _shelf_wm["key"] is not None and cur_count < prev_count:
                logger.info("serve: shelf count shrank %d->%d — full rebuild", prev_count, cur_count)
                builder._hydrate_retriever()
                _shelf_wm.update({"key": cur_key, "at": time.time()})
                return {"action": "full-rebuild"}

            # 差量拉取
            delta_rows = conn.execute(
                "SELECT id, content, namespace, project, track, importance, timestamp, tags, "
                "state, tier, derived_from, source_ids, visibility, updated_at "
                "FROM chunks WHERE COALESCE(updated_at,0) > ? LIMIT ?",
                (prev_max_upd, _SERVE_MAX_DELTA + 1),
            ).fetchall()
            if len(delta_rows) > _SERVE_MAX_DELTA:
                # 落后过多 → 全量重建（O-4 阈值）
                logger.info("serve: delta %d > %d — full rebuild", len(delta_rows), _SERVE_MAX_DELTA)
                builder._hydrate_retriever()
                _shelf_wm.update({"key": cur_key, "at": time.time()})
                return {"action": "full-rebuild"}

            from memory.hybrid_retriever import MemoryDoc as _ShelfDoc
            from memory.context_builder import _coerce_track
            n_add = n_patch = n_rm = 0
            n_vec_reuse = n_vec_cache = n_vec_compute = 0
            for dr in delta_rows:
                cid = str(dr["id"])
                _df = (dr["derived_from"] or "").strip() if "derived_from" in dr.keys() else ""
                _tier = (dr["tier"] or "raw") if "tier" in dr.keys() else "raw"
                if dr["state"] in ("superseded", "archived"):
                    if builder._retriever.remove(cid):
                        n_rm += 1
                    continue
                doc = _ShelfDoc(
                    id=cid,
                    content=str(dr["content"]),
                    timestamp=float(dr["timestamp"] or 0),
                    importance=float(dr["importance"] or 0.5),
                    tags=json.loads(dr["tags"] or "[]") if (dr["tags"] or "").startswith("[") else [],
                    track=_coerce_track(str(dr["track"] or "default")),
                    state=str(dr["state"] or "active"),
                    namespace=str(dr["namespace"] or ""),
                    visibility=str(dr["visibility"] or "shared"),
                    tier=_tier,
                    derived_from=_df,
                )
                # 8766 修复单件①（2026-09-13）：向量供给 = 书架同文复用 → 缓存（双校验硬闸）
                # → 现算，替换旧裸 append/patch(doc)（不传 embedding 必现算 979ms/条 的根因）
                vec, _vsrc = _serve_shelf_embedding(builder, conn, doc, cid)
                if _vsrc == "shelf-reuse":
                    n_vec_reuse += 1
                elif _vsrc == "cache":
                    n_vec_cache += 1
                else:
                    n_vec_compute += 1
                if cid in builder._retriever._id_to_idx:
                    builder._retriever.patch(doc, embedding=vec)
                    n_patch += 1
                else:
                    builder._retriever.append(doc, embedding=vec)
                    n_add += 1
            _shelf_wm.update({"key": cur_key, "at": time.time()})
            # 累计计数进 _PERF（stats 可读，复测对账用）；失败静默不挡同步
            try:
                _sv = _PERF.setdefault("serve", {}).setdefault(
                    "shelf_vec_sources", {"reuse": 0, "cache": 0, "compute": 0})
                _sv["reuse"] += n_vec_reuse
                _sv["cache"] += n_vec_cache
                _sv["compute"] += n_vec_compute
            except Exception:
                pass
            logger.info("serve: shelf sync delta=%d add=%d patch=%d rm=%d vec reuse/cache/compute=%d/%d/%d",
                        len(delta_rows), n_add, n_patch, n_rm, n_vec_reuse, n_vec_cache, n_vec_compute)
            return {"action": "delta", "add": n_add, "patch": n_patch, "rm": n_rm}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("serve dirty check failed: %s — full rebuild fallback", e)
        try:
            builder._hydrate_retriever()
        except Exception:
            pass
        return {"action": "rebuild-error", "err": str(e)[:120]}


_SERVE_WARM_INTERVAL_SECS = 600.0


def _serve_warm_loop(builder, stop_event):
    """_serve_warm_loop()：契约
    入参：builder（serve 的 ContextBuilder）、stop_event（threading.Event，serve 退出置位）
    返回：无（daemon 线程体）
    副作用：每 600s 向 Ollama /api/embed 发 1 条极短 input，请求体带 keep_alive="30m"
    谁调用：cmd_serve listen 前起线程（8766 修复单件②，2026-09-13）
    改动影响：零业务语义；失败静默——Ollama 挂了不重试不告警（黑名单/降级链路归
      embedding_provider 管，这里只管"保温"）。

    为什么有它：Ollama /api/embed 默认 keep_alive=5min，serve 空闲过 5min 模型卸载，
    下一次 embed 触发冷加载 +0.5~6s 浮动（诊断单 Run B 实锤：embed 6595.6ms vs 暖态
    873ms）。心跳间隔 600s < 30m TTL → 模型常驻。【不】动 embedding_provider.py
    （超本单范围锁）；根治=其请求体加 keep_alive 一行，待用户点头另派。
    仅 OllamaEmbedding 生效：非 Ollama provider（OpenAI 兼容端等）没有卸载问题，直接退出。
    """
    try:
        _prov = getattr(getattr(builder, "_retriever", None), "embedding_provider", None)
        base_url = str(getattr(_prov, "_base_url", "") or "")
        model = str(getattr(_prov, "_model", "") or "")
        if type(_prov).__name__ != "OllamaEmbedding" or not base_url or not model:
            return
        import httpx
        url = base_url.rstrip("/") + "/api/embed"
        # trust_env=False 同 embedding_provider 教训（错题本）：本地 Ollama 不走系统代理，
        # 系统代理 127.0.0.1:7890 会吞 loopback 请求回 502
        timeout = httpx.Timeout(5.0, connect=2.0, write=2.0, pool=2.0)
        while not stop_event.is_set():
            if stop_event.wait(_SERVE_WARM_INTERVAL_SECS):
                return
            try:
                httpx.post(url, json={"model": model, "input": ["warm"], "keep_alive": "30m"},
                           timeout=timeout, trust_env=False)
            except Exception:
                pass
    except Exception:
        pass


def _serve_prepare_args(cmd, argd, auth):
    """Validate a fixed command's JSON schema before any callback or shelf sync."""
    from memory.serve_protocol import RequestRejected, WRITE_COMMANDS
    if cmd in ('stats', 'hello'):
        # O02：hello 与 stats 同款——严格零参数。四验的期望值必须来自客户端
        # **自己的可信配置**；允许客户端塞期望值进来，四验就退化成"自己跟自己比"。
        if argd:
            raise RequestRejected('args_invalid', cmd + ' accepts no arguments')
        return argparse.Namespace(cmd=cmd, as_agent=auth.actor)
    parser = _build_arg_parser()
    command_parser = next(a for a in parser._actions
                          if isinstance(a, argparse._SubParsersAction)).choices[cmd]
    fields = {a.dest: a for a in command_parser._actions if a.dest != 'help'}
    if set(argd) - (set(fields) | {'as_agent'}):
        raise RequestRejected('args_invalid', 'unknown command arguments')
    if 'fix_source_ids' in argd:
        raise RequestRejected('command_forbidden', 'source mutation unavailable on serve')
    if argd.get('as_agent') not in (None, auth.actor):
        raise RequestRejected('identity_conflict', 'request actor conflicts with host binding')
    ns = argparse.Namespace(cmd=cmd)
    for key, action in fields.items():
        value = argd.get(key, action.default)
        if value is None:
            if action.required:
                raise RequestRejected('args_invalid', 'required argument missing: ' + key)
        else:
            kind = bool if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)) else (action.type or str)
            valid = ((type(value) is bool) if kind is bool else
                     (type(value) is int) if kind is int else
                     (type(value) in (int, float) and __import__('math').isfinite(value)) if kind is float else
                     isinstance(value, str))
            if not valid or (action.choices is not None and value not in action.choices):
                raise RequestRejected('args_invalid', 'invalid argument: ' + key)
        setattr(ns, key, value)
    ns.as_agent = auth.actor
    for key, ceiling in (('top', 200), ('depth', 16)):
        value = getattr(ns, key, None)
        if value is not None and not 1 <= value <= ceiling:
            raise RequestRejected('args_invalid', 'argument out of range: ' + key)
    if cmd in WRITE_COMMANDS:
        agent = argd.get('agent')
        if agent not in (None, 'cli', auth.actor):
            raise RequestRejected('identity_conflict', 'write agent conflicts with host binding')
        ns.agent = auth.actor
        namespace = getattr(ns, 'namespace', None) or auth.actor
        vis = getattr(ns, 'visibility', 'shared')
        if namespace != auth.actor and not (namespace == 'knowledge' and vis == 'shared' and cmd in {'store', 'observe'}):
            raise RequestRejected('permission_denied', 'write namespace unavailable to this actor')
        ns.namespace = namespace
    if cmd == 'store':
        for field in ('operation_id', 'observation_id', 'speaker', 'subject',
                      'source_refs_json', 'scope_json', 'valid_from', 'valid_until',
                      'confirmation_type'):
            setattr(ns, field, argd.get(field))
    if cmd == 'observe' and not getattr(ns, 'kind', '') and not getattr(ns, 'text', None):
        raise RequestRejected('args_invalid', 'observe requires text or an event kind')
    if cmd == 'trace':
        ns.json = True
    return ns


@diagnosed('serve', 'serve.dispatch')
def _serve_dispatch(request, builder, actor, token):
    """One authenticated request; ContextVar never changes process environment."""
    from memory.serve_protocol import authorize, RequestRejected
    try:
        auth, cmd, argd = authorize(request, actor, token)
        ns = _serve_prepare_args(cmd, argd, auth)
    except RequestRejected as exc:
        emit('serve', 'permissions', 'rejected', error=exc)
        return exc.response()
    handlers = {'recall': cmd_recall, 'lookup': cmd_lookup, 'store': cmd_store,
                'observe': cmd_observe, 'agents': cmd_agents,
                'bulletin': cmd_bulletin, 'stats': cmd_stats, 'hello': cmd_hello,
                'trace': cmd_trace}
    binding = _serve_auth.set(auth)
    builder_binding = _serve_builder.set(builder)
    try:
        if cmd == 'recall':
            started = time.perf_counter()
            _serve_sync_shelf(builder)
            _perf_mark('serve', 'sync_shelf_total', started)
        started = time.perf_counter()
        result = handlers[cmd](ns)
        _perf_mark('serve', 'cmd_' + cmd, started)
        return result if isinstance(result, dict) else {'ok': True, 'result': result}
    except Exception as exc:
        # Do not echo request secrets, arbitrary backend exception text, or paths.
        emit('serve', 'serve.dispatch', 'failed', error=exc)
        return {'ok': False, 'error_code': 'backend_error', 'error': 'serve command failed',
                'fallback_allowed': False}
    finally:
        _serve_auth.reset(binding)
        _serve_builder.reset(builder_binding)


def _serve_handle_connection(conn, builder, actor, token):
    from memory.serve_protocol import MAX_FRAME_BYTES, RequestRejected
    try:
        conn.settimeout(30.0)
        with conn.makefile('rb') as reader:
            while True:
                line = reader.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    result = RequestRejected('protocol_invalid', 'request too large').response()
                else:
                    try:
                        request = json.loads(line.decode('utf-8'))
                    except (ValueError, UnicodeError):
                        result = RequestRejected('protocol_invalid', 'invalid JSON request').response()
                    else:
                        result = _serve_dispatch(request, builder, actor, token)
                conn.sendall((json.dumps(result, ensure_ascii=False, default=str) + '\n').encode('utf-8'))
                if len(line) > MAX_FRAME_BYTES:
                    break
    except (OSError, TimeoutError):
        logger.debug('serve connection closed')
    finally:
        conn.close()


def cmd_serve(args):
    """xtmem serve —— 常驻书架服务（B方案 S2）。

    O02（2026-10-14）增补：启动时写一份**结构化端点描述**（原子写，见
    endpoint_config.write_descriptor），并把 port/db_source 一并记进 _PERF['meta']，
    供 hello/doctor 做应用层四验。旧整数 PID 文件（_serve_take_singleton）
    原样保留——兼容历史启动器，不删不改语义。
    """
    host = str(getattr(args, "host", "127.0.0.1") or "127.0.0.1")
    # ⚠️ G1 返修（R03）：无显式 `--port` 时走**唯一解析口径**（含 per-actor 覆盖），
    # 而不是各自读 _SERVE_DEFAULT_PORT/env。旧写法只有全局 env 生效 ⇒
    # 解析器认 `XTMEM_SERVE_PORT__DSH`、真实 serve 却听全局口（验收实测 5619 vs 5618）。
    _explicit_port = int(getattr(args, "port", 0) or 0)
    if _explicit_port:
        port = _explicit_port
    else:
        try:
            from memory import endpoint_config as _epc
            port = _epc.resolve_endpoint(os.environ.get('XTMEM_ACTOR', '').strip() or 'assistant').port
        except Exception:
            port = _SERVE_DEFAULT_PORT
    if host not in ("127.0.0.1", "localhost", "::1"):
        return {"ok": False, "error": "serve 只允许绑定回环地址（S2 钉边），拒绝 %s" % host}
    from memory.serve_protocol import actor_token, validate_actor, RequestRejected
    try:
        actor = validate_actor(os.environ.get('XTMEM_ACTOR', '').strip())
        # Require the host launcher to provision and inject its credential.
        # Legacy launchers cannot silently start a new anonymous service.
        if not os.environ.get('XTMEM_SERVE_TOKEN', ''):
            raise RequestRejected('auth_required', 'host launcher must inject a serve credential')
        token = actor_token(actor, DEFAULT_DB)
    except RequestRejected as exc:
        return exc.response()

    # 单例：已有活进程 → 直接退（幂等）
    # ⚠️ G4 返修（R10）：**旧整数 PID 只证明"有个进程活着"，不能证明"就是 serve"**。
    # 验收实测：pid 文件里塞本轮验收进程的 PID（与 serve 无关）、目标端口根本没服务，
    # 旧写法照样回 `ok=true, already_running=true` —— 那是在假报就绪。
    # 修法：整数锁只作**快速排他**，返回前必须做一次应用层核验（本端口有没有本角色的
    # serve 在答）。答不上来 ⇒ 不能宣称 already_running（如实报 not_serving_on_port，
    # 让调用方自己决定），且**绝不杀任何进程**。
    if not _serve_take_singleton():
        live = _serve_live_probe(actor, host, port)
        if live:
            return {"ok": True, "already_running": True, "port": port,
                    "verified": True, "live": live}
        return {"ok": False, "error_code": "port_not_serving",
                "error": "pid 文件显示有进程占位，但 %s:%d 无本角色 serve 应答"
                         "（旧整数 PID 不能证明端点身份；不误杀、不代修复）" % (host, port),
                "port": port, "verified": False}

    import socket
    import threading
    try:
        builder = get_builder()  # 启动即水化一次（含 WAL replay），首命令即秒级
    except Exception as e:
        _serve_release_singleton()
        return {"ok": False, "error": "serve 启动水化失败: %s" % e}

    # 8766 埋点装壳（2026-09-13 诊断单）：运行时包裹，engine 文件零改动；失败静默不挡 listen。
    _PERF["meta"]["started"] = time.time()
    from memory.serve_protocol import PROTOCOL_VERSION
    _actual_db = Path(builder._vector_store.db_path).resolve()
    _PERF['meta'].update(actor=actor, db_id=hashlib.sha256(str(_actual_db).encode('utf-8')).hexdigest(),
                         protocol_version=PROTOCOL_VERSION,
                         startup_cli_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                         ready=False)
    _perf_wrap_method(builder, "_hydrate_retriever", "hydrate_full_rebuild")
    _r = getattr(builder, "_retriever", None)
    if _r is not None:
        _perf_wrap_method(_r, "_embed", "embed")
        _perf_wrap_method(_r, "append", "shelf_append")
        _perf_wrap_method(_r, "patch", "shelf_patch")
        _perf_wrap_method(_r, "remove", "shelf_remove")
        _perf_wrap_method(_r, "_fts_bm25", "fts_leg")
        _perf_wrap_method(_r, "_mmr_rerank", "mmr")
        _perf_wrap_method(_r, "_search_locked", "search_hop",
                          stage_of=lambda kw: "search_hop_" + str(kw.get("tier_filter") or "all"))

    # 初始化书架水位
    try:
        conn = _get_conn()
        try:
            _shelf_wm["key"] = _shelf_watermark(conn)
            _shelf_wm["at"] = time.time()
        finally:
            conn.close()
    except Exception:
        pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError:
        _serve_release_singleton()
        return {"ok": False, "error": "serve 端口 %s:%d 被占（幂等退出）" % (host, port)}
    srv.listen(8)
    _PERF['meta']['ready'] = True
    srv.settimeout(1.0)
    stop = threading.Event()

    # ── O02（2026-10-14）：记全端点描述并落一份结构化描述文件 ──────────
    # 顺序很关键：**在 listen 成功之后**才写 ready/描述——TCP 都还没起来就写
    # 「ready=true」等于制造「假 already_ready」（O02-A04 明令不许）。
    # port 取 srv.getsockname()：`--port 0` 时内核分配的是临时端口，
    # 用入参 port 会把描述写成 0，客户端四验必然连错（真实坑，非假想）。
    try:
        from memory import endpoint_config as _epc
        _bound_port = int(srv.getsockname()[1])
        _resolved = _epc.resolve_db_path()
        _PERF['meta'].update(
            port=_bound_port,
            # host 同样要进 meta：描述文件里有 host，交叉核验就要有可比的对端事实
            # （G4/N07）。缺了它 host 这一格只能"单边可信"。
            host=host,
            db_source=_resolved['source'],
            process_start=_epc.process_start_time(os.getpid()),
        )
        _cfg = _epc.resolve_endpoint(actor)
        _desc = _epc.describe(
            _cfg,
            cli_path=Path(__file__),
            process_record={'startup_cli_sha256': _PERF['meta'].get('startup_cli_sha256')},
            ready=True,
            ready_reason="",
        )
        _desc.update({
            "pid": os.getpid(),
            # ⚠️ port 必须用**实际绑定端口**覆盖 resolve_endpoint 的解析值：
            #    `--port 0` 时内核分配临时端口，两者的差是必然的；写解析值等于
            #    把描述写成客户端连不上的端口（实测就踩到：描述写 8765，实际听别的口）。
            "port": _bound_port,
            "port_source": "bound_socket",
            # ⚠️ 这里【不】写 pid_state/pid_reason：描述文件的语义是"进程自述的
            #    客观事实"（我是谁、我听哪个口、我连哪个库、我何时启动），
            #    而「是否活着/是否被复用」是**读者按进程表现算的结论**。
            #    把结论写进事实里，读者就只能拿到写入时刻的快照，且自述时
            #    读到的还是盘上那份旧记录（实测：明明在听端口却写成 stale）。
            #    ⇒ 结论留给 classify_pid_state() 现算。
            "process_start": _PERF['meta'].get('process_start'),
            "started_at": _PERF['meta'].get('started'),
            "db_id": _PERF['meta'].get('db_id'),
            "startup_cli_sha256": _PERF['meta'].get('startup_cli_sha256'),
            "protocol_version": _PERF['meta'].get('protocol_version'),
        })
        _wrote = _epc.write_descriptor(_cfg.descriptor_path, {
            k: v for k, v in _desc.items()
            # ⚠️ descriptor_kind / legacy_pid_kind 是 describe() **读盘时**的产物：
            #    写盘这一刻盘上还是上次的记录，带上去等于把"读到的旧状态"当成
            #    "本次自述"永久固化（实测：描述里写着 descriptor_kind=missing，
            #    而文件明明刚写出来）。自述语境下这两格无意义，一律剔除。
            if k not in ("descriptor_kind", "legacy_pid_kind", "pid_reason", "pid_state")
        })
        if not _wrote.get("ok"):
            # 描述写不进去不挡服务（自兜底总则：探测永不阻塞主链路），
            # 但 doctor 会因为拿不到结构化描述而报 degraded——不静默。
            logger.warning("serve: 端点描述写入失败 %s", _wrote.get("error"))
    except Exception as e:
        logger.warning("serve: 端点描述构建失败 %s", type(e).__name__)

    def _handle(conn):
        _serve_handle_connection(conn, builder, actor, token)

    def _accept_loop():
        while not stop.is_set():
            try:
                c, _addr = srv.accept()
                threading.Thread(target=_handle, args=(c,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    # 8766 修复单件②（2026-09-13）：Ollama 自暖心跳（防 5min keep_alive 过期冷加载）
    threading.Thread(target=_serve_warm_loop, args=(builder, stop), daemon=True,
                     name="serve-warm").start()
    logger.info("xtmem serve listening on %s:%d (pid=%d)", host, _PERF['meta'].get('port') or port,
                os.getpid())
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        _serve_release_singleton()
        # O02：停机同时撤掉结构化描述——留着会让 doctor 报「描述说它还活着，
        # 但 PID 已死」的陈旧态；描述文件的语义是"当前活体自述"，不是历史账。
        try:
            from memory import endpoint_config as _epc
            _cfg_done = _epc.resolve_endpoint(actor)
            _p = Path(_cfg_done.descriptor_path)
            if _p.exists():
                _p.unlink()
        except Exception:
            pass
        try:
            srv.close()
        except Exception:
            pass
    return {"ok": True, "stopped": True, "port": _PERF['meta'].get('port') or port}



def _build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器（main 与 serve 共用——serve 需按 cmd 取默认值）。"""
    p = argparse.ArgumentParser(prog="xtmem", description="XTMEM 外部端 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    _tk = sub.add_parser("rest-token"); _tk.add_argument("action", choices=["issue"]); _tk.add_argument("--role", default=None); _tk.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("migrate"); sp.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    sp = sub.add_parser("store"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default=""); sp.add_argument("--visibility", default="shared"); sp.add_argument("--derived-from", default=""); sp.add_argument("--source-ids", default=""); sp.add_argument("--tier", default="refined"); sp.add_argument("--track", default=""); sp.add_argument("--operation-id", dest="operation_id", default=None); sp.add_argument("--observation-id", dest="observation_id", default=None); sp.add_argument("--speaker", default=None); sp.add_argument("--subject", default=None); sp.add_argument("--source-refs-json", dest="source_refs_json", default=""); sp.add_argument("--scope-json", dest="scope_json", default=""); sp.add_argument("--valid-from", dest="valid_from", type=float, default=None); sp.add_argument("--valid-until", dest="valid_until", type=float, default=None); sp.add_argument("--confirmation-type", dest="confirmation_type", default=None)
    sp = sub.add_parser("observe"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", default=None); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default=""); sp.add_argument("--kind", default=""); sp.add_argument("--meta-json", dest="meta_json", default="")
    sp = sub.add_parser("recall"); sp.add_argument("--query", required=True); sp.add_argument("--top", type=int, default=6); sp.add_argument("--namespace", default=None); sp.add_argument("--project", default=""); sp.add_argument("--cross", action="store_true"); sp.add_argument("--as", dest="as_agent", default=None); sp.add_argument("--entity", default=None); sp.add_argument("--mode", choices=["symbolic", "exact", "associated"], default=None); sp.add_argument("--rerank", type=float, default=None); sp.add_argument("--time-decay", choices=["on", "off"], default=None); sp.add_argument("--role-valve", type=float, default=None)
    sp = sub.add_parser("lookup"); sp.add_argument("--query", default=""); sp.add_argument("--top", type=int, default=50); sp.add_argument("--cross", action="store_true"); sp.add_argument("--namespace", default=None); sp.add_argument("--as", dest="as_agent", default=None); sp.add_argument("--id", default=None, help="按 ID 精确查询（走索引，忽略 query/cross/namespace）")
    sp = sub.add_parser("whisper"); sp.add_argument("--agent", default="assistant"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.6); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default="")
    sp = sub.add_parser("reveal"); sp.add_argument("--id", required=True); sp.add_argument("--to", default=None); sp.add_argument("--all", action="store_true"); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("promote"); sp.add_argument("--id", required=True); sp.add_argument("--reason", required=True); sp.add_argument("--as", dest="as_agent", default=None)
    _msg = sub.add_parser("msg"); _msg.add_argument("action", choices=["send", "inbox", "recent"]); _msg.add_argument("--to", default=None); _msg.add_argument("--from", dest="from_agent", default="cli"); _msg.add_argument("--text", default=None); _msg.add_argument("--as", dest="as_agent", default=None); _msg.add_argument("--top", type=int, default=20); _msg.add_argument("--room", default=""); _msg.add_argument("--topic", default=""); _msg.add_argument("--kind", default=""); _msg.add_argument("--state", default=""); _msg.add_argument("--reply-to", dest="reply_to", default=""); _msg.add_argument("--delegation-id", dest="delegation_id", default=""); _msg.add_argument("--wake-prompt", dest="wake_prompt", default=""); _msg.add_argument("--persist", action="store_true"); _msg.add_argument("--no-inject", dest="no_inject", action="store_true"); _msg.add_argument("--visibility", default=""); _msg.add_argument("--shared-with", dest="shared_with", default=""); _msg.add_argument("--chunk-id", dest="chunk_id", default=""); _msg.add_argument("--deadline-s", dest="deadline_s", default=None)
    _rm = sub.add_parser("rooms"); _rm.add_argument("action", choices=["create", "list", "get", "set"]); _rm.add_argument("--id", default=None); _rm.add_argument("--name", default=None); _rm.add_argument("--members", default=None); _rm.add_argument("--mode", default=None); _rm.add_argument("--invite-prompt", dest="invite_prompt", default=None)
    _rel = sub.add_parser("relations"); _rel.add_argument("action", choices=["set", "status", "summary"]); _rel.add_argument("--agent", default=None); _rel.add_argument("--agent-a", dest="agent_a", default=None); _rel.add_argument("--agent-b", dest="agent_b", default=None); _rel.add_argument("--sentiment", type=float, default=None); _rel.add_argument("--trust", type=float, default=None); _rel.add_argument("--debt", default=None); _rel.add_argument("--note", default=None); _rel.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("reflect"); sp.add_argument("--agent", default=None); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.4)
    sp = sub.add_parser("forget"); sp.add_argument("--id", default=None); sp.add_argument("--query", default=None); sp.add_argument("--as", dest="as_agent", default=None); sp.add_argument("--confirm", action="store_true", help="确认执行删除（缺省=只列出将命中的条目，不落任何写；见 cmd_forget 铁律）")
    sp = sub.add_parser("absorb"); sp.add_argument("--db", required=True); sp.add_argument("--namespace", default="hermes-webui"); sp.add_argument("--host-origin", default=None); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("export"); sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("import"); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("agents.md"); sp.add_argument("--out", default="agents.md")
    sp = sub.add_parser("agents")
    sp = sub.add_parser('diagnostics'); sp.add_argument('--top', type=int, default=20)
    sp = sub.add_parser("reembed"); sp.add_argument("--limit", type=int, default=0); sp.add_argument("--timeout", type=float, default=120)
    sp = sub.add_parser("sync"); sp.add_argument("--side", default="manual")
    sp = sub.add_parser("maintain"); sp.add_argument("--retention-days", type=int, default=None)
    _pred = sub.add_parser("prediction"); _pred.add_argument("action", choices=["hit", "miss"]); _pred.add_argument("--id", required=True); _pred.add_argument("--as", dest="as_agent", default=None)
    _se = sub.add_parser("session-end"); _se.add_argument("--messages", type=int, default=5); _se.add_argument("--agent", default=None); _se.add_argument("--as", dest="as_agent", default=None)
    _fb = sub.add_parser("feedback"); _fb.add_argument("kind", choices=["corrected", "confirmed"]); _fb.add_argument("--target", required=True); _fb.add_argument("--via", default="user-verbatim"); _fb.add_argument("--agent", default=None); _fb.add_argument("--as", dest="as_agent", default=None)
    _mk = sub.add_parser("mark"); _mk.add_argument("--id", required=True); _mk.add_argument("--as", dest="as_agent", default=None)
    # A16（2026-09-16，Z指导 裁定）：exposed 批量记账入口。dsh 注入层把本轮真进注入文本的
    # 条目扫出来后【一次 spawn 落全批】——get_builder() 是 prefetch_only 轻配置但每次 spawn
    # 仍秒级，逐条 spawn（一轮 5~12 次）会把每轮延迟拉爆。
    _ex = sub.add_parser("expose"); _ex.add_argument("--ids", default="", help="逗号分隔 chunk id 批量（本轮全部暴露条目，内部去重）"); _ex.add_argument("--id", default="", help="单条兼容位（等价于 --ids 传一条）"); _ex.add_argument("--as", dest="as_agent", default=None)
    # C-0 小件④（2026-09-16，用户批 / Z指导 补充）：可选事件写入，默认关。
    # ⚠️ 默认关是硬需求——行为必须与改前逐字节一致（事件表零新增）。
    _ex.add_argument("--record-event", dest="record_event", action="store_true",
                     help="额外写一行 xtmem_events(kind=exposed)。默认【不】写；写入复用 _record_cli_event_batch，受 stats.record_events 门控")
    _kb = sub.add_parser("bulletin"); _kb.add_argument("--as", dest="as_agent", default=None)
    _sg = sub.add_parser("sign"); _sg.add_argument("--text", required=True); _sg.add_argument("--agent", default="cli"); _sg.add_argument("--namespace", default=None); _sg.add_argument("--as", dest="as_agent", default=None)
    _g = sub.add_parser("graph"); _g.add_argument("action", choices=["rebuild", "stats"])
    _tr = sub.add_parser("trace"); _tr.add_argument("mem_id", help="格式 mem-xxx（或完整 id）"); _tr.add_argument("--depth", type=int, default=3); _tr.add_argument("--json", action="store_true"); _tr.add_argument("--as", dest="as_agent", default=None, help="本端身份（XTMEM_ACTOR 在场时以宿主为准；决定能看哪些 private/shared-with，缺省只看 shared）"); _tr.add_argument("--fix-source-ids", metavar="IDS", default=None, help="B-0 漏记小修：把 IDS (逗号分隔) 写入该条的 source_ids（独立补写，非 trace 本体）")
    _sv = sub.add_parser("serve"); _sv.add_argument("--host", default="127.0.0.1"); _sv.add_argument("--port", type=int, default=0, help="默认 " + str(_SERVE_DEFAULT_PORT) + "（XTMEM_SERVE_PORT 覆盖）")
    # O02（2026-10-14）：只读端点诊断。逻辑契约对应 Spec 的 GET /v1/doctor。
    # ⚠️ 有【不】自动修复语义：不建库/不迁移/不签凭据/不起服务/不杀进程。
    _doc = sub.add_parser("doctor", help="只读诊断端点/角色/主库/协议/schema/活体（普通角色脱敏）")
    _doc.add_argument("--db", dest="doctor_db", default=None, help="显式库路径覆盖（测试重定向用；不产生生产副作用）")
    _doc.add_argument("--hermes-home", dest="hermes_home", default=None, help="profile 根（读 config.yaml 的 memory_advanced 与 xtmem.yaml）")
    _doc.add_argument("--no-probe", dest="no_probe", action="store_true", help="跳过活体 TCP 探测（离线自检；会明确报 unsupported，不报 healthy）")
    _doc.add_argument("--json", action="store_true", help="以 JSON 输出（缺省亦为 JSON，此开关供显式声明）")
    _doc.add_argument("--as", dest="as_agent", default=None, help="本端身份（XTMEM_ACTOR 在场时以宿主为准）")

    return p


def _serve_default_namespace(cmd: str) -> argparse.Namespace:
    """S2：给 serve 请求补命令默认值。用同款 parser parse 一个带占位必填的合成 argv
    （argparse 不支持取 defaults 不校验 required——用临时必填占位，解析后删掉）。"""
    _need_req = {
        "store": ["--text"],
        "recall": ["--query"],
        "observe": [],
        "whisper": ["--text"],
        "reflect": ["--text"],
        "reveal": ["--id"],
        "promote": ["--id", "--reason"],
        "sign": ["--text"],
        "absorb": ["--db"],
        "forget": [],
        "lookup": [],
        "graph": ["rebuild"],
        "rooms": ["create"],
        "msg": ["send"],
        "relations": ["status"],
        "prediction": ["hit"],
        "mark": ["--id"],
        "rest-token": ["issue"],
    }
    _req_args = _need_req.get(cmd, [])
    _argv = [cmd]
    for _ra in _req_args:
        if _ra.startswith("--"):
            _argv += [_ra, "__serve_placeholder__"]
        else:
            _argv.append(_ra)
    try:
        _ns = _build_arg_parser().parse_args(_argv)
    except SystemExit:
        _ns = argparse.Namespace(cmd=cmd)
    # 清掉占位
    for _ra in _req_args:
        if _ra.startswith("--"):
            _key = _ra[2:].replace("-", "_")
            if getattr(_ns, _key, None) == "__serve_placeholder__":
                setattr(_ns, _key, None)
    return _ns


def cmd_diagnostics(args):
    """只读取有界、安全的阶段记录；不读记忆正文或凭据。"""
    from memory.diagnostics import read_recent, snapshot
    return {'ok': True, 'process': snapshot(), 'persisted': read_recent(args.top)}


def cmd_doctor(args):
    """O02：只读端点诊断（CLI 形态，逻辑契约 = Spec GET /v1/doctor）。

    身份来源与其它命令同一条链：_resolve_actor（XTMEM_ACTOR 优先，--as 不可覆盖）；
    视图由 _resolve_admin() 决定——普通角色拿脱敏摘要，管理能力才看得到本地路径。

    ⚠️ 本命令**不碰 get_builder()/_get_conn()**：那两条都会跑 _ensure_schema()
    （建表/迁移/写事件）。doctor 只经 memory.doctor 的只读连接看库
    （O02-A09 明令：不能借 CLI 的自动迁移入口"只看一下"）。
    """
    from memory import doctor as _doctor
    actor = _resolve_actor(args)
    if not actor:
        # 无身份不得借 doctor 拿他域/管理诊断（I12 同款边界）
        return {"ok": False, "overall": _doctor.OVERALL_AUTH_DENIED,
                "exit_code": _doctor.EXIT_AUTH_DENIED, "actor": None,
                "checks": {"identity": {"expected": {"actor": "valid host actor"},
                                        "observed": {"actor": None}, "status": "denied",
                                        "reason": "no_trusted_identity_supplied"}},
                "error": "doctor 需要身份（XTMEM_ACTOR 或 --as）；无身份不提供诊断"}
    try:
        admin = bool(_resolve_admin())
    except Exception:
        admin = False
    # 凭据：优先宿主注入的 serve token；否则按端点解析出的凭据目录取（只读，不 create）
    token = os.environ.get("XTMEM_SERVE_TOKEN", "").strip() or None
    if token is None:
        try:
            from memory.serve_protocol import actor_token
            from memory import endpoint_config as _epc
            _cfg0 = _epc.resolve_endpoint(actor)
            token = actor_token(actor, _cfg0.db_path)  # create 缺省 False ⇒ 绝不签发
        except Exception:
            token = None
    result = _doctor.run_doctor(
        actor,
        admin=admin,
        explicit_db=getattr(args, "doctor_db", None),
        hermes_home=getattr(args, "hermes_home", None),
        token=token,
        allow_probe=not bool(getattr(args, "no_probe", False)),
        cli_path=Path(__file__),
    )
    return result


@diagnosed('cli', 'capture')
def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    # R-03：非法 XTMEM_ADMIN 在进入任何命令/写路径之前就拒绝（不 fallback、不落库）
    _resolve_admin()
    fn = globals().get(("cmd_" + args.cmd).replace("-", "_").replace(".", "_"))
    if not fn:
        return {"ok": False, "error": f"unknown command {args.cmd}"}
    return fn(args)


def console_entry(argv=None):
    """两种命令行入口共用输出；返回整数，避免 setuptools 把字典当异常退出。"""
    global _CLI_TEXT_EMITTED
    _CLI_TEXT_EMITTED = False
    try:
        result = main(argv)
        if not _CLI_TEXT_EMITTED:
            print(json.dumps(result, ensure_ascii=False, default=str))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(console_entry())
