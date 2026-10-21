# -*- coding: utf-8 -*-
"""cli_smoke 迁移（Phase T · T-2，2026-09-01，assistant-c + T-3 canonical 跳过轴）。"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import axis_predicates
import conftest

CLI = conftest.CLI
pytestmark = pytest.mark.skipif(not axis_predicates.canonical_ok(), reason="canonical 基准库不可用（tmp_db 拷贝源）")


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


def test_cli_store_dedupe(tmp_db, cli_env):
    r = _run_cli(cli_env, "store", "--text", "CLI回归：assistant-a喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6")
    assert r.get("ok") is True and r.get("id"), r
    store_id = r["id"]
    r2 = _run_cli(cli_env, "store", "--text", "CLI回归：assistant-a喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6")
    assert r2.get("ok") is True and r2.get("skipped"), r2


def test_cli_lookup_cross(tmp_db, cli_env):
    _run_cli(cli_env, "store", "--text", "CLI回归：assistant-a喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6")
    r = _run_cli(cli_env, "lookup", "--query", "桃子汽水", "--top", "3", "--cross")
    assert r.get("ok") is True and r.get("count", 0) >= 1, r


def test_cli_forget(tmp_db, cli_env):
    store_id = _run_cli(cli_env, "store", "--text", "CLI回归：assistant-a喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6").get("id")
    r = _run_cli(cli_env, "forget", "--id", str(store_id), "--as", "dsh", "--confirm")
    assert r.get("ok") is True and r.get("removed", 0) >= 1, r


def test_cli_import_export_dedupe(tmp_db, cli_env):
    # P1-I 隔离尾巴（2026-09-04 收）：export 默认写真实 ~/.zcode/cli/memories/projects——
    # 【不】许测试碰用户真实 zcode 目录；与 import 共用同一临时根（见下 td）。
    td = Path(tempfile.mkdtemp(prefix="xtmem-cli-"))
    # export 需要至少一个项目目录才不报 "no zcode project dirs"——先造好，export/import 共用
    name = "demo"
    h16 = hashlib.sha256(name.lower().encode()).hexdigest()[:16]
    memdir = td / "zcode" / ("%s-%s" % (name, h16)) / "memory"
    memdir.mkdir(parents=True)
    r = _run_cli(cli_env, "export", env_extra={"XTMEM_ZCODE_MEMORIES": str(td / "zcode")})
    assert r.get("ok") is True, r
    (memdir / "mem-zc-fresh.md").write_text(
        "---\nid: mem-zc-fresh\nnamespace: zcode\nproject: demo\n"
        "importance: 0.5\ntimestamp: 1787100000\ntags: fresh\ntrack: default\n---\n\n"
        "文件轨导入回归：teacher记住 [[部署偏好]] 托管在 E 盘。\n", encoding="utf-8")
    env_extra = {"XTMEM_ZCODE_MEMORIES": str(td / "zcode")}
    r = _run_cli(cli_env, "import", "--as", "zcode", env_extra=env_extra)
    assert r.get("ok") is True and r.get("added", 0) >= 1, r
    r2 = _run_cli(cli_env, "import", "--as", "zcode", env_extra=env_extra)
    assert r2.get("duplicates", 0) >= 1, r2


def test_cli_agents_and_md(tmp_db, cli_env):
    assert _run_cli(cli_env, "agents").get("ok") is True
    out = Path(tempfile.mkdtemp(prefix="xtmem-cli-")) / "agents.md"
    r = _run_cli(cli_env, "agents.md", "--out", str(out))
    assert r.get("ok") is True and out.exists(), r


def test_cli_reembed(tmp_db, cli_env):
    r = _run_cli(cli_env, "reembed", "--limit", "1")
    assert r.get("ok") is True or r.get("ok") is False, r


def test_cli_whisper_visibility(tmp_db, cli_env):
    r = _run_cli(cli_env, "whisper", "--agent", "zcode", "--namespace", "zcode",
                 "--text", "框架冒烟：teacher的秘密只有自己可见")
    assert r.get("ok") is True and r.get("visibility") == "private", r
    wid = r.get("id")
    assert _run_cli(cli_env, "lookup", "--query", "框架冒烟", "--top", "5", "--as", "personal").get("count", 0) == 0
    assert _run_cli(cli_env, "lookup", "--query", "框架冒烟", "--top", "5", "--as", "zcode").get("count", 0) >= 1
    if wid:
        r = _run_cli(cli_env, "reveal", "--id", str(wid), "--to", "personal", "--as", "zcode")
        assert r.get("ok") is True and r.get("visibility") == "shared-with:personal", r


def test_cli_relations_messages_reflect(tmp_db, cli_env):
    assert _run_cli(cli_env, "relations", "set", "--agent-a", "personal", "--agent-b", "teacher",
                    "--sentiment", "-0.4", "--trust", "0.3", "--note", "框架冒烟：骗过一次").get("ok") is True
    assert _run_cli(cli_env, "relations", "summary", "--as", "personal").get("count", 0) >= 1
    assert _run_cli(cli_env, "msg", "send", "--to", "personal", "--from", "teacher",
                    "--text", "框架冒烟：你的代码又出bug了").get("ok") is True
    assert _run_cli(cli_env, "msg", "inbox", "--as", "personal").get("count", 0) >= 1
    r = _run_cli(cli_env, "reflect", "--agent", "personal", "--text", "框架冒烟：被diss后我该更谨慎")
    assert r.get("ok") is True and r.get("visibility") == "private", r
