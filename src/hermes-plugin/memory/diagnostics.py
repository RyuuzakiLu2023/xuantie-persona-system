# ================================================================
# O01 运行诊断 · 总导游图
# 作用：让关键失败可按同一个关联 ID 追踪，日志只留阶段/状态和哈希。
# 依赖：Python 标准库；给 CLI、provider、引擎及服务共用。
# 流程：operation 建请求上下文 → emit 固定字段/计数 → 节流/轮转日志 → snapshot。
# 铁律：不写异常正文、查询正文、凭据或任意 caller 字段；诊断失败不改变业务结果。
# 更新：2026-10-12 · O01 固定字段、跨线程关联、累计计数和有界日志。
# ================================================================
"""Safe, bounded operational evidence independent of the SQLite write path."""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import threading
import time
import uuid

COMPONENTS = frozenset({'cli', 'serve', 'wal', 'engine', 'retriever', 'provider', 'a2a', 'rest'})
STAGES = frozenset({'capture', 'write', 'write.wal', 'write.vector_store', 'write.index',
    'recovery', 'recovery.wal_read', 'recovery.cleanup', 'recovery.replay',
    'retrieval', 'retrieval.embedding', 'retrieval.fts', 'retrieval.tier',
    'context.assembly', 'delivery.prefetch', 'delivery.task', 'delivery.poll',
    'intake.enqueue', 'intake.outbox_persist', 'intake.outbox_load', 'intake.replay',
    'permissions', 'serve.dispatch', 'serve.transport', 'maintenance.warm', 'accounting'})
STATUSES = frozenset({'ok', 'failed', 'rejected', 'degraded', 'skipped', 'retry', 'queued',
                      'delivered', 'timeout', 'invalidated', 'duplicate'})
FALLBACKS = frozenset({'none', 'hash_embedding', 'vector_only', 'memory_bm25', 'raw_tier',
    'local_cli', 'outbox', 'retry', 'no_context', 'deferred_recovery', 'base_rpc', 'legacy_rpc',
    'invalid_wal_line', 'unknown'})
TIER_ROUTES = frozenset({'refined-first', 'raw-fallback', 'refined-empty-raw-fallback', 'empty', 'unknown'})
BACKENDS = frozenset({'fts5', 'memory_bm25', 'hybrid', 'embedding_provider', 'hash_embedding',
                       'vector_only', 'local_cli', 'tcp_serve', 'a2a_rpc', 'unknown'})
CONTEXT_STATES = frozenset({'idle', 'skipped', 'timeout', 'error', 'delivered', 'cooldown',
                            'no_match', 'low_score', 'filtered', 'invalidated', 'unknown'})
ERROR_TYPES = frozenset({'Exception', 'RuntimeError', 'ValueError', 'TypeError', 'OSError',
    'PermissionError', 'FileNotFoundError', 'TimeoutError', 'JSONDecodeError', 'UnicodeDecodeError',
    'OperationalError', 'IntegrityError', 'DatabaseError', 'ProgrammingError', 'RequestRejected',
    'HTTPError', 'URLError', 'HTTPStatusError', 'ConnectError', 'ReadTimeout', 'ConnectTimeout'})
_context = ContextVar('xtmem_diagnostic_context', default=None)
_lock = threading.RLock()
_counts = Counter()
_recent = deque(maxlen=64)
_last_logged = {}
_logger = logging.getLogger('xtmem.diagnostics')
MAX_LOG_BYTES = 1024 * 1024
THROTTLE_SECS = 5.0
_sink_failure_count = 0


def fingerprint(value):
    """Only a short digest leaves the operation; private input stays in memory."""
    return hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest()[:16]


def correlation_id():
    active = _context.get()
    return active['correlation_id'] if active else 'op-' + uuid.uuid4().hex


@contextmanager
def operation(component, stage, *, correlation=None):
    """Nested engine work and copied worker contexts retain the same operation ID."""
    prior = _context.get()
    candidate = correlation or (prior or {}).get('correlation_id') or ('op-' + uuid.uuid4().hex)
    if not isinstance(candidate, str) or not re.fullmatch(r'op-[0-9a-f]{32}', candidate):
        candidate = 'op-' + hashlib.sha256(str(candidate).encode('utf-8', 'replace')).hexdigest()[:32]
    binding = _context.set({'correlation_id': candidate, 'component': component, 'stage': stage})
    try:
        yield candidate
    finally:
        _context.reset(binding)


def diagnosed(component, stage):
    """Observe an exception without changing its class, retry policy, or return value."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with operation(component, stage):
                try:
                    return function(*args, **kwargs)
                except Exception as error:
                    emit(component, stage, 'failed', error=error)
                    raise
        return wrapped
    return decorate


def _log_path():
    configured = os.environ.get('XTMEM_DIAGNOSTIC_DIR', '')
    db = os.environ.get('XTMEM_DB_PATH') or str(Path(os.environ.get('MIMOX_HOME', __import__("memory.public_runtime", fromlist=["home_file"]).home_file(''))) / '.hermes/memory/vector_store.db')
    directory = Path(configured) if configured else Path(db).resolve().parent / 'diagnostics'
    return directory / ('events-' + str(os.getpid()) + '.jsonl')


def _persist(record):
    """Per-process files avoid cross-process rewrite races; each file stays bounded."""
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
        previous = path.with_suffix('.previous.jsonl')
        os.replace(path, previous)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n')


def emit(component, stage, status, *, error=None, fallback_reason='none', query=None,
         tier_route=None, retrieval_backend=None, context_state=None, **metrics):
    """Only closed enums and bounded numeric metrics are admissible evidence."""
    global _sink_failure_count
    if (not isinstance(component, str) or component not in COMPONENTS
            or not isinstance(stage, str) or stage not in STAGES
            or not isinstance(status, str) or status not in STATUSES):
        return None
    record = {'format_version': 1, 'timestamp': time.time(), 'pid': os.getpid(),
              'correlation_id': correlation_id(), 'component': component, 'stage': stage,
              'status': status, 'fallback_reason': fallback_reason if isinstance(fallback_reason, str) and fallback_reason in FALLBACKS else 'unknown'}
    if error is not None:
        # Class names never use str(error); backend strings can contain passwords/body.
        name = type(error).__name__
        record['error_type'] = name if name in ERROR_TYPES else 'Exception'
        code = getattr(error, 'sqlite_errorcode', None)
        if type(code) is int:
            record['sqlite_errorcode'] = code
    if query is not None:
        record['query_hash'] = fingerprint(query)
    for key, value, allowed in [('tier_route', tier_route, TIER_ROUTES),
                                ('retrieval_backend', retrieval_backend, BACKENDS),
                                ('context_state', context_state, CONTEXT_STATES)]:
        if value is not None:
            record[key] = value if isinstance(value, str) and value in allowed else 'unknown'
    for key in ('count', 'retries', 'line_number', 'pending', 'elapsed_ms', 'candidate_count', 'generation'):
        value = metrics.get(key)
        if type(value) in (int, float) and 0 <= value <= 10**12:
            record[key] = value
    key = (component, stage, status)
    throttle_key = key + (record.get('error_type'), record['fallback_reason'])
    now = time.monotonic()
    with _lock:
        _counts[key] += 1
        _recent.append(record)
        error_event = status in {'failed', 'rejected', 'degraded', 'retry', 'timeout'}
        if error_event and now - _last_logged.get(throttle_key, -THROTTLE_SECS) < THROTTLE_SECS:
            _counts[('diagnostics', 'sink', 'throttled')] += 1
            return record
        # The set of possible enum combinations is finite, no caller string key growth.
        _last_logged[throttle_key] = now
        record['observed_count'] = _counts[key]
        try:
            _persist(record)
        except Exception:
            _sink_failure_count += 1
        try:
            _logger.log(logging.WARNING if error_event else logging.DEBUG,
                        'xtmem_operation %s', json.dumps(record, ensure_ascii=True, separators=(',', ':')))
        except Exception:
            _sink_failure_count += 1
    return record


def snapshot():
    """Read-only process evidence; never reads private memory or credentials."""
    with _lock:
        return {'format_version': 1, 'pid': os.getpid(), 'sink_failure_count': _sink_failure_count,
                'counts': [{'component': c, 'stage': s, 'status': o, 'count': n}
                           for (c, s, o), n in sorted(_counts.items())],
                'recent': list(_recent)}


def read_recent(limit=20):
    """Bounded disk evidence for CLI doctor; accept only records emitted by O01."""
    limit = max(1, min(int(limit), 64))
    directory = _log_path().parent
    records = []
    if directory.exists():
        paths = sorted(directory.glob('events-*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:16]
        for path in paths:
            try:
                with path.open('rb') as handle:
                    handle.seek(max(0, path.stat().st_size - 65536))
                    data = handle.read(65536)
                for line in data.splitlines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if (not isinstance(item, dict) or item.get('format_version') != 1
                            or not isinstance(item.get('component'), str) or item['component'] not in COMPONENTS
                            or not isinstance(item.get('stage'), str) or item['stage'] not in STAGES
                            or not isinstance(item.get('status'), str) or item['status'] not in STATUSES):
                        continue
                    # Files are not an authority to add private arbitrary metadata.
                    allowed = {'format_version', 'timestamp', 'pid', 'correlation_id', 'component',
                               'stage', 'status', 'fallback_reason', 'error_type', 'sqlite_errorcode',
                               'query_hash', 'tier_route', 'retrieval_backend', 'context_state',
                               'count', 'retries', 'line_number', 'pending', 'elapsed_ms',
                               'candidate_count', 'generation', 'observed_count'}
                    if set(item) - allowed:
                        continue
                    valid = (type(item.get('timestamp')) in (int, float)
                             and isinstance(item.get('correlation_id'), str)
                             and re.fullmatch(r'op-[0-9a-f]{32}', item['correlation_id']))
                    for key, values in [('fallback_reason', FALLBACKS), ('tier_route', TIER_ROUTES),
                                        ('retrieval_backend', BACKENDS), ('context_state', CONTEXT_STATES),
                                        ('error_type', ERROR_TYPES)]:
                        if key in item and (not isinstance(item[key], str) or item[key] not in values):
                            valid = False
                    if 'query_hash' in item and (not isinstance(item['query_hash'], str)
                            or not re.fullmatch('[0-9a-f]{16}', item['query_hash'])):
                        valid = False
                    for key in allowed - {'correlation_id', 'component', 'stage', 'status', 'fallback_reason',
                                          'error_type', 'query_hash', 'tier_route', 'retrieval_backend', 'context_state'}:
                        if key in item and type(item[key]) not in (int, float):
                            valid = False
                    if not valid:
                        continue
                    records.append(item)
            except OSError:
                continue
    records.sort(key=lambda r: r.get('timestamp', 0), reverse=True)
    return {'format_version': 1, 'count': min(len(records), limit), 'records': records[:limit]}
