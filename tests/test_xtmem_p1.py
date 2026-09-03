# -*- coding: utf-8 -*-
"""p1 回归迁移（Phase T · T-2，2026-09-01，小D）。

原 xtmem_p1_regression.py 拆 4 组：WAL 并发锁 / 强锁重写 / REST 鉴权 / AutoDream 可见性。
断言保留；DB 相关用 conftest empty_db/tmp_db + cli_env。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import conftest

CLI = conftest.CLI
PLUGIN_DIR = conftest.PLUGIN_DIR
AUTODREAM = r"<XT_ROOT>/mimox-data/scripts/xtmem_autodream.py"


def _run_cli(cli_env, db, *argv, env_extra=None):
    env = dict(cli_env)
    env["XTMEM_DB_PATH"] = str(db)
    env.pop("XTMEM_ACTOR", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=env)
    try:
        return json.loads(p.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (p.stdout or "")[:200]}


def test_p19_wal_concurrent_no_loss():
    from memory.wal_engine import WALEngine
    with tempfile.TemporaryDirectory(prefix="xtmem-p1-") as td:
        eng_wd = Path(td) / "engine"
        eng_wd.mkdir()
        wal_dir = str(eng_wd / "wal")
        eng = WALEngine(wal_dir=wal_dir)
        for i in range(200):
            eng.store("base-%d" % i, type="conversation")
        ids = [e.id for e in eng.read_all()]
        errors = []

        def appender(prefix, n):
            try:
                for i in range(n):
                    eng.store("%s-%d" % (prefix, i), type="conversation")
            except Exception as e:
                errors.append("append: %r" % e)

        def compressor():
            try:
                eng.mark_compressed(ids[:100], "summary-p1")
            except Exception as e:
                errors.append("compress: %r" % e)

        ts = [threading.Thread(target=appender, args=("A", 100)),
              threading.Thread(target=appender, args=("B", 100)),
              threading.Thread(target=compressor)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        total = len(eng.read_all())
        assert total == 400 and not errors, "total=%d errors=%s" % (total, errors[:2])


def test_p19_rewrite_requires_lock():
    from memory.wal_engine import WALEngine
    import platform
    with tempfile.TemporaryDirectory(prefix="xtmem-p1-") as td:
        eng_wd = Path(td) / "engine"
        eng_wd.mkdir()
        wal_dir = str(eng_wd / "wal")
        eng = WALEngine(wal_dir=wal_dir)
        eng.store("x", type="conversation")
        lock_file = Path(wal_dir) / ".lock"
        with open(lock_file, "a+") as f:
            if platform.system() == "Windows":
                import msvcrt
                f.seek(0)
                if os.fstat(f.fileno()).st_size == 0:
                    f.write("\0")
                    f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            rejected = False
            try:
                eng.mark_compressed([], "x")
            except RuntimeError:
                rejected = True
            assert rejected, "mark_compressed should raise when lock held"
            if platform.system() == "Windows":
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def test_p110_rest_auth(tmp_db):
    import importlib
    import memory.api_server as ap
    importlib.reload(ap)
    from fastapi.testclient import TestClient
    from memory.vector_store import VectorStore
    try:
        ap._AUTH_TOKEN = "topsecret"
        app = ap.create_app(host="127.0.0.1")
        client = TestClient(app)
        assert client.get("/api/memory/health").status_code == 401
        r_ok = client.get("/api/memory/health", headers={"X-XTMEM-Token": "topsecret"})
        assert r_ok.status_code in (200, 503) and r_ok.status_code != 401, r_ok.status_code
        vs = VectorStore(db_path=str(Path(tmp_db).parent / ("rest-%s.db" % Path(tmp_db).name)))
        app2 = ap.create_app(vector_store=vs, host="127.0.0.1")
        c2 = TestClient(app2)
        h = {"X-XTMEM-Token": "topsecret"}
        assert c2.patch("/memory/x", headers=h, json={"state": "bogus"}).status_code == 400
        assert c2.patch("/memory/x", headers=h, json={"track": "nonsense"}).status_code == 400
        assert c2.patch("/memory/x", headers=h, json={"state": "archived"}).status_code in (200, 404)
        vs.close()
        ap._AUTH_TOKEN = ""
        refused = False
        try:
            ap.create_app(host="0.0.0.0")
        except RuntimeError:
            refused = True
        assert refused, "非 loopback 无 token 应拒绝启动"
    finally:
        ap._AUTH_TOKEN = ""


def test_ad_autodream_visibility(empty_db, cli_env):
    _run_cli(cli_env, empty_db, "store", "--agent", "dsh", "--namespace", "dsh",
             "--text", "公开记忆P1梦境验证", "--importance", "0.9")
    _run_cli(cli_env, empty_db, "whisper", "--agent", "dsh", "--namespace", "dsh",
             "--text", "私密记忆P1梦境不可见", "--importance", "0.9")
    dream_dir = Path(tempfile.mkdtemp(prefix="xtmem-p1-"))
    env = dict(cli_env)
    env["XTMEM_DB_PATH"] = str(empty_db)
    env["XTMEM_DREAM_DIR"] = str(dream_dir)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, AUTODREAM], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=env)
    try:
        out = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        out = {}
    assert out.get("ok") is True, proc.stdout[:200]
    dream_files = list(dream_dir.glob("xtmem-autodream-*.md"))
    assert len(dream_files) == 1
    text = dream_files[0].read_text(encoding="utf-8")
    assert "私密记忆P1梦境不可见" not in text, "private 记忆绝不能进公共梦境"
    assert "公开记忆P1梦境验证" in text
    assert "仅公开记忆" in text
