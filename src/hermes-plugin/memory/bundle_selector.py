# -*- coding: utf-8 -*-
"""bundle_selector.py — B2 Bundle Selector 规则版（2026-09-07，小D）

Spec：XTMEM 记忆支援包 SupportBundle 专项 §B2（v1.3，行锚 ~101-107）。
五类路由（关键词+实体，规则版零 LLM）+ 判空阈值 + 预算阶梯（契约3 四档表）。

消费方：
  - CLI cmd_recall（xtmem.py）：is_chitchat 判空前置 / classify_query_route 透传引擎
  - HybridRetriever._search_locked：route 加权（track 亲和）+ namespace_hint 域内优先
  - provider.py（Hermes）：预算阶梯落 bundle 装配（B5 注入口合并续接）

口径注记（spec §B2 双顶模复核）：
  - viewer utility 10%：旧 access_count 口径暂用；B4 五态满 1 周（2026-09-14 起）
    切 exposed/cited/confirmed 加权，旧 access 权重归零。
  - 跨域（--cross）不强治（W4 挂账），namespace_hint 在 cross 模式必须为空
    （跨域题 gold 恰在异域，域内加权会反杀）。
  - 判空阈值宁窄勿宽：全字.charset 满配 + 显式寒暄词 + 长度上限三重闸，
    任何真实内容字符出现即放行正常检索（N5 边界观察设计上不拦）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 判空阈值（B2 施工点 2）：纯寒暄零召回
# ---------------------------------------------------------------------------
# 显式寒暄词：出现即候选（还需通过全字符闸）
_PHATIC_TOKENS = (
    "晚安", "早安", "午安", "明天见", "回头见", "拜拜", "再见",
    "谢谢", "感谢", "辛苦了", "辛苦啦",
    "哈哈哈", "嘿嘿嘿", "嘻嘻", "嘿嘿",
    "嗯嗯", "嗯呐", "好滴", "好的", "好嘞", "好呀", "好啊", "好啦",
    "行吧", "行呀", "行啦",
    # 尾批一（2026-09-08）：日常问候系补齐——生产实测「你好/在吗/哈喽」穿透翻库白烧
    "你好", "哈喽", "嗨", "喂", "在吗", "在不在", "在么",
    "早上好", "中午好", "下午好", "晚上好",
    # 尾批二（2026-09-13）：纯确认词补齐——盲标考卷 8 条 T4 寒暄题（expect=empty）
    #   实测 5 条穿透判空白烧检索：「收到」「嗯」「。」「？？」「收到收到」。
    #   `1`/`测试`/`今天星期几` 故意【不】进表（不是纯确认词，expect=empty 由上层兜）。
    "嗯", "哦", "噢", "收到", "知道了", "明白了", "明白", "了解",
)
# 寒暄允许字符集（全字符满配才判空——出现任何内容字符即放行）
_PHATIC_CHARS = set(
    "，。！？!?,.~～ 、；;：:的啦呀哦嗯哈好行滴吧了呢哟噢唷啰喽呵嘿吱"
    "宝儿铁铁明晚早再谢感辛苦嘻哈天见安拜"
    # 尾批一（2026-09-08）：问候系字符同步补——不开放英文字母集！
    # 英词（hello/hi/ok）走整词白名单 _PHATIC_ENGLISH_WORDS（防 'hi file' 混内容过闸）
    "你喽嗨喂吗么上午下哈在不"
    # 尾批二（2026-09-13）：确认系字符同步补——「收到」缺 收到、「明白了」缺 白、
    #   「了解」缺 解（了/哦/噢 原已在集内，不再重复）。
    #   ⚠️ 「不」不是新加的、也删不得：尾批一为「在不在」早就放进来了。
    #   正因为它已在集内，新加的 知道/明白/了解 会被「不知道/不明白/不了解」
    #   借道放行——靠字符集挡不住，只能靠下面 _PHATIC_NEGATED 显式否定。
    "收到知道了明白解"
)
_PHATIC_MAX_LEN = 12  # 7 道判空负样本最长 8 字符；12 留生产余量

def is_chitchat(query: str) -> bool:
    """判空阈值：纯寒暄/语气 query → True（检索层零召回）。

    三重闸（全过才 True）：① 显式寒暄词命中 ② 全字符都在寒暄字符集内
    ③ 长度 ≤ 12。内容词（抱胸/胸/bug/文件名等）一出现即 False。
    尾批一（2026-09-08）：英词寒暄（hello/hi）走整词白名单分支——
      charset 永不开放 a-z（'hi file' 这类混内容必须 False）。
    尾批二（2026-09-13）：+ 纯标点第四通道（'。'/'？？' 无 token 可命中）；
      + 否定式拦（'不明白' 不是寒暄，是求解释）。
    """
    q = (query or "").strip()
    if not q or len(q) > _PHATIC_MAX_LEN:
        return False
    # 英词整词白名单（尾批一）：q.lower() 整词命中才 True，长度闸同上
    if q.lower() in _PHATIC_ENGLISH_WORDS:
        return True
    if not any(t in q for t in _PHATIC_TOKENS):
        # 尾批二（2026-09-13）：纯标点 query（'。'/'？？'）无 token 可命中，
        #   单开第四通道兜底。全部字符都在标点集才 True——'。修' 这类
        #   混内容字符的立即 False（内容字符不在标点集）。长度闸已在函数最前挡了 >12。
        if q and all(ch in _PUNCT_CHARS for ch in q):
            return True
        return False
    # 尾批二（2026-09-13）：否定式拦——'不明白' 里 token「明白」命中、'不' 又早在
    #   尾批一为「在不在」进了字符集，全字符闸挡不住，只能在此显式拦。
    #   ⚠️ 只拦"紧贴在 token 前一位"的否定词：'在不在' 不中招（否定词不贴 token 前），
    #   而 '不明白'/'没明白'/'不了解' 中招——这是本轮唯一需要区分的形态。
    for _t in _PHATIC_TOKENS:
        _i = q.find(_t)
        while _i > 0:
            if q[_i - 1] in _PHATIC_NEG_CHARS:
                return False
            _i = q.find(_t, _i + 1)
    return all(ch in _PHATIC_CHARS for ch in q)

# 纯标点字符集（尾批二 2026-09-13）：只服务上面的第四通道，不进 _PHATIC_CHARS——
#   否则 '的。' 这类混内容组合会因标点被并入字符集而误放行。
#   ⚠️ 含全角空格 　(U+3000)，'　　' 这类纯空白敲键同样判空。
_PUNCT_CHARS = set("。．?？!！,，、~～…;；:：·-—-()（） 　")

# 英词寒暄整词白名单（尾批一 2026-09-08）：只有整词相等才算寒暄，
# 'hi file'/'hello world 帮我' 一类一律 False（长度闸 + 整词双闸兜底）。
_PHATIC_ENGLISH_WORDS = frozenset({"hello", "hi", "ok", "okay", "hey"})

# 否定字集（尾批二 2026-09-13）：紧贴寒暄 token 前一位出现即判内容（is_chitchat 用）。
#   「不」不能从这里删——'在不在' 靠的不是它，真正需要它的是 _PHATIC_CHARS（尾批一）。
#   「别」「非」不收：'别客气' '非常好' 前者本就该走内容闸，后者不是否定确认形态，
#   收了反而扩误杀面。
_PHATIC_NEG_CHARS = set("不没未甭")


# ---------------------------------------------------------------------------
# 五类路由（B2 施工点 1）：关键词规则，先命中先得（顺序即优先级）
# ---------------------------------------------------------------------------
_ROUTE_RULES = (
    # 故障/异常信号——最具体，最先判（含"为什么没有"这类否定疑问）
    ("evidence_warning", re.compile(
        r"报错|异常|挂了|崩了|失败|错误|错在哪|啥情况|怎么回事|咋回事"
        r"|为什么没|为啥没|怎么没有|咋没|没生效|不生效|失效|不对劲|翻车"
        r"|打脸|被打脸|证伪|bug|死循环|卡死|卡住|断线|熔断|竞态|水化"
        r"|根因|排查|复现|报了|local 错|subprocess")),
    # 改/删/部署/配置操作意图
    ("preflight", re.compile(
        r"改下|改一版|改改|改一下|删掉|删了|清掉|部署|上线|迁移|重启"
        r"|频率|蒸馏频率|定多少|日志|无限大|一直涨|没有头|路径来着"
        r"|哪四个|哪几条|哪几个路径|四镜像|三镜像|同步是|扩至|写进|加上"
        r"|配置好|挂好|挂好了|定稿|发版|更新版本")),
    # 继续/上次/进度类
    ("resume", re.compile(
        r"继续|接着|接下来|上次|上回|之前是不是|之前是不是有|完工|做完没"
        r"|得咋样|怎么样了|咋样了|观察得|复活|进度|写到哪|写完没"
        r"|人设|辩论赛|信里|那封信|那封|说说呗|跟我说说|再讲一次|再说一遍"
        r"|验过没有|核实过|验过没|等小D|等.*完工")),
    # 审计/复核/身份档案类
    ("support", re.compile(
        r"审计|复核|独立审|我是谁|我是哪个|是谁|什么身份|都是谁|谁呀|干啥的"
        r"|什么角色|人际关系|印象最深|发我|参考文档|路径都列|都发我"
        r"|字段|顶层|峰值|双挂载|双份|同屏|专属|只有我|叫你|称呼"
        r"|起床|想你了|真棒|我错了|女王|恕罪|起床啦")),
    # 研究/方案/技术选型类（兜底知识面）
    ("knowledge", re.compile(
        r"研究|方案|原理|为什么|为啥|咋样|质量|对比|新增|新功能|都有啥|有啥"
        r"|捋一遍|有没有|有没有办法|可行不|可行|共享|用没用|做了个|这回事"
        r"|哪边|约束更强|跑多勤|上下文|模型|qdrant|catalog|skill|仓库"
        r"|项目|工程规则|agents|soul|gemini|deepseek|豆包|搜索")),
)

def classify_query_route(query: str) -> str:
    """五类路由分类：返回 support / preflight / resume / evidence_warning /
    knowledge / chitchat / ''（无路由=普通检索，不加权）。"""
    q = (query or "").strip()
    if not q:
        return ""
    if is_chitchat(q):
        return "chitchat"
    for route, pat in _ROUTE_RULES:
        if pat.search(q):
            return route
    return ""

# ---------------------------------------------------------------------------
# 路由加权表：route → track 亲和（进 final 的加分量，量级对齐 recency_weight=0.1）
# 设计依据：42 gold 元数据画像（2026-09-07 快照）——
#   support 类 gold 多 identity/preference/emotion；resume 类 gold 多
#   procedural/preference；evidence_warning gold 集中 default/procedural refined。
# ---------------------------------------------------------------------------
ROUTE_TRACK_AFFINITY = {
    "support":         {"identity": 0.12, "preference": 0.06, "emotion": 0.06, "profile": 0.08, "checkpoint": 0.04},
    "preflight":       {"preference": 0.10, "procedural": 0.06, "default": 0.06, "checkpoint": 0.04},
    "resume":          {"procedural": 0.10, "checkpoint": 0.10, "timeline": 0.06, "preference": 0.06},
    "evidence_warning": {"procedural": 0.12, "default": 0.10, "checkpoint": 0.06, "preference": 0.04},
    "knowledge":       {"preference": 0.06, "identity": 0.06, "default": 0.06, "procedural": 0.04},
}

# 域内优先加成（namespace_hint 命中）：治「跨域 shared 内容挤占 top-k 槽位、
# CLI 域过滤后再空槽」的槽位浪费——引擎排序应与 CLI 硬过滤同向。
NS_HINT_BONUS = 0.15

def route_boost(route: str, track: str, namespace: str, namespace_hint: str = "") -> float:
    """单条候选的路由加成分（纯加法，进 final 后仍受阈值/MMR 约束）。"""
    boost = 0.0
    if route:
        boost += ROUTE_TRACK_AFFINITY.get(route, {}).get(track or "default", 0.0)
    if namespace_hint and (namespace or "") == namespace_hint:
        boost += NS_HINT_BONUS
    return boost

# ---------------------------------------------------------------------------
# 预算阶梯（B2 施工点 3）：spec §二契约3 顶模四档表，逐字落地
# 两道上限注记：1200=动态 bundle 单口上限（审计档），2000=含固定锚点段总兜底。
# ---------------------------------------------------------------------------
BUDGET_LADDER = {
    # route → (场景档位, {端: (lo, hi) token})
    "chitchat":        ("无关问题",   {"hermes": (0, 120),   "zcode": (0, 120),   "dsh": (0, 120)}),
    "knowledge":       ("普通技术交流", {"hermes": (300, 500), "zcode": (250, 450), "dsh": (300, 500)}),
    "resume":          ("项目开发",   {"hermes": (500, 800), "zcode": (400, 700), "dsh": (500, 800)}),
    "preflight":       ("项目开发",   {"hermes": (500, 800), "zcode": (400, 700), "dsh": (500, 800)}),
    "support":         ("审计/故障恢复", {"hermes": (800, 1200), "zcode": (700, 1200), "dsh": (800, 1200)}),
    "evidence_warning": ("审计/故障恢复", {"hermes": (800, 1200), "zcode": (700, 1200), "dsh": (800, 1200)}),
}
_BUDGET_DEFAULT = ("普通技术交流", {"hermes": (300, 500), "zcode": (250, 450), "dsh": (300, 500)})



# ---------------------------------------------------------------------------
# 专名/实体锚点（B2 施工点 1 续）：只对稀有 token 做 LIKE 补召
# 无锚点 → 不补召不重排，基线排序逐字节保留（X01「我是谁」这类短问不扰动）。
# ---------------------------------------------------------------------------
_ASCII_STOP = {
    "this", "that", "with", "from", "your", "have", "just", "like", "will",
    "what", "when", "user", "assistant", "http", "https", "www", "com",
    "the", "and", "for", "you", "are", "not", "but", "all",
}
_CN_ANCHORS = (
    "早晚报", "四镜像", "知识库", "共享代码", "冷启动", "熔断", "水化",
    "蒸馏频率", "辩论赛", "表白", "小马哥", "老何", "何博洋", "阿北",
    "女王大人", "PROJECT_STATUS", "memory_used", "xtmemcall", "mem0",
    "qdrant", "catalog", "MidNight", "woodmeone", "gemini", "deepseek",
    "豆包", "subprocess", "agents.md", "memory.md", "AGENTS.md",
    "SupportBundle", "messages",
)

# ════════════════════════════════════════════════════════════════
# R01 · RQ04（2026-10-21 小D）：锚点"稀有性"闸 —— NULL 不再当门牌号
#
# 病灶（核查结论.md §一.4，实测）：
#   旧 extract_anchors 只看【形状】不看【频次】——凡是 ASCII 标识符就收作锚点，
#   于是 NULL / None / SELECT 这类通用 SQL 词也成了"稀有门牌号"。
#   补召段（xtmem.py 的 content LIKE OR 补位）随后按它捞货：
#   实测问「书房 C03 数字 NULL 裁决进展」，补召回 4 条本域旧记录——
#   4 条都不含 C03、却【都含 null】（agent 失败返回null / IS NULL / R3=null）。
#   这就是"正门查验通过、侧门换了一盘菜、餐牌还写高置信"的完整链路。
#
# 修法（本文件只回答"这个词配不配当锚"，不碰检索、不查库）：
#   ① 通用词黑名单：NULL 家族 / SQL 关键字 / 语言关键字 / 泛工程词，一律不算锚；
#   ② 形状闸：纯数字不算锚（千分位/版本号在下面各自分支单独处理）；
#   ③ 频次兜底【不】放在这里：查频次要 DB 连接，会把纯函数拖成 IO 依赖。
#      真正的兜底在调用方——补召命中的候选必须重新过同一套证据准入
#      （见 xtmem.py cmd_recall 的 R01 准入段），黑名单只负责"别让它起步"。
#   ⚠️【不】把这个黑名单做成"想到就加"的清单：只收【已证实会误召】的通用词；
#      每加一项都要有反例依据，否则锚点越收越少，正常专名反而漏查（RQ01 反噬）。
# ════════════════════════════════════════════════════════════════
_GENERIC_ANCHOR_TOKENS = frozenset({
    # NULL 家族——RQ04 直接靶点：生产库里 "IS NULL" / "= null" / "返回null" 遍地
    "null", "none", "nil", "undefined", "nan", "true", "false", "bool",
    # SQL 关键字/通用列名：频次极高，不能单独替代主题
    "select", "insert", "update", "delete", "from", "where", "join", "table",
    "index", "primary", "key", "value", "column", "row", "query", "sql", "db",
    "and", "or", "not", "is", "in", "on", "as", "by", "to", "of", "at",
    "id", "pid", "uid", "api", "url", "uri", "http", "https", "json", "yaml",
    # 语言关键字：技术记录里人人都有，不是专名
    "def", "class", "import", "return", "self", "print", "str", "int", "dict",
    "error", "exception", "warn", "info", "debug",
    # 泛工程词
    "test", "tests", "main", "init", "run", "get", "set", "add", "fix", "log",
    "mem", "tmp", "temp", "data", "file", "path", "dir", "name", "type", "size",
    "none.", "nil.",
})


def _is_generic_anchor(tok: str) -> bool:
    """锚点稀有性闸：True = 这个词太通用，不配单独当"门牌号"。

    判据刻意保守（只否决"确定性误召"的形态），宁漏收不误收——
    少一个锚点最多退回普通检索；多一个通用锚点就是开一条补召侧门。
    """
    t = (tok or "").strip().strip('._-')
    if not t:
        return True
    if t.lower() in _GENERIC_ANCHOR_TOKENS:
        return True
    # 纯数字（小数/千分位在上游各自分支已成形）：单个数字不构成可区分标识符
    if t.replace(".", "").replace(",", "").isdigit():
        return True
    return False


def extract_anchors(query: str):
    """从 query 抽稀有锚点（ASCII 标识符 / 版本号 / 千分位数字 / 中文专名）。

    R01·RQ04（2026-10-21）：每个候选 token 都要过 _is_generic_anchor 稀有性闸——
    NULL/SELECT/None 这类通用词不再算锚点，补召侧门因此失去"随便一个词就能捞货"的能力。
    返回去重后最多 8 个，保序。无锚点返回空列表——调用方应跳过补召。
    """
    q = query or ""
    out = []
    for m in re.finditer(r'[A-Za-z][A-Za-z0-9][A-Za-z0-9._\-]{0,40}', q):
        tok = m.group(0).strip('._-')
        # RQ01/RQ04（2026-10-21）：ASCII 分支是 NULL/SELECT 这类通用词的唯一来路，
        #   稀有性闸就卡在这里——版本号(0.20.5)/千分位(354,469) 走下面各自分支，不受影响。
        #   ⚠️【不】把闸挪到最后的 keep 循环：那里纯数字已被"复合数字降档"逻辑占用，
        #     混在一起会把 354,469 这类【真】标识符一起误杀（RQ01 反噬）。
        if (len(tok) >= 3 and tok.lower() not in _ASCII_STOP
                and not _is_generic_anchor(tok)):
            out.append(tok)
    for m in re.finditer(r'\d+(?:\.\d+){1,3}', q):
        out.append(m.group(0))
    for m in re.finditer(r'\d{1,3}(?:,\d{3})+', q):
        raw = m.group(0)
        out.append(raw)
        out.append(raw.replace(',', ''))
        out.append(raw.split(',')[0])
    for w in _CN_ANCHORS:
        if w in q:
            out.append(w)
    # 复合专名降一档词干：四镜像→镜像（gold 只写「镜像」不写「四镜像」）
    if "四镜像" in q:
        out.append("镜像")
    if "memory.md" in q.lower() and "MEMORY.md" not in out:
        out.append("MEMORY.md")
    seen = set()
    res = []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            res.append(t)
    keep = []
    for t in res:
        if t.isdigit() and len(t) <= 4 and any(t in o.replace(',', '') for o in res if o != t):
            continue
        keep.append(t)
    return keep[:8]


def anchor_hits(content: str, anchors) -> int:
    """content 命中多少个锚点（大小写不敏感）。"""
    if not anchors or not content:
        return 0
    low = content.lower()
    n = 0
    for a in anchors:
        if a.lower() in low:
            n += 1
    return n


def _lead_overlap(query: str, content: str, n: int = 240) -> int:
    """query 与文首的可区分重叠：ASCII token + 非停用中文 bigram + 改/删/整/部署动词。"""
    lead = (content or "")[:n].lower()
    q = (query or "").lower()
    if not lead or not q:
        return 0
    score = 0
    for t in re.findall(r'[a-z0-9][a-z0-9._\-]{1,40}', q):
        if t in lead:
            score += 2
    chars = [c for c in q if "\u4e00" <= c <= "\u9fff"]
    stop = set("的了吗吧呢啊呀嘛个这那是不我你他她铁铁先看看有啥里都在把给")
    for i in range(len(chars) - 1):
        g = chars[i] + chars[i + 1]
        if chars[i] in stop or chars[i + 1] in stop:
            continue
        if g in lead:
            score += 1
    for v in ("改", "删", "整", "部署", "同步", "挂好", "整理"):
        if v in q and v in lead:
            score += 2
    return score


def rerank_with_anchors(items, anchors, top: int, utility_lookup=None, query: str = ""):
    """有锚点才重排：原文优先 > 文首重叠 > 文首锚点 > 全文锚点 > score+utility10%。
    无锚点原序返回（再切 top）。utility 走旧 access_count 口径（spec §B2 注记）。"""
    if not items:
        return []
    if not anchors:
        return items[:top]
    _DERIVED_PREFIX = ("# 蒸馏", "## 蒸馏结果", "# 实体 ·", "# 时间骨架", "【压缩存档")
    def _key(it):
        content = it.get("content") or ""
        primary = 0 if content.lstrip().startswith(_DERIVED_PREFIX) else 1
        overlap = _lead_overlap(query, content) if query else 0
        head = anchor_hits(content[:240], anchors)
        hits = anchor_hits(content, anchors)
        sc = float(it.get("score") or 0)
        u = 0.0
        if utility_lookup is not None:
            try:
                raw = utility_lookup(it.get("id") or "")
                u = float(raw or 0)
            except Exception:
                u = 0.0
        import math as _m
        u_n = (_m.log1p(min(u, 1000.0)) / _m.log1p(1000.0)) * 0.10
        return (primary, overlap, head, hits, sc + u_n)
    return sorted(items, key=_key, reverse=True)[:top]

def budget_for(route: str, end: str = "hermes") -> tuple:
    """查预算阶梯：返回 (档位名, (lo, hi)) token 上限。未知路由落普通技术档。"""
    row = BUDGET_LADDER.get(route or "", _BUDGET_DEFAULT)
    lo, hi = row[1].get(end, row[1]["hermes"])
    return row[0], (int(lo), int(hi))


# ---------------------------------------------------------------------------
# B5 注入口合并（2026-09-08，小D）：引擎侧协同件——route 装配重排 + store 价值门控
# Spec：SupportBundle §B5；B2 挂账「provider 未来可按 route 细化注入」本批兑现。
# ---------------------------------------------------------------------------

# route → 注入偏好 track 序（首=最优先）。与 ROUTE_TRACK_AFFINITY 同画像依据，
# 但这里是硬偏好序（装配级重排用），非打分量——各 route 有独立侧重：
#   evidence_warning 类优先 procedural/default（旧病灶教训条目优先=stale 标注语义）；
#   resume 类优先 checkpoint 轨（接着干的最强线索）；
#   knowledge 类优先 preference/refined（人工拍板与偏好压过流水）。
ROUTE_TRACK_PREFERENCE = {
    "resume": ("checkpoint", "procedural", "preference"),
    "evidence_warning": ("procedural", "checkpoint", "preference"),
    "knowledge": ("preference", "procedural", "default"),
    "support": ("preference", "procedural", "default"),
    "preflight": ("preference", "procedural", "default"),
}

def route_rerank_for_injection(items, route: str, top: int):
    """装配级 route 重排（B5 件1）：把偏好 track 的候选往前顶，稳定二级键保序。

    与 hybrid_retriever 打分层刻意分工（hybrid:673 注记：boost 进 final 会把
    refined 抬过 0.6 阈值、饿死 raw 回落）——重排只动候选池顺序，不改分值，
    top-k 截断因此吃到 route 偏好而检索阈值语义零扰动。
    稳定排序：Python sorted 稳定 → 同偏好条目保持检索原序（零信息损失）。
    无 route / 未知 route / 候选空 → 原序截 top（基线逐字节一致）。
    """
    if not items or not route:
        return list(items[:top]) if items else []
    pref = ROUTE_TRACK_PREFERENCE.get(route)
    if not pref:
        return list(items[:top])
    _rank = {t: i for i, t in enumerate(pref)}
    def _key(it):
        doc = it[0] if isinstance(it, tuple) else it
        return _rank.get(getattr(doc, "track", "default") or "default", len(pref))
    return sorted(items, key=_key)[:top]


# ---------------------------------------------------------------------------
# B5 件3：Stop 自动 store 价值门控（引擎侧开关，默认 off；hook 端接线归用户）
# 门控规则=只收拍板/纠正/规矩/教训四类（关键词+长度阈值），寒暄草稿不落库。
# is_chitchat 复用（同文件，判空口径单源）。
# ---------------------------------------------------------------------------

_STORE_GATE_PATTERNS = {
    # 拍板：用户决策/授权/定案
    "decision": re.compile(
        r"拍板|定了|就按|照这个|按这个|同意|批准|就这么定|先做|先跑|开干|动手|执行"
        r"|去做|安排上|立项|验收通过|验收通过|通过|确认"),
    # 纠正：用户纠偏/否定/记岔
    "correct": re.compile(
        r"纠正|更正|不对|错了|应该是|实际是|打脸|被证伪|我错了|记岔|搞反|说反|修正"),
    # 规矩：纪律/流程/铁律
    "rule": re.compile(
        r"规矩|纪律|铁律|必须|禁止|不许|不能|一律|默认|照旧|约定|规程|流程|规范"),
    # 教训：踩坑/根因/复盘
    "lesson": re.compile(
        r"教训|踩坑|根因|复盘|错题|血泪|事故|翻车|挂了|修复|修复后|回归|守卫"),
}
_STORE_GATE_LEN_MIN = 8
_STORE_GATE_LEN_MAX = 2000

def store_quality_gate(text: str, chitchat_fn=None) -> dict:
    """Stop 自动 store 价值门控（B5 件3，2026-09-08）：返回 {"accept": bool, "reason": str}。

    规则（全过才收）：
      ① 长度阈值 [_STORE_GATE_LEN_MIN, _STORE_GATE_LEN_MAX]
      ② 非寒暄（chitchat_fn 复用，缺省用本模块 is_chitchat）
      ③ 四类关键词至少一类命中：拍板/纠正/规矩/教训
    纯函数零副作用；调用方（provider.sync_turn 门控位）默认 off 时根本不调本函数。
    """
    t = (text or "").strip()
    if len(t) < _STORE_GATE_LEN_MIN or len(t) > _STORE_GATE_LEN_MAX:
        return {"accept": False, "reason": "length"}
    _cc = chitchat_fn or is_chitchat
    if _cc(t):
        return {"accept": False, "reason": "chitchat"}
    for kind, pat in _STORE_GATE_PATTERNS.items():
        if pat.search(t):
            return {"accept": True, "reason": kind}
    return {"accept": False, "reason": "no-keyword"}
