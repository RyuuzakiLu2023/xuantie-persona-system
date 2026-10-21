# -*- coding: utf-8 -*-
"""xtmem_p2_regression.py — XTMEM P2 修复回归（2026-08-31，对应审计报告 P2-11/12/13 + 死代码）

覆盖：
  P2-11 content hash 统一：store(MD5) 与 import(MD5) 同内容去重命中，DB 仅 1 行
  P2-12 embedding cache key 统一：全文 sha256 共用函数；reembed 不再截前 2000 字
  P2-13 _ns_map 按需查询：provider 代码含 IN 子查询（源码断言）
  死代码归档：integration/auto_dream/core_memory 已移入 memory/_archived/，__init__ 零引用
数据保护：全部走临时目录/临时库。
用法：python tests/xtmem_p2_regression.py
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = os.environ.get("XTMEM_PLUGIN_DIR", _REPO / "src" / "hermes-plugin")
CLI = os.environ.get("XTMEM_CLI", _REPO / "xtmem"/ "xtmem.py")
PYTHON = sys.executable

sys.path.insert(0, PLUGIN_DIR)

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    if not cond:
        print("  FAIL:", name, detail)


def run_cli(db, *argv, env_extra=None):
    env = {**os.environ, "XTMEM_DB_PATH": str(db), "PYTHONUTF8": "1"}
    env.pop("XTMEM_ACTOR", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([PYTHON, CLI, *argv], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def main() -> int:
    from memory.utils import compute_content_hash, compute_embedding_cache_key
    with tempfile.TemporaryDirectory(prefix="xtmem-p2-") as td:
        td = Path(td)
        db = td / "v.db"

        # ══════════════ P2-11：content hash 统一 ══════════════
        text = "P2统一哈希验证内容：assistant-a的部署偏好是本地 Ollama bge-m3"
        r = run_cli(str(db), "store", "--agent", "dsh", "--namespace", "dsh", "--text", text)
        check("p211_store_ok", r.get("ok") is True and r.get("id"), r)
        # 引擎 MD5 与 CLI 一致性
        check("p211_engine_content_hash_md5",
              len(compute_content_hash(text)) == 32 and
              compute_content_hash(text) == compute_content_hash(" " + text + " "),
              compute_content_hash(text))
        # import 同内容 → 应判定重复
        name = "demo"
        h16 = hashlib.sha256(name.lower().encode()).hexdigest()[:16]
        memdir = td / "zcode" / ("%s-%s" % (name, h16)) / "memory"
        memdir.mkdir(parents=True)
        (memdir / "mem-zc-hash.md").write_text(
            "---\nid: mem-zc-hash\nnamespace: dsh\nproject: demo\n"
            "importance: 0.5\ntimestamp: 1787100000\ntags: hash\ntrack: default\n---\n\n" + text + "\n",
            encoding="utf-8")
        r = run_cli(str(db), "import", "--as", "zcode", env_extra={"XTMEM_ZCODE_MEMORIES": str(td / "zcode")})
        check("p211_import_dedup_hits", r.get("ok") is True and r.get("duplicates", 0) >= 1,
              str(r))
        con = sqlite3.connect(str(db))
        n_rows = con.execute("SELECT count(*) FROM chunks WHERE content=?", (text,)).fetchone()[0]
        con.close()
        check("p211_only_one_row", n_rows == 1, "rows=%d" % n_rows)

        # ══════════════ P2-12：embedding cache key 统一 ══════════════
        long_text = "长文本缓存键验证" + ("基" * 2500)
        check("p212_key_is_fulltext_sha256",
              compute_embedding_cache_key(long_text) ==
              hashlib.sha256(long_text.encode()).hexdigest())
        from memory.vector_store import VectorStore
        vs = VectorStore(db_path=str(td / "cache.db"))
        vec = [0.1] * 8
        vs.cache_embedding(long_text, vec, model="test")
        con = sqlite3.connect(str(td / "cache.db"))
        hit = con.execute("SELECT text, text_hash FROM embedding_cache WHERE text_hash=?",
                          (compute_embedding_cache_key(long_text),)).fetchone()
        con.close()
        check("p212_cache_written_with_unified_key", hit is not None and hit[0] == long_text)
        vs.close()
        # reembed 不再截前 2000 字（源码断言，防回归）
        cli_src = Path(CLI).read_text(encoding="utf-8")
        check("p212_reembed_no_truncated_key",
              'content or "")[:2000]' not in cli_src and "compute_embedding_cache_key as _cek" in cli_src)

        # ══════════════ P2-13：_ns_map 按需查询 ══════════════
        prov_src = (Path(PLUGIN_DIR) / "provider.py").read_text(encoding="utf-8")
        check("p213_ns_map_targeted_query",
              "id IN ({ph})" in prov_src and "self._ns_map(ids=[doc.id for doc, _ in results])" in prov_src)  # R1.3 改 WHERE id IN (无 state 过滤，回查真值)

        # ══════════════ 死代码归档 ══════════════
        archived = Path(PLUGIN_DIR) / "memory" / "_archived"
        check("p214_archived_modules",
              (archived / "integration.py").exists() and (archived / "auto_dream.py").exists()
              and (archived / "core_memory.py").exists())
        init_src = (Path(PLUGIN_DIR) / "memory" / "__init__.py").read_text(encoding="utf-8")
        check("p214_init_no_ref", "integration" not in init_src and "core_memory" not in init_src)

        all_ok = all(v is True for v in CHECKS.values())
        print(json.dumps({"ok": all_ok, "checks": CHECKS}, ensure_ascii=False))
        return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
