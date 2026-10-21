# -*- coding: utf-8 -*-
"""memory/graph.py — P5.0 波2 图传播：canon 共现图 + 传输场幂迭代（2026-09-02，小D）。

定位（总 Spec v1.12.1 附录F P5.0 / 三方案联合可行性 20260902 判断1）：
  节点 = entities.canon（NER 实体归一后），边 = 同一 chunk 内 canon 共现（chunk_entities 推导），
  边权 w = 共现次数 × min(idf_a, idf_b)，idf = ln(N / (1 + df))（抗 hub——未加权时边权
  Top6 全是 Hermes~hermes / DSH~记忆 这类泛节点噪声组合，联合报告实测）。
  数据源已从 VCP 原设计的 tags 换成 canon（tags 建图判死：conversation 单点覆盖 66.5%）。

⚠️ P5.0-b 复现提示（实现定稿语义，后人复现必须按此对齐，改公式=改契约）：
  - 图为无向图（tag_edges 只存 a<b 一行）；查询期构有向过渡矩阵（不是按邻边数除——见下）：
        P[a→b] = 0.9 · w_ab / Σ_{b'∈N(a)} w_ab'
    邻居求和语义 = “分母是节点 a 的邻边权之和（除度数！非平方/非对数），行归一后整体乘 0.9”，
    0.9 乘子保证严格次随机（每行出流预算 < 1，VCP 不变量3，resolvent 收敛）。
  - 源场（seed）S：query 的 canon 实体，均匀归一 S[a] = 1/|Q|；query 无实体 → 空源场，
    B_G 全 0（传播分无增益，行为退化为不加传播——诚实降级，不造假分）。
  - 幂迭代（传输场，默认 30 次；v1 只实现 u_T，局部场 u_L=0.85S+0.15T(u) 与 Ω 门控留后续）：
        u^{(0)} = S
        u^{(t+1)} = 0.45 · S + 0.55 · Pᵀ · u^{(t)}
    0.45/0.55 取自 VCP u_T 场（0.45S+0.55T(u)），镜像源：XTMEM-vcp移植方案 §3 P4.9-1。
  - 候选读取：B_G(doc) = max_{c ∈ canons(doc)} u[c]（聚合=max；sum 聚合给多实体 doc 过度加分，
    联合报告“不同分值数”口径下 max 并列更少、判别力更好——口径写死在这里）。
  - 融合：S_new = S0 + graph_prop_weight × B_G(doc)（非负结构增量相加，不进 clip；
    graph_prop_weight = 0 → S_new = S0，行为与现状逐字节一致——P4.8 影子期灰度先例）。

规模：当前活库 ~14 canon（df>0）/ 数千边，numpy bincount 幂迭代 30 次毫秒级（联合报告实测）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

# R5（P6 波1）：连接收编 open_db（schema 版本闸+降级自兜底）
from memory.open_db import open_db

logger = logging.getLogger(__name__)

DEFAULT_DB = os.environ.get("XTMEM_DB_PATH", __import__("memory.public_runtime", fromlist=["db_default"]).db_default())

# 幂迭代常数（见模块 docstring 公式契约）
_RESET = 0.45      # u_T 场的源场重置系数
_DAMP = 0.55       # u_T 场的传播系数
_SUBSTOCH = 0.9    # 行归一后整体乘子（严格次随机 sum<1）
_ITERS = 30


def _ensure_tag_edges(conn: sqlite3.Connection) -> None:
    """镜像 schema.py 的表定义——建表/重建入口自带兜底（CLI rebuild 直连时也成立）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tag_edges (
            a TEXT NOT NULL, b TEXT NOT NULL,
            cnt INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (a, b)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tag_edges_b ON tag_edges(b)")


def _pairs(canons: Sequence[str]) -> List[Tuple[str, str]]:
    """canon 列表 → 无向对（去重、a<b 排序）。"""
    cs = sorted({c for c in canons if c})
    return [(cs[i], cs[j]) for i in range(len(cs)) for j in range(i + 1, len(cs))]


def rebuild_edges(db_path: Optional[str] = None) -> dict:
    """P5.0-a：离线全量重建 tag_edges（幂等：DELETE + 从 chunk_entities 重算唯一真值）。

    触发键 = chunks.id 语义下的 chunk_entities（不依赖 content_hash——联合报告判断4，
    hash 收口与图增量解耦）。返回统计信息（含差分口径基线：nodes/edges/tagged_chunks）。
    """
    p = db_path or DEFAULT_DB
    conn = open_db(p, timeout=30)
    try:
        _ensure_tag_edges(conn)
        conn.execute("DELETE FROM tag_edges")
        now = time.time()
        # D3-1（2026-09-10 派单工单）：噪声切片不参与建图——机器生成的脏切片实体标签密集
        # （两域实验实测密度是全池 3 倍），delta_sum 按实体数排序时天然霸榜，把真记忆挤掉。
        # ⚠️ 判据单一源：统一走 memory.noise_filter（召回期 hybrid_retriever 调用同一函数），
        #    禁在本文件另写正则——口径漂移会让两边判的不是同一批切片。
        # ⚠️ 不删数据：只在【建图 SELECT 里排除】，chunks / chunk_entities 一行不动（可逆）。
        # enabled=false（默认）时 noise_chunk_ids 返回空表 → SQL 与改前逐字节等价。
        _noise_ids = []
        try:
            from memory.noise_filter import noise_chunk_ids
            _noise_ids = noise_chunk_ids(conn)
        except Exception:
            logger.debug("noise filter skip in rebuild_edges (non-fatal)", exc_info=True)
        if _noise_ids:
            _marks = ",".join("?" * len(_noise_ids))
            conn.execute(
                """INSERT INTO tag_edges(a, b, cnt, updated_at)
                   SELECT ce1.canon, ce2.canon, COUNT(DISTINCT ce1.chunk_id), ?
                   FROM chunk_entities ce1
                   JOIN chunk_entities ce2
                     ON ce1.chunk_id = ce2.chunk_id AND ce1.canon < ce2.canon
                   JOIN chunks ch ON ch.id = ce1.chunk_id
                   WHERE COALESCE(ch.derived_from, '') = ''
                     AND ce1.chunk_id NOT IN (""" + _marks + """)
                   GROUP BY ce1.canon, ce2.canon""",
                tuple([now] + _noise_ids),
            )
            logger.info("graph rebuild: noise chunks excluded = %d", len(_noise_ids))
        else:
            conn.execute(
                """INSERT INTO tag_edges(a, b, cnt, updated_at)
                   SELECT ce1.canon, ce2.canon, COUNT(DISTINCT ce1.chunk_id), ?
                   FROM chunk_entities ce1
                   JOIN chunk_entities ce2
                     ON ce1.chunk_id = ce2.chunk_id AND ce1.canon < ce2.canon
                   JOIN chunks ch ON ch.id = ce1.chunk_id
                   WHERE COALESCE(ch.derived_from, '') = ''
                   GROUP BY ce1.canon, ce2.canon""",
                (now,),
            )
        conn.commit()
        edges = conn.execute("SELECT COUNT(*) FROM tag_edges").fetchone()[0]
        nodes = conn.execute("SELECT COUNT(DISTINCT canon) FROM chunk_entities").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(DISTINCT chunk_id) FROM chunk_entities").fetchone()[0]
        return {"ok": True, "nodes": nodes, "edges": edges, "tagged_chunks": chunks}
    finally:
        conn.close()


def increment_for_chunk(chunk_id: str, db_path: Optional[str] = None) -> int:
    """P5.0-a：store 时增量维护——按 chunk_entities 现况重算该 chunk 涉及边的全局共现数。

    幂等：每次调用按“该 chunk 的 canon 对 → 全库共现 chunk 数”重算写回，重复调用不翻倍
    （与 rebuild 同一真值来源；store 钩子一次调用是常态，重复也安全）。
    失败零填不 raise（降级链学 embedding_provider / ner_tagger）。
    """
    p = db_path or DEFAULT_DB
    conn = open_db(p, timeout=30)
    try:
        _ensure_tag_edges(conn)
        # D3-1（2026-09-10）：噪声切片增量建图直接跳过（return 0 不建边）。
        # 与 rebuild_edges 共用 memory.noise_filter 同一判据（铁律：单一判据源）。
        # 幂等且便宜：判噪声的成本 = 一次 SELECT content/namespace + 域阈值表（缓存命中时极低）。
        # enabled=false（默认）时 is_noise_chunk 首行短路返回 False → 行为与现状一致。
        # ⚠️ 开闸分法（2026-09-10）：命中还不够，【还要按其规则代号过 build_exclude】——
        #   规则粒度开关必须与 rebuild_edges（走 noise_chunk_ids，内部已过同一个 build_exclude）
        #   口径一致；否则出现「全量重建排除了 R3，增量却把 R3 建进去」的分裂，
        #   表现为重建后有边、存一条新记忆后又冒出来（同 S5 双写口径漂移的坑）。
        try:
            from memory.noise_filter import is_noise_chunk, is_build_excluded
            _row = conn.execute(
                "SELECT content, COALESCE(namespace,'') FROM chunks WHERE id=?", (chunk_id,)
            ).fetchone()
            if _row is not None:
                _hit, _why = is_noise_chunk(_row[0], namespace=str(_row[1] or ""),
                                            chunk_id=chunk_id, conn=conn)
                if _hit and is_build_excluded(_why):
                    logger.debug("graph increment skipped for noise chunk %s (%s)", chunk_id, _why)
                    return 0
        except Exception:
            logger.debug("noise filter skip in increment_for_chunk (non-fatal)", exc_info=True)
        canons = [r[0] for r in conn.execute(
            "SELECT canon FROM chunk_entities WHERE chunk_id=?", (chunk_id,)
        ).fetchall()]
        now = time.time()
        for a, b in _pairs(canons):
            n = conn.execute(
                """SELECT COUNT(DISTINCT ce1.chunk_id)
                   FROM chunk_entities ce1
                   JOIN chunk_entities ce2 ON ce1.chunk_id = ce2.chunk_id
                   JOIN chunks ch ON ch.id = ce1.chunk_id
                   WHERE ce1.canon=? AND ce2.canon=? AND COALESCE(ch.derived_from, '') = ''""",
                (a, b),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO tag_edges(a, b, cnt, updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(a, b) DO UPDATE SET cnt=excluded.cnt, updated_at=excluded.updated_at""",
                (a, b, n, now),
            )
        conn.commit()
        return len(_pairs(canons))
    except Exception:
        logger.debug("graph increment for %s failed (non-fatal)", chunk_id, exc_info=True)
        return 0
    finally:
        conn.close()


def _runtime_faces() -> dict:
    """V1.6 三数据面（全只读、全自兜底——任何一面失败输出 {err} 不拖垮 stats）：
    jobs = 各 profile 调度面 jobs.json 的任务计数；schema = user_version+表数；
    connect = runtime_actors 上报端（V1.3 面）按 code_sha 分组。"""
    faces = {}
    db_env = os.environ.get("XTMEM_DB_PATH", DEFAULT_DB)
    try:
        import glob as _glob
        job_total = 0
        job_xtmem = 0
        for jp in _glob.glob(__import__("memory.public_runtime", fromlist=["home_file"]).home_file('profiles/*/cron/jobs.json')):
            with open(jp, encoding="utf-8") as f:
                data = json.load(f)
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            job_total += len(jobs)
            job_xtmem += sum(1 for x in jobs if str(x.get("name", "")).startswith("xtmem"))
        faces["jobs"] = {"total": job_total, "xtmem": job_xtmem}
    except Exception as e:
        faces["jobs"] = {"err": str(e)[:80]}
    try:
        conn2 = open_db(db_env, ro=True)
        try:
            uv = conn2.execute("PRAGMA user_version").fetchone()[0]
            tabs = conn2.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            faces["schema"] = {"user_version": uv, "tables": tabs}
        finally:
            conn2.close()
    except Exception as e:
        faces["schema"] = {"err": str(e)[:80]}
    try:
        conn3 = open_db(db_env, ro=True)
        try:
            row = conn3.execute("SELECT value FROM xtmem_meta WHERE key='runtime_actors'").fetchone()
            actors = json.loads(row[0]) if row else {}
            by_sha = {}
            for v in actors.values():
                if isinstance(v, dict):
                    key = str(v.get("code_sha", "?"))[:8]
                    by_sha[key] = by_sha.get(key, 0) + 1
            faces["connect"] = {"actors": len(actors), "by_code_sha": by_sha}
        finally:
            conn3.close()
    except Exception as e:
        faces["connect"] = {"err": str(e)[:80]}
    return faces


def graph_stats(db_path: Optional[str] = None) -> dict:
    """只读统计：节点/边/带实体 chunk 数 + 边权 Top5（用于建图后人工核验，防 hub）。"""
    p = db_path or DEFAULT_DB
    conn = open_db(p, ro=True)
    try:
        nodes = conn.execute("SELECT COUNT(DISTINCT canon) FROM chunk_entities").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM tag_edges").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(DISTINCT chunk_id) FROM chunk_entities").fetchone()[0]
        df = dict(conn.execute("SELECT canon, COUNT(*) FROM chunk_entities GROUP BY canon").fetchall())
        N = chunks or 1
        top = []
        for a, b, cnt in conn.execute(
            "SELECT a, b, cnt FROM tag_edges ORDER BY cnt DESC LIMIT 5"
        ).fetchall():
            wa = cnt * math.log(N / (1 + df.get(a, 0)))
            wb = cnt * math.log(N / (1 + df.get(b, 0)))
            top.append({"edge": f"{a}~{b}", "cnt": cnt, "w": round(min(wa, wb), 3)})
        out = {"ok": True, "nodes": nodes, "edges": edges, "tagged_chunks": chunks, "top_edges": top}
        # V1.6 图从码生（v1.3.2 工单批，2026-09-05）：stats 补三数据面（jobs/schema/connect）——
        # 「变更必变图」：jobs 增删/schema 升版/端上下线都会在下次 stats 反映（实时读真值面，无缓存）。
        out.update(_runtime_faces())
        return out
    finally:
        conn.close()


def load_graph(db_path: Optional[str] = None) -> Tuple[List[Tuple[str, str, int]], Dict[str, int], int]:
    """读图：返回 (edges=[(a,b,cnt)], df_map={canon:chunk数}, N=带实体 chunk 数)。"""
    p = db_path or DEFAULT_DB
    conn = open_db(p, ro=True)
    try:
        edges = [tuple(r) for r in conn.execute("SELECT a, b, cnt FROM tag_edges").fetchall()]
        df = dict(conn.execute(
            "SELECT canon, COUNT(*) FROM chunk_entities GROUP BY canon"
        ).fetchall())
        n = conn.execute("SELECT COUNT(DISTINCT chunk_id) FROM chunk_entities").fetchone()[0]
        return edges, df, n
    finally:
        conn.close()


def _query_seed(query: str, lexicon: Optional[Dict[str, str]] = None,
                db_path: Optional[str] = None) -> Dict[str, float]:
    """query → 源场（canon → 均匀概率）。lexicon 显式传入（测试用）时走子串匹配，
    否则复用 ner_tagger.extract_entities（生产唯一抽取器）；无实体 → 空 dict。"""
    if not query:
        return {}
    if lexicon is not None:
        ents = []
        for alias in sorted(lexicon, key=len, reverse=True):
            if len(alias) >= 2 and alias in query and lexicon[alias] not in ents:
                ents.append(lexicon[alias])
    else:
        try:
            from memory.ner_tagger import extract_entities
            ents = extract_entities(query)
        except Exception:
            ents = []
    if not ents:
        return {}
    w = 1.0 / len(ents)
    return {e: w for e in ents}


def propagate(seed: Dict[str, float], edges: List[Tuple[str, str, int]],
              df: Dict[str, int], N: int,
              iters: int = _ITERS) -> Dict[str, float]:
    """P5.0-b：传输场幂迭代（公式契约见模块 docstring，改公式=改契约）。

    返回 canon → u[c]（含只做 seed 不入图的孤立节点：其 u 恒 0.45·S，属诚实回显）。
    """
    if not seed:
        return {}
    try:
        import numpy as np
    except Exception:
        logger.warning("numpy missing; graph propagation disabled (non-fatal)")
        return {}
    # 节点全集 = seed ∪ 边端点
    node_names = sorted(set(seed.keys()) | {e[0] for e in edges} | {e[1] for e in edges})
    idx = {n: i for i, n in enumerate(node_names)}
    n = len(node_names)
    neighbors: List[List[Tuple[int, float]]] = [[] for _ in range(n)]
    for a, b, cnt in edges:
        ia, ib = idx.get(a), idx.get(b)
        if ia is None or ib is None or cnt <= 0:
            continue
        idf_a = math.log(N / (1 + df.get(a, 0))) if N else 0.0
        idf_b = math.log(N / (1 + df.get(b, 0))) if N else 0.0
        w = cnt * min(idf_a, idf_b)
        if w <= 0:
            continue
        neighbors[ia].append((ib, w))
        neighbors[ib].append((ia, w))
    rows: List[int] = []
    cols: List[int] = []
    vals: List[float] = []
    for i, nb in enumerate(neighbors):
        tot = sum(w for _, w in nb)
        if tot <= 0:
            continue
        for j, w in nb:
            rows.append(i)
            cols.append(j)
            vals.append(_SUBSTOCH * w / tot)
    S = np.zeros(n, dtype=np.float64)
    for name, s in seed.items():
        if name in idx:
            S[idx[name]] += s
    if S.sum() > 0:
        S = S / S.sum()
    u = S.copy()
    if rows:
        r = np.asarray(rows, dtype=np.int64)
        c = np.asarray(cols, dtype=np.int64)
        v = np.asarray(vals, dtype=np.float64)
        for _ in range(max(1, iters)):
            contrib = np.bincount(c, weights=v * u[r], minlength=n)
            u = _RESET * S + _DAMP * contrib
    return {name: float(u[i]) for name, i in idx.items()}


def doc_boosts(query: str, doc_ids: Sequence[str], db_path: Optional[str] = None,
               lexicon: Optional[Dict[str, str]] = None,
               iters: int = _ITERS) -> Dict[str, float]:
    """P5.0-c 数据面：query + 候选 doc_id 集 → {doc_id: B_G}（非负传播分，无增益为 0）。

    - seed 为空 / 图空 → 返回 {}（调用方按 0 处理，行为退化为现状）。
    - B_G(doc) = max_{c∈canons(doc)} u[c]；doc 无实体或实体不在图 → 0。
    """
    if not query or not doc_ids:
        return {}
    edges, df, N = load_graph(db_path)
    if not edges or N <= 0:
        return {}
    seed = _query_seed(query, lexicon=lexicon, db_path=db_path)
    if not seed:
        return {}
    scores = propagate(seed, edges, df, N, iters=iters)
    if not scores:
        return {}
    conn = open_db(db_path or DEFAULT_DB, ro=True)
    try:
        marks = ",".join("?" * len(doc_ids))
        rows = conn.execute(
            f"SELECT chunk_id, canon FROM chunk_entities WHERE chunk_id IN ({marks})",
            list(doc_ids),
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, float] = {}
    per_doc: Dict[str, List[str]] = {}
    for cid, canon in rows:
        per_doc.setdefault(cid, []).append(canon)
    for cid in doc_ids:
        cs = per_doc.get(cid) or []
        if not cs:
            out[cid] = 0.0
        else:
            out[cid] = max((scores.get(c, 0.0) for c in cs), default=0.0)
    return out
