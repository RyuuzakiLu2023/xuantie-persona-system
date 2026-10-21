
# ================================================================
# wal_engine.py · 总导游图（改代码前必读，读完再动手）
#
# 这是什么：WAL（Write-Ahead Logging）记忆引擎——所有记忆操作先写日志，保证持久性；
#           重启/崩溃前未落盘的条目可重放补写。
#
# 结构：MemoryEntry dataclass（含 namespace/project/host_origin/visibility/tier +
#       M01 血缘四件套 track/source_ids/derived_from/operation_id/write_status）→
#       append/store + 跨进程锁 _locked_wal（Windows msvcrt 1 字节区间锁）→
#       **_iter_wal_lines（字节级严格解码，三条读取路径的唯一入口）** →
#       read_uncompacted/read_all/get_delta → mark_compressed/cleanup_expired →
#       add_tombstone/read_tombstones/remove_tombstone。
#
# 修改铁律：
# 1. 整文件重写（mark_compressed/cleanup_expired/purge）必须走 _locked_wal(required=True) 强锁，
#    拿不到就抛 RuntimeError，绝不无锁重写（并发重写丢刚写的日志）。
# 2. MemoryEntry to_json/from_json 用 asdict/**data——加字段自动序列化；旧 JSON 行缺字段走默认（向后兼容）。
# 3. append 只追加；读方持有旧句柄时 os.replace 仍原子生效（CRT 共享删除模式）。
# 4. 【不】把 from_json 改回 cls(**data)：未知键会抛 TypeError，把"格式演进"变成"数据损坏"。
#    M01 已改为只认已知字段（见 from_json 注释），旧版本写的行必须照常读得出来。
# 5. tombstone 独立成 tombstones.jsonl，【不】混进 current.wal——WAL 会被整文件重写带走，
#    存根得活下来（理由见 WALEngine.__init__）。
# 6. 【M01 第三轮 · 咬人的两条，【不】许退回】
#    ① 读 WAL 一律走 _iter_wal_lines：**二进制读 + 逐行严格 decode("utf-8")**。
#       解码失败的行以 bytes 形态带出 → 留原始字节取证 → 跳过。**禁止** errors='replace'——
#       那是非法字节被洗成 U+FFFD 后冒充"正常条目"（含被篡改的 host_origin）进库的洞（R4）。
#       同理 from_json **不对 U+FFFD 做文本检查**：U+FFFD 是合法字符（误杀合法正文），
#       真篡改在字节层就被拦了（R4-valid-literal-UFFFD-recovery）。
#    ② 锁归属标志 `_lock_state.needs_lock` 是**线程局部**（threading.local），
#       【不】是普通实例属性——否则线程 A 持锁重写会把线程 B 的取证也标记成"无需进锁"（R5）。
# 更新：2026-10-13 · M01 第三轮（A 路）：字节级严格解码 + 原始字节无损取证（raw_b64/原始字节 sha16）、
#       from_json 身份必需性校验（不再凭空造 id/时间）+ 放行合法 U+FFFD、锁归属线程局部化、
#       存根读改整体 fail-closed（任一行坏 → None）+ 新增 remove_tombstone（CLI 失败补偿用）。
# 更新：2026-10-12 · M01 补全 WAL 病历：血缘字段随日志落盘、重放不再硬猜、坏行留取证副本、
#       tombstone 防遗忘条目重放复活；旧 WAL 行向后兼容。
# 更新：2026-10-12 · O01 追加/坏行/读取失败可追踪；保留强锁与原恢复语义，不输出坏行正文。
# ================================================================
# hermes/memory/wal_engine.py
# WAL (Write-Ahead Logging) 记忆引擎
# 所有记忆操作先写日志，保证持久性

import base64
import json
import os
import threading
import time
import uuid
import logging
import platform
import hashlib
from contextlib import contextmanager
from typing import Iterator, Tuple, Union
from dataclasses import dataclass, field, asdict, fields
# Any：source_refs 现在要装"字符串或结构化 SourceRef"，标注放开到 Any（理由见字段声明处注释）
from typing import Any, List, Optional, Literal
from pathlib import Path
from .utils import estimate_tokens
from .diagnostics import diagnosed, emit

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
    # M01（2026-10-12）：病历补全——血缘与写入身份随 WAL 落盘，重放不再硬猜。
    # 此前 track/source_ids/derived_from 根本没进日志，崩溃重放只能：
    #   track 用关键词硬猜、来源链清空、出处写死 "wal-replay"（B00 探针实测五字段丢失）。
    # ⚠️ 全部给默认值——旧 WAL 行缺这些字段时 from_json 走默认，【不】能炸（向后兼容铁律）。
    track: Optional[str] = None
    source_ids: List[str] = field(default_factory=list)
    derived_from: Optional[str] = None
    # 写入身份与状态机（Spec §9.1 契约 1/2）：operation_id 串起同一次写入的各阶段诊断；
    # write_status 记录本条已推进到 received/persisted/indexed/...（详见 WAL_WRITE_STATUSES）。
    operation_id: Optional[str] = None
    observation_id: Optional[str] = None
    request_hash: Optional[str] = None
    write_status: str = "received"
    # ★R2 补件（2026-10-13 验收方反例）：updated_at 旧版根本没进 WAL，重放只能拿
    #   恢复时的新时间顶替，updated_at 失真。补上这个字段随正文落盘。
    #   三态问题（"调用方没传"vs"显式空值"）不在 WAL 加哨兵——而是 store() 归一时就把
    #   DB 最终落库的值定死、WAL 存同一个值（visibility 落 'shared'、host_origin 落 ''），
    #   重放直接用不再二次猜默认；旧行缺字段（None）才走回落，天然区分。
    updated_at: Optional[float] = None

    # ══ ★M01 第三轮（2026-10-13）v2 信封字段（B 路区段）════════════════════════
    # Spec §9.1 契约 1：载荷必须能回答"这条是谁说的、出自哪里、什么时候有效"。
    # 以下字段【全部 Optional】——**缺就是缺，【不】编默认值造事实**：
    #   旧行没有 speaker/subject/source_refs，就在读取侧保持 None/空，
    #   绝不用"未知"冒充"已知"（把未知写成值，正是本件要根治的病根）。
    # ⚠️ 字段"存在性"【不】靠真假值判断（空串 '' 是合法已知值），而由 `_present_fields`
    #    单独记录——见 __post_init__ 的注释与契约 §5 的判据表。
    format_version: Optional[int] = None          # 2=本版信封；旧行缺它 = v1 载荷
    content_hash: Optional[str] = None            # 与 DB content_hash 同源，重放可核对
    speaker: Optional[str] = None                 # 谁说的（对话主体）
    subject: Optional[str] = None                 # 关于谁/关于什么
    # ★第三轮 R2：出处引用【不】限字符串——类型化 SourceRef（dict/dataclass）必须能原样往返，
    #   所以标注从 List[str] 放宽到"JSON 可安全往返的引用列表"。
    #   ⚠️【不】要"为了类型好看"在写入侧 str() 一下，那正是本轮要修的"结构烧成文本"
    #   （详见 __post_init__ / _json_safe_ref 的注释）。⚠️ 字段名保持 source_refs 不变——
    #   生产 WAL 存量行用的就是这个名字，改名等于把所有旧行读成"字段缺失"。
    source_refs: List[Any] = field(default_factory=list)  # 出处引用（字符串/结构化 SourceRef 均可）
    scope: Optional[str] = None                   # 适用范围（域/项目/会话）
    valid_from: Optional[float] = None            # 事实生效起点
    valid_until: Optional[float] = None           # 事实失效点（None=未声明失效）
    confirmation_type: Optional[str] = None       # 确认方式（explicit/inferred/...）
    provenance_status: Optional[str] = None       # verified / legacy_unverified

    def __post_init__(self):
        """构造后归一（B 路区段）。干两件事，缺一不可：

        1) 记 `_present_fields` = "这条载荷实际带了哪些字段"。
           新写入的条目 = 全部字段名（store() 侧本来就填齐）；读旧行时由 from_json
           用原始 JSON 的键集合覆盖它（A 路），重放侧据此判断"键到底在不在"。
           ⚠️【不】把它做成 dataclass 字段：一旦成为字段，asdict() 会把它写进 WAL 行，
              下一版读回来就变成"字段存在"，存在性判断被自己污染（自证循环）。
           ⚠️【不】用"值是真还是假"判断存在性——空串是合法已知值：R2 判据就是
              显式 `host_origin=""` 必须原样保住；真假值判断会把空串误当缺失再回落
              'wal-replay'，把已知事实洗成猜测值（round-2 实测红项）。
        2) provenance 缺失一律标 `legacy_unverified`（契约 §5）。
           ⚠️ 只标状态，【不】顺手补 speaker/subject/source_refs——补造就是编事实。
           改了会怎样：任一老条目重写（mark_compressed）后仍能被认出"出处未验证"，
           而不是被洗成"已验证"。
        """
        self._present_fields = frozenset(f.name for f in fields(self))
        # ★第三轮（2026-10-13）R2：source_refs 必须**结构化**往返，【不】许 str() 洗成
        #   Python 字面量串（"{'kind': 'conversation_event', 'revision': 2, ...}"）。
        #   旧写法 `[str(x) for x in ...]` 把类型化 SourceRef 的结构压成一串文本，
        #   WAL 读回来时 kind/locator/revision 再也分不出来 = 出处不可机器消费
        #   （验收 v3 `R2-typed-source-ref-preservation` 实测红项）。
        #   ⚠️ 兼容两种历史形态：旧 WAL 里 source_refs 可能是**字符串列表**（原样保留），
        #      也可能是**裸字符串**（不是 list）——包成单元素 list，
        #      【不】像旧写法那样逐字符拆开（"abc" → ['a','b','c'] 那种事故）。
        #   ⚠️ 改了会怎样：WAL/DB 两侧拿到的都是同一个结构化列表，
        #      出处对象经 to_json/from_json 一次往返后 `==` 原对象（逐字相等）。
        self.source_refs = self._coerce_source_refs(self.source_refs)
        if self.provenance_status is None:
            self.provenance_status = "legacy_unverified"

    @staticmethod
    def _json_safe_ref(item):
        """把单个出处引用归一成 **JSON 可无损往返**的形态（★M01 第三轮 R2 的根）。

        为什么要有它：契约 §5 要求 source_refs「可序列化并完整读回」。
        `to_json` 走的是 `json.dumps(asdict(self))`，能进 JSON 的类型有限——
        dataclass 实例直接塞进去会当场 `TypeError: Object of type SourceRef is not JSON serializable`，
        而"图省事 str() 一下"又会把结构烧成文本（这正是要修的病原）。所以出口只留这一条：
          - None / bool / int / float / str → 原样（JSON 原生类型，往返逐字相等）；
          - dict → 键统一成 str 后递归（JSON 的键本来就只能是 str）；
          - list / tuple → 递归成 list；
          - dataclass 实例 → 先 asdict 再递归（调用方直接传 SourceRef 对象也能落地）；
          - bytes / bytearray → utf-8 尽力解码（`\\xff` 这种非法字节走 replace，
            因为这里在做"结构归一"不在做"字节取证"，取证判据在 A 路 _iter_wal_lines）；
          - 其它未知对象 → str() 降级。这一条是**明确的类型降级**（"我不认识这个类型"），
            与旧写法的"结构性丢失"（认识也压成文本）不是一回事，日志/注释都点明了。
        """
        if item is None or isinstance(item, (bool, int, float, str)):
            return item
        if isinstance(item, (bytes, bytearray)):
            return bytes(item).decode("utf-8", "replace")
        if isinstance(item, dict):
            return {str(k): MemoryEntry._json_safe_ref(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [MemoryEntry._json_safe_ref(v) for v in item]
        if hasattr(item, "__dataclass_fields__"):
            try:
                return {str(k): MemoryEntry._json_safe_ref(v) for k, v in asdict(item).items()}
            except Exception:
                return str(item)
        return str(item)

    @classmethod
    def _coerce_source_refs(cls, raw):
        """入参 → list[JSON 安全项]。【不】丢结构、【不】逐字符拆裸字符串。

        返回值恒为 list（可能为空），下游 `for ref in entry.source_refs` 永远安全。
        """
        if raw is None:
            return []
        # 裸标量/裸字典/裸 dataclass：本身就是一个"引用"，包成单元素列表。
        # ⚠️ 必须显式列 str/dict，不能只判 `not isinstance(raw, list)` 后直接迭代——
        #    对字符串迭代会得到逐字符的结果（旧实现的事故形态）。
        if isinstance(raw, (str, bytes, bytearray, dict)) or hasattr(raw, "__dataclass_fields__"):
            raw = [raw]
        elif not isinstance(raw, (list, tuple)):
            raw = [raw]
        return [cls._json_safe_ref(x) for x in raw]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "MemoryEntry":
        """从 WAL 行反序列化（入参必须是【已严格解码】的 str，字节层判据见 _iter_wal_lines）。

        ★R4 修复（2026-10-13 验收方反例驱动，第三轮重写）：
        R4.1 旧行为：`json.loads('[]')` 得 list，`.items()` 抛 AttributeError，
             而 read_uncompacted 只捕获 (JSONDecodeError, TypeError) —— AttributeError
             掀翻整次读取，合法后续行全部丢失且不留取证。
             修法：顶层类型显式校验 + 逐行异常收口到统一护栏。（保留）
        R4.2 第二轮旧行为：content 内嵌非法字节经 errors='replace' 变 U+FFFD，仍能过 JSON 解析，
             篡改正文被当"正常条目"恢复入库且不留证。
             第二轮修法（**本轮已作废**）："content 含 U+FFFD 就 raise"。
             ⚠️ 作废原因（R4-valid-literal-UFFFD-recovery 实测）：U+FFFD 是**合法字符**，
               正文里真写了它（合法 UTF-8）会被这条文本检查误杀 → 条目丢失。
               而且文本层检查**拦不住**真正的篡改——真正的非法字节在字节层就被
               _iter_wal_lines 拦下留证了，根本走不到这里。
               所以：把非法字节判据下沉到字节层，本函数对 U+FFFD **放行**。
        R4.3 本轮新增：必需身份字段 **id / timestamp** 必须在原始 JSON 键里且类型合法。
             旧行为：缺 id/timestamp 的行会被 dataclass 默认值补出**随机 id + 当前时间**，
             等于凭空捏造一条"事实"进库（R4-missing-identity-not-invented 实测）。
             ⚠️ 注意判据是 `in data`（键存在性）而不是真值——timestamp=0.0 是合法时刻，
               不能因为 falsy 被误杀。id 判据保持原语义（空串/缺键都算无效）。
             ⚠️ 为什么不会误杀旧数据：生产 WAL 实测 4038/4038 可解析行**全部带 id 和 timestamp**
               （`python -c` 抽样核对过），代际边界清晰，向后兼容不受影响。
        【不】要把顶层校验删掉换回"只捕宽异常"：那等于把 R4.1 回归原样装回去。
        【不】要把 U+FFFD 文本检查加回来：它既多杀合法正文、又拦不住真篡改，两头不讨好。
        """
        data = json.loads(line)
        if not isinstance(data, dict):
            raise TypeError(f"wal line top-level must be an object, got {type(data).__name__}")
        # R4.3：身份必需性——必须在**原始键**里逐个确认，绝不让 dataclass 默认值替我们编事实。
        # （`data.get()` 判不了"键在不在"，这正是上一轮凭空造 id/时间的根因。）
        entry_id = data.get("id")
        if "id" not in data or not entry_id or not isinstance(entry_id, str):
            raise ValueError("wal line missing/invalid identity field: id")
        if "timestamp" not in data or not isinstance(data["timestamp"], (int, float)) \
                or isinstance(data["timestamp"], bool):
            raise ValueError("wal line missing/invalid identity field: timestamp")
        known = {f.name for f in fields(cls)}
        entry = cls(**{k: v for k, v in data.items() if k in known})
        # R4.3 补充：关键字段类型校验（防"content 是数字/列表"这类畸形行被静默接受）
        if not isinstance(entry.content, str):
            raise TypeError("wal line missing/invalid content")
        if not isinstance(entry.source_ids, list):
            entry.source_ids = [str(s) for s in entry.source_ids] if entry.source_ids else []
        if entry.write_status not in WAL_WRITE_STATUSES:
            entry.write_status = "received"
        # ★B 路接口（契约 §5）：记下原始键集合，供重放侧判"字段存在性"（R2 空出处保真的根）。
        # ⚠️ 必须留在实例上做**普通属性**（`_present_fields`），【不】做成 dataclass 字段——
        #   做成字段就会被 asdict() 序列化进 WAL 行，下次重写再读出来，键集合自我污染。
        entry._present_fields = frozenset(data.keys())
        return entry

    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return (time.time() - self.timestamp) > self.ttl


# P1-9 + R-01（2026-09-03）：跨进程锁。所有 WAL 写操作（append / 整文件重写）共用
# <wal_dir>/.lock 锁文件：Windows 走 msvcrt 1 字节区间锁，POSIX 走 fcntl.flock，
# 两分支统一强锁语义（required=True 拿不到即抛错，禁止静默无锁降级），防止：
#   - append 与 mark_compressed/cleanup_expired 整文件重写同时发生时互相踩踏
#   - Windows 上 os.replace 与未锁写入的竞态导致刚写的日志丢失
# 读操作不加锁：读方持有旧句柄时 replace 仍原子生效（CRT 共享删除模式）。
WAL_LOCK_ACQUIRE_TIMEOUT = 5.0      # 拿锁超时上限（秒）
WAL_LOCK_REWRITE_REQUIRED = True    # 重写操作强锁：拿不到就抛错，绝不无锁重写

# ── M01（2026-10-12）：写入状态机与 tombstone 文件 ──────────────────────
# 状态推进语义（Spec §9.1 契约 2）：received → persisted → indexed → extracted，
# 旁支 rejected/failed。
#   ⚠️ persisted 只允许在 WAL fsync 真正返回之后落 —— 它现在的含义就是「durable commit 过了」。
#   ★第三轮补 extracted（2026-10-13）：NER/图后置钩子跑完才是全链终点，Spec §9.1 明列，
#     round-2 漏了这一个词。漏了的后果：后置钩子阶段无法记账，「跑完没跑完」分不开，
#     只能借 indexed 冒充全链完成——正是本件要根治的"假报完成"。
#   索引失败标 indexed 的 failed 态（写进 WAL 行），【不】假报全链完成。
#   【不】要新增状态词：这些值会被重放侧与诊断侧当封闭枚举消费（见 diagnostics 同款纪律）。
WAL_WRITE_STATUSES = frozenset({
    "received", "persisted", "indexed", "extracted", "rejected", "failed",
})
# tombstone 文件名（与 current.wal 同目录，独立文件便于增量追加与人工取证）
TOMBSTONE_FILE_NAME = "tombstones.jsonl"
# ── 持久阶段回执（★M01 第三轮，2026-10-13，Spec §9.1 契约 2 + 验收 R3 补件）──
# 为什么另开一个文件而不是只靠 WAL 行/诊断事件：
#   ① WAL 行内 write_status 是"序列化那一刻"的快照，append-only 不回头改写，
#      后面 indexed/extracted 的推进它记不下来；
#   ② 诊断事件有节流 + 1MB 轮转，是"采样"，不是"逐笔账"。
#   验收方要的是"某次写入到底推进到哪一步、哪一步炸的"可逐笔回查，故单独立账。
# ⚠️【不】要用"WAL 行存在"反推 fsync 成功——行在 fsync 前也已落盘（同一条 append 调用里），
#    两者不可区分。这正是验收方 R3 点名的错误推断；durable 与否只认 append 有没有返回。
RECEIPT_FILE_NAME = "write_receipts.jsonl"
# 回执文件容量上限（8MB）：到顶停写 + emit 告警一次（【不】自动删旧，人工归档）。
# ⚠️ 为什么要上限：这是 append-only 的逐笔账，无界增长会把记忆目录灌满
#    （corrupt.log 曾在生产 17 分钟堆 35MB，见 _preserve_corrupt_line 血泪教训）。
#    到顶后停写而不是删旧——回执是取证材料，静默丢旧等于篡改证据链。
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
# 回执 stage 合法值（与 WAL_WRITE_STATUSES 同一套词，【不】造第二套。契约 §1.2）
RECEIPT_STAGES = WAL_WRITE_STATUSES
# 回执 status 合法值（封闭集，多余值一律归一成 failed，防野值污染账本）
RECEIPT_STATUSES = frozenset({"ok", "failed", "rejected"})
# 坏行取证单文件硬上限（8MB）：到顶就停写并告警。
# ⚠️ 设上限的原因见 _preserve_corrupt_line 的【血泪教训】——首版无上限的写法
#    在生产上 17 分钟堆了 103 个文件 / 35MB，把"留证"变成了"灌盘"。
MAX_CORRUPT_BYTES = 8 * 1024 * 1024
# 坏行取证文件名（单文件，恒定不新建；理由同见 _preserve_corrupt_line）
CORRUPT_FILE_NAME = "corrupt.log"


def derive_memory_id(operation_id: str, namespace: str, content: str) -> str:
    """由「操作身份 + 域 + 正文」确定性派生记忆 ID（★M01 第三轮，契约 §3）。

    为什么要有它（验收 R3 点名的病灶）：同一次写入重试时，旧实现每次都现摇随机
    `mem-<uuid4[:8]>` ⇒ 第一次失败、第二次成功，WAL 里留下**两条不同 id 的同内容行**，
    操作级幂等无从谈起（"重试"在账上看起来像"写了两条不同的事实"）。
    修法：只要调用方给了 operation_id（= 明确的"这就是同一次操作"意图），
    id 就由三者哈希派生——同操作重试必得同 id，幂等才成立。

    ⚠️ 格式【必须】是 `^mem-[0-9a-f]{8}$`：全库多处正则依赖这个形状
       （vector_store._PTR_RE、provider 的 [m:mem-xxxxxxxx] 引用解析）。
       破坏格式 = 连带炸掉召回链路，不是"改个 id 生成方式"那么轻。
    ⚠️ 用 `\\x00` 做分隔符：正文可能含任意字符，普通分隔符（-、:）会和内容撞车
       造成"不同三元组哈希相同"的假碰撞；NUL 不会出现在正常文本里。
    ⚠️ 编码走 errors="replace"：正文若含无法编码的代理字符，宁可替换也不要抛异常——
       这是写入主路径，抛异常 = 整条记忆写不进去。
    """
    digest = hashlib.sha256(
        f"{operation_id}\x00{namespace}\x00{content}".encode("utf-8", "replace")
    ).hexdigest()
    return "mem-" + digest[:8]


@contextmanager
def _locked_wal(wal_dir: str, *, required: bool = False, timeout: float = WAL_LOCK_ACQUIRE_TIMEOUT,
                blocking: bool = False):
    """跨进程互斥上下文。required=True 时拿不到锁会抛 RuntimeError（重写操作用）。

    R-01（2026-09-03）：跨平台统一锁后端——Windows 用 msvcrt 字节区间锁，
    POSIX 用 fcntl.flock；两分支共用同一锁文件 <wal_dir>/.lock 与同一
    忙等超时语义。blocking=True 为 append 专用：用阻塞锁等待直到拿到锁，
    保证竞争下最终写入而不是超时丢写。拿不到锁一律不静默降级。
    """
    lock_path = Path(wal_dir) / ".lock"
    with open(lock_path, "a+") as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write("\0")
            f.flush()
        locked = False
        if blocking:
            # R-01 修正 v2：阻塞锁必须无限等。msvcrt.LK_LOCK 内部约 10 次尝试后仍可能抛
            # OSError，4 写者热竞争时部分子进程会饿死；因此 OSError 后继续重试直到拿到锁。
            # POSIX flock(LOCK_EX) 阻塞语义同理，EINTR 也走重试。
            while True:
                try:
                    if platform.system() == "Windows":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        f.seek(0)
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    locked = True
                    break
                except OSError:
                    continue
        else:
            deadline = time.time() + timeout
            while True:
                try:
                    if platform.system() == "Windows":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        f.seek(0)
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.02)
        if required and not locked:
            raise RuntimeError(
                f"WAL lock not acquired within {timeout:.1f}s: {lock_path}"
            )
        try:
            yield locked
        finally:
            if locked:
                f.seek(0)
                try:
                    if platform.system() == "Windows":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
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
        # M01：tombstone 存根文件——遗忘/归档的 id 落这里，重放前先查它，防"删了又被 WAL 复活"。
        # 为什么独立文件而不是塞进 WAL 行：WAL 会被 mark_compressed/cleanup_expired 整文件重写，
        # 存根混在里面会被"清理过期"顺手带走；且 purge 是按 id 删 WAL 行的，存根得活下来。
        self.tombstone_file = self.wal_dir / TOMBSTONE_FILE_NAME
        # ★M01 第三轮：持久阶段回执落点（append-only 逐笔账，见 RECEIPT_FILE_NAME 注释）
        self.receipt_file = self.wal_dir / RECEIPT_FILE_NAME
        # 回执封顶告警只报一次（与 _corrupt_capped 同款语义：到顶后每笔都告警 = 刷屏）
        self._receipt_capped = False
        # M01 坏行取证去重状态：None=尚未加载（懒加载当天文件的 sha16 集合），
        # set()=已加载。⚠️ 必须是每实例状态，【不】做成类变量——多实例共享会让
        # "已见过"漏判（另一个 WAL 目录的坏行被误当见过，真坏行反而不留证）。
        self._corrupt_seen = None
        self._corrupt_capped = False
        # ★R5 线程局部锁归属（2026-10-13 第三轮）：True=本线程**未**持有 WAL 锁，取证要自己进锁；
        #   False=本线程已在 mark_compressed/cleanup_expired 的锁内，取证【绝不能】再要锁。
        #   ⚠️ 为什么必须线程局部（threading.local）而不是普通实例属性——
        #   `self._needs_lock` 是**整个实例**一份：线程 A 持锁重写时置 False，
        #   同实例的线程 B 此时来取证会读到 False → 跳过自己的锁 → 与 A 的重写并发写 corrupt.log
        #   （R5-thread-local-lock-ownership 实测：B 线程根本没进锁）。
        #   锁是「谁持有」的概念，归属标志就必须是「每线程一份」。
        #   ⚠️ 读侧一律 `getattr(self._lock_state, 'needs_lock', True)`：属性没设过时默认 True（保守=要锁）。
        #   【不】要改回普通实例属性：那是把跨线程串锁装回去。
        #   ⚠️ 同一线程内可重入性不受影响（重写路径在同线程内先置 False，取证同线程读到 False 不再要锁），
        #      自死锁守卫照旧有效；锁文件仍是跨进程互斥的那把 .lock。
        self._lock_state = threading.local()

    @diagnosed('wal', 'write.wal')
    def append(self, entry: MemoryEntry) -> None:
        """追加一条记忆到 WAL（fsync + Windows 文件锁保证持久性）

        Windows 说明：msvcrt.locking 锁定的是「当前文件指针」开始的字节区间。
        以追加模式写入后指针会前移，若在移动后的位置调用 LK_UNLCK 会解锁一段
        从未加锁的区间并抛出 PermissionError(Errno 13)。因此这里固定锁定并解锁
        字节 0（作为跨进程互斥的哨兵字节），写入前后都显式 seek 回该位置。
        """
        # ★R3 修复（2026-10-13 验收方反例）：write_status 只能【在 fsync 成功之后】置为
        # persisted。旧版在 fsync 之前就改状态，注入 os.fsync 失败时：业务调用确实收到异常，
        # 但「传入的 entry 对象」和「WAL 里仍可读出的那一行」都已自称 persisted——
        # 失败窗口里的状态与 durable receipt 不一致（验收方实测复现）。
        # 修法：写入序列化时保持 received；fsync 返回后才把**对象**改成 persisted。
        # ⚠️ 已写进 WAL 的那一行的行内字段仍是 received（append-only，不回头改写）——
        #   这是刻意的：durable 事实由「行存在」本身保证，状态字段供诊断/幂等判断，
        #   若要行内也标 persisted 需重写整文件，代价与风险远大于收益（Spec 契约 2 报告
        #   语义靠 emit + 对象状态，不靠行内字段）。
        line = entry.to_json() + "\n"

        # R-01（2026-09-03）：追加走阻塞强锁（blocking=True + required=True）——
        # 与整文件重写互斥时等待而不是超时抛错；绝不静默裸写、绝不超时丢写。
        try:
            with _locked_wal(self.wal_dir, required=True, blocking=True):
                with open(self.wal_file, "a", encoding="utf-8") as f:
                    f.seek(0, os.SEEK_END)  # 追加模式下写入始终落在文件末尾
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as exc:
            # ★M01 第三轮（2026-10-13）：写失败要留下 failed 回执（带 error_type）。
            # 为什么在这里记而不是往上抛给调用方记：本函数是**唯一**知道"fsync 到底成没成"
            # 的地方。契约 §4 明令【不】能用"WAL 行存在"反推 fsync 成功——行在 fsync 前
            # 也已落盘，两者不可区分；能区分二者的只有"append 有没有返回"这个事实本身。
            # 所以 durable 的正面证据（persisted）和反面证据（failed）都在这一层落账。
            # ⚠️ 回执是旁路：记完照样把异常抛出去，绝不吞——业务必须知道这条没写进 WAL。
            self.record_receipt(entry.operation_id, entry.id, "failed",
                                status="failed", error_type=type(exc).__name__)
            raise

        # ★R3：走到这里 = fsync 已成功 = durable commit 成立，才允许报 persisted
        entry.write_status = "persisted"
        # durable 回执（★第三轮）：行存在【不】是证据，fsync 返回才是。顺序焊死——
        # 先 append 返回、再落 persisted 回执；反过来的话失败注入时会留下"说了 durable
        # 其实没 durable"的假账（正是验收 R3 点名的错误推断）。
        self.record_receipt(entry.operation_id, entry.id, "persisted", status="ok")

        logger.debug("WAL append: %s (type=%s, importance=%.2f)", entry.id, entry.type, entry.importance)
        emit('wal', 'write.wal', 'ok')

    def store(self, content: str, **kwargs) -> MemoryEntry:
        """快捷方法：存储一条记忆"""
        entry = MemoryEntry(
            content=content,
            token_count=self._estimate_tokens(content),
            **kwargs,
        )
        self.append(entry)
        return entry

    # ── M01：坏行取证副本 ────────────────────────────────────────────
    def _preserve_corrupt_line(self, raw_line, line_number: int) -> None:
        """把读不动的 WAL 行原文留证到 wal_dir/corrupt.log，绝不猜正文。

        为什么要它（Spec §9.1 契约 5）：此前坏行只 emit 计数，原文随下一次整文件重写
        （mark_compressed/cleanup_expired）一起蒸发——事后想查"到底坏成什么样"已无证可取。

        ⚠️【血泪教训 ×3，改前必读】
        第一版事故（2026-10-12 施工自造）：「每次读到坏行就新开一个时间戳文件」，
          生产 WAL 有 64 条历史坏行 + 常驻进程每 ~15 秒重载读一次 ⇒ 17 分钟堆
          **103 个文件 / 35MB**（无界增长）。
        第二版被验收方证伪（2026-10-13 R5）：内存缓存只在进程内有效，两个真实进程
          顺序读同一新坏行 ⇒ corrupt.log 出现 2 份相同内容（缓存加载后**不在锁内**
          重新核对磁盘）；且封顶判定只查旧文件大小，单行稍大就写成 8388669 > 8388608。
        第三版被验收方证伪（2026-10-13 R4，本函数本轮修复）：旧签名只收 str，
          `errors='replace'` 一洗，**原始字节永久丢失**——corrupt.log 里 `raw`/`sha16`
          记的都是被 U+FFFD 替换后的文本，事后无法复原"到底坏在哪几个字节"。
        修复后的硬约束（【不】许退回）：
          ① 单文件 corrupt.log；
          ② 跨进程去重：懒加载 + 追加前**在锁内重读磁盘核对**（缓存只做快路径）；
          ③ 封顶判定用「旧大小 + 本次编码后记录大小」一起算，绝不先写后超限；
          ④ 锁内完成"核对→算量→追加"三步，与 WAL 同一把互斥锁；
          ⑤ **原始字节必须无损留证**（本轮新增）：记录里同时存
             `raw`（文本视图，兼容旧读法/人工肉眼）与 `raw_b64`（原始行字节的 base64，
             无损可还原），`sha16` 改为**原始字节**的摘要（旧版是 replace 后文本的摘要，
             校验的是"洗过的样子"，等于没校验）。
        ⚠️ 入参 raw_line 现在是 `str | bytes` 二态：
           - str  = 内容坏（JSON 坏了/身份字段坏了），文本本身就是合法 UTF-8 证据 → `raw` 存它；
           - bytes = 字节坏（严格 UTF-8 解码失败），**只有 bytes 能表达"哪几个字节非法"**
             → `raw` 存 replace 后的文本视图，`raw_b64` 存原始字节。
           两种都生成 `raw_b64`（str 走 utf-8 编码），下游还原逻辑统一。
        ⚠️ 本函数【不】参与重放判断——坏行一律不重放，只留证。取证失败也不向上抛。
        """
        try:
            # 原始字节：bytes 直接用；str 用 utf-8 编码。后续摘要/去重/落盘全以它为准。
            raw_bytes = raw_line if isinstance(raw_line, (bytes, bytearray)) else str(raw_line).encode("utf-8")
            # 文本视图：bytes 必须先 replace 成 str 才能塞进 JSON 字符串。
            raw_text = raw_bytes.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
            # ★R4：sha16 用**原始字节**算（旧版用 replace 后的文本，非法字节被洗成同一个
            #   U+FFFD 后不同坏行会算出同一个指纹 → 去重误判 + 无法校验原始内容）。
            #   与 _evidence_ok 共用 _corrupt_digest，两边算法永不漂移。
            digest = self._corrupt_digest(raw_bytes)
            record = {"sha16": digest, "line_number": line_number, "raw": raw_text,
                      "raw_b64": base64.b64encode(bytes(raw_bytes)).decode("ascii")}
            payload = json.dumps(record, ensure_ascii=False) + "\n"
            encoded_size = len(payload.encode("utf-8", "replace"))
            path = self.wal_dir / CORRUPT_FILE_NAME
            # 快路径：本线程进程内缓存命中就直接走（不进锁，避免热路径每次进互斥）
            if self._corrupt_seen is not None and digest in self._corrupt_seen:
                return
            # ★R5 修复 + 自死锁修复（2026-10-13）：
            #   取证的三步（去重→算量→追加）必须与 WAL 整文件重写互斥——但重写路径
            #   （mark_compressed/cleanup_expired → read_all → 本函数）**已经持有这把锁**。
            #   _locked_wal 是进程内不可重入的区间锁，锁内再要锁 = 自死锁
            #   （实测：带一条坏行调 mark_compressed 直接挂死 60s+，R4.3/R4.4 场景必触发）。
            #   修法：需要时才进锁（needs_lock=True 由调用方声明"当前线程没持有 WAL 锁"）。
            #   ⚠️【不】要改成无条件进锁：那等于把自死锁装回去。
            if getattr(self._lock_state, 'needs_lock', True):
                with _locked_wal(self.wal_dir, required=True, blocking=True):
                    return self._preserve_corrupt_line_locked(path, digest, payload, encoded_size, line_number)
            return self._preserve_corrupt_line_locked(path, digest, payload, encoded_size, line_number)
        except Exception:
            # 取证是旁路：失败只记 debug，【不】向上抛——读路径可用性优先于取证完整性
            logger.debug("corrupt line preservation failed", exc_info=True)

    def _preserve_corrupt_line_locked(self, path, digest, payload, encoded_size, line_number) -> None:
        """取证的实际写入段（调用方保证所需的锁已持有/无需持有）。"""
        try:
            # ★R5-cross-process 修复（2026-10-13 第三轮）：**每次都在锁内重读磁盘指纹**。
            #   旧版只在 `self._corrupt_seen is None` 时读一次——两个真实进程各自"已初始化过缓存"
            #   时，第二个进程进锁后看到的是自己内存里的旧快照，磁盘上刚被对端写进去的那条
            #   它看不见 ⇒ 同一坏行写 2 份（验收方实测 copies_of_identical_line=2）。
            #   ⚠️ 代价：每次取证多一次 corrupt.log 全量读。坏行是**异常路径**（正常 WAL 没有坏行），
            #      而且封顶上限 8MB，可接受；热路径的担心由 _preserve_corrupt_line 的缓存快路径承担。
            #   ⚠️【不】要退回"只在 None 时加载"：那等于把跨进程重复留证装回去。
            if self._corrupt_seen is None:
                self._corrupt_seen = set()
            self._corrupt_seen |= self._load_corrupt_digests(path)
            if digest in self._corrupt_seen:
                return  # 锁内复核磁盘指纹——另一个进程可能刚写过
            # ③ 容量：用「现大小 + 本次编码后大小」判，绝不允许先写后超限（R5b）
            cur_size = path.stat().st_size if path.exists() else 0
            if cur_size + encoded_size > MAX_CORRUPT_BYTES:
                if not self._corrupt_capped:
                    self._corrupt_capped = True
                    emit('wal', 'recovery.wal_read', 'degraded',
                         fallback_reason='invalid_wal_line', count=1)
                    logger.warning(
                        "corrupt log capped: %d + %d > %d bytes (bad line %d kept in WAL "
                        "until rewrite; see 错题本 #29 / 验收 R5)", cur_size, encoded_size,
                        MAX_CORRUPT_BYTES, line_number)
                return
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            self._corrupt_seen.add(digest)
        except Exception:
            logger.debug("corrupt line preservation (locked) failed", exc_info=True)

    def _load_corrupt_digests(self, path) -> set:
        """从取证文件加载已记指纹（坏行跳过不抛，坏文件宁可少拦几条也别停摆重放）。

        ⚠️ 读的是 `sha16`（现在语义=原始字节摘要）。旧 corrupt.log 里存的是"replace 后文本
        摘要"，与新版算法不一致 ⇒ 旧条目在新进程里会**匹配不上**、同一坏行可能再记一条。
        这是本轮刻意接受的代价：corrupt.log 是取证旁路（不是业务数据），重复一条远好过
        "指纹算错导致该留证的没留"；且真实生产 WAL 的坏行是历史一次性遗留，不会持续新增。
        """
        seen = set()
        if not path.exists():
            return seen
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        line = line.strip()
                        if line:
                            sha = json.loads(line).get("sha16")
                            if sha:
                                seen.add(sha)
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except Exception:
            pass
        return seen

    # ── M01：WAL 行迭代器（字节级严格解码，A 路新读取器；B 路重放依赖它）──────
    def _iter_wal_lines(self) -> Iterator[Tuple[int, Union[str, bytes]]]:
        """逐行产出 `(行号, 行内容)`；行内容二态：**str = 解码成功**，**bytes = 解码失败（原始字节）**。

        ★R4 修复（2026-10-13 第三轮，验收方反例 R4-invalid-utf8-provenance 驱动）：
        旧行为：`open(..., encoding='utf-8', errors='replace')` 读 WAL。非法字节被洗成 U+FFFD，
          于是 `host_origin: "origin\\xff"` 变成 `"origin\\ufffd"` **照常通过 JSON 解析**，
          条目被当正常事实恢复入库，corrupt.log 里一个字节的取证都没有。
        修法：**二进制读 + 逐行严格 `bytes.decode('utf-8')`**。解不开的行在这里就地判定为
          "字节坏"，把**原始字节**原样交给调用方留证，然后跳过——绝不 replace 后继续走解析。

        为什么返回 bytes 而不是当场留证：留证要走 `_preserve_corrupt_line`，而调用方
        （read_all 重写路径）需要知道"这条留证成没成"来决定要不要放弃整次重写（evidence_required），
        所以把"判定"和"留证+决定"分开，判据集中在调用方一处。

        ⚠️【不】要退回文本模式 + errors='replace'：一个坏字节就足以让"篡改正文/出处"
          冒充"正常条目"进库，这是 R4 最危险的洞。
        ⚠️【不】要改成 errors='strict' 让 UnicodeDecodeError 掀翻整次读：那会把"坏一行"
          升级成"整个 WAL 读不出来"（deferred_recovery 全量停摆，旧注释里的顾虑仍然成立）。
          正确做法就是本函数的"逐行分别判"。
        ⚠️ 按 `\\n` 切分，与文本模式的迭代语义对齐（WAL 是 UTF-8 文本，行内不含裸 \\n）。
          行尾 `\\r` 也一起剥掉——旧文本模式是 universal newlines 会自动剥，字节解析必须手动补。
        """
        if not self.wal_file.exists():
            return
        with open(self.wal_file, "rb") as f:
            for line_number, raw in enumerate(f.readlines(), 1):
                raw = raw.rstrip(b"\n").rstrip(b"\r")
                if not raw.strip():
                    continue
                try:
                    yield line_number, raw.decode("utf-8")   # 严格解码：默认 errors='strict'
                except UnicodeDecodeError:
                    # 解码失败 = 字节级损坏。bytes 原样带出，交由调用方留证（保留原始字节）。
                    yield line_number, raw

    # ── M01：tombstone（遗忘/归档存根）────────────────────────────────
    def add_tombstone(self, entry_id: str, reason: str = "", by: str = "") -> bool:
        """登记一条 tombstone（id + 时间 + 原因），供重放侧跳过已遗忘条目。

        契约（§6）：返回 True = 已登记或已存在（幂等）；False = 入参无效/写失败。
        ⚠️ 与 CLI 软删口径对齐：遗忘走 state='archived' 内容保留，本存根只负责
          「重放时别再把它写回库里」，【不】动 DB、【不】删正文。
        """
        if not entry_id or not isinstance(entry_id, str):
            return False
        existing = self.read_tombstones()
        # R6：存量文件读不动时【不】因幂等短路——必须照样落一条新存根（宁可重不漏）
        if existing is not None and entry_id in existing:
            return True  # 幂等：重复登记不重复写行
        record = {"id": entry_id, "ts": time.time(), "reason": str(reason or ""), "by": str(by or "")}
        try:
            with _locked_wal(self.wal_dir, required=True, blocking=True):
                with open(self.tombstone_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as exc:
            # ⚠️ 存根写失败必须让调用方知道（返回 False），不能静默——静默的后果是
            # 「以为删干净了，重启又被 WAL 复活」，这正是本件要根治的故障形态。
            emit('wal', 'recovery.replay', 'failed', error=exc, fallback_reason='deferred_recovery')
            return False
        return True

    def read_tombstones(self) -> Optional[set]:
        """读取全部 tombstone id（返回 set，供重放前 O(1) 查询）。

        ★R6 修复（2026-10-13 验收方反例驱动，第三轮收紧）：返回值三态，
        【不】再用 set() 兜底一切——
          - set(...)      ：文件可读且**每一行 JSON 都可解析** → 正常判断
          - None          ：文件不可读 / 非 UTF-8 / **任一行 JSON 坏** → 【fail-closed】
          - 空集           ：文件确实不存在（真没有遗忘记录）

        ⚠️★本轮语义变更（第三轮，验收方更严要求取代本鱼自拟守卫）：
          round-2 写的是"单行坏不算整文件坏，跳过坏行返回**部分集合**"——**已作废**。
          为什么必须整体 fail-closed：坏掉的那一行**可能正藏着一个存根**，跳过它返回部分集合
          = 那个被遗忘的条目在重放时查不到自己的存根 → **复活**（R6-valid-utf8-malformed-tombstone
          实测：JSON 尾部截断但文件仍是合法 UTF-8 → 旧版返回空 set → 已遗忘条目被重放复活）。
          遗忘方向必须 fail-safe：宁可"整批恢复暂停"（可查、可修、可重试），
          也不能"少了一条存根就把用户明确遗忘的数据悄悄写回来"（不可逆）。
        ⚠️ 消费方（context_builder 重放）拿到 None 必须【暂停该批恢复】，绝不能当空集放行。

        ⚠️★v3 返修（2026-10-13，验收 V3-R6 第二半）：**JSON 可解析 ≠ 一条可信存根**。
          v3 反例喂了一行 `{"ts":100,"reason":"forget"}`——它是**合法 JSON**，旧版 `json.loads`
          一路绿灯，然后 `record.get("id")` 拿到 None，被 `if tid:` 静默跳过，函数返回**空 set**。
          后果和"跳过坏行"一模一样：WAL 里那条被删的记录查不到自己的存根 → **复活为 active**
          （实测 row_resurrected=true）。所以校验必须往上提一层：
            - 顶层必须是 dict（数组/字符串/数字/null 都不是存根）；
            - `id` 必须存在、是 str、且非空（数字 100 或 null 一律不算身份）。
          任一条不满足 ⇒ 与坏 JSON 同待遇：**整体 fail-closed（返回 None）**。
          判据集中在"有没有可信身份"这一处，【不】要在这里做"尽力而为的部分解析"。

        ⚠️ 这里【不】调用 _preserve_corrupt_line 留证：本函数是纯查询 API，
          R6 CLI（D 路）会高频调用它做幂等判断，进锁写文件会把查询变成写操作、
          还可能和调用方的 WAL 锁自死锁。坏行的取证由上面的 emit + 落盘原文（人工可见）承担。
        ⚠️ 血泪教训 5（2026-10-13）：拿不准就别放行。遗忘方向只认"确认无疑"的存根集合，
          其余的都可疑——可疑就整批暂停恢复（可查/可修/可重试），绝不"少一条存根就复活一条数据"。
        """
        if not self.tombstone_file.exists():
            return set()  # 真没有存根文件：确实没有遗忘记录
        try:
            raw = self.tombstone_file.read_bytes()
        except OSError:
            logger.warning("read_tombstones: unreadable tombstone file — fail-closed", exc_info=True)
            return None
        try:
            text = raw.decode("utf-8")          # 严格解码：非法字节 = 存根不可信
        except UnicodeDecodeError:
            logger.warning("read_tombstones: non-UTF-8 tombstone file — fail-closed")
            emit('wal', 'recovery.wal_read', 'degraded',
                 fallback_reason='invalid_wal_line', count=1)
            return None
        out = set()
        for line_number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                # ★第三轮收紧：单行坏 = 整文件不可信 = fail-closed。
                #   （旧版这里 continue 跳过坏行返回部分集合，会把藏起来的存根连人带证一起放过。）
                logger.warning("read_tombstones: malformed line %d — fail-closed (R6)", line_number)
                emit('wal', 'recovery.wal_read', 'degraded', error=exc,
                     fallback_reason='invalid_wal_line', line_number=line_number)
                return None
            # ★v3 返修（V3-R6 第二半）：解析成功【不】等于可信——还要过"身份校验"这一关。
            #   为什么把 id 校验从 `if tid:` 改成显式三连判断：旧写法对 `{"ts":100,...}` 这种
            #   "合法 JSON、没有身份"的行是**静默跳过**，集合照常返回、函数照常"成功"，
            #   调用方（重放侧）永远不知道刚刚漏了一条存根 → 被遗忘条目复活。
            #   这里【不】抛异常也不 continue：与坏 JSON 同待遇，整体 fail-closed 返回 None。
            if not isinstance(record, dict):
                logger.warning("read_tombstones: non-object line %d — fail-closed (R6)", line_number)
                emit('wal', 'recovery.wal_read', 'degraded',
                     fallback_reason='invalid_wal_line', line_number=line_number)
                return None
            tid = record.get("id")
            if not isinstance(tid, str) or not tid:
                # ⚠️【不】要把这里放宽成"有 id 就收"或"数字 id 也收"：身份不可信 = 整个文件不可信。
                logger.warning("read_tombstones: line %d has no valid string id — fail-closed (R6)", line_number)
                emit('wal', 'recovery.wal_read', 'degraded',
                     fallback_reason='invalid_wal_line', line_number=line_number)
                return None
            out.add(tid)
        return out

    def remove_tombstone(self, entry_id: str) -> bool:
        """删除某个 id 的【全部】存根行并 fsync（§6 本轮新增，供 D 路失败补偿用）。

        语义：返回 True = 删掉了 / 本来就没有；False = 入参无效 / 写失败。
        为什么需要它：`cmd_forget` 的副作用链是「存根 → WAL purge → DB 事务」，
        DB 事务失败回滚时必须把已经落下的存根**补偿删掉**，否则会出现
        "DB 里还在、WAL 里还在、却被存根永久挡住重放"的幽灵条目。
        ⚠️ 读不动（None）时**不能**当"没有"直接返回 True 了事——那等于骗调用方
          "补偿成功"，实际存根可能还在。拿不准就返回 False，让 CLI 显式报补偿失败
          （契约 §8：补偿失败要 emit failed 并在返回值里标注，【不】静默）。
        ⚠️【不】用其它路径改写整文件：走 tmp + os.replace 原子替换，坏行**原样保留**
          （无法识别为 JSON 的行不是存根，是取证物，删不得）。
        """
        if not entry_id or not isinstance(entry_id, str):
            return False
        if not self.tombstone_file.exists():
            return True                     # 本来就没有存根文件 = 无可补偿
        existing = self.read_tombstones()
        if existing is None:
            # fail-closed：读不动的存根文件不敢动刀（可能整批都是有效存根），交给上层报错
            emit('wal', 'recovery.replay', 'failed', fallback_reason='deferred_recovery')
            logger.warning("remove_tombstone: tombstone file unreadable — refuse to rewrite")
            return False
        try:
            with _locked_wal(self.wal_dir, required=True, blocking=True):
                kept = []
                try:
                    # ⚠️ 锁内重读（不是复用上面的 existing）：从读到写之间的窗口里，
                    #    别的进程可能刚追加了新存根，用旧快照写回会把那条**悄悄抹掉**。
                    with open(self.tombstone_file, "r", encoding="utf-8") as f:
                        for line in f:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            try:
                                record = json.loads(stripped)
                            except (json.JSONDecodeError, TypeError):
                                kept.append(line.rstrip("\n"))   # 坏行原样保留（可能是取证物）
                                continue
                            if isinstance(record, dict) and record.get("id") == entry_id:
                                continue                          # 命中：丢掉这一行
                            kept.append(line.rstrip("\n"))
                except UnicodeDecodeError:
                    return False                                       # 非 UTF-8：不猜，不写
                tmp = self.tombstone_file.with_suffix(".jsonl.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    for line in kept:
                        f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.tombstone_file)
        except Exception as exc:
            emit('wal', 'recovery.replay', 'failed', error=exc, fallback_reason='deferred_recovery')
            logger.warning("remove_tombstone failed for %s", entry_id, exc_info=True)
            return False
        return True
    # ── ★M01 第三轮：持久阶段回执（Spec §9.1 契约 2 + 验收 R3 补件）────────
    def record_receipt(self, operation_id, memory_id, stage, status="ok", error_type=None) -> None:
        """记一笔"某次写入推进到哪个阶段"的持久回执。**旁路**：失败只记日志，绝不抛。

        为什么是旁路（关键设计，勿改）：这是**取证面**，不是业务面。回执写不进去
        （磁盘满/权限/文件锁）不能让用户那条记忆写失败——取证动作拖垮主链是倒挂。
        所以：异常一律吞掉 + logger.debug，与 _preserve_corrupt_line 同款纪律。

        参数（单行字段封闭，多余字段【不】写，契约 §4）：
          operation_id：同一次操作贯穿各阶段（重试【不】换新 id）
          memory_id   ：本条记忆身份（派生或随机，见 derive_memory_id）
          stage       ：received|persisted|indexed|extracted|rejected|failed（= WAL_WRITE_STATUSES）
          status      ：ok|failed|rejected
          error_type  ：异常类名（【不】传异常正文/凭据，只留类名）
        ⚠️ 非法 stage/status 一律**不写**（返回 None）：回执账本是封闭枚举消费方，
           放野值进去等于污染取证链；宁可少记一笔也不记错的。
        """
        try:
            if stage not in RECEIPT_STAGES:
                logger.debug("receipt rejected: unknown stage %r", stage)
                return
            if status not in RECEIPT_STATUSES:
                logger.debug("receipt rejected: unknown status %r", status)
                return
            if self._receipt_capped:
                return  # 已到顶：停写（【不】自动删旧，人工归档）
            record = {
                "ts": time.time(),
                "operation_id": operation_id if isinstance(operation_id, str) else None,
                "memory_id": memory_id if isinstance(memory_id, str) else None,
                "stage": stage,
                "status": status,
                # error_type 缺失时写 null（字段封闭：键恒在，值可空）
                "error_type": error_type if isinstance(error_type, str) else None,
            }
            payload = json.dumps(record, ensure_ascii=False) + "\n"
            encoded_size = len(payload.encode("utf-8", "replace"))
            # 容量闸：用「现大小 + 本次大小」判，绝不先写后超限
            #（与 _preserve_corrupt_line_locked 同款算法，R5b 血泪教训）
            cur_size = self.receipt_file.stat().st_size if self.receipt_file.exists() else 0
            if cur_size + encoded_size > MAX_RECEIPT_BYTES:
                if not self._receipt_capped:
                    self._receipt_capped = True
                    emit('wal', 'write.wal', 'degraded',
                         fallback_reason='deferred_recovery', count=1)
                    logger.warning(
                        "write receipts capped: %d + %d > %d bytes — 停写回执（不自动删旧，"
                        "请人工归档 %s）", cur_size, encoded_size, MAX_RECEIPT_BYTES, self.receipt_file)
                return
            # 与 WAL 同一把互斥锁：回执虽然不是 WAL 行，但同属"这个 wal_dir 的持久状态"，
            # 多进程并写同一文件走锁最省心（append-only 小行，锁开销可忽略）。
            with _locked_wal(self.wal_dir, required=True, blocking=True):
                with open(self.receipt_file, "a", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as exc:
            # ⚠️★O01 红线（2026-10-13 合并回归实测到的本单回归）：**绝不能用 exc_info=True**。
            #   本函数会被写入路径调用；注入/真实异常正文里可能混着调用方的私密载荷与凭据，
            #   `exc_info=True` 会把整段 traceback（含 `RuntimeError(<正文+凭据>)`）打进日志
            #   ⇒ 违反 O01「日志不回显正文/凭据」。只留**异常类名**，与 O01 其余取证口径
            #   （error_type 只记类名）一致。
            #   血泪教训：合并后 205 项回归里 O01 两条因此转红，见第三轮回执 §回归修复。
            logger.debug("record_receipt failed (non-fatal): %s", type(exc).__name__)

    def read_receipts(self, operation_id=None) -> List[dict]:
        """读回执账本。operation_id=None → 全部；给了就只回该操作的行。

        ⚠️ 坏行**跳过不抛**（与 _load_corrupt_digests 同款）：回执是取证面，
          一行坏了不能把整本账读不出来——"有一部分账"远好过"账打不开"。
        ⚠️ 只回 dict，【不】回原始行文本：调用方要的是结构化字段。
        """
        out: List[dict] = []
        if not self.receipt_file.exists():
            return out
        try:
            with open(self.receipt_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    if operation_id is not None and record.get("operation_id") != operation_id:
                        continue
                    out.append(record)
        except OSError as exc:
            # ⚠️★O01（2026-10-13，B 路回执区段）：去掉 exc_info=True。
            #   本函数读的是回执账本（旁路取证面），但同一条纪律适用于**本区段所有日志**：
            #   旁路日志只记异常类名 + 结构化字段，不许把 traceback 打出来——
            #   traceback 里带什么由异常来源决定，谁也不能保证它不带调用方载荷。
            #   OSError 本身基本只含路径，但"基本"不是"保证"，安全方向统一收口。
            logger.warning("read_receipts: unreadable receipt file (%s)", type(exc).__name__)
        return out

    # ── 取证结果通道：read_all/read_uncompacted 里留证是否全部成功 ────────
    def _evidence_ok(self, raw_line, line_number: int) -> bool:
        """留证并返回是否成功。★R4 补件（验收方）：取证失败时**禁止丢弃该行**——
        否则下一次整文件重写（cleanup_expired 是 ContextBuilder 启动的常规动作）
        会把没留成证的坏行连同原文一起物理抹掉 = 证据与数据双丢。
        语义：返回 False = 这行没留成证，调用方必须放弃"丢弃该行"的重写。
        ⚠️ 摘要必须与 _preserve_corrupt_line 用**同一算法**（原始字节的 sha256[:16]）——
        旧版这里用 replace 后文本算，跟留证侧对不上时 _evidence_ok 会永远返回 False
        （重写被无脑放弃）。两边现在都走 _corrupt_digest 一个函数，杜绝再次漂移。
        ⚠️ 入参 raw_line 同样是 str | bytes 二态（见 _preserve_corrupt_line）。
        """
        try:
            self._preserve_corrupt_line(raw_line, line_number)
        except Exception:
            return False
        return self._corrupt_digest(raw_line) in (self._corrupt_seen or set())

    @staticmethod
    def _corrupt_digest(raw_line) -> str:
        """坏行指纹 = **原始字节** sha256 前 16 位（str 按 utf-8 编码，bytes 直接用）。

        ⚠️【不】要用 `encode("utf-8", "replace")` 再算：非法字节会被洗成同一个 U+FFFD，
        不同的坏行算出同一个指纹 → 去重误判 + 事后无法用指纹校验原始内容。
        留证侧/校验侧/（旧文件的）读取侧三处必须共用本函数。
        """
        raw_bytes = raw_line if isinstance(raw_line, (bytes, bytearray)) else str(raw_line).encode("utf-8")
        return hashlib.sha256(bytes(raw_bytes)).hexdigest()[:16]

    @diagnosed('wal', 'recovery.wal_read')
    def read_uncompacted(self) -> List[MemoryEntry]:
        """读取所有未压缩、未过期的条目（逐行走 _iter_wal_lines 的字节级严格解码）。

        ★R4 修复（2026-10-13 第三轮）：读取侧从「文本模式 + errors='replace'」换成
        **二进制 + 逐行严格解码**（见 _iter_wal_lines）。旧行为下非法字节被洗成 U+FFFD，
        篡改过的正文/出处（如 host_origin="origin\\xff" → "origin\\ufffd"）能照常过 JSON 解析、
        被当正常事实恢复入库，且不留一个字节的取证。
        修法：解码失败的行在迭代器里就以**原始字节**形式返回 → 这里留证 + 跳过，绝不进重放。
        【不】要退回 errors='replace'：那是把"篡改冒充正常"装回去。
        """
        entries = []
        if not self.wal_file.exists():
            return entries

        malformed = 0
        for line_number, line in self._iter_wal_lines():
            if isinstance(line, bytes):
                # 字节级坏行：严格 UTF-8 解码失败。整行原始字节交给取证（唯一能证明"哪几个字节坏"的形态）。
                malformed += 1
                self._preserve_corrupt_line(line, line_number)
                emit('wal', 'recovery.wal_read', 'degraded',
                     fallback_reason='invalid_wal_line', line_number=line_number,
                     error_type='UnicodeDecodeError')
                continue
            try:
                entry = MemoryEntry.from_json(line)
                if not entry.compressed and not entry.is_expired():
                    entries.append(entry)
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
                # R4：异常类型收口到护栏——尤其 AttributeError（顶层是 list/null 时
                # data.items() 会抛它），旧行为掀翻整次读取、合法后续行全丢且不留证。
                malformed += 1
                # 文本级坏行（如缺 id/timestamp、JSON 语法坏）：留证原文是 str。
                self._preserve_corrupt_line(line, line_number)  # M01：先留证，再计数
                emit('wal', 'recovery.wal_read', 'degraded', error=e,
                     fallback_reason='invalid_wal_line', line_number=line_number)

        if malformed:
            emit('wal', 'recovery.wal_read', 'failed', fallback_reason='invalid_wal_line', count=malformed)

        return entries

    @diagnosed('wal', 'recovery.wal_read')
    def read_all(self, *, evidence_required: bool = False) -> List[MemoryEntry]:
        """读取所有条目（包括已压缩的，用于调试 + 整文件重写的读侧）。

        ★R4 修复（2026-10-13 验收方反例）：本函数是 mark_compressed / cleanup_expired
        整文件重写的读侧。旧版刻意「跳过坏行、不写取证」，注释里说"取证交给 read_uncompacted"——
        但那只在【先跑过重放】时才成立；直接 mark_compressed（真实 ContextBuilder 启动路径
        就是先 cleanup_expired 重写再重放）会把坏行和证据一起物理抹掉。
        修法：重写前本函数同样留证。去重由 _preserve_corrupt_line 的 sha16 负责，
        重放侧再读到同一坏行时不会重复记录。

        evidence_required=True（重写路径专用）：任一坏行留证失败 → 抛 RuntimeError，
        让上层【放弃重写】。★这是验收方 R4 补件的硬闸——"取证失败应保留原数据"：
        没留成证就把行重写掉 = 证据与数据双丢。⚠️【不】要在这里吞掉这个异常。
        ⚠️【不】要删这里的留证调用——"取证只在重放侧"的假设已被验收方证伪。
        """
        entries = []
        lost = []                       # 留证失败的坏行（重写时禁止丢弃）
        if not self.wal_file.exists():
            return entries

        # 逐行走 _iter_wal_lines 的字节级严格解码（理由见 read_uncompacted 同款注释）
        for line_number, line in self._iter_wal_lines():
            if isinstance(line, bytes):
                # 字节级坏行：原始字节留证（保留"哪几个字节坏"）
                ok = self._evidence_ok(line, line_number)
                if not ok:
                    lost.append((line_number, 'UnicodeDecodeError'))
                emit('wal', 'recovery.wal_read', 'degraded',
                     fallback_reason='invalid_wal_line', line_number=line_number,
                     error_type='UnicodeDecodeError')
                continue
            try:
                entries.append(MemoryEntry.from_json(line))
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
                # R4：重写路径也必须先留证——这是防"重写吞证据"的唯一闸
                ok = self._evidence_ok(line, line_number)
                if not ok:
                    lost.append((line_number, exc))
                emit('wal', 'recovery.wal_read', 'degraded', error=exc,
                     fallback_reason='invalid_wal_line', line_number=line_number)
                continue

        if evidence_required and lost:
            raise RuntimeError(
                f"WAL rewrite aborted: {len(lost)} corrupt line(s) could not be preserved "
                f"as evidence (lines {[n for n, _ in lost[:5]]}); original data kept (R4)"
            )
        return entries

    def mark_compressed(self, entry_ids: List[str], summary_id: str) -> None:
        """标记一批条目已被压缩进某个摘要（P1-9：强锁重写，防并发吞日志）

        ★R4 补件：evidence_required=True —— 任何坏行留证失败就放弃整次重写，
        原始 WAL 原样保留（"取证失败应保留原数据"，验收 R4）。
        """
        with _locked_wal(self.wal_dir, required=WAL_LOCK_REWRITE_REQUIRED, blocking=True):
            # ★自死锁修复 + R5 线程局部：本线程已持有 WAL 锁，read_all 里的取证【绝不能】再要锁。
            # ⚠️ 必须写进【本线程】的局部状态（threading.local），不能写实例属性——
            #   写实例属性会把"别的线程"也标记成"持有锁"，那些线程的取证就跳过锁了（R5 反例）。
            self._lock_state.needs_lock = False
            try:
                all_entries = self.read_all(evidence_required=True)
            finally:
                self._lock_state.needs_lock = True
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
        """清理过期条目，返回清理数量（P1-9：强锁重写，防并发吞日志）

        ★R4 补件：坏行留证失败 → 返回 -1 表示"本次清理放弃"（原始 WAL 原样保留），
        绝不在没留成证的情况下把坏行重写掉。调用方（重放）只认 >0 为清理数。
        """
        with _locked_wal(self.wal_dir, required=WAL_LOCK_REWRITE_REQUIRED, blocking=True):
            # ★自死锁修复 + R5 线程局部：与 mark_compressed 同理——锁内取证不能再要锁，
            # 且标志只能落在本线程（见 __init__ 里 _lock_state 的注释）。
            self._lock_state.needs_lock = False
            try:
                all_entries = self.read_all(evidence_required=True)
            except RuntimeError as exc:
                logger.warning("cleanup_expired aborted: %s", exc)
                emit('wal', 'recovery.cleanup', 'degraded', error=exc,
                     fallback_reason='deferred_recovery')
                return -1
            finally:
                self._lock_state.needs_lock = True
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

        # ★R4：与 read_uncompacted 共用 _iter_wal_lines（字节级严格解码）——
        #   旧版这里用的是文本模式 `open(..., "r")`（平台默认编码）+ 只捕 JSON/TypeError，
        #   两处读取语义不一致：非法字节行在本函数里可能直接抛 UnicodeDecodeError 掀翻整表。
        #   现在三条读取路径（read_uncompacted/read_all/get_delta）走同一个迭代器，判据只有一份。
        for line_number, line in self._iter_wal_lines():
            if isinstance(line, bytes):
                self._preserve_corrupt_line(line, line_number)   # 字节坏行：留证原始字节后跳过
                continue
            try:
                entry = MemoryEntry.from_json(line)
                if entry.timestamp > since_ts and not entry.is_expired():
                    results.append(entry)
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
                logger.warning("Skipping malformed WAL line in get_delta: %s", e)
                self._preserve_corrupt_line(line, line_number)   # 文本坏行：同样留证，别只记日志

        logger.debug("get_delta(since_ts=%.3f): found %d entries", since_ts, len(results))
        return results

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（中英混合）"""
        return estimate_tokens(text)
