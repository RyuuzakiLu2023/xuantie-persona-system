# -*- coding: utf-8 -*-
"""XTMEM 测试基建 · conftest.py

给 pytest 提供三类 fixture，让测试能在独立隔离的临时库上运行。
自动适配公开仓库路径，支持在干净开发环境中直接运行 pytest。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# 优先探测当前仓库内置的 hermes-plugin 目录，使公开仓库测试完全自洽
CANDIDATE_PLUGIN_DIRS = [
    REPO_ROOT / "src" / "hermes-plugin",
    REPO_ROOT / "src" / "hermes-plugin" / "memory",
]

PLUGIN_DIR = None
env_plugin_dir = os.environ.get("XTMEM_PLUGIN_DIR")
if env_plugin_dir and Path(env_plugin_dir).exists():
    PLUGIN_DIR = Path(env_plugin_dir)
else:
    for cand in CANDIDATE_PLUGIN_DIRS:
        if (cand / "memory").exists() or (cand / "__init__.py").exists():
            PLUGIN_DIR = cand
            break

# 确保 memory 包可直接 import
if PLUGIN_DIR:
    if (PLUGIN_DIR / "memory").exists():
        if str(PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR))
    elif (PLUGIN_DIR / "__init__.py").exists():
        if str(PLUGIN_DIR.parent) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR.parent))

# 被测 CLI 主文件
CLI = Path(os.environ.get("XTMEM_CLI", REPO_ROOT / "src" / "cli" / "xtmem.py"))


def _init_minimal_canonical_db(db_path: Path) -> None:
    """在没有外部基准库时，初始化一个合规的最小测试基准库。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            agent TEXT,
            namespace TEXT,
            project TEXT,
            visibility TEXT DEFAULT 'shared',
            tier TEXT DEFAULT 'refined',
            track TEXT,
            state TEXT DEFAULT 'active',
            importance REAL DEFAULT 0.5,
            confidence REAL DEFAULT 1.0,
            utility REAL DEFAULT 0.0,
            access_count INTEGER DEFAULT 0,
            cited_count INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL,
            last_accessed_at REAL,
            source_ids TEXT,
            tags TEXT,
            tokens INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS xtmem_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            agent TEXT,
            event_type TEXT,
            chunk_id TEXT,
            detail TEXT
        )
    """)
    con.commit()
    con.close()


def _pick_canonical_db() -> Path:
    """确定基准库路径：env XTMEM_CANONICAL_DB 优先；找不到则生成自包含的临时最小库。"""
    env = os.environ.get("XTMEM_CANONICAL_DB")
    if env and Path(env).exists():
        return Path(env)
    
    # 公开环境下生成一个临时 session 级 canonical db
    fallback = Path(tempfile.gettempdir()) / "xtmem_canonical_test.db"
    _init_minimal_canonical_db(fallback)
    return fallback


@pytest.fixture(scope="session")
def canonical_db():
    """基准库只读探针：校验存在 + integrity_check=ok，返回其路径。"""
    p = _pick_canonical_db()
    if not p.exists():
        _init_minimal_canonical_db(p)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            pytest.fail(f"canonical 基准库 integrity_check != ok: {p} ({ok})")
    finally:
        con.close()
    return p


def _sqlite_backup_copy(src: Path, dst: Path) -> None:
    """用 SQLite backup API 把 src 一致快照拷贝到 dst（WAL 安全的正确方法）。"""
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


@pytest.fixture()
def tmp_db(canonical_db):
    """每用例一个独立临时库副本（canonical 的 backup API 拷贝）。yield db 路径。"""
    src = Path(canonical_db)
    tmpdir = tempfile.mkdtemp(prefix="xtmem-test-")
    db = Path(tmpdir) / "v.db"
    _sqlite_backup_copy(src, db)
    try:
        yield db
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def empty_db():
    """全新空临时库（无 canonical 数据），供'全新库上跑'类用例。yield db 路径。"""
    tmpdir = tempfile.mkdtemp(prefix="xtmem-test-empty-")
    db = Path(tmpdir) / "v.db"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    try:
        yield db
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def cli_env(tmp_db):
    """基于 os.environ 注入 XTMEM_DB_PATH / XTMEM_ACTOR / PYTHONUTF8。"""
    prior = {
        k: os.environ.get(k)
        for k in ("XTMEM_DB_PATH", "XTMEM_ACTOR", "PYTHONUTF8")
    }
    os.environ["XTMEM_DB_PATH"] = str(tmp_db)
    env = {**os.environ, "XTMEM_DB_PATH": str(tmp_db), "PYTHONUTF8": "1"}
    try:
        yield env
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        assert os.environ.get("XTMEM_DB_PATH") is None, (
            "tmp_db fixture 收尾未清干净 XTMEM_DB_PATH"
        )
