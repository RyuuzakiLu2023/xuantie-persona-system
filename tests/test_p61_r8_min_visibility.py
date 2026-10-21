# -*- coding: utf-8 -*-
"""P6.1/R8 守卫（v1.3.1 §六 守卫1）：min_visibility 多源 shared-with 交集语义。

四条守卫：两源交集 / 无交集 private / 乱序规范化同值 / 三源 mixed=private。
"""
import pytest
from memory.visibility import min_visibility


def _lk(mapping):
    return lambda sid: mapping.get(sid)


def test_r8_two_sources_intersection():
    # 两源不同名单 → 交集
    assert min_visibility(["a", "b"], _lk({
        "a": "shared-with:alice,bob",
        "b": "shared-with:bob,carol",
    })) == "shared-with:bob"


def test_r8_no_intersection_private():
    # 有 shared-with 源但无交集 → private（禁回落 shared）
    assert min_visibility(["a", "b"], _lk({
        "a": "shared-with:alice",
        "b": "shared-with:bob",
    })) == "private"


def test_r8_order_normalized():
    # 乱序 → 序列化规范化，同值（",".join(sorted(...))）
    m = {"a": "shared-with:carol,alice,bob", "b": "shared-with:bob,alice"}
    va = min_visibility(["a", "b"], _lk(m))
    vb = min_visibility(["b", "a"], _lk(m))
    assert va == vb == "shared-with:alice,bob"


def test_r8_three_sources_mixed_private():
    # 三源 mixed（private+shared-with+shared）→ private（C-O7 落成测试）
    assert min_visibility(["a", "b", "c"], _lk({
        "a": "private",
        "b": "shared-with:bob",
        "c": "shared",
    })) == "private"


def test_r8_shared_only_stays_shared():
    # 全 shared 源（无 shared-with）→ shared（旧语义保持）
    assert min_visibility(["a", "b"], _lk({"a": "shared", "b": "shared"})) == "shared"


def test_r8_missing_source_private():
    # BF-3 沿旧：查不到/unknown → private
    assert min_visibility(["gone"], _lk({})) == "private"


def test_r8_empty_source_ids_private():
    assert min_visibility([]) == "private"
