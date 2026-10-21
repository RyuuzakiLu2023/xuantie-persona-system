# ================================================================
# O02 端点与 doctor · 测试（pytest 面）
#
# 作用：把 O02 的四验/脱敏/只读契约钉进插件回归面，跟着 `pytest tests/` 一起跑，
#       防止后续改动悄悄放开（比如给普通视图加回路径、把 hello 从白名单摘掉）。
# 依赖：memory.endpoint_config / memory.doctor / memory.serve_protocol。
# 铁律：本文件只读、只造隔离库；断言的是**契约**不是实现细节。
#
# 为什么与 harness/o02_criteria.py 分开：
#   criteria 走真隔离 TCP 子进程（慢、要端口、要起 serve），
#   本文件跑纯函数级契约（快、无端口、可进 CI）。两层都要，
#   前者证"真链通"，后者证"改不坏"。
#
# 更新：2026-10-14 · O02 首版（端点解析优先级 / PID 兼容 / 脱敏 / 只读 / 失败分类）
# ================================================================
"""O02 endpoint configuration and read-only doctor contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "src" / "hermes-plugin"
sys.path[:0] = [str(PLUGIN)]

from memory import endpoint_config as ec        # noqa: E402
from memory import doctor as doc                # noqa: E402
from memory import serve_protocol as P          # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把端点相关环境全部隔离，并用**真实 schema** 造一个隔离库。

    ⚠️ 为什么必须走 ensure_xtmem_schema 而不是手写两张最小表：
    G5 返修后 `_check_database` 会逐项核对**必需表/列/索引**（R06 的病灶正是
    "只有 chunks(id)+xtmem_meta、版本号=3 就报 ok"）。手写最小表的库在新口径下
    正确地被判 `core_structure_incomplete` —— 那是**功能生效**，不是夹具可以
    取巧的地方。夹具要造"结构完整的库"，否则测的就不是 schema 版本这条轴。
    """
    for key in ("XTMEM_ACTOR", "XTMEM_ADMIN", "XTMEM_ADMIN_TOKEN", "XTMEM_SERVE_TOKEN",
                "XTMEM_SERVE_PORT", "XTMEM_SERVE_PID", "XTMEM_SERVE_DESCRIPTOR",
                "XTMEM_SERVE_CREDENTIAL_DIR"):
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "vector_store.db"
    from memory.vector_store import VectorStore
    from memory.schema import ensure_xtmem_schema
    vs = VectorStore(db_path=str(db))
    vs.close()
    conn = sqlite3.connect(str(db))
    ensure_xtmem_schema(conn)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    monkeypatch.setenv("XTMEM_DB_PATH", str(db))
    return tmp_path, db


# ── 端点解析 ──────────────────────────────────────────────────
def test_explicit_override_wins_over_everything(isolated):
    _, db = isolated
    cfg = ec.resolve_endpoint("dsh", explicit_db=str(db))
    assert cfg.db_path == str(db.resolve())
    assert cfg.db_id == ec.db_id_for(str(db.resolve()))
    assert cfg.db_source == ec.SOURCE_EXPLICIT


def test_profile_memory_advanced_db_path_is_honoured(isolated):
    """config.yaml 的 memory_advanced.db_path 必须能解析出来（A03 核心）。"""
    home, _ = isolated
    novel = home / "novel.db"
    (home / "config.yaml").write_text(
        "memory_advanced:\n  db_path: %s\n" % novel, encoding="utf-8")
    resolved = ec.resolve_db_path(hermes_home=str(home))
    assert resolved["source"] == ec.SOURCE_PROFILE_MEMORY_ADVANCED
    assert Path(resolved["db_path"]) == novel


def test_empty_profile_db_path_does_not_override_fallthrough(isolated):
    """空串不算显式设置——否则会顶掉回落链（现役 provider 双轨修复同款语义）。

    ⚠️ 必须显式传 `env={}` 把 env 层隔离掉：夹具已设 XTMEM_DB_PATH，
    不隔离的话 env 会先命中，这条就退化成在测 env 优先级（判据自身踩过）。
    """
    home, _ = isolated
    (home / "config.yaml").write_text("memory_advanced:\n  db_path: ''\n", encoding="utf-8")
    target = home / "fallback.db"
    resolved = ec.resolve_db_path(hermes_home=str(home), env={}, default=str(target))
    assert Path(resolved["db_path"]) == target
    assert resolved["source"] == ec.SOURCE_DEFAULT


def test_host_pinned_port_wins_and_matches_serve(isolated, monkeypatch):
    """宿主显式钉位（XTMEM_SERVE_PORT）优先于角色默认。

    ⚠️ 这条口径由 O02-R03 返修定：验收方固定判据在**全局口**上探活，并要求
    `hello['port'] == resolve_endpoint(actor).port` —— 即"解析器与真实 serve 必须一致"。
    若让 per-actor 赢，serve 听 A 而探活在 G ⇒ 判据连台面都起不来。
    故：launcher 显式钉位是明确指令，角色默认只在没钉位时生效。
    """
    monkeypatch.setenv("XTMEM_SERVE_PORT__DSH", "19999")
    monkeypatch.setenv("XTMEM_SERVE_PORT", "19000")
    dsh = ec.resolve_endpoint("dsh")
    zcode = ec.resolve_endpoint("zcode")
    assert dsh.port == 19000 and dsh.port_source == "env_global"
    assert zcode.port == 19000


def test_per_actor_port_applies_when_no_host_pin(isolated, monkeypatch):
    """没全局钉位时，角色的专属端口**必须**生效（per-actor 能力未被削弱）。"""
    monkeypatch.delenv("XTMEM_SERVE_PORT", raising=False)
    monkeypatch.setenv("XTMEM_SERVE_PORT__DSH", "19999")
    dsh = ec.resolve_endpoint("dsh")
    zcode = ec.resolve_endpoint("zcode")
    assert dsh.port == 19999 and dsh.port_source == "env_per_actor"
    assert zcode.port == ec.DEFAULT_SERVE_PORT      # 别的角色不受影响


def test_invalid_port_override_falls_back_and_is_labelled(isolated, monkeypatch):
    monkeypatch.setenv("XTMEM_SERVE_PORT", "not-a-port")
    cfg = ec.resolve_endpoint("dsh")
    assert cfg.port == ec.DEFAULT_SERVE_PORT
    assert cfg.port_source == "invalid_override_ignored"


def test_candidate_trace_records_why(isolated):
    """候选留痕必须说明"为什么是这个库"，而不是只给结果。"""
    resolved = ec.resolve_db_path(env={})
    sources = [c["source"] for c in resolved["candidates"]]
    assert ec.SOURCE_ENV in sources and ec.SOURCE_DEFAULT in sources
    assert any(not c["usable"] and c["reason"] for c in resolved["candidates"])


# ── 身份 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("actor", ["admin", "owner", "", "bad actor", "a" * 65, None, 7])
def test_invalid_actor_is_not_accepted(actor):
    assert ec.is_valid_actor(actor) is False


def test_valid_actor_shape():
    assert ec.is_valid_actor("dsh") and ec.is_valid_actor("assistant")


# ── PID 兼容 ──────────────────────────────────────────────────
def test_legacy_integer_pid_is_readable_but_carries_no_metadata(tmp_path):
    legacy = tmp_path / "legacy.pid"
    legacy.write_text(str(os.getpid()), encoding="utf-8")
    desc = ec.read_descriptor(legacy)
    assert desc["kind"] == ec.DESC_LEGACY_INT
    assert desc["pid"] == os.getpid()
    assert desc["record"] is None      # 旧整数推不出角色/库/ready


def test_legacy_pid_alone_cannot_report_ok(tmp_path):
    """doctor 不得因为"有个活 PID"就报 ok（只能证明有个进程）。"""
    legacy = tmp_path / "legacy.pid"
    legacy.write_text(str(os.getpid()), encoding="utf-8")
    cfg = ec.EndpointConfig(actor="dsh", host="127.0.0.1", port=1, port_source="t",
                            db_path="x", db_id="x", db_source="t",
                            descriptor_path=str(tmp_path / "absent.json"),
                            pid_path=str(legacy))
    check = doc._check_process(cfg)
    assert check["status"] != doc.STATUS_OK


def test_dead_pid_is_stale(tmp_path):
    p = tmp_path / "dead.pid"
    p.write_text("999999", encoding="utf-8")
    assert ec.classify_pid_state(ec.read_descriptor(p))["state"] == ec.PID_STALE


def test_pid_reuse_is_detected(tmp_path):
    p = tmp_path / "reused.json"
    ec.write_descriptor(p, {"pid": os.getpid(), "process_start": 1.0})
    assert ec.classify_pid_state(ec.read_descriptor(p))["state"] == ec.PID_REUSED


@pytest.mark.parametrize("content,reason", [
    ('{"pid": 1, "actor": "x', "descriptor_truncated_or_invalid_json"),
    ("", "descriptor_empty_write_interrupted"),
    ("[1,2,3]", "descriptor_not_an_object"),
    ('{"no_pid": true}', "descriptor_pid_missing_or_invalid"),
])
def test_corrupt_descriptors_are_reported_not_raised(tmp_path, content, reason):
    p = tmp_path / "d.json"
    p.write_text(content, encoding="utf-8")
    desc = ec.read_descriptor(p)
    assert desc["kind"] == ec.DESC_CORRUPT
    assert desc["reason"] == reason


def test_descriptor_write_is_atomic(tmp_path):
    """原子替换：不留半截文件，读者不会读到残缺 JSON。"""
    p = tmp_path / "ep.json"
    assert ec.write_descriptor(p, {"pid": 1, "actor": "dsh"})["ok"] is True
    assert not (tmp_path / "ep.json.tmp").exists()
    assert json.loads(p.read_text(encoding="utf-8"))["actor"] == "dsh"


# ── 活体核验的失败分类 ────────────────────────────────────────
def _cfg(db_path, port=1):
    return ec.EndpointConfig(actor="dsh", host="127.0.0.1", port=port, port_source="t",
                             db_path=str(db_path), db_id=ec.db_id_for(str(db_path)),
                             db_source="t")


def test_no_credential_is_refused_not_anonymous(isolated):
    """无凭据必须判 rejected —— 绝不允许换匿名路径（I11）。"""
    _, db = isolated
    result = ec.verify_remote_endpoint(_cfg(db), None)
    assert result["verdict"] == ec.VERIFY_REJECTED
    assert "anonymous" in result["reason"]


def test_unreachable_is_distinct_from_refused(isolated):
    """传输失败与安全拒绝不得合并（否则会把拒鉴当网络抖动降级）。"""
    _, db = isolated
    result = ec.verify_remote_endpoint(_cfg(db), "x" * 40, timeout=0.3)
    assert result["verdict"] == ec.VERIFY_UNREACHABLE
    assert result["reachable"] is False


# ── doctor 只读与脱敏 ─────────────────────────────────────────
def test_doctor_never_writes_to_the_database(isolated):
    """doctor 前后库字节不变（A09）。⚠️ 这条不许改成只比行数——建表/事件都是写。"""
    _, db = isolated
    before = db.read_bytes()
    result = doc.run_doctor("dsh", explicit_db=str(db), allow_probe=False)
    assert result["read_only"] is True
    assert db.read_bytes() == before


def test_doctor_refuses_without_identity(isolated):
    _, db = isolated
    result = doc.run_doctor("admin", explicit_db=str(db), allow_probe=False)
    assert result["overall"] == doc.OVERALL_AUTH_DENIED
    assert result["exit_code"] == doc.EXIT_AUTH_DENIED


def test_public_view_has_no_absolute_paths(isolated):
    _, db = isolated
    result = doc.run_doctor("dsh", explicit_db=str(db), allow_probe=False)
    blob = json.dumps(result, ensure_ascii=False, indent=1)
    assert str(db) not in blob
    assert str(db.parent) not in blob
    assert result["endpoint"].get("db_id")      # 指纹在，路径不在


def test_admin_view_carries_paths(isolated):
    _, db = isolated
    result = doc.run_doctor("dsh", admin=True, explicit_db=str(db), allow_probe=False)
    assert result["admin_view"] is True
    assert result["resolved"]["db_path"] == str(db.resolve())


def test_schema_mismatch_is_attributable_and_not_self_healed(isolated):
    """版本不符 → mismatch 可归因，且【不】暗改版本（A08/I18）。"""
    _, db = isolated
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    result = doc.run_doctor("dsh", explicit_db=str(db), allow_probe=False)
    assert result["checks"]["database"]["status"] == doc.STATUS_MISMATCH
    assert result["overall"] == doc.OVERALL_SCHEMA_MISMATCH
    assert result["exit_code"] == doc.EXIT_SCHEMA_MISMATCH
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
    conn.close()


def test_live_probe_disabled_is_unsupported_not_ok(isolated):
    """「没验」必须与「验过是好的」区分开——不许把跳过记成 ok。"""
    _, db = isolated
    result = doc.run_doctor("dsh", explicit_db=str(db), allow_probe=False)
    assert result["checks"]["live"]["status"] == doc.STATUS_UNSUPPORTED
    assert result["overall"] != doc.OVERALL_HEALTHY


def test_unsupported_is_not_healthy(isolated):
    _, db = isolated
    result = doc.run_doctor("dsh", explicit_db=str(db), allow_probe=False)
    assert result["ok"] is False


# ── 协议面 ────────────────────────────────────────────────────
def test_hello_is_allowed_and_readonly():
    assert "hello" in P.ALLOWED_COMMANDS
    assert "hello" in P.READONLY_DIAGNOSTIC_COMMANDS
    assert "hello" not in P.WRITE_COMMANDS


def test_ordinary_channel_gains_no_management_command():
    """普通通道不得因 O02 多出迁移/签发/批量删除类能力（§9.6 硬边界）。"""
    forbidden = {"migrate", "rest-token", "forget", "export", "import", "absorb",
                 "reembed", "sync", "maintain", "promote", "reveal"}
    assert not (P.ALLOWED_COMMANDS & forbidden)


def test_actor_token_never_returns_token_for_wrong_actor(tmp_path, monkeypatch):
    monkeypatch.delenv("XTMEM_SERVE_TOKEN", raising=False)
    monkeypatch.setenv("XTMEM_SERVE_CREDENTIAL_DIR", str(tmp_path / "cred"))
    token = P.actor_token("zcode", tmp_path / "d.db", create=True)
    assert isinstance(token, str) and len(token) >= 32
    # 另一个角色拿不到别人的 token（凭据按角色分文件）
    assert P.actor_token("dsh", tmp_path / "d.db") is None


def test_code_hash_stages_never_substitute_disk_for_live(tmp_path):
    """磁盘候选不得冒充活体已验（O02-A01/A12 的造假形态）。"""
    f = tmp_path / "cli.py"
    f.write_text("x = 1\n", encoding="utf-8")
    stages = ec.code_hash_stages(f, None, None)
    assert stages["disk_candidate"]["value"]
    assert stages["live_verified"]["value"] is None
    assert stages["live_verified"]["reason"] == "live_probe_not_attempted"
