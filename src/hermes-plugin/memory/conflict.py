# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · memory/conflict.py · D2-2 矛盾候选召回与双向打分管线（影子态）
#
#   ▍这是什么
#   ① 仅针对 refined 层触发的矛盾候选召回与判定模块
#   ② 步骤：extract_anchors -> fts_search_bm25 -> 向量带通(cos_min~cos_max)
#           -> 显式版本下沉(content_time优先) -> cbcn/deepseek-v4-flash 双向打分
#   ③ 契约：只暴露不选边（写入 conflict_candidate + memory_evolution shadow_state=1），
#           严禁自动归档任何一方；提供 injection 警告辅助函数。
#
#   ▍修改铁律
#   1. 显式冲突必须走规则下沉（显式版本序优先，禁裸updated_at），禁调LLM。
#      ⚠️ 2026-09-10 更正：原写作「content_time 优先」——错。content_time 是时间戳不是版本序，
#         已废用 priority 3（详见 extract_explicit_version docstring）。别再按旧铁律放回去。
#   2. 写入两表必须幂等，pair_key = min(a,b)+'|'+max(a,b)。
#   3. 影子边绝不联动归档，必须保留双活状态。
#   4. 🔴【落库三闸 · 2026-09-11 P7.1 修复】写影子边时必须：
#      ① 方向 = 新取代旧（source=新取代者 / target=旧被取代者，与 trace.py 文件头方向铁律同向）；
#         取向优先级 details.newer_id/older_id → 内容显式版本序复算 → 时间信号，
#         【不】由调用方传参顺序决定（①②③ 都无证据时才保序，见 _orient_ids）；
#      ② visibility 走 min_visibility([a,b])（全库唯一实现），禁硬编码 'shared'；
#      ③ decision != 'supersedes'（非 error）时清掉该 pair 既有 shadow_state=1 边
#         ——重判要能覆盖旧结论，否则"重跑扫描"永远清不掉旧错误关系。
#      ⚠️ 修前病灶：边方向由扫描顺序决定 + 恒可见 shared + 重判后旧错边永久残留。
#      而 provider 已把 judged supersedes 当分歧提示消费 → 属【带病运行】，不是纯影子。
#
#   ▍更新记录
#   *更新：2026-09-11 · P7.2 四反例修复（小D，Z指导派单 P7.1+P7.2 合并单）——
#     ①extract_explicit_version 取值公式补 patch 位（major*1e6+minor*1e3+patch，regex 不动；
#       对旧公式已不同的对子保序单调，考试库 375 对推演 val/test 四分数零变化）；
#     ②judge_explicit_conflict 加跨项目守卫（双 project 非空且不同 → 不下沉）；
#     ③回滚极性：低版本侧「极性动词+本方版本号」同短语命中 → decision=rollback（非 supersedes），
#       方向以回滚陈述方为源；evaluate_pair_bidirectional 决策从 details 透传【不】再硬编码；
#     ④call_llm_score 负号保留（-0.8 不再吞成 0.8）、不可解析抛错走 error 路径【不】落假 0 分。
#     守卫：tests/test_xtmem_d2_rules.py 四反例各≥1条 + 反向验证改坏必红。*
#   *更新：2026-09-11 · P7.1 落库闭环（小D，Z指导派单 P7-A 任务 B）：record_conflict_result 三修——
#     方向改用 details.newer_id/older_id（LLM 路径按时间信号显式取向）、visibility 走 min_visibility、
#     重判非 supersedes 时清该 pair 旧影子边；scan 调用点传参顺序自此不再影响落库结果。*
#   *更新：2026-09-10 · D2-p3 修复（小D，Z指导派单 (a) 用户批）：废用 priority 3=content_time——
#     时间戳误当版本序致任何两条不同时间的记忆恒判 supersedes（回归集 18 触发 0 真阳性，
#     候选表 466 行污染）；规则下沉只保留 p1/p2（真版本语义），p1/p2 实测亦 0 真阳性待独立标准集复评。*
#   *更新：2026-09-09 · 创建（D2 全量工单，小D）*
#   *更新：2026-09-13 · 466 追单件①（Z指导 代执，工单 XTMEM-派单工单-466追单-流水豁免与81条复判-20260912）——
#     ①a 判定 prompt 提为常量 LLM_JUDGE_PROMPT 并补「同段对话流水豁免」条款（相邻轮次摘录
#       非独立长期事实，禁判版本推翻；466 抽检 11 误报中 9 对为此形态，含跨话题仍打 1.00 的红旗对）；
#     ①b 同 session 流水对识别 is_same_session_flow_pair + evaluate_pair_bidirectional 顶部豁免闸
#       （配置 same_session_flow_exempt，tuning conflict 段可关）。S2 相似度+时间信号实测无判别力
#       弃用（真演进 sim 0.051-0.187 与误报 0.021-0.461 完全重叠），S1 只认机器自产前缀（dsh 自动
#       记忆/蒸馏头），fail-open：解析不出不拦，回落现行判定。*
# ================================================================

import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TUNING_PATH = __import__("memory.public_runtime", fromlist=["home_file"]).home_file("xtmem_tuning.json")
LLM_BASE = os.environ.get("XTMEM_LLM_BASE", "").rstrip("/")
LLM_MODEL = os.environ.get("XTMEM_LLM_MODEL", "")

# 466 追单件①a（2026-09-13）：判定 prompt 提为常量——守卫测试断言豁免条款在场，
# 改坏必红（tests/test_xtmem_466b_flow_guard.py::test_prompt_has_flow_clause）。
# 豁免条款的由来：466 抽检 11 条 supersedes 误报中 9 条是「同一段对话的相邻轮次流水」
# （dsh 自动记忆 turn N vs turn M，其中一对跨话题仍打 1.00）——LLM 把"新轮次复述旧轮次"
# 当成版本推翻。条款要求流水对落 0.0，只有各自可独立驻库的事实才允许高分。
LLM_JUDGE_PROMPT = (
    "你是一个严谨的知识库冲突与矛盾裁判员。\n"
    "请判断【记忆 A】与【记忆 B】在事实陈述上是否存在直接矛盾或事实更新（如新配置推翻旧值、方案纠正、数字更新）。\n"
    "打分规则：\n"
    "- 1.0: 确凿的事实冲突或版本推翻（A 推翻或覆盖了 B）\n"
    "- 0.5: 存在部分分歧但定义不清或上下文不同\n"
    "- 0.0: 完全无关，或互为兼容补充（enriches/parallel）\n"
    "豁免条款：两条记录若为同一段对话的相邻轮次流水（问答摘录、执行反馈、任务进度播报——"
    "各自不是可独立驻库的长期事实陈述），不构成版本推翻关系，禁止给 1.0，按 0.0 处置；"
    "只有两条各自都是独立的事实/结论/契约，且 A 确实推翻 B 的陈述时才允许高分。\n"
    "请直接输出一个 0.0 到 1.0 之间的浮点数分数，严禁任何额外废话。"
)

def _get_api_key() -> str:
    """公共组件仅接调用方环境密钥，不搜索任何私人 profile 文件。"""
    return os.environ.get("XTMEM_LLM_KEY", "").strip()

def load_tuning_config() -> dict:
    defaults = {
        "cos_min": 0.45,
        "cos_max": 0.90,
        "t_gap": 3600,
        "top_k_bm25": 30,
        "llm_conflict_threshold": 0.65,
        "model": LLM_MODEL,
        "policy_version": "D2.2-v1",
        # 466 追单件①b（2026-09-13）：同 session 流水对豁免闸，false=关掉回落旧行为
        "same_session_flow_exempt": True
    }
    if os.path.exists(TUNING_PATH):
        try:
            with open(TUNING_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                return {**defaults, **d.get("conflict", {})}
        except Exception as e:
            logger.warning("Failed to read tuning config: %s", e)
    return defaults

def pair_key_of(id_a: str, id_b: str) -> str:
    return f"{min(id_a, id_b)}|{max(id_a, id_b)}"

def extract_explicit_version(content: str, content_time: Optional[float] = None) -> Tuple[int, float]:
    """提取显式版本序信号（病灶1修复版 + D2-p3 修复版：严密语义与前置排除）。
    返回值：(priority, value)
      3: 【已废用，2026-09-10 D2-p3 修复】曾为 content_time > 0，见下方警告块
      2: 显式语义版本号 (v1.2, V2.1.3, 版本 3.11 等)
      1: 阶段标注 (D1, P2, Phase 2 等)
      0: 无有效版本序 (IP段/日期/百分比/纯年份/端口等一律排除)

    ⚠️ 【不】priority 3 = content_time 已【永久废用】，别再放回去：
      content_time 是【时间戳】（NER 抽的正文时间基准），不是【版本序】。
      而 judge_explicit_conflict 的契约是「双方带同级别显式版本序 → 新版胜」。
      把时间戳塞进版本序槽位后，判据取值空间退化成「两条不同的记忆」= 恒真：
        任何两条 content_time 不同且都 >0 的记忆 → 一律判 supersedes，score=1.0。
      实测代价（2026-09-10，生产库 3912 active / 1038 条带 content_time=26.5%）：
        · 回归集 342 对上触发 18 次，真阳性 0（精度 0/18）；
        · conflict_candidate 466 行 supersedes 决策由本规则产出（全污染）；
        · 构造性反例已钉进守卫：A「我喜欢桃子汽水」(t=1000) vs B「今天下雨了」(t=900)
          曾判 supersedes —— 两条无关记忆被判「新推翻旧」。
      同层实测：priority 1/2（真版本语义）在回归集上 5 次触发也是 0 真阳性（见回执），
      说明「规则下沉」整体不是这个数据集上的有效信号；但 p1/p2 语义自洽（数字大小确实是序），
      保留待独立标准集复评，只废语义错位的 p3。

    ⚠️ content_time 形参【保留】仅为向后兼容调用签名，当前实现不再读取它
      （调用方仍传 c.get("content_time")，删参会砸调用面；见文件头铁律 1 的更新）。
    """
    # D2-p3（2026-09-10）：原 `if content_time and content_time > 0: return (3, ...)` 已删。
    # 【不】恢复——理由见 docstring 警告块，这是"判据写错"而非"阈值太松"。
    _ = content_time  # 保留形参、不参与判定（向后兼容签名）
    
    text = content or ""
    
    # 1. 前置排除（强行挖掉误导性数字串）
    cleaned = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){2,3}\b', ' ', text)
    cleaned = re.sub(r'\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b', ' ', cleaned)
    cleaned = re.sub(r'\b\d+(?:\.\d+)?%', ' ', cleaned)
    cleaned = re.sub(r'\b\d{4,}\b', ' ', cleaned)

    # 2. 语义版本号认定 (priority=2)
    # A) 显式带 v/V 前缀: v1.2, V2.1.3, v0.9
    v_pref = re.search(r'\b[vV](\d+)(?:\.(\d+)){1,2}\b', cleaned)
    if v_pref:
        try:
            raw = v_pref.group(0).lower().lstrip('v')
            parts = [int(p) for p in raw.split('.')]
            # P7.2 反例1（2026-09-11）：patch 位必须参与取值——旧公式 major*1000+minor 把
            # v1.2.3 与 v1.2.4 折成同值 1002，补丁版本对永远判不出冲突。
            # 新公式 major*1e6+minor*1e3+patch：对「旧公式下已不同」的对子保序单调
            # （minor*1e3+patch ≤ 999999 < 1e6，不会跨 major/minor 串位），只把旧公式
            # 下同值（仅 patch 位不同）的对子区分开。【不】动 regex——2~3 段的认定口径不变。
            val = parts[0] * 1000000 + (parts[1] if len(parts) > 1 else 0) * 1000 + (parts[2] if len(parts) > 2 else 0)
            return (2, float(val))
        except Exception:
            pass

    # B) 强语境词前置: 版本 3.11, version 2.0, 升级到 1.3, 更新到 2.5
    ctx_match = re.search(r'(?:版本|version|升级到|更新到|升至|升到|降到)\s*v?(\d+)(?:\.(\d+)){1,2}\b', cleaned, re.I)
    if ctx_match:
        try:
            m_str = re.search(r'\d+(?:\.\d+){1,2}', ctx_match.group(0))
            if m_str:
                parts = [int(p) for p in m_str.group(0).split('.')]
                # P7.2 反例1：与 A 支同口径，patch 位进公式（见上注释）
                val = parts[0] * 1000000 + (parts[1] if len(parts) > 1 else 0) * 1000 + (parts[2] if len(parts) > 2 else 0)
                return (2, float(val))
        except Exception:
            pass

    # 3. 阶段标注认定 (priority=1)
    # A) D1, D1.5, P2, P0.1
    dp_match = re.search(r'\b[DP](\d+)(?:\.(\d+))?\b', cleaned)
    if dp_match:
        try:
            m1 = int(dp_match.group(1))
            m2 = int(dp_match.group(2)) if dp_match.group(2) else 0
            return (1, float(m1 * 100 + m2))
        except Exception:
            pass

    # B) Phase 2, Phase 0, Phase 1.5
    phase_match = re.search(r'\bPhase\s*(\d+)(?:\.(\d+))?\b', cleaned, re.I)
    if phase_match:
        try:
            m1 = int(phase_match.group(1))
            m2 = int(phase_match.group(2)) if phase_match.group(2) else 0
            return (1, float(m1 * 100 + m2))
        except Exception:
            pass

    return (0, 0.0)

_ROLLBACK_PHRASE_RE = re.compile(
    r"(改回|回滚到|回退到|退回到|还原到|回退至|退回至|还原成|降级到|降到|降至)\s*[vV]?(\d+(?:\.\d+){1,2})\b"
)

def _rollback_polarity_hit(content: str, version_value: float) -> Optional[str]:
    """P7.2 反例3 辅助（2026-09-11）：判断低版本侧内容里是否有「极性动词 + 本方版本号」同短语命中。

    为什么必须紧跟版本字面量：正文任何位置飘着「改回/回滚」二字不构成回滚语义——
    考试库 train 上「改回」出现在无关句（如"v1 保留只写修订"段落）会误翻正常版本升级
    （2026-09-11 推演实测，松词表会把 labeled-supersedes 对误判成 rollback）。
    数值比对口径与 extract_explicit_version 一致：v1.2 与 v1.2.0 视为同值。
    返回命中短语原文（供 decision_reason 用），无命中返回 None。
    """
    v = int(version_value)
    text = content or ""
    for m in _ROLLBACK_PHRASE_RE.finditer(text):
        parts = [int(x) for x in m.group(2).split(".")]
        val2 = parts[0] * 1000000 + (parts[1] if len(parts) > 1 else 0) * 1000 + (parts[2] if len(parts) > 2 else 0)
        if val2 == v:
            return m.group(0)
    return None

def judge_explicit_conflict(c_a: dict, c_b: dict) -> Optional[dict]:
    """步骤五：显式冲突下沉规则判断。若双方带同级别显式版本序，直接判定新版胜。

    P7.2（2026-09-11）新增两道修正：
    ① 跨项目守卫（反例2）：双方都带非空 project 且不同 → 不构成同一条事实的版本演进，
       不下沉（项目 A 的 v1.2 和项目 B 的 v2.0 是两条平行事实）。project 缺失或相同
       则维持原判据——考试库与历史数据大量 project 为空，【不】能一刀切要求同项目。
    ② 回滚极性（反例3）：低版本侧内容带「极性动词+本方版本号」同短语命中（改回/降级到/回滚到…）
       → 这是回滚陈述在取代旧的高版本声明，决策记 rollback（不是 supersedes），方向以回滚陈述方为源。
    """
    p_a, v_a = extract_explicit_version(c_a.get("content", ""), c_a.get("content_time"))
    p_b, v_b = extract_explicit_version(c_b.get("content", ""), c_b.get("content_time"))

    # 只有当两方都具备有效版本序且优先级一致时，规则下沉
    if p_a > 0 and p_a == p_b and v_a != v_b:
        # P7.2 反例2：跨项目守卫
        proj_a = (c_a.get("project") or "").strip()
        proj_b = (c_b.get("project") or "").strip()
        if proj_a and proj_b and proj_a != proj_b:
            return None
        # P7.2 反例3：回滚极性（只对语义版本号判——D1/P2 阶段标注没有升级降级语义）
        if p_a == 2:
            lo_c, hi_c = (c_a, c_b) if v_a < v_b else (c_b, c_a)
            roll = _rollback_polarity_hit(lo_c.get("content", ""), v_a if v_a < v_b else v_b)
            if roll:
                return {
                    "is_conflict": True,
                    "decision": "rollback",
                    "direction": f"{lo_c['id']}_rolls_back_{hi_c['id']}",
                    "score": 1.0,
                    "rule": "explicit_version_rollback",
                    "newer_id": lo_c["id"],
                    "older_id": hi_c["id"],
                }
        newer_id = c_a["id"] if v_a > v_b else c_b["id"]
        older_id = c_b["id"] if v_a > v_b else c_a["id"]
        return {
            "is_conflict": True,
            "decision": "supersedes",
            "direction": f"{newer_id}_supersedes_{older_id}",
            "score": 1.0,
            "rule": f"explicit_version_priority_{p_a}",
            "newer_id": newer_id,
            "older_id": older_id
        }
    return None

def call_llm_score(text_a: str, text_b: str, model: str = LLM_MODEL) -> float:
    """调本地网关进行单向冲突概率评分 f(a,b)：评估 a 是否与 b 矛盾并覆盖 b。"""
    key = _get_api_key()
    prompt = LLM_JUDGE_PROMPT
    user_msg = f"【记忆 A】\n{text_a}\n\n【记忆 B】\n{text_b}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg}
        ],
        "temperature": 0.0,
        "max_tokens": 10
    }
    req = urllib.request.Request(
        LLM_BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "Mozilla/5.0"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        # P7.2 反例4（2026-09-11）：负号必须保留——模型用 -0.8 表达「明确不冲突」，
        # 旧正则 \d+ 吞掉负号后 -0.8 变 0.8（越过阈值成假阳性冲突）。夹取后负分=0.0 语义正确。
        m = re.search(r'-?\d+(?:\.\d+)?', content)
        if m:
            val = float(m.group(0))
            return max(0.0, min(1.0, val))
    # P7.2 反例4：「无法判断」等不可解析输出【不】落 0 分——0 分语义是「确定不冲突」，
    # 把判不出静默洗成无冲突属于假阴性造假。抛错交 evaluate_pair_bidirectional 的 error
    # 路径（decision=error：不闭旧边、下轮扫描重判），宁可诚实失败也不假 0 分。
    raise ValueError("LLM score unparseable: %r" % content)

# ── 466 追单件①b（2026-09-13）：同 session 对话流水对识别 ──────────────
# 勘测存档：new/zguide-466-workdir/survey_1b.py + survey_1b_s2.py。
# chunks 无结构化 session 字段（PRAGMA table_info 实证 22 列）。
# S2「相似度+时间相邻」实测无判别力：14 对人工标注集上，真演进对 sim∈[0.051,0.187]、
#   误报对 sim∈[0.021,0.461]，区间完全重叠 → 弃用，禁硬造模糊阈值（工单 R3 前车之鉴）。
# S1「正文前缀解析」可靠：dsh 自动记忆头「（turn N, session session-xxx）」与蒸馏头
#   「# 蒸馏结果 · ns:sessionid:ts」都是机器自产格式，session id 为 UUID 级精确匹配，
#   零阈值零模糊。fail-open：解析不出 → 不拦 → 回落现行判定（dsh 端改格式只会让闸失效，
#   不会产生新误拦）。覆盖率有限：外溢 99 对中两端可解析 44、同 session 36——
#   refined 正文无前缀的流水对拦不住，靠件①a 的 prompt 豁免条款兜底。
_RE_DSH_SESSION = re.compile(r"（turn\s+\d+,\s*session\s+(session-[0-9a-f\-]{8,})）")
_RE_DISTILL_SESSION = re.compile(r"#\s*蒸馏结果\s*·\s*[^:\s]+:([0-9]{8}_[0-9]{6}_[0-9a-f]+):\d+")


def _session_key_of(content: str) -> Optional[str]:
    """从机器自产正文前缀解析对话 session 键；解析不出返回 None。"""
    c = (content or "")[:400]
    m = _RE_DSH_SESSION.search(c)
    if m:
        return "dsh:" + m.group(1)
    m = _RE_DISTILL_SESSION.search(c)
    if m:
        return "distill:" + m.group(1)
    return None


def is_same_session_flow_pair(c_a: dict, c_b: dict) -> bool:
    """两端是否同一段对话的流水记录（件①b 判据，确定性、无阈值）。

    判定 = 两端各自解析出 session 键且相等。⚠️ 只认机器自产前缀，【不】扩到
    相似度/时间模糊信号——S2 已实测无判别力，扩了就是硬造脆弱判据。
    为什么拦：同 session 的轮次摘录是「对话流水」而非独立长期事实，判 supersedes
    等于把聊天记录当版本演进。已知盲区：intra-session 真演进（同会话先 v1 后 v2）
    会被一并豁免——raw 层流水本不该承载 supersedes 语义（件④请示 1 相关），可接受。
    """
    ka = _session_key_of(c_a.get("content", ""))
    return bool(ka) and ka == _session_key_of(c_b.get("content", ""))


def evaluate_pair_bidirectional(c_a: dict, c_b: dict, config: dict) -> dict:
    """双向评分平均 (f(a,b)+f(b,a))/2"""
    # 步骤〇（466 追单件①b）：同 session 流水对豁免——先于规则层与 LLM，
    # 直接判 enriches（非冲突）。record_conflict_result 会随之闭掉该 pair 的旧影子边。
    # 关闸方式：xtmem_tuning.json conflict.same_session_flow_exempt=false。
    if config.get("same_session_flow_exempt", True) and is_same_session_flow_pair(c_a, c_b):
        return {
            "is_conflict": False,
            "candidate_score": 0.0,
            "llm_score": None,
            "decision": "enriches",
            "policy_version": config["policy_version"],
            "model": "same_session_flow_guard",
            "prompt_hash": "flow_guard_v1",
            "details": {"flow_exempt": True}
        }

    # 步骤五优先：显式冲突下沉
    exp = judge_explicit_conflict(c_a, c_b)
    if exp:
        return {
            "is_conflict": True,
            "candidate_score": 1.0,
            "llm_score": 1.0,
            # P7.2 反例3（2026-09-11）：决策从 details 透传——规则层判 rollback 时这里
            # 【不】再硬编码 supersedes（旧写法会把回滚决策洗回取代决策，下游全跟着错）。
            "decision": exp.get("decision", "supersedes"),
            "policy_version": config["policy_version"],
            "model": "rule_explicit_version",
            "prompt_hash": "rule",
            "details": exp
        }
        
    # 步骤六：LLM 双向评分
    text_a = c_a.get("content", "")
    text_b = c_b.get("content", "")
    try:
        s_ab = call_llm_score(text_a, text_b, config["model"])
        s_ba = call_llm_score(text_b, text_a, config["model"])
        avg_score = (s_ab + s_ba) / 2.0
    except Exception as e:
        logger.error("LLM scoring failed for pair %s|%s: %s", c_a["id"], c_b["id"], e)
        return {
            "is_conflict": False,
            "candidate_score": 0.0,
            "llm_score": None,
            "decision": "error",
            "last_error": str(e),
            "policy_version": config["policy_version"],
            "model": config["model"]
        }
        
    threshold = config.get("llm_conflict_threshold", 0.65)
    is_conf = avg_score >= threshold
    return {
        "is_conflict": is_conf,
        "candidate_score": avg_score,
        "llm_score": avg_score,
        "decision": "supersedes" if is_conf else "enriches",
        "policy_version": config["policy_version"],
        "model": config["model"],
        "prompt_hash": hashlib.sha256(b"d2_bidirectional_v1").hexdigest()[:8],
        "details": {"s_ab": s_ab, "s_ba": s_ba, "threshold": threshold}
    }

def _visibility_of_pair(conn: object, c_a: dict, c_b: dict) -> str:
    """两端点最严可见性（全库唯一实现 min_visibility；查不到/未知按最严 private 兜底）。

    与 vector_store.save_evolution 同款口径：先按 id 取 visibility，再 min_visibility。
    ⚠️ 绝不硬编码 'shared'——把跨域 private 的冲突关系写成 shared，等于向无关方暴露
    "这两条记忆在互相打架"，关系本身也是信息。
    """
    from memory.visibility import min_visibility
    ids = [c_a["id"], c_b["id"]]
    try:
        rows = conn.execute(
            "SELECT id, visibility FROM chunks WHERE id IN (?,?)", (ids[0], ids[1])
        ).fetchall()
        vis_map = {r[0]: r[1] for r in rows}
    except Exception:
        vis_map = {}
    return min_visibility(ids, lambda sid: vis_map.get(sid, "private"))


def _pair_times(c: dict) -> float:
    """取该条的时间信号（content_time → updated_at → timestamp），取不到给 0。"""
    for k in ("content_time", "updated_at", "timestamp"):
        v = c.get(k)
        try:
            if v:
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _orient_ids(c_a: dict, c_b: dict, result: dict) -> Tuple[str, str]:
    """定影子边的 (source=新取代者, target=旧被取代者)——与 trace.py 文件头方向铁律同向。

    取向优先级（越靠前证据越硬，全程【不】依赖调用方传参顺序）：
    ① 规则下沉路径：judge_explicit_conflict 已算好 newer_id/older_id，直接采信
       （evaluate_pair_bidirectional 把它放进 result["details"]）。
    ② 无 details 但内容本身带显式版本序 → 就地复算规则方向。为什么要有这一档：
       落库方不该"调用方忘了传 details，方向就写歪"——审计复现脚本就是手工造的 res
       （没跑 evaluate_pair_bidirectional）；只有 ③ 的话，A=v1.2 / B=v2.0 这种最典型的
       显式版本对会退回传参顺序，等于病灶没修干净。
    ③ 时间信号（content_time → updated_at → timestamp）：LLM 双向打分 (f(a,b)+f(b,a))/2 是
       【对称】的，本身不含方向信息，故退到时间序——新的取代旧的。
    ④ ①②③ 都无证据（时间相同/缺失）→ 保留传参顺序。这里写明白：方向不再"悄悄"由扫描顺序决定。
    """
    det = result.get("details") or {}
    newer = det.get("newer_id") or result.get("newer_id")
    older = det.get("older_id") or result.get("older_id")
    if newer and older:
        return str(newer), str(older)
    # ②：caller 没走 evaluate_pair_bidirectional 也不该把方向写歪
    try:
        exp = judge_explicit_conflict(c_a, c_b)
        if exp and exp.get("newer_id") and exp.get("older_id"):
            return str(exp["newer_id"]), str(exp["older_id"])
    except Exception:
        pass
    # ③：时间序（新的取代旧的）
    if _pair_times(c_b) > _pair_times(c_a):
        return c_b["id"], c_a["id"]
    return c_a["id"], c_b["id"]


def _decision_reason_of(result: dict) -> str:
    """判定理由透传（2026-09-12 466 清创单步骤1：conflict_candidate 新写入必填）。

    消费侧白名单 get_conflict_warnings_for 只放行「reason 非空且不含 p3污染」的边，
    所以每条想外溢给用户的判定都必须带可审计理由：规则边透传规则代号，
    LLM 边透传双向均分与阈值。error 路径返回空串——UPDATE 走 CASE 回退保留旧行标记，
    【不】把 pending 行的 'p3污染回退' 清创标记洗掉，否则追溯链断裂。
    """
    if result.get("decision") == "error":
        return ""
    if result.get("model") == "same_session_flow_guard":
        # 件①b 豁免闸产物（llm_score=None，不能走下面的 %.2f 格式化）
        return "同session流水豁免(466追单件①b)"
    if result.get("model") == "rule_explicit_version":
        rule = (result.get("details") or {}).get("rule") or "explicit_version"
        return f"规则判定:{rule}"
    d = result.get("details") or {}
    avg = result.get("llm_score")
    if avg is None:
        # 兜底档：model 不在上面豁免名单且 llm_score 缺失（审计复现脚本手搓 result、
        # 或新增判定档没配 llm_score）→ 走 %.2f 会 TypeError，把 record_conflict_result
        # 连带落库整个炸掉（d2_guard 8.7 夹具实锤，2026-09-13 修）。宁缺理由不可炸。
        return "LLM分缺失"
    thr = d.get("threshold")
    return f"LLM双向均分 {avg:.2f}/{thr}"


def record_conflict_result(conn: object, c_a: dict, c_b: dict, result: dict) -> None:
    """步骤七：若判定冲突，记入 conflict_candidate 并写入 memory_evolution 影子边。

    P7.1（2026-09-11）：落库三闸见文件头修改铁律 4——方向按 newer/older 定、
    可见性走 min_visibility、重判非 supersedes 时清该 pair 旧影子边。
    """
    now = time.time()
    pkey = pair_key_of(c_a["id"], c_b["id"])
    cur = conn.cursor()
    
    # 1. 记录候选表（幂等）。decision_reason：新写入必填（466 清创单）；
    #    ON CONFLICT 时空理由【不】覆盖旧值（error 重试不能洗掉 p3污染回退标记）。
    cur.execute("""
        INSERT INTO conflict_candidate
        (pair_key, source_id, target_id, namespace, project, candidate_score, llm_score,
         decision, policy_version, model, prompt_hash, status, attempt, last_error, created_at, updated_at,
         decision_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pair_key) DO UPDATE SET
            candidate_score=excluded.candidate_score,
            llm_score=excluded.llm_score,
            decision=excluded.decision,
            policy_version=excluded.policy_version,
            model=excluded.model,
            prompt_hash=excluded.prompt_hash,
            status=excluded.status,
            attempt=conflict_candidate.attempt + 1,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at,
            decision_reason=CASE
                WHEN COALESCE(excluded.decision_reason, '') <> '' THEN excluded.decision_reason
                ELSE conflict_candidate.decision_reason END
    """, (
        pkey, c_a["id"], c_b["id"], c_a.get("namespace", ""), c_a.get("project", ""),
        result.get("candidate_score", 0.0), result.get("llm_score"),
        result.get("decision", "pending"), result.get("policy_version", ""),
        result.get("model", ""), result.get("prompt_hash", ""),
        "judged" if result.get("decision") != "error" else "failed",
        1, result.get("last_error", ""), now, now,
        _decision_reason_of(result)
    ))
    
    # 2. 若判定冲突，写入 memory_evolution 影子边 (shadow_state=1)
    if result.get("is_conflict"):
        # P7.1-①：方向 = 新取代旧（规则路径取 details.newer_id/older_id），不由传参顺序定
        src_id, tgt_id = _orient_ids(c_a, c_b, result)
        # P7.1-③：同 pair 若存在【方向不符】的旧影子边（上一轮扫描顺序 / 旧版本写歪的），先清掉
        #   再写，保证"同 pair 只有一条方向正确的影子边"。影子边从未执行（shadow_state=1），
        #   删掉不留历史债——bi-temporal 语义只对"已执行"的边有意义。
        cur.execute(
            "DELETE FROM memory_evolution WHERE shadow_state=1 AND "
            "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?)) AND "
            "NOT (source_id=? AND target_id=?)",
            (c_a["id"], c_b["id"], c_b["id"], c_a["id"], src_id, tgt_id),
        )
        exist = cur.execute(
            "SELECT id FROM memory_evolution WHERE source_id=? AND target_id=?",
            (src_id, tgt_id),
        ).fetchone()

        if not exist:
            # P7.1-②：可见性 = 两端点最严者（min_visibility），不再硬编码 'shared'
            cur.execute("""
                INSERT INTO memory_evolution
                (source_id, target_id, relation, confidence, visibility, valid_from, valid_to, shadow_state, adjudicator, policy_version, decision_reason)
                VALUES (?, ?, 'supersedes', ?, ?, ?, 0.0, 1, ?, ?, ?)
            """, (
                src_id, tgt_id, result.get("llm_score", 1.0),
                _visibility_of_pair(conn, c_a, c_b), now,
                result.get("model", ""), result.get("policy_version", ""),
                f"D2-2冲突检出: {result.get('decision')}"
            ))
    elif result.get("decision") != "error":
        # P7.1-③ 闭旧边：重判成 enriches（或其它非 supersedes 的明确结论）→ 该 pair 的旧
        #   supersedes 影子边必须失效，否则"重跑扫描覆盖"永远清不掉旧错误关系（0 残留是本条验收）。
        # ⚠️ decision == 'error'（LLM 打分抛异常）【不】删：那不是重判、是没判成——拿一次网络抖动
        #   抹掉既有判定属于数据丢失，扫下一轮自然会重判。已执行的边（shadow_state=0）【不】碰。
        cur.execute(
            "DELETE FROM memory_evolution WHERE shadow_state=1 AND "
            "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
            (c_a["id"], c_b["id"], c_b["id"], c_a["id"]),
        )

    conn.commit()

def get_conflict_warnings_for(conn: object, chunk_ids: List[str]) -> Dict[str, List[str]]:
    """为记忆注入提示词提供分歧列表：返回 {chunk_id: [counterpart_id, ...]}

    🔴 白名单过滤（2026-09-12 466 污染边清创单 · 消费侧加固）：
    provider.py 的警告装配走本函数（链：prefetch → _start_prefetch → _run → _assemble），
    它读的正是 `status='judged' AND decision='supersedes'`。p3 恒真 bug 产出的 466 条边
    曾满足该条件 ⇒ 会向真实用户贴「⚠ 与 m:xxxx 存在分歧」误导。修法按工单要求落在本函数
    （【不】在 provider 调用点硬编码）：
      · decision_reason 为空 → 不参与（历史旧行/未重判行，证据不足）；
      · decision_reason 含「p3污染」→ 不参与（清创期内已回退但缓存/旧快照残留的候选）。
    白名单语义 = 只有"带可审计理由且非污染来源"的判定才允许外溢给用户。
    """
    if not chunk_ids:
        return {}
    ph = ",".join("?" * len(chunk_ids))
    cur = conn.cursor()
    # 既查 conflict_candidate 也查 memory_evolution 影子边
    rows = cur.execute(f"""
        SELECT source_id, target_id FROM conflict_candidate
        WHERE status='judged' AND decision='supersedes'
          AND COALESCE(decision_reason, '') <> ''
          AND decision_reason NOT LIKE '%p3污染%'
          AND (source_id IN ({ph}) OR target_id IN ({ph}))
    """, tuple(chunk_ids) * 2).fetchall()
    
    mapping = {}
    for r in rows:
        s, t = r[0], r[1]
        if s in chunk_ids:
            mapping.setdefault(s, []).append(t)
        if t in chunk_ids:
            mapping.setdefault(t, []).append(s)
    return mapping
