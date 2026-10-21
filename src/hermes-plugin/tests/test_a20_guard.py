# ══════════════════════════════════════════════════════════════════════════
# A20 守卫 · 记账补漏三合一（2026-09-16 · assistant-c）
# ══════════════════════════════════════════════════════════════════════════
# 管什么：A20 三靶点的"不变量"。跑法：
#     python test_a20_guard.py            （无 pytest 也能跑）
#     pytest test_a20_guard.py -v
#
# ⚠️ 为什么用源码文本检查而不是 import：hook 是 zcode 的短命进程脚本（import 会带副作用、
#    且它 import 时要拉引擎），CLI 那份 import 会连库。解析文本 + AST 只看结构、零副作用。
#
# ⚠️ 反向验证为什么必须"sabotage 全部副本"（工单约束 6）：hook 有 5 份同 sha 活副本，
#    只改一份时，被测代码真实读到的可能是另一份 ⇒ 守卫照常绿、白验。故反向验证一次改 5 份。
# ══════════════════════════════════════════════════════════════════════════
import ast
import hashlib
import io
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HOOK_COPIES = [
    r"<USER_HOME>\.zcode\cli\plugins\cache\xtmem-local\xtmem\0.1.0\hooks\xtmem_hook.py",
    r"<USER_HOME>\.zcode\cli\plugins\marketplaces\xtmem-local\hooks\xtmem_hook.py",
    r"<XT_ROOT>\xtmem-project\src\zcode\hooks\xtmem_hook.py",
    r"<XT_ROOT>\xtmem-github\zcode-plugin\hooks\xtmem_hook.py",
    r"<XT_ROOT>\xtmem-zcode-plugin\hooks\xtmem_hook.py",
]
FROZEN = r"<XT_ROOT>\XTMEM\src\zcode\hooks\xtmem_hook.py"          # 挂 FROZEN.md，只报告不改
PUBLIC = r"<XT_ROOT>\XTMEM-public\src\zcode\hooks\xtmem_hook.py"   # 脱敏仓，不碰
XT = r"<XT_ROOT>\xtmem\xtmem.py"


def sha16(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]


def read(p):
    return io.open(p, encoding="utf-8", newline="").read()


# ── ① hook 五份必须同 sha（改一份=其余变旧版）──────────────────────────
def test_hook_five_copies_in_sync():
    shas = {p: sha16(p) for p in HOOK_COPIES if os.path.isfile(p)}
    missing = [p for p in HOOK_COPIES if not os.path.isfile(p)]
    assert not missing, "hook 副本缺失: %s" % missing
    assert len(set(shas.values())) == 1, (
        "hook 五份 sha 不一致（改一份没同步其余）: %s"
        % {os.path.basename(os.path.dirname(p)) + "/" + k: v for k, v in shas.items()}
    )


# ── ② exposed 只能走 expose 子命令，不得拿 recalled/mark 冒充 ────────────
def test_hook_exposed_only_uses_expose():
    for p in HOOK_COPIES:
        src = read(p)
        # record_exposed 函数体里只能出现 expose 子命令
        i = src.find("def record_exposed(")
        assert i > 0, "%s 找不到 record_exposed" % p
        body = src[i:src.find("\ndef ", i + 1) if src.find("\ndef ", i + 1) > 0 else len(src)]
        assert '"expose"' in body, "%s 的 record_exposed 没调 expose 子命令" % p
        for bad in ('["mark"', '["recall"', '"recalled"', '"cited"'):
            assert bad not in body, "%s 的 record_exposed 里混进了 %s（拿别的态冒充 exposed）" % (p, bad)


# ── ③ 空文本不记账 + 单事件内幂等（工单陷阱 1）─────────────────────────
def test_hook_empty_and_idempotent():
    for p in HOOK_COPIES:
        src = read(p)
        i = src.find("def record_exposed(")
        body = src[i:src.find("\ndef ", i + 1) if src.find("\ndef ", i + 1) > 0 else len(src)]
        assert "if not text" in body, "%s 没有空文本早退——SessionStart/Stop 会记出假账" % p
        assert "_seen_exposed" in body, "%s 没有单事件内去重——同一事件多入口会重复计数" % p


# ── ④ emit_context 必须真的调了 record_exposed（防"改了函数但没接上"）────
def test_hook_emit_context_wired():
    for p in HOOK_COPIES:
        src = read(p)
        i = src.find("def emit_context(")
        assert i > 0, "%s 找不到 emit_context" % p
        body = src[i:src.find("\ndef ", i + 1) if src.find("\ndef ", i + 1) > 0 else len(src)]
        assert "record_exposed(" in body, "%s 的 emit_context 没接记账——exposed 会永远 0" % p
        # 顺序：print 在前、记账在后（注入先落地，记账不挡它）
        assert body.find("print(") < body.find("record_exposed("), (
            "%s 里记账排在 print 之前——记账慢/失败会挡住注入" % p)


# ── ⑤ cmd_lookup --id 分支补了 recalled 记账，且【不】记 exposed ──────────
def _increment_calls_in(fn_node):
    """(行号, state 的【实参值】) —— 判值不判名、判代码不判注释。"""
    out = []
    for n in ast.walk(fn_node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "increment_utility"):
            continue
        sv = None
        for k in n.keywords:
            if k.arg == "state" and isinstance(k.value, ast.Constant):
                sv = k.value.value
        out.append((n.lineno, sv))
    return out


def test_cli_lookup_id_accounts():
    """cmd_lookup 的 --id 分支必须：① 真的调了 increment_utility ② state 实参值 == "recalled"
       ③ 【不】记 exposed ④ actor 走 _resolve_actor。

    ⚠️ 本条守卫第一版是【假绿】——它用 `assert 'state="recalled"' in branch` 做文本匹配，
       而那段代码的【注释】里就字面写着 state="recalled" ⇒ 把真调用的 state= 摘掉后，
       守卫在注释里命中、照常绿。A20 约束 6 的反向验证当场把它抓出来了。
       现改为 AST 取【实参值】，注释再也骗不到它。
    """
    src = read(XT)
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_lookup":
            fn = n
    assert fn is not None, "cmd_lookup 不见了"
    fn_src = ast.get_source_segment(src, fn) or ""
    # --id 分支 = 函数开头到 query 路径起点之间（分界行用特征串定位，不写死行号）
    split_rel = fn_src.find("\n    query =")
    assert split_rel > 0, "找不到 query 路径分界，先复核 cmd_lookup 结构"
    split_line = fn_src[:split_rel].count("\n") + fn.lineno

    id_calls = [(ln, sv) for ln, sv in _increment_calls_in(fn) if ln < split_line]
    assert id_calls, "--id 分支没调 increment_utility（零记账老病复发）"
    bad_state = [(ln, sv) for ln, sv in id_calls if sv != "recalled"]
    assert not bad_state, (
        "--id 分支的 increment_utility state 实参不是 'recalled'（行号,state值）: %s"
        "——不传 state 就是零计数假绿；记 exposed 则把候选冒充成真看见" % bad_state)
    # actor 必须解析（否则计数落到错误 viewer）
    assert "_resolve_actor" in fn_src[:split_rel], "--id 分支没解析 actor——计数会记到错误 viewer 上"


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    ok = 0
    fails = []
    for fn in ALL:
        try:
            fn()
            ok += 1
            print("  PASS  " + fn.__name__)
        except AssertionError as e:
            fails.append(fn.__name__)
            print("  FAIL  " + fn.__name__ + "\n        " + str(e).replace("\n", "\n        "))
    print("\nA20 守卫: %d passed / %d failed / %d total" % (ok, len(fails), len(ALL)))
    sys.exit(1 if fails else 0)
