# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/xtmem_d3_1_noise_guard_test.py · D3-1 噪声切片过滤器守卫
#
#   ▍这是什么
#   D3-1（噪声切片过滤器）的回归守卫。覆盖：
#     §1 判据：R1/R2 正例命中 + 负例零误杀
#     §2 建图期 rebuild_edges 不含噪声贡献
#     §3 increment_for_chunk 遇噪声 return 0（且按规则粒度判）
#     §4 enabled=false 全链路逐字节不变
#     §5 🔴 召回系数铁律：(0,1]，0/负数/超1/非数值必须被拒绝
#     §6 判据单一源（建图期与召回期同一模块，且都不自写正则）
#     §7 R3 分域密度阈值
#     §8 开闸分法（2026-09-10 派单工单 A-4 五条）：
#        8.1 R3 在召回期不产生降权（recall_weight.R3=null → final 不变）
#        8.2 同一 R3 命中条目【建图期被排除】（证明分治真的分了，不是整体关）
#        8.3 铁律不退化：recall_weight.R1=0 仍被拒绝回落 1.0
#        8.4 旧格式单键 enabled/build_exclude/recall_weight 兼容读
#        8.5 enabled=false 时全链路逐字节不变（现状保护不破）
#
#   ▍跑法
#   直接运行 exit 0/1；pytest 收集走 test_d3_1_noise_guard()
#   （体必须收在 _guard() 内，顶层 sys.exit 会打断整个收集——见错题本 #35）。
#
#   ▍修改铁律
#   1. 副本同步（xtmem-project/tests、XTMEM/tests、xtmem-project 镜像、XTMEM-public/tests）。
#   2. 断言只用临时夹具库（tempfile），严禁连生产库。
#   3. 🔴 recall_weight 只能 (0,1]：必须断言 0/负数被拒绝——设 0 会让噪声在
#      所有有效记忆掉线时也永不复活=失去兜底（用户A 2026-09-10 铁律）。
#   4. ⚠️【不】依赖环境配置：本守卫一律用 _set_cfg 显式钉住 _cfg 返回值。
#      血泪原因（2026-09-10）：D3-1 原始版把 §2.1/§4 写成"默认 enabled=false"，
#      14:52 开闸把生产 xtmem_tuning.json 真的翻成 enabled=true 后，这 4 条当场转红——
#      守卫跟着生产配置漂，就成了"配置回声"而不是回归网。
#
#   ▍更新记录
#   *更新：2026-09-10 · D3-1 开闸分法改造（显式控配置 + §8 五条分治断言，assistant-c）*
#   *创建：2026-09-10 · D3-1 派单工单，assistant-c*
# ================================================================
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import os
import shutil
import sqlite3
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

MASTER = _REPO / "src" / "hermes-plugin"
if MASTER not in sys.path:
    sys.path.insert(0, MASTER)

CHECKS = {}

_PREFIX = [
    "Review the conversation",
    "蒸馏 · ",
    "[IMPORTANT: You are running as a scheduled",
    "CONTEXT COMPACTION",
]
_STRUCT = ["exit code", "system-reminder"]

# 开闸分法形态（本单核心配置）
SPLIT_ON = {
    "noise_filter": {
        "enabled": True,
        "build_exclude": {"R1": True, "R2": True, "R3": True},
        "recall_weight": {"R1": 0.3, "R2": 0.3, "R3": None},
        "prefix_patterns": list(_PREFIX),
        "struct_patterns": list(_STRUCT),
        "entity_density_ratio": 2.5,
    }
}
# 旧格式单键形态（兼容性回归：build_exclude/recall_weight 均为标量）
LEGACY_ON = {
    "noise_filter": {
        "enabled": True,
        "build_exclude": True,
        "recall_weight": 0.3,
        "prefix_patterns": list(_PREFIX),
        "struct_patterns": list(_STRUCT),
        "entity_density_ratio": 2.5,
    }
}
# R1 不参与建图排除的变体（证明 build_exclude 真的按规则粒度生效：同一条 R1 噪声不再被跳）
SPLIT_R1_OFF_BUILD = {
    "noise_filter": {
        "enabled": True,
        "build_exclude": {"R1": False, "R2": True, "R3": True},
        "recall_weight": {"R1": 0.3, "R2": 0.3, "R3": None},
        "prefix_patterns": list(_PREFIX),
        "struct_patterns": list(_STRUCT),
        "entity_density_ratio": 2.5,
    }
}
# R3 不参与建图排除的变体（证明 build_exclude 真的按规则粒度生效）
SPLIT_NO_R3_BUILD = {
    "noise_filter": {
        "enabled": True,
        "build_exclude": {"R1": True, "R2": True, "R3": False},
        "recall_weight": {"R1": 0.3, "R2": 0.3, "R3": None},
        "prefix_patterns": list(_PREFIX),
        "struct_patterns": list(_STRUCT),
        "entity_density_ratio": 2.5,
    }
}
OFF_CFG = {"noise_filter": {"enabled": False}}


def check(name, cond, detail=""):
    CHECKS[name] = bool(cond)
    print(("  PASS: " if cond else "  FAIL: ") + name + ("" if cond else "  | " + str(detail)))


def _set_cfg(cfg):
    """把 memory.noise_filter 的 _cfg 钉死到给定配置（显式 tuning 仍优先）。

    为什么必须钉：各消费点（graph / hybrid_retriever）都是函数内 lazy import 后调用 _cfg，
    全局替换模块属性即可覆盖全部调用点；不这么做，守卫就跟着生产 xtmem_tuning.json 漂。
    """
    import memory.noise_filter as _nf

    def _stub(tuning=None):
        if isinstance(tuning, dict) and isinstance(tuning.get("noise_filter"), dict):
            return dict(tuning["noise_filter"])
        return dict(cfg["noise_filter"])

    _nf._cfg = _stub


def _restore_cfg(orig):
    import memory.noise_filter as _nf
    _nf._cfg = orig


def _guard() -> int:
    CHECKS.clear()
    print("\n=== XTMEM D3-1 噪声切片过滤器守卫（开闸分法版）===")

    import memory.noise_filter as _nf
    from memory.noise_filter import (
        is_noise_text, is_noise_chunk, get_recall_weight, rule_of,
        noise_chunk_ids, entity_density_thresholds, is_build_excluded,
    )
    _ORIG_CFG = _nf._cfg

    p_split = SPLIT_ON["noise_filter"]

    # ------------------------------------------------------------
    # §1. 判据：正例命中 + 负例防误杀
    # ------------------------------------------------------------
    pos_cases = [
        ("Review the conversation above and consider...", "R1 Review 模板"),
        ("蒸馏 · personal" + "\n" + "本窗口共 12 轮", "R1 蒸馏卡头"),
        ("[IMPORTANT: You are running as a scheduled task] 请执行", "R1 scheduled 注入"),
        ("CONTEXT COMPACTION — 会话已压缩", "R1 压缩日志"),
        ("命令执行完毕" + "\n" + "exit code 1" + "\n" + "输出如下", "R2 exit code 行首"),
        ("<system-reminder>当前时间 2026-09-10</system-reminder>", "R2 system-reminder"),
        ('{"tool_call": {"name": "read_file"}}', "R2 工具调用 JSON"),
    ]
    pos_ok = 0
    for txt, label in pos_cases:
        hit, _why = is_noise_text(txt, p_split)
        if hit:
            pos_ok += 1
        else:
            print("    [miss] %s" % label)
    check("1.1 R1/R2 正例全部命中（7 条）", pos_ok == len(pos_cases), "%d/%d" % (pos_ok, len(pos_cases)))

    neg_cases = [
        ("我刚跑了下命令结果报 exit code 1，你觉得是什么原因？", "讨论报错（exit code 在句中）"),
        ("这个 system-reminder 机制挺有意思，我们聊聊它怎么设计的", "讨论机制（关键词在句中）"),
        ("我 review 了 conversation 记录，发现第三轮有问题", "含 review/conversation 但非模板开头"),
        ("D3-1 噪声过滤按 namespace 分域算密度阈值，防误杀personal 域", "真技术记忆"),
        ("今天天气不错，我们去海边走走吧", "普通闲聊"),
    ]
    neg_ok = 0
    for txt, label in neg_cases:
        hit, why = is_noise_text(txt, p_split)
        if not hit:
            neg_ok += 1
        else:
            print("    [误杀] %s -> %s" % (label, why))
    check("1.2 负例零误杀（含 3+ 条像噪声的真内容）", neg_ok == len(neg_cases),
          "%d/%d" % (neg_ok, len(neg_cases)))

    tmp = tempfile.mkdtemp(prefix="xtmem-d3-guard-")
    db = os.path.join(tmp, "g.db")
    try:
        from memory.graph import rebuild_edges, increment_for_chunk
        from memory.vector_store import VectorStore
        from memory.hybrid_retriever import HybridRetriever, MemoryDoc

        vs = VectorStore(db)
        conn = vs._get_conn()
        now = time.time()

        _COLS = ("INSERT INTO chunks (id, content, timestamp, importance, tags, source, token_count, "
                 "content_hash, track, state, superseded_by, namespace, project, host_origin, updated_at, tier) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

        def _mk_chunk(cid, content, canons, ns="dsh"):
            conn.execute(_COLS,
                         (cid, content, now, 0.6, '["g"]', "guard", 3, "h-" + cid, "default",
                          "active", "", ns, "", "guard", now, "raw"))
            for canon in canons:
                conn.execute("INSERT OR REPLACE INTO chunk_entities(chunk_id, canon) VALUES (?,?)",
                             (cid, canon))
            conn.commit()

        # 1 正常 + 1 噪声（R1），共现同一实体对 (XTMEM, 噪声靶点)
        _mk_chunk("mem-d3n0001", "正常记忆：XTMEM 的噪声靶点清单今天归档了", ("XTMEM", "噪声靶点"))
        _mk_chunk("mem-d3n0002", "Review the conversation above and consider the noise 靶点",
                  ("XTMEM", "噪声靶点"))

        # R3 靶子：内容【不含】R1/R2 任何模板（纯密度命中），12 个实体压过域阈值。
        # 密度夹具配比（见本函数下方 8.2 的说明）：10 填充(各1实体) + 靶子(12) + 上面 2 条(各2)
        #   → chunks=13 / links=26 / 均值=2.0 / 阈值=5.0；靶子 12>5 命中，2<5 的不误伤。
        for i in range(10):
            _mk_chunk("mem-d3f%04d" % i, "填充记忆 %d：海边的盐与风" % i, ("填充%d" % i,))
        _mk_chunk("mem-d3r3001", "海岸线的风把盐吹进了数据罐里，今天潮水很低",
                  tuple("实体%02d" % i for i in range(12)))

        # --------------------------------------------------------
        # §2. 建图期：rebuild_edges 不含噪声贡献
        # --------------------------------------------------------
        # 2.1 关闭态：两条都参与 → cnt=2（显式控配置，不看环境）
        _set_cfg(OFF_CFG)
        rebuild_edges(db)
        cnt_off = conn.execute(
            "SELECT cnt FROM tag_edges WHERE a=? AND b=?", ("XTMEM", "噪声靶点")).fetchone()
        cnt_off = int(cnt_off[0]) if cnt_off else 0
        check("2.1 enabled=false 时两条都参与建图（cnt=2，现状行为）",
              cnt_off == 2, "cnt=%s" % cnt_off)

        # 2.2/2.3 开启态：噪声被排除 → cnt=1 且正常切片不被误判
        _set_cfg(SPLIT_ON)
        ids_on = noise_chunk_ids(conn)
        check("2.2 noise_chunk_ids 在开启态命中该噪声切片",
              "mem-d3n0002" in ids_on, "ids=%s" % ids_on)
        check("2.3 正常切片不被误判", "mem-d3n0001" not in ids_on)

        rebuild_edges(db)
        cnt_on = conn.execute(
            "SELECT cnt FROM tag_edges WHERE a=? AND b=?", ("XTMEM", "噪声靶点")).fetchone()
        cnt_on = int(cnt_on[0]) if cnt_on else 0
        check("2.5 开启态 rebuild_edges 排除噪声贡献（cnt=1）", cnt_on == 1, "cnt=%s" % cnt_on)

        # 2.4 关闭态 noise_chunk_ids 空表
        _set_cfg(OFF_CFG)
        check("2.4 enabled=false 时 noise_chunk_ids 返回空表（零行为变化）",
              noise_chunk_ids(conn) == [])

        # --------------------------------------------------------
        # §3. increment_for_chunk 遇噪声 return 0（且按规则粒度判）
        # --------------------------------------------------------
        _set_cfg(SPLIT_ON)
        n_noise = increment_for_chunk("mem-d3n0002", db_path=db)
        check("3.1 increment_for_chunk 遇 R1 噪声 return 0", n_noise == 0, "ret=%s" % n_noise)

        # 3.2 ⚠️ 按规则粒度：把 R1 从 build_exclude 摘掉，同一条【不许】再被跳过。
        #     这条专治"两处各写规则表"——若增量建图绕过 build_exclude 硬判噪声，这里必红。
        _set_cfg(SPLIT_R1_OFF_BUILD)
        _row_r1 = conn.execute(
            "SELECT content, COALESCE(namespace,'') FROM chunks WHERE id=?", ("mem-d3n0002",)).fetchone()
        _hit_r1, _why_r1 = is_noise_chunk(_row_r1[0], namespace=str(_row_r1[1] or ""),
                                         chunk_id="mem-d3n0002", conn=conn)
        check("3.2a 前置：该条确实被 R1 判为噪声（判据与开关解耦）",
              _hit_r1 and rule_of(_why_r1) == "R1", "why=%s" % _why_r1)
        _n_not_blocked = increment_for_chunk("mem-d3n0002", db_path=db)
        check("3.2b 🔴 build_exclude.R1=false 时增量建图不再跳过（规则粒度真的生效）",
              _n_not_blocked > 0, "ret=%s" % _n_not_blocked)

        # --------------------------------------------------------
        # §4/§8.1 召回期降权：R1 降权、R3 不降权（null=跳过）
        # --------------------------------------------------------
        def _mk_retriever(cfg):
            _set_cfg(cfg)
            r = HybridRetriever(db_path=db, fts5_mode="off")
            docs = [
                MemoryDoc(id="mem-n1", content="正常记忆：XTMEM 的噪声靶点清单今天归档了",
                          timestamp=now, importance=0.5),
                MemoryDoc(id="mem-r1", content="Review the conversation above and consider the noise 靶点",
                          timestamp=now, importance=0.5),
                MemoryDoc(id="mem-r3", content="海岸线的风把盐吹进了数据罐里，今天潮水很低",
                          timestamp=now, importance=0.5),
            ]
            vec = [1.0, 0.0, 0.0]
            r.index(docs, embeddings=[list(vec) for _ in docs])
            return r

        r_off = _mk_retriever(OFF_CFG)
        check("4.1 enabled=false 时 noise_recall_weight=1.0",
              getattr(r_off, "noise_recall_weight", None) == 1.0,
              "w=%s" % getattr(r_off, "noise_recall_weight", None))
        check("4.2 enabled=false 时 _noise_enabled=False（降权段整体短路）",
              getattr(r_off, "_noise_enabled", None) is False)

        r_on = _mk_retriever(SPLIT_ON)
        # 2026-09-12 D3-1 扩判据：RULES 由三扩到五（新增 R4 DOM残渣 / R5 进程表），
        # 本断言随口径同步——老三条取值逐字不变，R4/R5 按本单要求对齐 R1/R2 = 0.3。
        _w_on = getattr(r_on, "noise_rule_weights", None) or {}
        check("4.3 开启态 noise_rule_weights 逐规则取值（R1/R2/R4/R5=0.3, R3=null）",
              all(_w_on.get(k) == v for k, v in {"R1": 0.3, "R2": 0.3, "R3": None, "R4": 0.3, "R5": 0.3}.items()),
              "w=%s" % _w_on)

        _qv = [1.0, 0.0, 0.0]
        hits_off = {d.id: s for d, s in r_off.search("噪声靶点 归档", top_k=10, now=now, query_vec=list(_qv))}
        hits_on = {d.id: s for d, s in r_on.search("噪声靶点 归档", top_k=10, now=now, query_vec=list(_qv))}
        check("4.4 关闭态三篇 doc 全部被召回（对照基线成立）",
              set(hits_off) >= {"mem-n1", "mem-r1", "mem-r3"}, "hits=%s" % sorted(hits_off))

        d_r1 = abs(hits_on.get("mem-r1", 0.0) - hits_off.get("mem-r1", 0.0) * 0.3)
        check("8.1a R1 命中在召回期被 ×0.3", d_r1 < 1e-9,
              "on=%.6f off=%.6f 期望=%.6f" % (hits_on.get("mem-r1", 0.0), hits_off.get("mem-r1", 0.0),
                                             hits_off.get("mem-r1", 0.0) * 0.3))
        check("8.1b 🔴 R3 命中在召回期【不降权】（recall_weight.R3=null → final 不变）",
              abs(hits_on.get("mem-r3", 0.0) - hits_off.get("mem-r3", 0.0)) < 1e-9,
              "on=%.6f off=%.6f" % (hits_on.get("mem-r3", 0.0), hits_off.get("mem-r3", 0.0)))
        check("8.1c 正常条目 final 不受影响",
              abs(hits_on.get("mem-n1", 0.0) - hits_off.get("mem-n1", 0.0)) < 1e-9,
              "on=%.6f off=%.6f" % (hits_on.get("mem-n1", 0.0), hits_off.get("mem-n1", 0.0)))

        # --------------------------------------------------------
        # §8.2 分治真的分了：同一条 R3 靶子【建图期被排除】
        # --------------------------------------------------------
        _set_cfg(SPLIT_ON)
        _row_r3 = conn.execute(
            "SELECT content, COALESCE(namespace,'') FROM chunks WHERE id=?", ("mem-d3r3001",)).fetchone()
        _hit_r3, _why_r3 = is_noise_chunk(_row_r3[0], namespace=str(_row_r3[1] or ""),
                                          chunk_id="mem-d3r3001", conn=conn)
        check("8.2a R3 靶子确实被 R3 规则命中（前置条件成立）",
              _hit_r3 and rule_of(_why_r3) == "R3", "why=%s" % _why_r3)
        check("8.2b 🔴 同一 R3 条目建图期【被排除】（build_exclude.R3=true）",
              is_build_excluded(_why_r3) is True and "mem-d3r3001" in noise_chunk_ids(conn))
        _set_cfg(SPLIT_NO_R3_BUILD)
        check("8.2c build_exclude.R3=false 时同一条不再被排除（规则粒度可关）",
              is_build_excluded(_why_r3) is False and "mem-d3r3001" not in noise_chunk_ids(conn))

        # --------------------------------------------------------
        # §5/§8.3 召回系数铁律：(0,1]，0/负数必须被拒绝（含逐规则形态）
        # --------------------------------------------------------
        w_ok = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": 0.3}})
        check("5.1 合法系数 0.3 被接受", abs(w_ok - 0.3) < 1e-9, "w=%s" % w_ok)
        w_one = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": 1.0}})
        check("5.2 边界 1.0 被接受", abs(w_one - 1.0) < 1e-9, "w=%s" % w_one)
        w_zero = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": 0}})
        check("5.3 🔴 系数 0 被拒绝并回落 1.0（禁设 0=失去兜底）",
              abs(w_zero - 1.0) < 1e-9, "w=%s" % w_zero)
        w_neg = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": -0.5}})
        check("5.4 负数系数被拒绝并回落 1.0", abs(w_neg - 1.0) < 1e-9, "w=%s" % w_neg)
        w_big = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": 1.5}})
        check("5.5 超 1 系数被拒绝并回落 1.0", abs(w_big - 1.0) < 1e-9, "w=%s" % w_big)
        w_str = get_recall_weight({"noise_filter": {"enabled": True, "recall_weight": "abc"}})
        check("5.6 非数值系数被拒绝并回落 1.0", abs(w_str - 1.0) < 1e-9, "w=%s" % w_str)

        # 8.3 逐规则形态下铁律不退化：R1=0 拒绝，R3=null 合法
        w_r1_zero = get_recall_weight("R1", {"noise_filter": {
            "enabled": True, "recall_weight": {"R1": 0, "R2": 0.3, "R3": None}}})
        check("8.3a 🔴 逐规则 R1=0 仍被拒绝回落 1.0（铁律不退化）",
              abs(w_r1_zero - 1.0) < 1e-9, "w=%s" % w_r1_zero)
        check("8.3b R3=null 合法返回 None（跳过，不是乘 1）",
              get_recall_weight("R3", SPLIT_ON) is None)
        check("8.3c null 与 0 语义区分（0 会被硬拒，null 被接受为跳过）",
              get_recall_weight("R3", SPLIT_ON) is None
              and abs(get_recall_weight("R3", {"noise_filter": {
                  "enabled": True, "recall_weight": {"R1": 0.3, "R2": 0.3, "R3": 0}}}) - 1.0) < 1e-9)

        # --------------------------------------------------------
        # §8.4 旧格式单键兼容读
        # --------------------------------------------------------
        check("8.4a 旧格式 build_exclude:true → 三规则同开（R1/R3 都进排除集）",
              "mem-d3n0002" in noise_chunk_ids(conn, tuning=LEGACY_ON)
              and "mem-d3r3001" in noise_chunk_ids(conn, tuning=LEGACY_ON))
        check("8.4b 旧格式 recall_weight:0.3 → 三规则同系数（R3 不再是 null）",
              abs(get_recall_weight("R1", LEGACY_ON) - 0.3) < 1e-9
              and abs(get_recall_weight("R3", LEGACY_ON) - 0.3) < 1e-9)
        check("8.4c 旧格式 enabled:true 仍能开启识别",
              is_noise_text("Review the conversation above", LEGACY_ON["noise_filter"])[0] is True)

        # --------------------------------------------------------
        # §8.5 enabled=false 全链路逐字节不变（现状保护）
        # --------------------------------------------------------
        _set_cfg(OFF_CFG)
        check("8.5a 关闭态 noise_chunk_ids 恒空（建图期零变化）",
              noise_chunk_ids(conn) == [])
        check("8.5b 关闭态 is_build_excluded 恒 False（增量建图零变化）",
              is_build_excluded("R1:prefix:Review the conversation") is False)
        check("8.5c 关闭态 is_noise_chunk 恒 False（判据零变化）",
              is_noise_chunk("Review the conversation above", namespace="dsh",
                             chunk_id="mem-d3n0002", conn=conn)[0] is False)

        # --------------------------------------------------------
        # §6. 判据单一源（建图期与召回期调用同一模块）
        # --------------------------------------------------------
        import memory.graph as _graph_mod
        gsrc = open(_graph_mod.__file__, encoding="utf-8").read()
        check("6.1 graph.py 调用 memory.noise_filter（建图期同源）",
              "from memory.noise_filter import" in gsrc)
        import memory.hybrid_retriever as _hr_mod
        hsrc = open(_hr_mod.__file__, encoding="utf-8").read()
        check("6.2 hybrid_retriever.py 调用 memory.noise_filter（召回期同源）",
              "from memory.noise_filter import" in hsrc)
        check("6.3 graph.py 未自写噪声正则（无 Review 模板字面量）",
              "Review the conversation" not in gsrc)
        check("6.4 hybrid_retriever.py 未自写噪声正则（无 Review 模板字面量）",
              "Review the conversation" not in hsrc)
        check("6.5 graph.py 增量建图按 build_exclude 判（不硬判噪声）",
              "is_build_excluded" in gsrc)

        # --------------------------------------------------------
        # §7. R3 分域密度阈值（按 namespace 分别算，跨域不串味）
        # --------------------------------------------------------
        thr = entity_density_thresholds(conn, 2.5)
        check("7.1 分域阈值表非空且按域给值", isinstance(thr, dict) and len(thr) >= 1,
              "thr=%s" % {k: round(v, 2) for k, v in list(thr.items())[:4]})

        conn.close()
    finally:
        _restore_cfg(_ORIG_CFG)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [k for k, v in CHECKS.items() if not v]
    print("\n=== D3-1 守卫结果：%d/%d passed ===" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("FAILED CHECKS:", failed)
        return 1
    return 0


def test_d3_1_noise_guard():
    assert _guard() == 0


if __name__ == "__main__":
    sys.exit(_guard())
