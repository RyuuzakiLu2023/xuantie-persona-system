# -*- coding: utf-8 -*-
# D0-2 参数化单测：覆盖每类 TTL 豁免 + 关键词收窄 + 错误原料保持现 TTL
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass
MASTER = _REPO / "src" / "hermes-plugin"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

from memory.hybrid_retriever import _classify_ttl

CHECKS = {}
def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _guard() -> int:
    """脚本体收进函数：pytest 收集期只导入不执行，避免顶层 sys.exit 打断整个收集。"""
    CHECKS.clear()
    # ── 1. refined 全体豁免（哪怕正文含'临时'/'草稿'/'traceback'）──
    ttl_ref1 = _classify_ttl("这是一个临时方案", tier="refined")
    check("refined with '临时' -> TTL=0", ttl_ref1 == 0, f"got {ttl_ref1}")

    ttl_ref2 = _classify_ttl("Traceback (most recent call last):", tier="refined")
    check("refined with 'Traceback' -> TTL=0", ttl_ref2 == 0, f"got {ttl_ref2}")

    # ── 2. derived_from != '' 豁免 ──
    ttl_der = _classify_ttl("中间临时产物", tier="raw", derived_from="timeline")
    check("derived_from != '' with '临时' -> TTL=0", ttl_der == 0, f"got {ttl_der}")

    # ── 3. 五大免衰减分轨 ──
    for trk in ("procedural", "checkpoint", "explicit", "timeline", "profile", "identity", "preference"):
        res = _classify_ttl("普通流水内容", track=trk)
        check(f"track={trk} -> TTL=0", res == 0, f"got {res}")

    # ── 4. 符号保护与 metadata 保护 ──
    for sym in ("★", "🔒", "❗", "⚠️"):
        res = _classify_ttl(f"包含{sym}的关键记录", tier="raw")
        check(f"symbol {sym} -> TTL=0", res == 0, f"got {res}")

    for tag in ("important", "永久", "关键", "core"):
        res = _classify_ttl("正常记录", tags=[tag], tier="raw")
        check(f"tag {tag} -> TTL=0", res == 0, f"got {res}")

    # ── 5. raw 且非派生层的关键词生效（未被误豁免）──
    ttl_kw_temp = _classify_ttl("这是一个临时方案", tier="raw", derived_from="")
    check("raw non-derived with '临时' -> TTL=86400", ttl_kw_temp == 86400, f"got {ttl_kw_temp}")

    ttl_kw_err = _classify_ttl("error: connection refused", tier="raw", derived_from="")
    check("raw non-derived with 'error' -> TTL=3600", ttl_kw_err == 3600, f"got {ttl_kw_err}")

    # ── 6. raw 对话流水与普通 ──
    ttl_stream = _classify_ttl("user: hello", tags=["conversation"], tier="raw")
    check("raw conversation -> TTL=30 days", ttl_stream == 2592000, f"got {ttl_stream}")

    ttl_def = _classify_ttl("普通事实记录", tier="raw")
    check("raw default -> TTL=7 days", ttl_def == 604800, f"got {ttl_def}")

    # ── 7. 错误原料条目不提前豁免 ──
    ttl_err_raw = _classify_ttl("2026-09-05 错误·已纠正 记录", tags=["assistant-a误判"], tier="raw", derived_from="")
    check("error raw material NOT exempted -> retains normal TTL", ttl_err_raw > 0, f"got {ttl_err_raw}")

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== D0-2 守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    return 1 if failed else 0


def test_d02_guard():
    """pytest 入口：跑完整守卫并断言全绿。"""
    assert _guard() == 0, "见上方 FAIL 明细（D0-2）"


if __name__ == "__main__":
    sys.exit(_guard())
