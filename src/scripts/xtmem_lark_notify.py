# -*- coding: utf-8 -*-
"""xtmem_lark_notify.py — 江北→玄铁飞书 DM 推送器 v1.0（2026-08-27）

把「阿北发来消息：xxx」以交互卡片推到玄铁的飞书窗口（DM）。
复用玄铁已装的 hermes_feishu_card 包（FeishuClient + 玄铁 profile config.yaml 凭证）。

⚠️ 防呆设计（龙崎 2026-08-27 要求，群/私聊绝不混）：
  - TARGETS 白名单只认短名 "dm"；不允许裸传 oc_ 字符串。
  - 群「瞎扯淡」条目被注释封存——要开必须显式改代码 + --target group。

用法：
  python xtmem_lark_notify.py --text "三病修好了"            # 默认 target=dm
  python xtmem_lark_notify.py --dry-run                      # 只构造不发送
输出：JSON {ok, message_id?, target, chat_suffix}
"""
from __future__ import annotations

import argparse, asyncio, json, re, sys

# ── 目标白名单（防混发）──────────────────────────────────────────
# dm = 龙崎↔玄铁 的飞书私聊（gateway_routing 里 chat_type=dm，2026-08-27 活跃）
TARGETS = {
    "dm": "oc_f3f50fd686192f4fa626dec853eef857",
    # "group": "oc_18c822665e69cb8e8fa9dc46644e90e4",  # ⛔群「瞎扯淡」封存中
}
DEFAULT_TARGET = "dm"

CONFIG_YAML = r"<XT_ROOT>\mimox-data\profiles\xuantie_lark\config.yaml"
ENV_FILE = r"<XT_ROOT>\mimox-data\profiles\xuantie_lark\.env"  # 凭证真身（与网关同源）
FROM_NAME = "阿北"


def load_feishu_creds(env_path=ENV_FILE):
    """从 profile .env 读 FEISHU_APP_ID/APP_SECRET（键=值 行格式，# 注释跳过）。"""
    vals = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    app_id = vals.get("FEISHU_APP_ID") or ""
    app_secret = vals.get("FEISHU_APP_SECRET") or ""
    base_url = None
    domain = (vals.get("FEISHU_DOMAIN") or "").lower()
    if domain == "lark":
        base_url = "https://open.larksuite.com/open-apis"
    if not app_id or not app_secret:
        raise SystemExit(json.dumps({"ok": False, "error": f".env 里缺 FEISHU_APP_ID/APP_SECRET ({env_path})"}))
    return app_id, app_secret, base_url


def build_card(text: str, sender: str = FROM_NAME) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"💬 {sender}发来消息"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": text},
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                              "content": "XTMEM 跨角色消息总线 · 江北(WorkBuddy) → 玄铁(xuantie_lark)"}],
            },
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--from", dest="sender", default=FROM_NAME)
    ap.add_argument("--target", default=DEFAULT_TARGET, choices=sorted(TARGETS),
                    help="白名单短名；禁裸传 oc_")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.target not in TARGETS:
        raise SystemExit(json.dumps({"ok": False, "error": f"target 必须是 {sorted(TARGETS)}；禁止裸传 oc_"}))
    chat_id = TARGETS[args.target]

    card = build_card(args.text, args.sender)
    out = {"target": args.target, "chat_suffix": chat_id[-8:], "dry_run": args.dry_run,
           "card_header": card["header"]["title"]["content"]}

    if args.dry_run:
        print(json.dumps({"ok": True, **out}, ensure_ascii=False))
        return 0

    from hermes_feishu_card.feishu_client import FeishuClient, FeishuClientConfig

    app_id, app_secret, base_url = load_feishu_creds()
    kw = {"app_id": app_id, "app_secret": app_secret}
    if base_url:
        kw["base_url"] = base_url
    client = FeishuClient(FeishuClientConfig(**kw))
    message_id = asyncio.run(client.send_card(chat_id, card))
    out.update({"ok": True, "message_id": message_id})
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
