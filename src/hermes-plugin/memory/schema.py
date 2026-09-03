# -*- coding: utf-8 -*-
"""memory/schema.py — XTMEM 统一 schema 迁移模块（P0-4/P1-6 合并）。

一份 schema 定义，provider / CLI / 引擎都调这里，杜绝两套 schema 漂移：
- chunks 增列：namespace / project / host_origin / updated_at / visibility
- 索引：idx_chunks_ns / idx_chunks_vis
- 玩法/总线表：relations / messages / xtmem_state / xtmem_events

全部幂等（ALTER ... ADD COLUMN 仅当缺列时执行；CREATE IF NOT EXISTS）。
不写数据、不强制回填 namespace（回填是调用方策略）。
"""

import sqlite3
from typing import Optional


# P5.1-1（2026-09-02）：kind 合法取值收口单点常量表——CLI / API / 调度器统一引用这里，
# 防「类型枚举散三处」的坑（修订 6 纪律）。
KINDS = frozenset({"msg", "chat", "task", "decision", "notice"})


def ensure_xtmem_schema(conn: sqlite3.Connection,
                        default_namespace: Optional[str] = "xuantie_lark") -> None:
    """幂等迁移：chunks 增列 + 索引 + 玩法/总线表（规格书 §4.1。改这里一处即可）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    for col, col_type, default in (
        ("namespace", "TEXT", f"'{default_namespace}'"),
        ("project", "TEXT", "''"),
        ("host_origin", "TEXT", "''"),
        ("updated_at", "REAL", "NULL"),
        ("visibility", "TEXT", "'shared'"),
        # P0.1（2026-08-31）：血缘两列——source_ids（来源 chunk id 的 JSON 数组）+ derived_from（distill/summary/forecast/skill）
        ("source_ids", "TEXT", "'[]'"),
        ("derived_from", "TEXT", "''"),
        # P4.8 分层库 T1（2026-09-01）：tier 列——raw=全量流水层 / refined=蒸馏+显式要点+画像预判层；NULL=旧数据默认态（=raw，兼容老读路径）
        ("tier", "TEXT", "NULL"),
        # P4.9-e（2026-09-02）：content_time——NER TimeExtractor 抽出的正文时间基准（实体地基，P5.0 时间骨架用）
        ("content_time", "REAL", "NULL"),
    ):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE chunks ADD COLUMN {col} {col_type} DEFAULT {default}"
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_ns ON chunks(namespace, project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_vis ON chunks(visibility, namespace)")
    # P4.8 T1：分层库两阶段检索主索引 (namespace, tier, state)——第一跳只算 refined 池
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_tier ON chunks(namespace, tier, state)")

    # ── 玩法增强 E1-E3 / 总线表（v1.0 框架，幂等；不碰现有数据）──
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
            read INTEGER DEFAULT 0,
            room_id TEXT DEFAULT '',
            topic_id TEXT DEFAULT '',
            kind TEXT DEFAULT 'msg' CHECK(kind IN ('msg','chat','task','decision','notice')),
            state TEXT DEFAULT '',
            reply_to TEXT DEFAULT '',
            delegation_id TEXT DEFAULT '',
            persist INTEGER DEFAULT 0,
            inject INTEGER DEFAULT 1,
            wake_prompt TEXT DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'shared',
            shared_with TEXT DEFAULT '',
            chunk_id TEXT DEFAULT '',
            woke_at REAL,
            deadline_s INTEGER
        )"""
    )
    # P5.1-1（2026-09-02）：对【已存在】的 messages 表做幂等补列（CREATE IF NOT EXISTS 对老表跳过）。
    # 全新库走上面的 CREATE 已含列；老库走这里逐列补，避免 hot 库重建表（总则 2：schema 先副本演练）。
    _mcols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    for _mcol, _mtype, _mdef in (
        ("room_id", "TEXT", "''"),
        ("topic_id", "TEXT", "''"),
        ("kind", "TEXT", "'msg'"),
        ("state", "TEXT", "''"),
        ("reply_to", "TEXT", "''"),
        ("delegation_id", "TEXT", "''"),
        ("persist", "INTEGER", "0"),
        ("inject", "INTEGER", "1"),
        ("wake_prompt", "TEXT", "''"),
        # P5.1-2b（2026-09-02 契约缝合）：驾驶舱契约五列——visibility/shared_with（private 正文
        # 零下发是硬契约，库内仍存供注入端读）、chunk_id（入渊回跳）、woke_at/deadline_s（A2A 链路）
        ("visibility", "TEXT", "'shared'"),
        ("shared_with", "TEXT", "''"),
        ("chunk_id", "TEXT", "''"),
        ("woke_at", "REAL", "NULL"),
        ("deadline_s", "INTEGER", "NULL"),
    ):
        if _mcol not in _mcols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {_mcol} {_mtype} DEFAULT {_mdef}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            name TEXT, members TEXT, mode TEXT DEFAULT 'invite_only',
            invite_prompt TEXT, current_round INTEGER DEFAULT 0,
            pending_speakers TEXT DEFAULT '[]', ts REAL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_topic_ts ON messages(room_id, topic_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state)")
    # P5.1-2b（2026-09-02 契约缝合）：驾驶舱 Widget 网格的库侧落点——widget 不是消息域
    # （不占 messages 行、不进注入），POST /api/widget 写入、GET cockpit/state.widgets 列出、
    # POST /api/task/ack 以 widget id 拍板（handled=1）。私密 widget 的 body 库内保留、
    # 响应一律 null+redacted（与 messages private 同口径）。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS widgets (
            id TEXT PRIMARY KEY,
            from_agent TEXT NOT NULL,
            kind TEXT DEFAULT 'note',
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            visibility TEXT DEFAULT 'shared',
            actions TEXT DEFAULT '[]',
            ts REAL,
            handled INTEGER DEFAULT 0
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_widgets_ts ON widgets(ts)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS xtmem_state (
            side TEXT PRIMARY KEY,
            last_files TEXT DEFAULT '{}',
            last_sync REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS xtmem_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            agent TEXT,
            namespace TEXT,
            project TEXT,
            target_chunk TEXT,
            meta TEXT DEFAULT '{}'
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_xtmem_events_ts ON xtmem_events(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xtmem_events_agent_ts ON xtmem_events(agent, ts)"
    )

    # ── P4.9-e 实体地基（2026-09-02）：entities（canon 实体）+ entity_aliases（别名→canon）──
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entities (
            canon TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            first_seen REAL,
            last_seen REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entity_aliases (
            canon TEXT NOT NULL,
            alias TEXT NOT NULL,
            PRIMARY KEY (canon, alias)
        )"""
    )
    # P4.9-波1 A（2026-09-02，v1.12.1）：打标唯一落点——chunk↔canon 关联表（主键幂等，回填任意次不翻倍）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chunk_entities (
            chunk_id TEXT NOT NULL,
            canon TEXT NOT NULL,
            PRIMARY KEY (chunk_id, canon)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_entities_canon ON chunk_entities(canon)")
    # P5.0-a（2026-09-02）：canon 共现图边表——节点=entities.canon，边=同一 chunk 内共现（chunk_entities 推导）。
    #   只存 a<b 一行（无向图）；cnt=共现 chunk 数（幂等，可任意次重算）；IDF 加权发生在查询期（graph.py）。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tag_edges (
            a TEXT NOT NULL, b TEXT NOT NULL,
            cnt INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (a, b)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tag_edges_b ON tag_edges(b)")
    # P4.9-e：别名种子（四 agent 名 + 大小写变体 + 项目名），为后续 P5.0 canon 归一建基础
    _alias_seed = [
        ("玄铁", "xuantie_lark"), ("玄铁", "XuanTie"), ("玄铁", "玄铁"),
        ("江北", "workbuddy"), ("江北", "阿北"),
        ("小D", "dsh"), ("小D", "DSH"), ("小D", "小D"),
        ("龙崎", "龙崎"), ("龙崎", "lq"), ("龙崎", "Ryuuzaki"),
        ("Z老师", "zcode"), ("Z老师", "zhanglaoshi"), ("Z老师", "Z 老师"), ("Z老师", "Z老师"),
        ("XTMEM", "xtmem"), ("XTMEM", "XTMEM"),
        ("tuan-game", "tuan_game"),
        ("XT-Forge", "XT-Forge"),
        ("Hermes", "hermes"), ("Hermes", "赫尔墨斯"),
        ("Ollama", "ollama"), ("Ollama", "本地 Ollama"),
        ("bge-m3", "bge_m3"), ("bge-m3", "bge-m3"),
        ("Qdrant", "qdrant"),
        ("spaCy", "spacy"),
        ("jionlp", "jionlp"),
        ("Coze", "coze"),
        ("MCP", "mcp"),
        ("AGENTS.md", "AGENTS.md"),
    ]
    for _c, _a in _alias_seed:
        conn.execute("INSERT OR IGNORE INTO entity_aliases(canon, alias) VALUES (?,?)", (_c, _a))
        conn.execute("INSERT OR IGNORE INTO entities(canon, count, first_seen, last_seen) VALUES (?,0,?,?)", (_c, __import__("time").time(), __import__("time").time()))
    conn.commit()