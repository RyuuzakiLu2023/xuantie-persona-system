# -*- coding: utf-8 -*-
"""xtmem_distill_units.py — 评判式蒸馏 · 规则层（建窗口账本）。

【这是什么】
把 raw 层（tier='raw'）的对话/流水按「同一来源 + 相邻轮间隔 ≤ GAP」切成窗口，
写进 distill_units 表，作为「哪些窗口该蒸、蒸没蒸到」的唯一权威账。
本层只做规则（切窗 + 过滤已知垃圾 + 落账），零语义判断；判对错/提炼交给 LLM 层
（xtmem_distill_llm.py 读 state='pending'）。

【为什么要这一层】
按 (namespace, project) 整域一锅炖会出「血缘登记了、正文没承载」的假覆盖：账上写着
蒸过 N 轮，正文里只剩几轮。distill_units 把每个窗口单独记账，蒸馏器逐窗领取、
逐窗回写状态，蒸没蒸到一眼可查。

【窗口口径】
同 source（会话）内相邻轮间隔 ≤ GAP 分钟为一窗；跨阈值即断开。默认 60 分钟——
更长的窗容易单窗几万字符，需要二次切分，不划算。
尾窗口保护：最后一窗距现在 < GAP 分钟时不落账（对话可能还在继续，下一轮 cron 再建）。

【本次公开化改动（相对运行正本）】
  - 库路径/引擎位置统一走 xtmem_common（环境变量优先，不写生产绝对路径）。
  - 发言人标记不再写死「玄铁/小D」两套：默认同时认 User:/Assistant: 与 用户:/助手:，
    可用 $XTMEM_SPEAKER_JSON 覆盖（见 speaker_markers()）。
  - 默认域不再写死私人域：--domain 缺省时处理库内实际存在的全部 namespace。
  - 提示词/域名都可由调用方配置；本文件不读取任何 profile 配置或密钥。

用法：python xtmem_distill_units.py [--domain <ns>] [--gap 60] [--rebuild] [--db <path>]
输出：JSON 一行（cron 侧读 stdout）

环境变量：
  XTMEM_DB_PATH            库路径（最高优先）
  XTMEM_PLUGIN_ROOT        含 memory/ 的目录（仓库内直跑时可省）
  XTMEM_SPEAKER_JSON       发言人标记覆盖，形如
                           {"user":["User:","用户:"],"assistant":["Assistant:","助手:"]}
  XTMEM_DISTILL_NAMESPACE  未传 --domain 时的单域覆盖
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

try:  # 包内（wheel: xtmem_jobs）与同目录直跑两态
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

DEFAULT_DOMAIN = None          # None = 库内全部 namespace（不预设私人域）
GAP_MIN_DEFAULT = 60

# ---------------------------------------------------------------- 发言人标记
# 默认集合刻意同时容纳中英文两种常见形态：公开仓不知道调用方的 agent 框架用哪种前缀。
# ⚠️【不】在这里写死某个角色的名字（玄铁/小D/…）——那是部署配置，不是通用逻辑。
_DEFAULT_SPEAKERS = {
    "user": ["User:", "User：", "用户:", "用户：", "Human:", "human:"],
    "assistant": ["Assistant:", "Assistant：", "助手:", "助手：", "Bot:", "bot:"],
}


def speaker_markers() -> dict:
    """返回 {"user": [...], "assistant": [...]}；$XTMEM_SPEAKER_JSON 可整体覆盖。"""
    raw = os.environ.get("XTMEM_SPEAKER_JSON", "").strip()
    if not raw:
        return dict(_DEFAULT_SPEAKERS)
    try:
        data = json.loads(raw)
        out = {}
        for key in ("user", "assistant"):
            vals = data.get(key)
            if isinstance(vals, list) and vals:
                out[key] = [str(v) for v in vals if str(v)]
        if out.get("user"):
            out.setdefault("assistant", list(_DEFAULT_SPEAKERS["assistant"]))
            return out
    except (TypeError, ValueError):
        pass
    return dict(_DEFAULT_SPEAKERS)


def _user_line_re(markers: dict) -> re.Pattern:
    alts = "|".join(re.escape(m) for m in markers["user"])
    return re.compile(rf"^(?:{alts})\s*(.{{0,20}})$")


def _user_block_re(markers: dict) -> re.Pattern:
    alts = "|".join(re.escape(m) for m in markers["user"])
    return re.compile(rf"(?:{alts})\s*(.*?)(?=(?:\n(?:"
                      + "|".join(re.escape(m) for m in markers["assistant"])
                      + r"))|$)", re.S)


def _has_marker(text: str, variants: list[str]) -> bool:
    return any(v in text for v in variants)


SYS_MARKERS = (
    "Review the conversation above",
    "consider saving to memory",
    "update the skill library",
    "Be ACTIVE",
    "Has the user revealed",
)
# 寒暄词表：用户行整句只有这些才算寒暄（≤20 字且全匹配）
GREET_RE = re.compile(
    r"^(?:在吗|在不在|你好|哈喽|嗨|hello|hi|早安|晚安|嗯|哦|哈哈|呵呵|辛苦了|谢谢|感谢|ok"
    r"|好的|好|行|收到|再见|拜拜|睡了|早|晚|在|在么|干嘛呢|干啥呢|忙吗)"
    r"[\s\?？。!！~～，,、]*$",
    re.I,
)
# 纠正信号词（标记 has_correction，LLM 层可据此升级核验强度）
CORRECTION_WORDS = ("不对", "错了", "理解错", "你搞错", "不是这个意思", "反了",
                    "我说的是", "不是说", "又错", "胡说", "wrong", "incorrect")


def classify_junk(content: str, source: str, tags: str,
                  markers: dict | None = None) -> str | None:
    """返回过滤原因（None = 保留）。判据顺序：先看便宜字段，再看正文。"""
    markers = markers or speaker_markers()
    src = source or ""
    if src.startswith("cron"):
        return "cron"
    tg = tags or ""
    ct = content or ""
    if ("wiki" in tg and "xtmem" in tg) or ct.startswith("# 实体 ·"):
        return "wiki-card"
    if "可见性测试信标" in ct:
        return "beacon"
    head = ct[:300]
    for m in SYS_MARKERS:
        if m in head:
            return "sys-prompt"
    if not _has_marker(ct, markers["user"]):
        return "no-user"
    # 第一条用户行（截到第一处 assistant 标记为止）
    first = ct
    for am in markers["assistant"]:
        idx = ct.find("\n" + am)
        if idx >= 0:
            first = ct[:idx]
            break
    m = _user_line_re(markers).match(first.strip())
    if m and GREET_RE.match(m.group(1).strip()):
        return "greet"
    return None


def has_correction(content: str, markers: dict | None = None) -> bool:
    """用户行是否含纠正信号。⚠️ 只看用户行——agent 自己说"错了"不算用户纠正它。"""
    markers = markers or speaker_markers()
    m = _user_block_re(markers).search(content or "")
    user_part = m.group(1) if m else ""
    low = user_part.lower()
    return any(w.lower() in low for w in CORRECTION_WORDS)


# ---------------------------------------------------------------- 建表
DDL = """
CREATE TABLE IF NOT EXISTS distill_units (
  unit_id      TEXT PRIMARY KEY,
  namespace    TEXT NOT NULL,
  source       TEXT NOT NULL,
  ts_start     REAL NOT NULL,
  ts_end       REAL NOT NULL,
  turn_count   INTEGER NOT NULL,
  char_count   INTEGER NOT NULL,
  raw_ids      TEXT NOT NULL,
  prev_unit    TEXT,
  filtered_ids TEXT,
  has_correction INTEGER DEFAULT 0,
  state        TEXT NOT NULL,   -- pending / processing / distilled / partial / failed
  distilled_at REAL,
  refined_ids  TEXT,
  claim_token  TEXT,
  claimed_at   REAL,
  attempt      INTEGER DEFAULT 0,
  last_error   TEXT,
  created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_du_state ON distill_units(state, ts_start);
CREATE INDEX IF NOT EXISTS idx_du_ns ON distill_units(namespace, source);
"""


def ensure_schema(conn):
    """建表 + 补列 + 时间字段类型闸（trigger 版，兼容存量 TEXT 行）。

    ⚠️ 时间四列必须是数值 epoch：SQLite 动态类型会让 'YYYY-MM-DD' 字符串混进来，
      排序序 TEXT > REAL ⇒ "取最新"永远先命中老字符串，功能莫名失效且极难查。
      用 trigger 不用 CHECK：CHECK 要重建表，会把存量字符串行重新 INSERT 而卡死。
    """
    conn.executescript(DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(distill_units)")}
    if "claim_token" not in cols:
        conn.execute("ALTER TABLE distill_units ADD COLUMN claim_token TEXT")
    # ⚠️ claimed_at 是「本次领取时间」，与 created_at（窗口建账时间）不是一回事：
    #   僵尸回收只能看 claimed_at。运行正本用 created_at 判超时（见 xtmem_distill_llm.py
    #   原 `created_at < now-7200`），于是「3 小时前建的窗口、此刻正被另一个 worker
    #   正常蒸着」会被当成僵尸重置回 pending ⇒ 同一窗被两个 worker 重复提交 LLM。
    #   本公开版把判据换成 claimed_at；老库补列后为 NULL，按「无领取时间」保守回收。
    if "claimed_at" not in cols:
        conn.execute("ALTER TABLE distill_units ADD COLUMN claimed_at REAL")
    _du_when = ("NEW.{c} IS NOT NULL AND typeof(NEW.{c}) NOT IN ('real','integer')")
    _du_cond = " OR ".join(_du_when.format(c=c)
                           for c in ("ts_start", "ts_end", "created_at", "distilled_at"))
    for _trg, _ddl in (("trg_distill_units_time_type_ins", "BEFORE INSERT ON distill_units"),
                       ("trg_distill_units_time_type_upd", "BEFORE UPDATE ON distill_units")):
        try:
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS %s\n%s\nFOR EACH ROW WHEN %s\n"
                "BEGIN\n"
                "    SELECT RAISE(ABORT, 'distill_units time fields "
                "(ts_start/ts_end/created_at/distilled_at) must be numeric epoch');\n"
                "END;" % (_trg, _ddl, _du_cond)
            )
        except sqlite3.OperationalError:
            # 建闸失败不拖死建表流程；守卫测试会报红。
            pass
    conn.commit()


# ---------------------------------------------------------------- 切窗
def build_units(conn, domain: str, gap_min: int, rebuild: bool,
                markers: dict | None = None):
    """把该域 raw 切成窗口写 distill_units。

    幂等：unit_id = <ns>:<source>:<起始ts整数>，已存在则跳过（不覆盖 state，
    否则会把已蒸馏窗口打回 pending 重蒸）。
    ⚠️ 尾窗口保护：最后一窗若距今 < gap_min 分钟，本轮不落账（对话可能还在继续）。
    """
    markers = markers or speaker_markers()
    rows = conn.execute(
        "SELECT id, content, timestamp, source, tags FROM chunks "
        "WHERE namespace=? AND tier='raw' AND state='active' "
        "ORDER BY source, timestamp",
        (domain,),
    ).fetchall()

    kept, filtered = [], {}
    filtered_rows = []
    for r in rows:
        why = classify_junk(r["content"], r["source"], r["tags"], markers)
        if why:
            filtered[r["id"]] = why
            filtered_rows.append((r["source"], float(r["timestamp"]), r["id"], why))
        else:
            kept.append(r)

    units, cur, prev = [], [], None
    for r in kept:
        if prev is not None and (
            r["source"] != prev["source"]
            or (float(r["timestamp"]) - float(prev["timestamp"])) > gap_min * 60
        ):
            units.append(cur)
            cur = []
        cur.append(r)
        prev = r
    if cur:
        units.append(cur)

    now = time.time()
    if rebuild:
        conn.execute("DELETE FROM distill_units WHERE namespace=?", (domain,))

    new_n = skipped_n = held_n = 0
    prev_uid = None
    for w in units:
        ts_start = float(w[0]["timestamp"])
        ts_end = float(w[-1]["timestamp"])
        uid = f"{domain}:{w[0]['source']}:{int(ts_start)}"
        if (now - ts_end) < gap_min * 60:
            held_n += 1
            prev_uid = uid
            continue
        exists = conn.execute("SELECT 1 FROM distill_units WHERE unit_id=?", (uid,)).fetchone()
        if exists:
            skipped_n += 1
            prev_uid = uid
            continue
        w_filtered = {
            rid: why for src, t, rid, why in filtered_rows
            if src == w[0]["source"] and ts_start <= t <= ts_end
        }
        conn.execute(
            "INSERT INTO distill_units (unit_id, namespace, source, ts_start, ts_end, "
            "turn_count, char_count, raw_ids, prev_unit, filtered_ids, has_correction, "
            "state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, domain, w[0]["source"], ts_start, ts_end, len(w),
             sum(len(x["content"] or "") for x in w),
             json.dumps([x["id"] for x in w], ensure_ascii=False),
             prev_uid,
             json.dumps(w_filtered, ensure_ascii=False) if w_filtered else None,
             1 if any(has_correction(x["content"], markers) for x in w) else 0,
             "pending", now),
        )
        new_n += 1
        prev_uid = uid
    conn.commit()

    return {
        "raw_total": len(rows),
        "kept": len(kept),
        "filtered": len(filtered),
        "filtered_by": _count_by(filtered),
        "windows": len(units),
        "new_units": new_n,
        "skipped_existing": skipped_n,
        "held_inprogress": held_n,
    }


def _count_by(filtered: dict) -> dict:
    out = {}
    for why in filtered.values():
        out[why] = out.get(why, 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser(prog="xtmem_distill_units")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN,
                    help="只处理该 namespace；缺省=库内全部 namespace")
    ap.add_argument("--gap", type=int, default=GAP_MIN_DEFAULT)
    ap.add_argument("--rebuild", action="store_true",
                    help="清掉该域全部窗口账重建（⚠️ 已蒸馏状态一并丢，只在改切窗口径时用）")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db_path = common.resolve_db_path(args.db)
    conn = common.open_db(db_path)
    try:
        ensure_schema(conn)
        domains = ([args.domain] if args.domain
                   else common.resolve_namespaces(conn))
        if not domains:
            print(json.dumps({"ok": True, "db": db_path, "results": [],
                              "note": "no namespaces found"}, ensure_ascii=False))
            return 0
        results = []
        for domain in domains:
            stat = build_units(conn, domain, args.gap, args.rebuild)
            pend = conn.execute(
                "SELECT COUNT(*) n FROM distill_units WHERE namespace=? AND state='pending'",
                (domain,),
            ).fetchone()["n"]
            stat.update({"ok": True, "domain": domain, "gap_min": args.gap,
                         "pending_total": pend})
            results.append(stat)
        print(json.dumps({"ok": True, "db": db_path, "results": results},
                         ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
