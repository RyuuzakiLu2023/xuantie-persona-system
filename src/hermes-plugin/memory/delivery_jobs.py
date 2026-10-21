# -*- coding: utf-8 -*-
"""C01 持久 A2A 投递与幂等回程。

入参：既有 messages 行及 operation_id；返回：可重放任务和单次回程写入结果。
副作用：只操作本模块的 delivery_jobs 表，不创建另一套用户任务状态。
谁调用：memory.api_server 中 /api/msg/send 后台 worker、回程 poller 与直回入口。
改动影响：状态/唯一键改变会影响崩溃恢复和回程去重，必须保留数据库重开测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Optional

_SCHEMA = """CREATE TABLE IF NOT EXISTS delivery_jobs (
    operation_id TEXT PRIMARY KEY,
    source_message_id TEXT NOT NULL UNIQUE,
    target_agent TEXT NOT NULL,
    peer TEXT NOT NULL,
    context_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    request_id TEXT NOT NULL DEFAULT '',
    peer_task_id TEXT NOT NULL DEFAULT '',
    peer_context_id TEXT NOT NULL DEFAULT '',
    reply_hash TEXT NOT NULL DEFAULT '',
    ack_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""

# F03（2026-10-15 返修）：投递所有权列 —— "这单是谁发起的"。
# 为什么必须有：默认重试循环原来无差别消费 due_jobs（含别的进程/上次启动留下的
# sending/pending/awaiting_reply），等于 RESUME=0 关不住第一条管道。所有权必须落库
# 才可判定，不能靠"进程内内存集合"（重启就丢，历史单照样被接）。
# 列含义：
#   owner_instance —— create_app 每次启动生成的实例号；本实例发起的单才归它重试。
#   owner_pid      —— 建单进程号，诊断用（不参与所有权判定：同一 pytest 进程里
#                     "父进程造的历史单"与新 app 同 pid，只看 pid 会把历史单误判为自产）。
_OWNER_COLUMNS = (
    ("owner_instance", "TEXT NOT NULL DEFAULT ''"),
    ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
)

# MIG01（2026-10-16 返修）：并发首连拿写锁的有界退避。
# 为什么必须有界：BEGIN IMMEDIATE 撞锁属**瞬时竞争**（另一条连接正补列 / open_db 切 WAL
# 期间短暂持锁），应当退避重试；但绝不能无界等——拿不到就明确失败，比"无限挂着"强。
# 取值依据（实测）：并发争锁窗口在毫秒级，退避总时长上限 ≈ 0.05×(1+2+…+8) ≈ 1.8 秒，
# 远小于调用方超时，且足以覆盖"另一条连接补列 + 提交"的完整窗口。
_MIG01_LOCK_RETRIES = 8
_MIG01_LOCK_BACKOFF_S = 0.05


def _connect(db_path: str) -> sqlite3.Connection:
    """经 XTMEM 公共连接打开 C01 表；调用方负责关闭连接。

    【不】改回裸 sqlite3.connect（2026-10-15 R06 返修）：裸连会绕过 open_db 的两道拒写闸
    —— user_version 不匹配的只读降级、xtmem_meta.maintenance=1 的停写旗。实测两个对照里
    公共连接向 messages 写都被拒（sqlite3.DatabaseError），而本模块 enqueue 照样新增投递行，
    等于别人关门时这里另开一扇后门。走 open_db 后，版本/维护不匹配时这里的建表与写入
    会同样抛 sqlite3.DatabaseError，调用方按拒写处理。

    check_same_thread=False：worker/poller/恢复三路都在各自线程里开连接，
    open_db 默认 True 会在同连接跨线程时直接抛 ProgrammingError。
    """
    from memory.open_db import open_db
    conn = open_db(db_path, check_same_thread=False)
    try:
        # ── MIG01（2026-10-16 返修）：建表 + 补列 + 建索引整段放进**写事务**，让并发首连串行化。
        #
        # 【病根】原实现是"先 PRAGMA 读列，再依读到的列表逐项 ALTER"，读与改之间没有串行保护。
        # 两条连接都读到旧表缺 owner_instance，便各自 ALTER，后到的那条报
        # `duplicate column name: owner_instance` —— 像两位安装工都先看见墙上没门牌，
        # 各拿一块来装，后到的人不认已有牌、直接报施工失败。
        #
        # 【修法】先 BEGIN IMMEDIATE 拿写锁，**再**读列。持锁期间没有别的写者，读到的必然是最新
        # 结构；于是第二条连接进锁后重读会看到列已存在 → 不 ALTER → 幂等。
        # ⚠️【不】把 BEGIN 挪到 PRAGMA 之后：那样两条连接仍可能都先读完列再抢锁，竞态原样保留。
        #    串行化的前提是「读列发生在持锁之后」，不是"有个事务就行"。
        # ⚠️【不】改成 `except OperationalError: pass` 吞掉 duplicate column：那会把"迁移没做成"
        #    伪装成 ready（R06 关闭判据），并掩盖真实 DDL/权限故障。
        # ⚠️ BEGIN IMMEDIATE 立即取写锁：被版本/maintenance 降级的只读连接在此就会抛
        #    OperationalError → 走下方失败分支关连接并抛出，**拒写语义原样保留**（不是新加的闸）。
        # 实测依据（默认 isolation_level='' 下）：BEGIN IMMEDIATE 后执行 ALTER，in_transaction
        # 仍为 True、写锁不释放，另开连接 BEGIN IMMEDIATE 立刻 "database is locked" ⇒ 本段确实串行。
        #
        # ⚠️【不】把"撞锁"当成迁移失败直接抛（2026-10-16 实测）：并发首连时另一条连接可能正持锁
        #    （实测栈：open_db 内的 `PRAGMA journal_mode=WAL` 切 WAL 期间短暂持锁），此时抛错
        #    等于"两条连接同时进就有一条起不来"。BEGIN 撞锁是**可重试的瞬时竞争**，
        #    与"版本/maintenance 拒写"（真实失败，必须抛）性质不同：前者退避重试，后者照抛。
        # ⚠️【不】用无界重试或长 sleep：重试次数/退避有界（见 _MIG01_LOCK_RETRIES），
        #    耗尽仍未拿到锁才抛最后的锁错误——宁可明确失败，也不无限等。
        _mig01_locked = None
        for _attempt in range(_MIG01_LOCK_RETRIES):
            try:
                conn.execute("BEGIN IMMEDIATE")
                _mig01_locked = None
                break
            except sqlite3.OperationalError as _le:
                if "locked" not in str(_le) and "busy" not in str(_le):
                    raise                     # 非锁类错误（拒写/只读降级等）：立即抛，不重试
                _mig01_locked = _le
                time.sleep(_MIG01_LOCK_BACKOFF_S * (_attempt + 1))
        if _mig01_locked is not None:
            raise _mig01_locked                # 重试耗尽：照样抛出，不装成功
        conn.execute(_SCHEMA)
        # F03：老库补所有权列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
        # 幂等：持锁后读 table_info 再补缺的列；补列【不】改任何既有行语义——历史行的
        # owner_instance 落成空串，正好被"只认本实例"的过滤器排除（这正是我们要的：
        # 老库里的单默认不属于任何新实例，必须显式恢复才接）。
        have = {r[1] for r in conn.execute("PRAGMA table_info(delivery_jobs)")}
        for col, decl in _OWNER_COLUMNS:
            if col not in have:
                conn.execute("ALTER TABLE delivery_jobs ADD COLUMN %s %s" % (col, decl))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_jobs_due ON delivery_jobs(status,next_attempt,lease_until)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_jobs_owner ON delivery_jobs(owner_instance,status)")
        conn.commit()
    except BaseException:
        # 任何迁移失败都走这里：版本/maintenance 降级被拒写、DDL 失败、提交失败、乃至
        # duplicate column 之类的意外——一律**回滚 + 关连接 + 原样抛出**。
        # ⚠️【不】把这里改成 pass/忽略：表只补了一半却让调用方以为 ready，正是 R06 要挡的
        #    "伪完成"；也【不】只关连接不回滚，那会留下半截迁移。
        # ⚠️ 回滚本身失败【不】覆盖原异常（它只是尽力而为），否则真实病灶会被回滚错误顶掉。
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()
        raise
    return conn


def _dict(row):
    return dict(row) if row else None


# ================================================================
# G01（2026-10-15 返修）：回程绑定的**唯一实现** —— 「这条回帖算不算本单的业务回程」
#
# ▍为什么放在签收册（delivery_jobs）而不是调用方
#   同一张单有三扇门能落回程：CLI 直回（xtmem.py）、worker 同步终态、poller/恢复
#   （api_server.py）。三扇门各自判"依据"就必然漂移——本单 G01 正是这么来的：
#   CLI 那扇门已按「actor=原目标 + 接收者=原请求人」拒绝签收，而查重那扇门只看
#   from_agent+reply_to+正文，于是**错收件人的普通帖**被当成"请求人已拿到答案"，
#   真正答案再也写不进去、job 却 completed。⇒ 判定必须和签收册同层：签收册是唯一
#   权威记名处，准入规则就该只写一遍。
#
# ▍绑定的三方（缺一不可）
#   ① 可信 actor（from_agent）∈ job.target_agent 的合法身份 —— 谁被派了这单；
#   ② 接收者（to_agent）∈ 源消息 from_agent 的合法身份 —— 答案回到**原请求人**手里；
#   ③ 源消息必须存在 —— 缺源无从谈绑定（不消费名额、更不能记完成）。
#   ⚠️【不】只看 from_agent：那只证明"签字的是本端"，不证明"这张单允许他签"，
#      也完全不保证"答案送到了问的人那里"（G01 的错收件人帖 from_agent 就是对的）。
#   ⚠️【不】改用正文比对：正文相同不代表是同一次外发的回程——不同 operation 的同文
#      正文、或人工发的同文普通帖都必须照常落库（短单明写"不能靠全文去重吞不同操作"）。
#
# ▍身份别名复用既有映射
#   契约 id（dsh）/ namespace / A2A peer 名（xiaod）在本仓已有权威映射
#   （cockpit_contract.NS_OF/A2A_OF、a2a_client.AGENT_ALIASES）。再抄一份就是第二套
#   口径，早晚漂移。取不到映射（独立跑 CLI、无插件上下文）时退化成"只有它自己"——
#   宁可判窄不可判宽。
# ================================================================

def _field(row, name: str):
    """从 dict / sqlite3.Row / 普通对象取一列；取不到返回 None，绝不抛。

    ⚠️【不】假定连接设了 row_factory：本模块的 `_connect` 走 open_db 且**不设** row_factory，
    裸查询回来的是 tuple，`row[名字]` 会 TypeError。凡是拼 dict 的地方一律用
    `_select_dicts`，别指望 Row（本仓踩过的坑：Row 也没有 .get()）。
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except Exception:
        return getattr(row, name, None)


def _select_dicts(conn, sql: str, args=()):
    """按 cursor.description 把结果拼成 dict 列表（与连接的 row_factory 无关）。"""
    cur = conn.execute(sql, tuple(args))
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def legal_identities_of(agent) -> set:
    """某 agent/namespace 的合法身份别名集合（绑定判定的唯一换算处）。"""
    ids = set()
    a = str(agent or "").strip()
    if not a:
        return ids
    ids.add(a)
    try:
        from memory import cockpit_contract as cc
        ns_of = getattr(cc, "NS_OF", {}) or {}
        a2a_of = getattr(cc, "A2A_OF", {}) or {}
        for cid, ns in ns_of.items():
            if a in (str(cid), str(ns)):
                ids.add(str(cid))
                ids.add(str(ns))
                if a2a_of.get(cid):
                    ids.add(str(a2a_of[cid]))
    except Exception:
        pass
    try:
        from memory.a2a_client import AGENT_ALIASES
        for k, v in (AGENT_ALIASES or {}).items():
            if a in (str(k), str(v)):
                ids.add(str(k))
                ids.add(str(v))
    except Exception:
        pass
    return ids


def reply_binding_ok(source_row, job, actor, to_agent):
    """签收资格判定：返回 (ok, reason)。

    三条同时成立才放行：源存在、actor 属于 job.target_agent、接收者属于源发起方。
    reason 取值（供审计事件归因，别改字面量：既有诊断按它统计）：
    missing_source / no_job / actor_not_target / recipient_mismatch。

    ⚠️【不】把接收者检查写成 `if src_from and ...`（2026-10-15 审计发现，端到端复现）：
    源 from_agent 为空串时那条件**整条短路**，于是任意收件人都放行——错收件人帖照样
    被签收、job 假报 completed、真答案被 duplicate 吃掉。实测源 from_agent='' 可由
    真实 CLI（`--from ''`）造出。空来源的语义是"算不出答案该给谁"，属**算不出来**：
    与 row_is_bound_reply 同口径判否（两条实现必须对同一输入给同一答案，否则又是 G01 病根）。
    """
    if not source_row:
        return False, "missing_source"
    if not job:
        return False, "no_job"
    if not legal_identities_of(actor) & legal_identities_of(job.get("target_agent")):
        return False, "actor_not_target"
    src_from = str(_field(source_row, "from_agent") or "")
    if not src_from:
        return False, "recipient_mismatch"
    if not (legal_identities_of(to_agent) & legal_identities_of(src_from)):
        return False, "recipient_mismatch"
    return True, ""


def row_is_bound_reply(row, source_row, target_agent) -> bool:
    """一条**已存在**的消息行是否绑定了本单的 (可信 actor, 原请求人)。

    为什么要有它：查重与"是否已有回程"这两处原来只按 from_agent+reply_to（+正文）
    判，于是错收件人的普通帖会被冒认成有效回程。凡是要说"回程已存在"的地方，
    一律先过本函数——它是"既存回程"的唯一判据。
    ⚠️ 正文是否算答案（ack/notice 占位）由调用方另判：本函数只管身份绑定。
    """
    if not source_row:
        return False
    frm = str(_field(row, "from_agent") or "")
    to = str(_field(row, "to_agent") or "")
    if not frm or not to:
        return False
    if not (legal_identities_of(frm) & legal_identities_of(target_agent)):
        return False
    src_from = str(_field(source_row, "from_agent") or "")
    return bool(legal_identities_of(to) & legal_identities_of(src_from))


def existing_bound_reply_id(conn, from_agent, reply_to, content, source_row, target_agent):
    """库内是否已有**绑定到本单**的同文回程行；有则返回其 id，否则 None。

    为什么不能只按 from_agent+reply_to+content 判重（G01 病根）：
    「错收件人的同文普通帖」与「给原请求人的真答案」三列可以完全相同，
    只按这三列判重就会把前者当成"答案已送到"，真答案再也写不进去。
    所以判重必须再过一次身份绑定（actor ∈ 目标、接收者 ∈ 原请求人）。

    ⚠️ 【不】在 SQL 里加 `from_agent=?` 精确过滤（2026-10-15 返修自查发现）：
    绑定判定用的是**别名集合**（dsh 的合法别名含 xiaod），而 SQL 精确串只认字面量。
    两者并存会出现"校验说这行合法、取行又说没有它"的自相矛盾——实测：CLI 以别名
    `xiaod` 落一条收件人正确的真回程后，按 `from_agent='dsh'` 取行取不到，判定为
    "尚无回程"，于是同一条业务回程会被再插一遍（重复落库）。
    ⇒ 取出候选后一律由 row_is_bound_reply 统一筛，别名与 namespace 口径只此一处。
    """
    try:
        rows = _select_dicts(
            conn,
            "SELECT id, from_agent, to_agent, content FROM messages WHERE reply_to=? AND content=?",
            (str(reply_to), str(content)))
    except Exception:
        return None
    for r in rows:
        if row_is_bound_reply(r, source_row, target_agent):
            return r.get("id")
    return None


def bound_reply_rows(conn, from_agent, reply_to, source_row, target_agent, limit: int = 8):
    """该 reply_to 下**已绑定**的回程行（由新到旧，最多 limit 条），一律返回 dict。

    _reply_exists 这类"是否已有有效回程"的判断一律走它：错收件人的普通帖不在其中。
    ⚠️ 统一返回 dict 而不是 sqlite3.Row：Row 没有 `.get()`（本仓踩过的三陷阱之一），
    调用方拿到 Row 用 .get() 会抛 AttributeError，被 except 吞掉后表现为"查不到回程"。
    ⚠️ 同为不按 from_agent 精确串过滤：绑定判定用**别名集合**，SQL 精确串只认字面量，
    两者并存会自相矛盾（别名落库的真回程取不到 → 判"尚无回程" → 重复落库）。
    ⚠️【不】在 SQL 里加 `LIMIT` 把候选窗口截掉（2026-10-15 审计发现）：本函数是
    "先取候选、再按绑定筛"，一旦被截断，**合法的绑定回程可能落在窗口外** → 判"没有回程"
    → 重复落库。所以这里按 reply_to 取**全部**候选再筛，不设窗；limit 只作用于筛完之后。
    代价：单个 reply_to 的行数即扫描量（生产实测最多 4 行），换取"不会漏判"。
    """
    try:
        rows = _select_dicts(
            conn,
            "SELECT id, from_agent, to_agent, content, visibility, shared_with FROM messages "
            "WHERE reply_to=? ORDER BY ts DESC",
            (str(reply_to),))
    except Exception:
        return []
    return [r for r in rows if row_is_bound_reply(r, source_row, target_agent)][:max(1, int(limit))]


def enqueue(db_path: str, source_message_id: str, target_agent: str, peer: str,
            context_id: str, payload: str, operation_id: Optional[str] = None,
            owner_instance: str = "") -> dict:
    """发起 A2A 前原子保存完整投递意图；同源消息重试复用原 operation。

    owner_instance（F03）：记下"本单由哪个 app 实例发起"，供默认重试判断所有权。
    留空 = 无主（脚本直建/历史行），任何实例都【不】默认接手，只能显式恢复。
    """
    now = time.time()
    op = str(operation_id or ("op-" + source_message_id))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM delivery_jobs WHERE source_message_id=?", (source_message_id,)).fetchone()
        if row:
            job = _dict(row)
            if job["payload_hash"] != digest or job["target_agent"] != target_agent:
                raise ValueError("source message already has a different delivery payload")
            conn.commit()
            return job
        conn.execute(
            "INSERT INTO delivery_jobs (operation_id,source_message_id,target_agent,peer,context_id,payload_hash,payload,status,owner_instance,owner_pid,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?)",
            (op, source_message_id, target_agent, peer, context_id, digest, payload,
             str(owner_instance or ""), os.getpid(), now, now),
        )
        conn.commit()
        return _dict(conn.execute("SELECT * FROM delivery_jobs WHERE source_message_id=?", (source_message_id,)).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim(db_path: str, operation_id: str, lease_s: float = 45.0, max_attempts: int = 4) -> Optional[dict]:
    """持久化租约并计数；ack/完成态不能再次被 worker 领取。

    可领取 = 非终态 且 尝试未超限 且 next_attempt 已到 且 租约已过期。
    sending 且租约未过期 → 领不到（另一进程还在送）；租约过期后被 due_jobs 捞出，这里可再次领。
    """
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM delivery_jobs WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            conn.commit()
            return None
        job = _dict(row)
        if job["status"] in ("acked", "completed", "failed") or job["attempts"] >= max_attempts:
            conn.commit()
            return None
        if job["next_attempt"] > now or job["lease_until"] > now:
            conn.commit()
            return None
        conn.execute("UPDATE delivery_jobs SET status='sending',attempts=attempts+1,lease_until=?,updated_at=? WHERE operation_id=?",
                     (now + max(0.1, float(lease_s)), now, operation_id))
        conn.commit()
        return _dict(conn.execute("SELECT * FROM delivery_jobs WHERE operation_id=?", (operation_id,)).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_sent(db_path: str, operation_id: str, request_id: str, task_id: str, context_id: str,
                reply: str = "", state: str = "") -> None:
    """记录 RPC 已受理证据；task_id 只能由 a2a_client 从真实 task 对象提供。

    ⚠️ 【不】改成无条件 UPDATE（2026-10-15 R07 返修）：回程（record_reply）可能先到，
    发送线程随后才登记；无条件写会把已经盖好的 completed 章撕回 awaiting_reply，
    让"已完成"在并发/迟到下退回去。WHERE 排除终态行 = 旧租约/旧尝试不许覆盖新结果。

    ⚠️ 【不】用"正文非空即终态"判完成（R01/R02 同源）：对端 WORKING 状态也可能带正文，
    那是进度不是答案。完成态一律由 record_reply 落（正文与完成同事务），本函数只记受理身份。
    state 参数为签名兼容保留，不再参与状态判定。
    """
    now = time.time()
    digest = hashlib.sha256(reply.encode("utf-8")).hexdigest() if reply else ""
    # ⚠️【不】在这里凭"终态 + 正文"把状态置 completed（2026-10-15 R01 根因，修了两遍才对）：
    # record_sent 只登记"RPC 已被受理"的身份证据（request/task/context）。完成态必须由
    # record_reply 在【写入正文的同一事务】里落——否则顺序就是"先盖完成章、再补答案"，
    # 而 record_reply 见 completed 直接判 duplicate 返回，正文永远进不了消息表：
    # 同步终态答案静默丢失、源消息却显示 replied（正是独立测试断言 0 条的那条）。
    # 曾经的写法 `terminal = 'completed' if reply and normalized in TERMINAL` 就是病灶本身——
    # 修好状态归一化反而让这个分支真正生效，两处 bug 互相掩盖（本鱼实测踩到）。
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE delivery_jobs SET request_id=?,peer_task_id=?,peer_context_id=?,reply_hash=?,status=?,lease_until=0,updated_at=? "
            "WHERE operation_id=? AND status NOT IN ('completed','acked')",
            (request_id, task_id or "", context_id or "", digest, "awaiting_reply", now, operation_id))
        # 已被回程完成的单子：这里只补身份证据，绝不改状态（终态不可倒退）。
        conn.execute(
            "UPDATE delivery_jobs SET request_id=?,peer_task_id=COALESCE(NULLIF(?,''),peer_task_id),"
            "peer_context_id=COALESCE(NULLIF(?,''),peer_context_id),updated_at=? "
            "WHERE operation_id=? AND status IN ('completed','acked')",
            (request_id, task_id or "", context_id or "", now, operation_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_completed(db_path: str, operation_id: str, peer_task_id: str = "", peer_context_id: str = "",
                   reply_hash: str = "") -> bool:
    """对已由 peer 直接落入消息表的回程做原子完成记账，不再重复发送或插入。"""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE delivery_jobs SET status='completed',peer_task_id=COALESCE(NULLIF(peer_task_id,''),?),peer_context_id=COALESCE(NULLIF(peer_context_id,''),?),reply_hash=COALESCE(NULLIF(reply_hash,''),?),ack_at=?,lease_until=0,updated_at=? WHERE operation_id=? AND status NOT IN ('completed','acked')",
                           (peer_task_id, peer_context_id, reply_hash, time.time(), time.time(), operation_id))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_retry(db_path: str, operation_id: str, error_type: str, delay_s: float, max_attempts: int = 4) -> bool:
    """按有界退避安排重试；超限进入 uncertain，避免无限重放不确定的远端执行。"""
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT attempts,status FROM delivery_jobs WHERE operation_id=?", (operation_id,)).fetchone()
        if not row or row["status"] in ("acked", "completed", "failed"):
            conn.commit()
            return False
        terminal = row["attempts"] >= max_attempts
        conn.execute("UPDATE delivery_jobs SET status=?,next_attempt=?,lease_until=0,last_error=?,updated_at=? WHERE operation_id=?",
                     ("uncertain" if terminal else "pending", now + max(0.0, float(delay_s)), str(error_type)[:80], now, operation_id))
        conn.commit()
        return not terminal
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_reply(db_path: str, operation_id: str, peer_task_id: str, peer_context_id: str,
                 reply: str, insert_bus_reply) -> dict:
    """在唯一操作行锁内登记回程，消息写入与完成态同事务，重复/迟到回程只认第一次。

    ⚠️ G01（2026-10-15 返修）：completed 是"请求人真拿到答案"的章，不能由"回调跑过了"推出。
    调用方回调的返回值就是"有效业务回程是否真的存在"的证明，三态语义：
      返回 dict  → 按 {"reply_present": bool}（兼容读 "inserted"）判定；
      返回 False → 明确否证：没插入也没证明存在有效既存回程 → **不盖 completed**；
      返回 None  → 历史/单元层调用方未报证 → 保持旧行为（视为存在）。
                    ⚠️【不】把 None 也当否证：那会让既有单元夹具（只记插入次数的
                    回调、harness 里的 add()）全部误判成"没回程"，属改坏老齿轮。
    为什么否证时不能只跳过 UPDATE 就 commit：留下半截状态比不写更糟——
    单子停在 await/lease 上还能被下一轮恢复正确处理，而"消息没进去、章却盖了"
    正是 G01 要根除的假完成。⇒ 否证时整批回滚，单子保持原状。
    """
    digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM delivery_jobs WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "code": "unknown_operation", "inserted": False}
        job = _dict(row)
        if job["status"] in ("acked", "completed"):
            conn.commit()
            return {"ok": True, "code": "duplicate", "inserted": False}
        if job["peer_task_id"] and peer_task_id and job["peer_task_id"] != peer_task_id:
            conn.rollback()
            return {"ok": False, "code": "task_mismatch", "inserted": False}
        if job["peer_context_id"] and peer_context_id and job["peer_context_id"] != peer_context_id:
            conn.rollback()
            return {"ok": False, "code": "context_mismatch", "inserted": False}
        verdict = insert_bus_reply(conn, job, reply, digest)
        present = True
        if verdict is False:
            present = False
        elif isinstance(verdict, dict):
            present = bool(verdict.get("reply_present", verdict.get("inserted", False)))
        if not present:
            conn.rollback()
            return {"ok": False, "code": "no_valid_reply", "inserted": False}
        now = time.time()
        conn.execute("UPDATE delivery_jobs SET status='completed',peer_task_id=COALESCE(NULLIF(peer_task_id,''),?),peer_context_id=COALESCE(NULLIF(peer_context_id,''),?),reply_hash=?,ack_at=?,lease_until=0,updated_at=? WHERE operation_id=?",
                     (peer_task_id or "", peer_context_id or "", digest, now, now, operation_id))
        conn.commit()
        return {"ok": True, "code": "inserted", "inserted": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_job(db_path: str, operation_id: str) -> Optional[dict]:
    """按 operation_id 读取持久投递状态。"""
    conn = _connect(db_path)
    try:
        return _dict(conn.execute("SELECT * FROM delivery_jobs WHERE operation_id=?", (operation_id,)).fetchone())
    finally:
        conn.close()


def record_error(db_path: str, operation_id: str, error_type: str, delay_s: float, max_attempts: int = 4) -> bool:
    """对失败发送作上限为 max_attempts 的持久退避，超限保留 uncertain 而不自动重发。"""
    return mark_retry(db_path, operation_id, error_type, delay_s, max_attempts=max_attempts)


def mark_transport_accepted(db_path: str, operation_id: str, peer_context_id: str = "") -> bool:
    """纯 Message（无 task 号）响应的传输受理记账（§3 裁定）。

    【不】拿它当业务完成：没有 task 号只说明"对方收下了"，不等于"对方答复了"。
    所以独立状态 'accepted' —— 既不复用 completed（会造成 R01 式漏收，让真回程被
    record_reply 当 duplicate 吃掉），也不算 awaiting_reply（那会把纯 message 当异步任务去轮询，
    而 tasks/get 根本没有号可查，只能空转到超时）。
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE delivery_jobs SET status='accepted',peer_context_id=COALESCE(NULLIF(?,''),peer_context_id),"
            "lease_until=0,updated_at=? WHERE operation_id=? AND status NOT IN ('completed','acked')",
            (peer_context_id, time.time(), operation_id))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def needs_reply_lookup(db_path: str, operation_id: str) -> bool:
    """恢复时判断该单是否"已知 task、还欠一个回程"——是则先查回程，不盲目重发（R03）。"""
    job = get_job(db_path, operation_id)
    if not job:
        return False
    if job.get("status") in ("completed", "acked", "failed"):
        return False
    return bool(job.get("peer_task_id"))


def due_jobs(db_path: str, limit: int = 50, owner_instance: Optional[str] = None) -> list[dict]:
    """读取待恢复/待重试的投递行，供恢复入口消费（R03）。

    选取三类：
      pending/uncertain —— 未发出或结果不确定，且已到重试时刻；
      sending            —— 租约已过期（进程崩溃/被杀留下的"在途"），必须能被接手，
                            否则账还在但消息永远不再处理（R03 关闭判据）。
    不返回 completed/acked/failed 等终态行——终态不重发。

    owner_instance（F03 返修）：**哪个实例的单可以由谁消费**。
      owner_instance=None（缺省）→ 返回全部到期单（显式恢复/运维对账用，含历史单）；
      owner_instance='x'        → 只返回 owner_instance='x' 的到期单（默认重试线程用）。
    ⚠️【不】把它实现成"只排除 sending 字符串"：跨进程恢复要拦的是**整类无主单**
      （历史 pending/awaiting_reply 同样会真的外发），只挡 sending 等于开关形同虚设。
    """
    now = time.time()
    conn = _connect(db_path)
    try:
        sql = ("SELECT * FROM delivery_jobs WHERE "
               "((status IN ('pending','uncertain','awaiting_reply') AND next_attempt<=? AND lease_until<=?) "
               "OR (status='sending' AND lease_until<=?))")
        args = [now, now, now]
        if owner_instance is not None:
            sql += " AND owner_instance=?"
            args.append(str(owner_instance))
        sql += " ORDER BY created_at LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [_dict(row) for row in rows]
    finally:
        conn.close()
