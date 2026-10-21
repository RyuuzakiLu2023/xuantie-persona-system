"""Behavioral contracts for deadline, profile scoping and memory delivery."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get('HERMES_REPO', r'<XT_ROOT>\hermes-agent-main-v2\hermes-agent-main'))
sys.path[:0] = [str(PLUGIN), str(REPO)]

from provider import XtmemMemoryProvider, get_last_recalled_memories
from recall_policy import RecallPolicy
from memory.hybrid_retriever import HybridRetriever, MemoryDoc
from agent.memory_manager import MemoryManager
from agent.memory_provider import is_trivial_prompt
from agent.turn_context import compose_user_api_content


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'HERMES_HOME': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db_path = str(Path(self.temp.name) / 'memory.db')
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE chunks (id TEXT PRIMARY KEY, content TEXT, namespace TEXT, project TEXT, visibility TEXT, state TEXT, importance REAL, timestamp REAL, tags TEXT DEFAULT "[]", updated_at REAL DEFAULT 0)')
        self.addCleanup(self.conn.close)
        self.events = []
        self.results = []
        self.p = XtmemMemoryProvider()
        self.p._hermes_home = self.temp.name
        self.p._session_id = 'session-a'
        self.p._namespace = 'personal'
        self.p._plugin_cfg = {'db_path': self.db_path, 'prefetch_wait_secs': 6, 'recall_diagnostics': True,
                              'read_tools': True, 'include_shared_knowledge': True,
                              'recall_policy': {'substantive_short_queries': True}}
        self.p._configure_recall()
        self.p._prefetch_min_score = .5
        self.p._record_event_force = lambda *a, **kw: self.events.append((a, kw))
        self.p._write_probe_event = lambda *a, **kw: None
        self.retriever = SimpleNamespace(search_two_tier=self.search, search=self.search,
                                        _last_two_tier_mode='refined-first', _lock=threading.RLock(),
                                        _est=lambda s: len(s),)
        self.p._builder = SimpleNamespace(
            _retriever=self.retriever, _cfg={'retrieval_token_budget': 2000},
            _vector_store=SimpleNamespace(_get_conn=lambda: self.conn, get_utility=lambda *a, **kw: {}))
        self.p._builder_ready = True
        self.addCleanup(self.join_worker)

    def join_worker(self):
        thread = self.p._prefetch_thread
        if thread:
            thread.join(3)
        exposure = self.p._exposure_thread
        if exposure:
            exposure.join(3)

    def search(self, *args, **kwargs):
        return self.results

    def doc(self, num, ns='personal', vis='shared', state='active', content='useful memory', score=.9):
        mid = f'mem-{num:08x}'
        self.conn.execute('INSERT INTO chunks (id,content,namespace,project,visibility,state,importance,timestamp) VALUES (?,?,?,?,?,?,?,?)',
                          (mid, content, ns, '', vis, state, .5, 1.0))
        self.conn.commit()
        doc = MemoryDoc(id=mid, content=content, timestamp=time.time(), namespace=ns, visibility=vis, state=state)
        self.results.append((doc, score))
        return mid

    def test_short_content_passes_all_three_gates_and_confirmations_stay_quiet(self):
        from memory.bundle_selector import is_chitchat
        for q in ['豆包搜索', '豆包搜索怎么用', '怎么配', '不明白', '好，豆包怎么用', 'k8s', 'hi file']:
            with self.subTest(q=q):
                self.assertFalse(is_trivial_prompt(q))
                self.assertTrue(self.p._recall_policy.should_recall(q))
                self.assertFalse(is_chitchat(q))
        for q in ['好', '好！', '收到', '收到。', '晚安', '谢谢', 'ok!', '嗯', '继续', '？？', '']:
            with self.subTest(q=q):
                self.p._recall_policy.should_recall('豆包搜索')
                self.assertFalse(self.p._recall_policy.should_recall(q))

    def test_legacy_profile_keeps_short_query_behavior(self):
        for q in ['豆包搜索', '豆包搜索怎么用']:
            self.assertFalse(RecallPolicy({}).should_recall(q))
        other = XtmemMemoryProvider()
        self.assertEqual(other.get_tool_schemas(), [])
        self.assertEqual(other._prefetch_wait_secs, 2)
        self.assertFalse(other._include_shared_knowledge)

    def test_deadline_config_is_bounded_and_survives_session_switch(self):
        for value in [8, -1, 0, 'broken', float('nan'), float('inf')]:
            self.p._plugin_cfg['prefetch_wait_secs'] = value
            self.p._configure_recall()
            self.assertEqual(self.p._prefetch_wait_secs, 2)
        self.p._plugin_cfg['prefetch_wait_secs'] = 6
        self.p.on_session_switch('next', reset=True)
        self.assertEqual(self.p._prefetch_wait_secs, 6)
        self.assertTrue(self.p._recall_policy.should_recall('豆包搜索'))

    def test_delivered_card_count_and_api_sidecar_match(self):
        mid = self.doc(1, ns='knowledge')
        manager = MemoryManager()
        manager.add_provider(self.p)
        manager.on_turn_start(1, '水化')
        text = manager.prefetch_all('水化')
        api = compose_user_api_content('水化', text, [])
        self.assertIn('[m:' + mid + ']', api)
        self.assertIn('recalled 1 memory', manager.describe_recall())
        self.assertEqual(self.p.recall_status().count, 1)
        self.assertEqual(len(self.p.recall_diagnostics()['memories']), 1)
        self.join_worker()
        self.assertEqual(len(self.events), 1)
        self.assertIn(mid, self.p._recall_cooldown)

    def test_timeout_and_late_completion_never_claim_injection(self):
        release, entered = threading.Event(), threading.Event()
        mid = self.doc(2)
        def slow(*a, **kw):
            entered.set()
            release.wait(3)
            return self.results
        self.retriever.search_two_tier = slow
        self.p._prefetch_wait_secs = .03
        try:
            text = self.p.prefetch('豆包搜索')
            self.assertTrue(entered.wait(2))
            self.assertIn('检索未完成', text)
            self.assertIn('xtmem_recall', text)
            self.assertNotIn(mid, text)
            self.assertEqual(self.p.recall_status().count, 0)
        finally:
            release.set()
            self.join_worker()
        self.assertEqual(self.p.recall_diagnostics()['state'], 'timeout')
        self.assertEqual(self.events, [])
        self.assertEqual(self.p._recall_cooldown, {})
        self.p._prefetch_wait_secs = 6
        self.retriever.search_two_tier = self.search
        self.assertIn(mid, self.p.prefetch('豆包搜索'))

    def test_session_switch_invalidates_inflight_identical_query(self):
        release, entered = threading.Event(), threading.Event()
        self.doc(3)
        def slow(*a, **kw):
            entered.set()
            release.wait(3)
            return self.results
        self.retriever.search_two_tier = slow
        output = []
        turn = threading.Thread(target=lambda: output.append(self.p.prefetch('豆包搜索', session_id='session-a')))
        turn.start()
        try:
            self.assertTrue(entered.wait(2))
            self.p.on_session_switch('session-b')
        finally:
            release.set()
            turn.join(3)
        self.assertEqual(output, [''])
        self.assertEqual(self.p.recall_diagnostics()['count'], 0)
        self.assertEqual(self.events, [])

    def test_trivial_turn_resets_previous_count_even_when_core_skips_prefetch(self):
        self.doc(4)
        self.p.prefetch('豆包搜索')
        self.p.on_turn_start(2, 'ok')
        self.assertIsNone(self.p.recall_status())
        self.assertEqual(self.p.recall_diagnostics()['memories'], [])

    def test_empty_low_score_and_failure_have_distinct_honest_status(self):
        manager = MemoryManager()
        manager.add_provider(self.p)
        self.p.prefetch('不存在的主题')
        self.assertEqual(self.p.recall_diagnostics()['state'], 'no_match')
        self.assertIn('未命中', manager.describe_recall())
        self.doc(5, score=.49)
        self.p.prefetch('豆包搜索')
        self.assertEqual(self.p.recall_diagnostics()['state'], 'low_score')
        self.assertNotIn('recalled relevant memory', manager.describe_recall())
        self.retriever.search_two_tier = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('test error'))
        self.p.prefetch('豆包搜索')
        self.assertEqual(self.p.recall_diagnostics()['state'], 'error')
        self.assertEqual(self.events, [])

    def test_shared_knowledge_is_allowed_but_other_private_or_archived_is_not(self):
        good = self.doc(6, ns='knowledge')
        permitted = self.doc(7, ns='knowledge', vis='shared-with:personal')
        bad = [self.doc(8, ns='knowledge', vis='private'),
               self.doc(9, ns='other', vis='private'), self.doc(10, ns='other'),
               self.doc(11, ns='knowledge', state='archived'),
               self.doc(12, ns='knowledge', vis='shared-with:other')]
        text = self.p.prefetch('知识库')
        self.assertIn(good, text)
        self.assertIn(permitted, text)
        for mid in bad:
            self.assertNotIn(mid, text)
        self.assertEqual(self.p.recall_status().count, 2)

    def test_database_visibility_recheck_catches_stale_index_permissions(self):
        mid = self.doc(13, ns='knowledge')
        self.conn.execute("UPDATE chunks SET visibility='private' WHERE id=?", (mid,))
        self.conn.commit()
        self.assertNotIn(mid, self.p.prefetch('知识库'))
        for tool, args in [('xtmem_recall', {'query':'useful'}), ('xtmem_lookup', {'id':mid})]:
            result = json.loads(self.p.handle_tool_call(tool, args))
            self.assertEqual(result['count'], 0)

    def test_fixed_knowledge_bulletin_also_respects_visibility(self):
        shared = self.doc(20, ns='knowledge', content='public bulletin')
        private = self.doc(21, ns='knowledge', vis='private', content='private bulletin')
        self.conn.execute('UPDATE chunks SET tags=?, updated_at=1 WHERE id=?', ('["能力总账"]', shared))
        self.conn.execute('UPDATE chunks SET tags=?, updated_at=2 WHERE id=?', ('["能力总账"]', private))
        self.conn.commit()
        text = self.p._knowledge_block()
        self.assertIn('public bulletin', text)
        self.assertNotIn('private bulletin', text)

    def test_read_tools_cannot_impersonate_and_do_not_write(self):
        public = self.doc(14, ns='knowledge')
        private = self.doc(15, ns='other', vis='private')
        own = self.doc(16, vis='private')
        before = self.conn.total_changes
        for mid, count in [(public, 1), (private, 0), (own, 1)]:
            result = json.loads(self.p.handle_tool_call('xtmem_lookup', {'id':mid}))
            self.assertEqual(result['count'], count)
        for forbidden in ['namespace', 'viewer', 'as', 'db_path', 'agent_identity']:
            result = json.loads(self.p.handle_tool_call('xtmem_lookup', {'id':private, forbidden:'other'}))
            self.assertFalse(result['ok'])
        result = json.loads(self.p.handle_tool_call('xtmem_lookup', {'query':'useful', 'cross':False}))
        self.assertEqual([r['id'] for r in result['items']], [own])
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.p._last_prefetch_count, 0)

    def test_provider_tools_registered_before_initialize(self):
        Path(self.temp.name, 'xtmem.yaml').write_text('read_tools: true\nprefetch_wait_secs: 6\n', encoding='utf-8')
        p = XtmemMemoryProvider()
        manager = MemoryManager()
        manager.add_provider(p)
        self.assertTrue(manager.has_tool('xtmem_recall'))
        self.assertTrue(manager.has_tool('xtmem_lookup'))
        with patch.object(p, '_warmup_safe'):
            manager.initialize_all('isolated', hermes_home=self.temp.name, agent_identity='tester')
            p._warmup_thread.join(2)
        self.assertEqual(p._namespace, 'tester')
        self.assertEqual(p._prefetch_wait_secs, 6)

    def test_namespace_filter_happens_before_top_k(self):
        docs = [MemoryDoc(id=str(n), content='water hydration shelf', timestamp=time.time(), namespace='other', importance=.9)
                for n in range(10)]
        docs.append(MemoryDoc(id='knowledge', content='water hydration shelf', timestamp=time.time(), namespace='knowledge', importance=.5))
        retriever = HybridRetriever(fts5_mode='off', db_path='unused-test.db')
        retriever.index(docs, embeddings=[[1., 0.]] * len(docs))
        results = retriever.search('water hydration shelf', query_vec=[1., 0.], top_k=1,
                                   viewer='personal', namespace_filter=('personal', 'knowledge'))
        self.assertEqual([d.id for d, _ in results], ['knowledge'])

    def test_six_second_budget_delivers_query_that_exceeds_old_two_seconds(self):
        mid = self.doc(17)
        original = self.search
        def delayed(*args, **kwargs):
            threading.Event().wait(2.1)
            return original(*args, **kwargs)
        self.retriever.search_two_tier = delayed
        self.assertIn(mid, self.p.prefetch('豆包搜索'))
        self.assertEqual(self.p.recall_status().count, 1)

    def test_nested_citations_are_not_counted_as_delivered_memories(self):
        mid = self.doc(18, content='historical note references [m:mem-0000aaaa]')
        self.p.prefetch('豆包搜索')
        self.join_worker()
        self.assertEqual(set(self.p._recall_cooldown), {mid})
        self.assertEqual([kw['target_chunk'] for _, kw in self.events], [mid])

    def test_slow_statistics_cannot_block_delivery(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        mid = self.doc(19)
        def slow_stats(*a, **kw):
            entered.set()
            release.wait(3)
        self.p._record_event_force = slow_stats
        result = []
        def call():
            result.append(self.p.prefetch('豆包搜索'))
            finished.set()
        thread = threading.Thread(target=call)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(finished.wait(2))
            self.assertIn(mid, result[0])
        finally:
            release.set()
            thread.join(3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
