# -*- coding: utf-8 -*-
"""memory/a2a_client.py — 驾驶舱调度器用的 A2A v1.0 客户端层（2026-09-02，小D）。

stdlib urllib，零 a2a-sdk 依赖。提供 a2a_call(agent, text, context_id) -> (reply, ctx, state)，
供 chat_scheduler.run_round 注入调用。peer 解析：内置 xuantie(9900)/xiaod(9901)，或传完整 URL。
method 双轨：默认 SendMessage（PascalCase，对齐 hermes 客户端），-32601 自动回退 message/send。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

DEFAULT_TIMEOUT = 120
PROTOCOL_VERSION = "1.0"

PEERS = {
    "xuantie": "http://127.0.0.1:9900",
    "xiaod": "http://127.0.0.1:9901",
}

# P5.1-2b（2026-09-02 契约缝合）：照渊台契约 AgentId → A2A peer 注册名。
# 前端/房间 members 统一用契约 id（dsh），a2a 层自动翻成 xiaod（9901）；其余同名。
AGENT_ALIASES = {"dsh": "xiaod"}

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})")


def redact(text):
    return _SECRET_RE.sub("[REDACTED]", text or "")


def resolve_peer(agent):
    if agent.startswith("http://") or agent.startswith("https://"):
        return agent.rstrip("/")
    agent = AGENT_ALIASES.get(agent, agent)
    return PEERS.get(agent, "").rstrip("/")


def _fetch_card(base):
    req = urllib.request.Request(base + "/.well-known/agent-card.json", method="GET")
    with urllib.request.urlopen(req, timeout=min(DEFAULT_TIMEOUT, 30)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rpc_url(base):
    try:
        card = _fetch_card(base)
        for iface in card.get("supportedInterfaces", []) or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                return str(iface["url"]).rstrip("/")
        if isinstance(card.get("url"), str) and card["url"]:
            return card["url"].rstrip("/")
    except Exception:
        pass
    return base


def _post(base, method, message, context_id):
    ctx = context_id or ("ctx-" + uuid.uuid4().hex[:16])
    body = {"jsonrpc": "2.0", "id": "task-" + uuid.uuid4().hex[:16], "method": method,
            "params": {"message": {"role": "user", "parts": [{"text": redact(message), "mediaType": "text/plain"}],
                                  "messageId": uuid.uuid4().hex, "contextId": ctx}}}
    data = json.dumps(body).encode("utf-8")
    url = _rpc_url(base)
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "A2A-Version": PROTOCOL_VERSION}, method="POST")
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def a2a_call(agent, text, context_id=""):
    """A2A message/send：返回 (reply_text, ctx, state)。超时/错误抛异常（调度器降级）。"""
    base = resolve_peer(agent)
    if not base:
        raise ValueError("unknown peer: " + str(agent))
    if not base.startswith("http://127.0.0.1") and not base.startswith("http://localhost"):
        raise ValueError("A2A client only talks to 127.0.0.1 peers (no token => no remote)")
    resp = _post(base, "SendMessage", text, context_id)
    if "error" in resp:
        err = resp["error"] or {}
        if err.get("code") == -32601:
            # 双轨回退：PascalCase 未识别 → message/send
            resp = _post(base, "message/send", text, context_id)
        else:
            raise ValueError("peer error: " + str(err.get("message", err)))
    if "error" in resp:
        raise ValueError("peer error: " + str(resp["error"]))
    result = resp.get("result", {})
    payload = result.get("task") or result.get("message") or result
    if isinstance(payload, dict):
        for art in payload.get("artifacts", []) or []:
            t = _extract_text(art)
            if t:
                return t, payload.get("contextId", context_id or ""), str((payload.get("status") or {}).get("state", ""))
        st = payload.get("status") or {}
        if st.get("message"):
            return _extract_text(st["message"]), payload.get("contextId", context_id or ""), str(st.get("state", ""))
        t = _extract_text(payload)
        if t:
            return t, payload.get("contextId", context_id or ""), str((payload.get("status") or {}).get("state", ""))
    return str(payload), context_id or "", ""


def _extract_text(obj):
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return str(obj)
    msg = obj.get("message", obj)
    parts = msg.get("parts", []) if isinstance(msg, dict) else []
    chunks = []
    for p in parts if isinstance(parts, list) else []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            chunks.append(p["text"])
    return "\n".join(chunks)
