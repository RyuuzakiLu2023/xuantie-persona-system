# -*- coding: utf-8 -*-
"""R01 · 召回质量收口 —— 固定正反例（RQ01–RQ08 可自动化的部分）

这是什么：把 2026-10-21 核查报告里已证实的六组根因，逐条钉成"先红后绿"的回归用例。
为什么要有：核查报告第 §二 条的原话是"非空率能很高、内容却不相关"。
    所以本文件【一律断言最终 ID 集合】，不数非空、不看条数。
    任何一条被改回去，这里立刻变红。

边界（未验项如实写在这，别假装都覆盖了）：
    · RQ09（分路径真实耗时/加载版本）不在此文件——它要真跑 hook 新进程与常驻内核，
      属验收阶段的实测，本端施工只做静态 hash 记录（见交付回执）。
    · RQ10（原 T09/T14/T15 与权限恢复断言）沿用既有测试文件，不在本文件重复。
    · 全部用例跑在【合成夹具 + 隔离库】上，acl_mode='memory' 或独立临时库，绝不碰生产。

⚠️ 改这些用例前先读 R01 回执：每条用例的 docstring 记着"它防的是哪条实测反例"，
   删掉它就等于把那个反例放回生产。
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

from memory.bundle_selector import extract_anchors, _is_generic_anchor
from memory.hybrid_retriever import HybridRetriever, MemoryDoc, _query_content_terms


def _doc(num, content, ns="dsh", imp=0.5, ts=None, tier="raw", track="default"):
    return MemoryDoc(id="mem-%08x" % num, content=content,
                     timestamp=(ts if ts is not None else time.time() - 3600),
                     namespace=ns, visibility="shared", state="active",
                     importance=imp, tier=tier, track=track)


class R01AnchorRarityTests(unittest.TestCase):
    """RQ04 上半：通用词不得当锚点（补召侧门的入口）。"""

    def test_null_is_not_an_anchor(self):
        """防的反例：问「书房 C03 数字 NULL 裁决进展」，补召回 4 条只含 null 的旧记录。"""
        anchors = [a.lower() for a in extract_anchors("书房 C03 数字 NULL 裁决进展")]
        self.assertNotIn("null", anchors)
        self.assertIn("c03", anchors)

    def test_sql_and_language_keywords_are_not_anchors(self):
        for q in ("select from where", "def class import", "true false none"):
            for a in extract_anchors(q):
                self.assertTrue(_is_generic_anchor(a), "通用词被当成锚点: %r (query=%r)" % (a, q))

    def test_real_identifiers_survive(self):
        """反向保护（RQ01）：真标识符不能被稀有性闸误杀。"""
        anchors = extract_anchors("lowres_scale 和 ZXQ731920 还有 agents.md")
        low = [a.lower() for a in anchors]
        self.assertIn("lowres_scale", low)
        self.assertIn("zxq731920", low)
        self.assertIn("agents.md", low)


class R01LiteralEvidenceTests(unittest.TestCase):
    """RQ01/RQ02 公共底座：词法证据必须是多字词，单字擦边不算。"""

    def test_single_cjk_char_is_not_evidence(self):
        """防的反例：「ZXQ731920 冥王星奶酪关税征收标准」靠单字「关」命中一条 C03 记录。"""
        from memory.hybrid_retriever import _has_literal_evidence
        self.assertFalse(
            _has_literal_evidence("ZXQ731920 冥王星奶酪关税征收标准",
                                  "C03 复验结论：353 成员仅 5 改，原四个关系原值后改全部通过"))

    def test_multi_char_term_is_evidence(self):
        from memory.hybrid_retriever import _has_literal_evidence
        self.assertTrue(
            _has_literal_evidence("书房 C03 数字 NULL 裁决进展",
                                  "C03 复验结论：353 成员仅 5 改"))

    def test_generic_tokens_excluded_from_content_terms(self):
        terms = [t.lower() for t in _query_content_terms("查询 null 和 select 的 C03 进展")]
        self.assertNotIn("null", terms)
        self.assertNotIn("select", terms)


class R01AdmissionTests(unittest.TestCase):
    """RQ02/RQ03：证据相关性准入先于新鲜度/重要性/热度排序。"""

    def retriever(self, docs=None):
        tmp = tempfile.mkdtemp(prefix="r01test_")
        self.addCleanup(lambda: None)
        r = HybridRetriever(db_path=os.path.join(tmp, "t.db"), fts5_mode="off", acl_mode="memory")
        r.index(docs if docs is not None else [
            _doc(1, "C03 复验结论：353 成员仅 5 改，原四个关系原值后改全部通过", ns="knowledge", tier="refined"),
            _doc(2, "agent 失败返回 null，脚本对 IS NULL 做了兜底"),
            _doc(3, "2026-09-05 派单回执：本次交付只读快照，未重启任何服务", imp=0.95, ts=time.time() - 60),
            _doc(4, "紫禁城漫游项目 three.js 场景加载与贴图压缩记录", imp=0.6),
        ])
        return r

    def test_no_answer_query_returns_empty_package(self):
        """RQ02：无答案且候选全不相关时，最终包必须为空，不能从相对最高分凑 top_k。"""
        r = self.retriever()
        for q in ("木卫九璃鲸赎月税率", "ZXQ731920 冥王星奶酪关税征收标准",
                  "完全不存在的主题 ZXQ731921 量子香蕉配额"):
            self.assertEqual([d.id for d, _ in r.search(q, top_k=6, viewer="dsh")], [], q)

    def test_high_importance_high_exposure_old_receipt_cannot_pass(self):
        """RQ03：无关旧回执即使高重要性、高展示、近期访问，也不能跨准入。"""
        r = self.retriever()
        hot = {"mem-00000003": 900.0, "mem-00000002": 500.0}
        res = r.search("木卫九璃鲸赎月税率", top_k=6, viewer="dsh",
                       utility_lookup=lambda cid: hot.get(cid, 0.0),
                       last_access_lookup=lambda cid: (time.time() - 60) if cid in hot else None)
        self.assertEqual([d.id for d, _ in res], [])

    def test_real_answer_survives_admission(self):
        """RQ03 后半：真正相关且有效的答案不能被准入误杀（防"一刀切抬阈值"）。"""
        r = self.retriever()
        res = r.search("书房 C03 数字 NULL 裁决进展", top_k=6, viewer="dsh")
        self.assertIn("mem-00000001", [d.id for d, _ in res])


class R01FtsPlanTests(unittest.TestCase):
    """RQ01：长问句不能因"修饰词没全中"就查空，也不能全 OR 放大异题。"""

    def test_plans_are_ordered_and_topic_constrained(self):
        from memory.vector_store import _build_fts_plans
        plans = _build_fts_plans("书房 C03 数字 NULL 裁决 进展")
        self.assertGreaterEqual(len(plans), 2)
        # 第一套必须是原样 AND（行为零漂移）
        self.assertEqual(plans[0]["expr"], "书房 C03 数字 NULL 裁决 进展")
        # 最后兜底必须仍带主题词约束，而不是纯 OR
        last = plans[-1]
        self.assertIn("C03", last["expr"])
        self.assertNotIn(" OR ".join(["书房", "C03", "数字", "NULL", "裁决", "进展"]), last["expr"])

    def test_single_token_query_has_single_plan(self):
        from memory.vector_store import _build_fts_plans
        self.assertEqual(len(_build_fts_plans("C03")), 1)


class R01DomainScopeTests(unittest.TestCase):
    """RQ06：域范围在候选限额前生效——外域可读候选不能挤掉域内正确答案。"""

    def test_namespace_filter_applies_before_top_k(self):
        """防的反例：不传 namespace_filter 时先取全库 top_k，再筛本域 ⇒ 域内答案被挤空。"""
        tmp = tempfile.mkdtemp(prefix="r01ns_")
        r = HybridRetriever(db_path=os.path.join(tmp, "t.db"), fts5_mode="off", acl_mode="memory")
        docs = [_doc(200 + i, "无关的外域内容 filler %d" % i, ns="assistant", imp=0.9)
                for i in range(10)]
        docs.append(_doc(300, "书房 C03 数字 NULL 裁决进展：域内正确答案", ns="dsh", imp=0.3))
        r.index(docs)
        q = "书房 C03 数字 NULL 裁决进展"
        unrestricted = [d.id for d, _ in r.search(q, top_k=1, viewer="dsh")]
        scoped = [d.id for d, _ in r.search(q, top_k=1, viewer="dsh", namespace_filter=("dsh",))]
        # 不传域过滤时，外域高分候选可能先占位（这正是要修的现象）
        self.assertEqual(scoped, ["mem-0000012c"], "域内答案在有域过滤时必须拿到名额")
        # 至少证明域过滤确实能把外域排除干净
        self.assertTrue(all(i == "mem-0000012c" for i in scoped))
        _ = unrestricted  # 记录现象，不对它做断言（不受控于本文件）

    def test_cross_still_returns_permitted_sources(self):
        tmp = tempfile.mkdtemp(prefix="r01ns2_")
        r = HybridRetriever(db_path=os.path.join(tmp, "t.db"), fts5_mode="off", acl_mode="memory")
        r.index([_doc(400, "共享的 C03 记录", ns="knowledge"),
                 _doc(401, "他人 private 记录", ns="dsh")])
        got = [d.id for d, _ in r.search("共享的 C03 记录", top_k=5, viewer="")]  # 空身份 fail-closed
        self.assertIn("mem-00000190", got)


class R01UtilitySeparationTests(unittest.TestCase):
    """RQ05：召回/展示照旧记账，但不得自动增加有用反馈或刷新新鲜度。"""

    def test_exposure_does_not_inflate_rank_score(self):
        from memory.vector_store import VectorStore
        tmp = tempfile.mkdtemp(prefix="r01util_")
        vs = VectorStore(db_path=os.path.join(tmp, "t.db"))
        try:
            conn = vs._get_conn()
            conn.execute("INSERT OR REPLACE INTO chunks (id,content,namespace,visibility,state,importance,timestamp)"
                         " VALUES (?,?,?,?,?,?,?)",
                         ("mem-u0000001", "旧回执", "dsh", "shared", "active", 0.5, 1.0))
            conn.commit()
            for _ in range(50):
                vs.increment_utility("mem-u0000001", viewer="dsh", state="exposed")
            u = vs.get_utility("mem-u0000001", viewer="dsh")
            # 账要真实：50 次展示必须记下来
            self.assertEqual(u["exposed_n"], 50)
            self.assertGreater(u["utility_score_raw"], 0)     # 历史账不篡改
            self.assertEqual(u["utility_score"], 0.0)          # 但不作为"有用"加分
            self.assertIsNone(u["last_access_useful"])         # 也不刷新新鲜度
            # 一次真实引用才算数
            vs.increment_utility("mem-u0000001", viewer="dsh", state="cited")
            u2 = vs.get_utility("mem-u0000001", viewer="dsh")
            self.assertGreater(u2["utility_score"], 0)
            self.assertIsNotNone(u2["last_access_useful"])
        finally:
            vs.close()


if __name__ == "__main__":
    unittest.main()
