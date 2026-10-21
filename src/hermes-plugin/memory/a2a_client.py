# -*- coding: utf-8 -*-
"""memory/a2a_client.py — 驾驶舱调度器用的 A2A v1.0 客户端层（2026-09-02，小D）。

stdlib urllib，零 a2a-sdk 依赖。提供 a2a_call(agent, text, context_id) -> (reply, ctx, state)，
供 chat_scheduler.run_round 注入调用。peer 解析：内置 xuantie(9900)/xiaod(9901)，或传完整 URL。
method 双轨：默认 SendMessage（PascalCase，对齐 hermes 客户端），-32601 自动回退 message/send。
2026-10-12 O01：发送/轮询/协议回落记录同一关联 ID；日志不记消息或异常正文。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from .diagnostics import diagnosed, emit

DEFAULT_TIMEOUT = 120
PROTOCOL_VERSION = "1.0"

PEERS = json.loads(os.environ.get("XTMEM_A2A_PEERS", "{}"))

# P5.1-2b（2026-09-02 契约缝合）：照渊台契约 AgentId → A2A peer 注册名。
# 前端/房间 members 统一用契约 id（dsh），a2a 层自动翻成 xiaod（9901）；其余同名。
AGENT_ALIASES = {}

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
    except Exception as exc:
        emit('a2a', 'delivery.task', 'degraded', error=exc,
             fallback_reason='base_rpc', retrieval_backend='a2a_rpc')
    return base


def _post(base, method, message, context_id, request_id=None, message_id=None):
    """发送 A2A 请求并保留 request/message 身份；JSON-RPC id 不代表对端 task id。"""
    ctx = context_id or ("ctx-" + uuid.uuid4().hex[:16])
    body = {"jsonrpc": "2.0", "id": request_id or ("rpc-" + uuid.uuid4().hex[:16]), "method": method,
            "params": {"message": {"role": "user", "parts": [{"text": redact(message), "mediaType": "text/plain"}],
                                  "messageId": message_id or uuid.uuid4().hex, "contextId": ctx}}}
    data = json.dumps(body).encode("utf-8")
    url = _rpc_url(base)
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "A2A-Version": PROTOCOL_VERSION}, method="POST")
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_state(state):
    """把 A2A 的状态串归一成裸终态名：'TASK_STATE_COMPLETED' / 'task.Completed' → 'COMPLETED'。

    ⚠️【不】退回只按 '.' 切（2026-10-15 实测踩到）：A2A v1 的枚举是
    `TASK_STATE_COMPLETED`（下划线），只 rsplit('.') 得到原串，任何 `== 'COMPLETED'`
    判断永远不成立 —— 会把同步终态答案当非终态丢掉（R01/R02 修完又反向丢件）。
    下划线前缀与点号命名空间都要剥，且大小写归一。
    """
    s = str(state or "").strip().upper()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    if s.startswith("TASK_STATE_"):
        s = s[len("TASK_STATE_"):]
    return s


TERMINAL_STATES = ("COMPLETED", "SUCCEEDED")
FAILURE_STATES = ("FAILED", "REJECTED", "CANCELED", "CANCELLED")


def _task_identity(resp):
    """只从响应里的真实 task 对象取 task id；RPC request id 永不冒充 task id。"""
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, dict):
        return ""
    task = result.get("task")
    if isinstance(task, dict):
        return str(task.get("id") or "")
    # A2A v1 may return a Task directly as the result object.
    if isinstance(result.get("id"), str) and ("status" in result or "artifacts" in result):
        return str(result.get("id") or "")
    return ""


def _send_operation(agent, text, context_id, operation_id):
    """重复投递沿用相同操作、消息和请求身份，支持对端去重。"""
    base = resolve_peer(agent)
    _local_peer(base)
    op = str(operation_id or uuid.uuid4().hex)
    message_id = "msg-" + op
    request_id = "rpc-" + op
    resp = _post(base, "SendMessage", text, context_id, request_id=request_id, message_id=message_id)
    if "error" in resp:
        err = resp["error"] or {}
        if err.get("code") == -32601:
            emit('a2a', 'delivery.task', 'degraded', fallback_reason='legacy_rpc')
            resp = _post(base, "message/send", text, context_id, request_id=request_id, message_id=message_id)
        else:
            raise ValueError("peer error: " + str(err.get("message", err)))
    if "error" in resp:
        raise ValueError("peer error: " + str(resp["error"]))
    reply, ctx, state = _parse_result(resp, context_id)
    return reply, ctx, state, _task_identity(resp), request_id, message_id


def a2a_send_operation(agent, text, context_id="", operation_id=None):
    """可幂等投递的入口：返回 (reply, context_id, state, task_id, request_id, message_id)。"""
    return _send_operation(agent, text, context_id, operation_id)


def _parse_result(resp, context_id=""):
    """从 SendMessage / tasks/get 的 JSON-RPC 响应里提 (reply_text, ctx, state)。
    兼容 A2A v1.0 的 {task|message} 包装：先找 artifacts，再找 status.message，最后兜底。"""
    result = resp.get("result", {})
    payload = result.get("task") or result.get("message") or result
    ctx = context_id or ""
    if isinstance(payload, dict):
        for art in payload.get("artifacts", []) or []:
            t = _extract_text(art)
            if t:
                return t, str(payload.get("contextId") or ctx), str((payload.get("status") or {}).get("state", ""))
        st = payload.get("status") or {}
        if st.get("message"):
            return _extract_text(st["message"]), str(payload.get("contextId") or ctx), str(st.get("state", ""))
        t = _extract_text(payload)
        if t:
            return t, str(payload.get("contextId") or ctx), str((payload.get("status") or {}).get("state", ""))
    return str(payload), ctx, ""


def _local_peer(base):
    if not base:
        raise ValueError("unknown peer: " + str(base))
    if not base.startswith("http://127.0.0.1") and not base.startswith("http://localhost"):
        raise ValueError("A2A client only talks to 127.0.0.1 peers (no token => no remote)")


@diagnosed('a2a', 'delivery.task')
def a2a_send(agent, text, context_id=""):
    """兼容旧签名，task_id 仅来自真实 task 对象；纯 message 响应返回空 task_id。"""
    reply, ctx, state, task_id, _request_id, _message_id = _send_operation(
        agent, text, context_id, operation_id=None)
    return reply, ctx, state, task_id


@diagnosed('a2a', 'delivery.poll')
def a2a_get_task(agent, task_id):
    """A2A tasks/get：取对端异步任务的最终状态与真回复。

    返回 (reply_text, ctx, state, actual_task_id) —— 第 4 项是响应里【实际】的 task id。

    ⚠️ 【不】再把返回签名退回 3 元组（2026-10-15 R05 返修）：原先这里把响应里的真实 task.id
    丢掉、只把调用方自己传进去的 task_id 当上下文回填，调用方就拿"我请求的号码"去校验回程——
    等于发查询写了 A 单号、回来的包裹是 B 也照收。真实号码必须原样上抛，由调用方核对。
    """
    base = resolve_peer(agent)
    _local_peer(base)
    body = {"jsonrpc": "2.0", "id": "poll-" + uuid.uuid4().hex[:8],
            "method": "tasks/get", "params": {"taskId": str(task_id)}}
    data = json.dumps(body).encode("utf-8")
    url = _rpc_url(base)
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "A2A-Version": PROTOCOL_VERSION},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if "error" in raw:
        raise ValueError("peer error: " + str(raw["error"]))
    reply, ctx, state = _parse_result(raw, str(task_id))
    return reply, ctx, state, _task_identity(raw)


def a2a_call(agent, text, context_id=""):
    """A2A message/send（兼容旧签名）：返回 (reply_text, ctx, state)。超时/错误抛异常。"""
    reply, ctx, state, _task = a2a_send(agent, text, context_id)
    return reply, ctx, state


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
