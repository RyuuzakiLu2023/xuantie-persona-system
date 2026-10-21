# -*- coding: utf-8 -*-
"""evidence_admission.py — 证据准入的唯一可复用核心（R01-FIX1，2026-10-21 小D）

这是什么：回答一个问题——**这条候选凭什么算作「回答了这个问题」的证据**。
为什么单独成模块：独立验收 F4 的原话是「主检索/补召/关联扩展/最终包使用统一可追溯结果」，
  而此前主路在 hybrid_retriever 里有一套判据、补召在 CLI 里另抄了一套（非锚内容词+分档赋分）、
  hook 出口什么都不查。三处各说各话 = 正门与侧门拿不同规则盖章。
  §9.7.3：统一准入应由可复用核心给出，端侧只薄适配/检查准入结果。

判据（全部纯字符串/数值运算，不查库、不调模型）：
  ① 严格标识符（strict id）：query 里出现 ZXQ731920 / C03 / lowres_scale 这类**带数字或下划线的
     标识符**时，正文必须命中其中至少一个。⇒ 防「明确标识符因泛词重合被无关标识符替代」
     （实测：库里只有 ABC998877 记录，问 ZXQ731920 项目部署进展 仍返回 ABC）。
  ② 内容词（content term）：>=2 字中文词 / >=3 字符 ASCII 词，通用词表里的不算
     （NULL/项目/部署/进展这类泛词不能单独当证据）。
  ③ 语义够近：没有字面证据时，余弦要够 vector_admission_min 才算「够近」。

输出一律带 reason/basis 标签，供 mode/verdict/basis 与 hook 出口消费——**可追溯**是硬要求。

⚠️【不】在本模块里做加分排序：准入=入场券，排序=座位，两件事分开（RQ02）。
⚠️【不】复制第二份相似度阈值到端侧：端侧只读这里给出的 admitted 布尔与 reason。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

# 通用词表（与 bundle_selector 的黑名单同源；这里只做「不算证据」判定）
_GENERIC = frozenset(
    {"null", "none", "nil", "undefined", "nan", "true", "false", "bool",
     "select", "insert", "update", "delete", "from", "where", "join", "table",
     "index", "primary", "key", "value", "column", "row", "query", "sql", "db",
     "and", "or", "not", "is", "in", "on", "as", "by", "to", "of", "at",
     "id", "pid", "uid", "api", "url", "uri", "http", "https", "json", "yaml",
     "def", "class", "import", "return", "self", "print", "str", "int", "dict",
     "error", "exception", "warn", "info", "debug", "test", "main", "init", "run",
     "get", "set", "add", "fix", "log", "mem", "tmp", "temp", "data", "file",
     "path", "dir", "name", "type", "size",
     "项目", "部署", "进展", "内容", "记录", "情况", "问题", "东西", "这个", "那个",
     "服务", "版本", "完成", "已经", "可以", "什么", "怎么", "如何"}
)

# 严格标识符：带数字的 ASCII token（ZXQ731920 / C03 / R2 / h3）或含下划线的技术名
_STRICT_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*[0-9][A-Za-z0-9_.\-]*|[A-Za-z0-9]+_[A-Za-z0-9_]+")
_ASCII_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{2,}")


def strict_identifiers(query: str) -> List[str]:
    """抽出 query 里的严格标识符（带数字/下划线）。空列表=没有硬标识符约束。"""
    q = query or ""
    out, seen = [], set()
    for m in _STRICT_ID_RE.finditer(q):
        tok = m.group(0).strip("._-")
        if len(tok) < 3:
            continue
        low = tok.lower()
        if low in _GENERIC or low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return out


def content_terms(query: str) -> List[str]:
    """抽出 query 的内容词：中文>=2字 + ASCII>=3字符，去通用词，保序去重。"""
    q = (query or "").strip()
    if not q:
        return []
    terms, seen = [], set()
    for m in _ASCII_TERM_RE.finditer(q):
        tok = m.group(0).strip("._-")
        if not tok:
            continue
        low = tok.lower()
        if low in _GENERIC or low in seen:
            continue
        seen.add(low)
        terms.append(tok)
    try:
        import jieba
        words = [w for w in jieba.cut(q, cut_all=False) if len(w) >= 2]
    except Exception:
        cn = [c for c in q if "\u4e00" <= c <= "\u9fff"]
        words = ["".join(cn[i:i + 2]) for i in range(len(cn) - 1)]
    for w in words:
        low = w.lower()
        if not w.strip() or low in seen or low in _GENERIC:
            continue
        seen.add(low)
        terms.append(w)
    return terms


def literal_hits(query: str, content: str, terms: Optional[Sequence[str]] = None) -> List[str]:
    """正文里真正出现的内容词列表（空=没有字面证据）。"""
    body = (content or "").lower()
    if not body:
        return []
    terms = list(terms) if terms is not None else content_terms(query)
    return [t for t in terms if t.lower() in body]


def has_literal_evidence(query: str, content: str, terms: Optional[Sequence[str]] = None) -> bool:
    """有字面证据 = 至少一个内容词命中（单字不算，通用词已在抽词时剔除）。"""
    return bool(literal_hits(query, content, terms))


def assess(query: str, content: str, *, raw_bm: float = 0.0, raw_vec: float = 0.0,
           ttl_decay: float = 1.0, importance: float = 0.0,
           relevance_threshold: float = 0.15, vector_admission_min: float = 0.5,
           anchor_admission_ratio: float = 0.5, terms: Optional[Sequence[str]] = None,
           strict_ids: Optional[Sequence[str]] = None) -> dict:
    """统一证据准入：返回可追溯的判定结果（主路/补召/关联扩展共用这一份）。

    入参：
      raw_bm / raw_vec / ttl_decay —— 主路把它算好的相关性分量传进来；
        补召没有向量分，传 raw_bm=1.0（它本来就是字面 LIKE 命中）即可，语义一致。
      terms / strict_ids —— 可选预抽，避免循环里重复分词（纯优化，不传就现抽）。
    返回 dict：
      admitted      —— 是否准入
      score         —— 相关性得分（(bm+vec)*ttl_decay），排序/门槛用
      has_lexical / has_semantic / lexical_ok —— 各分项
      anchor_ok     —— 是否走锚点豁免（重要且确有字面证据）
      reason        —— 机器可读原因（供 mode/verdict/basis 与日志）
      basis         —— 人读依据（写进返回体，说明「凭什么算证据」）
    """
    terms = list(terms) if terms is not None else content_terms(query)
    strict_ids = list(strict_ids) if strict_ids is not None else strict_identifiers(query)
    body = (content or "").lower()

    hits = [t for t in terms if t.lower() in body]
    # ① 严格标识符硬约束：query 有硬标识符时，正文必须命中至少一个
    id_hits = [i for i in strict_ids if i.lower() in body]
    id_required = bool(strict_ids)
    id_ok = (not id_required) or bool(id_hits)

    # ② 单词法证据：内容词命中；但若 id 硬约束未满足，字面证据不算数
    has_lexical = bool(hits) and id_ok
    # ③ 语义：只有显式传了 raw_vec 才可能成立（补召没有向量分）
    has_semantic = float(raw_vec or 0.0) >= float(vector_admission_min)
    lexical_ok = has_lexical or has_semantic

    score = (float(raw_bm or 0.0) + float(raw_vec or 0.0)) * float(ttl_decay or 1.0)
    over_threshold = score >= float(relevance_threshold)
    anchor_ok = bool(has_lexical and float(importance or 0.0) >= 0.75
                     and score >= float(relevance_threshold) * float(anchor_admission_ratio))

    if not id_ok:
        reason = "identifier-mismatch"
        basis = "提问中的标识符未在正文出现（关键词重合不能替代具体标识符）"
        admitted = False
    elif not lexical_ok:
        reason = "no-evidence"
        basis = "既无内容词命中，语义相似度也不足"
        admitted = False
    elif not over_threshold and not anchor_ok:
        reason = "below-threshold"
        basis = "相关性得分未达准入线"
        admitted = False
    else:
        reason = "admitted"
        parts = []
        if hits:
            parts.append("命中内容词 %s" % (",".join(hits[:4])))
        if id_hits:
            parts.append("命中标识符 %s" % (",".join(id_hits[:3])))
        if has_semantic and not has_lexical:
            parts.append("语义相似 %.3f" % float(raw_vec))
        basis = "；".join(parts) or "通过准入"
        admitted = True

    return {
        "admitted": admitted,
        "score": round(score, 6),
        "has_lexical": has_lexical,
        "has_semantic": has_semantic,
        "lexical_ok": lexical_ok,
        "id_required": id_required,
        "id_ok": id_ok,
        "id_hits": id_hits,
        "term_hits": hits,
        "anchor_ok": anchor_ok,
        "reason": reason,
        "basis": basis,
        "strict_ids": strict_ids,
    }
