# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_f_phatic_tail2.py · 尾批二单F守卫
#
#   ▍这是什么
#   bundle_selector 判空词表（is_chitchat）补**纯确认词 + 纯标点通道**的守卫。
#   盲标考卷里 8 条 T4 寒暄题（expect=empty）实测 5 条穿透判空白烧检索：
#   「收到」「嗯」「。」「？？」「收到收到」——词表三重闸（token 命中 + 全字符 +
#   ≤12 字）救不了：裸「嗯」「收到」不在 tokens；纯标点无 token 可命中。
#   故补 ① tokens 加纯确认词 ② chars 同步补 ③ 新增纯标点第四通道。
#
#   本守卫钉死三件事：
#   - 新补的正例全判 True（含纯标点）；
#   - 防误杀组一条都不能错 False（内容是内容，寒暄是寒暄）；
#   - 工单点名【不】接的三条（`1`/`测试`/`今天星期几`）保持 False——
#     判空层故意不接，expect=empty 由上层兜，塞进词表就是过拟合考卷。
#
#   ▍怎么跑
#   python test_xtmem_f_phatic_tail2.py   （或 pytest 本文件）
#   纯函数测试，不碰库（生产零写）。
#
#   ▍修改铁律
#   1. 判据直载生产代码（deploy 树 memory/bundle_selector.py），禁在本文件里
#      抄一份词表当"影子实现"——那样测的是抄件不是生产件，改了生产件也不红。
#   2. 副本同步断言：实体副本三处 sha16 必须 EQ（junction 已实查：engine 是指向
#      memory 的 junction，物理同一份；另两处是独立实体副本，须同步）。
#   3. 反例组只许增不许减——删反例=给误杀开门。
#
#   ▍更新记录
#   *更新：2026-09-13 · 创建（尾批二单F，maintainer代执）*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import hashlib
import importlib.util
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 判据源 = deploy 树（测试 conftest 的 PLUGIN_DIR 同款，测试实际加载的就是这份）
MASTER_BS = _REPO / "src" / "hermes-plugin" / "memory" / "bundle_selector.py"
# 三处实体副本（junction 实查：plugins/xtmem/engine 是指向 memory 的 junction，
# 不是第四份；profiles/*/plugins/xtmem 也是 junction 指回 plugins/xtmem）
COPIES = [
    _REPO / "src" / "hermes-plugin" / "memory" / "bundle_selector.py",
    _REPO / "xtmem-project"/ "src"/ "hermes-plugin"/ "memory"/ "bundle_selector.py",
    _REPO / "XTMEM"/ "src"/ "hermes-plugin"/ "memory"/ "bundle_selector.py",
]

POSITIVE = [
    # 尾批二新补（5 条穿透题 + 补词覆盖）
    "收到", "嗯", "。", "？？", "收到收到",
    "知道了", "明白了", "了解", "哦", "噢",
    # 原有正例（不回归）
    "哦哦", "嗯嗯", "好的",
]
NEGATIVE = [
    # 工单指定的防误杀组（一条不能错）
    "修一下", "帮我看下", "测试服务器", "今天星期几", "1", "hi file",
    "帮我修复了bug", "修了",
    # 额外反证：确认词一旦混进内容字符必须立刻 False
    "不明白了", "收到文件了吗", "嗯这版不行", "。改一下",
]

CHECKS = {}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _load_master():
    spec = importlib.util.spec_from_file_location("bs_tail2_guard", MASTER_BS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sha16(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM 单F · 判空词表纯确认词守卫 ===")
    m = _load_master()

    for q in POSITIVE:
        check("1.x 正例判空 True: %r" % q, m.is_chitchat(q) is True, repr(q))
    for q in NEGATIVE:
        check("2.x 反例必须 False: %r" % q, m.is_chitchat(q) is False, repr(q))

    # 副本同步：三处（实体份）sha16 必须 EQ
    shas = {p: _sha16(p) for p in COPIES if os.path.exists(p)}
    print("  [evidence] copy sha16: %s" % {os.path.basename(os.path.dirname(os.path.dirname(p))): s
                                          for p, s in shas.items()})
    check("3.1 三处实体副本都在", len(shas) == len(COPIES),
          "缺: %s" % [p for p in COPIES if p not in shas])
    check("3.2 三处 sha16 EQ", len(set(shas.values())) == 1, str(shas))

    failed = [k for k, v in CHECKS.items() if not v]
    print(f"\n=== 单F 判空词表守卫结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} passed ===")
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_f_phatic_tail2():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
