# -*- coding: utf-8 -*-
"""test_p78_feedback_detector.py - P7.8(3) 五态用户反馈检测独立误报率测试合同。"""
import sys, re, pytest
from conftest import PLUGIN_DIR
sys.path.insert(0, str(PLUGIN_DIR))
from provider import XtmemMemoryProvider


class DummyProvider(XtmemMemoryProvider):
    def __init__(self, recent_exposed=None):
        self._recent_exposed = list(recent_exposed or ['mem-11111111', 'mem-22222222'])
        self._events = []
        self._namespace = 'dsh'
        self._db_path = ''

    def _record_event_force(self, kind, *, target_chunk='', meta=None, **kwargs):
        self._events.append({'kind': kind, 'target_chunk': target_chunk, 'meta': meta})

    def _overturn_bump(self, cid):
        pass

    def _resolve_corrected_targets(self, text, exposed_ids):
        mids = re.findall(r'\[m:(mem-[0-9a-f]{8})\]', text)
        if mids:
            return [m for m in mids if m in exposed_ids]
        return [exposed_ids[0]] if exposed_ids else []


# 样本集：普通否定/讨论句式（不得误报） vs 纠正记忆（必须触发）
BOUNDARY_SAMPLES = [
    ('不是吧，这也行？', False, '排除项：不是吧'),
    ('万一弄错了怎么办', False, '排除项：万一'),
    ('你说得对，但是还有个问题', False, '排除项：但是'),
    ('虽然搞反了先后顺序，不过影响不大', False, '排除项：不过'),
    ('然而事实并非如此', False, '排除项：然而'),
    ('这样改对吗？', False, '排除项：对吗'),
    ('是不是搞错了参数', False, '排除项：是不是'),
    ('这个方案对吧', False, '排除项：对吧'),
    ('你确定对么', False, '排除项：对么'),
    ('是不是对的呢', False, '排除项：是不是对'),
    ('这不是我们要的结果，需要重新构思方案，不能按之前的逻辑来处理问题', False, '长句超60字不判'),
    ('可能有点不对劲，但还可以用', False, '排除项：但'),

    ('不对 [m:mem-11111111]', True, '明确否定'),
    ('记错了，那条是旧版本', True, '记错了强纠正'),
    ('弄错了', True, '裸否定弄错了'),
    ('搞反了', True, '裸否定搞反了'),
    ('完全正确', True, '强肯定完全正确'),
    ('没错', True, '强肯定没错'),
    ('对的', True, '强肯定对的'),
    ('正是这个', True, '强肯定正是这个'),
    ('如你所写', True, '强肯定如你所写'),
    ('纠正一下：端口是8080', True, '纠正模板'),
    ('别这么记', True, '别这么记强否定'),
    ('按这个来', True, '按这个来强肯定'),
]


def test_boundary_dataset_size():
    assert len(BOUNDARY_SAMPLES) >= 20, '样本集必须 >= 20 条'


def test_false_positive_rate():
    dp = DummyProvider()
    neg_samples = [s for s in BOUNDARY_SAMPLES if not s[1]]
    assert len(neg_samples) >= 10, '负样本至少要有10条'

    fps = []
    for text, expected, desc in neg_samples:
        dp._events.clear()
        dp._classify_turn_signals(text)
        if bool(dp._events) != expected:
            fps.append((text, desc, list(dp._events)))

    fp_rate = len(fps) / len(neg_samples)
    print('\n负样本总数: %d, 误报数: %d, 误报率: %.1f%%' % (len(neg_samples), len(fps), fp_rate * 100))
    if fps:
        for t, d, evs in fps:
            print('  误报项: "%s" (%s)' % (t, d))

    assert fp_rate < 0.10, '误报率必须 < 10%%，实测为 %.1f%%' % (fp_rate * 100)


def test_true_positive_rate():
    dp = DummyProvider()
    pos_samples = [s for s in BOUNDARY_SAMPLES if s[1]]
    missed = []
    for text, expected, desc in pos_samples:
        dp._events.clear()
        dp._classify_turn_signals(text)
        if bool(dp._events) != expected:
            missed.append((text, desc))

    recall = (len(pos_samples) - len(missed)) / len(pos_samples)
    print('\n正样本总数: %d, 漏报数: %d, 召回率: %.1f%%' % (len(pos_samples), len(missed), recall * 100))
    assert recall >= 0.85, '召回率必须 >= 85%%，实测为 %.1f%%' % (recall * 100)