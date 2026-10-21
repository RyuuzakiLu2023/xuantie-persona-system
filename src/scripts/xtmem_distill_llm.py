# -*- coding: utf-8 -*-
"""xtmem_distill_llm.py — 评判式蒸馏 · LLM 层（评判 + 提炼 + 落库）。

【这是什么】
上游 xtmem_distill_units.py 已把 raw 切成窗口写进 distill_units 表。本脚本读
state='pending' 的窗口，逐窗投给 LLM 按提示词评判蒸馏，产出结构化记忆条目，
经 xtmem CLI store 写 refined 层，回写窗口状态。

【为什么要「评判」而不是摘要】
只记原答案等于把错结论当知识存库，比不存更糟。答错的条目必须同时记下
「当时的误判」和「真相」，这样日后检索到的是结论+更正，而不是一条毒知识。

【失败不吞】
单窗失败只 attempt+1 记 last_error 留 pending，下轮重试；连续失败 MAX_ATTEMPT 次
转 failed（等人工）。解析不出条目算失败，不写空产物。

【公开化改动（相对运行正本）】
  - 【不】再读任何 profile .env：LLM key 只从环境变量取；缺失则退出码 2 并留事件。
  - 【不】再带私有网关默认值：XTMEM_LLM_BASE / XTMEM_LLM_MODEL 必须显式配置。
  - 提示词默认用仓库自带 prompts/distill_v2_generic.md，可用 XTMEM_DISTILL_PROMPT 覆盖。
  - 域名不写死：--domain 缺省处理库内全部 namespace（或 $XTMEM_DISTILL_NAMESPACE）。
  - 库路径 / CLI / 引擎统一走 xtmem_common。

用法：python xtmem_distill_llm.py [--limit 20] [--domain <ns>] [--db <path>]
                                 [--unit <unit_id>] [--dry-run]
输出：JSON 一行

必需环境变量（缺一即退出，不再有兜底默认）：
  XTMEM_LLM_BASE        OpenAI 兼容接口根地址，如 https://api.example.com/v1
  XTMEM_LLM_MODEL       模型 id
  XTMEM_LLM_KEY         API Key（只从环境读，不落盘、不回显）
可选：
  XTMEM_LLM_EFFORT      reasoning_effort（默认 high；设为空串 = 完全不发送该参数，
                        网关不认 reasoning_effort 时用这个出口）
  XTMEM_LLM_MAX_TOKENS  默认 8192（保守值；长窗口需显式调高——实测长窗要 32000+，
                        否则输出可能被截断）
  XTMEM_LLM_TIMEOUT     默认 120 秒（慢模型/长窗口显式调高）
  XTMEM_DISTILL_PROMPT  提示词文件路径
  XTMEM_DB_PATH / XTMEM_PLUGIN_ROOT / XTMEM_CLI  见 xtmem_common
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

try:  # 包内与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

MAX_ATTEMPT = 3
DEFAULT_LIMIT = 20
# 领取超时：超过这个时间还挂在 processing 视为僵尸（worker 崩了）。
# 必须明显大于单窗最坏耗时（LLM timeout × 最多 2 次尝试），否则会把正在跑的
# 窗口误判成僵尸、被第二个 worker 重复领取。下方按实际 timeout 动态抬底。
STALE_CLAIM_SECONDS = 7200

# 明显的占位值不当真配置：宁可直接报错，也别拿 "changeme" 去打网关。
# ⚠️【不】把这个元组里放空串：str.startswith(("", ...)) 恒为 True，会把任何值都判成占位
#   （本文件首版就踩过：所有配置都被判 invalid）。空值由下面的 `not val` 单独判。
_PLACEHOLDER_PREFIXES = ("changeme", "your-", "your_", "xxx", "sk-xxx")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_optional(name: str, default: str = "") -> str:
    """可"显式设为空"的配置。

    ⚠️【不】用 `os.environ.get(name) or default`：那样"设成空串"会被当成"没设"，
      于是 `XTMEM_LLM_EFFORT=""`（表示网关不支持 reasoning_effort、别发送）会变回 high。
      这里按"键是否存在于环境"判：存在就原样取（含空串），不存在才用默认。
    """
    if name in os.environ:
        return (os.environ.get(name) or "").strip()
    return default


def _require_env(name: str) -> str:
    val = _env(name)
    if not val or val.lower().startswith(_PLACEHOLDER_PREFIXES):
        raise SystemExit(
            "missing required environment variable: %s "
            "(评判式蒸馏不再提供默认网关/模型/密钥；请显式配置)" % name
        )
    return val


def llm_config() -> dict:
    """读取 LLM 配置。

    ⚠️【不】在这里加任何回落地址或内置 key：公开仓不能带私有网关，
      也不能靠"找不到就猜一个"活着。缺配置就让调用方看见明确错误。

    默认值刻意保守（对陌生网关也能跑起来）：
      - max_tokens 8192：多数 OpenAI 兼容服务都接受；长窗口请显式调高
        （实测长窗需要 32000 以上，否则正文可能被截断——见下方 ⚠️）。
      - timeout 120 秒：先给一个不欺负小网关的值；长窗口/慢模型显式调高。
      - effort 默认 high，但**显式设为空串 = 不发送该参数**（网关不认时的唯一出口）。
    """
    base = _require_env("XTMEM_LLM_BASE").rstrip("/")
    model = _require_env("XTMEM_LLM_MODEL")
    key = _require_env("XTMEM_LLM_KEY")
    effort = _env_optional("XTMEM_LLM_EFFORT", "high")
    return {
        "base": base,
        "model": model,
        "key": key,
        "effort": effort,  # "" = 不发送 reasoning_effort
        "max_tokens": int(_env_optional("XTMEM_LLM_MAX_TOKENS", "8192") or 8192),
        "timeout": int(_env_optional("XTMEM_LLM_TIMEOUT", "120") or 120),
    }


def prompt_path_for(domain: str) -> str:
    """提示词文件：$XTMEM_DISTILL_PROMPT > 仓库内通用提示词。"""
    env_p = _env("XTMEM_DISTILL_PROMPT")
    if env_p:
        return env_p
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "prompts", "distill_v2_generic.md")


# ---------------------------------------------------------------- 原料拼装
def build_material(conn, unit, domain: str = "") -> tuple[str, list[str], list[str]]:
    """按提示词 §输入格式 拼窗口原料。格式必须和提示词一致，否则模型认不出轮序号/id。

    ★ACL 边界（安全红线）：只把**目标域自己读得到的行**投给 LLM。
      为什么必须在这里挡：蒸馏产物最终写进 `domain`，它的可见性由 source_ids 继承。
      如果原料里混进了 domain 读不到的私有行，模型会把那段内容转述进条目正文，
      而条目恰好可以声明「只来自可见的那几条」——私有内容就借着更宽的可见性出门。
      这是**原料准入**问题，事后过滤正文来不及。所以按 raw_ids 取行后先过统一读闸
      `entry_visibility_ok(row, domain)`；读不到的行根本不进 material，并单独计数回执。

    返回 (material, window_ids, dropped)：window_ids 是真正进了原料的 id，
    dropped 是被 ACL 挡掉的 id（只进回执，绝不进正文/日志正文）。
    """
    # ★跨域闸：窗口账本属于哪个 namespace，就只能由那个域来蒸。
    #   为什么必须挡：产物写进 `domain`，而原料来自 unit 的域——两者不一致就等于
    #   "拿 A 域的话去写 B 域的结论"，可见性与归属全乱。调用方传错域时直接拒绝，
    #   别让它在 ACL 过滤后变成"能读多少蒸多少"的半截行为。
    unit_ns = str(unit["namespace"] if "namespace" in unit.keys() else "")
    if unit_ns and domain and unit_ns != domain:
        raise ValueError(
            f"unit namespace {unit_ns!r} != write domain {domain!r} "
            "(跨域蒸馏被拒绝：产物归属必须与原料域一致)")

    raw_ids = json.loads(unit["raw_ids"])
    ph = ",".join("?" * len(raw_ids))
    rows = conn.execute(
        f"SELECT id, content, timestamp, namespace, visibility, retire_reason "
        f"FROM chunks WHERE id IN ({ph}) ORDER BY timestamp",
        raw_ids,
    ).fetchall()

    common.ensure_engine()
    from memory.visibility import entry_visibility_ok

    allowed, dropped = [], []
    for r in rows:
        if entry_visibility_ok(r, domain):
            allowed.append(r)
        else:
            dropped.append(str(r["id"]))
    if not allowed:
        return "", [], dropped

    import datetime

    def ts(x):
        return datetime.datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M") if x else "?"

    lines = [
        f"# 蒸馏原料 · {unit['unit_id']}",
        f"# 会话ID(source): {unit['source']}",
        f"# 轮数: {len(allowed)}   时间: {ts(unit['ts_start'])} ~ {ts(unit['ts_end'])}",
        "# 格式: 每轮以 ===[序号] id=... ts=... 开头, 下面是原文",
        "",
    ]
    for i, r in enumerate(allowed, 1):
        lines.append("=" * 69)
        lines.append(f"[{i}] id={r['id']} ts={ts(r['timestamp'])}")
        lines.append((r["content"] or "") + "\n")
    return "\n".join(lines), [r["id"] for r in allowed], dropped


# ---------------------------------------------------------------- LLM 调用
def _post_once(cfg: dict, prompt: str, material: str) -> tuple[str, dict]:
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content":
                "下面是需要你蒸馏的对话原料。严格按上面提示词的规则和输出格式执行，"
                "直接输出蒸馏结果，不要任何开场白。\n\n" + material},
        ],
        "temperature": 0.3,
        "max_tokens": cfg["max_tokens"],
    }
    if cfg.get("effort"):
        payload["reasoning_effort"] = cfg["effort"]
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        # ⚠️ 带浏览器 UA：部分网关/Cloudflare 会拦 urllib 默认 UA（HTTP 403）。
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"],
                 "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0 Safari/537.36")},
    )
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        raw = resp.read().decode("utf-8", "replace")

    try:
        body = json.loads(raw)
        msg = body["choices"][0]["message"]
        # finish_reason 必须抬进 usage：否则「合法 0 条交付」分支（要求 stop）不可达，
        # 有实质输出的干净窗会被误判 parse 失败，每窗白烧多次 LLM 重试。
        usage = body.get("usage") or {}
        usage["finish_reason"] = (body["choices"][0] or {}).get("finish_reason")
        return (msg.get("content") or ""), usage
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    # SSE 流回退：逐行取 delta
    chunks, usage, finish, err = [], {}, "", None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except Exception:
            continue
        if ev.get("error"):
            err = json.dumps(ev["error"], ensure_ascii=False)[:300]
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices", []):
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            d = ch.get("delta") or {}
            if d.get("content"):
                chunks.append(d["content"])
    usage["finish_reason"] = finish
    if err:
        usage["upstream_error"] = err
    return "".join(chunks), usage


def call_llm(cfg: dict, prompt: str, material: str) -> tuple[str, dict]:
    """调 LLM。

    ⚠️ 空正文重试一次：长窗口偶发上游中断（finish_reason='error'、正文 0 字符），
      原样重发常能成功。只对"空正文"重试，不改任何参数。
    """
    out, usage = _post_once(cfg, prompt, material)
    if out.strip():
        return out, usage
    time.sleep(3)
    out2, usage2 = _post_once(cfg, prompt, material)
    usage2["retried"] = True
    usage2["first_attempt"] = {"finish_reason": usage.get("finish_reason"),
                              "upstream_error": usage.get("upstream_error")}
    return out2, usage2


# ---------------------------------------------------------------- 输出解析
# 提示词字段名 → 内部键。冒号可能是全角，字段名后可能带空格。
FIELD_MAP = {
    "桶": "bucket",
    "条目类型": "kind",
    "时间": "when",
    "信息截至": "as_of",
    "提问方": "asker",
    "玄铁回答判定": "verdict",
    "回答判定": "verdict",
    "问的是": "question",
    "玄铁答的是": "answer",
    "答的是": "answer",
    "事实/结论": "fact",
    "真相/更正": "truth",
    "结论价值": "value",
    "血缘": "lineage",
}
FIELD_RE = re.compile(r"^\s*[-*]\s*(" + "|".join(map(re.escape, FIELD_MAP)) + r")\s*[:：]\s*(.*)$")
HEAD_RE = re.compile(r"^\s*#{2,5}\s*\[(\d+)\]\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^\s*#{2,3}\s*(记忆条目|本窗口未定论/待跟进|蒸馏官备注)\s*$")


def parse_entries(text: str) -> tuple[list[dict], str, str]:
    """解析 Markdown 输出为条目列表 + 未定论区 + 备注区。

    容错要点（模型常犯）：标题层级 2~5 级都收；全角冒号；字段值跨多行续写；
    字段名后带空格。解析不出条目 → 上层按失败处理（不写空产物）。
    """
    entries, undecided, notes = [], [], []
    cur, cur_field, zone = None, None, "head"

    for line in text.splitlines():
        ms = SECTION_RE.match(line)
        if ms:
            if cur:
                entries.append(cur)
                cur, cur_field = None, None
            zone = {"记忆条目": "entries", "本窗口未定论/待跟进": "undecided",
                    "蒸馏官备注": "notes"}[ms.group(1)]
            continue

        mh = HEAD_RE.match(line)
        if mh and zone in ("entries", "head"):
            if cur:
                entries.append(cur)
            cur = {"idx": int(mh.group(1)), "title": mh.group(2)}
            cur_field = None
            zone = "entries"
            continue

        if zone == "undecided":
            if line.strip():
                undecided.append(line.rstrip())
            continue
        if zone == "notes":
            if line.strip():
                notes.append(line.rstrip())
            continue

        if cur is None:
            continue
        mf = FIELD_RE.match(line)
        if mf:
            cur_field = FIELD_MAP[mf.group(1)]
            cur[cur_field] = mf.group(2).strip()
        elif cur_field and line.strip() and not line.lstrip().startswith(("-", "*", "#")):
            cur[cur_field] = (cur.get(cur_field, "") + " " + line.strip()).strip()

    if cur:
        entries.append(cur)

    # 标题 + 至少 3 个字段才算成条
    entries = [e for e in entries if len([k for k in e if k not in ("idx", "title")]) >= 3]
    return entries, "\n".join(undecided), "\n".join(notes)


def entry_to_text(e: dict) -> str:
    """条目 → 落库正文。字段顺序固定，空字段省略（省 token、少噪音）。

    ⚠️ 判定档必须留在正文里——它是"这条是不是错答案"的唯一线索，检索时要能看见。
    """
    parts = [f"【{e.get('bucket', '?')}·{e.get('verdict', '?')}】{e.get('title', '')}"]
    if e.get("when"):
        parts.append(f"时间: {e['when']}")
    if e.get("as_of"):
        parts.append(f"信息截至: {e['as_of']}")
    if e.get("asker"):
        parts.append(f"提问方: {e['asker']}")
    for label, key in (("问", "question"), ("答", "answer"),
                       ("事实", "fact"), ("真相/更正", "truth"), ("价值", "value")):
        v = (e.get(key) or "").strip()
        if v and v not in ("—", "-", "无"):
            parts.append(f"{label}: {v}")
    return "\n".join(parts)


def pick_lineage(e: dict, window_ids: list[str]) -> tuple[list[str], str]:
    """条目声明的血缘 ∩ 本窗口真实 raw id。返回 (ids, scope)。

    ⚠️ 这是防「血缘灌水」与「假精确来源」的闸门：
      - 只认**精确相等**的 id。历史实现用双向 startswith 模糊匹配，`mem-abcd` 会命中
        一整批 `mem-abcdxxxx`，等于给条目安了一个它没读过的来源；
      - 声明了一堆但一个都对不上 → 退回整窗，并把 scope 标成 `window`，不假装精确。

    scope ∈ {"declared", "window"}：
      declared = 条目声明的血缘可核实（且调用方还会再过 ACL 收窄检查）；
      window   = 声明缺失/不可核实，按整窗登记。
    """
    raw = e.get("lineage") or ""
    found = set(re.findall(r"mem-[0-9a-f]{4,}", raw))
    if not found:
        return list(window_ids), "window"
    exact = [wid for wid in window_ids if wid in found]
    if not exact:
        return list(window_ids), "window"
    return exact, "declared"


def _visibility_sig(row) -> tuple:
    """一行的"读者范围签名"（namespace + visibility）。"""
    try:
        return (str(row["namespace"] or ""), str(row["visibility"] or ""))
    except (IndexError, KeyError, TypeError):
        return ("", "")


def acl_safe_source_ids(declared: list[str], window_rows: list,
                        window_ids: list[str]) -> tuple[list[str], str]:
    """收窄 source_ids 前的 ACL 检查。返回 (最终 source_ids, scope)。

    ★为什么不能直接采信 LLM 挑的子集：派生记忆的可见性由 source_ids 的读者集合交集决定。
      整窗里若混有更严可见性的行（模型没引用它），而条目只登记了宽松的那几条，
      派生页就会以更宽的可见性落库——模型的转述可能已经带出那条的内容。
      所以：整窗读者签名不一致 ⇒ 一律按整窗登记（可见性取最严），声明子集作废。
    """
    if not declared:
        return list(window_ids), "window"
    sigs = {_visibility_sig(r) for r in window_rows}
    if len(sigs) > 1:
        return list(window_ids), "window-mixed-acl"
    return declared, "declared"


# ---------------------------------------------------------------- 落库
# 判定档 → importance 加成：错误档最值得被记住（防同一个坑再踩），未定论最低
VERDICT_IMPORTANCE = {
    "错误·已纠正": 0.85,
    "错误·未纠正": 0.80,
    "正确": 0.70,
    "正确·未核验": 0.60,
    "正确·外部未核验": 0.60,
    "未定论": 0.50,
    "不适用": 0.65,
}
BUCKET_TRACK = {"技术": "procedural", "事务": "default"}


def norm_verdict(raw: str) -> str:
    """判定档规范化。模型常在档位后加括号说明，按最长前缀归一。"""
    v = (raw or "").strip()
    for k in sorted(VERDICT_IMPORTANCE, key=len, reverse=True):
        if v.startswith(k):
            return k
    return v


def store_entry(e: dict, source_ids: list[str], domain: str, db_path: str,
                operation_id: str | None = None) -> dict:
    verdict = norm_verdict(e.get("verdict"))
    imp = VERDICT_IMPORTANCE.get(verdict, 0.65)
    track = BUCKET_TRACK.get((e.get("bucket") or "").strip(), "default")
    tags = ["distill-judge", "xtmem"]
    if verdict.startswith("错误"):
        tags.append("判错")
    if (e.get("bucket") or "").strip():
        tags.append(e["bucket"].strip())
    return common.store_via_cli(
        db_path=db_path, agent=domain, namespace=domain,
        text=entry_to_text(e), importance=imp,
        derived_from="distill-judge", source_ids=source_ids,
        tier="refined", track=track, tags=tags,
        operation_id=operation_id,
    )


# ---------------------------------------------------------------- 原料退役（D1-3）
_VS: dict = {}


def _vecstore(db_path: str):
    """按库路径缓存 VectorStore（同库只建一次连接，别每窗重开）。"""
    vs = _VS.get(db_path)
    if vs is None:
        vs = common.vector_store(db_path)
        _VS[db_path] = vs
    return vs


def retire_source_raws(db_path: str, pairs: list) -> dict:
    """蒸馏产物写成功后，把喂它的 raw 原料置 superseded。

    为什么要退役：raw 流水不退役就一直占 active 池（水化上限、检索噪声两头都是它）。
    refined 已经是它的替代品，两条同时在架等于同一件事查两次。

    🔴 致命红线【绝不可改成 archived】：raw 置 archived 会触发血缘传播——
      派生链上游归档 → refined 被打 'lineage-stale' 标签 → 蒸馏产物自己把自己毒死。
      superseded 才是「有替代」的语义，archived 是「没了」。

    pairs: [(refined_id, [raw_id, ...]), ...]。只动 tier='raw' 且 state='active' 的行——
    精产品之间不互相退役（那是 profile/timeline 的事）。
    """
    want = {}
    for rid, raw_ids in pairs:
        for r in raw_ids or []:
            if r and r != rid and r not in want:
                want[r] = rid
    out = {"candidates": len(want), "retired": 0, "skipped": 0}
    if not want:
        return out
    vs = _vecstore(db_path)
    cur = vs._get_conn()
    for raw_id, successor in want.items():
        row = cur.execute(
            "SELECT state, tier FROM chunks WHERE id=?", (raw_id,)
        ).fetchone()
        if row is None or row["state"] != "active" or str(row["tier"] or "raw") != "raw":
            out["skipped"] += 1
            continue
        if vs.mark_superseded(raw_id, successor_id=successor,
                              reason="distill-retire", by="distill_llm"):
            out["retired"] += 1
        else:
            out["skipped"] += 1
    return out


# ---------------------------------------------------------------- 主流程
def run_unit(conn, unit, cfg: dict, prompt: str, db_path: str,
             domain: str, dry: bool) -> dict:
    try:
        material, window_ids, dropped = build_material(conn, unit, domain)
    except ValueError as exc:
        # 跨域/账本异常：本窗判失败（交给 mark_failure 计次），
        # 不让一个坏窗口把整批任务崩掉。
        return {"unit": unit["unit_id"], "ok": False, "stage": "material",
                "error": str(exc)[:300]}
    t0 = time.time()

    def _sanitize_err(ex: Exception) -> str:
        s = repr(ex)
        s = re.sub(r'Bearer\s+[^\s\'",]+', 'Bearer [REDACTED]', s)
        s = re.sub(r'sk-[0-9a-zA-Z]{10,}', 'sk-[REDACTED]', s)
        # URL 里的 query（可能带 key/token）一并抹掉，别让异常文本把它带出去
        s = re.sub(r'([?&](?:key|token|api_key|access_token)=)[^&\s\'"]+', r'\1[REDACTED]', s, flags=re.I)
        return s[:300]

    # 原料被 ACL 全部挡掉：不调模型（省钱、也不把空窗当"无信息"记成 distilled）
    if not window_ids:
        return {"unit": unit["unit_id"], "ok": False, "stage": "material",
                "error": "no readable material after ACL filter",
                "acl_dropped": len(dropped), "elapsed": round(time.time() - t0, 1)}

    try:
        out, usage = call_llm(cfg, prompt, material)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as ex:
        return {"unit": unit["unit_id"], "ok": False, "stage": "llm",
                "error": _sanitize_err(ex), "acl_dropped": len(dropped)}
    dt = time.time() - t0

    entries, undecided, notes = parse_entries(out)
    if not entries:
        # 「宁缺毋滥出 0 条 + 备注」是合法交付不是失败：
        # 模型走完正常结构（finish=stop 且有未定论/备注区）说明它确实读过原料并决定不蒸。
        # 纯空白/半截输出（finish!=stop 或无任何区标记）仍算失败。
        _has_zone = ("未定论" in out) or ("备注" in out)
        if usage.get("finish_reason") == "stop" and _has_zone and out.strip():
            if dry:
                return {"unit": unit["unit_id"], "ok": True, "dry_run": True, "entries": 0,
                        "empty_delivery": True, "usage": usage, "elapsed": round(dt, 1),
                        "acl_dropped": len(dropped),
                        "preview": ["（0 条交付·宁缺毋滥）" + (notes or undecided)[:60]]}
            conn.execute(
                "UPDATE distill_units SET state='distilled', distilled_at=?, refined_ids='[]', "
                "last_error=?, claim_token=NULL, claimed_at=NULL WHERE unit_id=?",
                (time.time(),
                 "0条交付（宁缺毋滥）· 备注: " + ((notes or undecided or "").strip()[:120]),
                 unit["unit_id"]),
            )
            conn.commit()
            return {"unit": unit["unit_id"], "ok": True, "entries": 0, "stored": 0,
                    "empty_delivery": True, "usage": usage, "elapsed": round(dt, 1),
                    "acl_dropped": len(dropped)}
        # ⚠️ 上游正文【不】进日志/回执：parse 失败要能定位，但不能把模型原文
        #   （可能含用户隐私）写进 cron 日志或终端。只给长度 + 指纹 + finish_reason。
        return {"unit": unit["unit_id"], "ok": False, "stage": "parse",
                "error": f"no entries parsed (out={len(out)} chars, "
                         f"sha256={hashlib.sha256((out or '').encode('utf-8')).hexdigest()[:12]}, "
                         f"finish={usage.get('finish_reason')})",
                "elapsed": round(dt, 1), "acl_dropped": len(dropped)}

    if dry:
        return {"unit": unit["unit_id"], "ok": True, "dry_run": True, "entries": len(entries),
                "buckets": _tally(entries, "bucket"), "verdicts": _tally(entries, "verdict"),
                "usage": usage, "elapsed": round(dt, 1), "acl_dropped": len(dropped),
                "preview": [e.get("title", "")[:70] for e in entries]}

    # ── 逐条落库 ───────────────────────────────────────────────────────────
    # operation_id：同一 unit 的同一段正文重试必须得到同一个记忆 id。
    # 为什么必须带：partial 失败重跑（或"写成功但响应丢了"重试）时，没有幂等键就会
    # 写出两条同内容记忆——重复召回 + 重复计费，而且账本只记得住一条。
    unit_tag = hashlib.sha256(str(unit["unit_id"]).encode("utf-8")).hexdigest()[:12]
    window_rows = conn.execute(
        f"SELECT id, namespace, visibility FROM chunks WHERE id IN ({','.join('?' * len(window_ids))})",
        window_ids,
    ).fetchall()

    refined_ids, errs, scopes = [], [], {}
    lineage_pairs = []  # [(refined_id, [原料 raw id...])]：只登记写成功的
    for i, e in enumerate(entries):
        declared, scope = pick_lineage(e, window_ids)
        lin, scope = acl_safe_source_ids(declared, window_rows, window_ids)
        scopes[scope] = scopes.get(scope, 0) + 1
        text_for_id = entry_to_text(e)
        op_id = "distill:%s:%s" % (
            unit_tag, hashlib.sha256(text_for_id.encode("utf-8")).hexdigest()[:16])
        res = store_entry(e, lin, domain, db_path, operation_id=op_id)
        cid = res.get("id") or (res.get("chunk") or {}).get("id")
        if res.get("ok") and cid:
            refined_ids.append(cid)
            lineage_pairs.append((cid, lin))
        else:
            errs.append((res.get("error") or str(res)[:120]))

    if not refined_ids:
        return {"unit": unit["unit_id"], "ok": False, "stage": "store",
                "error": "; ".join(errs)[:300], "entries": len(entries),
                "acl_dropped": len(dropped)}

    # ── 部分成功：不许冒充整窗成功 ────────────────────────────────────────
    # 语义：
    #   distilled = 每条都写成功（errs 为空）
    #   partial   = 至少一条写成功、至少一条失败。已写入的 id 全保留（不能丢），
    #               账本 last_error 记剩余错误；state='partial' 不属于自动领取集合，
    #               因此不会被下一轮 cron 整窗重蒸（重蒸会重复计费）。
    #               要补跑用 `--unit <id>` 显式重试，靠 operation_id 幂等。
    partial = bool(errs)
    state = "partial" if partial else "distilled"
    err_txt = "; ".join(errs)[:500] or None
    conn.execute(
        "UPDATE distill_units SET state=?, distilled_at=?, refined_ids=?, last_error=?, "
        "claim_token=NULL, claimed_at=NULL WHERE unit_id=?",
        (state, time.time(), json.dumps(refined_ids, ensure_ascii=False),
         err_txt, unit["unit_id"]),
    )
    conn.commit()
    # refined 已落库 → 喂它的 raw 原料退役。失败不影响本窗结论（产物在架，
    # 原料最坏下轮再退；幂等：非 active 直接 skip）。
    try:
        _ret = retire_source_raws(db_path, lineage_pairs)
    except Exception as exc:
        _ret = {"candidates": 0, "retired": 0, "skipped": 0, "error": str(exc)[:160]}
    return {"unit": unit["unit_id"], "ok": not partial, "partial": partial,
            "state": state, "entries": len(entries),
            "stored": len(refined_ids), "store_errors": len(errs), "store_error_detail": errs[:3],
            "lineage_scope": scopes, "acl_dropped": len(dropped),
            "raw_retired": _ret.get("retired", 0), "raw_retire_skipped": _ret.get("skipped", 0),
            "buckets": _tally(entries, "bucket"), "verdicts": _tally(entries, "verdict"),
            "usage": usage, "elapsed": round(dt, 1)}


def _tally(entries, key):
    out = {}
    for e in entries:
        k = (e.get(key) or "?").strip()
        if key == "verdict":
            k = norm_verdict(k)
        out[k] = out.get(k, 0) + 1
    return out


def mark_failure(conn, unit, err: str):
    att = int(unit["attempt"] or 0) + 1
    state = "failed" if att >= MAX_ATTEMPT else "pending"
    conn.execute("UPDATE distill_units SET attempt=?, last_error=?, state=?, "
                 "claim_token=NULL, claimed_at=NULL WHERE unit_id=?",
                 (att, err[:500], state, unit["unit_id"]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(prog="xtmem_distill_llm")
    ap.add_argument("--domain", default=None,
                    help="只处理该 namespace；缺省=库内全部 namespace")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--db", default=None)
    ap.add_argument("--unit", help="只跑指定 unit_id（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="调模型但不落库、不改状态")
    args = ap.parse_args()

    db_path = common.resolve_db_path(args.db)

    # 缺配置显式报错退出并写事件留痕（事件写失败不掩盖退出码）
    try:
        cfg = llm_config()
    except SystemExit as exc:
        err_msg = str(exc)
        print(json.dumps({"ok": False, "error": err_msg}, ensure_ascii=False), file=sys.stderr)
        try:
            conn_ev = common.open_db(db_path)
            try:
                conn_ev.execute(
                    "INSERT INTO xtmem_events (ts, kind, agent, namespace, project, "
                    "target_chunk, meta) VALUES (?, 'migration', 'distill', ?, 'XTMEM', '', ?)",
                    (time.time(), args.domain or "", json.dumps({
                        "action": "distill_abort_missing_config",
                        "error": err_msg[:300]}, ensure_ascii=False)))
                conn_ev.commit()
            finally:
                conn_ev.close()
        except Exception:
            pass
        return 2

    conn = common.open_db(db_path)
    try:
        domains = ([args.domain] if args.domain else [])
        if not domains:
            # 只有账本里已有窗口的域才需要跑 LLM；没有再退到库内全部 namespace。
            domains = [r[0] for r in conn.execute(
                "SELECT DISTINCT namespace FROM distill_units WHERE state='pending' "
                "AND namespace IS NOT NULL AND namespace<>'' ORDER BY namespace").fetchall()]
            if not domains:
                domains = common.resolve_namespaces(conn)
        all_results, rc = [], 0
        for domain in domains:
            prompt_path = prompt_path_for(domain)
            if not os.path.exists(prompt_path):
                all_results.append({"ok": False, "domain": domain,
                                    "error": f"prompt missing: {prompt_path}"})
                rc = 1
                continue
            with open(prompt_path, encoding="utf-8") as f:
                prompt = f.read()
            res = _run_domain(conn, db_path, domain, prompt, cfg, args)
            all_results.append(res)
            # 任一域出现 failed/partial 都要让整体退出码非 0：cron 靠退出码判红。
            if not res.get("ok", False):
                rc = 1
        print(json.dumps({"ok": rc == 0, "model": cfg["model"], "db": db_path,
                          "results": all_results}, ensure_ascii=False))
        return rc
    finally:
        conn.close()


def _run_domain(conn, db_path: str, domain: str, prompt: str, cfg: dict, args) -> dict:
    """领取并处理某域的一批窗口。领取/回写口径与原实现一致。"""
    from memory.open_db import write_txn  # 引擎已在 common.open_db 时导入

    if args.unit:
        units = conn.execute("SELECT * FROM distill_units WHERE unit_id=?",
                             (args.unit,)).fetchall()
    else:
        token = uuid.uuid4().hex
        # 僵尸阈值必须大于"单窗最坏合法耗时"：call_llm 最多 2 次 × timeout。
        # 否则慢模型跑着跑着就被判僵尸，同一窗会被第二个 worker 再提交一次。
        stale_seconds = max(STALE_CLAIM_SECONDS, int(cfg.get("timeout", 120)) * 2 + 600)
        # 🔴 领取必须包在 BEGIN IMMEDIATE 事务里：
        #    裸 UPDATE..IN(SELECT) 在并发下不原子——python sqlite3 默认 deferred，
        #    worker B 在 worker A commit 前建快照，子查询在旧快照上选出相同窗口，
        #    多个 worker 会重复蒸同一池。IMMEDIATE 让第二个 worker 等第一个提交后再读。
        with write_txn(conn):
            # 僵尸回收：只回收**领取时刻**超时的窗口（claimed_at），不看 created_at。
            # ⚠️ 为什么不能看 created_at：created_at 是窗口建账时间，几小时前建好的窗口
            #   完全可能此刻正被另一个 worker 正常蒸着；按建账时间判僵尸会把它重置回
            #   pending，同一窗被两个 worker 各提交一次 LLM（重复产物 + 重复计费）。
            #   老库无 claimed_at（NULL）→ 视为「无法确认仍在处理」，超时后保守回收。
            conn.execute(
                "UPDATE distill_units SET state='pending', claim_token=NULL, claimed_at=NULL "
                "WHERE namespace=? AND state='processing' "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (domain, time.time() - stale_seconds),
            )
            cands = conn.execute(
                "SELECT unit_id FROM distill_units WHERE namespace=? AND state='pending' "
                "AND attempt < ? ORDER BY ts_start LIMIT ?",
                (domain, MAX_ATTEMPT, args.limit),
            ).fetchall()
            ids = [c["unit_id"] for c in cands]
            if ids:
                ph = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE distill_units SET state='processing', claim_token=?, "
                    f"claimed_at=?, attempt=attempt+1 WHERE unit_id IN ({ph})",
                    [token, time.time()] + ids,
                )
        units = (conn.execute(
            "SELECT * FROM distill_units WHERE namespace=? AND claim_token=? ORDER BY ts_start",
            (domain, token),
        ).fetchall() if ids else [])

    results, done, failed, partial_n, entries_n = [], 0, 0, 0, 0
    for u in units:
        r = run_unit(conn, u, cfg, prompt, db_path, domain, args.dry_run)
        results.append(r)
        if r.get("partial"):
            # 部分成功：产物已写入，**不**能按失败重排 pending（会把整窗再蒸一遍），
            # 也不能算 done。标记 pending_left 之外的独立计数，等人工/--unit 补跑。
            partial_n += 1
            entries_n += r.get("stored", 0)
        elif r.get("ok"):
            done += 1
            entries_n += r.get("stored", r.get("entries", 0))
        else:
            failed += 1
            if not args.dry_run:
                mark_failure(conn, u, r.get("error", "?"))

    left = conn.execute(
        "SELECT COUNT(*) n FROM distill_units WHERE namespace=? AND state='pending'",
        (domain,),
    ).fetchone()["n"]
    # ok 的口径：本批没有 failed/partial 才算真成功。partial 必须显式报红——
    # 它的语义是"有产物但没写全"，冒充整窗成功会让上层以为这批已处理干净。
    batch_ok = (failed == 0 and partial_n == 0)
    return {"ok": batch_ok, "domain": domain, "units_tried": len(units),
            "units_done": done, "units_failed": failed, "units_partial": partial_n,
            "entries_written": entries_n, "pending_left": left,
            "dry_run": args.dry_run, "results": results}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
