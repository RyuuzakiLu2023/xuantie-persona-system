
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
# Any：v2 出处载荷里 source_refs/scope 可以是结构化对象（dict/dataclass），见 Chunk 字段注释
from typing import Any, List, Optional, Tuple

from memory.utils import compute_content_hash, estimate_tokens
from memory.visibility import normalize_visibility
# 单R4（2026-09-15）：正文花括号隔断——全库写入唯一收口用（_validate_content 末尾）
from memory.text_sanitize import neutralise_template_braces
# ★B-补（2026-10-13）：provenance 落库序列化要用 MemoryEntry._json_safe_ref。
#
# 【为什么放模块顶层，不放函数内（改前先读，别顺手挪进去）】：
#   1) **依赖是单向的，不可能成环**：wal_engine.py 只 import `.utils` / `.diagnostics`
#      （两者都不 import vector_store），**没有** vector_store ← wal_engine 的反向边；
#      父包 memory/__init__.py 也是"先加载 wal_engine、再加载 vector_store"。
#      ⇒ 顶层 import 时 wal_engine 模块对象已完整，拿得到 MemoryEntry。
#      实测 4 种入口顺序（谁先 import）均无环：见 _evidence\B3-import-cycle.txt。
#   2) **顶层 import = 导入期 fail-fast**：一旦将来有人删了这个名字，`import vector_store`
#      当场炸，而不是拖到"第一次写带 provenance 的记忆"才 NameError——后者正是本次
#      要修的坑形态：平时不触发、判据全绿，直到真有结构化载荷才炸（假绿）。
#      放函数内会让同一类事故**再藏一轮**，所以【不】挪进 _normalize_chunk。
#   3) 与文件既有风格一致（上面三行同样用 `from memory.xxx import ...` 顶层写法）。
# ⚠️ 改了会怎样：若将来 wal_engine 真的反向 import 了 vector_store，这里会立刻
#    ImportError（而不是静默半初始化）——那时应改为"函数内延迟 import + 类型注解走字符串"。
from memory.wal_engine import MemoryEntry

logger = logging.getLogger(__name__)

# P1-L / G-01：tier 白名单（分层库两级检索池）
TIERS = frozenset({"raw", "refined"})


def _normalize_tier(t):
    """tier 归一：None/非 str → 'raw'；str → strip + lower（'  REFINED  ' → 'refined'）。"""
    if t is None:
        return "raw"
    if isinstance(t, str):
        return t.strip().lower()
    return "raw"


# v2（2026-09-04）：专名自定义词典——按 jieba 模块实例追踪注册（线程安全），首批 6 词
#（AB 实测暴露"飞书"类切碎问题）。用 id() 追踪：jieba 实例被替换/重载（如测试里 sys.modules
# jieba=None 后恢复成新实例）时自动对新实例重注册，旧实例不重复注册。
_JIEBA_REGISTERED = None
_JIEBA_LOCK = threading.Lock()
_JIEBA_CUSTOM_WORDS = tuple(json.loads(os.environ.get("XTMEM_TOKENIZER_WORDS", '["XTMEM", "Ollama", "bge-m3"]')))


def _ensure_jieba_dict():
    """v2（2026-09-04）：对当前 jieba 实例注册专名词典（add_word）；失败静默（不挡写入）。"""
    global _JIEBA_REGISTERED
    try:
        import jieba as _jb
    except Exception:
        return  # jieba 不可用时由 _tokenize_for_fts5 的 ImportError 降级兜底
    with _JIEBA_LOCK:
        if _JIEBA_REGISTERED == id(_jb):
            return
        for _w in _JIEBA_CUSTOM_WORDS:
            try:
                _jb.add_word(_w)
            except Exception:
                pass
        _JIEBA_REGISTERED = id(_jb)


def _tokenize_for_fts5(text: str) -> str:
    """对 CJK 文本做分词，使 unicode61 tokenizer 能正确切分中文。

    唯一口径 = jieba 词级精确模式（v2，2026-09-04 D1'；依据 AB 实测短语查询 3.1x 胜出），
    版本标识 tok-jieba-v2 由 xtmem_meta.tokenizer_version 防呆；
    ImportError（环境缺 jieba）时降级单字+bigram 仅作兜底——版本锁触发前保写入不崩。
    """
    import re as _re
    has_cjk = bool(_re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', text))
    if not has_cjk:
        return text

    # jieba 精确模式（v2 唯一口径；专名词典首调前注册）
    try:
        import jieba
        _ensure_jieba_dict()
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
        pass  # 降级兜底

    # 降级兜底：单字+bigram
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


TOKENIZER_VERSION = "tok-jieba-v2"  # v2（2026-09-04 D1'/D2'）：jieba 词级口径（AB 实测 3.1x 胜出）；bigram 仅兜底


# ══ ★B-补（2026-10-13）：provenance 八列的"唯一列名清单" ═══════════════════════
# 为什么要有这个常量（改前先读）：
#   store_chunk 与 batch_store_chunks 是**两条独立的 INSERT 语句**（本文件修改铁律 1：
#   "列清单/VALUES/ON CONFLICT/元组 四者必须同步"）。原来它们靠人工保持同步——
#   而本件要修的坑恰恰就是这八个 provenance 列**只加进了 schema、两条 INSERT 都没带**，
#   于是 Chunk 上有值、DB 里全 NULL（判据只查"列存在" ⇒ 假绿）。
#   把列名收口成一个元组、SQL 片段由它生成，加列时只需改这一处，
#   从机制上消掉"两条语句不同步"的复发面（比再写一遍注释靠谱）。
# ⚠️【不】要把它拆回两条语句里硬编码：那正是本坑的成因形态。
# ⚠️ 顺序即是 bind 顺序（`_prov_bind` 严格按这个顺序取值），改顺序两条语句会一起错、
#    且读侧不认列名——所以这个元组**只能追加、不能重排**。
_PROV_CHUNK_FIELDS = (
    "speaker", "subject", "source_refs", "scope",
    "valid_from", "valid_until", "confirmation_type", "provenance_status",
)
# 下面三片段全部由上面这一个元组派生 ⇒ 列名 / 占位符 / ON CONFLICT 三者在结构上不可能脱钩。
_PROV_COLS_SQL = "".join(", " + c for c in _PROV_CHUNK_FIELDS)
_PROV_VALUES_SQL = "".join(", ?" for _ in _PROV_CHUNK_FIELDS)
_PROV_UPSERT_SQL = ", ".join("%s=excluded.%s" % (c, c) for c in _PROV_CHUNK_FIELDS)


def _prov_bind(chunk):
    """provenance 八列的绑定值元组——顺序与 `_PROV_CHUNK_FIELDS` **严格一致**。

    ★B-补（2026-10-13）：**source_refs / scope 的 JSON 序列化唯一落点就在这里**。
    为什么放这而不是放 `_normalize_chunk`（改前必读，别顺手挪回去）：
      `_normalize_chunk` 会拿到**同一条 Chunk**被调用多次（store_chunk 一次、
      调用方重存再一次），若在那里把结构化 list 改写成 JSON 文本，第二次调用就**无法
      区分**"已是编码后的文本"与"调用方本来就传了个字符串"，对 scope 会编成
      `"\\"{...}\\""`（双重编码，读回来是一段带引号的字符串）。
      本函数每次调用都从 Chunk 上的**结构化原值**出发现编，天然幂等。
    归一路径与 WAL 侧同一个函数（`MemoryEntry._json_safe_ref`），保证两侧同一份口径：
      - dataclass 实例（SourceRef）→ asdict 递归；裸 json.dumps 会 TypeError；
      - **空 source_refs → None（NULL，不是 "[]"）**：与 D_PROV_CHUNK_COLS
        「NULL=没带」的缺省语义对齐，读侧再还原成 []，往返相等且语义不撒谎；
      - scope=None → NULL（没带），非 None → json.dumps（含字符串，落成带引号的 JSON 串，
        读侧 loads 回同一个字符串，**不**会被悄悄读成 int/其他类型）。
    ⚠️ 已是 str 的 source_refs 按"调用方已自行 JSON 化"原样透传（兼容旧调用形态），
       【不】二次 dumps（否则落成 "\"[...]\"" 这种带引号字符串，B2 探针实测过该形态）。
    返回值恒为 8 元组，缺字段一律 None ⇒ 落库即 NULL（契约 §5「缺就是缺」）。
    """
    refs = getattr(chunk, "source_refs", None)
    if isinstance(refs, str):
        refs_sql = refs            # 已是 JSON 文本：透传，不重复编码
    elif refs:
        refs_sql = json.dumps([MemoryEntry._json_safe_ref(x) for x in refs],
                              ensure_ascii=False)
    else:
        refs_sql = None            # 空/未带 → NULL（【不】写 "[]"）
    scope = getattr(chunk, "scope", None)
    scope_sql = (json.dumps(MemoryEntry._json_safe_ref(scope), ensure_ascii=False)
                 if scope is not None else None)
    # ⚠️ 用**名字→值**的字典、再按 _PROV_CHUNK_FIELDS 顺序取值，【不】写裸位置元组——
    #    裸元组一旦与列名清单错位就会把 speaker 静默写进 subject（错位落库比报错更难查）。
    #    这样"加列/改顺序"时两边永远同源，缺名字当场 KeyError（fail-fast）。
    by_name = {
        "speaker": getattr(chunk, "speaker", None),
        "subject": getattr(chunk, "subject", None),
        "source_refs": refs_sql,
        "scope": scope_sql,
        "valid_from": getattr(chunk, "valid_from", None),
        "valid_until": getattr(chunk, "valid_until", None),
        "confirmation_type": getattr(chunk, "confirmation_type", None),
        "provenance_status": getattr(chunk, "provenance_status", None),
    }
    missing = [c for c in _PROV_CHUNK_FIELDS if c not in by_name]
    if missing:
        raise ValueError("provenance 列缺少绑定值（列名与 _prov_bind 不同步）: %r" % (missing,))
    return tuple(by_name[c] for c in _PROV_CHUNK_FIELDS)


class _DerivedGateRejected(Exception):
    """★F1（2026-10-14 第二轮返修）：写事务内复验发现"授权依据已变"。

    为什么用**异常**而不是返回值把这个信号传出来：
      权威复验发生在 _store_chunk_conn 的事务体内部（INSERT 之前），
      那里已经在写事务里了——必须让外层**回滚**整个事务，而不是"打印一行、继续写"。
      Python 里把"必须回滚"表达成异常，才不会有人顺手 return False 继续往下走。
    ⚠️ 它是**预期控制流**，不是事故：store_chunk 捕获后回滚并返回 False。
      调用方看到的是"这条没落库"，与锁外快速拒绝的表现一致。
    """

    def __init__(self, chunk_id: str = ""):
        super().__init__("derived gate rejected in write transaction: %s" % (chunk_id,))
        self.chunk_id = chunk_id


def _tokenizer_version_ok(conn) -> bool:
    """S2（2026-09-03）：tokenizer 版本防呆校验（不匹配拒绝 FTS 写入 + warning，不静默）。

    - xtmem_meta 无 tokenizer_version 行 → 首次播种当前版本并放行（存量库首写不误伤）；
    - 值 == TOKENIZER_VERSION → 放行；
    - 值 != 当前版本 → log.warning + 拒绝（调用方不再写 chunks_fts）。
    """
    row = conn.execute("SELECT value FROM xtmem_meta WHERE key='tokenizer_version'").fetchone()
    cur = str(row[0]) if row else None
    if cur is None:
        conn.execute(
            "INSERT OR IGNORE INTO xtmem_meta(key, value) VALUES('tokenizer_version', ?)",
            (TOKENIZER_VERSION,),
        )
        return True
    if cur == TOKENIZER_VERSION:
        return True
    logger.warning(
        "tokenizer_version mismatch: db=%r want=%r — refusing FTS write", cur, TOKENIZER_VERSION
    )
    return False


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
    # ══ ★M01 第三轮（2026-10-13）R2 补件：v2 出处/归属载荷（B 路）══════════════
    # 为什么要往 Chunk 上加：验收 v3 `R2-database-provenance` 实测——这些字段写进了 WAL，
    #   但 Chunk 无字段、chunks 无列，DB 侧读不回完整载荷。契约 §5 要求
    #   **WAL 与 DB 从同一份规范化载荷构造**，所以 Chunk 必须能承载它们。
    # ⚠️ 缺省一律 None / 空列表（**缺就是缺**），【不】编默认值造事实——
    #    旧行/旧请求没有这些值就是 None，绝不拿 "" 冒充"确实没有说话人"。
    # ⚠️ `source_refs` / `scope` 在内存里保持**结构化**（list / 任意 JSON 值），
    #    只有 SQLite bind 那一刻才 json.dumps 成文本（见 _normalize_chunk 与 _row_to_chunk）。
    #    为什么不像 source_ids 那样在字段上就存 JSON 文本：① R2 的判据就是"结构化可往返"，
    #    字段上先烧成文本会让"读回来是不是原对象"变成一次额外解析；② WAL 侧
    #    （MemoryEntry.source_refs）本轮已定为结构化，两侧口径必须一致，否则又成"两套口径"。
    speaker: Optional[str] = None
    subject: Optional[str] = None
    source_refs: List[Any] = field(default_factory=list)
    scope: Optional[Any] = None
    valid_from: Optional[float] = None
    valid_until: Optional[float] = None
    confirmation_type: Optional[str] = None
    # verified / legacy_unverified；None = 旧行（v2 之前），读取侧据此认出"出处未经验证"。
    # ⚠️ 这一列是 DB 侧"这条到底带没带 provenance"的**唯一权威表征**——
    #    source_refs 为空列表时无法区分"显式没有出处"与"根本没带这个字段"，靠它兜住。
    provenance_status: Optional[str] = None
    operation_id: Optional[str] = None
    observation_id: Optional[str] = None
    request_hash: Optional[str] = None

    def __post_init__(self):
        # R1.1（2026-08-31）：importance 钳制 0..1
        self.importance = max(0.0, min(1.0, float(self.importance)))
        # P4.9-a 血缘强制闸（derived_from 非空 → source_ids 必须非空）已在 P1-H 收口到
        # 写侧 _normalize_chunk —— 不能在 __post_init__ 拦，否则读路径（get_chunk/list_chunks
        # 构造历史遗留行）会把只读也炸掉。写路径仍有同语义拦截。


# ── FTS5 查询消毒双防线（2026-09-13 顶模"第一刀"件①）────────────────────
# "剥离正则"：MATCH 前把一切非词字符剥成空格。字符类坑位契约（⚠️ 改前必读）：
#   1. `-` 只许放类末尾（紧贴 ] 才是字面量）——放中间会和相邻字符构成区间。
#      B0-3 血案：补反引号时把类拼成 `\'\\-` `，正则引擎看到 \` (92) 到 \` (96) 的
#      区间，连字符 - 本尊 (45) 漏在剥离集外 → 残 - 进 MATCH 被 FTS5 解析成
#      NOT/减法语义 → "no such column: B" → 词法腿整条静默死亡（取证 10 条技术
#      query 中招 7 条）。
#   2. `_`(95) 必须显式在列——旧 bug 区间曾"意外"剥掉 92–96（含 _），拆区间后若不补，
#      下划线 query（session_search 这类）会从"分词 AND"变成 FTS5 短语相邻匹配，
#      召回静默变严。
_FTS_SANITIZE_RE = re.compile(r'["*():^.,;!?\uFF1B\uFF0C\uFF01\uFF1F\uFF1A\u3001<>{}\[\]|\\/%~@$&+=\'`#_-]')
# "二次防线"：剥离正则之后的最后一道闸——分词产物若仍残留 FTS5 语法字符
#   （- 短语/NOT、() 分组、: 列过滤、^ 前缀锚、" 短语、* 前缀通配、' 引号），一律转空格。
#   为什么有剥离正则还要它：剥离集是人工维护的单点，历史上已两犯（B2 漏点号/逗号、
#   B0-3 区间漏连字符）——防线语义 = 宁可少召不可炸腿（炸腿 = 词法腿整条静默消失）。
#   ⚠️ 它只拦符号不拦字母：大写 AND/OR/NOT/NEAR 是合法 FTS5 关键词，穿透两层防线
#   的畸形组合（如 "OR OR OR"）会真抛错，由 hybrid_retriever 的 fts5_fallback 留痕兜底。
_FTS_SYNTAX_RE = re.compile(r'[-():^"\'*]')


# ════════════════════════════════════════════════════════════════════
# R01 · RQ01（2026-10-21 小D）：长问句的 FTS5 查询分层
#
# 这是什么：把分词后的查询词，编排成三套"从严到宽"的 MATCH 表达式。
# 为什么要有：FTS5 里空格分隔 = 隐式 AND。旧实现直接把全部分词拼进去，
#   于是「书房 C03 数字 NULL 裁决进展」要求六个词同时出现 ⇒ 0 命中；
#   而答案（C03 相关记录）明明在库里。这不是"全文检索坏了"，
#   是"把整句话当成一个必须全中的过滤条件"。
#
# 三套方案（返回值列表，顺序即优先级）：
#   ① 全 AND           —— 与改前逐字节一致，非空就赢，既有行为零漂移
#   ② 稀有词 AND (其余 OR) —— 主题/标识符是硬约束（C03 必须在），修饰词只需沾一部分
#   ③ 其余 OR + 最少命中数 —— ②仍空时的宽召回；要求 ≥2 个内容词命中，
#                            全 OR 会把"只沾一个词"的异题拉进来（RQ01 明令禁止）
#
# ⚠️【不】把 ②③ 做成常量开关就完事：真正的护栏是"命中词数"这个可数的量，
#   它让"宽召回"有下限，而不是把门槛一降到底。
# ⚠️【不】在这里查库算词频：本函数必须是纯字符串处理（调用方持有连接，
#   且它在检索热路径上——每次检索多一次全库 count 是白烧）。
#   稀有性由 memory/bundle_selector._is_generic_anchor 的黑名单负责。
# ════════════════════════════════════════════════════════════════════

def _fts_plan_tokens(safe_query: str):
    """把已消毒的查询串拆成去重词元（保持原序）。空串/仅 * 返回 []。"""
    if not safe_query or safe_query.strip() == "*":
        return []
    seen = set()
    toks = []
    for t in str(safe_query).split():
        key = t.lower()
        if key and key not in seen:
            seen.add(key)
            toks.append(t)
    return toks


def _fts_topic_tokens(tokens):
    """挑出"主题/标识符"这类必须命中的词：含数字、或含 ASCII 字母、或长度 >= 4 的非通用词。"""
    try:
        from memory.bundle_selector import _is_generic_anchor
    except Exception:
        def _is_generic_anchor(_t):
            return False
    topic = []
    for t in tokens:
        core = t.strip('._-')
        if not core:
            continue
        if _is_generic_anchor(core):
            continue
        has_digit = any(c.isdigit() for c in core)
        has_ascii = any(c.isascii() and c.isalpha() for c in core)
        if has_digit or has_ascii or len(core) >= 4:
            topic.append(t)
    return topic


def _build_fts_plans(safe_query: str):
    """生成从严到宽的查询方案列表：[{expr, min_hits, topic}]（见上方总说明）。

    min_hits = 这条候选【正文里至少命中几个查询词】才算数（调用方在 Python 侧数）。
      ① 全 AND  → 0（MATCH 已保证全中，不必再数）
      ② 主题AND(其余OR) → len(topic)（主题词一个都不能少）
      ③ 全词 OR  → 2（或词元数，取小）——这是"禁止全 OR 扩大异题"的硬护栏：
         只沾一个词的记录不算命中，必须至少命中两个词。
    topic 字段把主题词带给调用方计数用。

    返回 [plan1] 或 [plan1, plan2, plan3]；单 token / 空查询只返回一项，
    调用方因此走的就是"原样"路径（逐字节不变）。
    """
    toks = _fts_plan_tokens(safe_query)
    if len(toks) <= 1:
        return [{"expr": safe_query or "*", "min_hits": 0, "topic": []}]
    plan_and = " ".join(toks)
    plans = [{"expr": plan_and, "min_hits": 0, "topic": []}]
    topic = _fts_topic_tokens(toks)
    others = [t for t in toks if t not in topic]
    if topic and others:
        # ② 主题 AND (其余 OR…)：主题词是硬约束，修饰词只需沾一部分
        plans.append({
            "expr": "(" + " ".join(topic) + ") AND (" + " OR ".join(others) + ")",
            "min_hits": len(topic),
            "topic": list(topic),
        })
    if topic:
        # ③ 只留主题/标识符 —— 这是"长句为什么查不到"的正解：
        #   修饰词（数字/NULL/裁决/进展…）一个都不要求，但主题词必须全在。
        #   ⚠️ ③ 比 ② 更宽，但【仍不是全 OR】：异题记录里没有 C03，照样进不来。
        plans.append({
            "expr": " ".join(topic),
            "min_hits": len(topic),
            "topic": list(topic),
        })
    else:
        # 整句都是通用词（无主题可辨）才退到全词 OR，且必须配 min_hits>=2——
        #   只沾一个词就算命中就是"全 OR 扩大异题"，正是 RQ01 后半句禁止的形态。
        plans.append({
            "expr": " OR ".join(toks),
            "min_hits": min(2, len(toks)),
            "topic": [],
        })
    seen = set()
    out = []
    for pl in plans:
        if pl["expr"] not in seen:
            seen.add(pl["expr"])
            out.append(pl)
    return out


def fts_search_bm25(conn, query, top_k: int = 20, viewer: str = None):
    """G2（2026-09-04）：SQL FTS5 词级 BM25 查询——收口实现，VectorStore.search_bm25 与
    HybridRetriever 只读复用（单点 SQL 防漂移）。返回 [(chunk_id, score)]，score 越大越好。
    过滤：state='active'（SQL）+ visibility_ok(viewer)（python，含 shared-with 名单）。

    ★M03（2026-10-13）缺省 viewer 从 '__admin__' 改为 **None（= 按空身份判，只放行 shared）**。
    为什么改（实测可观测的旁路，非理论隐患）：
      旧缺省是 '__admin__'，于是"忘了传 viewer" == "以管理员身份检索"。
      实测（<TEMP>\\DSH\\m03-probe7d.py）：
        不传 viewer        → 命中 1 条**他人 private**  🔴
        viewer=''（空身份） → 命中 0 条（fail-closed）
        viewer='dsh'       → 命中 0 条
      ⇒ 越权只需要"少传一个参数"，而少传参数在重构中是最常见的动作。
    ⚠️ 语义澄清（改前必读，别"顺手"改回上帝视角）：
      viewer=None 与 viewer='__admin__' **不是**同一个意思。
        None    = 调用方没声明身份 → 按最保守处理（只 shared）
        '__admin__' = 调用方**显式**声明管理员身份（凭证闸在 CLI/REST 层）
      要上帝视角就老老实实传 '__admin__'——让它出现在调用点、能被 review 看见。
    ⚠️ 本函数内 base['viewer'] 仍用 `viewer or "__admin__"`？**不行**——见上面这条。
      下半段 _fts_search_bm25_legacy 的兼容包装保留原样供旧调用方，但主实现已收口。
    """
    from memory.visibility import visibility_ok
    # B2（2026-09-07，小D）：消毒缺口修复——旧表 ['\"*():^'] 漏了点号/逗号等，
    #   "agents.md" / "354,469" / "hermes0.20.5" / "gemini3.8flash" 这类 query 直接触发
    #   fts5 syntax error → BM25 腿静默死亡（错题本：静默吞异常）。unicode61 分词器
    #   在索引侧本就把这些符号当分隔符，查询侧对齐剥掉即恢复词级命中。
    # B0-3（2026-09-11）：字符类补 `#` 与反引号。原类漏这两个 → "issue #40 的根因"、
    #   "`open_db.py` 是啥" 这类**技术类高频 query** 原样送进 FTS5 → syntax error →
    #   被 hybrid_retriever.py:540 吞成 [] → fts5_mode="on" 时词法腿整条消失、只剩向量腿。
    #   （`#` 在正则字符类内无需转义；反引号同理。）
    # 2026-09-13（顶模"第一刀"件①）：B0-3 接反引号时把类拼成 \` (92)–\` (96) 区间，
    #   连字符本尊漏剥（机制见 _FTS_SANITIZE_RE 上方坑位契约）。修法三件：`-` 置类末尾、
    #   `_` 显式入列、正则提为模块级常量 + 二次防线 _FTS_SYNTAX_RE。
    sanitized = _FTS_SANITIZE_RE.sub(' ', query)
    sanitized = ' '.join(sanitized.split())
    safe_query = _tokenize_for_fts5(sanitized if sanitized else "*")
    # 二次防线（最后一道闸，契约见 _FTS_SYNTAX_RE 注释）：残留语法字符转空格重造；
    #   空串兜底 "*" 与上行同契约
    if _FTS_SYNTAX_RE.search(safe_query):
        safe_query = ' '.join(_FTS_SYNTAX_RE.sub(' ', safe_query).split()) or "*"
    # ── R01 · RQ01（2026-10-21 小D）：长问句不再"缺一个词就查空" ────────────────
    # 病灶（核查结论.md §一.1，生产只读副本实测）：
    #   本函数把分词结果【原样】塞进 FTS5 MATCH。空格分隔 = 隐式 AND ⇒ 长问句要求
    #   "每个词都出现"，少一个就 0 命中：
    #     「书房 C03 数字 NULL 裁决进展」→ 0 条（而 C03 单独查有 9 条）
    #     「H3 接龙 lowres_scale 发虚」  → 0 条（而 lowres_scale 单独查有 1 条）
    #   于是"答案明明在库里、词法腿却整条哑掉"，只能靠向量腿兜——而向量腿的门槛
    #   又放得很松（见 hybrid_retriever 的准入改造）。
    #
    # 修法：**分层退让**，不改第一个方案的语义（现状逐字节保留 = 单测不受扰动）：
    #   方案①（原样）  全部词 AND                  ← 与改前完全一致，优先执行
    #   方案②（主题约束）稀有词 AND (内容词 OR…)   ← 长句漏查的主治：主题/标识符仍是
    #                    硬约束（C03 必须在），通用修饰词只要求命中一部分
    #   方案③（宽召回）  内容词 OR + 最小重叠数    ← ②仍空时才用，且要求命中 ≥2 个
    #                    内容词（全 OR 会把"只沾一个词"的异题拉进来，RQ01 明令禁止）
    #   ⚠️ 第②③方案【只】在①返回空时才跑——非空路径的 SQL、顺序、条数一字不动，
    #      这样 M03/recall_performance 那些既有断言不会被"优化"撞红。
    #   ⚠️【不】把①直接换成 OR：那等于自己把"异题也能命中"请回来，正好是 RQ01 的后半句。
    _fts_plans = _build_fts_plans(safe_query)
    # 确保 row_factory 为 Row（conn 可能未设置）
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    # ★★R3-2（2026-10-14 独立验收返修）：**权限过滤必须发生在取限额之前**。
    #
    # 病灶（实测，见 m03-review/evidence/integration-results.json 的
    #      SPEC16-fts-permissions-before-limit）：
    #   旧实现一条 SQL 取 `LIMIT max(50, top_k*8)`，**之后**才在 Python 里按 viewer 过滤。
    #   于是"排名更高但读不到"的行会把限额吃光，**可读的行被挤掉**：
    #   实测 60 条他域 private + 1 条可读 shared、top_k=1 ⇒ 返回空（可读的明明存在）。
    #   这直接违反 §16「FTS 限额不能在权限过滤前取 top-k」。
    #
    # 修法：**翻页取，直到攒够 top_k 条"过滤后"的结果**（或行取尽）。
    #   为什么不把权限塞进 SQL 的 WHERE：viewer 的 shared-with 名单匹配在 SQL 里只能写成
    #   LIKE 近似（'shared-with:a,b' 里匹配 'a' 会误命中 'ab'），近似式放 WHERE **会漏**
    #   可读行；只能拿它做"超集预筛"，而真正判定仍得逐行调 visibility_ok。
    #   与其写个看着像优化的近似，不如老老实实翻页——语义正确优先。
    # ⚠️【不】把 LIMIT 改大（如 top_k*100）来"缓解"：那是把病换成"绝大多数情况看不见"，
    #   一旦他域 private 命中超过新限额，同一个漏可读的 bug 原样复现。
    _page = max(50, top_k * 8)
    out = []
    # R01·RQ01：按方案①②③依次尝试，**先到先用**。①为空才走②，②为空才走③。
    #   每套方案各自从 offset 0 翻页——不同 MATCH 表达式的排名不能混在一起翻页。
    #   ⚠️ 方案①（原样 AND）一字未改：它非空时本函数行为与改前逐字节一致，
    #      既有 M03 权限翻页断言（SPEC16-fts-permissions-before-limit）不受影响。
    #   ⚠️ 整个 for 仍在同一 try 内：row_factory 必须还原（见下面 finally 的说明）。
    try:
        for _plan_idx, _plan in enumerate(_fts_plans):
            _match_expr = _plan["expr"]
            _min_hits = int(_plan.get("min_hits") or 0)
            _topic = _plan.get("topic") or []
            _tokens_for_hits = [t for t in _fts_plan_tokens(safe_query)]
            _offset = 0
            _plan_out = []
            # 需要数命中词的方案（②③）必须多取一些行再筛：MATCH 只保证"至少中一个"，
            #   真正的门槛在 Python 侧。
            #
            # ★R01-FIX1（2026-10-21，独立验收 F2-A）：
            #   我上一版在这里给「不需要数命中词」的方案（即方案①原样 AND）加了
            #   _scan_cap = _page*2 ——**这是错的**：它把 R3-2 花力气做的"翻页直到
            #   攒够可读结果"截断了。实测：130 条 dsh/private 同词记录排在前面、
            #   后面 1 条 zcode/shared 答案，top_k=1 时最多只扫 100 行 ⇒ 返回 []，
            #   而旧基线（无上限、一直翻）能取到那条 shared 答案。
            #   也就是说：我为了防"无限翻页"把**可读答案**给丢了，属于用性能换正确性，
            #   正是本单 §5 明令禁止的「以丢可读答案收口」。
            #
            # 正确做法：**上限要按"已扫描行数"兜底，但必须在达到 top_k 前持续翻页**，
            #   并且上限必须高于"可能的不可读前缀"——真实库里不可读前缀没上界，
            #   所以这里改成「翻页总量上限 = max(页大小, 需要量) 的若干倍，且**未攒够
            #   就继续翻**，直到行取尽」：即恢复原语义，只用 _scan_cap 做大库保护闸，
            #   且该闸的判定是「已翻页数」而非「已返回条数」。
            # 具体：_scan_cap 只用于**方案②③**（它们本来就要多扫才能数命中词）；
            #   方案①（原样 AND）不设上限，与改前逐字节一致。
            #   ⚠️【不】给方案①再加任何行数上限——除非能证明"必然能翻到可读答案"，
            #      否则就是又一次拿正确性换延迟。
            _need_scan = _min_hits > 0
            _scan_cap = _page * 8 if _need_scan else None
            _scanned = 0
            while len(_plan_out) < top_k and (_scan_cap is None or _scanned < _scan_cap):
                rows = conn.execute(
                    """
                    SELECT c.id, c.content, c.namespace, c.visibility, bm25(chunks_fts) AS rank
                    FROM chunks_fts fts
                    JOIN chunks c ON c.id = fts.id
                    WHERE chunks_fts MATCH ? AND c.state='active'
                    ORDER BY rank ASC
                    LIMIT ? OFFSET ?
                    """,
                    (_match_expr, _page, _offset),
                ).fetchall()
                if not rows:
                    break
                _scanned += len(rows)
                for row in rows:
                    # ★M03：viewer=None（未声明身份）直接喂给 visibility_ok → 空身份 fail-closed，
                    #   只放行 shared。【不】在这里 `viewer or "__admin__"`——那正是被修掉的旁路。
                    if not visibility_ok(str(row["visibility"] or ""), str(row["namespace"] or ""), viewer or ""):
                        continue
                    if _need_scan:
                        _body = str(row["content"] or "").lower()
                        # 主题词必须一个不少（②的硬约束）——这是"不能只沾修饰词就进门"
                        if _topic and not all(t.lower() in _body for t in _topic):
                            continue
                        _hits = sum(1 for t in _tokens_for_hits if t.lower() in _body)
                        if _hits < _min_hits:
                            continue
                    _plan_out.append((row["id"], -row["rank"]))
                    if len(_plan_out) >= top_k:
                        break
                if len(rows) < _page:
                    break          # 已到结果集末尾，再翻也是空
                _offset += _page
            if _plan_out:
                out = _plan_out
                # 记住这一跳实际用的是第几方案：CLI/hook 的 basis 靠它说明"这轮为什么能查到"
                globals()["_LAST_FTS_PLAN"] = _plan_idx
                break
    finally:
        # ⚠️ 必须还原：conn 是引擎常驻单例连接，改坏它的 row_factory 会波及后续所有查询
        #    （别处按元组/Row 取值的地方会静默错位）。原实现就是 try/finally 的，返修别丢。
        conn.row_factory = old_factory
    return out

class VectorStore:
    """SQLite 持久化向量存储

    表结构：
      - chunks: 存储文本块
      - chunks_fts: FTS5 全文索引（BM25 搜索）
      - embedding_cache: embedding 缓存
      - associations: chunk 间关联关系
      - chunk_utility: chunk 效用追踪
    """

    DEFAULT_DB_PATH = __import__("memory.public_runtime", fromlist=["db_default"]).db_default()

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_lock = threading.Lock()
        # R01·RQ05（2026-10-21）：效用读取语义开关——True=召回/展示只记账不加分（新口径）；
        # False=回到"累加即加分"的旧口径（灰度/回滚用，验收可对比两份行为）。
        self.utility_read_semantic = True
        # ★G2：写作用域用的 SAVEPOINT 序号（同一连接可重入，名字必须互不相同）。
        self._sp_seq = 0
        # ★G2（第五轮）：收口/回滚失败后连接状态未知 ⇒ 置真，后续写入一律明确报错，
        #   绝不允许"返回一个没落盘的成功"（见 _mark_conn_unusable）。
        self._conn_broken = False
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # ★G2（第五轮）：收口/回滚失败后事务残留状态**未知**，此时下一条"正常"写入
        #   会把残留事务当成调用方事务、只 RELEASE 就返回成功 ⇒ 假成功。
        #   宁可让后续调用明确炸掉，也不许它谎报落盘。恢复方式是重建 VectorStore 实例。
        if getattr(self, "_conn_broken", False):
            raise sqlite3.DatabaseError(
                "VectorStore 连接在一次失败的写作用域收口后已被标记为不可继续安全使用"
                "（M03 G2）；请重建 VectorStore 实例后再写入")
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    # R5（P6 波1）：引擎核心连接收编 open_db——schema 版本闸+降级自兜底
                    # 从 memory.open_db import 到函数内防循环依赖
                    from memory.open_db import open_db
                    self._conn = open_db(self.db_path, timeout=30, check_same_thread=False)
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
        # ⚠️ 必要升级失败意味着本实例结构不完整；关闭连接并原样抛出，绝不继续初始化或接单。
        try:
            from memory.schema import ensure_xtmem_schema
            ensure_xtmem_schema(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                # ⚠️ 清理失败不能掩盖真正的 schema 升级异常；实例仍标不可用。
                pass
            finally:
                self._conn = None
                self._conn_broken = True
            raise

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

        # S2（2026-09-03）：xtmem_meta——tokenizer_version 防呆落点（方案 D2，实测 xtmem_state 是三方同步表塞不进）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS xtmem_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )

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
                orphan INTEGER DEFAULT 0,
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

        # P1-C / R-07：embedding_cache 加 orphan 标记（NULL chunk_id 且反查不到 chunks 的历史行）
        try:
            _ec_cols = [r[1] for r in conn.execute("PRAGMA table_info(embedding_cache)").fetchall()]
            if "orphan" not in _ec_cols:
                conn.execute("ALTER TABLE embedding_cache ADD COLUMN orphan INTEGER DEFAULT 0")
                logger.info("Added orphan column to embedding_cache")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_chunk ON embedding_cache(chunk_id)")

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
        # D1-1（2026-09-09）裁决三列：adjudicator=谁裁的（模型名/人工/脚本指纹）、
        # policy_version=按哪版策略、decision_reason=为什么。缺这三列，D2 冲突曝光无从归责。
        # 为什么补在这里而不是只写 schema.py：ensure_xtmem_schema 在本表 CREATE【之前】跑
        # （_init_db 307 行），新库那次 ALTER 必然落空——两边都写才覆盖新库+老库两条路。
        for _c, _t, _d in (("visibility","TEXT","'shared'"),("valid_from","REAL","0"),("valid_to","REAL","0"),("shadow_state","INTEGER","1"),("adjudicator","TEXT","''"),("policy_version","TEXT","''"),("decision_reason","TEXT","''")):
            if _c not in _evo_cols:
                conn.execute("ALTER TABLE memory_evolution ADD COLUMN {} {} DEFAULT {}".format(_c, _t, _d))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evo_source ON memory_evolution(source_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evo_target ON memory_evolution(target_id)")

        # 466c（2026-09-13）conflict_candidate 补 decision_reason：466 清创单起
        # record_conflict_result（conflict.py INSERT 17 列）必写此列，schema.py 的
        # CREATE TABLE 曾漏列——手工 ALTER 过的生产库活着、新建库一写即炸（错题本#52）。
        # 与上面 D1 段同理：schema.py 管新库、这里管存量老库，两边都写才两条路都覆盖。
        _cc_cols = [r[1] for r in conn.execute("PRAGMA table_info(conflict_candidate)").fetchall()]
        if _cc_cols and "decision_reason" not in _cc_cols:
            conn.execute("ALTER TABLE conflict_candidate ADD COLUMN decision_reason TEXT DEFAULT ''")

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
    # ⚠️ 2026-09-06 攻击样本演练（波2 硬闸债）发现真缺口：原 \w 含下划线，
    # "i_n_o_r_e all previous instructions" 归一后仍带下划线 → 绕开所有正则。
    # 修法：显式字符类替代 \w（字母数字中文，不含 _），下划线随其他标点一起被剥。
    _INJECTION_NORMALIZE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]", re.UNICODE)
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
    # 单A5（2026-09-16，小D）：**加主语约束**——原「动词组+可选定语+名词组」无主语要求，
    #   把**第三人称讨论该判据本身**的正常技术写作当攻击拦（D2 自指型）。
    #   改法沿用单R7 在 EXFIL-CN 上的成功手法（自指复合名词 vs 泛名词+自身定语），并针对 CN-main 的
    #   **裸泛名词兜底职责**（R7 守卫要求「输出提示词」这类规避探针保持拦）新增两臂：
    #     · **自指复合名词**（系统/内部/隐藏的 + 名词）：无条件命中
    #     · **泛名词 + 自身定语**（你的/您的/自己的/自身的/我自己的/本人的）：命中
    #     · **裸泛名词**：仅当其后**不跟技术后缀**（模板/长度/统计/哈希/剪贴板/字段/清单/结构/…）时命中
    #     · **后缀反用守卫**：泛名词+技术后缀 后 40 字内出现请求动词（给我/发给我/取出/公开…）仍命中
    #       ——防「V+名词+模板+给我」用后缀白名单反手套取（本单自测 4/4 堵住，技术散文 0 误拦）。
    #   ⚠️ 否定断言必须写在裸泛名词**之前**（写成 名词(?!后缀) 会因正则回溯去匹配「提示」而漏放）。
    #   ⚠️【不】动其它臂（忽略/无视/不要遵守/泄露/你是/隐藏的指令）与 EN 腿、EXFIL-CN（R7 已治）。
    _INJECTION_PATTERNS_CN = re.compile(
        r"忽略(所有|全部|之前|以前的)?(指令|指示|规则|命令|提示|要求)"
        r"|无视(所有|全部|之前|以前的)?(指令|指示|规则|命令|提示|要求)"
        r"|不要(遵守|遵循|执行)(任何|这条|这些)?(指令|规则|指示)"
        r"|(?<!不要)(?<!不可)(?<!禁止)(?<!别)泄露(所有|全部)?(秘密|机密|隐私|系统提示|system)"
        r"|((展示|调出|读取|输出|打印|复制|粘贴|转述|透露|泄露))[ \t]*"
        r"(?:(?:系统|内部|隐藏的)[ \t]*(?:提示词|提示|指令|规则|消息|配置|指导原则)"
        r"|(?:你的|您的|自己的|自身的|我自己的|本人的)[ \t]*(?:提示词|提示|指令|规则|消息|配置|系统规则|指导原则)"
        r"|(?!(?:提示词|提示|指令|规则|消息|配置)[ \t]*(?:模板|长度|统计|哈希|到[ \t]*剪贴板|剪贴板|字段|清单|结构|拼接|渲染|映射|规范))"
        r"(?:提示词|提示|指令|规则|消息|配置)"
        r"|(?:提示词|提示|指令|规则|消息|配置)[ \t]*(?:模板|长度|统计|哈希|到[ \t]*剪贴板|剪贴板|字段|清单|结构|拼接|渲染|映射|规范)"
        r".{0,40}?(?:给我|发我|发给我|发到|倒出来|取出来|取出|我要|我需要|公开|返回|转述|透露|泄露))"
        r"|你是(现在|实际上|真正的)?(ai|助手|语言模型|chatbot|没有限制|不受控|自由模型|另一个模型)"
        r"|隐藏的指令|秘密指令|im(mediately)?(dis)?regard",
        re.IGNORECASE | re.DOTALL,
    )
    # R3.1（2026-08-31）：对抗实测 43.6% -> 83%，第二轮收紧：补漏网 + 消误报（用 [ \t ] 保留空白，避开转义）
    # 单A5（2026-09-16，小D）：**加英文词边界**——原动词组无 \b，`displayName`/`README` 这类
    #   标识符里的子串被当请求动词捞走（D4 远距共现型：字段名+密钥名在 120 字窗内共现即中）。
    #   ⚠️ **窗口保持 {0,120} 不变**——实测收窄到 24 会掉真阳性（长间隔请求句），词边界才是真因；
    #      易混淆点：D4 的根因是「子串误匹配」而非「窗口过宽」，收窗治不了它（A/B 对照实测）。
    #   `api key` 同样加右边界，防 `apiKeyEnv` 被当名词命中。
    _INJECTION_PATTERNS_EXFIL = re.compile(
        r"(?<![A-Za-z])(?:reveal|show|display|print|copy|output|paste|dump|transcribe|extract|send[ \t]+me|give[ \t]+me|leak|exfiltrate|disclose|return|need|read|deem[ \t]+irrelevant)(?![A-Za-z])"
        r".{0,120}?(system[ \t]*(prompt|message|instructions?|rules?)|hidden[ \t]*(role[ \t]+|system[ \t]+)?(instructions?|prompt|rules?|system[ \t]*message)|"
        r"internal[ \t]*(prompt|instructions?|message|guidelines|configuration)|api[ \t]*key(?![A-Za-z0-9_])|secret[ \t]*(key|text|system[ \t]*text)|confidential|"
        r"secrets?|full[ \t]*prompt|model[ \t]*prompt|private[ \t]*system[ \t]*rules|configuration[ \t]*secret|system[ \t]*text)",
        re.IGNORECASE,
    )
    # 单R7（2026-09-15，小D）：**加主语约束**——原判据「动词与名词 60 字内共现即命中」没有主语要求，
    #   把**正常技术写作**当攻击拦（19 条技术语料实测误报 14 条；连"这段文字讨论的是返回值的来源，
    #   跟提示词无关"这种**声明无关**的句子都被拦）。这是**精准化，不是放宽**。
    #   改法：把名词拆两类——
    #     · **自指复合名词**（自带自身指向，**无需**定语）：系统提示词/系统提示/系统消息/模型提示词/
    #       角色提示词/私有系统规则/隐藏(的)(角色)(提示|指令|规则)/内部(提示|指令|规则|消息|配置|指导原则)/
    #       秘密/机密/密钥/秘密配置
    #     · **泛名词**（**必须**带自身定语 OWN）：提示词 / 系统规则 / 指导原则
    #   另加一条臂：**请求类动词紧邻泛名词**（给我X / 公开X / 取出来X）。
    #   ⚠️ 该臂**故意排除"返回"**——"返回"是描述性误报的主因（"该函数返回X字段名"这类技术句子）。
    #   ⚠️ **敏感名词**（秘密/机密/密钥/秘密配置）**单独成臂**并改用**紧邻窗 {0,8}** + **否定守卫**：
    #      根因=实测文档里"…是否真密钥…乙也未展示…"这种**远距无关共现**会被 60 字窗捞进来；
    #      收紧后"打印密钥/把密钥取出来/读取密钥文件"等**邻接攻击仍全中**，而远距散文误报清零。
    #      否定守卫（不/未/别/勿/没有）沿用本仓 CN-main 腿既有惯例，非新造。
    #   实测（14 验收样本 + 14 规避探针 + 19 技术语料 + 10 敏感攻击 + 4 敏感散文 + 1 真实文档片段）：
    #     真攻击 5/5 **保持** ｜ 假阳性 5/6 → **0/6** ｜ 规避探针 14/14 **保持**
    #     ｜ EXFIL-CN 自身技术语料误报 14 → 0 ｜ 敏感散文误报 4 → 0 ｜ 真实文档片段 1 → 0
    #   ⚠️【不】动其它腿（英文 EXFIL / OVERRIDE / CN-main / 重复行检测）——CN-main 另有同类误报，属另一条腿。
    _INJECTION_PATTERNS_EXFIL_CN = re.compile(
        # 臂0：请求类动词紧邻泛名词（"给我X""公开X"）——注意**不含"返回"**
        r"(给我|发给我|发到|倒出来|取出来|取出|我要|我需要|公开)[ \t]*(提示词|系统规则|指导原则)"
        # 臂1：动词 → 名词（名词须自带自身指向）
        r"|(展示|调出|读取|输出|打印|复制|粘贴|转述|透露|给我|发给我|发到|倒出来|取出来|取出|返回|我要|我需要|公开)"
        r".{0,60}?(系统提示词|系统提示|系统消息|模型提示词|角色提示词|私有系统规则"
        r"|隐藏的?(角色[ \t]*)?(提示|指令|规则)|内部(提示|指令|规则|消息|配置|指导原则)"
        r"|(你的|您的|自己的|自身的|我自己的|本人的)[ \t]*(提示词|系统规则|指导原则))"
        # 臂2：动词 → 敏感名词（**紧邻窗 {0,8}** + 否定守卫）
        r"|(?<!不)(?<!未)(?<!别)(?<!勿)(?<!没有)"
        r"(展示|调出|读取|输出|打印|复制|粘贴|转述|透露|给我|发给我|发到|倒出来|取出来|取出|返回|我要|我需要|公开)"
        r".{0,8}?(秘密配置|秘密|机密|密钥)"
        # 臂3：名词 → 动词（对称臂，名词形态同臂1）
        r"|(系统提示词|系统提示|系统消息|模型提示词|角色提示词|私有系统规则"
        r"|隐藏的?(角色[ \t]*)?(提示|指令|规则)|内部(提示|指令|规则|消息|配置|指导原则)"
        r"|(你的|您的|自己的|自身的|我自己的|本人的)[ \t]*(提示词|系统规则|指导原则))"
        r".{0,60}?(给我|发我|发到|输出|展示|复制|粘贴|转述|透露|倒出来|取出来|取出|粘贴出来|我要)"
        # 臂4：敏感名词 → 动词（**紧邻窗 {0,8}** + 否定守卫）
        r"|(秘密配置|秘密|机密|密钥).{0,8}?"
        r"(?<!不)(?<!未)(?<!别)(?<!勿)(?<!没有)(给我|发我|发到|输出|展示|复制|粘贴|转述|透露|倒出来|取出来|取出|粘贴出来|我要)",
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
    # 单A5（2026-09-16，小D）：**伪装臂补宾语约束**——原 `伪装(成|为)?` **无宾语要求**，
    #   退化成「正文含该词即命中」（D3 退化型）：安全审计报告 / Anti-MITM 设计 / 字段语义说明
    #   一律被判注入。改后要求 `伪装成/为` 指向**身份类客体**（系统/管理员/用户/AI/助手/模型/root…）。
    #   ⚠️ 只收紧该臂；「假装你是」「你的新身份是」「忽略系统指令」等臂原样未动。
    _INJECTION_PATTERNS_OVERRIDE_CN = re.compile(
        r"你(现在|实际上|真正|实际)?是(一个|个)?(没有限制|不受控|恶意|地下|内部|自由模型|另一个模型|真正的?)"
        r"|假装(你|是|成|做)"
        r"|伪装(?:成|为)?[ \t]*(?:系统|管理员|用户|AI|助手|模型|已被授权|授权|root|admin|开发者|超级用户|内部人员|权威)"
        r"|忽略[^，。]{0,8}(系统|内部|隐藏的)(指令|提示|规则|指示)"
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
        # 单R4（2026-09-15，小D）：花括号隔断——**全库正文写入的唯一收口**。
        # 为什么必须在这个位置（⚠️ 换序会出两种事故，勿动）：
        #   - 插在**注入检测之前**：检测面对的是已被零宽空格打散的字串，某些注入正则可能失效（漏检真注入）；
        #   - 插在检测之后：检测用**原文**（不漏检），落库用**隔断后的文本**（不炸宿主），两头都对。
        # 为什么这一处就能全覆盖：store_chunk / batch_store_chunks / CLI / MCP(serve) / 蒸馏写回
        #   全部经 _normalize_chunk → 此处；ContextBuilder.store() 另有一次直调（context_builder.py:375）。
        #   ⇒ 该函数对同一条正文可能被调**两次**（store() 一次，store_chunk 内一次），
        #     故 neutralise_template_braces 的**幂等性是硬需求**（非幂等会插两个零宽空格）。
        # 自动收益：_normalize_chunk 用本返回值重算 content_hash / token_count（719/720 行），
        #   _store_chunk_conn 用 chunk.content 写 chunks_fts（793 行）——三者天然同源，不会脱钩。
        return neutralise_template_braces(content)

    @staticmethod
    def _normalize_chunk(chunk: Chunk) -> Optional[Chunk]:
        """P1-A / R-05：校验—规范化—构造参数唯一入口。

        store_chunk 与 batch_store_chunks 共用：
          - 清洗 content 并写回 chunk.content；内容非法返回 None。
          - 按清洗后文本重算 content_hash / token_count（与单条写入同函数）。
          - source_ids / derived_from 为 Python list/tuple 时序列化为 JSON 字符串，
            修复 SQLite bind 参数类型错误（Error binding parameter 17）。
        """
        clean = VectorStore._validate_content(chunk.content)
        if clean is None:
            return None
        chunk.content = clean
        nv = normalize_visibility(chunk.visibility)
        if nv is None:
            # P1-E / R-09：未知/空 visibility 拒写（fail-closed），不回落 shared
            return None
        chunk.visibility = nv
        # P1-L / G-01：tier 白名单——归一后不在 raw/refined → 拒写（不做静默默认）
        _nt = _normalize_tier(chunk.tier)
        if _nt not in TIERS:
            logger.warning("store rejected: tier %r 不在白名单 raw/refined", chunk.tier)
            return None
        chunk.tier = _nt
        # P4.9-a（2026-09-02）/ P1-H：血缘强制闸写侧唯一落点——
        # derived_from 非空时 source_ids 必须非空（JSON 解析后），否则拒写。
        if chunk.derived_from:
            try:
                _sid = json.loads(chunk.source_ids) if isinstance(chunk.source_ids, str) else (chunk.source_ids or [])
            except Exception:
                _sid = []
            if not _sid:
                logger.warning("store rejected: derived_from 需要非空 source_ids（P4.9-a 血缘强制闸）")
                return None
        chunk.content_hash = compute_content_hash(clean)
        chunk.token_count = estimate_tokens(clean)
        if isinstance(chunk.source_ids, (list, tuple)):
            chunk.source_ids = json.dumps(list(chunk.source_ids), ensure_ascii=False)
        if isinstance(chunk.derived_from, (list, tuple)):
            chunk.derived_from = json.dumps(list(chunk.derived_from), ensure_ascii=False)
        # ★M01 第三轮 R2 / ★B-补（2026-10-13）：v2 出处载荷 source_refs / scope 的归一落点。
        #   ⚠️ 这里**只校验、不改写**（本函数是"校验—规范化"入口，但它对同一条 Chunk
        #     可能被调多次：store_chunk 一次、调用方重存再调一次）。旧写法在这里把它
        #      改写成 JSON 文本，于是**第二次调用无法区分**"已是编码后的文本"与
        #      "调用方本来就传了个字符串"，对 scope 会编成 `"\"{...}\""`（双重编码，
        #      读回来变成一段带引号的字符串）。⇒ 真正的序列化统一挪到 bind 那一刻的
        #      `_prov_bind`（那里每次都是"从结构化原值出发"，天然幂等）。
        #   为什么必须在这里做一层校验：非法类型（如把 list 塞进 scope 又期望标量语义）
        #      会在 bind 时被 `_json_safe_ref` 归一，不会炸；真正的 TypeError 风险
        #      （dataclass 实例裸 json.dumps）同样在 `_prov_bind` 里被 `_json_safe_ref` 兜住。
        #   语义保持：**空 = 没带 = NULL**（见 D_PROV_CHUNK_COLS 缺省语义），
        #      读侧 `_row_to_chunk` 把 NULL 还原成 `[]`/None，往返仍相等。
        return chunk

    @staticmethod
    def _readers_repr(readers) -> str:
        """读者集合的可读渲染——**日志专用**，避免 sorted(ALL_READERS) 抛 TypeError。

        ⚠️ 为什么要单独一个函数：ALL_READERS 是顶层哨兵（memory/visibility._Top），
           **不是可迭代集合**。任何 `sorted(readers)` / `list(readers)` / `",".join(...)`
           在它是 ALL 时都会抛 TypeError。而它在**日志语句**里——
           日志抛异常会把"本该拒写并记一行"变成"抛异常"，错误分类就错了（本单实测踩到）。
           本文件所有打印读者集合的地方一律走这里。
        """
        from memory.visibility import ALL_READERS
        if readers is ALL_READERS:
            return "ALL_READERS"
        try:
            return str(sorted(readers))
        except TypeError:
            return str(readers)

    def _apply_derived_readers_gate(self, conn, chunk: Chunk) -> bool:
        """派生读者集合闸（M03 §9.3「派生实际读者不扩大」）——写侧最后一道防线。

        _apply_derived_readers_gate()：契约
        入参：conn（当前连接）/ chunk（**已经过 _normalize_chunk** 的块）
        返回：True=通过（可能已就地收紧 chunk.visibility）/ False=拒写
        副作用：就地改 chunk.visibility
        谁调用：store_chunk() / batch_store_chunks() —— 两个写入口**都**要过
        改动影响：本闸是"派生行读者集合 ⊆ 全部来源读者集交集"的最后落点。
                 放宽它 = 直接复现 I09。

        为什么放在 vector_store 而不是只在 context_builder：
            context_builder 是**常用入口**，不是**唯一入口**——batch_store_chunks、
            WAL 重放、蒸馏脚本、未来 CST01 都可能直接落到这里。权限闸只设在常用路径上
            等于给绕行留门（I09 的同族病）。

        ★决策**不在这里**：调用 resolver.derived_visibility_for_write()——与
          context_builder.store() 是同一个函数。M01 契约 §5 要求 WAL 与 DB 的
          visibility 同源，两处各判一次就是"两套口径"（M03 要根治的病）。
          本方法只负责"把决策结果落到 chunk 上，并校验调用方声明的值没有超界"。
        """
        if not chunk.derived_from:
            return True
        try:
            _sid = json.loads(chunk.source_ids) if isinstance(chunk.source_ids, str) else (chunk.source_ids or [])
        except Exception:
            _sid = []
        if not _sid:
            return True   # 已在 _normalize_chunk 的血缘闸拦过；到这里只可能是空且非派生
        from memory.source_resolver import get_resolver
        from memory.visibility import is_subset, reader_set_of
        ns = str(chunk.namespace or "")
        # ★R1-1（2026-10-14 返修）：把**调用方声明的 visibility 一起传进去**。
        #   声明范围是额外上界：调用方主动收窄到 private，不能因为"来源是 shared"就被改宽。
        #   旧实现把它取出来只打了一行 warning，然后无条件用决策值覆盖（声明形同虚设），
        #   实测：来源 shared + 声明 private 的派生落库后变成 shared（他人可读）。
        #   ⇒ 决策函数内部做 来源交集 ∩ 声明读者集合，这里只负责把声明值交给它。
        declared_vis = str(chunk.visibility or "")
        decision = get_resolver(conn).derived_visibility_for_write(
            list(_sid), ns, declared_visibility=declared_vis)
        if not decision["ok"]:
            # 该拒的两类情形：
            #   ① 来源全部可解析、但交集在拟落域下表达不出来（含为空）——落 private 就是
            #      把这条白送给拟落域（凭空授权，I09 复发）。
            #   ② 有来源查不到、且**已知来源的读者**里不含拟落域——同上，只是来源不全。
            logger.warning(
                "store rejected: 派生来源读者交集在域 %r 下无法表达（M03 §9.3 拒绝生成）"
                " readers=%s refusals=%s",
                ns,
                self._readers_repr(decision["readers"]),
                decision["refusals"])
            return False

        # ★派生读者 = 来源交集 ∩ 声明范围（Spec §9.3 + R1-1）。这里落**收紧后**的值而不是拒写：
        #   §9.3 说派生读者**取**交集——取交集 = 收紧，不是拒收。
        # ⚠️【不】把"声明值比交集宽"改成 return False（本单实测踩过这个坑）：
        #   ① 语义上错："shared 源的派生被声明成 shared，而来源里还有一个 private"这种
        #      极常见的正常写入，会被误判成越权而整条拒掉（实测：shared + private 双源派生被拒）。
        #   ② 运维上错：WAL 重放/遗留数据里的声明值本来就是旧口径写的，
        #      逐条拒写 = 重启后旧派生行整批消失（数据丢失），而 M01 契约要求它们仍能落库。
        #   ⇒ 声明值比交集宽时**收紧 + warning**；比交集窄时由决策函数取交（同样收紧）。
        declared = reader_set_of(declared_vis, ns)
        if not is_subset(declared, decision["readers"]):
            logger.warning(
                "derive gate: 声明值比来源交集宽，已按交集收紧（M03 §9.3）"
                " declared=%s tightened=%s ns=%r",
                self._readers_repr(declared), self._readers_repr(decision["readers"]), ns)
        chunk.visibility = decision["visibility"]
        # ★R4-1（2026-10-14 返修）：把本次决策**所依据的来源版本指纹**挂到 chunk 上，
        #   供 INSERT 前复验（见 _revalidate_derived_gate）。
        #   病灶（独立验收实测）：闸通过之后、INSERT 之前，另一条真实连接把来源
        #   shared 改成 private；原写操作继续成功 ⇒ 一条"按旧权限算出"的跨域派生落了库。
        #   光靠"两个入口调同一个函数"不能保证**决策在落库那一刻仍然有效**——
        #   函数同源解决的是口径一致，不解决 TOCTOU。
        try:
            chunk._m03_gate_revisions = {
                "%s:%s" % (s.ref.kind, s.ref.id): str(s.revision or "")
                for s in (decision.get("snapshots") or [])
            }
        except Exception:
            # 挂属性失败不该阻断写入（复验会因拿不到基线而跳过，退回旧行为）
            chunk._m03_gate_revisions = None
        return True

    def _revalidate_derived_gate(self, conn, chunk: Chunk) -> bool:
        """★R4-1：INSERT 前复验"决策所依据的来源版本"是否仍然成立（**写锁外的快速拒绝**）。

        _revalidate_derived_gate()：契约
        入参：conn / chunk（已过 _apply_derived_readers_gate）
        返回：True=来源未变，可以落库 / False=来源已变，**拒写**
        副作用：无（只读）

        ★★F1（2026-10-14 第二轮返修）：本函数**降级为快速拒绝**，不再是权威判定。
        独立验收裁定（原文）："已实测来源收紧成功后，旧授权派生仍以 active/shared 写入；
        不能用『极短/有意取舍』豁免……授权依据检查与写入必须有真实原子性
        或提交前在已取得写锁的事务中有效复验。"
        病灶：本函数在写锁**之外**执行，复验到 INSERT/commit 之间仍有窗口——
        另一条连接恰在此刻收紧来源，旧授权派生照样落库、且落库后**没有任何补偿更新**
        （收紧方此前扫描为 0，之后也不会回头），违规行持续可读。批量入口的窗口更宽，
        跨越后续行的规范化与分批提交（实测 batch_size=1 时第一条派生仍 active/shared）。

        ⇒ 真正的权威判定是 `_derived_gate_still_valid`：它在**写事务内**（INSERT 之后、
        commit 之前）复验，此时本连接已持有写锁，竞争方拿不到锁 ⇒ 复验与提交之间
        不存在任何可写窗口（WAL 下其他读者只能看到事务开始前的快照）。
        本函数保留是为了**便宜地早拒**：能在这里发现变化就不必白写一趟。
        ⚠️ 两处调用的判据必须是同一份（都委托给 _derived_gate_still_valid），
          否则又成"两套口径"——正是 M03 要根治的病。
        """
        return self._derived_gate_still_valid(conn, chunk)

    def _derived_gate_still_valid(self, conn, chunk: Chunk) -> bool:
        """★F1 权威复验：来源版本指纹是否仍与决策时一致（**必须在写事务内调用**）。

        _derived_gate_still_valid()：契约
        入参：conn（已持有写锁——由调用方的 INSERT/UPDATE 建立）/ chunk（带 _m03_gate_revisions）
        返回：True=授权依据仍成立 / False=已变，必须回滚本次写入
        副作用：无（只读）
        谁调用：store_chunk（INSERT 之后、commit 之前）/ batch_store_chunks（每批写事务内）
        改动影响：这是"过期授权不得落库"的**唯一权威判据**。放宽/绕过 = 复现 F1 两条。

        ⚠️ 为什么【不】用"开写锁（BEGIN IMMEDIATE）后再复验"的显式写法：
           本函数被调用时，调用方已经执行过写语句（INSERT/executemany），SQLite 的
           延迟事务在那一步就已取得写锁。再加一条 BEGIN 反而会撞上调用方已开的事务。
           判据是"调用时机"（写锁内），不是"有没有那句 BEGIN"——见各自调用点的注释。
        ⚠️【不】把本函数改名成 `_revalidate_derived_gate` 的实现体直接内联回去：
           那会让"快速拒绝"与"权威判定"重新合成一个**在锁外也会被调用**的函数，
           评审方注入的时序钩子（patch _revalidate_derived_gate）就会把权威判定
           也拖进死锁窗口。两者分开是**有意的结构**，别合并。
        """
        if not getattr(chunk, "derived_from", ""):
            return True
        expected = getattr(chunk, "_m03_gate_revisions", None)
        if not expected:
            return True      # 没基线（非派生/挂属性失败）⇒ 无复验，维持旧行为
        from memory.source_resolver import get_resolver
        r = get_resolver(conn)
        current = {}
        for key in expected:
            kind, _, cid = key.partition(":")
            snap = r.resolve({"kind": kind or "chunk", "id": cid})
            current[key] = str(snap.revision or "")
        if current != expected:
            logger.warning(
                "store rejected: 闸通过后来源版本发生变化（F1 授权依据已失效）chunk=%s "
                "expected=%s current=%s", chunk.id, expected, current)
            return False
        return True

    def store_chunk(self, chunk: Chunk) -> bool:
        """存储文本块。返回 True=已落库 / False=内容被安全校验拒绝（P0-3）。

        正文 + namespace/project/host_origin/visibility/updated_at 同一事务写入（P0-4），
        不再靠调用方二段式 UPDATE 补 metadata。

        ★★F1（2026-10-14 第二轮返修）：授权判定与写入**必须原子**。
        本函数现在的顺序是"锁外快速拒绝 → 写事务内权威复验 → INSERT → 提交"：
          · `_revalidate_derived_gate`（锁外）只是为了**便宜地早拒**；
          · `_derived_gate_still_valid`（写事务内、INSERT 之前）才是**权威判定**——
            此时本连接已持有写锁，竞争方提交不进来，复验与提交之间无窗口。
        ⚠️【不】把权威复验挪回锁外：那正是独立验收 F1 打回的形态
          （复验返回 True 后、INSERT 前来源被另一连接收紧，旧授权派生照样落库且无补偿）。

        ★★G2（2026-10-14 第四轮返修）：**事务归属判定覆盖整个写操作**。
        病灶（独立验收 v3 实测三条，F1-*-preserves-caller-*）：
          `_begin_write_txn` 的返回值 owns 此前**只是被接住、没接到提交/回滚路径**——
          ① 调用方已开事务时仍然无条件 `conn.commit()` ⇒ 把调用方事务**提前提交**，
             调用方随后 rollback 已失效（无关 marker 与派生都留了下来）；
          ② 批量入口同款；
          ③ 权威闸拒绝时无条件 `conn.rollback()` ⇒ 连**调用方此前的无关工作**一起销毁。
        ⇒ 修法：本函数在**任何写语句之前**定一次归属（见 _begin_write_scope）：
          · 自持（调用方无事务）→ BEGIN IMMEDIATE，本函数负责 commit/rollback（F1 原子性不变）；
          · 参与调用方事务 → 开 SAVEPOINT，成功 RELEASE、失败 ROLLBACK TO，
            **绝不 commit/rollback 外层**——提交权始终在调用方手里（R4-2 的同一纪律）。
        ⚠️ 归属必须在 `_tokenizer_version_ok` 之前判定：它首次会播种一行
          （INSERT OR IGNORE），那一下会把 `in_transaction` 变成 True，
          事后再判就会把自己的播种误认成"调用方事务"（F1 的写锁也就跟着没了）。
        """
        orig_id = chunk.id[:16]
        chunk = self._normalize_chunk(chunk)
        if chunk is None:
            logger.warning("store_chunk: rejecting chunk %s (empty/invalid content)", orig_id)
            return False
        conn = self._get_conn()
        # ★M03：派生读者集合闸（写侧最后一道防线，见方法 docstring）
        if not self._apply_derived_readers_gate(conn, chunk):
            return False
        # ★R4-1 / F1：锁外快速拒绝。权威判定在 _store_chunk_conn 的写事务内再做一次。
        if not self._revalidate_derived_gate(conn, chunk):
            return False
        tags_json = json.dumps(chunk.tags, ensure_ascii=False)
        updated_at = chunk.updated_at if chunk.updated_at is not None else time.time()

        # ★★G2：**归属判定提到写之前**（含 _tokenizer_version_ok 的首次播种）。
        #   _scope_ready=False 表示作用域尚未建立成功——那种情况下**绝不**碰事务，
        #   否则会把"没开成的 savepoint"误当成"自持事务"去 rollback 调用方的工作。
        _sp = None
        _scope_ready = False

        # P1-5（2026-09-06 外部审计整改总单-1）：异常路径回滚——懒加载单例连接下，
        # 异常残留的事务（chunks INSERT 已执行、FTS 未写）会被后续成功提交一并带出，
        # 产出"chunks=T fts=F"的残疾行（故障注入实锤：报告修正 1）。失败即回滚本次
        # 写入范围再 raise，让调用方看到干净失败而不是污染下一个 store。
        # ⚠️【不】在这里直接写 conn.rollback()：调用方有事务时那是**销毁别人的工作**
        #   （G2 判据三条里的第③条，实测 marker 被一起回滚）。
        #   一律走 _finish_write_scope：自持才 rollback，参与外层只回退到 savepoint。
        try:
            _sp = self._begin_write_scope(conn)
            _scope_ready = True
            if not _tokenizer_version_ok(conn):
                self._finish_write_scope(conn, _sp, ok=False)
                return False
            self._store_chunk_conn(conn, chunk, tags_json, updated_at)
        except _DerivedGateRejected:
            # ★F1：权威复验在写事务内发现授权依据已变 ⇒ 回退本次写入范围，干净拒绝。
            #   这里是**预期路径**（不是异常事故），所以返回 False 而不是向上抛：
            #   调用方（context_builder / 蒸馏脚本）按"这条没写进去"处理即可。
            if _scope_ready:
                # ★G2（第五轮）：回退本身失败不许盖掉这次"业务拒绝"的语义，
                #   只额外留痕（_finish_write_scope 内部已标记连接不可用）。
                try:
                    self._finish_write_scope(conn, _sp, ok=False)
                except Exception:
                    logger.exception("store_chunk: 拒绝路径回退写作用域失败（G2）")
            logger.warning("store_chunk: 授权依据在写事务内已失效，拒绝落库 chunk=%s", orig_id)
            return False
        except Exception:
            if _scope_ready:
                # ★G2：回退失败不得掩盖**原始异常**（否则调用方看到的是次生故障）。
                try:
                    self._finish_write_scope(conn, _sp, ok=False)
                except Exception:
                    logger.exception("store_chunk: 异常路径回退写作用域失败（G2）")
            raise
        self._finish_write_scope(conn, _sp, ok=True)
        return True

    def _begin_write_txn(self, conn) -> bool:
        """★F1：取得写锁并开启自持写事务；返回 True=本函数开的（调用方负责提交/回滚）。

        为什么必须显式 BEGIN IMMEDIATE：
          Python sqlite3 的隐式事务是 **deferred**（先读后写会在升级锁时撞 BUSY_SNAPSHOT，
          busy handler 不救 deferred）。而 F1 要求"复验时已持有写锁"——
          deferred 事务在第一次写之前**没有写锁**，复验就不算原子。
        ⚠️ 已被调用方开启事务时（in_transaction=True）不动它：那种情况下本连接要么已经
          写过（隐式写事务已持锁），要么是调用方自己的事务——**不接管提交权**。
          这是 R4-2"不偷提交调用方事务"的同一条纪律。
        """
        if conn.in_transaction:
            return False
        conn.execute("BEGIN IMMEDIATE")
        return True

    def _begin_write_scope(self, conn) -> Optional[str]:
        """★G2：开一个**写作用域**并返回它的归属标识。

        _begin_write_scope()：契约
        入参：conn
        返回：None = 本函数开了**自持**写事务（调用方必须用 _finish_write_scope 收口）
              str  = 参与**调用方**事务；该串是本次用的 SAVEPOINT 名
        副作用：None 分支执行 BEGIN IMMEDIATE；str 分支执行 SAVEPOINT
        谁调用：store_chunk() / batch_store_chunks()——两个写入口都要（G2 判据第②条：
                批量与单条必须一致，否则"单条不偷提交、批量偷"就是新的分歧面）

        ⚠️ 为什么参与外层事务时用 SAVEPOINT 而不是"什么都不做、跟着外层提交"：
            本作用域要能**只撤销自己这一次写**（权威闸拒绝 / INSERT 抛异常），
            而调用方此前写入的无关工作必须原样保留（G2 判据第③条）。
            SAVEPOINT 正是 SQLite 提供的局部回滚点；不用它就只能二选一——
            要么 rollback 整个外层（销毁调用方工作），要么把半成品留给外层。
        ⚠️ 为什么自持时仍要 BEGIN IMMEDIATE 而不是让 SQLite 隐式开 deferred：
            见 _begin_write_txn 的说明（deferred 在首次写之前没有写锁，
            复验就不算原子——那正是 F1 打回的形态）。
        ⚠️ 本方法**不吞异常**：BEGIN/SAVEPOINT 失败时直接抛出，由调用方靠
            _scope_ready 标志判断"作用域没建起来、别碰事务"。
        """
        if self._begin_write_txn(conn):
            return None
        # 参与调用方事务：局部回滚点。名字带自增序号防重入时重名。
        self._sp_seq += 1
        sp = "_m03_g2_sp_%d" % self._sp_seq
        conn.execute("SAVEPOINT %s" % sp)
        return sp

    def _finish_write_scope(self, conn, sp: Optional[str], *, ok: bool) -> None:
        """★G2：收口一次写作用域——**只处置自己开的那个范围**。

        ok=True  ：自持 → commit；参与外层 → RELEASE SAVEPOINT（把改动并入调用方事务）
        ok=False ：自持 → rollback；参与外层 → ROLLBACK TO + RELEASE（只撤本次，外层不动）

        ⚠️【不】在参与外层时改成 conn.commit()/conn.rollback()：
            G2 的三条判据全部指向同一件事——**提交权属于开事务的那一方**。
            本函数是那条纪律的**唯一落点**，放宽它 = 直接复现 F1-preserves-caller-*。
        ★★G2 第五轮（2026-10-14）：**收口失败一律不许假成功、不许留残留事务**。
        病灶（独立验收 v4 实测两条）：
          · G2-owned-commit-failure-rolls-back-write：自持 commit 被拒后只抛出，
            事务仍 in_transaction=True、失败行仍待提交；下一次**正常的**独立 store
            把该残留事务当成"调用方事务"只做 RELEASE ⇒ 返回 True 却没落盘，
            之后任何一次 commit 还会把此前失败的行一并带出来。
          · G2-caller-savepoint-release-failure-is-visible：参与外层时 RELEASE 被拒
            只记一行日志 ⇒ store_chunk 返回 True，调用方以为写成功，实际作用域
            没按契约收口。
        ⇒ 分档处置（**两条都必须让失败可见**）：
            自持(commit 拒绝)  → 回滚自持作用域（失败行不得残留），再抛出；
            自持(rollback)     → 回滚；回滚失败 ⇒ 标记连接不可用；
            参与外层           → 尽量 **ROLLBACK TO 局部撤销本次**（外层工作保留），
                                 然后把失败**抛给调用方**；撤销也失败 ⇒ 标记连接不可用。
        """
        if sp is None:
            # ───────── 自持写事务：本函数负全责 ─────────
            if ok:
                try:
                    conn.commit()
                    return
                except BaseException:
                    logger.exception(
                        "write scope 收口失败（G2）：自持 commit 被拒 —— 回滚自持作用域")
                    self._rollback_owned(conn)
                    raise
            self._rollback_owned(conn)
            return

        # ───────── 参与调用方事务：只碰自己的 savepoint ─────────
        if not ok:
            self._rollback_savepoint(conn, sp)
            return
        try:
            conn.execute("RELEASE SAVEPOINT %s" % sp)
        except BaseException:
            logger.exception(
                "write scope 收口失败（G2）：RELEASE 被拒 savepoint=%s —— 局部撤销并上报", sp)
            try:
                conn.execute("ROLLBACK TO SAVEPOINT %s" % sp)
                conn.execute("RELEASE SAVEPOINT %s" % sp)
            except Exception:
                logger.exception("write scope 局部撤销亦失败（G2）：savepoint=%s", sp)
                self._mark_conn_unusable()
            raise

    def _rollback_owned(self, conn) -> None:
        """★G2：回滚本函数**自持**的写事务；失败 ⇒ 连接标记为不可继续安全使用。"""
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            logger.exception("自持事务回滚失败（G2）—— 连接不可继续安全使用")
            self._mark_conn_unusable()

    def _rollback_savepoint(self, conn, sp: str) -> None:
        """★G2：只回退 **本次** savepoint 写入的内容（调用方外层工作原样保留）。"""
        try:
            conn.execute("ROLLBACK TO SAVEPOINT %s" % sp)
            conn.execute("RELEASE SAVEPOINT %s" % sp)
        except Exception:
            logger.exception("savepoint 局部回滚失败（G2）：savepoint=%s", sp)
            self._mark_conn_unusable()
            raise

    def _mark_conn_unusable(self) -> None:
        """★G2：把连接标记为**不可继续安全使用**。

        为什么必须显式标记而不是"下次调用再试"：收口/回滚失败后事务残留状态未知，
        下一条正常写入会把残留事务当成"调用方事务"只做 RELEASE ⇒ 返回**假成功**
        （独立验收 v4 的 G2-owned-commit-failure 后半段实测的正是这个形态）。
        标记后 _get_conn 会明确抛错，把"未知状态"变成"可见失败"。
        """
        self._conn_broken = True

    def _store_observation_conn(self, conn, chunk) -> None:
        """写入与正文同事务的观察收据；调用方持有当前写作用域。"""
        conn.execute(
            """INSERT INTO memory_observations
               (observation_id, operation_id, memory_id, namespace, project,
                content_hash, request_hash, speaker, subject, scope, valid_from,
                valid_until, confirmation_type, source_refs, provenance_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk.observation_id, chunk.operation_id, chunk.id, chunk.namespace,
             chunk.project, chunk.content_hash, chunk.request_hash or chunk.content_hash, chunk.speaker,
             chunk.subject, json.dumps(chunk.scope, ensure_ascii=False) if chunk.scope is not None else None,
             chunk.valid_from, chunk.valid_until, chunk.confirmation_type,
             json.dumps(chunk.source_refs, ensure_ascii=False),
             chunk.provenance_status or "legacy_unverified", time.time()),
        )

    def _store_chunk_conn(self, conn, chunk, tags_json, updated_at) -> None:
        """store_chunk 的事务体（P1-5 拆出：让外层 try/except 包住 INSERT 全程）。

        ★F1：本方法在**已取得写锁的作用域内**被调用，此处复验授权依据；不等就抛
        _DerivedGateRejected，由外层回退整个写作用域。复验与 INSERT 同处一个写事务
        ⇒ 不存在竞争窗口。
        ⚠️G2（2026-10-14 第四轮返修）：**本方法不再自己开事务、也不再 commit**。
          归属与收口统一由 store_chunk 的 _begin_write_scope/_finish_write_scope 负责——
          这里再多一次 BEGIN/commit 就是"两个地方管提交权"，正是 G2 的病灶形态。
        """
        request_hash = chunk.request_hash or chunk.content_hash
        if chunk.operation_id:
            existing = conn.execute(
                """SELECT o.operation_id, o.observation_id, o.memory_id, o.request_hash, c.state
                   FROM memory_observations AS o LEFT JOIN chunks AS c ON c.id=o.memory_id
                   WHERE o.operation_id=? AND o.namespace=? AND o.project=?""",
                (chunk.operation_id, chunk.namespace, chunk.project),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise sqlite3.IntegrityError("operation payload conflicts with its durable observation")
                if str(existing["state"] or "") != "active":
                    raise sqlite3.IntegrityError("operation_id refers to an archived or forgotten observation")
                if chunk.observation_id:
                    collision = conn.execute(
                        "SELECT operation_id, namespace, project, memory_id FROM memory_observations WHERE observation_id=?",
                        (chunk.observation_id,),
                    ).fetchone()
                    if collision and (collision["operation_id"] != chunk.operation_id
                                      or collision["namespace"] != chunk.namespace
                                      or collision["project"] != chunk.project):
                        raise sqlite3.IntegrityError("observation_id belongs to another operation or owner")
                chunk.id = existing["memory_id"]
                chunk.observation_id = existing["observation_id"]
                # An idempotent retry resolves to the original effect. Do not insert
                # another body or FTS row under the caller's new tentative memory ID.
                return
        if chunk.observation_id:
            existing_id = conn.execute(
                """SELECT operation_id, observation_id, memory_id, namespace, project,
                          request_hash, content_hash
                   FROM memory_observations WHERE observation_id=?""",
                (chunk.observation_id,),
            ).fetchone()
            if existing_id:
                if (existing_id["operation_id"] == chunk.operation_id
                        and existing_id["namespace"] == chunk.namespace
                        and existing_id["project"] == chunk.project
                        and existing_id["request_hash"] == request_hash
                        and existing_id["content_hash"] == chunk.content_hash):
                    return
                raise sqlite3.IntegrityError("observation_id belongs to a different operation or scope")
        # ★F1 权威复验：此刻已持写锁 ⇒ 读到的来源版本就是提交时的版本。
        if not self._derived_gate_still_valid(conn, chunk):
            raise _DerivedGateRejected(chunk.id)
        # 1. chunks 表：正文+metadata 单事务存原始文本
        # ★B-补：provenance 八列由 _PROV_COLS_SQL/_PROV_VALUES_SQL/_PROV_UPSERT_SQL 派生
        #   （见文件上方常量区）——与 batch_store_chunks 共用同一份列名，改一处两边同改。
        conn.execute(
            """
            INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, visibility, source_ids, derived_from, tier""" + _PROV_COLS_SQL + """)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?""" + _PROV_VALUES_SQL + """)
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
                tier=excluded.tier,
                """ + _PROV_UPSERT_SQL + """
            """,
            (chunk.id, chunk.content, chunk.timestamp, chunk.importance, tags_json, chunk.source,
             chunk.token_count, chunk.content_hash, chunk.track, chunk.state, chunk.superseded_by,
             chunk.namespace, chunk.project, chunk.host_origin, updated_at, chunk.visibility,
              chunk.source_ids, chunk.derived_from, getattr(chunk, "tier", "raw") or "raw")
            + _prov_bind(chunk),
        )
        if chunk.operation_id and chunk.observation_id:
            conn.execute("UPDATE chunks SET operation_id=?, observation_id=? WHERE id=?",
                         (chunk.operation_id, chunk.observation_id, chunk.id))
        if chunk.operation_id and chunk.observation_id:
            self._store_observation_conn(conn, chunk)
        rowid = conn.execute("SELECT rowid FROM chunks WHERE id=?", (chunk.id,)).fetchone()
        if rowid:
            # 2. chunks_fts：存分词文本（用于中文搜索）
            ft_content = _tokenize_for_fts5(chunk.content)
            conn.execute(
                """INSERT OR REPLACE INTO chunks_fts(rowid, id, content, tags, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (rowid[0], chunk.id, ft_content, tags_json, chunk.source),
            )
        # ⚠️G2：**不在这里 commit**——提交权归 _finish_write_scope，见其 docstring。

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
        # ★★G2-1（2026-10-14 第五轮）：**归属判定必须在任何本函数写入之前**。
        #   病灶（独立验收 v4 实测 G2-first-batch-write-commits-tokenizer-and-data）：
        #     旧写法先调 _tokenizer_version_ok(conn)——新库没有 tokenizer_version 时
        #     它会 `INSERT OR IGNORE` **先**打开隐式事务；随后 _begin_write_scope 看见
        #     in_transaction=True，把**本函数自己的首次播种**误认成"调用方事务"，
        #     只做 SAVEPOINT/RELEASE ⇒ 整批没有 commit：接口返回 1，另一连接看不见，
        #     关闭写连接后记录消失。单条入口正常（它已把归属提到播种之前）——
        #     这正是"单条与批量必须同一套"的漏洞点。
        #   ⇒ 播种本身也进入一个**写作用域**：自持时 BEGIN IMMEDIATE → 播种 → 立即提交
        #     （与单条入口同款）；参与调用方事务时开 savepoint，播种并入调用方事务。
        _boot_sp = self._begin_write_scope(conn)
        try:
            if not _tokenizer_version_ok(conn):
                self._finish_write_scope(conn, _boot_sp, ok=False)
                return 0
            self._finish_write_scope(conn, _boot_sp, ok=True)
        except Exception:
            try:
                self._finish_write_scope(conn, _boot_sp, ok=False)
            except Exception:
                logger.exception("batch_store_chunks: 播种作用域回退失败（G2）")
            raise
        # P1-A/R-05：校验—规范化入口统一（清洗+重算 hash/token+list 序列化）
        # ★M03：规范化后立刻过派生读者集合闸——批量入口与单条入口**同闸**，
        #   否则"单条写被拦、批量写放行"就是一个越权绕行口。
        normalized = []
        for c in chunks:
            n = self._normalize_chunk(c)
            if n is not None:
                if not self._apply_derived_readers_gate(conn, n):
                    continue
                # ★R4-1：批量入口与单条入口**同款复验**——否则绕行口就在批量这边。
                if not self._revalidate_derived_gate(conn, n):
                    continue
                normalized.append(n)
        chunks = normalized
        total = 0
        # ★B-补：与 store_chunk 同一份 provenance 列清单（_PROV_* 派生，见文件上方常量区）。
        #   ⚠️ 本文件修改铁律 1 —— 两条 INSERT 的列清单必须同步，此处若漏，批量写入的记忆
        #   会再次"Chunk 有值、DB 全 NULL"，而 store_chunk 那条却是对的 ⇒ 更难查的半盲区。
        sql = """
            INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, visibility, source_ids, derived_from, tier""" + _PROV_COLS_SQL + """)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?""" + _PROV_VALUES_SQL + """)
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
                tier=excluded.tier,
                """ + _PROV_UPSERT_SQL + """
        """
        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset: offset + batch_size]
            # ★★G2：每批一个**写作用域**；归属判定覆盖本批的复验+写入+收口。
            _sp = None
            _scope_ready = False
            try:
                # ★★F1（2026-10-14 第二轮返修）：每批**先取写锁再复验授权依据**。
                #   病灶（独立验收实测）：批量复验在"整列表规范化"阶段做（锁外），
                #   而提交发生在后面的分批循环里——窗口跨越了后续行的规范化与分批提交，
                #   batch_size=1 时第一条派生照样以 active/shared 落库。
                #   ⇒ 权威复验必须在**本批的写事务内**：此刻已持写锁，
                #     竞争方提交不进来，复验与提交之间没有可写窗口。
                _sp = self._begin_write_scope(conn)
                _scope_ready = True
                # Each batch retains one write scope; observation receipts and FTS
                # are added below before that same scope is released.
                eligible = []
                seen_operations = {}
                seen_observations = {}
                for c in batch:
                    req_hash = c.request_hash or c.content_hash
                    op_key = (c.namespace, c.project, c.operation_id) if c.operation_id else None
                    if op_key and op_key in seen_operations:
                        prior_hash, prior_observation = seen_operations[op_key]
                        if prior_hash != req_hash or prior_observation != c.observation_id:
                            raise sqlite3.IntegrityError("batch repeats operation with conflicting identity")
                        continue
                    if op_key:
                        seen_operations[op_key] = (req_hash, c.observation_id)
                        existing = conn.execute(
                            "SELECT observation_id, request_hash FROM memory_observations WHERE operation_id=? AND namespace=? AND project=?",
                            (c.operation_id, c.namespace, c.project),
                        ).fetchone()
                        if existing:
                            if existing["request_hash"] != req_hash or existing["observation_id"] != c.observation_id:
                                raise sqlite3.IntegrityError("batch operation conflicts with durable observation")
                            continue
                    if c.observation_id:
                        prior = seen_observations.get(c.observation_id)
                        if prior is not None and prior != (c.operation_id, c.namespace, c.project, req_hash):
                            raise sqlite3.IntegrityError("batch observation_id collision")
                        seen_observations[c.observation_id] = (c.operation_id, c.namespace, c.project, req_hash)
                        existing_id = conn.execute(
                            "SELECT operation_id, namespace, project, request_hash, content_hash FROM memory_observations WHERE observation_id=?",
                            (c.observation_id,),
                        ).fetchone()
                        if existing_id:
                            if (existing_id["operation_id"] != c.operation_id
                                    or existing_id["namespace"] != c.namespace
                                    or existing_id["project"] != c.project
                                    or existing_id["request_hash"] != req_hash
                                    or existing_id["content_hash"] != c.content_hash):
                                raise sqlite3.IntegrityError("batch observation_id belongs to another operation")
                            continue
                    eligible.append(c)
                batch = eligible
                if not batch:
                    self._finish_write_scope(conn, _sp, ok=True)
                    continue
                batch = [c for c in batch if self._derived_gate_still_valid(conn, c)]
                if not batch:
                    # 本批全部因授权失效被拒：回退本批作用域（不留下任何半成品），继续下一批。
                    # ★G2：走 _finish_write_scope——自持才 rollback；参与调用方事务时
                    #   只回退到本批 savepoint，**绝不**销毁调用方此前的工作。
                    #   ⚠️ 回退失败（G2 第五轮）必须**抛出**：本批"全部被拒"与
                    #      "作用域收口也失败"是两回事，后者要把失败暴露给调用方，
                    #      不能 continue 走人（那又是一种静默假成功）。
                    self._finish_write_scope(conn, _sp, ok=False)
                    continue
                conn.executemany(
                    sql,
                    [
                        (c.id, c.content, c.timestamp, c.importance,
                         json.dumps(c.tags, ensure_ascii=False), c.source, c.token_count, c.content_hash,
                         c.track, c.state, c.superseded_by, c.namespace, c.project, c.host_origin,
                         c.updated_at if c.updated_at is not None else time.time(), c.visibility,
                         c.source_ids, c.derived_from, getattr(c, "tier", "raw") or "raw")
                        + _prov_bind(c)
                        for c in batch
                    ],
                )
                for chunk in batch:
                    if chunk.operation_id and chunk.observation_id:
                        conn.execute("UPDATE chunks SET operation_id=?, observation_id=? WHERE id=?",
                                     (chunk.operation_id, chunk.observation_id, chunk.id))
                        self._store_observation_conn(conn, chunk)
                    tags_json = json.dumps(chunk.tags, ensure_ascii=False)
                    updated_at = chunk.updated_at if chunk.updated_at is not None else time.time()
                    self._store_chunk_conn(conn, chunk, tags_json, updated_at)
                    durable_id = chunk.id
                    if chunk.operation_id:
                        receipt = conn.execute(
                            "SELECT memory_id FROM memory_observations WHERE operation_id=? AND namespace=? AND project=?",
                            (chunk.operation_id, chunk.namespace, chunk.project),
                        ).fetchone()
                        if receipt:
                            durable_id = receipt["memory_id"]
                    row = conn.execute("SELECT rowid FROM chunks WHERE id=?", (durable_id,)).fetchone()
                    if row:
                        ft_content = _tokenize_for_fts5(chunk.content)
                        tags_json = json.dumps(chunk.tags, ensure_ascii=False)
                        conn.execute(
                            """INSERT OR REPLACE INTO chunks_fts(rowid, id, content, tags, source)
                               VALUES (?, ?, ?, ?, ?)""",
                            (row[0], durable_id, ft_content, tags_json, chunk.source),
                        )
                # ★G2：提交权归 _finish_write_scope（参与调用方事务时是 RELEASE，
                #   不是 commit——无条件 commit 会把调用方事务提前提交掉）。
                self._finish_write_scope(conn, _sp, ok=True)
                total += len(batch)
            except Exception as e:
                # ★F1：异常路径必须回退本批（否则半截事务会被后续成功提交带出，
                #   产出"授权依据已失效却落了库"的行——正是 F1/F4 的同一形态）。
                # ★G2：同样走 scope——只撤本批，不销毁调用方的无关工作。
                # ⚠️ 回退失败不得盖掉**原始异常** e（否则日志里只剩次生故障）。
                if _scope_ready:
                    try:
                        self._finish_write_scope(conn, _sp, ok=False)
                    except Exception:
                        logger.exception(
                            "batch_store_chunks: 异常路径回退写作用域失败（G2）offset=%d", offset)
                logger.warning("batch_store_chunks: batch starting at %d failed: %s", offset, e)
                break
        return total

    @staticmethod
    def _row_to_chunk(row) -> Chunk:
        """P1-B / R-06：唯一 row→Chunk 转换器。

        覆盖 schema 全部列；get_chunk / list_chunks 一律调用。
        对缺失/未知列采用不改变权限语义的 fail-closed 默认值：
        - visibility 缺省/未知 → 'private'（绝不乐观归一成 shared）
        - 其余 metadata 缺省为空/['raw']/None。
        """
        keys = row.keys()
        def _get(name, default=None):
            return row[name] if name in keys else default

        vis = _get("visibility", "private")
        if vis is None or vis not in ("shared", "private", "") and not str(vis).startswith("shared-with:"):
            vis = "private"
        # ★B-补（2026-10-13）：v2 provenance 八列的**对称回读**。
        #   为什么必须在这里解析而不是原样返回 JSON 文本：契约 §5 要求"结构化可往返"——
        #   `chunk.source_refs[0]['kind']` 必须能直接取到，取不到就等于"出处不可机器消费"
        #   （R2 判据的载荷形态正是结构化 dict）。写侧 json.dumps、读侧 json.loads，往返逐字相等。
        #   ⚠️【不】用 `or []` 之类的真假值兜底去猜——NULL 就是"这条没带 provenance"
        #      （见 D_PROV_CHUNK_COLS 的缺省语义）。但 Chunk 字段类型是 List[Any]，None 会让
        #      下游 `for x in chunk.source_refs` 当场 TypeError，所以 NULL → [] 是**类型兜底**
        #      而非语义补造：真正的"带没带"由 provenance_status 表征（NULL=旧行/没带）。
        #   ⚠️ 写侧已统一为"scope 也恒为 JSON 文本"（见 _normalize_chunk 末尾），
        #      故这里 source_refs 与 scope 走**完全相同**的 loads 路径，不再有 raw_strings_ok 特例。
        def _json_field(name, default):
            raw = _get(name, None)
            if raw is None or raw == "":
                return default
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    # 解析不了的脏值（历史裸串/人工改库/半截 JSON）：原样保留文本
                    # （至少不丢数据），并留痕便于事后定位。⚠️ 这里【不】raise——
                    # 一条坏行不该让 get_chunk 乃至整批召回全挂（fail-soft；与 visibility 的
                    # fail-closed 不同：visibility 是权限语义猜错会越权，出处只是元数据）。
                    logger.warning("chunks.%s holds non-JSON text, returning raw: %.80r", name, raw)
                    return raw
            return raw

        return Chunk(
            id=_get("id", "") or "",
            content=_get("content", "") or "",
            timestamp=float(_get("timestamp", 0.0) or 0.0),
            importance=float(_get("importance", 0.5) or 0.5),
            tags=json.loads(_get("tags", "[]") or "[]"),
            source=_get("source", "") or "",
            token_count=int(_get("token_count", 0) or 0),
            content_hash=_get("content_hash", "") or "",
            track=_get("track", "default") or "default",
            state=_get("state", "active") or "active",
            superseded_by=_get("superseded_by", "") or "",
            namespace=_get("namespace", "") or "",
            project=_get("project", "") or "",
            host_origin=_get("host_origin", "") or "",
            visibility=vis,
            updated_at=_get("updated_at", None),
            source_ids=_get("source_ids", "[]") or "[]",
            derived_from=_get("derived_from", "") or "",
            tier=_get("tier", "raw") or "raw",
            # ★B-补：provenance 八列。speaker/subject/confirmation_type/provenance_status 是
            #   标量列，原样取回；source_refs/scope 是 JSON 文本列，走 _json_field 结构化还原。
            #   ⚠️ 旧库（未迁移/未写入）这些列可能是 None ⇒ 原样返回 None，【不】编默认值。
            speaker=_get("speaker", None),
            subject=_get("subject", None),
            source_refs=_json_field("source_refs", []),
            scope=_json_field("scope", None),
            valid_from=_get("valid_from", None),
            valid_until=_get("valid_until", None),
            confirmation_type=_get("confirmation_type", None),
            provenance_status=_get("provenance_status", None),
            operation_id=_get("operation_id", None),
            observation_id=_get("observation_id", None),
            request_hash=_get("request_hash", None),
        )

    def get_chunk(self, chunk_id: str) -> Optional[Chunk]:
        """获取单个文本块"""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        return self._row_to_chunk(row)

    def chunk_exists_by_hash(self, content_hash: str) -> bool:
        """检查是否存在相同 hash 的 chunk"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE content_hash = ? AND content_hash != '' LIMIT 1",
            (content_hash,)
        ).fetchone()
        return row is not None

    def find_observation(self, operation_id: str, namespace: str = "", project: str = ""):
        conn = self._get_conn()
        row = conn.execute(
            """SELECT observation_id, operation_id, memory_id, content_hash, request_hash
               FROM memory_observations WHERE operation_id=? AND namespace=? AND project=?""",
            (operation_id, namespace, project),
        ).fetchone()
        return dict(row) if row else None

    def find_observation_by_operation(self, operation_id: str, namespace: str = "", project: str = ""):
        return self.find_observation(operation_id, namespace, project)

    def find_observation_by_memory(self, memory_id: str):
        row = self._get_conn().execute(
            "SELECT observation_id, operation_id, memory_id, content_hash, request_hash FROM memory_observations WHERE memory_id=? ORDER BY created_at DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        return dict(row) if row else None

    def count_observations_for_content_scope(self, content_hash: str, namespace: str = "", project: str = "") -> int:
        row = self._get_conn().execute(
            "SELECT COUNT(*) FROM memory_observations WHERE content_hash=? AND namespace=? AND project=?",
            (content_hash, namespace, project),
        ).fetchone()
        return int(row[0])

    def dedup_by_hash(self) -> int:
        """Count same-scope duplicate candidates without deleting evidence."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT content_hash, namespace, project, COUNT(*) AS total
            FROM chunks WHERE content_hash != ''
            GROUP BY content_hash, namespace, project HAVING COUNT(*) > 1
        """).fetchall()
        return sum(int(row["total"]) - 1 for row in rows)

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

    # 真指针判据（D1-1）：superseded_by 只认这个形状，其余全是「原因」不是「指针」。
    _PTR_RE = re.compile(r"^mem-[0-9a-f]{8}$")

    def mark_superseded(self, target_id: str, source_id: str = None, reason: str = "",
                        by: str = "", successor_id: str = None) -> bool:
        """将旧记忆标记为 superseded（R2.3：同步清 FTS5，防 superseded 幽灵命中）。

        D1-1（2026-09-09 Spec v1.2 §D1-1）指针/原因分离，改这段前先读：
        - superseded_by 从此只留「真指针」或空串。非指针入参（历史上脚本把 cleanup-*/forget
          这类原因字串塞进 source_id，生产库存量 1226 条）自动改道 retire_reason，绝不写回
          superseded_by——写回等于 D1 白做，B4 指针完整性指标立刻回红。
        - reason/by/retired_at 承接「为什么退/谁裁的/何时退」，D2 冲突曝光按 retired_by 归责。
        - successor_id 是 D1-3 蒸馏退役的具名写法（等价 source_id），给一个即可；老调用方
          位置参 (target_id, source_id) 一字不动照常工作（全树 41 处引用）。
        - embedding_cache 清理加了「同 hash 无其他 active」判据：同文重复条目场景下盲删会让
          另一条 active 记忆下次召回现算 embedding。D1 前是无条件删（S3 补的幽灵源头治理，
          测覆盖见 test_xtmem_fts_s2s3.py，别拿这条当回归借口回退判据）。
        """
        conn = self._get_conn()
        succ = successor_id if successor_id else source_id
        succ = "" if succ is None else str(succ)
        _is_ptr = bool(self._PTR_RE.fullmatch(succ))
        ptr = succ if _is_ptr else ""
        why = str(reason or "")
        if not _is_ptr and succ:
            why = why or succ  # 老口径：原因塞在 source_id 位上，保留信息不丢
        # 单A23-补-3①（2026-09-16 小D，Z指导 派单）：退役必须带原因，堵"哑账"入口。
        # 哑账的危害：「被新版本取代」和「误判被清掉」在库里长得一模一样，
        # 将来出岔子根本没法归因——A17/A12 反复出问题的根因之一就是历史决策查不到出处。
        # 分两档，判据刻意分层：
        #   ① 有继任指针 → 派生 'superseded-by:<ptr>'。这是【推导】不是【编造】：
        #      指针就摆在入参里，工单 §2.2② 明确认可这种回填口径。
        #   ② 既无指针又无 reason = 调用方误用（退役却说不清谁接替、为啥退）→ 拒绝写入。
        #      拒写比写空好：写空就是又留一笔查不出出处的账。
        # ⚠️ 现存 6 个调用点（context_builder / distill / distill_llm / profile / timeline /
        #    prune_wiki_raw_cards）【全部】传了 successor 或 source_id ⇒ ②分支当前不会被触发，
        #    它是给未来新调用方的保险。若哪天它真的拒了，说明有新代码在裸调本函数。
        if not why:
            if ptr:
                why = "superseded-by:" + ptr
            else:
                logger.warning(
                    "mark_superseded 拒绝退役 target=%s：无 successor_id/source_id 且无 reason "
                    "（写空 retire_reason 会留下无法归因的哑账，单A23-补-3）", target_id)
                return False
        _now = time.time()
        # S5（2026-09-10 合并单 A-1）：必须同写 updated_at——serve 常驻书架靠
        # "updated_at > 上次 max" 拉差量；只改 state 不 bump 时间戳 = 水位变了但差量 0 条
        # → 退役条永远留在书架里且 doc.state 还是旧快照 active，检索层 state 过滤拦不住
        # = 幽灵可召回。见错题本 #43。对照已正确的 lineage_resolve / prune_wiki_raw_cards。
        cur = conn.execute(
            "UPDATE chunks SET state='superseded', superseded_by=?, retire_reason=?, retired_by=?, "
            "retired_at=?, updated_at=? WHERE id=? AND state='active'",
            (ptr, why, str(by or ""), _now, _now, target_id),
        )
        if cur.rowcount > 0:
            # 与 cmd_forget 同款 rowid 映射删除（FTS5 id 列 UNINDEXED，按 id 删是假删除）
            conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE id=?)",
                (target_id,),
            )
            # S3（2026-09-03）：补删 embedding_cache——旧实现只删 FTS 行不删缓存行=向量腿幽灵源头
            _tc = conn.execute("SELECT content, content_hash FROM chunks WHERE id=?", (target_id,)).fetchone()
            if _tc:
                from memory.utils import compute_embedding_cache_key
                _h = str(_tc["content_hash"] or "")
                _dup_active = 0
                if _h:
                    _dup_active = conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE content_hash=? AND id<>? AND state='active'",
                        (_h, target_id),
                    ).fetchone()[0]
                if not _dup_active:
                    conn.execute(
                        "DELETE FROM embedding_cache WHERE text_hash=?",
                        (compute_embedding_cache_key(str(_tc["content"] or "")),),
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

    # ---------------------------------------------------------------
    # V1.1（#6 反向血缘，2026-09-05，小D）：源收紧 → 派生 stale/跟随降 vis
    # ---------------------------------------------------------------
    def _find_derived_of(self, conn, src_id):
        """查 source_ids 含 src_id 的 active 派生 chunk id（LIKE 带引号防半匹配）。

        source_ids 是 JSON 数组文本（["mem-xxx",...]）；diary-distill 存文件名
        （非 chunk 血缘）天然不匹配 chunk id——LIKE '%"mem-xxx"%' 只咬 JSON 字符串值。
        """
        rows = conn.execute(
            "SELECT id FROM chunks WHERE derived_from != '' AND state='active' "
            "AND source_ids LIKE ?",
            ('%"' + src_id + '"%',),
        ).fetchall()
        return [r[0] for r in rows]

    def propagate_source_archived(self, src_id, reason="archive"):
        """V1.1（#6 反向血缘）：源 chunk 被 forget/archive → 指向它的 active 派生打 stale。

        源 archived = 主动删/遗忘 → 派生仍 active 但基于失效源，不该继续当活知识注入。
        打 tags 'lineage-stale:<src8>'（不改 state：保留可检索/可恢复，等 cron 重蒸换新）。
        ⚠️ 只处理 archived/forget（主动删）；源 superseded = 演化正常覆盖不触发
        （聚合型派生引多代 superseded raw 是设计：摸底 430 superseded vs 3 archived）。
        invalidate_evolution【不】动——只管演化边；本方法只管 chunks 血缘。
        事件留痕 kind='lineage-stale'。
        """
        conn = self._get_conn()
        derived = self._find_derived_of(conn, src_id)
        if not derived:
            return 0
        import time as _t
        now = _t.time()
        tag = "lineage-stale:" + str(src_id)[-8:]
        for cid in derived:
            row = conn.execute("SELECT tags FROM chunks WHERE id=?", (cid,)).fetchone()
            try:
                tags = json.loads(row["tags"] or "[]") if row else []
                if not isinstance(tags, list):
                    tags = []
            except Exception:
                tags = []
            if tag not in tags:
                tags.append(tag)
            conn.execute(
                "UPDATE chunks SET tags=?, updated_at=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), now, cid),
            )
        conn.execute(
            "INSERT INTO xtmem_events (ts, kind, meta) VALUES (?, 'lineage-stale', ?)",
            (now, json.dumps({
                "source_id": src_id, "reason": reason, "derived": derived,
                "n": len(derived), "tag": tag, "by": "V1.1-propagate",
            }, ensure_ascii=False)),
        )
        conn.commit()
        return len(derived)

    def follow_source_visibility(self, src_id, src_vis):
        """V1.1（#6 反向血缘）：源 vis 收紧 → 派生 vis 跟随降级。

        漏洞面：源 private 派生仍 shared = 明文越源泄漏。收紧时立即降（不等 cron）。
        放宽（private→shared）不触发——min 语义只会更严不会更宽。
        事件留痕 kind='lineage-vis-follow'。

        ★M03（2026-10-13）重写：**改为重算每条派生的「全部来源读者交集」**，不再做
          「源新值 × 该派生当前值」的两者 min。

        为什么必须改（旧实现的漏洞，不是风格问题）：
          旧写法 `min_visibility([src_id, cid])` 只把**被收紧的那一个源**与派生当前值比。
          多源派生（A+B+C 合成）里收紧 A 时，B、C 的读者集合根本没被重新纳入计算，
          于是新值可能是"A 与派生旧值的较严者"，而不是"A∩B∩C"——**交集算错**，
          既可能漏收（该更严却没更严）也可能把 B/C 的读者放进来（越权）。
          M03 还要求「读者集合」而非"字串最严"，两者跨域时不等价（见 visibility.py 抬头）。

        ⚠️ 保留本方法名与返回类型（返回"变了多少条"的 int）：既有调用方
           `schema.quarantine_bad_visibility` 与 `tests/test_xtmem_v11_lineage.py`
           锁定的是这个签名与语义（它们断言的是"收紧后不越源"，仍然成立且更严）。
        ⚠️ 真正干活的逻辑在 SourceResolver.tighten_source()——那里也负责把
           「编码不出来的交集」**隔离/失效**（Spec §9.3「保留受限归属」）。
           本方法只做"把当前值写进 DB 后再让 resolver 重算"这一层适配。

        ★★R2-1（2026-10-14 独立验收返修）：**不再自己先 UPDATE+commit**。
        病灶（实测，见 m03-review/evidence/boundary-results.json 的
              I10-existing-follow-entry-propagates / I10-follow-after-source-update-propagates）：
          旧实现先把源 UPDATE 并 **commit**，再调 tighten_source；后者看"值已相等"
          便提前返回 changed=0 ⇒ **本方法实际什么都不传播**（返回恒 0）。
          而且"来源先被调用方改掉、再调 follow"这条真实调用序（schema.quarantine 就是）
          同样命中。
        ⇒ 修法：本方法只负责**把调用方给的目标值交给 resolver 去落**，
          UPDATE 由 resolver 统一做（它是唯一入口，铁律 5）。
        ⚠️ 事务归属：resolver 现在只在"事务由它自己开启"时才 commit（R4-2）。
          所以这里**不能**替它先 commit——那正好会把调用方（如 quarantine）的事务切碎。
        """
        from memory.source_resolver import get_resolver
        conn = self._get_conn()
        res = get_resolver(conn).tighten_source(str(src_id), str(src_vis))
        if not res.get("ok"):
            logger.warning(
                "follow_source_visibility 未生效：src=%s src_vis=%r error=%r",
                src_id, src_vis, res.get("error"))
            return 0
        # 返回值语义保持"变了多少条"（含被隔离的，它们也是"权限面被收掉了"的变更）
        return int(res.get("derived_tightened", 0) or 0)

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

    def search_bm25(self, query: str, top_k: int = 20, viewer: str = None) -> List[Tuple[str, float]]:
        """BM25 全文搜索（S3 过滤；G2 起收口到模块函数 fts_search_bm25 供 hybrid 只读复用）。

        ★M03：viewer 缺省从 '__admin__' 改为 None（未声明身份 ⇒ 只放行 shared）。
        理由与实测证据见 fts_search_bm25() 的 docstring——"少传一个参数就变管理员"是旁路。
        """
        # 单A24 §四（B1 拆雷，2026-09-16 小D）：原实现是
        #     conn = self._get_conn()
        #     try:    return fts_search_bm25(conn, ...)
        #     finally: conn.close()          ← 【已删除，勿加回】
        # ⚠️ 为什么那是雷：_get_conn() 返回的是【引擎常驻单例】（见本文件 __init__ 的
        #   self._conn 缓存 + _get_conn 的懒加载），不是本函数自己开的连接。
        #   谁调用本函数一次 = 把整个引擎的数据库连接关掉 ⇒ 之后引擎所有库操作全炸，
        #   而且报错点离肇事点很远，极难定位。引擎是常驻进程，自己【没有】"关闭"的语义
        #   （要真关，走 VectorStore.close() 那个显式出口）。
        # 实测该函数当前零调用点（AST 数过），所以是"埋着的雷"而非"正在炸"。
        # 保留函数本身：它是个合法的查询封装，只是不该管连接生命周期。
        conn = self._get_conn()
        return fts_search_bm25(conn, query, top_k=top_k, viewer=viewer)

    def search_vector(self, query: str, embed_fn, top_k: int = 20, viewer: str = None) -> List[Tuple[str, float]]:
        """向量相似度搜索（numpy 加速，fallback 纯 Python）

        ★M03：viewer 缺省从 '__admin__' 改为 None（未声明身份 ⇒ 只放行 shared）。
        理由与实测证据见 fts_search_bm25() 的 docstring。

        P1-C / R-07：
        - 每行按自己的 dim 反序列化（不再复用首行 dim）。
        - chunk_id 为 NULL 或 orphan=1 时用 text_hash 反查 chunks（与写入一致的缓存键
          compute_embedding_cache_key(content)），查到才返回并回填 chunk_id，查不到按孤儿跳过并告警。
        """
        query_vec = embed_fn(query)

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT text_hash, text, embedding, dim, chunk_id, orphan FROM embedding_cache ORDER BY created_at DESC LIMIT 10000"
        ).fetchall()

        if not rows:
            return []

        from memory.utils import compute_embedding_cache_key

        # 反序列化所有 embeddings（每行按自身 dim，P1-C）
        all_vecs = []
        for row in rows:
            all_vecs.append(self._deserialize_embedding(row["embedding"], row["dim"]))

        # 计算批量余弦相似度（P1-C：异构 dim 不能进 numpy 2D，按自身 dim 逐行算）
        sims = []
        if len({row["dim"] for row in rows}) == 1:
            try:
                import numpy as np
                q = np.array(query_vec, dtype=np.float32)
                m = np.array(all_vecs, dtype=np.float32)
                q_norm = q / (np.linalg.norm(q) + 1e-8)
                m_norm = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-8)
                sims = np.dot(m_norm, q_norm)
            except (ImportError, ValueError):
                sims = [self._cosine_similarity(query_vec, v) for v in all_vecs]
        else:
            sims = [self._cosine_similarity(query_vec, v) for v in all_vecs]

        # 排序取 top_k
        indices = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)

        results = []
        for i in indices:
            if sims[i] <= 0:
                continue
            row = rows[i]
            cache_key = row["text_hash"]
            cid = row["chunk_id"]
            orphan = row["orphan"] if "orphan" in row.keys() else 0
            # 同文可对应多个owner/project的独立观察；cache按正文hash共享向量，不能把单个chunk_id当唯一事实。
            # 用缓存键枚举所有匹配active chunk，再逐条过权限；否则先写入者会把其他owner的向量腿吞掉。
            active_rows = conn.execute(
                "SELECT id, content, namespace, visibility FROM chunks WHERE state='active'"
            ).fetchall()
            matching = [cr for cr in active_rows
                        if compute_embedding_cache_key(str(cr["content"] or "")) == cache_key]
            if not matching:
                if not cid or orphan:
                    logger.warning("search_vector: skip orphan embedding_cache row text_hash=%s", cache_key)
                continue
            # chunk_id 缺失或标为 orphan 时维持兼容回填；检索结果仍按hash映射枚举全部观察。
            if (not cid or orphan) and matching:
                cid = matching[0]["id"]
                conn.execute(
                    "UPDATE embedding_cache SET chunk_id=?, orphan=0 WHERE text_hash=?",
                    (cid, cache_key),
                )
            # S3（2026-09-03）：回验 chunks——state 非 active / visibility 不过滤一律跳过（F-5 幽灵封口）
            # ★M03：viewer=None（未声明身份）→ `viewer or ""` = 空身份 → fail-closed 只放行 shared。
            from memory.visibility import visibility_ok
            for _cr in matching:
                if not visibility_ok(str(_cr["visibility"] or ""),
                                     str(_cr["namespace"] or ""), viewer or ""):
                    continue
                candidate_id = _cr["id"]
                if candidate_id not in {existing[0] for existing in results}:
                    results.append((candidate_id, float(sims[i])))
                if len(results) >= top_k:
                    break
            if len(results) >= top_k:
                break

        conn.commit()
        return results

    def batch_embed(self, texts: List[str], model: str = "simple", chunk_ids: Optional[List[str]] = None) -> List[List[float]]:
        """批量计算并缓存 embedding 向量。

        P1-C / R-07：写入 embedding_cache 时必带 chunk_id（调用方 hydrate/store 传对应 chunk id），
        并把原文存入 text 列，便于孤儿反查与存量迁移。chunk_ids 与 texts 长度需一致（缺省不传则 chunk_id=NULL）。
        """
        if not texts:
            return []

        results: List[List[float]] = []
        conn = self._get_conn()
        now = time.time()

        for i, text in enumerate(texts):
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
                cid = chunk_ids[i] if chunk_ids and i < len(chunk_ids) else None
                # text 列不存明文（安全）；只存 text_hash，占位 ''
                conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache (text_hash, text, embedding, dim, model, created_at, chunk_id, orphan) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (text_hash, '', blob, len(vec), model, now, cid),
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
            INSERT OR REPLACE INTO embedding_cache (text_hash, text, embedding, dim, model, created_at, chunk_id, orphan)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            # text 列不存明文（安全），只存 text_hash；orphan 反查走 chunks.content 缓存键
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

    def backfill_embedding_cache_orphans(self) -> dict:
        """P1-C / R-07：存量迁移——回填 embedding_cache 缺失的 chunk_id。

        对 chunk_id 为 NULL/'' 或 orphan=1 的行：
        - 按与写入一致的缓存键 compute_embedding_cache_key(chunks.content) 反查 active chunks 匹配；
        - 匹配 → UPDATE chunk_id, orphan=0；
        - 不匹配 → UPDATE orphan=1（孤儿，保留但不进检索）。

        返回 {'matched': n, 'orphaned': m, 'still_missing': k}。
        注意：本方法只改 embedding_cache，不改 chunks、不删数据。
        """
        conn = self._get_conn()
        from memory.utils import compute_embedding_cache_key
        rows = conn.execute(
            "SELECT text_hash, text FROM embedding_cache WHERE chunk_id IS NULL OR chunk_id='' OR orphan=1"
        ).fetchall()
        matched = orphaned = 0
        for row in rows:
            cache_key = row["text_hash"]
            hit = None
            for cr in conn.execute("SELECT id, content FROM chunks WHERE state='active'"):
                if compute_embedding_cache_key(str(cr["content"] or "")) == cache_key:
                    hit = cr["id"]
                    break
            if hit:
                conn.execute("UPDATE embedding_cache SET chunk_id=?, orphan=0 WHERE text_hash=?", (hit, cache_key))
                matched += 1
            else:
                conn.execute("UPDATE embedding_cache SET orphan=1 WHERE text_hash=?", (cache_key,))
                orphaned += 1
        conn.commit()
        still = conn.execute(
            "SELECT COUNT(*) FROM embedding_cache WHERE chunk_id IS NULL OR chunk_id='' OR orphan=1"
        ).fetchone()[0]
        return {"matched": matched, "orphaned": orphaned, "still_missing": still}

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
        return [self._row_to_chunk(row) for row in rows]

    def iter_chunks_all(self, state: str = "active", page: int = 2000,
                        source: Optional[str] = None,
                        max_rows: int = 500_000) -> List[Chunk]:
        """D1-3（2026-09-09 Spec v1.2）分页全量取 chunk，取代 list_chunks(limit=10_000) 的静默截断（B14 触顶）。

        为什么用 rowid keyset 而不是 OFFSET：OFFSET 每页都要扫过前面所有行（整库水化退化成 O(n²)），
        且并发写入时页与页之间会重复或漏行。rowid 是 SQLite 隐式主键、稳定单调，
        「rowid < 上页最小值」每页走索引，两种失真都免疫。
        为什么还要 max_rows：B14 的原罪是**静默**丢数据（截断了没人知道）。超上限改成显式告警，
        宁可吵一行 log 也别悄悄少一截书架——少的那部分只表现为「记了但查不到」，事后无从定位。
        注意 _row_to_chunk 按列名取值（keys() 判空），多带一个 rowid 列不影响它。
        """
        conn = self._get_conn()
        out: List[Chunk] = []
        last = 9223372036854775807  # 首页从最大 rowid 起，等价无下界
        while True:
            if source:
                rows = conn.execute(
                    "SELECT rowid, * FROM chunks WHERE state=? AND source=? AND rowid<? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (state, source, last, page),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rowid, * FROM chunks WHERE state=? AND rowid<? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (state, last, page),
                ).fetchall()
            if not rows:
                break
            out.extend(self._row_to_chunk(r) for r in rows)
            last = rows[-1]["rowid"]
            if len(rows) < page:
                break
            if len(out) >= max_rows:
                logger.warning(
                    "iter_chunks_all 触顶 max_rows=%d (state=%s)——显式告警，非静默截断；请查库是否异常增长",
                    max_rows, state,
                )
                break
        return out

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

    # B4 五态效用权重表（2026-09-07 SupportBundle Spec：曝光低保底，使用高权重；
    # 评分权重表 B2 阶段细调，本批先落事件与记账分离的分层口径）
    UTILITY_STATE_WEIGHTS = {
        "recalled": 0.02,
        "exposed": 0.1,
        "cited": 0.5,
        "confirmed": 1.0,
        "corrected": 0.8,
    }

    # ── A16（2026-09-16，小D 提出 / Z指导 裁定）：五态 → 计数列 映射 ──────────────
    # ⚠️【单一事实源】UTILITY_STATE_WEIGHTS 的【每个 key 都必须在这里有列映射】。
    #    否则该态会出现最阴的假绿：校验通过 → delta 按权重分层加了 utility_score
    #    → 但 `if col:` 不成立 ⇒ 【一个计数列都不递增】——"记了却看不见"。
    #    recalled 就是这么丢的：权重表写了 0.02（设计意图就是"要记"），映射表漏了它，
    #    于是全库 recalled 从来没记上过（含 provider.py 那条路径，同走本函数）。
    #    守卫 tests/test_utility_state_guard.py 盯着两表 key 一致：缺映射即红。
    _state_cols = {
        "recalled": "recalled_n",
        "exposed": "exposed_n",
        "cited": "cited_n",
        "confirmed": "confirmed_n",
        "corrected": "corrected_n",
    }
    # 幂等补列清单：与 _state_cols 的【值集】同源，勿手抄第三份（抄了就会跟上面脱节）
    _state_cols_all = tuple(_state_cols.values())

    def increment_utility(self, chunk_id: str, delta: float = 0.1, viewer: str = "",
                          state: str = "") -> None:
        """递增 chunk 效用分数（P0.6：按 viewer 计，不影响他人视角）。

        B4（2026-09-07 SupportBundle Spec）：五态记账分离——
        state ∈ recalled/exposed/cited/confirmed/corrected 时：
          ① 对应 *_n 计数列 +1（幂等 ALTER，旧库自动补列）；
          ② delta 未显式传时按 UTILITY_STATE_WEIGHTS 分层取权；
          ③ state 传空 = 旧口径兼容（只累加不计数）。
        """
        conn = self._get_conn()
        # 幂等补列（旧库迁移；ALTER 失败=列已在，忽略）
        # A16（2026-09-16）：清单改由 _state_cols_all 派生——原来硬写四列，正是
        # "权重表有 recalled、列没有"这条断链的另一半。换态只改 _state_cols 一处。
        #
        # ★R01-FIX1（2026-10-21，F3）：last_access_useful 也在这里补列——
        #   它是「真实有用时间」的唯一载体，必须和五态计数列一起走同一个
        #   幂等迁移入口（读路径不偷偷 DDL，见 get_utility 的说明）。
        #   ⚠️ 新列默认 NULL 而不是 0：NULL = **未知**（旧账从来没记过真实有用时间），
        #      0 = 1970 年，会被新鲜度算成"极度陈旧"，两种语义不能混。
        for _ddl in [f"ALTER TABLE chunk_utility ADD COLUMN {c} INTEGER DEFAULT 0"
                     for c in self._state_cols_all] +                     ["ALTER TABLE chunk_utility ADD COLUMN last_access_useful REAL DEFAULT NULL"]:
            try:
                conn.execute(_ddl)
            except Exception:
                pass
        state = state if state in self.UTILITY_STATE_WEIGHTS else ""
        _now_useful = time.time()   # F3：与下面 INSERT 用同一个时刻，避免两处时间漂移
        if state and delta == 0.1:  # 未显式定制 delta 时按态分层
            delta = self.UTILITY_STATE_WEIGHTS[state]
        # D0-1（2026-09-09，小D）：corrected 不再正向累加 utility_score，仅递增 corrected_n
        if state == "corrected":
            delta = 0.0
        col = self._state_cols.get(state)
        conn.execute(
            """
            INSERT INTO chunk_utility (chunk_id, viewer_agent, utility_score, access_count, last_access)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chunk_id, viewer_agent) DO UPDATE SET
                utility_score = utility_score + excluded.utility_score,
                access_count = access_count + 1,
                last_access = excluded.last_access
            """,
            # R01·RQ05：delta 已在上面按态分层。recalled/exposed 的权重仍会写进
            #   utility_score 列（历史账保持真实、可审计），但【读取面】get_utility
            #   只采信 cited/confirmed —— 写入与读取分开，不改过去的数据。
            (chunk_id, viewer or "", delta, _now_useful),
        )
        if col:
            conn.execute(
                f"UPDATE chunk_utility SET {col} = {col} + 1 WHERE chunk_id = ? AND viewer_agent = ?",
                (chunk_id, viewer or ""),
            )
        # ★R01-FIX1（F3，独立验收原话）：「真实有用时间只由对应事件更新」。
        #   只有 cited/confirmed 是"真被用过" ⇒ 才推进 last_access_useful。
        #   recalled/exposed 照旧记账（计数 +1、last_access 刷新 —— 那是对外可审计的
        #   访问流水，保留），但【不碰】last_access_useful。
        #   ⚠️ 这里不写 last_access（上面那条 INSERT/UPDATE 已经写了），只补有用时间，
        #      避免把"展示"混进"有用"。
        if state in self.UTILITY_SCORING_STATES:
            conn.execute(
                "UPDATE chunk_utility SET last_access_useful = ? WHERE chunk_id = ? AND viewer_agent = ?",
                (_now_useful, chunk_id, viewer or ""),
            )
        conn.commit()

    # ══════════════════════════════════════════════════════════════════
    # R01·RQ05（2026-10-21 小D）：召回/展示 ≠ 有用反馈
    #
    # 病灶（核查结论.md §一.3，生产账本实测）：
    #   recalled(0.02) 与 exposed(0.1) 都是【正向】累加进 utility_score，
    #   而 utility_score 又在排序里加分、last_access 又被当成"新鲜度"。
    #   结果是"越被拿出来看，越像新鲜有用"——旧回执 568/386 次展示、0 次引用，
    #   分数却被抬到 0.437（向量 0.270 + 新鲜度 0.100 + 重要性 0.035 + 热度）。
    #   展示=广告播放量，引用/确认=真实购买，两者语义不同，不能同一个池子加分。
    #
    # 修法（两件事分开，且都【不】动历史账）：
    #   ① 记账照旧：recalled_n / exposed_n 计数继续 +1（真实性/可审计性不变）
    #   ② 但 utility_score 的【正向累加】只对 cited/confirmed 生效；
    #      recalled/exposed 只记账、不加分。历史已累加的分不回收、不篡改
    #      （§9.7.2-4「保留历史分账」「不删账清库治排序」），
    #      新政策从这里往后生效。
    #   ⚠️【不】把 recalled/exposed 的权重改成负数：那等于用"惩罚曝光"治病，
    #     会让正常热门内容被压死，且历史账会被反向解读。
    #   ⚠️ read_semantic=False 时可完全回到旧口径（灰度/回滚开关）。
    # ══════════════════════════════════════════════════════════════════

    # 哪些态算"真实有用反馈"（可正向加分）；其余态只记账
    UTILITY_SCORING_STATES = ("cited", "confirmed")

    def get_utility(self, chunk_id: str, viewer: str = "") -> Optional[dict]:
        """获取 chunk 效用统计（P0.6：按 viewer 维度）。

        R01·RQ05（2026-10-21）：本函数是【排序读取面】。默认返回的 utility_score
        已经剔除了 recalled/exposed 的历史累计贡献，只用 cited/confirmed 作为
        "真实有用"信号；last_access 的语义拆成两个键：
          · last_access        —— 原始值（记账/审计用，保持真实）
          · last_access_useful —— 只有 cited/confirmed 事件才写入它，供"新鲜度"排序用

        ★R01-FIX1（2026-10-21，独立验收 F1）：**首次读取必须能跑**。
          旧写法直接 SELECT recalled_n/exposed_n/...，而这些列只由 increment_utility
          按需 ALTER 补出来 —— 一个刚建好、还没记过账的库（新 profile / 隔离库 /
          第一次正常召回）根本没有这些列，于是第一条正常查询就
          OperationalError: no such column: recalled_n，rc=1。
          **不能要求用户先 expose 一次来"修库"**（验收原话），也不能靠预先记账掩盖。
          修法：按当前表的**实际列**动态拼 SELECT，缺列按 0 处理；
          补列仍走唯一合法入口 increment_utility 的幂等 ALTER（不在读路径偷偷 DDL——
          读路径 DDL 会在只读连接/并发下炸，且绕过公共迁移闸）。
          ⇒ 无账 = 正常空统计；有历史账 = 原值；账列存在与否都不影响读取。
        ★R01-FIX1（F3）：last_access_useful 现在读**独立列**，不再由 last_access 推导。
        read_semantic=False 时原样返回（旧口径，灰度/回滚）。
        """
        conn = self._get_conn()
        cols = self._utility_columns(conn)
        _present = [c for c in self._state_cols_all if c in cols]
        _sel = ["utility_score", "access_count", "last_access"]
        _sel += ["%s AS %s" % (c, c) if c in cols else ("0 AS %s" % c) for c in self._state_cols_all]
        _sel.append("last_access_useful AS last_access_useful"
                    if "last_access_useful" in cols else "NULL AS last_access_useful")
        row = conn.execute(
            "SELECT " + ", ".join(_sel) + " FROM chunk_utility "
            "WHERE chunk_id = ? AND viewer_agent = ?",
            (chunk_id, viewer or ""),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        if not getattr(self, "utility_read_semantic", True):
            return out
        # 用计数列重算"真实有用"部分：与 increment_utility 的权重表同源，
        # 不在这里另写一份数字（防止两处权重漂移，见 UTILITY_STATE_WEIGHTS 单一源）。
        useful = 0.0
        for st in self.UTILITY_SCORING_STATES:
            _n = int(out.get(st + "_n") or 0)
            if _n:
                useful += _n * float(self.UTILITY_STATE_WEIGHTS.get(st, 0.0))
        out["utility_score_raw"] = out.get("utility_score") or 0.0
        out["utility_score"] = useful
        # 新鲜度读取面（F3）：只认独立记录的真实有用时间。
        #   ⚠️ 旧账没有这一列 / 值为 NULL ⇒ 明确「未知」，【不】拿 last_access 补造
        #      （last_access 仍会被 exposed/recalled 刷新，正是"展示续命"的病根）。
        out["last_access_raw"] = out.get("last_access")
        out["last_access_useful"] = out.get("last_access_useful") or None
        return out

    def _utility_columns(self, conn) -> set:
        """当前 chunk_utility 表的实际列名集合（F1：读路径不假设列存在）。"""
        try:
            return {str(r[1]) for r in conn.execute("PRAGMA table_info(chunk_utility)")}
        except Exception:
            return {"chunk_id", "viewer_agent", "utility_score", "access_count", "last_access"}

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
        # 叁②（防再删三层，v1.3.2）：批量删守卫——>500 行须旗语，否则 raise（本函数
        # 外层无事务包裹，raise 前守卫自身已写事件）
        from memory.open_db import guard_bulk_cache_delete
        guard_bulk_cache_delete(conn, len(hashes), where="LRU eviction")
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
            if not _tokenizer_version_ok(conn):
                raise RuntimeError("tokenizer_version mismatch; refusing rebuild")
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

            # 2. 逐行走 _tokenize_for_fts5（S2 D7：删除直拷 SQL，彻底消除 raw 源）
            for _r in conn.execute(
                "SELECT rowid, id, content, tags, source FROM chunks"
            ).fetchall():
                conn.execute(
                    "INSERT INTO chunks_fts_new(rowid, id, content, tags, source) VALUES (?,?,?,?,?)",
                    (_r["rowid"], _r["id"], _tokenize_for_fts5(str(_r["content"] or "")),
                     _r["tags"], _r["source"]),
                )

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
