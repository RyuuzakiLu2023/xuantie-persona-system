# -*- coding: utf-8 -*-
"""test_xtmem_skip_axes.py — Phase T T-3：跳过轴登记表四重自证。"""

import pathlib
import re

import axis_predicates as ap

TEST_DIR = pathlib.Path(__file__).parent
NO_GATE = {"ollama"}

# 代码里引用的是判据函数名，映射回轴 key（判据与轴共用一张表，不复制）
FN_TO_AXIS = {
    "ollama_available": "ollama",
    "jieba_available": "jieba",
    "canonical_ok": "canonical",
}


def _code_axis_refs():
    refs = set()
    for f in TEST_DIR.glob("test_*.py"):
        src = f.read_text(encoding="utf-8")
        for name in re.findall(r"axis_predicates\.(\w+)", src):
            if name in FN_TO_AXIS:
                refs.add(FN_TO_AXIS[name])
    return refs


def test_registry_matches_code():
    refs = _code_axis_refs()
    registered = set(ap.AXES)
    assert refs <= registered, "未登记轴: %s" % (refs - registered)
    assert registered <= (refs | NO_GATE), "登记但未生效且不在 NO_GATE: %s" % (registered - refs - NO_GATE)


def test_each_skip_point_one_axis():
    for f in TEST_DIR.glob("test_*.py"):
        src = f.read_text(encoding="utf-8")
        for name in re.findall(r"axis_predicates\.(\w+)", src):
            if name in FN_TO_AXIS:
                assert FN_TO_AXIS[name] in ap.AXES, "skip 点引用未知轴: %s in %s" % (name, f.name)


def test_each_axis_gates_at_least_one():
    refs = _code_axis_refs()
    for axis in ap.AXES:
        if axis in NO_GATE:
            continue
        assert axis in refs, "轴 %s 登记但无用例门控（不允许只登记不验证）" % axis


def test_doc_numbers_equal():
    assert len(ap.AXES) == 3
    assert set(ap.AXES) == {"ollama", "jieba", "canonical"}
