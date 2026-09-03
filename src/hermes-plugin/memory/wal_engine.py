
# ================================================================
# wal_engine.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：WAL（Write-Ahead Logging）记忆引擎——所有记忆操作先写日志，保证持久性；
#           重启/崩溃前未落盘的条目可重放补写。
#
# 结构：MemoryEntry dataclass（含 namespace/project/host_origin/visibility/tier）→
#       append/store + 跨进程锁 _locked_wal（Windows msvcrt 1 字节区间锁）→
#       read_uncompacted/mark_compressed/cleanup_expired/purge。
#
# 修改铁律：
# 1. 整文件重写（mark_compressed/cleanup_expired/purge）必须走 _locked_wal(required=True) 强锁，
#    拿不到就抛 RuntimeError，绝不无锁重写（并发重写丢刚写的日志）。
# 2. MemoryEntry to_json/from_json 用 asdict/**data——加字段自动序列化；旧 JSON 行缺字段走默认（向后兼容）。
# 3. append 只追加；读方持有旧句柄时 os.replace 仍原子生效（CRT 共享删除模式）。
# ================================================================
# hermes/memory/wal_engine.py
# WAL (Write-Ahead Logging) 记忆引擎
# 所有记忆操作先写日志，保证持久性

import json
import os
import time
import uuid
import logging
import platform
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from pathlib import Path
from .utils import estimate_tokens

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """单条记忆条目"""
    id: str = field(default_factory=lambda: f"mem-{uuid.uuid4().hex[:8]}")
    timestamp: float = field(default_factory=time.time)
    operation: Literal["store", "update", "delete"] = "store"
    type: Literal["conversation", "fact", "preference", "task", "tool_result", "error"] = "conversation"
    content: str = ""
    importance: float = 0.5          # 0.0 - 1.0
    tags: List[str] = field(default_factory=list)
    ttl: Optional[int] = None        # 秒，None = 永久
    token_count: int = 0             # 预估 token 数
    session_id: str = "default"
    compressed: bool = False         # 是否已被压缩
    compressed_into: Optional[str] = None  # 压缩后摘要的 ID
    # R2.2（2026-08-31）：WAL 条目带四标签，重放可恢复 namespace/project/host_origin/visibility；
    # 旧 JSON 行缺这些字段走默认 None（向后兼容）。
    namespace: Optional[str] = None
    project: Optional[str] = None
    host_origin: Optional[str] = None
    visibility: Optional[str] = None
    # P4.8 T3（2026-09-01）：分层库 tier——WAL 重放后仍保真（缺省 None→raw）
    tier: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "MemoryEntry":
        data = json.loads(line)
        return cls(**data)

    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return (time.time() - self.timestamp) > self.ttl


# P1-9（2026-08-31）：跨进程锁。所有 WAL 写操作（append / 整文件重写）共用
# <wal_dir>/.lock 锁文件上的 1 字节区间锁（msvcrt），防止：
#   - append 与 mark_compressed/cleanup_expired 整文件重写同时发生时互相踩踏
#   - Windows 上 os.replace 与未锁写入的竞态导致刚写的日志丢失
# 读操作不加锁：读方持有旧句柄时 replace 仍原子生效（CRT 共享删除模式）。
WAL_LOCK_ACQUIRE_TIMEOUT = 5.0      # 拿锁超时上限（秒）
WAL_LOCK_REWRITE_REQUIRED = True    # 重写操作强锁：拿不到就抛错，绝不无锁重写


@contextmanager
def _locked_wal(wal_dir: str, *, required: bool = False, timeout: float = WAL_LOCK_ACQUIRE_TIMEOUT):
    """跨进程互斥上下文。required=True 时拿不到锁会抛 RuntimeError（重写操作用）。"""
    lock_path = Path(wal_dir) / ".lock"
    with open(lock_path, "a+") as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write("\0")
            f.flush()
        locked = False
        if platform.system() == "Windows":
            import msvcrt
            f.seek(0)
            deadline = time.time() + timeout
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.02)
        if not locked and required:
            raise RuntimeError(
                f"WAL lock not acquired within {timeout:.1f}s: {lock_path}"
            )
        try:
            yield locked
        finally:
            if locked:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


class WALEngine:
    """WAL 记忆引擎

    职责：
    1. 所有记忆变更先写 WAL 文件（fsync 保证落盘）
    2. 提供读取接口（未压缩的条目）
    3. 标记已压缩条目
    4. 清理过期条目
    """

    def __init__(self, wal_dir: str = ".hermes/memory/wal", max_entries_before_compact: int = 50):
        self.wal_dir = Path(wal_dir)
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.wal_file = self.wal_dir / "current.wal"
        self.max_entries_before_compact = max_entries_before_compact

    def append(self, entry: MemoryEntry) -> None:
        """追加一条记忆到 WAL（fsync + Windows 文件锁保证持久性）

        Windows 说明：msvcrt.locking 锁定的是「当前文件指针」开始的字节区间。
        以追加模式写入后指针会前移，若在移动后的位置调用 LK_UNLCK 会解锁一段
        从未加锁的区间并抛出 PermissionError(Errno 13)。因此这里固定锁定并解锁
        字节 0（作为跨进程互斥的哨兵字节），写入前后都显式 seek 回该位置。
        """
        line = entry.to_json() + "\n"

        # P1-9：追加也走统一跨进程锁（失败可降级无锁，但重写操作是强锁、会等我们）
        with _locked_wal(self.wal_dir, required=False) as locked:
            if not locked:
                logger.warning("WAL append: lock not acquired, writing lock-free (%s)", entry.id)
            with open(self.wal_file, "a", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)  # 追加模式下写入始终落在文件末尾
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

        logger.debug("WAL append: %s (type=%s, importance=%.2f)", entry.id, entry.type, entry.importance)

    def store(self, content: str, **kwargs) -> MemoryEntry:
        """快捷方法：存储一条记忆"""
        entry = MemoryEntry(
            content=content,
            token_count=self._estimate_tokens(content),
            **kwargs,
        )
        self.append(entry)
        return entry

    def read_uncompacted(self) -> List[MemoryEntry]:
        """读取所有未压缩、未过期的条目"""
        entries = []
        if not self.wal_file.exists():
            return entries

        with open(self.wal_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = MemoryEntry.from_json(line)
                    if not entry.compressed and not entry.is_expired():
                        entries.append(entry)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Skipping malformed WAL line: %s", e)

        return entries

    def read_all(self) -> List[MemoryEntry]:
        """读取所有条目（包括已压缩的，用于调试）"""
        entries = []
        if not self.wal_file.exists():
            return entries

        with open(self.wal_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(MemoryEntry.from_json(line))
                except (json.JSONDecodeError, TypeError):
                    continue

        return entries

    def mark_compressed(self, entry_ids: List[str], summary_id: str) -> None:
        """标记一批条目已被压缩进某个摘要（P1-9：强锁重写，防并发吞日志）"""
        with _locked_wal(self.wal_dir, required=WAL_LOCK_REWRITE_REQUIRED):
            all_entries = self.read_all()
            updated_lines = []

            for entry in all_entries:
                if entry.id in entry_ids:
                    entry.compressed = True
                    entry.compressed_into = summary_id
                updated_lines.append(entry.to_json() + "\n")

            # 原子重写 WAL
            tmp_file = self.wal_file.with_suffix(".wal.tmp")
            with open(tmp_file, "w") as f:
                f.writelines(updated_lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.wal_file)

        logger.info("Marked %d entries as compressed into %s", len(entry_ids), summary_id)

    def cleanup_expired(self) -> int:
        """清理过期条目，返回清理数量（P1-9：强锁重写，防并发吞日志）"""
        with _locked_wal(self.wal_dir, required=WAL_LOCK_REWRITE_REQUIRED):
            all_entries = self.read_all()
            alive = [e for e in all_entries if not e.is_expired()]
            removed = len(all_entries) - len(alive)

            if removed > 0:
                lines = [e.to_json() + "\n" for e in alive]
                tmp_file = self.wal_file.with_suffix(".wal.tmp")
                with open(tmp_file, "w") as f:
                    f.writelines(lines)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_file, self.wal_file)
                logger.info("Cleaned up %d expired WAL entries", removed)

        return removed

    def should_compact(self) -> bool:
        """是否应该触发压缩"""
        entries = self.read_uncompacted()
        return len(entries) >= self.max_entries_before_compact

    def get_stats(self) -> dict:
        """获取 WAL 统计信息"""
        entries = self.read_all()
        uncompacted = [e for e in entries if not e.compressed and not e.is_expired()]
        total_tokens = sum(e.token_count for e in uncompacted)

        return {
            "total_entries": len(entries),
            "uncompacted_entries": len(uncompacted),
            "total_tokens": total_tokens,
            "total_chars": sum(len(e.content) for e in uncompacted),
        }

    def get_delta(self, since_ts: float = 0.0) -> List[MemoryEntry]:
        """增量同步：返回 since_ts 之后写入的所有条目。

        通过逐行扫描 WAL 文件并比较每条 entry 的 timestamp，高效地
        返回自指定时间戳以来的新条目，用于下游同步（例如向量存储
        增量索引）。

        Args:
            since_ts: Unix 时间戳，只返回 timestamp > since_ts 的条目。
                      默认 0.0 表示返回所有条目。

        Returns:
            符合条件的 MemoryEntry 列表（未过期的条目）。
        """
        results: List[MemoryEntry] = []
        if not self.wal_file.exists():
            return results

        with open(self.wal_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = MemoryEntry.from_json(line)
                    if entry.timestamp > since_ts and not entry.is_expired():
                        results.append(entry)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Skipping malformed WAL line in get_delta: %s", e)

        logger.debug("get_delta(since_ts=%.3f): found %d entries", since_ts, len(results))
        return results

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（中英混合）"""
        return estimate_tokens(text)
