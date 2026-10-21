# ══════════════════════════════════════════════════════════════════════════
# A16 守卫 · exposure 引用率记账断链（2026-09-16 · assistant-c）
# ══════════════════════════════════════════════════════════════════════════
# 管什么：本单改动的"不变量"。跑法：
#     pytest tests/test_a16_exposed_guard.py -v
#     或 python tests/test_a16_exposed_guard.py      （无 pytest 也能跑）
#
# ⚠️ 为什么用 AST 而不是 import 引擎：import memory.vector_store 会拉起整个 engine
#    （sqlite 连接、embedding provider 初始化），守卫跑起来慢且有副作用。解析源码的
#    AST 只看结构，零副作用、零依赖——守卫必须能在任何机器上秒级跑绿。
#
# ⚠️ 为什么做"自证敏感性"（每条守卫都带一个"喂坏数据必须红"的反证）：
#    工单 §四 第 2 条点名了「自证循环」陷阱——实现与夹具共享同一个错误假设时，
#    全绿毫无意义。所以每条断言都必须先证明自己会红。
# ══════════════════════════════════════════════════════════════════════════
import ast
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve()
# 引擎（_state_cols / UTILITY_STATE_WEIGHTS 所在）
VS = HERE.parent.parent / "memory" / "vector_store.py"
# CLI（三处 increment_utility 调用所在）—— 注意要爬四层：tests→xtmem→plugins→mimox-data
XT = HERE.parent.parent.parent.parent / "xtmem" / "xtmem.py"
# 路径一错就立刻喊，别让守卫在半个文件里跑到一半才炸（今天已经踩过一次）
assert VS.exists(), "vector_store.py 没找到: %s" % VS
assert XT.exists(), "xtmem.py 没找到: %s" % XT


def _cls_attr_literals(src_path, attr_names):
    """从源码 AST 里抠出类属性的字面量值（dict/tuple）。
    不 import，只解析——理由见文件头。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id in attr_names:
                    try:
                        out[t.id] = ast.literal_eval(stmt.value)
                    except Exception:
                        out[t.id] = None
    return out


def _increment_calls(src_path):
    """返回源码里所有 increment_utility(...) 调用的 (lineno, 有没有传 state=)。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "increment_utility":
            kw_names = [k.arg for k in node.keywords]
            hits.append((node.lineno, "state" in kw_names, kw_names))
    return hits


# ── 守卫 1（maintainer 追加）：权重表的每个态都必须有计数列映射 ──────────────────────
def test_state_cols_covers_all_weights():
    """UTILITY_STATE_WEIGHTS 的【每个 key】都必须能在 _state_cols 找到列映射。

    为什么这是最要紧的一条：缺映射时 increment_utility 会
      ① 校验通过（state 合法）→ ② delta 按权重分层 → ③ utility_score 加了
      → 但 `if col:` 不成立 ⇒ 【一个计数列都不递增】。
    = "记了却看不见"的静默假绿。recalled 就是这么丢了很久（权重写了 0.02、列没有）。
    """
    attrs = _cls_attr_literals(VS, {"UTILITY_STATE_WEIGHTS", "_state_cols"})
    weights = attrs.get("UTILITY_STATE_WEIGHTS") or {}
    cols = attrs.get("_state_cols") or {}
    assert weights, "权重表没解析到——VS 路径或类名可能变了，先修守卫再谈别的"
    assert cols, "_state_cols 没解析到——它是类属性了吗？（本单把它从方法内提到了类上）"
    missing = sorted(set(weights) - set(cols))
    assert not missing, (
        "这些态有权重却没有计数列映射（会静默假绿）: %s\n"
        "  权重表: %s\n  映射表: %s" % (missing, sorted(weights), sorted(cols))
    )
    # 反证：喂一个缺 key 的映射表，守卫必须抓到（证明本断言不是永远绿）
    bad = dict(cols)
    bad.pop("recalled", None)
    assert sorted(set(weights) - set(bad)), "反证失败：摘掉 recalled 映射后没被抓到 → 守卫无效"


# ── 守卫 2（工单 §四 第 6 条 ①）：CLI 三处调用点确实传了 state ─────────────────
def test_cli_increment_sites_pass_state():
    """xtmem.py 里【每一处】 increment_utility 调用都必须显式传 state=。

    不传 state = 旧口径兼容 = 只累加 utility_score、不计任何 _n
    —— 正是 dsh/zcode 两端 exposed/cited/confirmed/corrected 全 0 的根因。
    """
    hits = _increment_calls(XT)
    assert hits, "xtmem.py 里没找到任何 increment_utility 调用——路径或写法变了，先修守卫"
    bare = [ln for ln, has_state, _ in hits if not has_state]
    assert not bare, "这些 increment_utility 调用没传 state=（会静默不计数）: 行 %s" % sorted(bare)
    # 本单点名的三处（特征串定位，不写死行号——BOM/工具差异会骗人）
    src = XT.read_text(encoding="utf-8")
    for probe in ('_record_cli_event_batch("recall"',  # 精确栈段 + 主路径段的锚
                  '精确回查（lookup by id）',           # cmd_lookup 段的锚
                  'P4.0-b'):                            # 主路径段特征串
        assert probe in src, "特征串 %r 不在源码里——靶点可能被移动，先复核再修" % probe
    # 反证：摘掉第一处非 cmd_expose 调用的 state= 后，本守卫必须红
    mutated = _mutate_strip_first_state(src)
    assert mutated != src, "反证没生效：AST 没找到可摘的 state= 调用"
    mutated_hits = _increment_calls_from_text(mutated)
    assert [ln for ln, has_state, _ in mutated_hits if not has_state], (
        "反证失败：把 state= 摘掉后守卫没抓到 → 守卫形同虚设"
    )


def _mutate_strip_first_state(text):
    """摘掉第一处【非 cmd_expose 内】increment_utility 调用的 state= 实参。

    ⚠️ 同 _mutate_first_recalled_to_exposed：走 AST 不走 str.replace——源码注释里
    可能字面写着 state="..."，字符串替换会去改注释、代码纹丝不动，反证变空操作。
    """
    tree = ast.parse(text)
    expose_span = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_expose":
            expose_span = (n.lineno, n.end_lineno or n.lineno + 60)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "increment_utility"):
            continue
        if expose_span and expose_span[0] <= node.lineno <= expose_span[1]:
            continue
        keep = [k for k in node.keywords if k.arg != "state"]
        if len(keep) != len(node.keywords):
            node.keywords = keep
            return ast.unparse(tree)
    return text


def _increment_calls_from_text(text):
    tree = ast.parse(text)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "increment_utility":
            kw_names = [k.arg for k in node.keywords]
            hits.append((node.lineno, "state" in kw_names, kw_names))
    return hits


# ── 守卫 3：exposed 只能由"拼装注入文本的那层"记，CLI recall 不得冒充 ───────────
def _exposed_state_lines(text):
    """返回源码里所有【state 值 == "exposed"】的 increment_utility 调用行号。

    ⚠️ 判值不判名：keyword 的 arg 名是 'state'，值才是 'exposed'——拿名字比值永远不命中
    （本守卫第一版就是这么写错的，自己被反证抓出来了）。
    """
    tree = ast.parse(text)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "increment_utility"):
            continue
        for k in node.keywords:
            if k.arg == "state" and isinstance(k.value, ast.Constant) and k.value.value == "exposed":
                out.append(node.lineno)
    return out


def _mutate_first_recalled_to_exposed(text):
    """把第一处【非 cmd_expose 内】的 state="recalled" 实参改成 "exposed"，返回新源码。

    ⚠️ 为什么走 AST 而不是 str.replace：源码注释里可能【字面写着 state="recalled"】
    （本单注释就写了），str.replace(...,1) 会去改注释、代码纹丝不动，反证就成了空操作
    ——守卫看起来"没抓到"，实际是"根本没改"。AST 只碰真调用的实参。
    """
    tree = ast.parse(text)
    expose_span = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_expose":
            expose_span = (n.lineno, n.end_lineno or n.lineno + 60)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "increment_utility"):
            continue
        if expose_span and expose_span[0] <= node.lineno <= expose_span[1]:
            continue
        for k in node.keywords:
            if k.arg == "state" and isinstance(k.value, ast.Constant) and k.value.value == "recalled":
                k.value = ast.Constant(value="exposed")
                return ast.unparse(tree)
    return text


def test_cli_does_not_fake_exposed():
    """CLI 的 recall/lookup 路径【不】得传 state="exposed"。

    CLI 返回的是【候选】（dsh 取 top12 / zcode 取 topN），候选 ≠ 真进注入文本。
    拿候选冒充 exposed 会让计数虚高 —— 尺子照样是坏的（假绿），比全 0 更难发现。
    唯一允许记 exposed 的入口是 cmd_expose（由注入层拼完最终文本后调用）。
    """
    src = XT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    expose_def = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "cmd_expose"]
    assert expose_def, "cmd_expose 不见了——它是注入层记 exposed 的唯一入口，删了 exposed 就归 0"
    legal = expose_def[0].lineno
    legal_end = expose_def[0].end_lineno or (legal + 60)  # 用真实函数边界，别拍脑袋加 60

    offenders = [ln for ln in _exposed_state_lines(src) if ln < legal or ln > legal_end]
    assert not offenders, (
        "除了 cmd_expose，还有别处直接记 exposed（会把候选冒充成真暴露）: 行 %s" % sorted(offenders)
    )
    # 反证：AST 把 recall 段第一处 recalled 改成 exposed，本守卫必须红
    mutated = _mutate_first_recalled_to_exposed(src)
    assert mutated != src, "反证没生效：AST 没找到可改的 recalled 调用"
    mutated_offenders = [ln for ln in _exposed_state_lines(mutated)
                         if ln < legal or ln > legal_end]
    assert mutated_offenders, (
        "反证失败：把 recall 段改成 exposed 后守卫没抓到 → 守卫无效"
    )


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
            fails.append((fn.__name__, str(e)))
            print("  FAIL  " + fn.__name__ + "\n        " + str(e).replace("\n", "\n        "))
    print("\nA16 守卫: %d passed / %d failed / %d total" % (ok, len(fails), len(ALL)))
    sys.exit(1 if fails else 0)
