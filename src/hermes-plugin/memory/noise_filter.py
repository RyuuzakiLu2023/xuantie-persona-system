# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · memory/noise_filter.py · D3-1 噪声切片判据（单一判据源）
#
#   ▍这是什么
#   识别「机器生成的脏切片」——模板提示词 / 蒸馏卡头 / 工具回显 / 系统注入——
#   供【建图期】剔除与【召回期】降权共用。纯规则零 LLM。
#
#   ▍为什么必须有这个文件（铁律：单一判据源）
#   建图期（graph.py）与召回期（hybrid_retriever.py）必须调用同一个函数。
#   两处各写一套正则 = 口径漂移，等两域实验量出的噪声霸榜再回来时，两边判的
#   根本不是同一批切片（同类教训：D3-2 假灰度开关、S5 双写漏 bump）。
#
#   ▍五条规则（用户 2026-09-10 裁定只做三条，R4 role 判据不做；
#              2026-09-12 D3-1 扩判据单新增 R4/R5 两类）
#   R1 前缀：Review the conversation / 蒸馏 · <domain> / [IMPORTANT: ... scheduled / CONTEXT COMPACTION
#   R2 结构：正文含 exit code / system-reminder / 工具调用残渣
#   R3 实体密度：单切片实体数 > 【同 namespace 均值】× ratio（按角色域分别算，
#              玄铁域闲聊散、专有词稀，混算会拉低均值误杀正常记忆）
#   R4 DOM 渲染残渣：带 class 属性标签数 ≥ dom_class_tag_min 且 含标签行占比 ≥ dom_tagline_ratio_min
#              （双闸同开才判噪——单看标签数会误杀"正经正文里嵌了一段 HTML 表格"的样本：
#               用户判卷 2427-A 标签 36 个 > 垃圾 2369-A 的 18 个，只有"行占比"能把两者分开）
#   R5 瞬态进程/端口状态表：markdown 表行 ≥ proctable_row_min 且 端口号(4-5位) ≥ proctable_port_min
#              且 状态标记(🟢🔴🟡/活着/已死/timeout…) ≥ proctable_status_min（三闸同开）
#               ⚠️【不】"见端口就杀"：正经工单/回执里引用单个端口的 prose 表行数为 0，天然免伤
#   R6 裸 JSON / 抓包堆密度：JSON 键值对数 ≥ json_kv_min 且 花括号数 ≥ json_brace_min（双闸同开）
#               ⚠️ 三条护栏（本单生命线，R3 曾乱标 JSON 误伤 138 条）：围栏 ``` 内不计 /
#               只认 "key": value 形态 / 双信号齐达标才算——正经文档里贴 JSON 片段不会中招
#
#   ▍开闸分法（2026-09-10 Z指导实测定案 + 2026-09-12 扩判据）：
#   - build_exclude: R1/R2/R3/R4/R5 全 true（全参与建图排除）
#   - recall_weight: R1=0.3, R2=0.3, R3=null（R3 不降权！专有词密集≠机器生成，
#                    实测用户贴配置/JSON 被 R3 命中 138 条，召回必须放过）、
#                    R4=0.3, R5=0.3（与 R1/R2 对齐；实测依据见 2026-09-12 回执五对探针表）
#   - 兼容：旧格式单键 enabled 仍可读（回落=三规则同开关）；
#   - 铁律：recall_weight 任何值 ∈(0,1]，写 0/负数/非法一律拒绝+回落 1.0 告警；
#     null 与"设 0"语义严格区分（null=跳过不降权，0 会打穿兜底被硬拒绝）。
#
#   ▍更新记录
#   *更新：2026-09-12 · D3-1 扩判据（小项二合一）：新增 R6（裸 JSON/抓包堆密度，双闸：JSON 键值对数≥4
#     且 花括号数≥3，围栏内不计 + 只认 "key": value 形态）；RULES 扩六，build_exclude/recall_weight 同步。
#     靶：判卷 2478-B（必中）+ 三类护栏（正经样本/围栏内代码/含 JSON 片段的正常文档均须放行）。*
#   *更新：2026-09-12 · D3-1 扩判据：新增 R4（DOM 渲染残渣）/ R5（瞬态进程表）两类，
#     双闸/三闸同开判噪（阈值宁松勿紧，防误杀正经记忆）；阈值全部走 _cfg 可配 + 默认兜底。
#     靶：用户判卷五对（4 垃圾全中 / 2 正经全放），守卫 tests/test_xtmem_d31_noise_r45.py。*
#   *更新：2026-09-10 · D3-1 开闸分法改造（按规则分治 build_exclude/recall_weight，小D）*
#   *创建：2026-09-10 · D3-1 噪声切片判据单一源落地（小D）*
# ================================================================

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

RULES = ("R1", "R2", "R3", "R4", "R5", "R6")

DEFAULT_BUILD_EXCLUDE: Dict[str, bool] = {
    "R1": True,
    "R2": True,
    "R3": True,
    "R4": True,
    "R5": True,
    "R6": True,
}

DEFAULT_RECALL_WEIGHT: Dict[str, Optional[float]] = {
    "R1": 0.3,
    "R2": 0.3,
    "R3": None,
    "R4": 0.3,
    "R5": 0.3,
    "R6": 0.3,
}

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": False,
    "build_exclude": DEFAULT_BUILD_EXCLUDE,
    "recall_weight": DEFAULT_RECALL_WEIGHT,
    "prefix_patterns": [
        "Review the conversation",
        "蒸馏 · ",
        "[IMPORTANT: You are running as a scheduled",
        "CONTEXT COMPACTION",
    ],
    "struct_patterns": [
        "exit code",
        "system-reminder",
    ],
    # R1 行首标记变体（2026-09-12 件②补口）：机器注入常被 `User: ` 前缀包裹，
    # 纯 startswith(prefix_patterns) 吃不到（实测 34/40 漏网）。这里按"行首标记"匹配。
    "lead_marker_patterns": [
        "[IMPORTANT: You are running as a scheduled",
        "[IMPORTANT: Background process",
    ],
    "entity_density_ratio": 2.5,
    # R4（2026-09-12）：DOM 渲染残渣双闸——两闸同开才判噪（阈值宁松勿紧）
    "dom_class_tag_min": 5,
    "dom_tagline_ratio_min": 0.30,
    # R5（2026-09-12）：瞬态进程/端口状态表四闸——表头语义 + 表行 + 端口 + 状态标记全开才判噪
    #   （2026-09-12 影响面实测后由三闸收紧为四闸：去表头闸会把玄铁域正经分析表误判成噪声）
    "proctable_row_min": 2,
    "proctable_port_min": 2,
    "proctable_status_min": 2,
    # R6（2026-09-12 小项二合一）：裸 JSON / 抓包堆密度——形态无关的双闸
    #   ⚠️ 误杀护栏是生命线（R3 曾乱标 JSON 误伤用户贴配置 138 条）：
    #   ① 围栏内（``` 代码块/配置样例）一律不计
    #   ② 必须"JSON 键值对形态"才算（"key": value），不是见花括号就杀
    #   ③ 同时要求 kv 对数与花括号数双达标（单信号不判）
    "json_kv_min": 4,
    "json_brace_min": 3,
}

# R1 信号（2026-09-13 466 追单件③）：XTMEM 自产资产页头——实体/画像/蒸馏/时间骨架。
# 行首命中即豁免 R1 prefix 分支（口径对齐 R6 的 _JSON_ASSET_GUARD_RE 自产资产豁免先例）。
_ASSET_HEADER_RE = re.compile(r'^\s*#{1,2}\s*(?:实体|画像|蒸馏|时间骨架|蒸馏结果)\s*·')

_TOOL_CALL_RE = re.compile(
    r'"(tool_call|function_call|tool_use|tool_name|tool_result)"\s*:'
    r"|\btool_call_id\b"
    r'|^\s*\{?\s*"(name|arguments)"\s*:',
    re.I | re.M,
)

# R4 信号（2026-09-12）：HTML 标签 + 带 class 属性的标签 + 围栏感知（代码块里的样例不算）
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?/?>")
_HTML_CLASS_TAG_RE = re.compile(r"<[A-Za-z][A-Za-z0-9]*\s+[^<>]*\bclass\s*=")
_FENCE_RE = re.compile(r"^\s*```")

# R5 信号（2026-09-12）：markdown 表行 + 端口号 + 状态标记
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_PORT_RE = re.compile(r"\b\d{4,5}\b")
_STATUS_RE = re.compile(r"🟢|🔴|🟡|活着|已死|超时|timeout|running|stopped", re.I)
# 表头语义闸（2026-09-12 影响面实测后补）：只有"进程/端口表"才算瞬态进程表
_PROC_HEADER_RE = re.compile(r"进程|端口|PID", re.I)

# R6 信号（2026-09-12 小项二合一）：裸 JSON / 抓包堆密度
#   形态无关：先把字面 \n / \t 还原成真实字符（2478-B 的 JSON 被压成单行字面 \n 存储），
#   否则"行占比"类判据对它天然失效（R3 当年翻车主因之一）。
_JSON_LITERAL_NL_RE = re.compile(r"\\[nt]")
# JSON 键值对形态："key": <值>（值可以是字符串/数字/布尔/null/对象/数组开头）
_JSON_KV_RE = re.compile(
    r'"[^"\n]{1,60}"\s*:\s*(?:"(?:[^"\\]|\\.)*"|[\d.\-+eE]+|true|false|null|\{|\[)'
)
_JSON_BRACE_RE = re.compile(r"[{}]")
# 🔴 R6 的"窄闸"（2026-09-12 影响面实测两轮后定案，本单生命线）：
#   判据 = 「说话人标签（用户:/Assistant:/本鱼:/User:）后【紧跟】JSON 对象/数组开头」
#          AND JSON 键值对数 ≥ json_kv_min AND 花括号数 ≥ json_brace_min。
#   为什么必须这么窄：单靠 kv/brace 密度会误伤 17 条正经片段（玄铁配置对比、curl 示例、
#   蒸馏产物、UI 选项卡……它们的 JSON 都是"先讲事、后贴数据"）；实测只有"标签后直接贴
#   运行时转储"（2478-B / dsh DOM dump / genui 组件数据）才是真残渣——误杀面 17 → 0。
_JSON_SPEAKER_RE = re.compile(r'(?:用户|Assistant|本鱼|User)\s*[:：]\s*[\'"]?\s*[\[{]')
# 护栏：XTMEM 自产资产（实体索引页 / 蒸馏结果 / 【…】标题条目）绝不算噪声
_JSON_ASSET_GUARD_RE = re.compile(r'^\s*(#\s*实体\s*·|##\s*蒸馏结果|【)')

_STRUCT_DENSE_N = 3


def _cfg(tuning: Optional[dict] = None) -> dict:
    """读 noise_filter 配置：显式传入优先，否则从 xtmem_tuning.json 读，缺项用默认。"""
    cfg = dict(DEFAULT_CFG)
    data = None
    if isinstance(tuning, dict):
        data = tuning.get("noise_filter")
    if data is None:
        try:
            from memory.tuning import load_tuning
            data = load_tuning().get("noise_filter")
        except Exception:
            data = None
    if isinstance(data, dict):
        for k, v in data.items():
            if not str(k).startswith("_"):
                cfg[k] = v
    return cfg


def _norm_build_exclude(raw: Any) -> Dict[str, bool]:
    """把 build_exclude 归一为 {R1:bool, R2:bool, R3:bool}。
    兼容旧格式：若为标量 bool/int，则三规则同值（回落行为）。
    """
    out = dict(DEFAULT_BUILD_EXCLUDE)
    if isinstance(raw, dict):
        for r in RULES:
            if r in raw and raw[r] is not None:
                out[r] = bool(raw[r])
    elif isinstance(raw, (bool, int)):
        v = bool(raw)
        out = {r: v for r in RULES}
    return out


def _norm_recall_weights(raw: Any) -> Dict[str, Optional[float]]:
    """把 recall_weight 归一为 {R1: Optional[float], R2: ..., R3: ...}。
    兼容旧格式：若为标量，则三规则赋予同一标量（旧行为）。
    注意：null/None 明确保留，表示该规则不参与召回降权。
    """
    out = dict(DEFAULT_RECALL_WEIGHT)
    if isinstance(raw, dict):
        for r in RULES:
            if r in raw:
                out[r] = raw[r]
    elif raw is not None:
        out = {r: raw for r in RULES}
    return out


def rule_of(why: str) -> Optional[str]:
    """从命中原因串提取规则代号（如 'R1:prefix:...' -> 'R1'）。"""
    if not why:
        return None
    part = str(why).split(":", 1)[0].strip().upper()
    return part if part in RULES else None


def is_build_excluded(why: str, tuning: Optional[dict] = None) -> bool:
    """【单一裁决】该规则是否参与建图期排除。
    enabled=false 时返回 False（零行为变化）；
    未命中或规则未被 build_exclude 配置排除时返回 False。
    """
    p = _cfg(tuning)
    if not p.get("enabled"):
        return False
    r = rule_of(why)
    if not r:
        return False
    ex_map = _norm_build_exclude(p.get("build_exclude"))
    return bool(ex_map.get(r, False))


def get_recall_weight(rule: Optional[str] = None, tuning: Optional[dict] = None) -> Optional[float]:
    """获取指定规则的召回降权系数——只能取 (0, 1] 或 None。
    
    入参：
      rule: 'R1' | 'R2' | 'R3' 或 None（旧式调用）
      tuning: 显式配置字典
    返回：
      Optional[float]: 浮点降权系数（如 0.3）；
      None: 该规则不参与召回降权（如 R3: null），召回打分逐字节跳过。
    
    【铁律】任何降权数值永远不许设 0（设 0 导致有效记忆掉线时失去兜底）。
    若配置了 0、负数、>1 或非数值，一律拒绝并回落 1.0 + 告警！
    
    兼容性约定：
      - 若 rule 位置传入 dict 且 tuning 为空，自动按 tuning 识别（防历史调用错位）；
      - 若未指定 rule（rule 为 None），按旧版单键逻辑检查：若三规则一致则返回该值，否则回落 1.0。
    """
    if isinstance(rule, dict) and tuning is None:
        tuning = rule
        rule = None

    p = _cfg(tuning)
    weights = _norm_recall_weights(p.get("recall_weight"))

    target_rule = str(rule).upper() if rule else None
    if target_rule and target_rule in RULES:
        raw = weights.get(target_rule)
    elif target_rule is None:
        vals = list(weights.values())
        if len(set(vals)) == 1:
            raw = vals[0]
        else:
            raw = weights.get("R1")
    else:
        return None

    if raw is None:
        return None

    try:
        w = float(raw)
    except (TypeError, ValueError):
        logger.warning("noise_filter.recall_weight[%s] 非数值(%r) — 已拒绝，回落 1.0（不降权）", target_rule, raw)
        return 1.0

    if not (0.0 < w <= 1.0):
        logger.warning(
            "noise_filter.recall_weight[%s]=%r 非法（只允许 (0,1]；设 0 会让噪声在所有有效记忆"
            "掉线时也永不复活=失去兜底）— 已拒绝，回落 1.0（不降权）", target_rule, raw
        )
        return 1.0

    return w


# 对话前缀（2026-09-12 件②）：机器注入切片落库时常带 `User: `/`用户：` 等说话人标签包裹，
# 纯标点剥离吃不掉词前缀 → 34/40（实测真货 fixture 4/40）漏网。剥完可能还有内层引号，
# 所以与标点剥离放进同一个循环交替剥。⚠️ 会连「引用了注入原文」的记忆一起判噪——
# 标记引导整行才算，句中引用不受影响；这是 R1 的本职（注入原文本身就该进噪声池）。
_SPEAKER_PREFIX_RE = re.compile(
    r"^(?:user|assistant|human|ai|system|助手|用户|本鱼|系统|小D|Z指导|用户)\s*[:：]\s*",
    re.I,
)


def _marker_leads_line(text: str, marker: str) -> bool:
    """该标记是否【起一行】（剥掉空白/标点/引号/代码块符号/说话人标签后行首等于/起于该标记）。

    ⚠️ 匹配必须在剥离【前】先试：旧版先剥标点再匹配，而剥离字符集里含 `[`，
    会把 `[IMPORTANT:` 的方括号剥成 `IMPORTANT:` —— 连裸行首标记都吃不到（件②真凶之二）。
    """
    for raw_line in (text or "").split("\n"):
        line = raw_line.lstrip()
        while line:
            if line.startswith(marker):
                return True
            if line[0] in '\'"`([{<>-*#\t ':
                line = line[1:]
                continue
            m = _SPEAKER_PREFIX_RE.match(line)
            if m:
                line = line[m.end():].lstrip()
                continue
            break
    return False


def _dom_residue_signals(text: str) -> Tuple[int, float]:
    """R4 信号（2026-09-12）：返回 (带 class 属性标签数, 含标签行占比)。

    占比只数围栏（```）外的行——代码块里的 HTML 示例属"正经引用"不算残渣。
    为什么要两个信号：单看标签数会误杀"正文里嵌了一段 HTML 表格"的正经切片
    （用户判卷 2427-A 标签 36 个，比垃圾 2369-A 的 18 个还多），行占比才是分界线。
    """
    raw = text or ""
    lines = raw.split("\n")
    in_fence = False
    tag_lines = 0
    counted = 0
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        counted += 1
        if _HTML_TAG_RE.search(ln):
            tag_lines += 1
    cls_tags = len(_HTML_CLASS_TAG_RE.findall(raw))
    ratio = (tag_lines / counted) if counted else 0.0
    return cls_tags, ratio


def _proc_table_signals(text: str) -> Tuple[int, int, int, bool]:
    """R5 信号（2026-09-12）：返回 (markdown 表行数, 端口号数, 状态标记数, 表头是否含进程/端口语义词)。

    表头语义词闸是 2026-09-12 影响面实测后补的——只凭"表行+端口+状态"三闸会把玄铁域
    正经技术分析（表格里夹端口数字）一并判噪（实测误伤）。表头写着「进程/端口/PID」
    才是「进程状态表」，其余表格（对比表/路径表）端口再多也不是。
    """
    raw = text or ""
    rows = _MD_TABLE_ROW_RE.findall(raw)
    header = rows[0] if rows else ""
    return (len(rows),
            len(_PORT_RE.findall(raw)),
            len(_STATUS_RE.findall(raw)),
            bool(_PROC_HEADER_RE.search(header)))


def _json_blob_signals(text: str) -> Tuple[int, int, bool, bool]:
    """R6 信号（2026-09-12）：返回 (JSON 键值对数, 花括号数, 说话人标签后紧跟 JSON, 自产资产护栏)。

    护栏（本单生命线，缺一不可）：
      ① 自产资产护栏：实体索引页 / 蒸馏结果 / 【…】条目 = 合法资产，直接排除；
      ② 窄闸：必须是"说话人标签后紧跟 JSON 开头"（用户直接贴运行时转储），
         光有 JSON 密度不够——正经文档"先讲事、后贴数据"一律放行（实测相差 17 条）；
      ③ 调用方再要求 kv 与 brace 双达标（单信号不判）。
    形态无关：字面 \\n/\\t 先还原（2478-B 的 JSON 被压成单行字面转义存储，
    只按真实换行分行会让行占比类判据完全失效——R3 翻车主因之一）。
    """
    raw = _JSON_LITERAL_NL_RE.sub("\n", text or "")
    guarded = bool(_JSON_ASSET_GUARD_RE.match(raw))
    speaker = bool(_JSON_SPEAKER_RE.search(raw))
    return len(_JSON_KV_RE.findall(raw)), len(_JSON_BRACE_RE.findall(raw)), speaker, guarded


def _strip_fences(text: str) -> str:
    """剥掉 ``` 围栏块（含内容），返回围栏外文本。

    2026-09-12 件②抽出公共函数：R2 的 tool-call-json 与 R4 行占比都要"围栏外"语义，
    各写一份必漂移（同类教训：判据单一源铁律）。代码块里的 JSON/工具示例属"正经引用"。
    """
    out = []
    in_fence = False
    for ln in (text or "").split("\n"):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(ln)
    return "\n".join(out)


def is_noise_text(content: str, patterns: Optional[dict] = None) -> Tuple[bool, str]:
    """R1+R2+R4+R5+R6 纯文本判据（不含 R3 实体密度）。返回 (是否噪声, 命中原因)。"""
    text = content or ""
    if not text.strip():
        return (False, "")
    p = patterns or _cfg()
    head = text.lstrip()

    # R1 前缀类（⚠️ 466 追单件③ 2026-09-13：XTMEM 自产资产页豁免——实体/画像/蒸馏/
    # 时间骨架页的正文常引用「蒸馏 · 」开头的历史条目（近因 Top3 等），prefix-repeat
    # 的 count>=2 会把同页引用当噪声信号（mem-4d66d183 实体页误杀实锤，count=2）。
    # 口径对齐 R6 的 _JSON_ASSET_GUARD_RE 先例：自产资产头行首命中 → 本分支整体豁免。
    # 行首标记类（lead_marker）【不】豁免——资产页里引用机器注入原文仍属噪声证据。）
    asset_page = bool(_ASSET_HEADER_RE.match(text))
    for pat in list(p.get("prefix_patterns") or []):
        if not pat or asset_page:
            continue
        s = str(pat)
        if head.startswith(s):
            return (True, "R1:prefix:" + s)
        if text.count(s) >= 2:
            return (True, "R1:prefix-repeat:" + s)

    # R1 行首标记类（2026-09-12 件②补口）：机器注入常被对话前缀包裹
    # （`User: [IMPORTANT: ...`），纯 startswith 吃不到 → 复用 _marker_leads_line。
    for pat in list(p.get("lead_marker_patterns") or []):
        if not pat:
            continue
        s = str(pat)
        if _marker_leads_line(text, s):
            return (True, "R1:lead-marker:" + s)

    # R2 结构类
    for pat in list(p.get("struct_patterns") or []):
        if not pat:
            continue
        s = str(pat)
        if s not in text:
            continue
        if _marker_leads_line(text, s):
            return (True, "R2:struct-lead:" + s)
        if text.count(s) >= _STRUCT_DENSE_N:
            return (True, "R2:struct-dense:" + s)

    # R2 工具调用残渣（JSON 形态）——⚠️ 2026-09-12 件②：在**剥围栏后**的文本上匹配，
    # 代码块里的 JSON/工具示例属正经引用（护栏：mem-db1e840c 必须放行）。
    if _TOOL_CALL_RE.search(_strip_fences(text)):
        return (True, "R2:tool-call-json")

    # R4 DOM 渲染残渣（双闸同开：带 class 标签数 + 含标签行占比）
    if bool(p.get("dom_residue_enabled", True)):
        cls_tags, tag_ratio = _dom_residue_signals(text)
        if (cls_tags >= int(p.get("dom_class_tag_min", 5))
                and tag_ratio >= float(p.get("dom_tagline_ratio_min", 0.30))):
            return (True, "R4:dom-residue:cls=%d,ratio=%.2f" % (cls_tags, tag_ratio))

    # R5 瞬态进程/端口状态表（四闸同开：表头语义 + 表行 + 端口号 + 状态标记）
    if bool(p.get("proc_table_enabled", True)):
        rows, ports, stats, hdr_ok = _proc_table_signals(text)
        if (hdr_ok
                and rows >= int(p.get("proctable_row_min", 2))
                and ports >= int(p.get("proctable_port_min", 2))
                and stats >= int(p.get("proctable_status_min", 2))):
            return (True, "R5:proc-table:rows=%d,ports=%d,status=%d,hdr=1" % (rows, ports, stats))

    # R6 裸 JSON / 抓包堆（窄闸：说话人标签后紧跟 JSON + 双密度达标；自产资产豁免）
    if bool(p.get("json_blob_enabled", True)):
        kv, brace, speaker, guarded = _json_blob_signals(text)
        if (speaker and not guarded
                and kv >= int(p.get("json_kv_min", 4))
                and brace >= int(p.get("json_brace_min", 3))):
            return (True, "R6:json-blob:kv=%d,brace=%d" % (kv, brace))

    return (False, "")


def entity_density_thresholds(conn: sqlite3.Connection, ratio: float = 2.5) -> Dict[str, float]:
    """按 namespace 分域算实体密度阈值（= 该域均值 × ratio）。"""
    out: Dict[str, float] = {}
    try:
        rows = conn.execute(
            "SELECT COALESCE(c.namespace,'') ns, COUNT(DISTINCT c.id) nchunks, "
            "COUNT(ce.chunk_id) nlinks "
            "FROM chunks c LEFT JOIN chunk_entities ce ON ce.chunk_id = c.id "
            "WHERE c.state='active' GROUP BY COALESCE(c.namespace,'')"
        ).fetchall()
    except Exception:
        logger.debug("entity_density_thresholds failed (non-fatal)", exc_info=True)
        return out
    for r in rows:
        try:
            ns = str(r[0] or "")
            nch = int(r[1] or 0)
            nlink = int(r[2] or 0)
        except Exception:
            continue
        if nch <= 0:
            continue
        out[ns] = (nlink / nch) * float(ratio)
    return out


def is_noise_chunk(content: str,
                   namespace: str = "",
                   chunk_id: Optional[str] = None,
                   conn: Optional[sqlite3.Connection] = None,
                   tuning: Optional[dict] = None,
                   density_thresholds: Optional[Dict[str, float]] = None) -> Tuple[bool, str]:
    """【单一判据源】判断一个切片是否为机器生成噪声。返回：(是否噪声, 原因)。"""
    p = _cfg(tuning)
    if not p.get("enabled"):
        return (False, "")

    hit, why = is_noise_text(content, p)
    if hit:
        return (True, why)

    ratio = float(p.get("entity_density_ratio", 2.5) or 2.5)
    if conn is not None and chunk_id:
        try:
            if density_thresholds is None:
                density_thresholds = entity_density_thresholds(conn, ratio)
            thr = density_thresholds.get(str(namespace or ""))
            if thr is not None and thr > 0:
                n_ent = conn.execute(
                    "SELECT COUNT(*) FROM chunk_entities WHERE chunk_id=?", (chunk_id,)
                ).fetchone()[0]
                if n_ent > thr:
                    return (True, "R3:density:%d>%.2f" % (int(n_ent), thr))
        except Exception:
            logger.debug("R3 density check failed for %s (non-fatal)", chunk_id, exc_info=True)

    return (False, "")


def noise_chunk_ids(conn: sqlite3.Connection,
                    tuning: Optional[dict] = None,
                    limit: int = 200000) -> List[str]:
    """批量识别噪声 chunk id（建图期剔除用；尊重 build_exclude 各规则设置）。只读。"""
    p = _cfg(tuning)
    if not p.get("enabled"):
        return []
    ex_map = _norm_build_exclude(p.get("build_exclude"))
    if not any(ex_map.values()):
        return []

    ratio = float(p.get("entity_density_ratio", 2.5) or 2.5)
    thr_map = entity_density_thresholds(conn, ratio)
    out: List[str] = []
    try:
        rows = conn.execute(
            "SELECT id, content, COALESCE(namespace,'') ns FROM chunks "
            "WHERE state='active' LIMIT ?", (int(limit),)
        ).fetchall()
    except Exception:
        logger.debug("noise_chunk_ids failed (non-fatal)", exc_info=True)
        return []
    for r in rows:
        try:
            cid = str(r[0])
            content = r[1]
            ns = str(r[2] or "")
        except Exception:
            continue
        hit, why = is_noise_chunk(content, namespace=ns, chunk_id=cid, conn=conn,
                                  tuning={"noise_filter": p}, density_thresholds=thr_map)
        if hit:
            r_name = rule_of(why)
            if r_name and ex_map.get(r_name, False):
                out.append(cid)
    return out
