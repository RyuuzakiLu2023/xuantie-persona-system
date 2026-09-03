# -*- coding: utf-8 -*-
"""XTMEM 测试基建 · conftest.py（Phase T · T-1，2026-09-01，小D）

给 pytest 提供三类 fixture，让"序列脚本"拆成"独立用例 + fixture 隔离 + 跳过轴 + 守卫"四层。

fixture 一览：
  canonical_db  定位基准库（只读），校验存在与 integrity_check=ok
  tmp_db        每用例一个独立临时库副本 —— SQLite backup API 拷贝（禁文件级 COPY，总则 3 同款）
  empty_db      全新空临时库（供"全新库上跑"类用例——去重/隔离不靠残留环境）
  cli_env       CLI 子进程环境：注入 XTMEM_DB_PATH / XTMEM_ACTOR / PYTHONUTF8；
                yield 后 **finally pop** 掉 XTMEM_DB_PATH（防污染后续非 pytest 进程，总则 2 教训），
                并断言环境未漂移（"谁改坏的谁红"）

⚠️ 两个坑（血泪教训，写在注释里防后人犯）：
  1. 临时库必须**每用例重建**——store 去重/隔离测试会受前面用例的残留影响，
     这也是为什么旧脚本只能按固定顺序跑；拆用例后这个限制解除。
  2. `XTMEM_DB_PATH` 残留曾把正式 hermes 指到测试库（总则 2）。fixture 收尾必须
     `finally: os.environ.pop("XTMEM_DB_PATH", None)`，**不是只还原**——防 pytest
     把环境泄漏给后续非 pytest 进程。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# 被测插件目录（与既有冒烟脚本同款默认值；可用 XTMEM_PLUGIN_DIR 覆盖）
PLUGIN_DIR = Path(
    os.environ.get("XTMEM_PLUGIN_DIR", r"<XT_ROOT>/mimox-data/plugins/xtmem")
)
# 被测 CLI 主文件（可用 XTMEM_CLI 覆盖）
CLI = Path(os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"))

# 让测试能 `from memory.xxx import ...`（守卫测试判据直接从生产代码抽纯函数）
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# 基准库默认：优先用干净快照（避免每用例都去备份热库、也避免误写生产库）。
#   缺省主库路径只会被读（canonical_db 只读连接），但拷贝热库每用例 46MB 太慢。
_CANONICAL_DEFAULT_CANDIDATES = [
    r"<USER_DOCS>/xtmem-test/vector_store_r3_canonical.db",
    r"<XT_ROOT>/mimox-data/.hermes/memory/vector_store.db",
]


def _pick_canonical_db() -> Path:
    """确定基准库路径：env `XTMEM_CANONICAL_DB` 优先；其次干净快照；最后主库。

    只读探针，绝不写它。拷贝进 tmp_db 用的是 SQLite backup API（一致快照），
    不是文件级 COPY（WAL 下文件 COPY = 撕裂快照）。
    """
    env = os.environ.get("XTMEM_CANONICAL_DB")
    if env:
        return Path(env)
    for cand in _CANONICAL_DEFAULT_CANDIDATES:
        p = Path(cand)
        if p.exists():
            return p
    raise RuntimeError("找不到 canonical 基准库，请设 XTMEM_CANONICAL_DB")


@pytest.fixture(scope="session")
def canonical_db():
    """基准库只读探针：校验存在 + integrity_check=ok，返回其路径。"""
    p = _pick_canonical_db()
    if not p.exists():
        pytest.fail(f"canonical 基准库不存在: {p}")
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
    """全新空临时库（无 canonical 数据），供"全新库上跑"类用例。yield db 路径。"""
    tmpdir = tempfile.mkdtemp(prefix="xtmem-test-empty-")
    db = Path(tmpdir) / "v.db"
    # 先触达一次，让 sqlite 建到盘（store/CLI 会建 schema）
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    try:
        yield db
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def cli_env(tmp_db):
    """基于 os.environ 注入 XTMEM_DB_PATH / XTMEM_ACTOR / PYTHONUTF8。

    yield 一个 dict 供 subprocess.run(env=...) 使用；
    收尾 finally 把临时注入的环境变量 pop/还原，并断言未漂移。
    注意：**必须 pop 而非只还原 XTMEM_DB_PATH**——防泄漏给后续非 pytest 进程。
    """
    prior = {
        k: os.environ.get(k)
        for k in ("XTMEM_DB_PATH", "XTMEM_ACTOR", "PYTHONUTF8")
    }
    os.environ["XTMEM_DB_PATH"] = str(tmp_db)
    # 默认不设 XTMEM_ACTOR（各用例按需设）；这里语义是"提供给子进程的 env base"
    env = {**os.environ, "XTMEM_DB_PATH": str(tmp_db), "PYTHONUTF8": "1"}
    try:
        yield env
    finally:
        # 还原成"被测前"的值；XTMEM_DB_PATH 即便还原值也必须是 None 才算干净
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 断言环境未漂移：任何用例结束后 XTMEM_DB_PATH/ACTOR 都不该残留
        assert os.environ.get("XTMEM_DB_PATH") is None, (
            "tmp_db fixture 收尾未清干净 XTMEM_DB_PATH（会污染非 pytest 进程/正式 hermes）"
        )
