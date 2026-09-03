
# ================================================================
# vector_store.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：SQLite 持久化向量存储——chunks 主表 + chunks_fts(FTS5) + embedding_cache +
#           chunk_utility（per-viewer 效用）+ memory_evolution（演化边，bi-temporal）+ xtmem_events。
#
# 结构：Chunk dataclass → store_chunk/batch_store_chunks（同一事务落正文+metadata+tier）→
#       get_utility/increment_utility → save_evolution/invalidate_evolution → dedup_by_hash → list_chunks。
#
# 修改铁律：
# 1. chunks 表列清单 / VALUES / ON CONFLICT / 元组 四者必须同步（列数不匹配=运行时错），加列四处同改；
#    store_chunk 与 batch_store_chunks 的列清单是同一份。
# 2. 正文+metadata 单事务落库（P0-4），禁止二段式 UPDATE 补 metadata。
# 3. 血缘闸（P0.1）：derived_from 非空 → source_ids 必须非空；min_visibility 继承最严源（P4.1 画像可见性）。
# 4. FTS 删除必须 rowid 映射 DELETE（id 列 UNINDEXED，按 id 删是假删除）。
#
# 特殊机制：
# - tier（P4.8）：raw=流水层 / refined=蒸馏+要点+画像预判；NULL→raw。
# - valid_to（P4.0-c）：save_evolution 支持 valid_to + invalidate_evolution 关闭边。
# ================================================================
"""L4 SQLite 存储层 — 持久化向量存储 + BM25 全文搜索"""

import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _tokenize_for_fts5(text: str) -> str:
    """对 CJK 文本做分词，使 unicode61 tokenizer 能正确切分中文。

    使用 jieba 分词（精确模式），支持中英文混合文本。
    未安装 jieba 时自动降级为单字+bigram 方案。
    """
    import re as _re
    has_cjk = bool(_re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', text))
    if not has_cjk:
        return text

    # jieba 精确模式（优先）
    try:
        import jieba
        words = list(jieba.cut(text, cut_all=False))
        # jieba 不区分中文和标点，因此保留原文中的非词元字符
        result = []
        pos = 0
        for w in words:
            idx = text.find(w, pos)
            if idx > pos:
                result.append(text[pos:idx])
            result.append(' ' + w + ' ')
            pos = idx + len(w)
        if pos < len(text):
            result.append(text[pos:])
        return ''.join(result)
    except ImportError:
        pass

    # 降级：单字+bigram
    result = []
    i = 0
    while i < len(text):
        c = text[i]
        if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf' or '\uf900' <= c <= '\ufaff':
            result.append(' ' + c + ' ')
            if i + 1 < len(text) and ('\u4e00' <= text[i+1] <= '\u9fff' or '\u3400' <= text[i+1] <= '\u4dbf' or '\uf900' <= text[i+1] <= '\ufaff'):
                result.append(c + text[i+1] + ' ')
            i += 1
        else:
            result.append(c)
            i += 1
    return ''.join(result)


@dataclass
class Chunk:
    """文本块数据结构"""
    id: str = ""
    content: str = ""
    timestamp: float = 0.0
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    source: str = ""
    token_count: int = 0
    content_hash: str = ""
    # Phase 1 R5: 零退化分轨
    track: str = "default"   # identity / preference / emotion / default
    # Phase 1 R9: 知识演化追踪
    state: str = "active"    # active / superseded / archived
    superseded_by: str = ""  # 被哪条记忆取代（chunk_id）
    # P0-4：正文与 metadata 单事务落库（二段式遗留列，插入时随正文一起写）
    namespace: str = ""
    project: str = ""
    host_origin: str = ""
    visibility: str = "shared"   # shared / private / shared-with:a,b
    updated_at: Optional[float] = None
    # P0.1（2026-08-31）：血缘——来源 chunk id 数组（JSON 文本）+ 衍生类型
    source_ids: str = "[]"
    derived_from: str = ""
    # P4.8 T3（2026-09-01）：分层库 tier——raw=流水层 / refined=蒸馏+显式要点+画像预判；缺省 raw
    tier: str = "raw"

    def __post_init__(self):
        # R1.1（2026-08-31）：第三道闸，兜住绕过 CLI/builder 直构 Chunk 的路径
        self.importance = max(0.0, min(1.0, float(self.importance)))
        # P4.9-a（2026-09-02）：血缘强制闸——derived_from 非空时 source_ids 必须非空（JSON 解析后），
        # 否则拒写（与 P0.1 血缘闸同语义，这里兜住直构 Chunk 的路径）。
        if self.derived_from:
            try:
                _sid = json.loads(self.source_ids) if isinstance(self.source_ids, str) else (self.source_ids or [])
            except Exception:
                _sid = []
            if not _sid:
                raise ValueError("memory write rejected: derived_from 需要非空 source_ids（P4.9-a 血缘强制闸）")


class VectorStore:
    """SQLite 持久化向量存储

    表结构：
      - chunks: 存储文本块
      - chunks_fts: FTS5 全文索引（BM25 搜索）
      - embedding_cache: embedding 缓存
      - associations: chunk 间关联关系
      - chunk_utility: chunk 效用追踪
    """

    DEFAULT_DB_PATH = ".hermes/memory/vector_store.db"

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
                    self._conn.row_factory = sqlite3.Row
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=NORMAL")
                    self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self) -> None:
        """初始化数据库 schema"""
        conn = self._get_conn()

        # chunks 表：存储文本块
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                importance REAL DEFAULT 0.5,
                tags TEXT DEFAULT '[]',
                source TEXT DEFAULT '',
                token_count INTEGER DEFAULT 0
            )
        """)

        # 迁移：添加 content_hash 字段
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN content_hash TEXT DEFAULT ''")
            logger.info("Added content_hash column to chunks table")
        except sqlite3.OperationalError:
            pass  # 列已存在

        # Phase 1 R5: 迁移：添加 track 字段
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN track TEXT DEFAULT 'default'")
            logger.info("Added track column to chunks table")
        except sqlite3.OperationalError:
            pass

        # Phase 1 R9: 迁移：添加 state/superseded_by 字段
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN state TEXT DEFAULT 'active'")
            logger.info("Added state column to chunks table")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN superseded_by TEXT DEFAULT ''")
            logger.info("Added superseded_by column to chunks table")
        except sqlite3.OperationalError:
            pass

        # P0-4/P1-6 统一 schema：namespace/project/host_origin/updated_at/visibility + 玩法/总线表
        try:
            from memory.schema import ensure_xtmem_schema
            ensure_xtmem_schema(conn)
        except Exception as e:
            logger.warning("ensure_xtmem_schema failed: %s", e)
            conn.rollback()

        # 建索引（幂等）
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_hash 
            ON chunks(content_hash)
        """)

        # chunks_fts 表：FTS5 全文索引（独立存储，不依赖 external content）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                id UNINDEXED,
                content,
                tags,
                source,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)

        # 删除旧的 external content 触发器（改用代码手动管理 FTS5 同步）
        conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
        conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
        conn.execute("DROP TRIGGER IF EXISTS chunks_au")

        # P0-5 兼容迁移：旧版 external-content FTS → 独立 FTS（DELETE 才真正生效）
        try:
            if self._is_external_content_fts(conn):
                logger.warning(
                    "chunks_fts is legacy external-content FTS; rebuilding as standalone FTS"
                )
                conn.commit()
                self.rebuild_index_atomic()
        except Exception as e:
            logger.warning("chunks_fts legacy migration failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass

        # embedding_cache 表：缓存 embedding 向量（带 chunk_id 外键）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                chunk_id TEXT,
                embedding BLOB NOT NULL,
                dim INTEGER NOT NULL,
                model TEXT DEFAULT 'simple',
                created_at REAL NOT NULL,
                FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            )
        """)

        # associations 表：chunk 间关联关系（append-only consolidation）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS associations (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                rel_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                metadata TEXT DEFAULT '{}',
                PRIMARY KEY (source_id, target_id, rel_type)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assoc_source ON associations(source_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assoc_target ON associations(target_id)
        """)

        # chunk_utility 表：效用追踪（P0.6：per-viewer 复合主键，清表从零）
        _util_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_utility'"
        ).fetchone()
        if _util_exists:
            _util_cols = [r[1] for r in conn.execute("PRAGMA table_info(chunk_utility)").fetchall()]
            if "viewer_agent" not in _util_cols:
                conn.execute("DROP TABLE chunk_utility")  # 旧 schema（chunk_id 单主键）清表从零
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunk_utility (
                chunk_id TEXT NOT NULL,
                viewer_agent TEXT NOT NULL DEFAULT '',
                utility_score REAL DEFAULT 0.0,
                access_count INTEGER DEFAULT 0,
                last_access REAL DEFAULT 0.0,
                PRIMARY KEY (chunk_id, viewer_agent)
            )
        """)

        # 索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_timestamp ON chunks(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_hash ON embedding_cache(text_hash)")

        # Phase 1 R9: 知识演化关系表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_evolution (
                id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now')),
                visibility TEXT DEFAULT 'shared',
                valid_from REAL DEFAULT 0,
                valid_to REAL DEFAULT 0,
                shadow_state INTEGER DEFAULT 1
            )
        """)
        # P1.6（2026-08-31）：存量表补边可见性 + bi-temporal 列
        _evo_cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_evolution)").fetchall()]
        for _c, _t, _d in (("visibility","TEXT","'shared'"),("valid_from","REAL","0"),("valid_to","REAL","0"),("shadow_state","INTEGER","1")):
            if _c not in _evo_cols:
                conn.execute("ALTER TABLE memory_evolution ADD COLUMN {} {} DEFAULT {}".format(_c, _t, _d))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evo_source ON memory_evolution(source_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evo_target ON memory_evolution(target_id)")

        # Phase 1 R5: 索引 track/state
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_track ON chunks(track)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_state ON chunks(state)")

        conn.commit()
        logger.debug("VectorStore initialized at %s", self.db_path)

    # ── 存储安全层 ──────────────────────────────────────

    MAX_CONTENT_LENGTH = 100_000
    _INJECTION_PATTERNS = re.compile(
        # 直接指令覆盖
        r"ignore\s*(all\s*)?(your\s*)?(previous|prior|earlier)\s*(instructions?|directions?|prompts?|rules?|guidelines?)"
        r"|forget\s*(all\s*|everything\s*)?(you\s*)?(learned|know|were\s*told|remember)"
        r"|you\s*are\s*(not\s*|now\s*)?(an?\s*|the\s*)?(AI|assistant|model|GPT|LLM|language\s*model|chatbot)"
        r"|disregard\s*(all\s*|previous\s*|prior\s*)?(instructions?|commands?|directives?|rules?)"
        r"|do\s*not\s*follow\s*(the\s*|any\s*|these\s*)?(instructions?|rules?|guidelines?)"
        r"|you\s*must\s*(ignore|forget|override|bypass)\s"
        # 角色劫持
        r"|from\s*now\s*on\s*you\s*are\s*(an?\s*)?"
        r"|act\s*as\s*(if\s*)?(you\s*are\s*)?(an?\s*)?"
        r"|pretend\s*(you\s*are|to\s*be)\s*(an?\s*)?"
        r"|your\s*(new|real|true|actual)\s*(name|identity|role|purpose)\s*is"
        # 系统级注入
        r"|<\|?im_start\|?>|<\|?im_end\|?>"
        r"|<\|?system\|?>|<\|?user\|?>|<\|?assistant\|?>"
        r"|\[system\s*(prompt|message|instruction)\]"
        r"|\[/?(system|prompt)\]"
        r"|<\s*(system|prompt|instruction)\s*>"
        # 重复内容轰炸（>80% 相同行出现 3 次以上）
        r"|\[REPEATED_CONTENT\]",
        re.IGNORECASE | re.DOTALL,
    )
    _INJECTION_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]", re.UNICODE)
    # P0-2：Unicode 全角/同形字符折叠表（防全角、Cyrillic/带音符 ASCII 同形绕过）
    _INJECTION_FOLD = {
        ord(c): ord(u) for c, u in {
            "\uff10": "0", "\uff11": "1", "\uff12": "2", "\uff13": "3", "\uff14": "4",
            "\uff15": "5", "\uff16": "6", "\uff17": "7", "\uff18": "8", "\uff19": "9",
            "\uff41": "a", "\uff42": "b", "\uff43": "c", "\uff44": "d", "\uff45": "e", "\uff46": "f",
            "\uff47": "g", "\uff48": "h", "\uff49": "i", "\uff4a": "j", "\uff4b": "k", "\uff4c": "l",
            "\uff4d": "m", "\uff4e": "n", "\uff4f": "o", "\uff50": "p", "\uff51": "q", "\uff52": "r",
            "\uff53": "s", "\uff54": "t", "\uff55": "u", "\uff56": "v", "\uff57": "w", "\uff58": "x",
            "\uff59": "y", "\uff5a": "z",
            "\uff21": "a", "\uff22": "b", "\uff23": "c", "\uff24": "d", "\uff25": "e", "\uff26": "f",
            "\uff27": "g", "\uff28": "h", "\uff29": "i", "\uff2a": "j", "\uff2b": "k", "\uff2c": "l",
            "\uff2d": "m", "\uff2e": "n", "\uff2f": "o", "\uff30": "p", "\uff31": "q", "\uff32": "r",
            "\uff33": "s", "\uff34": "t", "\uff35": "u", "\uff36": "v", "\uff37": "w", "\uff38": "x",
            "\uff39": "y", "\uff3a": "z",
            "\u0456": "i", "\u00ed": "i", "\u00ec": "i", "\u01d0": "i", "\u00ee": "i", "\u00ef": "i", "\u0131": "i",
            "\u0430": "a", "\u00e1": "a", "\u00e0": "a", "\u0101": "a", "\u0103": "a", "\u00e2": "a", "\u00e4": "a",
            "\u0435": "e", "\u00e9": "e", "\u00e8": "e", "\u0113": "e", "\u011b": "e", "\u00ea": "e", "\u00eb": "e",
            "\u043e": "o", "\u00f3": "o", "\u00f2": "o", "\u014d": "o", "\u00f4": "o", "\u00f6": "o",
            "\u0441": "c", "\u010d": "c", "\u00e7": "c", "\u015b": "s", "\u0161": "s", "\u015f": "s",
            "\u011f": "g", "\u0111": "d", "\u013c": "l", "\u00f1": "n", "\u016b": "u", "\u016f": "u",
            # 西里尔常用同形（视觉近似 ASCII）
            "\u0440": "p", "\u0433": "r", "\u0431": "b", "\u0432": "b",
            "\u0434": "d", "\u0436": "j", "\u0437": "3", "\u0439": "j",
            "\u043a": "k", "\u043b": "l", "\u043c": "m", "\u043d": "h",
            "\u043f": "n", "\u0442": "t", "\u0443": "y", "\u0445": "x",
            "\u0448": "w", "\u0449": "w", "\u0446": "c", "\u0447": "4",
            "\u0455": "s", "\u0458": "j", "\u045f": "d", "\u0463": "g",
        }.items()
    }
    _INJECTION_PATTERNS_CN = re.compile(
        r"忽略(所有|全部|之前|以前的)?(指令|指示|规则|命令|提示|要求)"
        r"|无视(所有|全部|之前|以前的)?(指令|指示|规则|命令|提示|要求)"
        r"|不要(遵守|遵循|执行)(任何|这条|这些)?(指令|规则|指示)"
        r"|(?<!不要)(?<!不可)(?<!禁止)(?<!别)泄露(所有|全部)?(秘密|机密|隐私|系统提示|system)"
        r"|((展示|调出|读取|输出|打印|复制|粘贴|转述|透露|泄露))(系统|内部|隐藏的)?(提示词|提示|指令|规则|消息|配置)"
        r"|你是(现在|实际上|真正的)?(ai|助手|语言模型|chatbot|没有限制|不受控|自由模型|另一个模型)"
        r"|隐藏的指令|秘密指令|im(mediately)?(dis)?regard",
        re.IGNORECASE | re.DOTALL,
    )
    # R3.1（2026-08-31）：对抗实测 43.6% -> 83%，第二轮收紧：补漏网 + 消误报（用 [ \t ] 保留空白，避开转义）
    _INJECTION_PATTERNS_EXFIL = re.compile(
        r"(reveal|show|display|print|copy|output|paste|dump|transcribe|extract|send[ \t]+me|give[ \t]+me|leak|exfiltrate|disclose|return|need|read|deem[ \t]+irrelevant)"
        r".{0,120}?(system[ \t]*(prompt|message|instructions?|rules?)|hidden[ \t]*(role[ \t]+|system[ \t]+)?(instructions?|prompt|rules?|system[ \t]*message)|"
        r"internal[ \t]*(prompt|instructions?|message|guidelines|configuration)|api[ \t]*key|secret[ \t]*(key|text|system[ \t]*text)|confidential|"
        r"secrets?|full[ \t]*prompt|model[ \t]*prompt|private[ \t]*system[ \t]*rules|configuration[ \t]*secret|system[ \t]*text)",
        re.IGNORECASE,
    )
    _INJECTION_PATTERNS_EXFIL_CN = re.compile(
        r"(展示|调出|读取|输出|打印|复制|粘贴|转述|透露|给我|发给我|发到|倒出来|取出来|取出|返回|我要|我需要|公开)"
        r".{0,60}?(系统提示|系统消息|系统提示词|隐藏的?(角色[ \t]*)?(提示|指令|规则)|内部(提示|指令|规则|消息|配置|指导原则)|提示词|模型提示词|角色提示词|秘密|机密|密钥|秘密配置|指导原则|系统规则|私有系统规则)"
        r"|(系统提示|系统消息|系统提示词|隐藏的?(角色[ \t]*)?(提示|指令|规则)|内部(提示|指令|规则|消息|配置|指导原则)|提示词|模型提示词|角色提示词|秘密|机密|密钥|秘密配置|指导原则|系统规则|私有系统规则)"
        r".{0,60}?(给我|发我|发到|输出|展示|复制|粘贴|转述|透露|倒出来|取出来|取出|粘贴出来|我要)",
        re.IGNORECASE,
    )
    _INJECTION_PATTERNS_OVERRIDE = re.compile(
        r"you[ \t]*are[ \t]*(now|actually|really)?[ \t]*(a[ \t]+|an[ \t]+)?(free|unrestricted|rogue|malicious|no[ \t]*restrictions|another[ \t]*model|model)[A-Za-z0-9_ \t]*"
        r"|from[ \t]*(now[ \t]*on|today)[ ]*(your[ \t]*)?(role|purpose|job|identity)[ \t]*is"
        r"|do[ \t]*not[ \t]*follow[ \t]*(the[ \t]*)?(system[ \t]*)?(instructions?|rules?|guidelines?|prompt)"
        r"|disregard[ \t]+(?:(?:all|any|the|previous|prior|system)[ \t]+)*(?:instructions?|commands?|rules?|guidelines?|prompt)"
        r"|(your[ \t]*)?purpose[ \t]*is[ \t]*to[ \t]*(disclose|dump|leak|exfil|reveal)",
        re.IGNORECASE,
    )
    _INJECTION_PATTERNS_OVERRIDE_CN = re.compile(
        r"你(现在|实际上|真正|实际)?是(一个|个)?(没有限制|不受控|恶意|地下|内部|自由模型|另一个模型|真正的?)"
        r"|假装(你|是|成|做)|伪装(成|为)?|忽略[^，。]{0,8}(系统|内部|隐藏的)(指令|提示|规则|指示)"
        r"|你的(新|真实|实际)(身份|角色|作用|名字)是"
        r"|从现在起|从此刻起|从今天起.{0,20}(你是|你的角色|你的作用|你的身份)"
        r"|不要(遵守|遵循|执行)(任何|这条|这些)?(指令|规则|指示|系统提示)"
        r"|无视(所有|全部|系统)?(指令|规则|提示|指示|安全)",
        re.IGNORECASE,
    )

    @classmethod
    def _check_injection(cls, content: str) -> bool:
        """增强注入检测：原始匹配 + 标准化匹配。

        标准化去除所有标点和空格后匹配，防止攻击者用 "i g n o r e"
        或 "i.g.n.o.r.e" 等变体绕过。
        """
        # 原始匹配
        if cls._INJECTION_PATTERNS.search(content):
            return True

        # 标准化匹配：去除非字母数字中文后检查
        normalized = cls._INJECTION_NORMALIZE_RE.sub("", content).lower()
        if cls._INJECTION_PATTERNS.search(normalized):
            return True
        # P0-2：全角/同形折叠后再查（覆盖 i.g.n.o.r.e / 全角 / Cyrillic 同形）
        folded = content.translate(cls._INJECTION_FOLD)
        norm_folded = cls._INJECTION_NORMALIZE_RE.sub("", folded).lower()
        if cls._INJECTION_PATTERNS.search(norm_folded):
            return True
        # P0-2：中文注入关键词（内容原文 + 归一后各查一次）
        if cls._INJECTION_PATTERNS_CN.search(content):
            return True
        if cls._INJECTION_PATTERNS_CN.search(norm_folded):
            return True
        # R3.1（2026-08-31）：补白盒/外泄/越权/身份变体检查
        if cls._INJECTION_PATTERNS_EXFIL.search(content):
            return True
        if cls._INJECTION_PATTERNS_EXFIL_CN.search(content):
            return True
        if cls._INJECTION_PATTERNS_OVERRIDE.search(content):
            return True
        if cls._INJECTION_PATTERNS_OVERRIDE_CN.search(content):
            return True

        # 重复行检测
        lines = [l.strip().lower() for l in content.split("\n") if l.strip()]
        if len(lines) > 6:
            from collections import Counter
            counts = Counter(lines)
            most_common = counts.most_common(1)
            if most_common and most_common[0][1] >= 3 and most_common[0][1] / len(lines) > 0.3:
                return True

        return False

    @staticmethod
    def _is_external_content_fts(conn) -> bool:
        """P0-5 兼容迁移：旧版 external-content FTS（content=chunks）DELETE 不生效，
        当前代码手动维护 FTS，必须重建为独立表。"""
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
            ).fetchone()
            return bool(row and "content=" in (row[0] or ""))
        except Exception:
            return False

    @staticmethod
    def _validate_content(content: str) -> Optional[str]:
        """验证并清理待存储内容，返回清理后的文本或在拒绝时返回 None。"""
        if not content or not content.strip():
            return None
        # 长度限制
        if len(content) > VectorStore.MAX_CONTENT_LENGTH:
            logger.warning(
                "content too long (%d chars, max %d), truncating",
                len(content), VectorStore.MAX_CONTENT_LENGTH,
            )
            content = content[:VectorStore.MAX_CONTENT_LENGTH]
        # 控制字符清理（保留 \n \t \r）
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", content)
        if cleaned != content:
            logger.info("content contained control chars, stripped %d bytes", len(content) - len(cleaned))
            content = cleaned
        # 注入模式检测：原始 + 标准化二次匹配
        if VectorStore._check_injection(content):
            logger.warning(
                "REJECTED: prompt injection pattern detected (len=%d, prefix=%s)",
                len(content), content[:80].replace("\n", " "),
            )
            return None
        return content

    def store_chunk(self, chunk: Chunk) -> bool:
        """存储文本块。返回 True=已落库 / False=内容被安全校验拒绝（P0-3）。

        正文 + namespace/project/host_origin/visibility/updated_at 同一事务写入（P0-4），
        不再靠调用方二段式 UPDATE 补 metadata。
        """
        content = self._validate_content(chunk.content)
        if content is None:
            logger.warning("store_chunk: rejecting chunk %s (empty/invalid content)", chunk.id[:16])
            return False
        chunk.content = content
        conn = self._get_conn()
        tags_json = json.dumps(chunk.tags, ensure_ascii=False)
        updated_at = chunk.updated_at if chunk.updated_at is not None else time.time()

        # 1. chunks 表：正文+metadata 单事务存原始文本
        conn.execute(
            """
            INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, visibility, source_ids, derived_from, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                timestamp=excluded.timestamp,
                importance=excluded.importance,
                tags=excluded.tags,
                source=excluded.source,
                token_count=excluded.token_count,
                content_hash=excluded.content_hash,
                track=excluded.track,
                state=excluded.state,
                superseded_by=excluded.superseded_by,
                namespace=excluded.namespace,
                project=excluded.project,
                host_origin=excluded.host_origin,
                updated_at=excluded.updated_at,
                visibility=excluded.visibility,
                source_ids=excluded.source_ids,
                derived_from=excluded.derived_from,
                tier=excluded.tier
            """,
            (chunk.id, chunk.content, chunk.timestamp, chunk.importance, tags_json, chunk.source,
             chunk.token_count, chunk.content_hash, chunk.track, chunk.state, chunk.superseded_by,
             chunk.namespace, chunk.project, chunk.host_origin, updated_at, chunk.visibility,
              chunk.source_ids, chunk.derived_from, getattr(chunk, "tier", "raw") or "raw"),
        )
        rowid = conn.execute("SELECT rowid FROM chunks WHERE id=?", (chunk.id,)).fetchone()
        if rowid:
            # 2. chunks_fts：存分词文本（用于中文搜索）
            ft_content = _tokenize_for_fts5(chunk.content)
            conn.execute(
                """INSERT OR REPLACE INTO chunks_fts(rowid, id, content, tags, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (rowid[0], chunk.id, ft_content, tags_json, chunk.source),
            )
        conn.commit()
        return True

    # R2.3（2026-08-31）：store_chunks() 死代码已删除——
    # 唯一入口 batch_store_chunks（自含 FTS 同步，且逐批 commit）。

    def batch_store_chunks(self, chunks: List[Chunk], batch_size: int = 500) -> int:
        """分批存储大量文本块，返回成功存储的总数。

        与 store_chunks 的区别：
          - 将大列表拆分为 batch_size 大小的批次，每批独立 commit，
            避免单次超大事务占用过多 WAL 空间或触发 SQLite 锁超时。
          - 异常时已完成的批次不会回滚，返回已成功写入的数量。
        """
        conn = self._get_conn()
        # P0-2/P0-3：落库前统一安全校验，被拒内容不计数
        chunks = [c for c in chunks
                  if self._validate_content(c.content) is not None]
        total = 0
        sql = """
            INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, visibility, source_ids, derived_from, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                timestamp=excluded.timestamp,
                importance=excluded.importance,
                tags=excluded.tags,
                source=excluded.source,
                token_count=excluded.token_count,
                content_hash=excluded.content_hash,
                track=excluded.track,
                state=excluded.state,
                superseded_by=excluded.superseded_by,
                namespace=excluded.namespace,
                project=excluded.project,
                host_origin=excluded.host_origin,
                updated_at=excluded.updated_at,
                visibility=excluded.visibility,
                source_ids=excluded.source_ids,
                derived_from=excluded.derived_from,
                tier=excluded.tier
        """
        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset: offset + batch_size]
            try:
                conn.executemany(
                    sql,
                    [
                        (c.id, c.content, c.timestamp, c.importance,
                         json.dumps(c.tags, ensure_ascii=False), c.source, c.token_count, c.content_hash,
                         c.track, c.state, c.superseded_by, c.namespace, c.project, c.host_origin,
                         c.updated_at if c.updated_at is not None else time.time(), c.visibility,
                         c.source_ids, c.derived_from, getattr(c, "tier", "raw") or "raw")
                        for c in batch
                    ],
                )
                conn.commit()
                # 同步 FTS5（分词文本用于中文搜索）
                for chunk in batch:
                    row = conn.execute("SELECT rowid FROM chunks WHERE id=?", (chunk.id,)).fetchone()
                    if row:
                        ft_content = _tokenize_for_fts5(chunk.content)
                        tags_json = json.dumps(chunk.tags, ensure_ascii=False)
                        conn.execute(
                            """INSERT OR REPLACE INTO chunks_fts(rowid, id, content, tags, source)
                               VALUES (?, ?, ?, ?, ?)""",
                            (row[0], chunk.id, ft_content, tags_json, chunk.source),
                        )
                conn.commit()
                total += len(batch)
            except Exception as e:
                logger.warning("batch_store_chunks: batch starting at %d failed: %s", offset, e)
                break
        return total

    def get_chunk(self, chunk_id: str) -> Optional[Chunk]:
        """获取单个文本块"""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        return Chunk(
            id=row["id"],
            content=row["content"],
            timestamp=row["timestamp"],
            importance=row["importance"],
            tags=json.loads(row["tags"]),
            source=row["source"],
            token_count=row["token_count"],
            content_hash=row["content_hash"],
            track=row["track"] if "track" in row.keys() else "default",
            state=row["state"] if "state" in row.keys() else "active",
            superseded_by=row["superseded_by"] if "superseded_by" in row.keys() else "",
        )

    def chunk_exists_by_hash(self, content_hash: str) -> bool:
        """检查是否存在相同 hash 的 chunk"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE content_hash = ? AND content_hash != '' LIMIT 1",
            (content_hash,)
        ).fetchone()
        return row is not None

    def dedup_by_hash(self) -> int:
        """删除重复的 chunk（只保留最新的一条）
        
        注意：使用 CTE 避免 SQLite < 3.33.0 的同表子查询限制。
        当前环境 Python 3.11 + SQLite 3.39+ 满足要求。
        """
        conn = self._get_conn()
        # 找出重复的 hash
        duplicates = conn.execute("""
            SELECT content_hash, COUNT(*) as cnt
            FROM chunks
            WHERE content_hash != ''
            GROUP BY content_hash
            HAVING cnt > 1
        """).fetchall()
        
        removed = 0
        for row in duplicates:
            content_hash = row[0]
            # 保留最新一条，其余删除（同步删 FTS 行，防幽灵残留 P0-5）
            keep = conn.execute(
                "SELECT id FROM chunks WHERE content_hash=? ORDER BY timestamp DESC LIMIT 1",
                (content_hash,),
            ).fetchone()
            if keep is None:
                continue
            dup_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM chunks WHERE content_hash=? AND id<>?",
                (content_hash, keep[0]),
            )]
            for cid in dup_ids:
                conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
                    (cid,),
                )
                conn.execute("DELETE FROM chunks WHERE id=?", (cid,))
                removed += 1

        conn.commit()
        return removed

    def delete_chunk(self, chunk_id: str) -> bool:
        """删除文本块（P0-5：FTS 按 rowid 映射删除，UNINDEXED 列 WHERE 不可靠）

        R1.4（2026-08-31）：同款清 embedding_cache——先读 content 算 text_hash 再删。
        别指望 CASCADE：SQLite FK 默认不启用，必须显式删。
        """
        conn = self._get_conn()
        from memory.utils import compute_embedding_cache_key
        crow = conn.execute("SELECT content FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if crow:
            conn.execute(
                "DELETE FROM embedding_cache WHERE text_hash=?",
                (compute_embedding_cache_key(str(crow["content"] or "")),),
            )
        # 先删 FTS5 索引（chunks_fts.rowid == chunks.rowid，经 chunks 映射）
        conn.execute(
            "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
            (chunk_id,),
        )
        cur = conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Phase 1 R5: 零退化分轨 ──────────────────────────

    def update_chunk_track(self, chunk_id: str, track: str) -> bool:
        """更新 chunk 的分轨标签"""
        conn = self._get_conn()
        cur = conn.execute("UPDATE chunks SET track = ? WHERE id = ?", (track, chunk_id))
        conn.commit()
        return cur.rowcount > 0

    # ── Phase 1 R9: 知识演化追踪 ──────────────────────────

    def mark_superseded(self, target_id: str, source_id: str) -> bool:
        """将旧记忆标记为 superseded（R2.3：同步清 FTS5，防 superseded 幽灵命中）"""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE chunks SET state = 'superseded', superseded_by = ? WHERE id = ? AND state = 'active'",
            (source_id, target_id),
        )
        if cur.rowcount > 0:
            # 与 cmd_forget 同款 rowid 映射删除（FTS5 id 列 UNINDEXED，按 id 删是假删除）
            conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
                (target_id,),
            )
        conn.commit()
        if cur.rowcount > 0:
            logger.info("Memory superseded: %s by %s", target_id, source_id)
        return cur.rowcount > 0

    def save_evolution(self, source_id: str, target_id: str, relation: str, confidence: float = 0.0, visibility: str = None, shadow_state: int = 1, valid_to: float = 0.0) -> None:
        """记录知识演化关系（P1.6：edge 带可见性 = 两端点最严者 + bi-temporal；P2.2：shadow_state 1=shadow 未执行 / 0=已执行）。

        P4.0-c（2026-08-31）：valid_to 不再恒 0——边被覆盖/退役时可写当前时间戳，使 P4.2 预判块
        的"失效/退役"有 bi-temporal 语义可依。默认 0 = 一直有效。
        """
        conn = self._get_conn()
        if visibility is None:
            from memory.visibility import min_visibility
            rows = conn.execute(
                "SELECT id, visibility FROM chunks WHERE id IN (?,?)", (source_id, target_id)
            ).fetchall()
            vis_map = {r["id"]: r["visibility"] for r in rows}
            visibility = min_visibility([source_id, target_id], lambda sid: vis_map.get(sid, "private"))
        now = time.time()
        conn.execute(
            "INSERT INTO memory_evolution (source_id, target_id, relation, confidence, visibility, valid_from, valid_to, shadow_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, target_id, relation, confidence, visibility, now, float(valid_to or 0.0), shadow_state),
        )
        conn.commit()

    def invalidate_evolution(self, source_id: str, target_id: str) -> int:
        """关闭 source/target 之间仍开放（valid_to=0）的演化边，写当前时间戳（P4.0-c）。

        返回受影响行数。当一条 supersedes/replaces 新边落库时，先关掉这对节点间的旧边，
        让"被覆盖"有真实的 bi-temporal 语义（旧边 valid_to=now）。
        """
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE memory_evolution SET valid_to=? WHERE valid_to=0 AND ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
            (time.time(), source_id, target_id, target_id, source_id),
        )
        conn.commit()
        return cur.rowcount

    def search_similar_for_evolution(self, content: str, top_k: int = 5, namespace: str = None, exclude_id: str = None) -> List[Tuple[str, str, float]]:
        """搜索相似记忆用于演化检测。返回 [(chunk_id, content, importance), ...]

        R1.2（2026-08-31）：namespace + 可见性双重过滤——只在本域、只用 shared 记忆建边。
        实测 7 条指向 non-shared 的边全部同域，只限 namespace 一条都拦不住。
        BF-1（2026-09-01）：加 exclude_id 排除当前正在 store 的自己——否则刚落库的自己在第一位、
        jaccard=1.0 自匹配，产 source_id=target_id 的自环边（历史 974/1693 即此）。
        """
        conn = self._get_conn()
        # P4.9-c（2026-09-02）：图写侧参数外置——候选上限 + Jaccard 粗筛阈从 tuning 读，缺项用默认兜底。
        try:
            from memory.tuning import get as _tg
            _search_limit = int(_tg("evolution", "search_limit", 100))
            _jaccard_candidate = float(_tg("evolution", "jaccard_candidate", 0.1))
        except Exception:
            _search_limit, _jaccard_candidate = 100, 0.1
        # P1.6（2026-08-31）：恢复同域 private 演化——去掉 shared-only，只按 namespace 卡同域；
        # 边可见性由 save_evolution 用 min_visibility 计算（private 同域边可见、跨域 private 边不建）
        sql = ("SELECT id, content, importance FROM chunks "
               "WHERE state='active'")
        params: list = []
        if exclude_id:
            sql += " AND id != ?"
            params.append(exclude_id)
        if namespace:
            sql += " AND namespace=?"
            params.append(namespace)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(_search_limit)
        rows = conn.execute(sql, params).fetchall()
        # 简单字符级 Jaccard 筛选（不依赖 embedding，零成本）
        results = []
        for row in rows:
            jaccard = _jaccard_similarity(content, row["content"])
            if jaccard > _jaccard_candidate:  # 粗筛阈（P4.9-c：tuning evolution.jaccard_candidate）
                results.append((row["id"], row["content"], row["importance"], jaccard))
        results.sort(key=lambda x: x[3], reverse=True)
        return [(r[0], r[1], r[2]) for r in results[:top_k]]

    def _sanitize_fts_query(self, query: str) -> str:
        """清理 FTS5 查询，防止注入攻击"""
        # 移除 FTS5 特殊字符
        sanitized = re.sub(r'["*():^]', ' ', query)
        # 移除多余空格
        sanitized = ' '.join(sanitized.split())
        return sanitized if sanitized else "*"

    def search_bm25(self, query: str, top_k: int = 20) -> List[Tuple[str, float]]:
        """BM25 全文搜索

        Returns:
            List of (chunk_id, score) tuples
        """
        conn = self._get_conn()
        # 清理查询防止 FTS5 注入，并对 CJK 分词
        safe_query = _tokenize_for_fts5(self._sanitize_fts_query(query))
        # FTS5 bm25() 返回负值（越小越好），取反使其越大越好
        rows = conn.execute(
            """
            SELECT c.id, c.content, bm25(chunks_fts) AS rank
            FROM chunks_fts fts
            JOIN chunks c ON c.id = fts.id
            WHERE chunks_fts MATCH ? AND c.state='active'
            ORDER BY rank ASC
            LIMIT ?
            """,
            (safe_query, top_k),
        ).fetchall()
        return [(row["id"], -row["rank"]) for row in rows]

    def search_vector(self, query: str, embed_fn, top_k: int = 20) -> List[Tuple[str, float]]:
        """向量相似度搜索（numpy 加速，fallback 纯 Python）"""
        query_vec = embed_fn(query)

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT text_hash, embedding, dim, chunk_id FROM embedding_cache ORDER BY created_at DESC LIMIT 10000"
        ).fetchall()

        if not rows:
            return []

        chunk_ids = [row["chunk_id"] for row in rows]
        dim = rows[0]["dim"]

        # 反序列化所有 embeddings
        all_vecs = [self._deserialize_embedding(row["embedding"], dim) for row in rows]

        # 计算批量余弦相似度
        try:
            import numpy as np
            q = np.array(query_vec, dtype=np.float32)
            m = np.array(all_vecs, dtype=np.float32)
            q_norm = q / (np.linalg.norm(q) + 1e-8)
            m_norm = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-8)
            sims = np.dot(m_norm, q_norm)
        except ImportError:
            # fallback 纯 Python
            sims = [self._cosine_similarity(query_vec, v) for v in all_vecs]

        # 排序取 top_k
        indices = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)

        results = []
        for i in indices[:top_k]:
            if sims[i] > 0:
                # 现在所有行都有 chunk_id（孤儿已清、backfill 已补），直接用 chunk_id 关联
                cid = chunk_ids[i]
                if cid:
                    results.append((cid, float(sims[i])))
                else:
                    # 防御性 fallback：chunk_id 为空时用 text_hash 反查 chunks.content
                    row = conn.execute(
                        "SELECT content FROM chunks WHERE id = ? LIMIT 1", (chunk_ids[i],)
                    ).fetchone()
                    if row:
                        # 此时已知 content，但不需要返回 content，只需确认 chunk 存在
                        results.append((chunk_ids[i], float(sims[i])))

        return results

    def batch_embed(self, texts: List[str], model: str = "simple") -> List[List[float]]:
        """批量计算并缓存 embedding 向量。"""
        if not texts:
            return []

        results: List[List[float]] = []
        conn = self._get_conn()
        now = time.time()

        for text in texts:
            from memory.utils import compute_embedding_cache_key
            text_hash = compute_embedding_cache_key(text)
            row = conn.execute(
                "SELECT embedding, dim FROM embedding_cache WHERE text_hash = ?",
                (text_hash,),
            ).fetchone()
            if row:
                vec = self._deserialize_embedding(row["embedding"], row["dim"])
                results.append(vec)
            else:
                vec = self._simple_embed(text)
                blob = self._serialize_embedding(vec)
                conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache (text_hash, text, embedding, dim, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (text_hash, '', blob, len(vec), model, now),
                )
                results.append(vec)

        conn.commit()
        return results

    def cache_embedding(self, text: str, embedding: List[float], model: str = "simple", chunk_id: str = None) -> None:
        """缓存 embedding 向量

        R1.4（2026-08-31）：带 chunk_id 写入，方便按 chunk 追溯/清删；兼容旧调用（None）。
        """
        conn = self._get_conn()
        from memory.utils import compute_embedding_cache_key
        text_hash = compute_embedding_cache_key(text)
        blob = self._serialize_embedding(embedding)
        dim = len(embedding)
        conn.execute(
            """
            INSERT OR REPLACE INTO embedding_cache (text_hash, text, embedding, dim, model, created_at, chunk_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (text_hash, '', blob, dim, model, time.time(), chunk_id),
        )
        conn.commit()

    def get_cached_embedding(self, text: str) -> Optional[List[float]]:
        """获取缓存的 embedding"""
        conn = self._get_conn()
        from memory.utils import compute_embedding_cache_key
        text_hash = compute_embedding_cache_key(text)
        row = conn.execute(
            "SELECT embedding, dim FROM embedding_cache WHERE text_hash = ?", (text_hash,)
        ).fetchone()
        if not row:
            return None
        return self._deserialize_embedding(row["embedding"], row["dim"])

    def list_chunks(self, source: Optional[str] = None, limit: int = 100,
                     state: str = "active") -> List[Chunk]:
        """列出文本块（P0-5：默认只取 active，归档/淘汰不进入检索索引）。"""
        conn = self._get_conn()
        if source:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE source = ? AND state = ? ORDER BY timestamp DESC LIMIT ?",
                (source, state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE state = ? ORDER BY timestamp DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        return [
            Chunk(
                id=row["id"],
                content=row["content"],
                timestamp=row["timestamp"],
                importance=row["importance"],
                tags=json.loads(row["tags"]),
                source=row["source"],
                token_count=row["token_count"],
                track=row["track"] if "track" in row.keys() else "default",
                state=row["state"] if "state" in row.keys() else "active",
                superseded_by=row["superseded_by"] if "superseded_by" in row.keys() else "",
                namespace=row["namespace"] if "namespace" in row.keys() else "",
                project=row["project"] if "project" in row.keys() else "",
                host_origin=row["host_origin"] if "host_origin" in row.keys() else "",
                visibility=row["visibility"] if "visibility" in row.keys() else "shared",
                updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
            )
            for row in rows
        ]

    # ── Association API ────────────────────────────────────────

    def create_association(
        self,
        source_id: str,
        target_id: str,
        rel_type: str = "similar",
        metadata: Optional[str] = None,
    ) -> None:
        """创建 chunk 关联关系"""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO associations (source_id, target_id, rel_type, created_at, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, target_id, rel_type, time.time(), metadata or "{}"),
        )
        conn.commit()

    def remove_association(self, source_id: str, target_id: str, rel_type: str = "similar") -> bool:
        """删除关联关系"""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM associations WHERE source_id = ? AND target_id = ? AND rel_type = ?",
            (source_id, target_id, rel_type),
        )
        conn.commit()
        return cur.rowcount > 0

    def get_associations(self, chunk_id: str) -> List[dict]:
        """获取指定 chunk 的所有关联（作为 source 或 target）"""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT source_id, target_id, rel_type, created_at, metadata
               FROM associations
               WHERE source_id = ? OR target_id = ?
               ORDER BY created_at DESC""",
            (chunk_id, chunk_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_associations(self) -> List[Tuple[str, str]]:
        """获取所有 (source_id, target_id) 对（用于去重检查）"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT source_id, target_id FROM associations"
        ).fetchall()
        return [(r["source_id"], r["target_id"]) for r in rows]

    # ── Utility API ────────────────────────────────────────────

    def increment_utility(self, chunk_id: str, delta: float = 0.1, viewer: str = "") -> None:
        """递增 chunk 效用分数（P0.6：按 viewer 计，不影响他人视角）。"""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO chunk_utility (chunk_id, viewer_agent, utility_score, access_count, last_access)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chunk_id, viewer_agent) DO UPDATE SET
                utility_score = utility_score + excluded.utility_score,
                access_count = access_count + 1,
                last_access = excluded.last_access
            """,
            (chunk_id, viewer or "", delta, time.time()),
        )
        conn.commit()

    def get_utility(self, chunk_id: str, viewer: str = "") -> Optional[dict]:
        """获取 chunk 效用统计（P0.6：按 viewer 维度）。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT utility_score, access_count, last_access FROM chunk_utility WHERE chunk_id = ? AND viewer_agent = ?",
            (chunk_id, viewer or ""),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    # ── 内部方法 ──────────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取存储统计"""
        conn = self._get_conn()
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        cache_count = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
        total_tokens = conn.execute("SELECT COALESCE(SUM(token_count), 0) FROM chunks").fetchone()[0]
        assoc_count = conn.execute("SELECT COUNT(*) FROM associations").fetchone()[0]
        return {
            "chunk_count": chunk_count,
            "cache_count": cache_count,
            "total_tokens": total_tokens,
            "association_count": assoc_count,
        }

    def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def cleanup_old_cache(self, max_entries: int = 10000) -> int:
        """LRU 淘汰旧的 embedding 缓存条目，返回删除数量。

        当 embedding_cache 行数超过 max_entries 时，按 created_at 升序
        删除最旧的条目，直到行数降至 max_entries。这实现了近似 LRU
        淘汰策略：最近最少使用的条目（最早 created_at）优先被淘汰。
        """
        conn = self._get_conn()
        current = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
        if current <= max_entries:
            return 0

        to_remove = current - max_entries
        rows = conn.execute(
            "SELECT text_hash FROM embedding_cache ORDER BY created_at ASC LIMIT ?",
            (to_remove,),
        ).fetchall()
        if not rows:
            return 0

        hashes = [row["text_hash"] for row in rows]
        placeholders = ",".join("?" * len(hashes))
        conn.execute(
            f"DELETE FROM embedding_cache WHERE text_hash IN ({placeholders})",
            hashes,
        )
        conn.commit()
        logger.info("LRU-evicted %d old embedding cache entries (max_entries=%d)", len(hashes), max_entries)
        return len(hashes)

    def rebuild_index_atomic(self) -> None:
        """原子化重建 FTS 索引（蓝绿部署）

        创建临时 FTS 表，复制数据，然后原子替换：
        1. 创建 chunks_fts_new 表
        2. 从 chunks 表复制数据到新 FTS 表
        3. 删除旧 chunks_fts 表
        4. 重命名新表为 chunks_fts
        5. 重新创建触发器

        整个过程通过事务保证原子性，避免长时间锁定主表。
        """
        conn = self._get_conn()

        # 在整个操作序列外用显式事务保证原子性
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. 创建临时 FTS 表
            conn.execute("DROP TABLE IF EXISTS chunks_fts_new")
            conn.execute("""
                CREATE VIRTUAL TABLE chunks_fts_new USING fts5(
                    id UNINDEXED,
                    content,
                    tags,
                    source,
                    tokenize='unicode61 remove_diacritics 2'
                )
            """)

            # 2. 从 chunks 表复制数据到新 FTS 表
            conn.execute("""
                INSERT INTO chunks_fts_new(rowid, id, content, tags, source)
                SELECT rowid, id, content, tags, source FROM chunks
            """)

            # 3. 删除旧 chunk_fts 表
            conn.execute("DROP TABLE IF EXISTS chunks_fts")

            # 4. 重命名新表为 chunks_fts
            conn.execute("ALTER TABLE chunks_fts_new RENAME TO chunks_fts")

            conn.commit()
            logger.info("FTS index rebuilt atomically")
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.error("FTS index rebuild failed: %s", e)
            raise

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算两个向量的余弦相似度"""
        n = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(n))
        na = sum(x * x for x in a[:n]) ** 0.5
        nb = sum(x * x for x in b[:n]) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0

    @staticmethod
    def _serialize_embedding(vec: List[float]) -> bytes:
        """序列化 embedding 向量为紧凑二进制格式"""
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _deserialize_embedding(blob: bytes, dim: int) -> List[float]:
        """反序列化 embedding 向量"""
        return list(struct.unpack(f"{dim}f", blob))

    @staticmethod
    def _simple_embed(text: str, dim: int = 128) -> List[float]:
        """快速哈希嵌入（纯 Python fallback，不依赖外部模型）"""
        vec = [0.0] * dim
        tokens = list(text.lower().strip())
        bigrams = [tokens[i: i + 2] for i in range(len(tokens) - 1)]
        for gram in bigrams:
            h = hashlib.md5("".join(gram).encode("utf-8", "ignore")).hexdigest()
            idx = int(h[:8], 16) % dim
            sign = 1 if int(h[8:9], 16) % 2 == 0 else -1
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def _jaccard_similarity(a: str, b: str) -> float:
    """字符级 Jaccard 相似度（中文友好）"""
    set_a = set(a)
    set_b = set(b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)
