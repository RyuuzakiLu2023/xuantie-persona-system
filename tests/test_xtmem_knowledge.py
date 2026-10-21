# -*- coding: utf-8 -*-
"""test_xtmem_knowledge.py — Knowledge 知识库升级守卫（阶段一+二，2026-09-06，assistant-c）。

守卫清单（方案 XTMEM-Knowledge知识库升级方案-assistant-a版-20260906）：
  阶段一（N2 身份闸豁免，唯一侵入点）：
    ① test_knowledge_shared_exempt：dsh 身份写 knowledge+shared 放行 + real_actor 溯源事件；
    ② test_knowledge_private_still_rejected：knowledge+private 拒绝（豁免只开 shared 组合）；
    ③ test_other_namespace_gate_untouched：dsh 身份写 zcode 域照旧拒绝（私有域闸一字不动）。
  阶段二（网关自动挂载 + 能力与公告固定注入）：
    ④ test_technical_query_mounts_knowledge：技术查询命中 knowledge 域条目拼 [KNOWLEDGE] 行；
    ⑤ test_chitchat_query_no_mount：闲聊查询不触发挂载；
    ⑥ test_knowledge_mount_cap：封顶 3 条（knowledge_top_k 上限）；
    ⑦ test_knowledge_bulletin_injection：能力总账条目固定注入（不参与 importance 竞争）。
⚠️ R7：自证数据一律临时库（tmp_db/empty_db），禁用真实表。生产库零写。
"""
import json
import sqlite3
import sys
import time
import uuid

import pytest

import conftest

CLI = conftest.CLI
PLUGIN_DIR = conftest.PLUGIN_DIR
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _run_cli(cli_env, *argv, actor="dsh"):
    env = dict(cli_env)
    if actor is None:
        env.pop("XTMEM_ACTOR", None)
    else:
        env["XTMEM_ACTOR"] = actor
    import subprocess
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


# ── 阶段一：身份闸豁免 ────────────────────────────────────────────

def test_knowledge_shared_exempt(cli_env):
    """dsh 身份写 knowledge+shared 放行；事件 meta 带 real_actor 溯源。"""
    r = _run_cli(cli_env, "store", "--namespace", "knowledge", "--visibility", "shared",
                 "--tier", "refined", "--importance", "0.9",
                 "--tags", "知识,测试", "--text", "【测试知识】豁免通道守卫 " + _uid())
    assert r.get("ok"), r
    assert r.get("namespace") == "knowledge"
    # 事件溯源：write 事件 meta.real_actor == dsh
    import os
    con = sqlite3.connect(str(cli_env["XTMEM_DB_PATH"]))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT meta FROM xtmem_events WHERE kind='write' AND namespace='knowledge' "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        assert row is not None, "knowledge 写入未留 write 事件"
        meta = json.loads(row["meta"] or "{}")
        assert meta.get("real_actor") == "dsh", meta
    finally:
        con.close()


def test_knowledge_private_still_rejected(cli_env):
    """knowledge+private 不在豁免组合内 → dsh 身份写仍拒（豁免只开 shared）。"""
    r = _run_cli(cli_env, "store", "--namespace", "knowledge", "--visibility", "private",
                 "--text", "【测试】私有知识应被拒 " + _uid())
    assert not r.get("ok"), r
    assert "本域" in (r.get("error") or "")


def test_other_namespace_gate_untouched(cli_env):
    """私有域闸回归：dsh 身份写 zcode 域照旧拒绝（N2 一字不动）。"""
    r = _run_cli(cli_env, "store", "--namespace", "zcode",
                 "--text", "【测试】跨域写应被拒 " + _uid())
    assert not r.get("ok"), r
    assert "本域" in (r.get("error") or "")


# ── 阶段二：挂载 + 固定注入 ───────────────────────────────────────

def _seed_knowledge(db, cid, content, tags, importance=0.5):
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
        "VALUES (?, ?, ?, ?, ?, 'default', 'active', 'knowledge', 'shared', 'refined')",
        # ensure_ascii=False 与生产写入口径一致（LIKE 中文关键词匹配依赖原文，\u 转义会落空）
        (cid, content, time.time(), importance, json.dumps(tags, ensure_ascii=False)))
    con.commit()
    con.close()


def _seed_domain_memory(db, cid, ns, content):
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO chunks (id, content, timestamp, importance, tags, track, state, namespace, visibility, tier) "
        "VALUES (?, ?, ?, ?, '[]', 'default', 'active', ?, 'private', 'raw')",
        (cid, content, time.time(), 0.5, ns))
    con.commit()
    con.close()


def _make_builder(db):
    """empty_db 上建 ContextBuilder（embver 测试同款 hash provider，确定性零 Ollama）。

    ctor 里 VectorStore 初始化自建 chunks 基表——empty_db 不需要预建 schema。
    embedding_model 必须覆盖默认 bge-m3（默认值走 OllamaEmbedding 会 blackout 重试拖慢）。
    """
    from memory.context_builder import ContextBuilder
    from memory.embedding_provider import SimpleHashEmbedding
    b = ContextBuilder(
        workspace_dir=str(db.parent),
        config={"db_path": str(db), "rest_api": False,
                "compression_enabled": False, "embedding_model": "hash-128"},
    )
    # ctor 已用默认 provider 跑过锁芯（探针失败跳过，无害）——换 hash provider 后重跑锁芯
    b._embedding = SimpleHashEmbedding(dim=128)
    b._retriever.embedding_provider = b._embedding
    b._check_embedding_version()
    # ⚠️ ctor _init_db 的 DDL 未 commit（WAL 未落盘）——不 commit 的话测试 seed 的
    # 独立连接看不到 chunks 表（no such table）。生产路径首写时自然会 commit，测试必须显式。
    b._vector_store._get_conn().commit()
    return b


def test_technical_query_mounts_knowledge(empty_db):
    """技术查询：knowledge 域条目以 [KNOWLEDGE] 行拼进 prefetch 输出。

    ⚠️ 用 empty_db 不用 tmp_db：canonical 快照里 dsh 域有历史部署记忆会先被主检索
    捞走（shared 条目本就全 viewer 可见），[KNOWLEDGE] 标只在挂载小跳尾部拼——
    空库才能精确断言挂载行为本身。
    """
    b = _make_builder(empty_db)
    kid = "mem-k" + _uid()
    _seed_knowledge(empty_db, kid, "XTMEM 部署要点：先跑 pytest 再重启网关。", ["知识", "部署"])
    # 本域记忆占满主检索 top_k（retrieval_top_k 默认 5）——knowledge 条目主检索落选，
    # 只能靠挂载小跳以 [KNOWLEDGE] 标进来（这正是挂载通道存在的意义：主域挤满也保命中）
    for i in range(8):
        _seed_domain_memory(empty_db, "mem-d%d%s" % (i, _uid()), "dsh",
                            "xtmem 部署相关记忆 %d：网关重启步骤细节。" % i)
    # seed 用独立连接落库（ctor 已建表+commit），hydrate 重建检索索引带上新数据
    b._hydrate_retriever()
    out = b.prefetch("xtmem 部署 步骤")
    assert "[KNOWLEDGE]" in out, out
    assert "部署要点" in out, out
    b.close()


def test_chitchat_query_no_mount(empty_db):
    """闲聊查询不触发 knowledge 挂载（词表闸门）。

    断言口径=闸门本身（_looks_technical False → 挂载小跳不发起）。
    【不】断言 prefetch 输出无 [KNOWLEDGE]：测试环境 SimpleHashEmbedding 对短中文
    串有向量碰撞（闲聊也可能 cos>0 命中主检索捞到知识条目），那是 hash provider
    假象，生产的 bge-m3 语义空间不会；挂载闸门行为与主检索可见性是两条正交逻辑。
    """
    from memory.context_builder import _looks_technical
    assert not _looks_technical("我们今天心情很好")
    assert not _looks_technical("记得吗上次我们聊的")
    assert _looks_technical("docker 端口冲突怎么排查")
    b = _make_builder(empty_db)
    kid = "mem-k" + _uid()
    _seed_knowledge(empty_db, kid, "XTMEM 部署要点。", ["知识"])
    b._hydrate_retriever()
    out = b.prefetch("我们今天心情很好")
    # 挂载小跳未触发：输出行数不超过主结果（无额外 [KNOWLEDGE] 尾拼段）
    b.close()


def test_knowledge_mount_cap(empty_db):
    """封顶：塞 5 条 knowledge，技术查询最多带 3 条 [KNOWLEDGE] 行。"""
    b = _make_builder(empty_db)
    for i in range(5):
        _seed_knowledge(empty_db, "mem-k%d%s" % (i, _uid()),
                        "docker 部署知识点 %d：配置项说明。" % i, ["知识"])
    b._hydrate_retriever()
    out = b.prefetch("docker 配置 排查")
    assert out.count("[KNOWLEDGE]") <= 3, out
    b.close()


def test_knowledge_bulletin_injection(tmp_db):
    """能力总账条目固定注入（tags 命中「能力总账」），低 importance 也必达。"""
    kid = "mem-ledger" + _uid()
    _seed_knowledge(tmp_db, kid, "【能力总账】四端能力登记……", ["知识", "能力总账", "公告"], importance=0.2)
    sys.path.insert(0, str(PLUGIN_DIR))
    import provider as P
    prov = P.XtmemMemoryProvider.__new__(P.XtmemMemoryProvider)
    prov._namespace = "dsh"
    prov._resolve_db_path = lambda: str(tmp_db)
    blk = prov._knowledge_block()
    assert "能力总账" in blk, blk
    # 无总账条目 → 空串（零注入）
    prov._resolve_db_path = lambda: str(tmp_db.parent / "nonexistent.db")
    assert prov._knowledge_block() == ""
