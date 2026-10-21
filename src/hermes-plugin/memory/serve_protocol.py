"""S01 native serve credentials, immutable identity, and bounded JSON transport.

Credentials are host-owned local files, never model arguments. This protects the
RPC boundary; clients sharing full filesystem access still share host trust.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
SECURITY_ERRORS = frozenset({'auth_required', 'auth_invalid', 'identity_conflict',
                             'command_forbidden', 'args_invalid', 'permission_denied',
                             'protocol_invalid', 'response_invalid'})
# 本协议**自己会的**全部错误码。serve 侧只会产出这些，客户端只许转述这些。
# ⚠️ 为什么必须是白名单而不是"净化字符集"（G7 返修 / 验收 N10）：
#   对端是完全不可信的。它把**本次收到的凭据**原样塞进 `error_code` 回传时，
#   那个串由合法字符组成 ⇒ 任何"过滤非法字符/截断长度"的规则都会放它过去，
#   于是普通 doctor 的 reason 里就出现了完整 token（实测就是这么漏的）。
#   字符集过滤解决的是"内容长得像不像正文"，解决不了"这个内容本来就是我们发给它的"。
#   唯一可靠的做法：**只转述我们自己定义的码**，白名单外一律换成固定占位码，
#   原始值一个字符都不外传。
KNOWN_ERROR_CODES = SECURITY_ERRORS | frozenset({
    'backend_error', 'response_invalid', 'unknown_error_code',
    'port_not_serving', 'not_serving_on_port', 'schema_mismatch', 'endpoint_unverified',
})
# 白名单外错误码的统一替身。保留"对端确实报了个我们不认识的码"这个**可归因事实**，
# 但不保留它的内容——可归因性与不外泄凭据/正文，两件都要。
UNKNOWN_ERROR_CODE = 'unknown_error_code'


def safe_error_code(value):
    """把对端给的错误码收敛到本协议白名单内；不认识的一律换成占位码。

    返回的一定是 KNOWN_ERROR_CODES 里的固定短串（或空串），
    **绝不**是调用方传入的原始文本。上层据此拼 reason 就不会回显对端内容。
    """
    if isinstance(value, str) and value in KNOWN_ERROR_CODES:
        return value
    if value in (None, ''):
        return ''
    return UNKNOWN_ERROR_CODE


ALLOWED_COMMANDS = frozenset({'recall', 'lookup', 'store', 'observe',
                              'agents', 'bulletin', 'stats', 'trace', 'hello'})
WRITE_COMMANDS = frozenset({'store', 'observe'})
# O02（2026-10-14）：只读握手/诊断类命令。**不含任何管理能力**——普通通道
# 依旧拿不到迁移、token 签发、批量删除（Spec §9.6 硬边界）。加命令前先问：
# 它会不会让 ordinary 通道多出写能力或越权视野？会 → 不许加。
READONLY_DIAGNOSTIC_COMMANDS = frozenset({'hello', 'stats'})


class RequestRejected(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def response(self):
        return {'ok': False, 'error_code': self.code, 'error': str(self),
                'fallback_allowed': False}


@dataclass(frozen=True)
class AuthContext:
    actor: str
    admin: bool = False


def validate_actor(actor):
    if (not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', actor)
            or actor.lower() in {'admin', 'owner'}):
        raise RequestRejected('identity_conflict', 'serve requires a fixed non-admin host actor')
    return actor


def credential_path(actor, db_path):
    validate_actor(actor)
    directory = Path(os.environ.get('XTMEM_SERVE_CREDENTIAL_DIR') or
                     str(Path(db_path).resolve().parent / 'serve-auth'))
    return directory / (actor + '.json')


def _protect_directory(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        account = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
        result = subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', account + ':(OI)(CI)F'],
                                capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RequestRejected('auth_invalid', 'cannot restrict serve credential directory')
    else:
        directory.chmod(0o700)


def actor_token(actor, db_path, *, create=False):
    """Host provisioning only. Callers keep actor fixed; no token appears in logs."""
    validate_actor(actor)
    configured = os.environ.get('XTMEM_SERVE_TOKEN', '')
    if configured:
        if len(configured) < 32 or not configured.isascii():
            raise RequestRejected('auth_invalid', 'serve credential is too short')
        return configured
    path = credential_path(actor, db_path)
    if create and not path.exists():
        _protect_directory(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump({'actor': actor, 'token': secrets.token_urlsafe(32),
                           'protocol_version': PROTOCOL_VERSION}, handle)
                handle.flush()
                os.fsync(handle.fileno())
    if not path.exists():
        return None
    for attempt in range(5):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            break
        except (OSError, ValueError):
            if attempt == 4:
                raise RequestRejected('auth_invalid', 'serve credential file is invalid') from None
            time.sleep(0.02)
    if (data.get('actor') != actor or not isinstance(data.get('token'), str)
            or len(data['token']) < 32 or not data['token'].isascii()
            or data.get('protocol_version') != PROTOCOL_VERSION):
        raise RequestRejected('auth_invalid', 'serve credential binding is invalid')
    return data['token']


def authorize(request, actor, token):
    """Authentication precedes all command and argument handling."""
    validate_actor(actor)
    if not isinstance(request, dict):
        raise RequestRejected('protocol_invalid', 'request must be an object')
    supplied = request.get('token')
    if not isinstance(supplied, str) or not supplied:
        raise RequestRejected('auth_required', 'serve credential required')
    if not supplied.isascii() or not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(supplied, token):
        raise RequestRejected('auth_invalid', 'invalid serve credential')
    if set(request) - {'cmd', 'args', 'token', 'protocol_version'}:
        raise RequestRejected('protocol_invalid', 'unknown request fields')
    if type(request.get('protocol_version', PROTOCOL_VERSION)) is not int or request.get('protocol_version', PROTOCOL_VERSION) != PROTOCOL_VERSION:
        raise RequestRejected('protocol_invalid', 'unsupported serve protocol')
    cmd = request.get('cmd')
    if not isinstance(cmd, str) or cmd not in ALLOWED_COMMANDS:
        raise RequestRejected('command_forbidden', 'command unavailable on ordinary serve channel')
    args = request.get('args', {})
    if not isinstance(args, dict):
        raise RequestRejected('args_invalid', 'args must be an object')
    return AuthContext(actor), cmd, args


def is_security_rejection(result):
    return isinstance(result, dict) and (result.get('fallback_allowed') is False
            or result.get('error_code') in SECURITY_ERRORS)


def send_request(host, port, request, token, timeout=8.0):
    """None means a transport failure; server or protocol rejection is terminal."""
    if not token:
        return RequestRejected('auth_required', 'serve credential required').response()
    wire = {**request, 'token': token, 'protocol_version': PROTOCOL_VERSION}
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall((json.dumps(wire, ensure_ascii=False) + '\n').encode('utf-8'))
            data = bytearray()
            while b'\n' not in data:
                part = conn.recv(65536)
                if not part:
                    return RequestRejected('response_invalid', 'serve response incomplete').response()
                data.extend(part)
                if len(data) > MAX_FRAME_BYTES:
                    return RequestRejected('response_invalid', 'serve response too large').response()
            try:
                result = json.loads(bytes(data).split(b'\n', 1)[0].decode('utf-8'))
            except (ValueError, UnicodeError):
                return RequestRejected('response_invalid', 'serve response invalid').response()
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                return RequestRejected('response_invalid', 'serve response schema invalid').response()
            return result
    except (OSError, TimeoutError):
        return None
