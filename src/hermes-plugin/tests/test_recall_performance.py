"""Behavior and cache-invalidation coverage for resident recall optimization."""
import concurrent.futures
import json
import math
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from memory import noise_filter, tuning
from memory.context_builder import ContextBuilder
from memory.embedding_provider import OllamaEmbedding
from memory.hybrid_retriever import HybridRetriever, MemoryDoc, SimpleEmbedder


class EmbeddingClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.clients = []
        self.fail_text = None
        real_client = httpx.Client

        def handle(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if self.fail_text in body['input']:
                return httpx.Response(500, json={'error': 'test failure'})
            return httpx.Response(200, json={'embeddings': [[1.0, 0.0] for _ in body['input']]})

        def factory(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertIsNot(kwargs.get('verify'), False)
            client = real_client(transport=httpx.MockTransport(handle), **kwargs)
            self.clients.append(client)
            return client

        self.factory = patch('httpx.Client', side_effect=factory).start()
        self.addCleanup(patch.stopall)
        self.provider = OllamaEmbedding()
        self.addCleanup(self.provider.close)

    def test_reuses_transport_and_releases_it(self):
        self.assertEqual(self.provider.embed_single('first'), [1.0, 0.0])
        self.assertEqual(self.provider.embed_single('second'), [1.0, 0.0])
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(all(r['keep_alive'] == '30m' for r in self.requests))
        self.provider.close()
        self.assertTrue(self.clients[0].is_closed)
        self.provider.close()
        self.assertEqual(self.provider.embed_single('after close'), [1.0, 0.0])
        self.assertEqual(len(self.clients), 2)

    def test_concurrent_initialization_shares_one_client(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.provider.embed_single, map(str, range(12))))
        self.assertEqual(results, [[1.0, 0.0]] * 12)
        self.assertEqual(len(self.clients), 1)

    def test_failed_batch_keeps_zero_fill_and_can_recover(self):
        self.provider.embed_single('seed dimension')
        self.fail_text = 'fail'
        self.assertEqual(self.provider.embed_single('fail'), [0.0, 0.0])
        self.assertEqual(self.provider.embed_single('recovered'), [1.0, 0.0])
        self.assertEqual(len(self.clients), 1)

    def test_dimension_probe_reuses_client_after_initial_failure(self):
        self.fail_text = 'fail'
        self.assertEqual(self.provider.embed_single('fail'), [0.0, 0.0])
        self.assertEqual(self.requests[-1]['input'], ['dimension probe'])
        self.assertEqual(len(self.clients), 1)

    def test_empty_input_does_not_initialize_transport(self):
        self.assertEqual(self.provider.embed([]), [])
        self.assertEqual(self.clients, [])

    def test_builder_closes_embedding_even_when_database_close_fails(self):
        builder = ContextBuilder.__new__(ContextBuilder)
        builder._vector_store = Mock()
        builder._vector_store.close.side_effect = RuntimeError('close failed')
        builder._embedding = self.provider
        self.provider.embed_single('ready')
        with self.assertRaises(RuntimeError):
            builder.close()
        self.assertTrue(self.clients[0].is_closed)


class ResidentSearchTests(unittest.TestCase):
    def setUp(self):
        self.config = {'noise_filter': {'enabled': True, 'prefix_patterns': ['NOISE:']}}
        self.tuning_patch = patch.object(tuning, '_cache', self.config)
        self.tuning_patch.start()
        self.addCleanup(self.tuning_patch.stop)
        self.now = 2000000000.0

    def doc(self, key, content='useful content', **kwargs):
        return MemoryDoc(id=key, content=content, timestamp=self.now,
                         importance=0.5, namespace='owner', visibility='shared', **kwargs)

    def retriever(self, docs=None, vectors=None, mode='off'):
        retriever = HybridRetriever(fts5_mode=mode, db_path='missing-test-store.db')
        retriever.index(docs or [self.doc('a'), self.doc('b')],
                        embeddings=vectors or [[1.0, 0.0], [0.0, 2.0]])
        return retriever

    def test_cached_cosine_matches_original_numerics(self):
        rng = random.Random(721)
        retriever = self.retriever()
        for size in (1, 8, 128, 1024):
            for _ in range(8):
                a = [rng.uniform(-1, 1) for _ in range(size)]
                b = [rng.uniform(-1, 1) for _ in range(size)]
                self.assertEqual(retriever._cosine(a, b), SimpleEmbedder.cosine(a, b))
        self.assertEqual(retriever._cosine([], []), 0.0)
        self.assertEqual(retriever._cosine([1.0], [1.0, 0.0]), 0.0)
        self.assertEqual(retriever._cosine([0.0], [1.0]), 0.0)
        self.assertEqual(retriever._cosine([math.nan], [1.0]), 0.0)

    def test_document_and_query_norms_are_reused(self):
        retriever = self.retriever()
        query = [1.0, 1.0]
        with patch.object(retriever, '_compute_vector_norm', wraps=retriever._compute_vector_norm) as compute:
            for _ in range(3):
                for vector in retriever._embeddings:
                    retriever._cosine(query, vector)
            self.assertEqual(compute.call_count, 1)
        for i in range(20):
            retriever._cosine([float(i), 1.0], retriever._embeddings[0])
        self.assertLessEqual(len(retriever._query_norms), 2)

    def test_patch_and_remove_invalidate_derived_data(self):
        retriever = self.retriever()
        old_vector = retriever._embeddings[0]
        retriever.patch(self.doc('a', 'NOISE: new text'), embedding=[0.0, 4.0])
        new_vector = retriever._embeddings[-1]
        self.assertEqual(retriever._cosine([0.0, 1.0], new_vector), 1.0)
        self.assertNotIn(id(old_vector), retriever._vector_norms)
        self.assertTrue(retriever._noise_cache['a'][1][0])
        retriever.remove('a')
        self.assertNotIn('a', retriever._noise_cache)
        self.assertNotIn(id(new_vector), retriever._vector_norms)
        hits = retriever.search('new text', now=self.now, viewer='owner', query_vec=[0.0, 1.0])
        self.assertNotIn('a', [d.id for d, _ in hits])

    def test_mutated_query_vector_recomputes_its_norm(self):
        retriever = self.retriever()
        query = [1.0, 0.0]
        self.assertEqual(retriever._cosine(query, retriever._embeddings[0]), 1.0)
        query[:] = [0.0, 4.0]
        self.assertEqual(retriever._cosine(query, retriever._embeddings[1]), 1.0)

    def test_custom_embedding_cosine_is_preserved(self):
        retriever = self.retriever()
        retriever.embedding_provider = Mock()
        retriever.embedding_provider.cosine.return_value = 0.125
        self.assertEqual(retriever._cosine([1.0], [1.0]), 0.125)

    def test_reindex_discards_old_caches(self):
        retriever = self.retriever()
        old_vector = retriever._embeddings[0]
        retriever.index([self.doc('c')], embeddings=[[2.0, 2.0]])
        self.assertNotIn(id(old_vector), retriever._vector_norms)
        self.assertEqual(set(retriever._noise_cache), {'c'})

    def test_noise_results_are_reused_and_config_changes_invalidate(self):
        with patch.object(noise_filter, 'is_noise_text', wraps=noise_filter.is_noise_text) as classify:
            retriever = self.retriever([self.doc('a', 'NOISE: text'), self.doc('b')])
            calls_after_hydrate = classify.call_count
            retriever.search('content', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
            retriever.search('different', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
            self.assertEqual(classify.call_count, calls_after_hydrate)
            self.config['noise_filter']['prefix_patterns'] = ['OTHER:']
            retriever.search('content', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
            self.assertGreater(classify.call_count, calls_after_hydrate)
            self.assertFalse(retriever._noise_cache['a'][1][0])

    def test_in_place_text_change_does_not_reuse_stale_noise_verdict(self):
        retriever = self.retriever()
        retriever.docs[0].content = 'NOISE: changed'
        retriever.search('content', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
        self.assertTrue(retriever._noise_cache['a'][1][0])

    def test_fts_on_avoids_memory_scan_but_off_and_shadow_keep_it(self):
        for mode in ('off', 'shadow', 'on'):
            with self.subTest(mode=mode):
                retriever = self.retriever(mode=mode)
                with patch.object(retriever.bm25, 'search', wraps=retriever.bm25.search) as memory, \
                     patch.object(retriever, '_fts_bm25', return_value=[(0, 1.0)]) as fts:
                    retriever.search('content', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
                    self.assertEqual(memory.call_count, 0 if mode == 'on' else 1)
                    self.assertEqual(fts.call_count, 0 if mode == 'off' else 1)

    def test_fts_failure_preserves_vector_only_degradation(self):
        retriever = self.retriever(mode='on')
        connection = Mock()
        with patch.object(retriever, '_fts_db', return_value=connection), \
             patch('memory.hybrid_retriever.fts_search_bm25', side_effect=RuntimeError('FTS failed')), \
             patch.object(retriever, '_write_fts_fallback_event'):
            hits = retriever.search('content', now=self.now, viewer='owner', query_vec=[1.0, 1.0])
        self.assertTrue(hits)
        self.assertEqual(retriever._fts_fallback_count, 1)
        connection.close.assert_called_once()

    def test_private_memory_stays_filtered(self):
        secret = self.doc('private')
        secret.visibility = 'private'
        retriever = self.retriever([secret, self.doc('shared')])
        hits = retriever.search('content', now=self.now, viewer='other', query_vec=[1.0, 1.0])
        self.assertNotIn('private', [d.id for d, _ in hits])
        self.assertIn('shared', [d.id for d, _ in hits])


if __name__ == '__main__':
    unittest.main(verbosity=2)
