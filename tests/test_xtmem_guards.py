# -*- coding: utf-8 -*-
"""XTMEM 守卫测试（Phase T · T-4，2026-09-01，小D）。

首批三条守卫直接挂当前 3🔴（修判据与守判据必须同一次 commit，总则 10）：
  T-4① test_visibility_min_fail_closed  —— BF-3（min_visibility 查不到→private 兜底 fail-closed）
  T-4② test_search_excludes_self         —— BF-1（search_similar_for_evolution 排除自身自环）
  T-4③ test_tuning_matches_hardcoded     —— BF-7（json 的 enrich/confirm 阈值与代码 fallback 一致）

写法模板（总则 10）：人造违规必抓 / 合法不误伤 / 自证值不与现实重合。
"""

from memory.visibility import min_visibility


def test_visibility_min_fail_closed():
    """BF-3 + T-4①：min_visibility 查不到来源 → private 兜底（fail-closed），禁回落 shared。"""
    # 人造违规样本：来源 id 不存在（lookup→None）。旧版 `or "shared"` 会降级 shared → 必红；新逻辑必须 private
    assert min_visibility(["ghost-id"], lambda s: None) == "private", (
        "来源 id 不存在时必须 private 兜底（fail-closed）；旧版 or 'shared' 会让它降级 shared"
    )

    # 合法样本不误伤：真 shared 来源 → shared
    assert min_visibility(["a"], lambda s: "shared") == "shared"
    # 真 private 来源 → private
    assert min_visibility(["a"], lambda s: "private") == "private"
    # 混合 shared+private → private（取最严）
    assert min_visibility(["a", "b"], lambda s: {"a": "shared", "b": "private"}[s]) == "private"
    # 空 source_ids → private（无血缘衍生物一律最保守）
    assert min_visibility([], None) == "private"

    # 自证值不与现实重合：未知 id 的判定结果必须 != "shared"（防"自证恰好等于现实"假绿灯）
    assert min_visibility(["ghost-id"], lambda s: None) != "shared"


def test_search_excludes_self(tmp_db, cli_env):
    """BF-1 + T-4②：search_similar_for_evolution 排除自身（自环边修复）。

    演化的"刚落库自己在第一位、jaccard=1.0 自匹配"是 BF-1 自环边（974/1693）的根因。
    守卫：同一内容带 exclude_id 时不得返回自己；不带 exclude 时应返回自己（证明判据有区分度）。
    """
    import json, subprocess, sys
    from memory.vector_store import VectorStore

    content = "BF-1 自环边守卫：玄铁偏好本地 Ollama bge-m3 部署"
    cli = r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"
    p = subprocess.run([sys.executable, cli, "store", "--agent", "dsh", "--namespace", "dsh",
                        "--text", content, "--tags", "bf1"], env=cli_env,
                       capture_output=True, text=True, encoding="utf-8")
    r = json.loads(p.stdout.strip())
    assert r.get("ok"), f"store 失败: {r}"
    mid = r["id"]

    vs = VectorStore(str(tmp_db))
    # 合法样本不误伤：带 exclude_id 时不得返回自己
    got = vs.search_similar_for_evolution(content, top_k=5, namespace="dsh", exclude_id=mid)
    got_ids = [g[0] for g in got]
    assert mid not in got_ids, f"search_similar_for_evolution 未排除自身（自环边来源），返回含 {mid}"

    # 判据自证（区分度）：不带 exclude_id 时，自己应当出现（证明并非"判据恰好排掉一切"）
    got_all = vs.search_similar_for_evolution(content, top_k=5, namespace="dsh")
    assert any(g[0] == mid for g in got_all), "判据自证失败：无 exclude_id 也查不到刚存的内容，测验无区分度"


def test_tuning_matches_hardcoded():
    """BF-7 + T-4③：json / tuning.py _DEF 的 enrich/confirm 阈值须与代码 fallback（context_builder.py:404）一致。

    历史 bug：写反了 enrich=0.3/confirm=0.5（正确 = enrich=0.5/confirm=0.3）。
    后果：enriches 门槛 0.5→0.3 建边变多 + elif 短路致 confirms 分支永不可达（库 relation 全集零 confirms 印证）。
    守卫：json 一旦再被写反，本测试必红。
    """
    from memory.tuning import load_tuning

    evo = load_tuning()["evolution"]
    # context_builder.py:404 的 fallback（正确原值）：
    #     _j_rep, _j_enr, _j_con, _conf = 0.2, 0.5, 0.3, 0.6
    # 守卫断言 json 的 enrich/confirm 与该 fallback 一致（BF-7 修复目标）。
    assert evo["jaccard_enrich"] == 0.5, (
        f"jaccard_enrich 应为 0.5（代码 fallback），实际 {evo['jaccard_enrich']}——BF-7 曾写反成 0.3"
    )
    assert evo["jaccard_confirm"] == 0.3, (
        f"jaccard_confirm 应为 0.3（代码 fallback），实际 {evo['jaccard_confirm']}——BF-7 曾写反成 0.5，导致 confirms 分支死"
    )
    # 顺序语义也应自洽：enrich 阈值应高于 confirm（enrich 是更宽的关系，confirm 更严）
    assert evo["jaccard_enrich"] >= evo["jaccard_confirm"]

    # 自证值不与现实重合：读到的必须是有意义的两相异值（防"两值相等恰好通过"的假绿灯）
    assert evo["jaccard_enrich"] != evo["jaccard_confirm"]
