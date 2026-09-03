# -*- coding: utf-8 -*-
"""xtmem_lark_talk.py — 江北⇄玄铁飞书对话一键闭环 v1.0（2026-08-27）

一条命令完成三连：①我的话推卡片上窗 → ②hermes one-shot 拉玄铁真实回话 → ③回复推卡上窗。
窗口里呈现为一段自然聊天流（两张卡：阿北发来消息 / 玄铁发来消息）。

用法：
  python xtmem_lark_talk.py --text "老板娘忙嘛呢"          # 全流程（调模型，花少量 API 费）
  python xtmem_lark_talk.py --text "xxx" --no-reply        # 只发我的卡，不拉玄铁
  python xtmem_lark_talk.py --dry-run --text "hi"          # 演算不发送

依赖：xtmem_lark_notify.py（同目录）+ hermes.exe（Python311 Scripts）+ 玄铁 profile .env。
安全：one-shot 限 --max-turns 2 / --run-budget 180；专用会话 "XTMEM总线" 可续聊。
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
NOTIFY = os.path.join(HERE, "xtmem_lark_notify.py")
PY = r"<PYTHON_EXE>"
HERMES = r"<HERMES_EXE>"
HERMES_HOME = r"<XT_ROOT>/mimox-data/profiles/xuantie_lark"
HERMES_PROFILE = "xuantie_lark"
WORK_DIR = r"<XT_ROOT>/mimox-data/profiles/xuantie_lark"
SESSION_NAME = "XTMEM总线"
MAX_TURNS = "2"
BUDGET = "180"


def run_notify(text: str, sender: str) -> dict:
    r = subprocess.run(
        [PY, NOTIFY, "--text", text, "--from", sender],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    try:
        return json.loads(r.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": (r.stderr or r.stdout)[:300]}


# ── 对话双录：双方原话都抄送进共享库（2026-08-27 补：此前只有窗口可见，玄铁本体不知情）──
XTMEM = r"<XT_ROOT>/mimox-data/xtmem/xtmem.py"


def archive(text: str, author: str):
    """作者=workbuddy 存 workbuddy 域；author=玄铁 存 xuantie_lark 域（她自己的会话 recall 必命中）。
    另用 msg send 各留一条流水。"""
    ns = "xuantie_lark" if author != "阿北" else "workbuddy"
    label = "阿北" if author == "阿北" else "玄铁"
    arc = f"【总线对话存档 {time.strftime('%m-%d %H:%M')}】{label}：{text[:1500]}"
    # R1.7（2026-08-31）：私聊存档默认 private + 身份跟随目标域（env 注入 XTMEM_ACTOR）
    try:
        subprocess.run(
            [PY, XTMEM, "store", "--agent", "workbuddy", "--namespace", ns,
             "--project", "bus-chat", "--tags", "bus,chat",
             "--importance", "0.65", "--visibility", "private", "--text", arc],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**os.environ, "XTMEM_ACTOR": ns},
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"lark_talk archive store failed: {e}"},
                         ensure_ascii=False), file=sys.stderr)
    try:
        subprocess.run(
            [PY, XTMEM, "msg", "send", "--from", label, "--to", "xuantie_lark" if label == "阿北" else "workbuddy",
             "--text", text[:800]],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"lark_talk archive msg failed: {e}"},
                         ensure_ascii=False), file=sys.stderr)


def ask_xuantie(my_text: str) -> dict:
    """one-shot 拉玄铁回话：query=我的话原文（来源标注一行），不写指令腔。"""
    query = f"【阿北 · 刚弹进你飞书窗口的卡片正文】\n\n{my_text}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(query)
        qf = f.name
    env = dict(os.environ, HERMES_HOME=HERMES_HOME)
    try:
        r = subprocess.run(
            [HERMES, "-p", HERMES_PROFILE, "chat",
             "--in", WORK_DIR, "-c", SESSION_NAME, "--create-if-missing", "-Q",
             "--query-file", qf, "--max-turns", MAX_TURNS, "--run-budget", BUDGET],
            capture_output=True, text=True, encoding="utf-8", timeout=240, env=env,
        )
    finally:
        try: os.unlink(qf)
        except OSError: pass
    out = (r.stdout or "").strip()
    # -Q 模式下回复在 stdout 尾部；掐掉 session 头部行
    for marker in ("session_id:", "Session "):
        idx = out.rfind("\n\n", 0)
    # 简化：取最后一个空行之后的文本为正文
    parts = [p.strip() for p in out.split("\n\n") if p.strip()]
    reply = parts[-1] if parts else ""
    if not reply or ("session_id" in reply and len(parts) == 1):
        return {"ok": False, "error": "hermes 无有效回复", "raw": out[-400:], "err": (r.stderr or "")[-200:]}
    return {"ok": True, "reply": reply}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--from", dest="sender", default="阿北")
    ap.add_argument("--target", default="dm")
    ap.add_argument("--no-reply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True,
                          "steps": ["notify(阿北→DM)", *(["skip"] if args.no_reply else ["hermes one-shot", "notify(玄铁→DM)"])]},
                         ensure_ascii=False))
        return 0

    log = {}

    # ① 我的话上窗
    log["sent_card"] = run_notify(args.text, args.sender)
    archive(args.text, "阿北")

    if args.no_reply:
        print(json.dumps({"ok": True, **log}, ensure_ascii=False))
        return 0

    # ②③ 玄铁回话 + 上窗 + 存档
    r = ask_xuantie(args.text)
    log["ask_xuantie"] = {k: v for k, v in r.items() if k != "reply"}
    if r.get("ok"):
        log["reply_card"] = run_notify(r["reply"], "玄铁")
        log["reply_preview"] = r["reply"][:120]
        archive(r["reply"], "玄铁")
    else:
        log["reply_card"] = {"ok": False}

    ok = bool(log["sent_card"].get("ok")) and (args.no_reply or bool(log["reply_card"].get("ok")))
    print(json.dumps({"ok": ok, **log}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
