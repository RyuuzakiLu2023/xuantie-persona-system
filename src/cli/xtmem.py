# -*- coding: utf-8 -*-
"""xtmem.py — XTMEM 外部端 CLI（零依赖单文件，Python 3.11）。

规格书 §4.2 / 阶段 2.1：
- sys.path 指向引擎：优先 XTMEM_HERMES_ROOT（生产源码或插件 engine），
  默认插件内 plugins/xtmem/memory。
- 共享库默认 mimox-data/.hermes/memory/vector_store.db（龙崎拍板基准），
  可用 XTMEM_DB_PATH 覆盖（测试副本）。
- 二段式写入：ContextBuilder.store() + UPDATE chunks 补 namespace/project/host_origin/track/updated_at。
- 命令：migrate / store / observe / recall / lookup / forget / absorb / export(→zcode 文件轨) / import(←zcode) / sync / agents / agents.md。
- v2.3：无 soul 命令（独立人格，零人格分发）。

用法示例：
    python xtmem.py migrate --namespace xuantie_lark
    python xtmem.py store --agent dsh --project X --text "..." --importance .8
    python xtmem.py recall --cross --query "部署偏好" --top 6
    python xtmem.py absorb --db "<ISOLATED>/hermes-webui (2)/.hermes/memory/vector_store.db" --namespace hermes-webui
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
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

DEFAULT_DB = os.environ.get(
    "XTMEM_DB_PATH",
    "xtmem.db",
)
DEFAULT_NAMESPACE = "xuantie_lark"

_builder = None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    for col, ctype, default in (
        ("namespace", "TEXT", "'xuantie_lark'"),
        ("project", "TEXT", "''"),
        ("host_origin", "TEXT", "''"),
        ("updated_at", "REAL", "NULL"),
        ("visibility", "TEXT", "'shared'"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} {ctype} DEFAULT {default}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_ns ON chunks(namespace, project)")
    # 玩法增强 E1-E3（v1.0 框架，幂等；不碰现有数据）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_vis ON chunks(visibility, namespace)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS relations (
            agent_a TEXT NOT NULL, agent_b TEXT NOT NULL,
            sentiment REAL DEFAULT 0, trust REAL DEFAULT 0.5,
            debt TEXT DEFAULT '{}', last_interaction REAL,
            history TEXT DEFAULT '[]',
            PRIMARY KEY (agent_a, agent_b)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
            content TEXT NOT NULL, ts REAL,
            read INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS xtmem_state (
            side TEXT PRIMARY KEY,
            last_files TEXT DEFAULT '{}',
            last_sync REAL
        )"""
    )
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def get_builder():
    global _builder
    if _builder is not None:
        return _builder
    if ENGINE_ROOT not in sys.path and Path(ENGINE_ROOT).name != "memory":
        sys.path.insert(0, ENGINE_ROOT)
    from memory.context_builder import ContextBuilder

    _builder = ContextBuilder(
        workspace_dir=os.path.dirname(DEFAULT_DB),
        context_window=128_000,
        config={
            "db_path": DEFAULT_DB,
            "prefetch_only": True,
            "compression_enabled": False,
            "rest_api": False,
        },
    )
    return _builder


def _apply_metadata(conn, chunk_id, namespace, project, host_origin, track=None):
    sets = ["namespace=?", "project=?", "host_origin=?", "updated_at=?"]
    params = [namespace, project, host_origin, time.time()]
    if track:
        sets.append("track=?")
        params.append(track)
    params.append(chunk_id)
    conn.execute(f"UPDATE chunks SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


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
    mid = builder.store(
        text,
        metadata={
            "importance": float(getattr(args, "importance", 0.5)),
            "tags": (args.tags.split(",") if args.tags else ["xtmem", ns]),
            "session_id": args.agent or "cli",
            "type": "conversation",
        },
    )
    if not mid:
        return {"ok": True, "skipped": True, "reason": "duplicate"}
    conn = _get_conn()
    _apply_metadata(conn, mid, ns, args.project or "", f"cli:{args.agent or 'unknown'}")
    vis = getattr(args, "visibility", "shared")
    if vis and vis != "shared":
        conn.execute("UPDATE chunks SET visibility=?, updated_at=? WHERE id=?", (vis, time.time(), mid))
        conn.commit()
    conn.close()
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
    shared-with:a,b 名单内或本 namespace 可见。无 actor（CLI 上帝视角）恒 True。"""
    vis = entry.get("visibility") or "shared"
    ns = entry.get("namespace") or ""
    if not actor or vis == "shared":
        return True
    if vis == "private":
        return ns == actor
    if vis.startswith("shared-with:"):
        allowed = [x.strip() for x in vis[len("shared-with:"):].split(",") if x.strip()]
        return ns == actor or actor in allowed
    return True


def cmd_recall(args):
    query = (args.query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    builder = get_builder()
    results = builder._retriever.search(query, top_k=int(args.top or 6))
    conn = _get_conn()
    ns_map = {
        r["id"]: (r["namespace"] or DEFAULT_NAMESPACE, r["project"] or "", r["visibility"] or "shared")
        for r in conn.execute("SELECT id, namespace, project, visibility FROM chunks WHERE state='active'")
    }
    conn.close()
    items = []
    for doc, score in results:
        if score < 0.01:
            continue
        entry = ns_map.get(doc.id)
        actor = getattr(args, "as_agent", None)
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
        })
    return {"ok": True, "count": len(items), "items": items}


def cmd_lookup(args):
    query = (args.query or "").strip()
    conn = _get_conn()
    where = "state='active'"
    params = []
    if query:
        where += " AND (content LIKE ? OR content LIKE ?)"
        params = [f"%{query}%", f"%{query.lower()}%"]
    if getattr(args, "namespace", None):
        where += " AND namespace = ?"
        params.append(args.namespace)
    sql = (f"SELECT id, content, namespace, project, host_origin, track, importance, timestamp, visibility "
           f"FROM chunks WHERE {where} ORDER BY importance DESC, timestamp DESC LIMIT ?")
    params.append(int(args.top or 50))
    rows = conn.execute(sql, params).fetchall()
    actor = getattr(args, "as_agent", None)
    if actor:
        rows = [r for r in rows if _visibility_ok({"namespace": r["namespace"] or DEFAULT_NAMESPACE, "visibility": r["visibility"] or "shared"}, actor)]
    conn.close()
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
    wal = os.path.join(os.path.dirname(DEFAULT_DB), "wal", "current.wal")
    if not os.path.exists(wal):
        return False
    try:
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
    """E1：把 private 记忆选择性揭露（--to 某角色 / --all 全公开）。"""
    conn = _get_conn()
    if args.all:
        vis = "shared"
    elif args.to:
        vis = "shared-with:" + args.to
    else:
        row = conn.execute("SELECT visibility FROM chunks WHERE id=?", (args.id,)).fetchone()
        conn.close()
        return {"ok": False, "error": "--to <agent> or --all required",
                "current": row["visibility"] if row else None}
    conn.execute("UPDATE chunks SET visibility=?, updated_at=? WHERE id=?",
                 (vis, time.time(), args.id))
    conn.commit()
    conn.close()
    return {"ok": True, "id": args.id, "visibility": vis}


def cmd_msg(args):
    """E3：角色间留言板（send / inbox / recent）。"""
    conn = _get_conn()
    if args.action == "send":
        if not (args.to and args.text):
            conn.close()
            return {"ok": False, "error": "--to and --text required"}
        mid = "msg-" + hashlib.sha1(
            (args.from_agent + ":" + args.to + ":" + args.text + ":" + str(time.time())).encode("utf-8")
        ).hexdigest()[:8]
        conn.execute(
            "INSERT OR REPLACE INTO messages (id, from_agent, to_agent, content, ts, read)"
            " VALUES (?,?,?,?,?,0)",
            (mid, args.from_agent, args.to, args.text, time.time()),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "id": mid, "from": args.from_agent, "to": args.to}
    who = args.as_agent or args.from_agent or "cli"
    if args.action == "inbox":
        rows = list(conn.execute(
            "SELECT id, from_agent, content, ts FROM messages"
            " WHERE to_agent=? AND read=0 ORDER BY ts DESC LIMIT ?",
            (who, args.top),
        ))
        conn.execute("UPDATE messages SET read=1 WHERE to_agent=? AND read=0", (who,))
        conn.commit()
    else:  # recent
        rows = list(conn.execute(
            "SELECT id, from_agent, to_agent, content, ts, read FROM messages"
            " ORDER BY ts DESC LIMIT ?",
            (args.top,),
        ))
    conn.close()
    return {"ok": True, "count": len(rows), "items": [dict(r) for r in rows]}


def cmd_relations(args):
    """E2：agent-agent 关系网（set / status / summary）。"""
    conn = _get_conn()
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
        who = args.agent
        if who:
            rows = list(conn.execute(
                "SELECT * FROM relations WHERE agent_a=? OR agent_b=?", (who, who)))
        else:
            rows = list(conn.execute("SELECT * FROM relations"))
        conn.close()
        return {"ok": True, "count": len(rows), "items": [dict(r) for r in rows]}
    # summary
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
    conn = _get_conn()
    if args.id:
        mid = args.id
        _purge_wal_entry(mid)
        cur = conn.execute("UPDATE chunks SET state='archived', superseded_by='forget' WHERE id=?", (mid,))
    else:
        cur = conn.execute(
            "UPDATE chunks SET state='archived', superseded_by='forget' WHERE content LIKE ?",
            (f"%{args.query}%",),
        )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "removed": n}


def _copy_chunk_row(src_conn, dst_conn, row, namespace, host_origin):
    """把源库一行 chunks 复制到共享库（content_hash 去重，含 FTS）。"""
    existing = dst_conn.execute(
        "SELECT 1 FROM chunks WHERE content_hash=?", (row["content_hash"],)
    ).fetchone()
    if existing:
        return 0
    if not row["content_hash"]:
        row["content_hash"] = hashlib.sha256((row["content"] or "").encode("utf-8", "ignore")).hexdigest()
    project = row["project"] if "project" in row.keys() else ""
    rows = dst_conn.execute(
        """INSERT OR IGNORE INTO chunks
           (id, content, timestamp, importance, tags, source, token_count, content_hash,
            track, state, superseded_by, namespace, project, host_origin, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["id"], row["content"], row["timestamp"], row["importance"],
            row["tags"] or "", row["source"] or "", row["token_count"] or 0, row["content_hash"],
            row["track"] or "default", "active", "", namespace, project,
            f"absorb:{host_origin}", time.time(),
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
    src = args.db
    if not os.path.exists(src):
        return {"ok": False, "error": f"source db not found: {src}"}
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    src_conn.row_factory = sqlite3.Row
    dst_conn = _get_conn()
    rows = src_conn.execute("SELECT * FROM chunks WHERE state='active'").fetchall()
    added = 0
    skipped = 0
    for row in rows:
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
    return {"ok": True, "source": src, "source_total": len(rows), "added": added, "duplicates": skipped}


ZCODE_MEMORIES_ROOT = os.environ.get(
    "XTMEM_ZCODE_MEMORIES",
    str(Path.home() / ".zcode" / "cli" / "memories" / "projects"),
)


def _hash16(name: str) -> str:
    """hash16 公式（用户侧已破解）：sha256(path.lower())[:16]"""
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
            out.append({"dir": d, "name": parts[0], "hash16": parts[1], "valid": _hash16(parts[0]) == parts[1]})
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
    rows = conn.execute(
        "SELECT * FROM chunks WHERE state='active' AND (namespace='zcode' OR host_origin LIKE 'zcode:%')"
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
    """zcode 文件轨 → DB（mem- 分区增量；content_hash 去重=自痕迹跳过；防循环三件套之一）。"""
    projects = _zcode_projects()
    conn = _get_conn()
    from memory.vector_store import _tokenize_for_fts5
    added = 0
    duplicates = 0
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
            content_hash = hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()
            if conn.execute("SELECT 1 FROM chunks WHERE content_hash=?", (content_hash,)).fetchone():
                duplicates += 1
                continue
            mid = meta.get("id") or f"mem-zc-{content_hash[:8]}"
            ts = float(meta.get("timestamp") or time.time())
            imp = float(meta.get("importance") or 0.5)
            cur = conn.execute(
                "INSERT OR IGNORE INTO chunks "
                "(id, content, timestamp, importance, tags, source, token_count, content_hash, "
                " track, state, superseded_by, namespace, project, host_origin, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, content, ts, imp, meta.get("tags") or "", "zcode:import", 0, content_hash,
                 meta.get("track") or "default", "active", "", meta.get("namespace") or "zcode",
                 proj["name"], "zcode:import", time.time()),
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
    return {"ok": True, "added": added, "duplicates": duplicates, "projects": [p["name"] for p in projects]}


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

    缓存键与引擎一致：text_hash = sha256(content[:2000])；"缺失"按该键判定，
    每次最多 --limit 条；--limit 0=全部（分批自调）。返回 remaining=处理后缺失数。
    """
    from memory.embedding_provider import OllamaEmbedding
    from memory.vector_store import VectorStore

    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, content FROM chunks c WHERE state='active'"
    ).fetchall()
    cached = {r[0] for r in conn.execute("SELECT text_hash FROM embedding_cache")}
    pending = []
    for r in rows:
        text = str(r["content"] or "")[:2000]
        th = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="xtmem", description="XTMEM 外部端 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("migrate"); sp.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    sp = sub.add_parser("store"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default=""); sp.add_argument("--visibility", default="shared")
    sp = sub.add_parser("observe"); sp.add_argument("--agent", default="cli"); sp.add_argument("--project", default=""); sp.add_argument("--text", default=None); sp.add_argument("--importance", type=float, default=0.5); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default="")
    sp = sub.add_parser("store").add_argument("--visibility", default="shared") if False else None
    sp = sub.add_parser("recall"); sp.add_argument("--query", required=True); sp.add_argument("--top", type=int, default=6); sp.add_argument("--namespace", default=None); sp.add_argument("--project", default=""); sp.add_argument("--cross", action="store_true"); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("lookup"); sp.add_argument("--query", default=""); sp.add_argument("--top", type=int, default=50); sp.add_argument("--cross", action="store_true"); sp.add_argument("--namespace", default=None); sp.add_argument("--as", dest="as_agent", default=None)
    sp = sub.add_parser("whisper"); sp.add_argument("--agent", default="xuantie_lark"); sp.add_argument("--project", default=""); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.6); sp.add_argument("--namespace", default=None); sp.add_argument("--tags", default="")
    sp = sub.add_parser("reveal"); sp.add_argument("--id", required=True); sp.add_argument("--to", default=None); sp.add_argument("--all", action="store_true")
    _msg = sub.add_parser("msg"); _msg.add_argument("action", choices=["send", "inbox", "recent"]); _msg.add_argument("--to", default=None); _msg.add_argument("--from", dest="from_agent", default="cli"); _msg.add_argument("--text", default=None); _msg.add_argument("--as", dest="as_agent", default=None); _msg.add_argument("--top", type=int, default=20)
    _rel = sub.add_parser("relations"); _rel.add_argument("action", choices=["set", "status", "summary"]); _rel.add_argument("--agent", default=None); _rel.add_argument("--agent-a", dest="agent_a", default=None); _rel.add_argument("--agent-b", dest="agent_b", default=None); _rel.add_argument("--sentiment", type=float, default=None); _rel.add_argument("--trust", type=float, default=None); _rel.add_argument("--debt", default=None); _rel.add_argument("--note", default=None)
    sp = sub.add_parser("reflect"); sp.add_argument("--agent", default=None); sp.add_argument("--text", required=True); sp.add_argument("--importance", type=float, default=0.4)
    sp = sub.add_parser("forget"); sp.add_argument("--id", default=None); sp.add_argument("--query", default=None)
    sp = sub.add_parser("absorb"); sp.add_argument("--db", required=True); sp.add_argument("--namespace", default="hermes-webui"); sp.add_argument("--host-origin", default=None)
    sp = sub.add_parser("export"); sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("import")
    sp = sub.add_parser("agents.md"); sp.add_argument("--out", default="agents.md")
    sp = sub.add_parser("agents")
    sp = sub.add_parser("reembed"); sp.add_argument("--limit", type=int, default=0); sp.add_argument("--timeout", type=float, default=120)
    sp = sub.add_parser("sync"); sp.add_argument("--side", default="manual")

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
