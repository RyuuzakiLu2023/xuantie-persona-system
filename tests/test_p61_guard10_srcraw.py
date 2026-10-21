# -*- coding: utf-8 -*-
"""P6 波1 验收补测：守卫10（O3-a 演化 [src-raw] 守卫）+ 守卫7 残留。

守卫10：含 [src-raw:] 的候选从演化输入剔除；正常候选保留。
"""
from memory.context_builder import _filter_srcraw_candidates


def test_srcraw_candidate_excluded():
    cands = [("mem-aaaa1111", "画像正文 [src:abcd1234] 正常", 0.7),
             ("mem-bbbb2222", "画像回落 [src-raw:bbbb2222] 兜底", 0.6),
             ("mem-cccc3333", "画像回落 [src-raw:cccc3333] 又一条", 0.5)]
    out = _filter_srcraw_candidates(cands)
    assert len(out) == 1, out
    assert out[0][0] == "mem-aaaa1111", out


def test_srcraw_empty_candidates():
    assert _filter_srcraw_candidates([]) == []


def test_srcraw_all_normal_kept():
    cands = [("mem-dddd4444", "纯 normal [src:dddd4444]", 0.8)]
    assert len(_filter_srcraw_candidates(cands)) == 1


def test_srcraw_content_none_safe():
    # 内容为 None 不炸（c[1] or ""）
    cands = [("mem-eeee5555", None, 0.3)]
    assert len(_filter_srcraw_candidates(cands)) == 1, "None content 应保留（无标记）"
