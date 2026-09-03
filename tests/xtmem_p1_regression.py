# -*- coding: utf-8 -*-
"""xtmem_p1_regression.py — XTMEM P1 修复回归（2026-08-31，对应审计报告 P1-8/9/10 + AutoDream）

覆盖：
  P1-9  WAL 跨进程锁：append×2 线程 与 mark_compressed 强锁重写并发 → 日志零丢失
  P1-10 REST 鉴权：token 校验 / 非本机无 token 拒绝启动 / state+track 枚举校验
  AutoDream visibility：private 记忆绝不进公共梦境文件
数据保护：全部走临时目录，正式库零接触。
用法：python tests/xtmem_p1_regression.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PLUGIN_DIR = os.environ.get("XTMEM_PLUGIN_DIR", r"<XT_ROOT>/mimox-data/plugins/xtmem")
CLI = os.environ.get("XTMEM_CLI", r"<XT_ROOT>/mimox-data/xtmem/xtmem.py")
AUTODREAM = os.environ.get("XTMEM_AUTODREAM", r"<XT_ROOT>/mimox-data/scripts/xtmem_autodream.py")
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
    with tempfile.TemporaryDirectory(prefix="xtmem-p1-") as td:
        td = Path(td)
        eng_wd = td / "engine"
        eng_wd.mkdir()

        # ══════════════ P1-9：WAL 并发锁 ══════════════
        from memory.wal_engine import WALEngine
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

        ts = [
            threading.Thread(target=appender, args=("A", 100)),
            threading.Thread(target=appender, args=("B", 100)),
            threading.Thread(target=compressor),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        total = len(eng.read_all())
        check("p19_wal_no_loss_on_concurrent_rewrite", total == 400 and not errors,
              "total=%d errors=%s" % (total, errors[:2]))
        # 强锁可用性：重写拿不到锁时（预占锁）应抛错而非无锁重写
        lock_file = Path(wal_dir) / ".lock"
        with open(lock_file, "a+") as f:
            import platform
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
            check("p19_rewrite_requires_lock", rejected,
                  "mark_compressed should raise when lock held")
            if platform.system() == "Windows":
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

        # ══════════════ P1-10：REST 鉴权 ══════════════
        import importlib
        import memory.api_server as ap
        importlib.reload(ap)
        from fastapi.testclient import TestClient
        try:
            ap._AUTH_TOKEN = "topsecret"
            app = ap.create_app(host="127.0.0.1")
            client = TestClient(app)
            r_no = client.get("/api/memory/health")
            r_ok = client.get("/api/memory/health", headers={"X-XTMEM-Token": "topsecret"})
            r_bad = client.patch("/memory/whatever", headers={"X-XTMEM-Token": "topsecret"},
                                 json={"state": "bogus"})
            r_bad_track = client.patch("/memory/whatever", headers={"X-XTMEM-Token": "topsecret"},
                                       json={"track": "nonsense"})
            check("p110_token_required", r_no.status_code == 401, r_no.status_code)
            # 无 vector_store 时 health 走 DB 降级；临时环境无库 → 503 属正常，
            # 关键断言是"带对 token 就不再 401"
            check("p110_token_ok", r_ok.status_code in (200, 503) and r_ok.status_code != 401,
                  r_ok.status_code)
            # 需要真实 vector_store 才走到枚举校验：无 vector_store 时会先 503，
            # 所以这里构造带 vector_store 的 app 验证枚举 400
            from memory.vector_store import VectorStore
            vs = VectorStore(db_path=str(eng_wd / "rest.db"))
            app2 = ap.create_app(vector_store=vs, host="127.0.0.1")
            c2 = TestClient(app2)
            r_enum = c2.patch("/memory/x", headers={"X-XTMEM-Token": "topsecret"},
                              json={"state": "bogus"})
            r_enum2 = c2.patch("/memory/x", headers={"X-XTMEM-Token": "topsecret"},
                               json={"track": "nonsense"})
            r_enum3 = c2.patch("/memory/x", headers={"X-XTMEM-Token": "topsecret"},
                               json={"state": "archived"})
            check("p110_state_enum", r_enum.status_code == 400, r_enum.text[:80])
            check("p110_track_enum", r_enum2.status_code == 400, r_enum2.text[:80])
            check("p110_valid_state_passes_to_db", r_enum3.status_code in (200, 404),
                  r_enum3.status_code)
            vs.close()
            ap._AUTH_TOKEN = ""
            refused = False
            try:
                ap.create_app(host="0.0.0.0")
            except RuntimeError:
                refused = True
            check("p110_non_loopback_refused_without_token", refused)
        except Exception as e:
            import traceback
            check("p110_rest_auth", False, "EXC: %r %s" % (e, traceback.format_exc()[-300:]))
        finally:
            ap._AUTH_TOKEN = ""

        # ══════════════ AutoDream visibility ══════════════
        adb = td / "ad.db"
        r = run_cli(str(adb), "store", "--agent", "dsh", "--namespace", "dsh",
                    "--text", "公开记忆P1梦境验证", "--importance", "0.9")
        check("ad_seed_shared_ok", r.get("ok") is True)
        r = run_cli(str(adb), "whisper", "--agent", "dsh", "--namespace", "dsh",
                    "--text", "私密记忆P1梦境不可见", "--importance", "0.9")
        check("ad_seed_private_ok", r.get("ok") is True)
        dream_dir = td / "dreams"
        env = {**os.environ, "XTMEM_DB_PATH": str(adb), "XTMEM_DREAM_DIR": str(dream_dir),
               "PYTHONUTF8": "1"}
        proc = subprocess.run([PYTHON, AUTODREAM], capture_output=True, text=True,
                              encoding="utf-8", timeout=120, env=env)
        try:
            out = json.loads(proc.stdout.strip() or "{}")
        except Exception:
            out = {}
        check("ad_script_ok", out.get("ok") is True, proc.stdout[:200])
        dream_files = list(dream_dir.glob("xtmem-autodream-*.md"))
        check("ad_dream_file_exists", len(dream_files) == 1)
        if dream_files:
            text = dream_files[0].read_text(encoding="utf-8")
            check("ad_private_excluded", "私密记忆P1梦境不可见" not in text)
            check("ad_shared_included", "公开记忆P1梦境验证" in text)
            check("ad_scope_label", "仅公开记忆" in text)

        all_ok = all(v is True for v in CHECKS.values())
        print(json.dumps({"ok": all_ok, "checks": CHECKS}, ensure_ascii=False))
        return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
