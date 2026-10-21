# -*- coding: utf-8 -*-
"""test_xtmem_b_s3_shared.py — B方案 S3 provider 共享 builder 守卫（2026-09-06，assistant-c）。

判据（DoD）：
  ① _build_builder 二次调用命中模块级 _SHARED_BUILDERS（refcount+1，不重复水化）；
  ② shutdown 只减 refcount（resident 模式不 close builder）；
  ③ resident_builder=false 时回退 per-agent 构建（I3 一键回退语义）。
环境纪律：tmp_db 副本；不真起 hermes 网关（S3 端到端 <300ms 需网关重启排窗——
这里验证注册表复用逻辑，端到端等用户A排窗后验收）。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import json
import os
import sqlite3
import sys

import pytest

from conftest import PLUGIN_DIR
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# 被测模块
import importlib
import memory.api_server  # noqa
sys.path.insert(0, str(PLUGIN_DIR))
import provider as prov_mod


@pytest.fixture()
def _prov_env(tmp_db, monkeypatch):
    monkeypatch.setenv("XTMEM_DB_PATH", str(tmp_db))
    monkeypatch.setenv("XTMEM_HERMES_ROOT", _REPO / "src" / "hermes-plugin")
    return tmp_db


def _make_provider(tmp_db, resident=True):
    """构造 XtmemMemoryProvider 实例（initialize 会起 warmup 线程——测试里直接手动调 _build_builder）。"""
    p = prov_mod.XtmemMemoryProvider()
    p._hermes_home = str(tmp_db).rsplit("\\", 1)[0] if "\\" in str(tmp_db) else str(tmp_db)
    p._plugin_cfg = {"resident_builder": resident, "db_path": str(tmp_db),
                     "memory_advanced": {}, "stats": {}}
    p._namespace = "dsh"
    p._project = ""
    p._resolve_db_path = lambda: str(tmp_db)  # 钉死测试库
    return p


def test_s3_second_provider_reuses_shared_builder(tmp_db, _prov_env):
    """两个 provider 实例：第二个 _build_builder 必须命中共享注册表（同一 builder 对象）。"""
    # 清注册表（隔离）
    with prov_mod._SHARED_LOCK:
        for k in list(prov_mod._SHARED_BUILDERS):
            prov_mod._SHARED_BUILDERS.pop(k, None)
        prov_mod._SHARED_REFCOUNT.clear()
        prov_mod._SHARED_RESIDENT_ENABLED.clear()

    p1 = _make_provider(tmp_db, resident=True)
    b1 = p1._build_builder()
    p1._builder = b1  # 生产走 _ensure_builder 赋值；测试直调需手动挂
    p1._builder_ready = True
    assert b1 is not None

    # 第二个 provider：应命中共享（同对象）
    p2 = _make_provider(tmp_db, resident=True)
    b2 = p2._build_builder()
    p2._builder = b2
    p2._builder_ready = True
    assert b2 is b1, "第二个 provider 应复用同一 builder 对象（S3 常驻书架）"

    # refcount 应为 2
    with prov_mod._SHARED_LOCK:
        rc = prov_mod._SHARED_REFCOUNT.get(str(tmp_db), 0)
    assert rc == 2, f"refcount 应为 2，实际 {rc}"

    # shutdown p1：resident 模式不 close，refcount 减到 1
    p1.shutdown()
    with prov_mod._SHARED_LOCK:
        rc = prov_mod._SHARED_REFCOUNT.get(str(tmp_db), 0)
        still_shared = prov_mod._SHARED_BUILDERS.get(str(tmp_db))
    assert rc == 1, f"shutdown 后 refcount 应为 1，实际 {rc}"
    assert still_shared is b1, "resident 模式 shutdown 不应 close 共享 builder"

    # 清理
    p2.shutdown()


def test_s3_resident_false_falls_back_per_instance(tmp_db, _prov_env):
    """resident_builder=false → 每实例独立构建（I3 一键回退语义）。"""
    with prov_mod._SHARED_LOCK:
        for k in list(prov_mod._SHARED_BUILDERS):
            prov_mod._SHARED_BUILDERS.pop(k, None)
        prov_mod._SHARED_REFCOUNT.clear()
        prov_mod._SHARED_RESIDENT_ENABLED.clear()

    p1 = _make_provider(tmp_db, resident=False)
    b1 = p1._build_builder()
    p1._builder = b1
    p2 = _make_provider(tmp_db, resident=False)
    b2 = p2._build_builder()
    p2._builder = b2
    assert b1 is not None and b2 is not None
    # 非 resident：不进共享表
    with prov_mod._SHARED_LOCK:
        assert str(tmp_db) not in prov_mod._SHARED_BUILDERS, "resident=false 不进共享注册表"
    p1.shutdown()
    p2.shutdown()
