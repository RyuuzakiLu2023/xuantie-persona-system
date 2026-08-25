# -*- coding: utf-8 -*-
"""XTMEM plugin smoke test (permanent regression asset, XTMEM spec §7).

Run (from repo root, with the hermes venv python that has deps):
    cd E:\\git项目源码\\hermes-agent-mainv0.21
    set PYTHONPATH=E:\\git项目源码\\hermes-agent-mainv0.21
    set XTMEM_PLUGIN_DIR=E:\\2222\\3333\\mimox-data\\plugins\\xtmem
    "C:\\...\\python.exe" tests\\xtmem_plugin_smoke.py

Covers:
- XtmemMemoryProvider 能被实例化（MemoryProvider ABC 子类）。
- initialize 后台建 builder；sync_turn 走 二段式写入（namespace/project/host_origin/updated_at）。
- prefetch 检索 + namespace 过滤 + token budget。
- 全部使用临时 DB 副本（不碰生产库）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = os.environ.get("XTMEM_PLUGIN_DIR", r"E:\2222\3333\mimox-data\plugins\xtmem")


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    sys.path.insert(0, PLUGIN_DIR)

    # 直接把插件作为普通包加载（绕开 hermes 插件注册器，便于单测）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "xtmem_plugin_test.provider", str(Path(PLUGIN_DIR) / "provider.py")
    )
    provider_mod = importlib.util.module_from_spec(spec)
    sys.modules["xtmem_plugin_test.provider"] = provider_mod
    spec.loader.exec_module(provider_mod)
    XtmemMemoryProvider = provider_mod.XtmemMemoryProvider

    with tempfile.TemporaryDirectory(prefix="xtmem-test-", ignore_cleanup_errors=True) as tmp:
        db_path = os.path.join(tmp, "vector_store.db")
        os.environ["XTMEM_DB_PATH"] = db_path

        p = XtmemMemoryProvider()
        p.initialize(
            "smoke-session",
            hermes_home=tmp,
            platform="cli",
            agent_context="primary",
            agent_identity="xuantie_lark",
        )
        for _ in range(60):
            if getattr(p, "_builder_ready", False) or getattr(p, "_builder_error", None):
                break
            time.sleep(0.2)
        if not p._builder_ready:
            print("FAIL builder not ready:", p._builder_error)
            return 1

        # 二段式写入（后台线程 store+metadata，轮询等两段都到位）
        p.sync_turn("测试记忆：铁铁喜欢喝冰美式", "好的，本小姐记住了！", session_id="smoke")
        row = None
        for _ in range(60):
            con = sqlite3.connect(db_path)
            row = con.execute(
                "SELECT namespace, project, host_origin, updated_at FROM chunks "
                "WHERE content LIKE '%铁铁喜欢喝冰美式%' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row is not None and row[3] is not None:
                break
            time.sleep(0.5)
        assert row is not None, "stored chunk not found"
        assert row[0] == "xuantie_lark", f"namespace mismatch: {row}"
        assert row[2] == "hermes:xuantie_lark", f"host_origin mismatch: {row}"
        assert row[3] is not None, "updated_at not set (two-stage write missing)"

        # 检索 + 过滤
        p._start_prefetch("冰美式")
        for _ in range(20):
            if p._prefetch_done:
                break
            time.sleep(0.3)
        assert "冰美式" in (p._prefetch_result or ""), "prefetch did not return the stored memory"
        assert p._last_prefetch_count > 0

        # S2 挂点：backup_paths 包含共享库
        assert db_path in p.backup_paths(), f"backup_paths missing db: {p.backup_paths()}"

        # S2 挂点：on_memory_write 旁路镜像（显式记忆 → 共享库）
        p.on_memory_write("add", "memory", "显式记忆S2挂点：玄铁审计归档策略", metadata={"source": "smoke"})
        found = False
        for _ in range(60):
            con = sqlite3.connect(db_path)
            row = con.execute(
                "SELECT namespace, project, host_origin, track FROM chunks "
                "WHERE content LIKE '%显式记忆S2挂点%' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row is not None and "memory-write" in (row[2] or ""):
                found = True
                assert row[0] == "xuantie_lark", f"S2 namespace mismatch: {row}"
                assert row[1] == "memory", f"S2 project mismatch: {row}"
                assert row[3] == "explicit", f"S2 track mismatch: {row}"
                break
            time.sleep(0.5)
        assert found, "on_memory_write did not mirror (metadata never applied)"

        # S2 挂点：on_delegation 观测子代理
        p.on_delegation("审计归档", "子代理发现两个残留挂点，已修复")
        found = False
        for _ in range(60):
            con = sqlite3.connect(db_path)
            row = con.execute(
                "SELECT track, host_origin FROM chunks "
                "WHERE content LIKE '%子代理发现两个残留挂点%' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row is not None and "delegation" in (row[1] or ""):
                found = True
                assert row[0] == "delegation", f"S2 delegation track mismatch: {row}"
                break
            time.sleep(0.5)
        assert found, "on_delegation did not mirror"

        # namespace 隔离：novel 域检索不到 xuantie 测试记忆
        p2 = XtmemMemoryProvider()
        p2.initialize("smoke2", hermes_home=tmp, platform="cli", agent_context="primary",
                      agent_identity="novel")
        for _ in range(60):
            if getattr(p2, "_builder_ready", False):
                break
            time.sleep(0.2)
        p2._start_prefetch("冰美式")
        for _ in range(20):
            if p2._prefetch_done:
                break
            time.sleep(0.3)
        assert "铁铁喜欢喝冰美式" not in (p2._prefetch_result or ""), "namespace filter failed"

        for p_ in (p, p2):
            try:
                p_.shutdown()
                b = getattr(p_, "_builder", None)
                if b is not None:
                    vs = getattr(b, "_vector_store", None)
                    if vs is not None and hasattr(vs, "close"):
                        vs.close()
            except Exception:
                pass

    print("OK: xtmem plugin smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
