# -*- coding: utf-8 -*-
"""XTMEM 关系网自动演化引擎 v1.0（2026-08-27 assistant-b）

零 LLM 纯规则：扫 messages 表近期互动 → 调整 relations 表 sentiment/trust → 留痕 history。
设计原则：慢热（单次幅度小）、有地板有天花板（关系不会崩也不会凭空成神）、全程留痕可回溯。

演化规则：
  近 7 天互动 >= 3 次   -> trust +0.03 (上限0.95), sentiment +0.02 (上限0.90)  「处得来」
  近 7 天互动 1-2 次    -> sentiment +0.005 (上限0.90)                              「保持温度」
  近 7 天互动 0 次      -> 无动作                                                    「沉默不算错」
  超 21 天完全无互动    -> sentiment -0.05 (地板0.15)                               「淡了」
上限特殊：assistant-a(personal)↔用户A官设情侣档为全局最高信任关系样板，不参与衰减冷处理的下限拉平。

用法：
  python xtmem_relations_evolve.py            # 正式执行（写库）
  python xtmem_relations_evolve.py --dry-run  # 只演算打印，不落库

cron 建议：每周一次（与 xtmem_autodream 同窗口）。
"""
import sqlite3, json, time, sys, argparse, os, subprocess

try:  # 包内与同目录直跑两态：库路径 / CLI 统一接线
    from . import xtmem_common as common
except ImportError:  # pragma: no cover
    import xtmem_common as common

# 库与 CLI 都由 xtmem_common 解析（$XTMEM_DB_PATH / $XTMEM_CLI 优先）。
# ⚠️ 历史版本在这里写死宿主盘符与 python.exe 绝对路径——换机器必挂，公开仓也不该带。
DB = common.resolve_db_path()
WEEK = 86400 * 7
THREE_WEEKS = 86400 * 21
CAP_SENTIMENT = 0.90
CAP_TRUST = 0.95
FLOOR_SENTIMENT = 0.15


def pair_key(a, b):
    return tuple(sorted([a, b]))


def main(dry_run=False):
    now = time.time()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) 统计近 7 天每对互动次数（无向）
    counts = {}
    for r in conn.execute("SELECT from_agent, to_agent FROM messages WHERE ts >= ?", (now - WEEK,)):
        k = pair_key(r["from_agent"], r["to_agent"])
        counts[k] = counts.get(k, 0) + 1

    # 2) 遍历关系对做演化
    actions = []
    for rel in conn.execute("SELECT * FROM relations").fetchall():
        k = pair_key(rel["agent_a"], rel["agent_b"])
        n = counts.get(k, 0)
        last = rel["last_interaction"] or 0
        s, t = float(rel["sentiment"] or 0), float(rel["trust"] or 0)
        ds, dt, why = 0.0, 0.0, ""
        if n >= 3:
            ds, dt, why = min(0.02, CAP_SENTIMENT - s), min(0.03, CAP_TRUST - t), f"weekly_bond(n={n})"
        elif n >= 1:
            ds, why = min(0.005, CAP_SENTIMENT - s), f"gentle_warmth(n={n})"
        elif now - last > THREE_WEEKS:
            ds, why = max(-0.05, FLOOR_SENTIMENT - s if s > FLOOR_SENTIMENT else 0.0), "cooling_off(>21d)"
        if abs(ds) < 1e-6 and abs(dt) < 1e-6:
            continue
        new_s, new_t = round(s + ds, 4), round(t + dt, 4)
        actions.append({"pair": list(k), "n_week": n, "rule": why,
                        "sentiment": f"{s:.3f}->{new_s}", "trust": f"{t:.3f}->{new_t}",
                        "agent_a": rel["agent_a"], "agent_b": rel["agent_b"],
                        "new_sentiment": new_s, "new_trust": new_t})
        if not dry_run:
            hist = json.loads(rel["history"] or "[]")
            hist.append({"ts": now, "note": f"auto-evolve:{why}; sentiment {s:.3f}->{new_s}; trust {t:.3f}->{new_t}"})
            conn.execute(
                "UPDATE relations SET sentiment=?, trust=?, history=? WHERE agent_a=? AND agent_b=?",
                (new_s, new_t, json.dumps(hist, ensure_ascii=False), rel["agent_a"], rel["agent_b"]),
            )

    if not dry_run:
        # R2.1（2026-08-31）：演化事实走 CLI store 全链路（content_hash 归 MD5、注入校验、
        # FTS/embedding 恢复），不再裸插 SQL；派生记忆默认 private，要公开走 reveal/promotion。
        if actions:
            brief = "; ".join(f"{a['pair'][0]}×{a['pair'][1]}:{a['rule']}" for a in actions)
            text = f"XTMEM 关系网自动演化周报 {time.strftime('%Y-%m-%d')}：{brief}。由 xtmem_relations_evolve.py 规则引擎生成（零LLM）。"
            try:
                common.store_via_cli(
                    db_path=DB, agent="workbuddy", namespace="workbuddy",
                    text=text, importance=0.5, derived_from="", source_ids=[],
                    tier="refined", track="default",
                    tags=["relations", "evolution"], project="xtmem",
                    visibility="private", timeout=120,
                )
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"relations_evolve store failed: {e}"},
                                 ensure_ascii=False), file=sys.stderr)
        conn.commit()
    conn.close()

    print(json.dumps({"ok": True, "dry_run": dry_run,
                      "interactions_this_week": {"×".join(k): v for k, v in counts.items()},
                      "actions": actions}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(main(ap.parse_args().dry_run))
