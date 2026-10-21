# -*- coding: utf-8 -*-
# ================================================================
# XTMEM · conversation_bootstrap.py（CST01 · 候选的可加载装配入口 + 真实入站接线）
#
# ▍这是什么
# ① 干什么：把 CST01 的三件套（证据落库 / 来源解析器注册 / 可信身份装配）装成一个
#    **可加载、默认关闭**的装配。没有显式启用就是**纯 no-op**；启用后它
#    · 在真实宿主入站入口处自动把规范化字段落进证据库；
#    · 把 conversation_event provider 注册到**引擎实际使用的那个**解析器实例上；
#    · 用 adapter 身份装配查询上下文（供有权查询）。
# ② 技术栈：纯 Python + sqlite3；对宿主的接触面只有“实例方法覆写”。
# ③ 依赖：memory.conversation_capture / _source / _adapter_bridge。
# ④ 给谁看：部署接线者、CST01 复核者。
#
# ▍F01 的病根与本次修法（独立验收实测，改前必读）
#   病根：分离的三件事各自“看起来对”，合起来没人负责——
#     · RecordingFeishuAdapter 收下 conn 却**从不使用它**，落库靠外部 sink_writer 或
#       测试体事后手工调 capture；真实入站返回后 cs_events 是 0 行。
#     · install() 只挂**默认解析器**，而引擎用的是按库 resolver，两者不是同一对象。
#     · 采集写在原始入口 **await 之后**，而原入口内部已经 dispatch 过了：
#       依赖它的消费方可能先看到“没有这条证据”。
#   修法：
#     ① 本模块覆写真实 adapter 的 _process_inbound_message（websocket 与 webhook 两条路的
#        **共同必经点**），抢在它内部 dispatch **之前**落库 ⇒ 采集时序早于依赖它的消费。
#        原方法仍一字不动照跑（真 normalize / 去点名 / 提示注入 / 回复解析）。
#        选这里而不是更外层的 _handle_message_event_data：外层只是**其中一个**调用者，
#        而且比这里更早（还没过宿主的重复判定/白名单，采到的是宿主未必收下的事件）。
#     ② enable 时同时做 capture 装配与 provider 注册——**一个开关管全套**，
#        不存在“开了采集忘了注册”的半吊子状态。
#     ③ 注册走 source_resolver.register_default_provider()：已存在的槽立刻补挂，
#        此后新建的槽构造时自带 ⇒ 引擎用哪个 resolver 都看得到。
#
# ▍上线只差什么（主装 vs 隔离候选，别混）
#   本模块 + host-isolated 的组合是**不改宿主一行代码**的候选接线；主装机还需要
#   部署侧在宿主 adapter 的 _process_inbound_message 里插一行本模块的调用
#   （或把本模块的安装动作放进宿主启动钩子）。那一步需要部署授权，
#   **本单不做**——所以在隔离里证明的是“这套装配跑得通”，不是“生产已上线”。
#
# ▍外号表
# "装配"   → wire()（把三件套装上）
# "接线点" → 宿主 adapter 的 _process_inbound_message（真实入站必经点，dispatch 之前）
# "半吊子" → 只装采集没注册解析器（或反之）——本模块用一个开关杜绝
#
# ▍更新记录
# *创建：2026-10-16 · 小D（CST01-F01）：默认关闭的候选装配 + 真实入站自动采集 +
#   进程级 provider 注册 + 身份装配；采集抢在 dispatch 依赖之前*
# ================================================================
# ▍B01/B02/B03 续修（2026-10-16 · 独立验收实测，改前必读）
#   B01 **阻断**：_connect_for_capture 直接 sqlite3.connect ⇒ 默认开启的开库路径
#     **绕过公共写闸**。实测：核心版本不符 / maintenance=1 的库，正式 open_db 拒写，
#     本条却各写进 1 条。修法：一律走 open_db(path)，版本闸与维护闸由它统一施加；
#     本模块**不复刻第二套判定、不关闸、不改 EXPECTED_SCHEMA_VERSION**。
#   B02：_WIRED 非空就返回旧 wiring ⇒ 第二个 adapter 实例“假装装好了”，实际从未被
#     包裹（主链跑、证据 0 行）。修法：按（库, 身份）算装配键；键同 → 幂等补装新实例；
#     键不同 → 明确拒绝（log + None），绝不静默串用别的库/身份。
#   B03：可信 is_bot=True 被硬写成 sender_kind="human" ⇒ 机器人发言记成真人。
#     修法：按入口形参 is_bot 映射（True → "agent"，否则 "human"）；**不**凭显示名猜。
#
# ▍更新记录
# *更新：2026-10-16 · 小D（CST01-B01/B02/B03）：采集连接改走公共 open_db（保留版本/
#   维护拒写且不打断主链）；装配单例改键控（同键幂等补装、异键明确拒绝）；主体种类
#   按可信 is_bot 映射（机器人 → agent，真人 → human）*
# *创建：2026-10-16 · 小D（CST01-F01）：默认关闭的候选装配 + 真实入站自动采集 +
#   进程级 provider 注册 + 身份装配；采集抢在 dispatch 依赖之前*
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from memory import conversation_adapter_bridge as bridge
from memory import conversation_capture as cc
from memory import conversation_source as csource
from memory.open_db import open_db

logger = logging.getLogger("xtmem.conversation_bootstrap")

# 启用开关（**默认关闭**）。值必须是字面量 "1"，与 feature flag 的口径一致——
# 不接受 "true"/"yes"/空格，免得“看起来开了其实没开”这种最难查的形态。
ENABLE_ENV = "XTMEM_CONVERSATION_CAPTURE_ENABLED"

# 装配后的进程内单例：**一个进程一套**（避免重复覆写把原始方法包成洋葱）。
#
# ★B02（2026-10-16 · 独立验收实测）：单例不是“随便什么请求都返回它”。
#   病根：旧实现只要 _WIRED 非空就 `return _WIRED` —— 既不看这次的 adapter 是不是
#   已经被装过，也不看这次的身份/库跟已装配的是不是同一套。后果：
#   同进程第二个 adapter 实例调 wire() 拿到“成功”对象，可它的入站入口**从没被包裹过**，
#   主链照跑而证据 0 行（静默漏采，且调用方以为已经接好）。
#   修法三条：①按（库, 身份）算**装配键**，键相同才复用；②键相同但 adapter 是**新对象**
#   → 把它补装进同一个 wiring（真装上，不是假装）；③键不同 → **明确拒绝**（log + None），
#   绝不静默串用别的库/身份的装配。
_WIRED: Optional["ConversationCaptureWiring"] = None
_WIRED_KEY: Optional[tuple] = None


def _conn_db_key(conn: sqlite3.Connection) -> str:
    """连接指向哪个库（用于装配键）。取不到路径就退回按对象身份，宁可不同一也不误判同一。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if str(row[1]) == "main" and str(row[2] or ""):
                return str(row[2])
    except Exception:  # noqa: BLE001 —— 取不到就走对象身份，不猜
        pass
    return "<conn:%d>" % id(conn)


def _wiring_key(*, db_path: str = "", conn: Optional[sqlite3.Connection] = None,
                identity: Optional[cc.AdapterIdentity] = None) -> tuple:
    """装配键 =（库, 身份四元组）。

    为什么两个都要：同一个库可以有两个身份（跨 bot），同一个身份也可能对两个库；
    只比其中一个都会把“另一套装配”错当成同一套（B02 的静默串用）。
    """
    db = _conn_db_key(conn) if conn is not None else str(db_path or "")
    ident = (str(getattr(identity, "profile", "")), str(getattr(identity, "platform", "")),
             str(getattr(identity, "tenant_id", "")), str(getattr(identity, "bot_id", "")))
    return (db, ident)


def is_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """显式启用判定。默认**关闭**；只认字面量 "1"。"""
    src = os.environ if env is None else env
    return str(src.get(ENABLE_ENV, "")).strip() == "1"


def _connect_for_capture(db_path: str) -> sqlite3.Connection:
    """开一条**独立**的采集连接 —— 但必须走**正式公共连接** open_db()。

    为什么单独开连接而不复用宿主连接：宿主可能正持有事务，而 capture 的契约是
    “调用方不得持事务”（R02）。独立连接 + WAL 让采集与宿主互不阻塞。

    ★B01（2026-10-16 · 独立验收实测，改前必读）：这里原先直接 sqlite3.connect，
      **绕过了全树唯一的写闸**。后果不是“本地 Python 能写”这种泛化问题，而是：
      本产品**默认开启**的开库路径主动绕过 I18 的版本/维护拒写 —— 库宣布停写时它照写。
      实测：核心版本不符、maintenance=1 的两座库，正式 open_db 拒写，而本条原实现各写进 1 条。
    ⇒ 修法：一律 open_db(path)。版本闸（user_version 不匹配 → 只读降级）与维护闸
      （xtmem_meta.maintenance=1 → 只读降级）由它统一施加；本模块**不复刻、不关闸、
      不改 EXPECTED_SCHEMA_VERSION 常量**。降级后写入会被 SQLite 直接拒
      （"attempt to write a readonly database"）→ 被 capture 的 best-effort 吞掉 → 0 新增，
      且宿主消息链继续跑（拒写不许打断主链路）。
    ⚠️【不】在这里补第二套版本/maintenance 判定：两套判定就是两套口径，早晚漂移；
       闸的权威只能有一个（open_db）。
    ⚠️ 打不开就如实报错，**不**回落到宿主的连接上写（那会踩 R02 的自持事务契约）。
    """
    conn = open_db(str(db_path), timeout=5.0)
    # ⚠️ open_db 已设 row_factory=Row / WAL / busy_timeout；这里不再重复设，免得两处不一致。
    return conn


class ConversationCaptureWiring:
    """一次装配的持有物：采集连接 + 适配器身份 + 已装上的宿主入口。"""

    def __init__(self, *, db_path: str, identity: cc.AdapterIdentity,
                 conn: Optional[sqlite3.Connection] = None,
                 sink_writer: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 auto_register_providers: bool = True):
        self.db_path = str(db_path or "")
        self.identity = identity
        if conn is not None:
            self.conn = conn
        elif self.db_path:
            self.conn = _connect_for_capture(self.db_path)
        else:
            # ⚠️ 不给路径就不许“顺手开个临时库”：那会让 cs_events 建在一个谁都找不到
            #    的地方，看起来在写、其实写丢 —— 比明确报错危险得多。
            raise ValueError("ConversationCaptureWiring 需要 conn 或 db_path 之一")
        self.sink: List[Dict[str, Any]] = []
        self._sink_writer = sink_writer
        self._installed: List[Any] = []
        self.captured_count = 0
        # ⚠️ 构造即注册来源解析器：这一步**不许**拆出去让调用方记得调。
        #    只装采集不注册解析器 = “半吊子”装配（F01 点名形态）——
        #    证据写进去了，可引擎拿着真实连接却解析不到这个来源。
        if auto_register_providers:
            self.provider_slots()

    # ---------- 落库 ----------
    def capture_record(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把入站记录落成一条证据（best-effort：绝不打断消息流）。"""
        self.sink.append(record)
        out = _capture_from_record(self.conn, self.identity, record)
        if out is not None and out.get("status") == cc.CAPTURE_STATUS_CAPTURED:
            self.captured_count += 1
        if self._sink_writer is not None:
            try:
                self._sink_writer(record)
            except Exception:  # noqa: BLE001 —— 观察者回调失败不拖垮主链路
                logger.warning("sink_writer raised; ignored", exc_info=True)
        return out

    # ---------- 装配 ----------
    def install_on_adapter(self, adapter: Any) -> None:
        """把真实入站入口包成“**先落库**、再走宿主逻辑”。改的是实例，不是类。

        接线点为什么选 _process_inbound_message 的**入口**而不是 _handle_message_event_data：
          · 它是 websocket 与 webhook 两条路的**共同必经点**（_handle_message_event_data 只是
            其中一个调用者），装这里两条路都覆盖；
          · 它处在宿主“已经判过重复、已过白名单”之后 —— 采到的是**宿主真的收下了**的消息，
            不会把宿主丢弃的事件也当证据（那等于变相扩大监听面）；
          · 它自己内部才调 dispatch ⇒ 在**入口**落库 = 采集严格早于任何依赖它的消费。
        ⚠️【不】改成入口的 await 之后：那是 R01 的病根（依赖方可能先看到“没有这条证据”）。
        """
        # ⚠️ 同一个 adapter 重复安装 = 把原方法包成洋葱（两层都落库/两层都调原逻辑）。
        #    B02 要求“同对象重复安装幂等”，所以先查已装清单。
        if any(t is adapter for t in self._installed):
            return
        original = adapter._process_inbound_message

        async def wired(*, data: Any, message: Any, sender_id: Any, chat_type: str,
                        message_id: str, is_bot: bool = False) -> Any:
            record = _record_from_inbound(data=data, message=message, sender_id=sender_id,
                                          chat_type=chat_type, message_id=message_id,
                                          is_bot=is_bot)
            if record is not None:
                self.capture_record(record)
            # ⚠️ 宿主逻辑一字不动照跑：normalize / 去点名 / 提示注入 / 回复解析 / dispatch
            return await original(data=data, message=message, sender_id=sender_id,
                                  chat_type=chat_type, message_id=message_id, is_bot=is_bot)

        adapter._process_inbound_message = wired
        self._installed.append(adapter)

    def provider_slots(self) -> Dict[str, Any]:
        """把 provider 注册到“已存在的槽 + 以后新建的槽”。幂等。"""
        return csource.install()

    def auth_for(self, *, principal_id: str, reader_scope_kind: str, reader_scope_id: str,
                 capabilities: Any = None, visible_watermark: int = 1 << 62) -> cc.AuthContext:
        """用 **adapter 身份**（不是自报值）装配查询上下文。"""
        return cc.AuthContext(
            principal_id=str(principal_id), profile=self.identity.profile,
            bot_id=self.identity.bot_id, reader_scope_kind=str(reader_scope_kind),
            reader_scope_id=str(reader_scope_id),
            visible_watermark=int(visible_watermark),
            capabilities=frozenset(capabilities or ()),
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不抛
            pass


def _host_modules(host_root: Optional[str] = None) -> Any:
    """导入宿主 adapter 模块（宿主包需在 sys.path）。抽出来方便隔离环境替换。"""
    import sys
    root = host_root or os.environ.get("XTMEM_HOST_ROOT", "")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    import plugins.platforms.feishu.adapter as adapter_mod
    return adapter_mod


def _resolve_db_path(explicit: Optional[str] = None) -> str:
    """采集库路径：显式参数 > 环境变量 > 无（=不着库，装配按未配置处理）。"""
    if explicit:
        return str(explicit)
    return str(os.environ.get("XTMEM_DB_PATH", "") or "")


def wire(*, db_path: Optional[str] = None, identity: Optional[cc.AdapterIdentity] = None,
         host_root: Optional[str] = None, force: bool = False,
         conn: Optional[sqlite3.Connection] = None,
         sink_writer: Optional[Callable[[Dict[str, Any]], Any]] = None,
         adapter: Any = None) -> Optional["ConversationCaptureWiring"]:
    """**默认关闭**的一键装配：开关没开就返回 None（纯 no-op，不碰宿主一个字节）。

    契约：
    入参：db_path / identity / host_root / force / conn / sink_writer / adapter
    返回：ConversationCaptureWiring（已装上）或 None（未启用 / 条件不全）
    副作用：覆写目标 adapter 实例的入站入口；注册 source provider；可选开采集连接
    谁调用：宿主启动路径（部署侧插入）/ 隔离候选 / 测试
    改动影响：默认关闭 ⇒ 不启用时对宿主**零影响**；`force=True` 只给隔离与测试用，
              生产调用点**不**该传它（那等于绕过开关）。

    ⚠️ 条件不全时**不静默半装**：明确 log 一条原因并返回 None。
       半装（装了采集没注册解析器）正是 F01 判据点名的“半吊子”形态。
    ⚠️ `adapter` 显式给定时只装它；不给则尝试装宿主模块里的出厂实例。
       生产路径应显式给 adapter，免得依赖“模块里恰好有个单例”。
    """
    global _WIRED, _WIRED_KEY
    if not force and not is_enabled():
        return None
    if identity is None:
        logger.warning("conversation capture not wired: no adapter identity supplied")
        return None
    # ⚠️ 路径检查**与 adapter 无关**：没有库就没地方落证据，无论装不装入口都不能算装配成功。
    #    （自查时踩过：只在 adapter is None 时才检查，于是“有 adapter、没路径”走到构造器里
    #    抛 ValueError —— 契约要求的是**明确 log + 返回 None**，不是把异常甩给调用方。）
    if conn is None:
        path = _resolve_db_path(db_path)
        if not path:
            logger.warning(
                "conversation capture not wired: no database path (XTMEM_DB_PATH unset)")
            return None
        # ⚠️ 路径不存在就**不装**（也不许顺手把它建出来）：sqlite3 会默默创建一个空文件，
        #    而空库既没 feature 结构也没 xtmem_meta —— 装上去只会每条消息都写失败刷日志，
        #    更糟的是“配置写错路径”时会留下一个没人认领的垃圾库。
        #    正确顺序是【先做 feature 迁移（ensure_feature_schema），再启用装配】。
        if not os.path.exists(str(path)):
            logger.warning(
                "conversation capture not wired: database path does not exist (migrate first)")
            return None
        db_path = path
    # ── 解析这次请求的目标 adapter（决定“装到哪台机器上”）──────────────
    targets: List[Any] = []
    if adapter is not None:
        targets.append(adapter)
    else:
        try:
            adapter_mod = _host_modules(host_root)
        except Exception as exc:  # noqa: BLE001 —— 宿主不可导入时如实报，不假装装上
            logger.warning("conversation capture not wired: host adapter unavailable (%s)",
                           type(exc).__name__)
            return None
        for attr in ("_ADAPTER_SINGLETON", "adapter"):
            candidate = getattr(adapter_mod, attr, None)
            if candidate is not None and hasattr(candidate, "_process_inbound_message"):
                targets.append(candidate)

    # ── B02：单例只在**同一套（库, 身份）**下复用；新 adapter 要真装上 ──
    key = _wiring_key(db_path=db_path or "", conn=conn, identity=identity)
    if _WIRED is not None:
        if key != _WIRED_KEY:
            # ⚠️ 不同库/不同身份：**明确拒绝**，绝不把别的装配当自己的返回。
            #    静默复用 = 调用方以为接好了，实际证据落进另一个库/另一个身份（串库串身份）。
            logger.warning(
                "conversation capture not rewired: existing wiring is for a different "
                "database/identity (requested=%r, wired=%r); refusing to silently reuse",
                key, _WIRED_KEY)
            return None
        # 同一套装配：把这次的新 adapter 补装进去（幂等；同对象直接返回）。
        for target in targets:
            _WIRED.install_on_adapter(target)
        return _WIRED

    # provider 在构造时就注册好了（见 ConversationCaptureWiring.__init__）：
    # 解析器比采集先就位 ⇒ 采到的证据立刻可被解析。
    wiring = ConversationCaptureWiring(
        db_path=_resolve_db_path(db_path), identity=identity, conn=conn,
        sink_writer=sink_writer)
    for target in targets:
        wiring.install_on_adapter(target)
    _WIRED = wiring
    _WIRED_KEY = key
    return wiring


def migrate(db_path: str) -> Dict[str, Any]:
    """显式迁移入口：对**已存在**的库建好 feature 结构（幂等）。

    为什么单独给一个入口：装配只负责“接上线”，**不负责建库**（见 wire 的路径检查）。
    部署顺序是：① 确认库文件已存在（生产库本来就存在）② migrate()（幂等，已就绪则无操作）
    ③ 启用开关并 wire()。

    契约：
    入参：db_path（必须已存在的库文件路径）
    返回：ensure_feature_schema 的 feature_state（含 ready/reason/缺项明细）
    副作用：建 additive feature 表/索引/触发器 + 写 feature flag（**不动 user_version**）
    谁调用：部署步骤、隔离候选、测试夹具
    改动影响：本函数是装配启用前的唯一准备动作；跳过它 = 启用后每条消息都写失败。
    """
    from memory import conversation_schema as cs
    path = str(db_path or "")
    if not path or not os.path.exists(path):
        raise ValueError("migrate 需要已存在的库文件：%r" % (path,))
    conn = open_db(path, timeout=5.0)
    try:
        return cs.ensure_feature_schema(conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不抛
            pass


def current() -> Optional["ConversationCaptureWiring"]:
    """取当前进程已装配的实例（没装返回 None）。"""
    return _WIRED


def reset_for_tests() -> None:
    """【仅测试】清掉进程内单例（隔离用例之间互不污染）。"""
    global _WIRED, _WIRED_KEY
    _WIRED = None
    _WIRED_KEY = None


# ================================================================
# 入站记录 → 证据（与 wiring 分离，方便单独测）
# ================================================================

def _record_from_inbound(*, data: Any, message: Any, sender_id: Any, chat_type: str,
                         message_id: str, is_bot: bool = False) -> Optional[Dict[str, Any]]:
    """从**宿主真实入口的实参**里取原始正文与来源边界；取不到就返回 None（不编）。

    为什么用实参而不是从 data 里逐层 getattr：
      data 的形状随传输（websocket/webhook）与 SDK 版本变化；入口的形参才是稳定的契约。
      真出现两者不一致时，以**宿主交给入口的实参**为准。

    ⚠️ 这里**不**做任何加工：不剥 @点名、不注入提示。加工后的话由宿主自己的
       _process_inbound_message 处理，本函数只负责“用户实际发出来的那句话”。
    """
    if message is None:
        return None
    raw_content = getattr(message, "content", "") or ""
    raw_text = _extract_raw_text(raw_content)
    if not str(raw_text).strip():
        # 纯 @点名/无正文的消息宿主自己也会丢（"Ignoring empty text message"）。
        # ⚠️ 采到它等于把“没有原话”记成一条空证据 —— 不入库才是如实。
        return None
    return {
        "message_id": str(message_id or getattr(message, "message_id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "chat_type": str(chat_type or getattr(message, "chat_type", "") or "p2p"),
        "raw_text": raw_text,
        "sender_primary": (getattr(sender_id, "open_id", None)
                           or getattr(sender_id, "user_id", None)
                           or getattr(sender_id, "union_id", None)
                           or "<unknown>"),
        # ★B03（2026-10-16 · 独立验收实测）：主体种类取**宿主给出的可信 is_bot**，
        #   不是硬写 human。旧实现把 is_bot 只放进临时 record，sender_kind 恒为 "human"
        #   ⇒ 另一台机器人的话被记成真人发言（后续会把 bot 意见当真人的话引用）。
        #   ⚠️ 判据只认入口形参 is_bot（宿主已按可信面判定）；**不**从显示名/文本猜身份。
        "sender_kind": _sender_kind_from_inbound(is_bot),
        "reply_to_message_id": (getattr(message, "parent_id", None)
                                or getattr(message, "upper_message_id", None)),
        "thread_id": (getattr(message, "thread_id", None)
                      or getattr(message, "root_id", None)),
        "is_bot": bool(is_bot),
    }


def _sender_kind_from_inbound(is_bot: Any) -> str:
    """入站主体种类：可信 is_bot=True → "agent"；否则 "human"。

    ★B03：CST01 管的是**可信发言归属**，主体种类必须如实。宿主把 is_bot 作为形参
    交进来（它的判定面在发送者类型上），这就是本函数唯一的依据。

    ⚠️【不】从显示名、昵称、"Bot" 字样或正文内容猜身份 —— 名字是自报面，不是凭据。
    ⚠️ 契约里 sender_kind 的取值域是 (human, agent, tool, platform)：
      可信机器人落 **agent**（契约允许的明确非 human 种类），不做 "bot" 这种表外值
      （schema 的 CHECK 会直接拒，那会变成“拒写”而不是“记错”，但同样是错）。
    """
    return "agent" if is_bot is True else "human"


def _extract_raw_text(raw_content: str) -> str:
    """平台原始正文：只把 JSON 里的 text 取出来，**不做任何加工**。"""
    try:
        payload = json.loads(raw_content) if raw_content else {}
    except (TypeError, ValueError):
        return str(raw_content or "")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return str(raw_content or "")


def _capture_from_record(conn: sqlite3.Connection, identity: cc.AdapterIdentity,
                         record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """一条入站记录 → 一次 capture（best-effort，异常只 log）。

    ⚠️ 宿主行号（host_message_row_id / session_id）此处**不给**：
       真实宿主侧取 SessionDB 行号需要宿主配合（本单未接），拿不到就如实
       标 locator_kind=unknown + reason，**不编**一个看起来像地址的东西。
    """
    if conn is None:
        return None
    try:
        return bridge.capture_host_event(
            conn, adapter=identity,
            raw_text=record.get("raw_text"), chat_id=record.get("chat_id"),
            chat_type=record.get("chat_type"), sender_principal=record.get("sender_primary"),
            message_id=record.get("message_id"), sender_kind=record.get("sender_kind") or "human",
            reply_to_message_id=record.get("reply_to_message_id"),
            thread_id=record.get("thread_id"))
    except Exception as exc:  # noqa: BLE001 —— 旁路证据不许打断主链路
        logger.warning("conversation capture skipped (%s): %s", type(exc).__name__, exc)
        return None


__all__ = [
    "ENABLE_ENV", "is_enabled", "wire", "current", "reset_for_tests", "migrate",
    "ConversationCaptureWiring",
]
