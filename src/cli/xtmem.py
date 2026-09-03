# -*- coding: utf-8 -*-
"""xtmem.py — XTMEM 外部端 CLI（零依赖单文件，Python 3.11）。

规格书 §4.2 / 阶段 2.1：
- sys.path 指向引擎：优先 XTMEM_HERMES_ROOT（生产源码或插件 engine），
  默认插件内 plugins/xtmem/memory。
- 共享库默认 mimox-data/.hermes/memory/vector_store.db（龙崎拍板基准），
  可用 XTMEM_DB_PATH 覆盖（测试副本）。
- 单事务写入：ContextBuilder.store() 一次 INSERT 落正文+namespace/project/host_origin/visibility/track/updated_at（P0-4）。
- 命令：migrate / store / observe / recall / lookup / forget / absorb / export(→zcode 文件轨) / import(←zcode) / sync / agents / agents.md。
- v2.3：无 soul 命令（独立人格，零人格分发）。

用法示例：
    python xtmem.py migrate --namespace xuantie_lark
    python xtmem.py store --agent dsh --project X --text "..." --importance .8
    python xtmem.py recall --cross --query "部署偏好" --top 6
    python xtmem.py absorb --db "E:/hermes-webui (2)/.hermes/memory/vector_store.db" --namespace hermes-webui
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time

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
DEFAULT_ENGINE = os.path.join(HERE, "..", "plugins", "xtmem", "memory")
ENGINE_ROOT = os.environ.get("XTMEM_HERMES_ROOT", DEFAULT_ENGINE)
# 若指向插件内 memory/（名字=memory），父目录才是 import memory 的搜索根
_PARENT = str(Path(ENGINE_ROOT).resolve().parent)
if Path(ENGINE_ROOT).name == "memory":
    sys.path.insert(0, _PARENT)
else:
    sys.path.insert(0, ENGINE_ROOT)

DEFAULT_CLI = os.path.abspath(__file__)
DEFAULT_DB = os.environ.get(
    "XTMEM_DB_PATH",
    r"<XT_ROOT>\mimox-data\.hermes\memory\vector_store.db",
)
DEFAULT_NAMESPACE = "xuantie_lark"

_builder = None


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
    conn = sqlite3.connect(DEFAULT_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn



def _resolve_actor(args) -> Optional[str]:
    """P0-1：身份固定。环境变量 XTMEM_ACTOR 优先（DSH/zcode 桥接层强制注入），
    --as 只在无环境身份时可用（本地管理员显式指定）。"""
    env_actor = os.environ.get("XTMEM_ACTOR", "").strip()
    if env_actor:
        return env_actor
    return getattr(args, "as_agent", None) or None


def _stats_enabled():
    """读 mimox-data/xtmem.yaml 的 stats.record_events（与 provider 同源）。"""
    try:
        import yaml
        path = os.environ.get("XTMEM_STATS_YAML", "<XT_ROOT>/mimox-data/xtmem.yaml")
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


def cmd_store(args):
    text = (args.text or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    builder = get_builder()
    ns = args.namespace or args.agent or DEFAULT_NAMESPACE
    vis = getattr(args, "visibility", "shared") or "shared"
    # N2（2026-08-31）：有身份时禁止写入他人域（XTMEM_ACTOR/--as = 本端身份）
    actor = _resolve_actor(args)
    if actor and ns != actor:
        return {"ok": False, "error": f"本端记忆只能写入本域 {actor}（自己记自己的）"}
    if not actor and args.agent and ns != args.agent:
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
        mid = builder.store(
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
            },
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "reason": "rejected"}
    if not mid:
        return {"ok": True, "skipped": True, "reason": "duplicate"}
    _record_cli_event("write", ns, target_chunk=mid, meta={"agent": args.agent or "dsh"})
    return {"ok": True, "id": mid, "namespace": ns, "visibility": vis}



def cmd_observe(args):
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
    shared-with:a,b 名单内或本 namespace 可见。无 actor（CLI 上帝视角）恒 True。

    R1.3（2026-08-31）：薄封装——真正判定收敛到 memory.visibility.visibility_ok。
    """
    from memory.visibility import visibility_ok
    return visibility_ok(
        entry.get("visibility") or "shared",
        entry.get("namespace") or "",
        actor or "",
    )


def cmd_recall(args):
    query = (args.query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    builder = get_builder()
    # P0.3（2026-08-31）：检索按 viewer（本端域）过滤可见性；actor 从 --as/XTMEM_ACTOR 取
    # P1.1（2026-08-31）：per-viewer utility 加权
    _actor = _resolve_actor(args)
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
            _where += " AND namespace=?"
            _params.append(args.namespace or _actor or DEFAULT_NAMESPACE)
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
            if _actor and not _visibility_ok({"namespace": r["namespace"] or DEFAULT_NAMESPACE,
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
        _record_cli_event_batch("recall", _batch_agent, [it["id"] for it in items])
        try:
            for it in items:
                builder._vector_store.increment_utility(it["id"], viewer=_batch_agent)
        except Exception:
            pass
        return {"ok": True, "count": len(items), "items": items, "mode": "exact",
                "verdict": "精确栈（LIKE 精确检索）", "basis": "SQL LIKE + importance 排序，未走 hybrid",
                "modifiers": {"mode": _mode, "rerank": None, "time_decay": None, "role_valve": _role_valve}}
    # P4.8 T4（2026-09-01）：两阶段检索路由——refined 首跳高分即返，miss 回落 raw 兜底（天然不相交去重）
    results = builder._retriever.search_two_tier(
        query, top_k=int(args.top or 6), viewer=_actor,
        time_decay=False if _tdecay == "off" else None,
        utility_lookup=lambda cid: (builder._vector_store.get_utility(cid, viewer=_actor or "") or {}).get("utility_score", 0.0),
        last_access_lookup=lambda cid: (builder._vector_store.get_utility(cid, viewer=_actor or "") or {}).get("last_access"),
    )
    conn = _get_conn()
    ns_map = {
        r["id"]: (r["namespace"] or DEFAULT_NAMESPACE, r["project"] or "", r["visibility"] or "shared", r["tier"] or "raw")
        for r in conn.execute("SELECT id, namespace, project, visibility, tier FROM chunks WHERE state='active'")
    }
    conn.close()
    items = []
    for doc, score in results:
        if score < 0.01:
            continue
        entry = ns_map.get(doc.id)
        actor = _resolve_actor(args)
        # P5.0-e 修饰器：--role-valve X = 命中块 importance ≥ X 才返回（默认 None 不筛）
        if _role_valve is not None and float(getattr(doc, "importance", 0.5) or 0.5) < _role_valve:
            continue
        if actor and not _visibility_ok({"namespace": entry[0] if entry else "", "visibility": entry[2] if entry else "shared"}, actor):
            continue
        if args.cross:
            ns_ok = project_ok = True
        else:
            ns_ok = (entry is None) or (entry[0] == (args.namespace or DEFAULT_NAMESPACE))
            project_ok = (entry is None) or (not args.project) or (entry[1] == args.project)
        if not (ns_ok and project_ok):
            continue
        items.append({
            "id": doc.id,
            "content": str(doc.content),
            "score": round(float(score), 4),
            "namespace": entry[0] if entry else "",
            "project": entry[1] if entry else "",
            "visibility": entry[2] if entry else "shared",
            "tier": entry[3] if entry else "raw",  # P4.4 ⑱：层级透明（refined/raw）
            "importance": float(doc.importance),
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
                    if _actor and not _visibility_ok({"namespace": r["namespace"] or DEFAULT_NAMESPACE,
                                                      "visibility": r["visibility"] or "shared"}, _actor):
                        continue
                    if _role_valve is not None and float(r["importance"] or 0) < _role_valve:
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
    # P4.0-d（2026-08-31）：recall 改批量一行事件（ids 放 meta），不再每命中写一行。
    _record_cli_event_batch("recall", _batch_agent, [it["id"] for it in items])
    for it in items:
        # P4.0-b（2026-08-31，P4 第 2 波）：CLI recall 回写 utility——打通 zcode/DSH 端 per-viewer 数据。
        # （已存在同段代码的 utility_lookup 是读取侧，这里补写入侧，P1.1/P1.2 才对这端生效。）
        try:
            builder._vector_store.increment_utility(it["id"], viewer=_batch_agent)
        except Exception:
            pass
    # P4.4 ⑱：诚实输出——告诉调用方这次结果怎么来的（模式/置信/依据），防谄媚。
    _mode = getattr(builder._retriever, "_last_two_tier_mode", None) or "unknown"
    _max_score = max([it["score"] for it in items], default=0.0)
    _verdict = "高置信（refined 首跳命中）" if _mode == "refined-first" else ("兜底（raw 回落）" if _mode == "raw-fallback" else ("空结果" if not items else "未知"))
    _basis = ("query + 画像/要点（refined 池）匹配" if _mode == "refined-first" else "refined 无高分，回落 raw 流水兜底")
    return {"ok": True, "count": len(items), "items": items, "mode": _mode, "verdict": _verdict, "basis": _basis,
            "max_score": round(_max_score, 4),
            "modifiers": {"mode": _mode, "rerank": _rerank_w, "reranked": _reranked,
                          "time_decay": _tdecay, "role_valve": _role_valve}}


def cmd_lookup(args):
    query = (args.query or "").strip()
    conn = _get_conn()
    where = "state='active'"
    params = []
    if query:
        where += " AND (content LIKE ? OR content LIKE ?)"
        params = [f"%{query}%", f"%{query.lower()}%"]
    # R2.4（2026-08-31）：lookup 默认只回本域；--cross 才全库（与 recall 对齐）
    if not getattr(args, "cross", False):
        ns = args.namespace or _resolve_actor(args) or DEFAULT_NAMESPACE
        where += " AND namespace = ?"
        params.append(ns)
    sql = (f"SELECT id, content, namespace, project, host_origin, track, importance, timestamp, visibility "
           f"FROM chunks WHERE {where} ORDER BY importance DESC, timestamp DESC LIMIT ?")
    params.append(int(args.top or 50))
    rows = conn.execute(sql, params).fetchall()
    actor = _resolve_actor(args)
    if actor:
        rows = [r for r in rows if _visibility_ok({"namespace": r["namespace"] or DEFAULT_NAMESPACE, "visibility": r["visibility"] or "shared"}, actor)]
    conn.close()
    # P4.0-b（2026-08-31，P4 第 2 波）：CLI lookup 回写 utility，同 recall（get_builder 仅取 vector_store）。
    # P4.0-d（2026-08-31）：lookup 改批量一行事件。
    try:
        _builder = get_builder()
    except Exception:
        _builder = None
    _batch_agent = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
    _record_cli_event_batch("recall", _batch_agent, [r["id"] for r in rows])
    for r in rows:
        if _builder is not None:
            try:
                _builder._vector_store.increment_utility(r["id"], viewer=_batch_agent)
            except Exception:
                pass
    return {
        "ok": True,
        "count": len(rows),
        "items": [{
            "id": r["id"], "content": str(r["content"]),
            "namespace": r["namespace"] or DEFAULT_NAMESPACE,
            "project": r["project"] or "", "host_origin": r["host_origin"] or "",
            "track": r["track"] or "default", "importance": r["importance"] or 0,
            "timestamp": r["timestamp"] or 0, "visibility": r["visibility"] or "shared",
        } for r in rows],
    }


def _purge_wal_entry(chunk_id):
    # N1（2026-08-31）：清的是引擎真实 WAL（<ws>/.hermes/memory/wal/current.wal），
    # 与 ContextBuilder 路径推导一致；旧写法 dirname(db)/wal 与引擎错位，清了个寂寞。
    wal = os.path.join(_builder_workspace(), ".hermes", "memory", "wal", "current.wal")
    if not os.path.exists(wal):
        return False
    try:
        # P1-9：purge 也是整文件重写，走引擎同一把跨进程锁（尽力型，
        # 拿不到锁降级重写——最坏残留行，无复活风险，不阻塞 forget）
        from memory.wal_engine import _locked_wal
        with _locked_wal(os.path.dirname(wal), required=False) as locked:
            if not locked:
                logger.warning("_purge_wal_entry: WAL lock unavailable, rewriting lock-free")
            lines = open(wal, encoding="utf-8").read().splitlines()
            kept = [l for l in lines if not (l.strip() and json.loads(l).get("id") == chunk_id)]
            open(wal, "w", encoding="utf-8").write("\n".join(kept))
        return True
    except Exception:
        return False


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
    admin = bool(os.environ.get("XTMEM_ADMIN"))
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
    admin = bool(os.environ.get("XTMEM_ADMIN"))
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
        mid = "msg-" + hashlib.sha1(
            (args.from_agent + ":" + args.to + ":" + args.text + ":" + str(time.time())).encode("utf-8")
        ).hexdigest()[:8]
        room_id = str(getattr(args, "room", "") or "")
        topic_id = str(getattr(args, "topic", "") or "")
        state = str(getattr(args, "state", "") or "")
        reply_to = str(getattr(args, "reply_to", "") or "")
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
        conn.execute(
            "INSERT OR REPLACE INTO messages"
            " (id, from_agent, to_agent, content, ts, read, room_id, topic_id, kind, state, reply_to, delegation_id, persist, inject, wake_prompt, visibility, shared_with, chunk_id, woke_at, deadline_s)"
            " VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (mid, args.from_agent, args.to, args.text, time.time(), room_id, topic_id, kind, state, reply_to, delegation_id, persist, inject, wake_prompt, visibility, shared_with, chunk_id, deadline_s),
        )
        conn.commit()
        conn.close()
        _record_cli_event("msg_send", args.from_agent or "cli", meta={"to": args.to, "kind": kind, "room": room_id, "id": mid})
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
        _upd = conn.execute("UPDATE messages SET read=1 WHERE to_agent=? AND read=0", (who,))
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
    admin = bool(os.environ.get("XTMEM_ADMIN"))
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
    """R1.5（2026-08-31）：forget 身份收口——有 actor 时只能操作本域（--id 精确 / --query 命中域）；
    管理员走 XTMEM_ADMIN=1；成功记账 xtmem_events kind=forget。"""
    conn = _get_conn()
    actor = _resolve_actor(args)
    admin = bool(os.environ.get("XTMEM_ADMIN"))
    if not actor and not admin:
        conn.close()
        return {"ok": False, "error": "forget 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    if args.id:
        mid = args.id
        _purge_wal_entry(mid)
        cur = conn.execute("UPDATE chunks SET state='archived', superseded_by='forget' WHERE id=?", (mid,))
        ids = [mid] if cur.rowcount else []
    elif args.query:
        q = f"%{args.query}%"
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE content LIKE ?", (q,))]
        for cid in ids:
            _purge_wal_entry(cid)
            conn.execute("UPDATE chunks SET state='archived', superseded_by='forget' WHERE id=?", (cid,))
    else:
        conn.close()
        return {"ok": False, "error": "--id or --query required"}
    # 身份域校验：非管理员不能越域 forget
    if actor and not admin:
        bad = []
        for cid in ids:
            row = conn.execute("SELECT namespace FROM chunks WHERE id=?", (cid,)).fetchone()
            ns = (row["namespace"] or DEFAULT_NAMESPACE) if row else DEFAULT_NAMESPACE
            if ns != actor:
                bad.append((cid, ns))
        if bad:
            conn.close()
            return {"ok": False, "error": f"只能操作本域记忆（{actor}）；越域命中 {len(bad)} 条，已拒绝",
                    "cross_domain": bad[:10]}
    # R1.4（2026-08-31）：让遗忘变成真遗忘——出库的正文同步从 embedding_cache 删掉，
    # 否则向量腿仍可命中已 forget 的正文（副作用：query 匹配到"已删"内容）。
    # 别指望 CASCADE：SQLite FK 默认不启用，且 chunks 从不 DELETE（forget 是 UPDATE）。
    from memory.utils import compute_embedding_cache_key
    for cid in ids:
        crow = conn.execute("SELECT content FROM chunks WHERE id=?", (cid,)).fetchone()
        if crow:
            conn.execute(
                "DELETE FROM embedding_cache WHERE text_hash=?",
                (compute_embedding_cache_key(str(crow["content"] or "")),),
            )
    # P0-5：归档/删除必须同步 FTS（否则幽灵行仍可被 BM25/FTS 命中）。
    # 注意：FTS5 的 id 列是 UNINDEXED，按 id 删在旧 external-content 表上是假删除，
    # 必须经 chunks rowid 映射删除。
    for cid in ids:
        conn.execute(
            "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
            (cid,),
        )
    conn.commit()
    n = len(ids)
    for cid in ids:
        _record_cli_event("forget", actor or "admin", target_chunk=cid, meta={"query": args.query})
    conn.close()
    # 同进程内复用 builder 时，重灌内存检索器（CLI 单次进程也兜底）
    if _builder is not None:
        try:
            _builder._hydrate_retriever()
        except Exception:
            pass
    return {"ok": True, "removed": n}



def _copy_chunk_row(src_conn, dst_conn, row, namespace, host_origin):
    """把源库一行 chunks 复制到共享库（content_hash 去重，含 FTS）。"""
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
    admin = bool(os.environ.get("XTMEM_ADMIN"))
    if not actor and not admin:
        return {"ok": False, "error": "absorb 需要身份（--as/XTMEM_ACTOR）；管理员操作设 XTMEM_ADMIN=1"}
    if actor and args.namespace and args.namespace != actor:
        return {"ok": False, "error": f"只能吸收到本域（{actor}）；指定域 {args.namespace} 不一致"}
    src = args.db
    if not os.path.exists(src):
        return {"ok": False, "error": f"source db not found: {src}"}
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    src_conn.row_factory = sqlite3.Row
    dst_conn = _get_conn()
    from memory.vector_store import VectorStore
    rows = src_conn.execute("SELECT * FROM chunks WHERE state='active'").fetchall()
    added = 0
    skipped = 0
    rejected = 0
    for row in rows:
        # P0-2：所有写入路径（含 absorb）统一安全校验；注入内容不收入编
        if VectorStore._validate_content(str(row["content"] or "")) is None:
            rejected += 1
            continue
        if dst_conn.execute("SELECT 1 FROM chunks WHERE content_hash=?", (row["content_hash"],)).fetchone():
            skipped += 1
            continue
        try:
            added += _copy_chunk_row(src_conn, dst_conn, row, args.namespace, args.host_origin or "hermes-webui")
        except Exception:
            skipped += 1
    dst_conn.commit()
    src_conn.close()
    dst_conn.close()
    _record_cli_event("absorb", actor or "admin", meta={"source": src, "added": added, "duplicates": skipped, "rejected": rejected})
    return {"ok": True, "source": src, "source_total": len(rows),
            "added": added, "duplicates": skipped, "rejected": rejected}


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
    admin = bool(os.environ.get("XTMEM_ADMIN"))
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
            mid = meta.get("id") or f"mem-zc-{content_hash[:8]}"
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
        conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, text, chunk_id, embedding, dim, model, created_at) VALUES (?,?,?,?,?,?,?)",
            (th, text, mid, blob, len(vec), "bge-m3", now),
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
    """P4.2 验证回路：prediction hit/miss 事件 + 连续落空 2 次自动 archived（验收 2）。

    miss 递增 prediction chunk tags 里 overturn:N；>=2 时置 state='archived'（P2.1：检索排除、保留引用，不删）。
    """
    action = args.action
    cid = args.id
    actor = _resolve_actor(args) or getattr(args, "namespace", None) or DEFAULT_NAMESPACE
    _record_cli_event("prediction_" + action, actor, target_chunk=cid)
    if action != "miss":
        return {"ok": True, "action": action}
    conn = _get_conn()
    row = conn.execute("SELECT tags, state FROM chunks WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "action": action, "error": "chunk not found"}
    try:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
    except Exception:
        tags = []
    # 本 miss 计 1 次；已有 overturn:X 则 +1（第 1 次 miss→overturn:1，第 2 次→overturn:2→归档）
    n = 1
    for t in list(tags):
        if str(t).startswith("overturn:"):
            n = int(str(t).split(":")[1]) + 1
            tags.remove(t)
    tags.append("overturn:%d" % n)
    if n >= 2:
        conn.execute("UPDATE chunks SET state='archived', tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), cid))
        conn.commit()
        conn.close()
        return {"ok": True, "action": action, "archived": True, "overturn": n}
    conn.execute("UPDATE chunks SET tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), cid))
    conn.commit()
    conn.close()
    return {"ok": True, "action": action, "overturn": n}


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


def main(argv=None):
    p = argparse.ArgumentParser(prog="xtmem", description="XTMEM 外部端 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("migrate"); sp.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    sp = sub.add_parser("store"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default=""); sp.add_argument("--visibility", default="shared"); sp.add_argument("--derived-from", default=""); sp.add_argument("--source-ids", default=""); sp.add_argument("--tier", default="refined"); sp.add_argument("--track", default="")
    sp = sub.add_parser("observe"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", default=None); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default="")
    sp = sub.add_parser("recall"); sp.add_argument("--query", required=True); sp.add_argument("--top", type=int, default=6); sp.add_argument("--namespace", default=None); sp.add_argument("--project", default=""); sp.add_argument("--cross", action="store_true"); sp.add_argument("--as", dest="as_agent", default=None); sp.add_argument("--entity", default=None); sp.add_argument("--mode", choices=["symbolic", "exact", "associated"], default=None); sp.add_argument("--rerank", type=float, default=None); sp.add_argument("--time-decay", choices=["on", "off"], default=None); sp.add_argument("--role-valve", type=float, default=None)
    sp = sub.add_parser("lookup"); sp.add_argument("--query", default=""); sp.add_argument("--top", type=int, default=50); sp.add_argument("--cross", action="store_true"); sp.add_argument("--namespace", default=None); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("whisper"); sp.add_argument("--agent", default="xuantie_lark"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.6); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default="")
    sp = sub.add_parser("reveal"); sp.add_argument("--id", required=True); sp.add_argument("--to", default=None); sp.add_argument("--all", action="store_true"); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("promote"); sp.add_argument("--id", required=True); sp.add_argument("--reason", required=True); sp.add_argument("--as", dest="as_agent", default=None)
    _msg = sub.add_parser("msg"); _msg.add_argument("action", choices=["send", "inbox", "recent"]); _msg.add_argument("--to", default=None); _msg.add_argument("--from", dest="from_agent", default="cli"); _msg.add_argument("--text", default=None); _msg.add_argument("--as", dest="as_agent", default=None); _msg.add_argument("--top", type=int, default=20); _msg.add_argument("--room", default=""); _msg.add_argument("--topic", default=""); _msg.add_argument("--kind", default=""); _msg.add_argument("--state", default=""); _msg.add_argument("--reply-to", dest="reply_to", default=""); _msg.add_argument("--delegation-id", dest="delegation_id", default=""); _msg.add_argument("--wake-prompt", dest="wake_prompt", default=""); _msg.add_argument("--persist", action="store_true"); _msg.add_argument("--no-inject", dest="no_inject", action="store_true"); _msg.add_argument("--visibility", default=""); _msg.add_argument("--shared-with", dest="shared_with", default=""); _msg.add_argument("--chunk-id", dest="chunk_id", default=""); _msg.add_argument("--deadline-s", dest="deadline_s", default=None)
    _rm = sub.add_parser("rooms"); _rm.add_argument("action", choices=["create", "list", "get", "set"]); _rm.add_argument("--id", default=None); _rm.add_argument("--name", default=None); _rm.add_argument("--members", default=None); _rm.add_argument("--mode", default=None); _rm.add_argument("--invite-prompt", dest="invite_prompt", default=None)
    _rel = sub.add_parser("relations"); _rel.add_argument("action", choices=["set", "status", "summary"]); _rel.add_argument("--agent", default=None); _rel.add_argument("--agent-a", dest="agent_a", default=None); _rel.add_argument("--agent-b", dest="agent_b", default=None); _rel.add_argument("--sentiment", type=float, default=None); _rel.add_argument("--trust", type=float, default=None); _rel.add_argument("--debt", default=None); _rel.add_argument("--note", default=None); _rel.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("reflect"); sp.add_argument("--agent", default=None); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.4)
    sp = sub.add_parser("forget"); sp.add_argument("--id", default=None); sp.add_argument("--query", default=None); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("absorb"); sp.add_argument("--db", required=True); sp.add_argument("--namespace", default="hermes-webui"); sp.add_argument("--host-origin", default=None); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("export"); sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("import"); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("agents.md"); sp.add_argument("--out", default="agents.md")
    sp = sub.add_parser("agents")
    sp = sub.add_parser("reembed"); sp.add_argument("--limit", type=int, default=0); sp.add_argument("--timeout", type=float, default=120)
    sp = sub.add_parser("sync"); sp.add_argument("--side", default="manual")
    sp = sub.add_parser("maintain"); sp.add_argument("--retention-days", type=int, default=None)
    _pred = sub.add_parser("prediction"); _pred.add_argument("action", choices=["hit", "miss"]); _pred.add_argument("--id", required=True); _pred.add_argument("--as", dest="as_agent", default=None)
    _mk = sub.add_parser("mark"); _mk.add_argument("--id", required=True); _mk.add_argument("--as", dest="as_agent", default=None)
    _sg = sub.add_parser("sign"); _sg.add_argument("--text", required=True); _sg.add_argument("--agent", default="cli"); _sg.add_argument("--namespace", default=None); _sg.add_argument("--as", dest="as_agent", default=None)
    _g = sub.add_parser("graph"); _g.add_argument("action", choices=["rebuild", "stats"])

    args = p.parse_args(argv)
    fn = globals().get(("cmd_" + args.cmd).replace(".", "_"))
    if not fn:
        return {"ok": False, "error": f"unknown command {args.cmd}"}
    return fn(args)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)