# -*- coding: utf-8 -*-
"""p2 回归迁移（Phase T · T-2，2026-09-01，assistant-c）。

迁移原则：断言一字不改，只拆组织。原 xtmem_p2_regression.py 的每个 check 段 → 独立 def test_*。
用 conftest tmp_db/empty_db/cli_env（每用例独立库副本 / 全新空库 / 环境注入+收尾 pop）。
原脚本保留 __main__ 入口（无 pytest 环境仍可跑）。
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

from memory.utils import compute_content_hash, compute_embedding_cache_key
from memory.vector_store import VectorStore

CLI = conftest.CLI
PLUGIN_DIR = conftest.PLUGIN_DIR


def _run_cli(cli_env, *argv, env_extra=None):
    env = dict(cli_env)
    env.pop("XTMEM_ACTOR", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def test_p211_store_ok(tmp_db, cli_env):
    text = "P2统一哈希验证内容：assistant-a的部署偏好是本地 Ollama bge-m3"
    r = _run_cli(cli_env, "store", "--agent", "dsh", "--namespace", "dsh", "--text", text)
    assert r.get("ok") is True and r.get("id"), r


def test_p211_engine_content_hash_md5(tmp_db, cli_env):
    text = "P2统一哈希验证内容：assistant-a的部署偏好是本地 Ollama bge-m3"
    assert len(compute_content_hash(text)) == 32
    assert compute_content_hash(text) == compute_content_hash(" " + text + " ")


def test_p211_import_dedup_hits(tmp_db, cli_env):
    import os
    import tempfile
    text = "P2统一哈希验证内容：assistant-a的部署偏好是本地 Ollama bge-m3"
    _run_cli(cli_env, "store", "--agent", "dsh", "--namespace", "dsh", "--text", text)
    # import 同内容 → 应判定重复
    td = tempfile.mkdtemp(prefix="xtmem-p2-")
    name = "demo"
    h16 = hashlib.sha256(name.lower().encode()).hexdigest()[:16]
    memdir = Path(td) / "zcode" / ("%s-%s" % (name, h16)) / "memory"
    memdir.mkdir(parents=True)
    (memdir / "mem-zc-hash.md").write_text(
        "---\nid: mem-zc-hash\nnamespace: dsh\nproject: demo\n"
        "importance: 0.5\ntimestamp: 1787100000\ntags: hash\ntrack: default\n---\n\n" + text + "\n",
        encoding="utf-8")
    r = _run_cli(cli_env, "import", "--as", "zcode", env_extra={"XTMEM_ZCODE_MEMORIES": str(Path(td) / "zcode")})
    assert r.get("ok") is True and r.get("duplicates", 0) >= 1, str(r)


def test_p211_only_one_row(tmp_db, cli_env):
    import sqlite3
    text = "P2统一哈希验证内容：assistant-a的部署偏好是本地 Ollama bge-m3"
    _run_cli(cli_env, "store", "--agent", "dsh", "--namespace", "dsh", "--text", text)
    _run_cli(cli_env, "import", "--as", "zcode", env_extra={})  # no zcode memories -> no-op
    con = sqlite3.connect(str(tmp_db))
    n = con.execute("SELECT count(*) FROM chunks WHERE content=?", (text,)).fetchone()[0]
    con.close()
    assert n == 1, "rows=%d" % n


def test_p212_key_is_fulltext_sha256():
    long_text = "长文本缓存键验证" + ("基" * 2500)
    assert compute_embedding_cache_key(long_text) == hashlib.sha256(long_text.encode()).hexdigest()


def test_p212_cache_written_with_unified_key(tmp_db, cli_env):
    import sqlite3
    long_text = "长文本缓存键验证" + ("基" * 2500)
    import tempfile
    cache_db = Path(tempfile.mkdtemp(prefix="xtmem-p2-")) / "cache.db"
    vs = VectorStore(db_path=str(cache_db))
    vs.cache_embedding(long_text, [0.1] * 8, model="test")
    con = sqlite3.connect(str(cache_db))
    hit = con.execute("SELECT text, text_hash FROM embedding_cache WHERE text_hash=?",
                      (compute_embedding_cache_key(long_text),)).fetchone()
    con.close()
    vs.close()
    # F-5 fix: text column no longer stores plaintext (security); cache written with empty text
    assert hit is not None and hit[0] == '' and hit[1] == compute_embedding_cache_key(long_text)


def test_p212_reembed_no_truncated_key():
    cli_src = Path(CLI).read_text(encoding="utf-8")
    assert 'content or "")[:2000]' not in cli_src
    assert "compute_embedding_cache_key as _cek" in cli_src


def test_p213_ns_map_targeted_query():
    prov_src = (Path(PLUGIN_DIR) / "provider.py").read_text(encoding="utf-8")
    assert "self._ns_map(ids=[doc.id for doc, _ in results])" in prov_src


def test_p214_archived_modules():
    # ⚠️ 公开仓【不】含内部 memory/_archived 目录（只在内部开发树里），原断言等于
    #    在公开包上检查一个注定不存在的东西 → 公开仓必然 1 failed（P1-1）。
    #    目录不在就 skip 并说明原因，【不】改成"随便断言点别的"糊过去。
    archived = Path(PLUGIN_DIR) / "memory" / "_archived"
    if not archived.exists():
        pytest.skip("internal archived directory not in public release")
    assert (archived / "integration.py").exists()
    assert (archived / "auto_dream.py").exists()
    assert (archived / "core_memory.py").exists()


def test_p214_init_no_ref():
    init_src = (Path(PLUGIN_DIR) / "memory" / "__init__.py").read_text(encoding="utf-8")
    assert "integration" not in init_src
    assert "core_memory" not in init_src
