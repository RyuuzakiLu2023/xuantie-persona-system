# -*- coding: utf-8 -*-
"""memory/ner_tagger.py — XTMEM 实体地基 NER 打标器（P4.9-f，2026-09-02，小D）。

把 chunk 正文抽成 canon 实体 + 正文时间基准，供 P4.9-h `--entity` 过滤与 P5.0 图谱/时间骨架用。

设计：
  - 实体：词典匹配（alias→canon，来自 entity_aliases 表；预置四 agent 名 + 大小写变体 + 项目名）。
         jionlp LexiconNER 若可用则优先，否则退化为确定性子串匹配（零依赖、可解释、永不挂）。
  - 时间：jionlp TimeExtractor（`jionlp.algorithm.ner.time_extractor.TimeExtractor`，构造无参、time_base 调用时传）。
  - 降级链（学 embedding_provider.py:152-182）：单批失败零填不 raise；60s 冷却。

用法：from memory.ner_tagger import extract_entities, extract_time, canon_of
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime  # P4.9 审红一（2026-09-02）：模块级导入，避免 :extract_time 内迟导入被 except 吞

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db")

# 冷却：60s 内失败不重试（学 embedding_provider 降级）
_NER_FAIL_AT = 0.0
_NER_COOLDOWN = 60.0


def _load_lexicon():
    """从 entity_aliases 表读 alias→canon（构建词典层）。"""
    try:
        c = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
        rows = c.execute("SELECT canon, alias FROM entity_aliases").fetchall()
        c.close()
        lex = {}
        for canon, alias in rows:
            lex.setdefault(alias, canon)
        return lex
    except Exception:
        return {}


def canon_of(alias: str) -> str:
    """别名 → canon（未在表里则原样返回）。"""
    lex = _load_lexicon()
    return lex.get(alias, alias)


def extract_entities(text: str) -> list:
    """抽实体，返回 [canon, ...]（去重，保序）。

    双层（P4.9-f 选2）：
      1. alias 匹配（手种已知实体：agent/项目/工具）——确定性子串，长优先。
      2. jionlp check_person_name 校验 jieba 分词 token——只认标准姓+名（如"何伟"），
         认不出昵称/别名（玄铁/江北/老何），那是 alias 表/LLM 的事。
    边界：新昵称/动态实体（不在任何词典）→ 由语义层（bge+BM25）或后续 LLM 抽取覆盖。
    """
    if not text:
        return []
    canon_seen = []
    lex = _load_lexicon()
    for alias in sorted(lex, key=len, reverse=True):
        if len(alias) >= 2 and alias in text:
            can = lex[alias]
            if can not in canon_seen:
                canon_seen.append(can)
    # v1.12.1：CheckPersonName 只认标准姓+名（何伟），昵称/别名靠 alias 表；逐 token 推断太慢（回填超时元凶），
    # 故打标主路径只走 alias 确定性匹配（快、可解释）。标准人名可作为后续半自动 seed 候选。
    return canon_seen


def extract_time(text: str, base: float = None) -> float:
    """用 jionlp TimeExtractor 抽正文时间基准（返回 epoch 秒）；失败返回 None（零填不 raise）。

    实测 TimeExtractor 是 __call__ 可调用（无 .extract），返回 [{'type':'time_point', 'detail':{'time':['YYYY-MM-DD HH:MM:SS', ...]}}]。
    """
    global _NER_FAIL_AT
    if not text:
        return None
    if not (time.time() - _NER_FAIL_AT) < _NER_COOLDOWN:
        try:
            # jionlp 1.5.29 包级副作用（util/__init__.py:18 HelpSearch() → help_search.py:33 print 广告）：
            # 只要 import 必打一次广告，污染 CLI/桥接 stdout（实测 "bad xtmem output 但写入成功" 真因）。
            # Option A（2026-09-02 诊断确认 + 本轮回归触发）：import 期用 redirect_stdout 吞掉，零功能影响。
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                from jionlp.algorithm.ner.time_extractor import TimeExtractor
            te = TimeExtractor()
            # v1.12.1（2026-09-02）：time_base 必须传 ISO 串（YYYY-MM-DD HH:MM:SS），裸 float（缺年月日）会 0 命中/异常
            _tb = (datetime.fromtimestamp(base or time.time()).strftime("%Y-%m-%d %H:%M:%S"))
            r = te(text, time_base=_tb)
            if isinstance(r, list) and r:
                _t = r[0].get("detail", {}).get("time") if isinstance(r[0], dict) else None
                if isinstance(_t, list) and _t:
                    return datetime.strptime(str(_t[0])[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            _NER_FAIL_AT = time.time()
    return None


def upsert_entities(chunk_id: str, canon_list: list):
    """把 chunk 的 canon 实体写进 chunk_entities 关联表（唯一落点）；entities.count = 含该实体 active chunk 数。

    v1.12.1 A：幂等靠 chunk_entities 主键（INSERT OR IGNORE），回填任意次不翻倍；
    仅当新插入一行才把 entities.count +1（= 文档数，不是出现次数——P5.0 图 df 用它）。
    """
    if not canon_list or not chunk_id:
        return
    try:
        c = sqlite3.connect(DEFAULT_DB, timeout=30)
        now = time.time()
        for canon in canon_list:
            cur = c.execute("INSERT OR IGNORE INTO chunk_entities(chunk_id, canon) VALUES(?,?)", (chunk_id, canon))
            if cur.rowcount:
                c.execute("INSERT INTO entities(canon,count,first_seen,last_seen) VALUES(?,1,?,?) "
                          "ON CONFLICT(canon) DO UPDATE SET count=count+1, last_seen=excluded.last_seen",
                          (canon, now, now))
        c.commit()
        c.close()
    except Exception:
        pass


def tag_chunk(chunk_id: str, content: str) -> None:
    """store 后置钩子：抽实体+时间，写 chunk_entities + content_time（P4.9-波1 D 接线）。"""
    if not chunk_id or not content:
        return
    try:
        ents = extract_entities(content)
        upsert_entities(chunk_id, ents)
        ct = extract_time(content)
        if ct is not None:
            c = sqlite3.connect(DEFAULT_DB, timeout=30)
            c.execute("UPDATE chunks SET content_time=? WHERE id=?", (ct, chunk_id))
            c.commit()
            c.close()
    except Exception:
        pass
