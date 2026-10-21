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
import time
from typing import Optional


# P5.1-1（2026-09-02）：kind 合法取值收口单点常量表——CLI / API / 调度器统一引用这里，
# 防「类型枚举散三处」的坑（修订 6 纪律）。
KINDS = frozenset({"msg", "chat", "task", "decision", "notice"})


# D1-1（2026-09-09）新增列清单——唯一口径：建列（本函数）、迁移器 dry-run、守卫断言都读这里。
# 为什么收成一处的常量表：三处各写一份列名，改一处漏两处 = 迁移器报「已加列」而引擎仍报缺列，
# 这种漂移比缺列更难查。（vector_store._init_db 里那份 evolution 补列是同义复制，因它跑在
# 本函数之前、新库建表之前；两边一致性由 xtmem_d1_guard_test 断言兜住。）
D1_CHUNK_COLS = (
    ("retire_reason", "TEXT", "''"),   # 退役原因（历史上被塞进 superseded_by 的那批字串）
    ("retired_by", "TEXT", "''"),      # 谁裁的（manual-batch-* / 脚本名 / forget 发起者）
    ("retired_at", "REAL", "NULL"),    # 何时退（epoch；回填取原 updated_at 或 forget 事件 ts）
)
D1_EVO_COLS = (
    ("adjudicator", "TEXT", "''"),     # 边的裁决者：模型名 / 人工 / 脚本指纹
    ("policy_version", "TEXT", "''"),  # 按哪一版策略裁的（D2 冲突曝光要按版本归责）
    ("decision_reason", "TEXT", "''"), # 为什么这么裁
)

# ══ ★M01 第三轮（2026-10-13）R2 补件：v2 信封「出处/归属」载荷落库（B 路）════════
# 病根（验收 v3 `R2-database-provenance` 实测）：store() 把 speaker/subject/source_refs/
#   scope/valid_from/valid_until/confirmation_type 写进了 WAL，**但 DB 侧没有落点**——
#   Chunk 无字段、chunks 无列、也没有关联表。于是"这条是谁说的、出自哪里、何时有效"
#   只在日志里，一旦 WAL 被压缩/清理（mark_compressed、cleanup_expired 都是常规动作），
#   DB 就永远读不回完整载荷。契约 §5 明令 **WAL 与 DB 必须从同一份规范化载荷构造**。
#
# ★为什么选「加列」而不是「新建 memory_provenance 表」（本件的核心取舍，改前先读）：
#   1) **基数就是 1:1**：一个 chunk 只有一份出处/归属，不是一对多。建旁表只换来一次
#      JOIN 和"多一个写入点"。
#   2) **旁表会破 P0-4 的单事务铁律**：chunks 必须与正文同事务落库（禁止二段式补刀）。
#      旁表就意味着 chunk INSERT 与 provenance INSERT 两次写，中间崩溃 = 正文在、出处丢，
#      而且"丢"是静默的（谁也不知道少了行）。加列天然在同一 INSERT 语句里，无此窗口。
#   3) **旁表会绕过既有权限/可见性闸**：chunks 有 visibility 合法性触发器
#      （trg_chunks_visibility_ins/upd）与 namespace 过滤，所有读路径都按 chunks 走。
#      speaker/subject 属于私密归属信息，塞进一张没有这些闸的旁表 = 新开一个泄漏面。
#   4) **兼容迁移最省事**：本文件已有成熟的 `ALTER TABLE ... ADD COLUMN`（仅缺列时执行）
#      幂等范式；加列对存量行就是把值留成 NULL，无需回填、无需补造（契约 §5 铁律）。
#   5) 代价如实登记：chunks 列数变多（22→30）。这是**有意的**——出处是记忆的一等属性，
#      不是次要元数据；列数增长不改变任何既有查询语义（一律按列名取，无 SELECT * 位置依赖）。
#
# ⚠️ 缺省一律 **NULL**（不是 ''）：NULL = "这条没带 provenance"，'' = "调用方显式给了空"。
#    混用会把"不知道"洗成"已知为空"，正是本件要根治的病根。旧行迁移后全是 NULL。
# ⚠️ 旧库/旧请求【不】补造 provenance：只加列、不 UPDATE 存量行、不填默认值造事实。
# ⚠️ 回滚：本组列纯增量且无数据依赖，回滚 = 代码停写 +（可选）逐列 DROP COLUMN
#    （SQLite ≥3.35 支持；Python 3.11 自带 3.39+）。见 PATCH-B.md 的迁移与回滚段。
D_PROV_CHUNK_COLS = (
    ("speaker", "TEXT", "NULL"),            # 谁说的（对话主体标识）
    ("subject", "TEXT", "NULL"),            # 关于谁/关于什么
    ("source_refs", "TEXT", "NULL"),        # 出处引用列表（JSON 文本，结构化 SourceRef 可往返）
    ("scope", "TEXT", "NULL"),              # 适用范围（JSON 文本：字符串/对象/数组均可）
    ("valid_from", "REAL", "NULL"),         # 事实生效起点（epoch）
    ("valid_until", "REAL", "NULL"),        # 事实失效点（NULL=未声明失效，注意与"列缺省"同形，
                                            #   故用 provenance_status 区分"这条到底带没带载荷"）
    ("confirmation_type", "TEXT", "NULL"),  # 确认方式（explicit/inferred/…）
    ("provenance_status", "TEXT", "NULL"),  # verified / legacy_unverified；NULL=旧行（v2 之前）
)


def ensure_xtmem_schema(conn: sqlite3.Connection,
                        default_namespace: Optional[str] = "assistant") -> None:
    """幂等迁移：所有DDL作为一个事务单元；失败只撤本函数创建的结构。"""
    owns_transaction = not conn.in_transaction
    savepoint = "xtmem_schema_migration"
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT " + savepoint)
    try:
        _ensure_xtmem_schema_body(conn, default_namespace)
        if owns_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT " + savepoint)
    except BaseException:
        try:
            if owns_transaction:
                if conn.in_transaction:
                    conn.rollback()
            elif conn.in_transaction:
                conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                conn.execute("RELEASE SAVEPOINT " + savepoint)
        except BaseException:
            # Unknown transaction state must escape; callers must not treat a
            # partially migrated connection as ready.
            raise
        raise


def _ensure_xtmem_schema_body(conn: sqlite3.Connection,
                              default_namespace: Optional[str]) -> None:
    """Apply additive DDL without changing transaction ownership."""
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
    # D1-1（2026-09-09 Spec v1.2 §D1-1）指针/原因分离三列（清单见文件头 D1_CHUNK_COLS）。
    # 【勿动】旧数据语义：superseded_by 从此只允许「^mem-[0-9a-f]{8}$ 真指针」或空串，
    #   历史原因字串（cleanup-*/p41fix/forget）一律迁到 retire_reason，裁决者进 retired_by。
    #   把字串塞回 superseded_by，等于把 D1 的分离成果再搅浑一次（B4 指标立刻回红）。
    for col, col_type, default in D1_CHUNK_COLS:
        if col not in cols:
            conn.execute(
                f"ALTER TABLE chunks ADD COLUMN {col} {col_type} DEFAULT {default}"
            )
    # ★M01 第三轮 R2：v2 出处/归属载荷补列（清单见文件头 D_PROV_CHUNK_COLS，取舍理由也在那里）。
    #   ⚠️ 必须先**重新** PRAGMA 一次：上面两轮 ALTER 改过表结构，沿用函数开头那份 `cols`
    #   是旧快照——"刚加过的列"不在里面，会重复执行 ALTER 撞 `duplicate column name`
    #   （SQLite 对 ADD COLUMN 重复会抛 OperationalError，被 _init_db 的 try 吞掉并 rollback，
    #    连带把本函数后半段整批跳过；同款事故在 memory_evolution 段犯过，注释里记着）。
    #   ⚠️ 全部走 ADD COLUMN + IF 判存在：**幂等**，对旧库是纯增量（存量行留 NULL），
    #   对已迁移过的库是空操作。这是"兼容旧库"的实现方式——【不】重建表、【不】删列、
    #   【不】回填（回填 = 补造 provenance，契约 §5 明令禁止）。
    _pcols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    for col, col_type, default in D_PROV_CHUNK_COLS:
        if col not in _pcols:
            conn.execute(
                f"ALTER TABLE chunks ADD COLUMN {col} {col_type} DEFAULT {default}"
            )
    _m02_cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    for col in ("operation_id", "observation_id"):
        if col not in _m02_cols:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} TEXT DEFAULT NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_observations (
            observation_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            namespace TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            speaker TEXT,
            subject TEXT,
            scope TEXT,
            valid_from REAL,
            valid_until REAL,
            confirmation_type TEXT,
            source_refs TEXT NOT NULL DEFAULT '[]',
            provenance_status TEXT NOT NULL DEFAULT 'legacy_unverified',
            created_at REAL NOT NULL,
            UNIQUE(namespace, project, operation_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_observations_memory ON memory_observations(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_observations_content_scope ON memory_observations(content_hash, namespace, project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_ns ON chunks(namespace, project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_vis ON chunks(visibility, namespace)")
    # P4.8 T1：分层库两阶段检索主索引 (namespace, tier, state)——第一跳只算 refined 池
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_tier ON chunks(namespace, tier, state)")
    _m02_cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    for col in ("operation_id", "observation_id"):
        if col not in _m02_cols:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} TEXT DEFAULT NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_observations (
            observation_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            namespace TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            speaker TEXT,
            subject TEXT,
            scope TEXT,
            valid_from REAL,
            valid_until REAL,
            confirmation_type TEXT,
            source_refs TEXT NOT NULL DEFAULT '[]',
            provenance_status TEXT NOT NULL DEFAULT 'legacy_unverified',
            created_at REAL NOT NULL,
            UNIQUE(namespace, project, operation_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_observations_memory ON memory_observations(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_observations_content_scope ON memory_observations(content_hash, namespace, project)")

    # P1-E / R-09：chunks.visibility 合法值约束（trigger 实现 CHECK —— 存量表免 rebuild）
    # 非法值 = NULL / 非 shared/private 且不以 'shared-with:' 开头。
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_chunks_visibility_ins
        BEFORE INSERT ON chunks
        WHEN NEW.visibility IS NULL
           OR (NEW.visibility NOT IN ('shared','private') AND NEW.visibility NOT LIKE 'shared-with:%')
        BEGIN
            SELECT RAISE(ABORT, 'invalid visibility value');
        END;
    """)
    try:
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_chunks_visibility_upd
            BEFORE UPDATE OF visibility ON chunks
            WHEN NEW.visibility IS NULL
               OR (NEW.visibility NOT IN ('shared','private') AND NEW.visibility NOT LIKE 'shared-with:%')
            BEGIN
                SELECT RAISE(ABORT, 'invalid visibility value');
            END;
        """)
    except sqlite3.OperationalError:
        pass

    # P1-L / G-01：chunks.tier 合法值约束（trigger 实现 CHECK —— 存量表免 rebuild）。
    # 写入侧已先归一（REFINED → refined）；这里兜底直插/直改的非法值。
    # 注：tier NULL 历史语义 = raw，放行；仅拦非 NULL 且不在白名单的值。
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_chunks_tier_ins
        BEFORE INSERT ON chunks
        WHEN NEW.tier IS NOT NULL AND NEW.tier NOT IN ('raw','refined')
        BEGIN
            SELECT RAISE(ABORT, 'invalid tier value');
        END;
    """)
    try:
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_chunks_tier_upd
            BEFORE UPDATE OF tier ON chunks
            WHEN NEW.tier IS NOT NULL AND NEW.tier NOT IN ('raw','refined')
            BEGIN
                SELECT RAISE(ABORT, 'invalid tier value');
            END;
        """)
    except sqlite3.OperationalError:
        pass

    # ── 单A23-补-4（2026-09-16 小D）：distill_units 时间字段类型契约 ──
    # 病根：SQLite 是动态类型，REAL 列照样能塞 TEXT 字符串。distill_units.distilled_at
    # 曾被历史脚本写成 '2026-09-05 21:50:47'（存量 9 条，全 assistant / state=distilled）。
    # SQLite 排序序类型 TEXT > REAL ⇒ "ORDER BY distilled_at DESC 取最新"永远先命中那 9 条
    # 最老的 = 预判熔断闸被脏数据带偏。症状是"功能莫名不工作"，根因在数据格式，极难排查。
    # （活证据：scripts/xtmem_predict.py 258 行起有前人对同一病根的完整分析注释。）
    #
    # ⚠️ 为什么用 trigger 不用 CHECK —— 这是本条的核心取舍，改之前先读：
    #   SQLite 加 CHECK 只能走"12 步重建表"，而重建会把存量 9 条 TEXT 重新 INSERT 一遍
    #   ⇒ 当场 IntegrityError: CHECK constraint failed 卡死（小D 在 <TEMP> 副本上实测）。
    #   而工单 §四明确「清那 9 条」属补-1、用户尚未授权 ⇒ CHECK 在本单范围内根本不可行。
    #   trigger = "等价的写入端校验"（工单 §3.2 原文允许），不重写任何存量行、
    #   不碰那 9 条、DROP TRIGGER 即可回滚，且与本文件既有惯例一致
    #   （上方 chunks.visibility / chunks.tier 两组 trigger 就是同一个理由）。
    #
    # ⚠️ 放行 integer：scripts/xtmem_distill_commit.py 写的是 int(time.time())，typeof='integer'；
    #   scripts/xtmem_distill_llm.py 写 time.time()，typeof='real'。只认 'real' 会当场误伤
    #   现存两个正常写端，所以白名单是 ('real','integer')，只拦 TEXT/BLOB。
    #
    # 落点两处才两条路都覆盖（同上方 D1 段的理由）：本函数管"老库已有表"（provider/CLI 启动时跑），
    # scripts/xtmem_distill_units.py:ensure_schema 管"新库建表"（distill_units 是那个脚本建的，
    # 不是本插件建的，故这里必须先判表在不在场，否则新建空库上 CREATE TRIGGER 会炸）。
    _du_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='distill_units'"
    ).fetchone()
    if _du_exists:
        _du_when = ("NEW.{c} IS NOT NULL AND typeof(NEW.{c}) NOT IN ('real','integer')")
        _du_cols = ("ts_start", "ts_end", "created_at", "distilled_at")
        _du_cond = " OR ".join(_du_when.format(c=c) for c in _du_cols)
        for _trg, _ddl in (
            ("trg_distill_units_time_type_ins", "BEFORE INSERT ON distill_units"),
            ("trg_distill_units_time_type_upd", "BEFORE UPDATE ON distill_units"),
        ):
            try:
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS %s\n%s\nFOR EACH ROW WHEN %s\n"
                    "BEGIN\n"
                    "    SELECT RAISE(ABORT, 'distill_units time fields "
                    "(ts_start/ts_end/created_at/distilled_at) must be numeric epoch');\n"
                    "END;" % (_trg, _ddl, _du_cond)
                )
            except sqlite3.OperationalError:
                # 非"已存在"分支：IF NOT EXISTS 撞名是静默跳过、不抛异常。
                # 这里兜的是建表语句本身被拒（极老 SQLite 不认某语法）——
                # 守卫闸建不起来不该把 provider/CLI 整个启动拖死，故吞掉并留给
                # 守卫测试报红（它会逐条比对触发器 WHEN 是否真覆盖四列）。
                # ⚠️ 反面：同名旧触发器若 WHEN 子句比这里窄，IF NOT EXISTS 会静默保留旧的
                #   ——所以改列清单时必须同步 DROP 旧触发器，别指望 IF NOT EXISTS 帮你换。
                pass

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

    # D1-1（2026-09-09 Spec v1.2 §D1-1）：memory_evolution 增「裁决三列」——谁裁的/哪版策略/为什么。
    # 为什么要这三列：边只有 relation+confidence，事故回溯时分不清是模型判的、人工批的还是脚本改的，
    # D2 冲突曝光要按裁决者归责，缺这三列就无从归责。CREATE IF NOT EXISTS 对老表跳过，故逐列补。
    # ⚠️ 先判表存在再 ALTER：memory_evolution 由 vector_store._init_db 在本函数【之后】才建表。
    # 对不存在的表 ALTER 会抛 OperationalError，被 _init_db 的 try 吞成一行 warning 并 rollback，
    # 连带把本函数后半段（chunk_review_state / entities / …）整批跳过——D1 首版真踩过，冒烟抓出。
    _etab = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_evolution'"
    ).fetchone()
    if _etab:
        _ecols = [r[1] for r in conn.execute("PRAGMA table_info(memory_evolution)").fetchall()]
        for _ecol, _etype, _edef in D1_EVO_COLS:
            if _ecol not in _ecols:
                conn.execute(f"ALTER TABLE memory_evolution ADD COLUMN {_ecol} {_etype} DEFAULT {_edef}")

    # D0-1（2026-09-09 SupportBundle Spec v1.2）：chunk_review_state 幂等状态表（user_version 不动）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chunk_review_state (
            chunk_id TEXT PRIMARY KEY,
            overturn_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'none',
            first_required_at REAL,
            last_required_at REAL,
            policy_version TEXT DEFAULT '',
            updated_at REAL
        )"""
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
    # 通用软件词种子不夹带维护者的人物与项目；用户可另行登记自己的别名。
    _alias_seed = [
        ("XTMEM", "xtmem"), ("XTMEM", "XTMEM"),
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
        conn.execute("INSERT OR IGNORE INTO entities(canon, count, first_seen, last_seen) VALUES (?,0,?,?)", (_c, time.time(), time.time()))

    # ── D2 冲突治理表（Spec v1.2 §D2，独立存储，禁进 chunks，user_version=3 保持不动）──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conflict_regression (
            pair_id TEXT PRIMARY KEY,
            a_id TEXT,
            b_id TEXT,
            label TEXT CHECK(label IN ('supersedes','enriches','unrelated','ambiguous')),
            direction TEXT,
            adjudicator TEXT,
            ts REAL,
            split TEXT CHECK(split IN ('train','val','test')),
            note TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conflict_reg_split ON conflict_regression(split)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conflict_reg_label ON conflict_regression(label)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS conflict_candidate (
            pair_key TEXT PRIMARY KEY,
            source_id TEXT,
            target_id TEXT,
            namespace TEXT,
            project TEXT,
            candidate_score REAL,
            llm_score REAL,
            decision TEXT,
            policy_version TEXT,
            model TEXT,
            prompt_hash TEXT,
            status TEXT,
            attempt INTEGER DEFAULT 0,
            last_error TEXT,
            created_at REAL,
            updated_at REAL,
            decision_reason TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conflict_cand_status ON conflict_candidate(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conflict_cand_pair ON conflict_candidate(source_id, target_id)")


def quarantine_bad_visibility(conn, default_namespace=None, vector_store=None) -> dict:
    """P1-E / R-09：存量迁移——把 chunks.visibility 非法值置为 private 并写审计事件。

    非法 = 不是 'shared'/'private' 且不以 'shared-with:' 开头（含 NULL/空）。
    只改 visibility，不删数据。返回 {'quarantined': n, 'vis_followed': m}。
    迁移脚本先备份再调用。

    W3（2026-09-05，W 批接线）：可选 vector_store 实例——传入时每条转 private 的源
    额外调 follow_source_visibility(id, 'private')：源 vis 收紧 → 指向它的 shared 派生
    必须同步降（防"源已 private、派生仍 shared"明文越源泄漏面）。schema 不 import
    vector_store（防循环）；由调用方（迁移脚本/守卫测试）持 db 路径实例化后传入。
    follow 异常不中断 quarantine（源已 private 为主目标，派生降级可下轮补）。
    """
    import json as _json
    import time as _time
    bad = conn.execute(
        "SELECT id, visibility FROM chunks "
        "WHERE visibility IS NULL OR (visibility NOT IN ('shared','private') AND visibility NOT LIKE 'shared-with:%')"
    ).fetchall()
    now = _time.time()
    followed = 0
    for r in bad:
        conn.execute("UPDATE chunks SET visibility='private' WHERE id=?", (r["id"],))
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, "visibility-quarantine", None, None, None, r["id"],
             _json.dumps({"old": r["visibility"] or ""})),
        )
        # W3：转 private 的源 → 派生 vis 跟随收紧（min_visibility 只会更严不会放宽）
        if vector_store is not None:
            try:
                followed += int(vector_store.follow_source_visibility(r["id"], "private") or 0)
            except Exception:
                pass  # 派生降级失败不影响 quarantine 主目标（下轮补）
    conn.commit()
    return {"quarantined": len(bad), "vis_followed": followed}


def quarantine_bad_tier(conn) -> dict:
    """P1-L / G-01：存量迁移——把 chunks.tier 非法值归一/隔离并写审计事件。

    - NULL → 'raw'（历史 NULL 语义 = raw，先统计后执行）；
    - 大小写/空白变体（'  REFINED  '）→ 归一为 raw/refined；
    - 其余非法值 → 'raw'（fail-closed）并写 xtmem_events(kind='tier-quarantine')。
    只改 tier，不删数据。返回 {'normalized': n, 'quarantined': m}。
    """
    import json as _json
    import time as _time
    # 选中所有非规范形态（含大小写/空白变体）——变体归一为 raw/refined，真非法置 raw
    rows = conn.execute(
        "SELECT id, tier FROM chunks "
        "WHERE tier IS NULL OR tier NOT IN ('raw','refined')"
    ).fetchall()
    now = _time.time()
    normalized = quarantined = 0
    for r in rows:
        old = r["tier"] or ""
        if old == "":
            new = "raw"
            quarantined += 1
        else:
            cand = str(old).strip().lower()
            new = cand if cand in ("raw", "refined") else "raw"
            if cand in ("raw", "refined"):
                normalized += 1
            else:
                quarantined += 1
        conn.execute("UPDATE chunks SET tier=? WHERE id=?", (new, r["id"]))
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, target_chunk, meta) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, "tier-quarantine", None, None, None, r["id"],
             _json.dumps({"old": old, "new": new})),
        )
    conn.commit()
    return {"normalized": normalized, "quarantined": quarantined}
