"""R01-FIX1 四组返修回归（2026-10-21 小D）

每条用例都对应独立验收报告《验收结论-20261021.md》里一个已复现的失败，
注释写「防的是哪条实测」，删用例=把那个缺陷放回生产。

边界：本文件只覆盖 F1–F4 的【直接】反例与必要正常对照；全量/双向/30轮不在施工阶段跑。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "src" / "hermes-plugin"
sys.path.insert(0, str(PLUGIN))

from memory.vector_store import VectorStore, Chunk, fts_search_bm25
from memory.hybrid_retriever import HybridRetriever, MemoryDoc
from memory import evidence_admission as EA


class F1FreshLibraryTests(unittest.TestCase):
    """F1：刚建好、从未记过使用账的库，第一次正常召回必须能跑。

    防的反例：get_utility 直接 SELECT recalled_n 等列，而这些列只由 increment_utility
    按需 ALTER 补 —— 新库第一条查询就是 OperationalError: no such column: recalled_n。
    验收原话：**不能要求用户先 expose 一次来「修库」**。
    """

    def test_get_utility_on_brand_new_library(self):
        tmp = tempfile.mkdtemp(prefix='fix1_f1_')
        vs = VectorStore(db_path=os.path.join(tmp, 'v.db'))
        try:
            vs.store_chunk(Chunk(id='mem-a0000001', content='书房 C03 裁决结论',
                                 timestamp=time.time(), namespace='zcode',
                                 visibility='shared', tier='refined', importance=0.5))
            cols = [r[1] for r in vs._get_conn().execute('PRAGMA table_info(chunk_utility)')]
            self.assertNotIn('recalled_n', cols, '前提：新库确实没有计数列')
            got = vs.get_utility('mem-a0000001', viewer='zcode')
            # 无账 = 正常空统计，而不是抛异常（也绝不能返回 None 混淆「查不到」）
            self.assertIsNone(got, '没有账的行应返回 None（区别于报错）')
        finally:
            vs.close()

    def test_recall_after_store_without_prior_account(self):
        """官方 store_chunk → 立刻 get_utility（模拟 CLI 首次召回读账）。"""
        tmp = tempfile.mkdtemp(prefix='fix1_f1b_')
        vs = VectorStore(db_path=os.path.join(tmp, 'v.db'))
        try:
            vs.store_chunk(Chunk(id='mem-a0000002', content='书房 C03 裁决结论',
                                 timestamp=time.time(), namespace='zcode',
                                 visibility='shared', tier='refined', importance=0.5))
            # 关键：不调用 increment_utility，直接读账（这正是验收的复现路径）
            vs.get_utility('mem-a0000002', viewer='zcode')
            vs.increment_utility('mem-a0000002', viewer='zcode', state='cited')
            got = vs.get_utility('mem-a0000002', viewer='zcode')
            self.assertEqual(got['cited_n'], 1)
            self.assertAlmostEqual(got['utility_score'], 0.5, places=6)
        finally:
            vs.close()


class F2ScopeBeforeLimitTests(unittest.TestCase):
    """F2：namespace/project/ACL 都在**候选限额之前**生效。"""

    def test_readable_answer_survives_130_private_prefix(self):
        """防的反例：130 条他域 private 排在前面，top_k=1 时最多扫 100 行 ⇒ 返回 []。

        修法是「翻页直到攒够可读结果」，不是「把上限调大」——上限调大只是把
        bug 推远，超过新上限照样复现。
        """
        tmp = tempfile.mkdtemp(prefix='fix1_f2a_')
        vs = VectorStore(db_path=os.path.join(tmp, 'v.db'))
        try:
            now = time.time()
            for i in range(130):
                vs.store_chunk(Chunk(id='mem-%08x' % (0x30000000 + i), content='unique731920',
                                     timestamp=now, namespace='dsh', visibility='private',
                                     importance=0.5))
            vs.store_chunk(Chunk(id='mem-40000001', content='unique731920 ' + ('正常内容 ' * 80),
                                 timestamp=now, namespace='zcode', visibility='shared',
                                 importance=0.5))
            hits = fts_search_bm25(vs._get_conn(), 'unique731920', top_k=1, viewer='zcode')
            self.assertIn('mem-40000001', [h[0] for h in hits])
        finally:
            vs.close()

    def test_project_filter_applies_before_top_k(self):
        """防的反例：--project right 时主路筛掉 wrong，补召又把 wrong 塞回来。"""
        tmp = tempfile.mkdtemp(prefix='fix1_f2b_')
        r = HybridRetriever(db_path=os.path.join(tmp, 'v.db'), fts5_mode='off', acl_mode='memory')
        r.index([
            MemoryDoc(id='mem-60000001', content='ABC998877 项目部署进展：服务已经启动，版本核对完成。',
                      timestamp=time.time(), namespace='zcode', visibility='shared',
                      state='active', importance=0.5, tier='refined', project='wrong'),
        ])
        got = [d.id for d, _ in r.search('ABC998877 项目部署进展', top_k=4, viewer='zcode',
                                         project_filter='right')]
        self.assertEqual(got, [], 'project=wrong 的记录不能在 --project right 下返回')


class F3UsefulTimeTests(unittest.TestCase):
    """F3：展示不能给「真实有用时间」续命。

    防的反例：昨天 cited、今天只 exposed，last_access_useful 却从昨天跳到今天。
    """

    def test_exposed_does_not_touch_useful_time(self):
        tmp = tempfile.mkdtemp(prefix='fix1_f3_')
        vs = VectorStore(db_path=os.path.join(tmp, 'v.db'))
        try:
            conn = vs._get_conn()
            conn.execute('INSERT INTO chunks(id,content,namespace,visibility,state,importance,timestamp)'
                         ' VALUES(?,?,?,?,?,?,?)',
                         ('mem-10000001', '历史引用的正常记录', 'zcode', 'shared', 'active', 0.5, time.time()))
            conn.commit()
            vs.increment_utility('mem-10000001', viewer='zcode', state='cited')
            first = vs.get_utility('mem-10000001', viewer='zcode')['last_access_useful']
            self.assertIsNotNone(first)
            time.sleep(0.01)
            vs.increment_utility('mem-10000001', viewer='zcode', state='exposed')
            later = vs.get_utility('mem-10000001', viewer='zcode')
            self.assertEqual(later['exposed_n'], 1, '展示仍要如实记账')
            self.assertEqual(later['last_access_useful'], first, '展示不得刷新有用时间')
            self.assertGreater(later['last_access'], first, '原始访问时间照常更新（可审计）')
        finally:
            vs.close()

    def test_legacy_row_has_unknown_useful_time(self):
        """旧账没有真实引用时间 ⇒ 明确未知（None），不拿 last_access 补造。"""
        tmp = tempfile.mkdtemp(prefix='fix1_f3b_')
        vs = VectorStore(db_path=os.path.join(tmp, 'v.db'))
        try:
            vs.increment_utility('mem-10000002', viewer='zcode', state='exposed')
            got = vs.get_utility('mem-10000002', viewer='zcode')
            self.assertIsNone(got['last_access_useful'])
            self.assertIsNotNone(got['last_access_raw'])
        finally:
            vs.close()


class F4UnifiedAdmissionTests(unittest.TestCase):
    """F4：主路/补召/关联扩展/最终出口共用同一份准入判据。"""

    def test_strict_identifier_cannot_be_replaced_by_generic_overlap(self):
        """防的反例：库里只有 ABC998877，问 ZXQ731920 项目部署进展 仍返回 ABC。"""
        v = EA.assess('ZXQ731920 项目部署进展',
                      'ABC998877 项目部署进展：服务已经启动，版本核对完成。',
                      raw_bm=1.0, raw_vec=0.6)
        self.assertFalse(v['admitted'])
        self.assertEqual(v['reason'], 'identifier-mismatch')

    def test_generic_words_alone_are_not_evidence(self):
        """通用词（项目/部署/进展）不能单独替代主题。"""
        terms = [t.lower() for t in EA.content_terms('ZXQ731920 项目部署进展')]
        self.assertNotIn('项目', terms)
        self.assertNotIn('部署', terms)
        self.assertNotIn('进展', terms)

    def test_real_answer_with_identifier_is_admitted(self):
        """必要正常对照：带标识符的真答案必须照常准入（别把闸关死）。"""
        v = EA.assess('书房 C03 数字 NULL 裁决进展',
                      '书房 C03 裁决结论：数字 NULL 保留原值，不补默认值。',
                      raw_bm=0.9, raw_vec=0.8, importance=0.5)
        self.assertTrue(v['admitted'])
        self.assertIn('C03', v['id_hits'])
        self.assertTrue(v['basis'])

    def test_backfill_and_main_share_the_same_verdict(self):
        """同一段正文，补召（无向量分）与主路（有向量分）都用同一个函数判。"""
        content = '书房 C03 裁决结论：数字 NULL 保留原值'
        main_v = EA.assess('书房 C03 数字 NULL 裁决进展', content, raw_bm=0.9, raw_vec=0.8)
        bk_v = EA.assess('书房 C03 数字 NULL 裁决进展', content, raw_bm=1.0, raw_vec=0.0)
        self.assertTrue(main_v['admitted'])
        self.assertTrue(bk_v['admitted'])
        self.assertEqual(main_v['id_hits'], bk_v['id_hits'])


if __name__ == '__main__':
    unittest.main()
