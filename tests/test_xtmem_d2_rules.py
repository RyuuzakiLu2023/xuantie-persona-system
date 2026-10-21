# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_d2_rules.py · P7.2 四反例守卫
#
#   ▍这是什么
#   maintainer派单 P7.1+P7.2 合并单（2026-09-11）§2.2 验收要求的守卫测试：
#   每个反例至少 1 条守卫 + 反向验证改坏必红（铁律7）。
#   反例1 patch 版本不触发 / 反例2 跨项目误判 / 反例3 回滚极性无感 / 反例4 LLM 解析吞负号+无法判断落 0。
#
#   ▍修改铁律
#   1. 读取路径钉死运行时副本 MASTER——错题本 M1 教训：pytest 从仓库根跑会优先命中
#      <repo>/src/hermes-plugin/memory 旧副本；本文件用 importlib.util.spec_from_file_location
#      按绝对路径直载运行时 conflict.py，读的就是生产改的那份（反向验证 sabotage 它必红），
#      且不往 sys.path 插路径（避免污染整个 pytest 会话的解析，多副本混线会炸收集）。
#   2. 断言只用纯函数与 tempfile，严禁连生产库（铁律2：P7.2 零生产写）。
#   3. 本文件双副本同步（XTMEM/tests 与 XTMEM-public/tests），sha 不等即视为未交付。
#
#   ▍更新记录
#   *更新：2026-09-11 · 创建（assistant-c,P7.2 四反例守卫 10 条）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import importlib.util
import sys

# ⚠️ 取码方式：按【绝对路径直载】运行时副本，既不往 sys.path 插路径（那会污染整个 pytest
# 会话的模块解析，XTMEM-public/src 那份 09-04 旧副本缺 normalize_visibility，混线会炸收集），
# 又钉死"测试读的就是生产改的那份"（错题本 M1 教训：反向验证必须打中真实读取路径）。
MASTER_CONFLICT = _REPO / "src" / "hermes-plugin" / "memory" / "conflict.py"
_spec = importlib.util.spec_from_file_location("xtmem_conflict_under_test", MASTER_CONFLICT)
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


# ── 反例1：patch 版本位 ──────────────────────────────────────────

def test_f1_patch_version_distinguishes():
    """v1.2.3 vs v1.2.4 必须判 supersedes（旧公式 major*1000+minor 折成同值 1002 永不触发）"""
    a = {"id": "a1", "content": "服务版本 v1.2.3 已上线"}
    b = {"id": "b1", "content": "服务版本 v1.2.4 已上线"}
    r = C.judge_explicit_conflict(a, b)
    assert r is not None and r["decision"] == "supersedes"
    assert r["newer_id"] == "b1" and r["older_id"] == "a1"


def test_f1_patch_value_monotonic_and_semver_equal():
    """新取值对旧公式已不同的对子保序；v1.2 与 v1.2.0 同值（语义版本口径）"""
    vals = {}
    for name, txt in [("v123", "版本 v1.2.3"), ("v124", "版本 v1.2.4"), ("v13", "版本 v1.3"),
                      ("v199", "版本 v1.9.9"), ("v20", "版本 v2.0"), ("v12", "版本 v1.2"), ("v120", "版本 v1.2.0")]:
        p, v = C.extract_explicit_version(txt)
        assert p == 2
        vals[name] = v
    assert vals["v123"] < vals["v124"] < vals["v13"] < vals["v199"] < vals["v20"]
    assert vals["v12"] == vals["v120"]


# ── 反例2：跨项目 ────────────────────────────────────────────────

def test_f2_cross_project_not_supersede():
    """项目 alpha 的 v1.2 与项目 beta 的 v2.0 是平行事实，不下沉"""
    a = {"id": "a2", "content": "升级到 v1.2", "project": "alpha"}
    b = {"id": "b2", "content": "升级到 v2.0", "project": "beta"}
    assert C.judge_explicit_conflict(a, b) is None


def test_f2_same_project_still_supersedes():
    """同项目版本演进照常下沉（守卫不许误伤）"""
    a = {"id": "a3", "content": "升级到 v1.2", "project": "alpha"}
    b = {"id": "b3", "content": "升级到 v2.0", "project": "alpha"}
    r = C.judge_explicit_conflict(a, b)
    assert r is not None and r["decision"] == "supersedes"


def test_f2_empty_project_still_supersedes():
    """project 缺失不拦——历史数据大量为空，一刀切要求同项目会砸召回"""
    a = {"id": "a4", "content": "版本 v1.2"}
    b = {"id": "b4", "content": "版本 v2.0"}
    assert C.judge_explicit_conflict(a, b) is not None


# ── 反例3：回滚极性 ──────────────────────────────────────────────

def test_f3_rollback_decision_and_direction():
    """低版本侧带「改回+本方版本号」→ rollback 决策，方向=回滚陈述方为源"""
    hi = {"id": "h1", "content": "服务版本 v2.0 已上线"}
    lo = {"id": "l1", "content": "线上出问题，已改回 v1.2 并观察"}
    r = C.judge_explicit_conflict(hi, lo)
    assert r is not None and r["decision"] == "rollback"
    assert r["newer_id"] == "l1" and r["older_id"] == "h1"
    assert r["direction"] == "l1_rolls_back_h1"


def test_f3_rollback_word_alone_not_enough():
    """极性词不跟本方版本号 → 仍是正常 supersedes（松词表会把考试库 train 的正常升级误翻，教训见 conflict.py）"""
    a = {"id": "a5", "content": "配置升级到 v2.0（此前讨论过回滚预案，未实施）"}
    b = {"id": "b5", "content": "版本 v2.1"}
    r = C.judge_explicit_conflict(a, b)
    assert r is not None and r["decision"] == "supersedes"


def test_f3_evaluate_passthrough_rollback():
    """evaluate_pair_bidirectional 必须透传 rollback 决策（旧代码在规则路径硬编码 supersedes 会把回滚洗回取代）"""
    hi = {"id": "h2", "content": "版本 v2.0"}
    lo = {"id": "l2", "content": "内存吃紧，降级到 v1.0 运行"}
    res = C.evaluate_pair_bidirectional(hi, lo, {"policy_version": "p72-test"})
    assert res["decision"] == "rollback"
    assert res["details"]["newer_id"] == "l2"


# ── 反例4：LLM 解析 ─────────────────────────────────────────────

class _FakeResp:
    """urlopen 假响应：with 语法 + read() 返回预设 body"""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_f4_negative_score_sign_preserved(monkeypatch):
    """-0.8 不吞负号：夹取后 0.0（旧正则吞成 0.8 → 越阈值假阳性）"""
    monkeypatch.setattr(C, "_get_api_key", lambda: "test-key")
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda req, timeout=30: _FakeResp(b'{"choices":[{"message":{"content":"-0.8"}}]}'))
    assert C.call_llm_score("a", "b") == 0.0


def test_f4_unparseable_raises_not_zero(monkeypatch):
    """「无法判断」必须抛 ValueError 走 error 路径，不许静默落 0 分假阴性"""
    monkeypatch.setattr(C, "_get_api_key", lambda: "test-key")
    monkeypatch.setattr(C.urllib.request, "urlopen", lambda req, timeout=30: _FakeResp('{"choices":[{"message":{"content":"无法判断"}}]}'.encode("utf-8")))
    try:
        C.call_llm_score("a", "b")
    except ValueError:
        return
    raise AssertionError("不可解析输出应抛 ValueError，而不是落 0 分")


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
