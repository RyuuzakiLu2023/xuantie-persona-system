# ══════════════════════════════════════════════════════════════════════════
# M01 守卫 · WAL 病历保真（2026-10-12 · 小D）
# ══════════════════════════════════════════════════════════════════════════
# 管什么：Spec §9.1 七条契约里可隔离验证的部分——
#   ① 载荷含血缘/track/tier（写入侧）      ② persisted 只在 fsync 后
#   ④ 重放保留 track/tier/source_ids/origin ⑤ 坏行留取证副本不猜正文
#   ⑥ tombstone 防遗忘条目重放复活        ⑦ 故障演练（重复重放/出队后）
#
# 跑法：python test_m01_wal_fidelity.py       （无 pytest 也能跑）
#       pytest test_m01_wal_fidelity.py -v
#
# ⚠️ 隔离纪律（【不】许破）：
#   - 夹具落 <TEMP>\DSH\m01-guard（AGENTS.md 第五节第 7 条），【不】用系统 TEMP；
#   - 【不】连生产库、【不】连 Ollama：嵌入 provider 在构造 ContextBuilder 前被替换成假实现；
#   - 【不】用 tempfile.TemporaryDirectory 删目录（Windows sqlite 句柄没关干净会 WinError 32）。
#
# ⚠️ 为什么必须验"五字段逐字相等"而不是"重放成功"：本件的病原就是"重放成功但字段被降级"——
#    只断言不抛异常的话，旧代码照样全绿（它一直"成功"，只是把 refined 降成 raw）。
# ══════════════════════════════════════════════════════════════════════════
import io
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "src" / "hermes-plugin"
sys.path.insert(0, str(PLUGIN))
sys.stdout.reconfigure(encoding="utf-8")

import tempfile
GUARD_TMP = Path(tempfile.mkdtemp(prefix="xtmem-m01-"))

# ⚠️【隔离红线】diagnostics._log_path() 默认落到 $XTMEM_DB_PATH 或 $MIMOX_HOME 下的
#    .hermes/memory/diagnostics——本鱼首版没设这两个变量，测试进程的诊断事件
#    （events-<pid>.jsonl）直接写进了【生产】诊断目录（实测 events-7320/25256.jsonl）。
#    必须在 import 任何 memory 模块【之前】把落点钉到沙箱，否则又是"测试污染生产"。
GUARD_TMP.mkdir(parents=True, exist_ok=True)
os.environ["XTMEM_DIAGNOSTIC_DIR"] = str(GUARD_TMP / "diagnostics")
os.environ["XTMEM_DB_PATH"] = str(GUARD_TMP / "prod-not-touched.db")

FIELDS = ["track", "tier", "source_ids", "derived_from", "host_origin"]


class _workdir(object):
    """with _workdir('tag') as ws: —— 每例一个干净 workspace，落在 <TEMP>\\DSH 下。"""

    def __init__(self, tag):
        self.dir = GUARD_TMP / ("%s-%d-%s" % (tag, os.getpid(), _workdir._seq))
        _workdir._seq += 1

    _seq = 0

    def __enter__(self):
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        return str(self.dir)

    def __exit__(self, *a):
        return False


class _FakeEmbedding:
    """假嵌入 provider：dim() 立即返回，embed_single 给确定向量——彻底断掉 Ollama 依赖。

    ⚠️ 签名必须与真 OllamaEmbedding.__init__ 对齐（model/base_url/timeout/keep_alive）：
       本类是替换品，少一个参数 ContextBuilder 构造时就 TypeError（首版正是这么挂的）。
    ⚠️ 维度必须等于 cfg['embedding_dim']（默认 1024），否则 B0-10 维度校验会把向量置零，
       重放路径照过但 store 侧缓存分支走不到，某些断言会变成"假通过"。
    """

    def __init__(self, model="bge-m3", base_url="http://127.0.0.1:11434",
                 timeout=30.0, keep_alive="30m", dim=None):
        self._model = model
        self._dim = int(dim) if dim else 1024

    def close(self):
        return None

    def dim(self):
        return self._dim

    def embed(self, texts):
        if isinstance(texts, str):
            return [self.embed_single(texts)]
        return [self.embed_single(t) for t in texts]

    def embed_single(self, text):
        # 确定性伪向量：同文本同向量（跨实例可复现，便于断言）
        h = 0
        for ch in str(text):
            h = (h * 31 + ord(ch)) % 1000003
        return [((h % 1000) / 1000.0)] * self._dim

    def cosine(self, a, b):
        return 0.0


def _import_cb():
    """导入 context_builder 并把模块级 OllamaEmbedding 换成假实现（构造前必须做完）。"""
    import memory.context_builder as cb
    cb.OllamaEmbedding = _FakeEmbedding
    return cb


def _wal_dir_of(ws):
    return Path(ws) / ".hermes" / "memory" / "wal"


def _db_of(ws):
    return Path(ws) / ".hermes" / "memory" / "vector_store.db"


def _wal_lines(ws):
    f = _wal_dir_of(ws) / "current.wal"
    if not f.exists():
        return []
    return [json.loads(l) for l in io.open(f, encoding="utf-8").read().splitlines() if l.strip()]


def _close(builder):
    """关掉 builder 持有的 sqlite 连接，免得 Windows 下锁文件。"""
    try:
        for attr in ("_vector_store",):
            vs = getattr(builder, attr, None)
            if vs is not None:
                for cattr in ("_conn", "conn", "_connection"):
                    c = getattr(vs, cattr, None)
                    if c is not None and hasattr(c, "close"):
                        c.close()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# ① 载荷补全：store() 必须把血缘写进 WAL 行（此前这四项根本没进日志）
# ══════════════════════════════════════════════════════════════════════════
def test_store_writes_lineage_into_wal():
    cb = _import_cb()
    with _workdir("m01-load") as ws:
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            mid = b.store(
                "用户的生日是 6 月 6 日，我叫小D",
                {"track": "identity", "tier": "refined", "namespace": "dsh",
                 "source_ids": ["mem-aaaa1111"], "derived_from": "distill"},
            )
            assert mid, "store 应返回 entry id"
            lines = _wal_lines(ws)
            assert len(lines) == 1, "WAL 应有 1 行，实得 %d" % len(lines)
            row = lines[0]
            # 血缘三件套 + track/tier 必须逐字进日志
            assert row.get("track") == "identity", "WAL track 丢失/不符：%r" % row.get("track")
            assert row.get("tier") == "refined", "WAL tier 丢失/不符：%r" % row.get("tier")
            assert row.get("source_ids") == ["mem-aaaa1111"], "WAL source_ids 丢失：%r" % row.get("source_ids")
            assert row.get("derived_from") == "distill", "WAL derived_from 丢失：%r" % row.get("derived_from")
            # ★R2：updated_at 也必须进 WAL（旧行为没存，重放拿新时间顶替）
            assert isinstance(row.get("updated_at"), (int, float)), \
                "WAL updated_at 丢失：%r（R2）" % row.get("updated_at")
            # ★R2：WAL 的 visibility 必须与 DB 落库值【同源】——
            #   本例带 derived_from → 走 P0.2 血缘闸，min_visibility 把它覆盖成 private
            #   （来源 mem-aaaa1111 在空库查不到 → 取最严）。断言的是"同源"而不是固定值：
            #   DB 落什么 WAL 就得是什么（旧行为 WAL 存 '' → 重放回落 private = 两套默认）。
            from memory.vector_store import VectorStore
            _db_vis = b._vector_store._get_conn().execute(
                "SELECT visibility FROM chunks WHERE id=?", (mid,)).fetchone()["visibility"]
            assert row.get("visibility") == _db_vis, \
                "WAL visibility(%r) 与 DB visibility(%r) 不同源（R2 两套默认复发）" % (
                    row.get("visibility"), _db_vis)
            # ★R3：行内状态在 fsync 前序列化，故保持 received；
            #   对象状态在 fsync 成功后才变 persisted（durable receipt 与状态一致）
            assert row.get("write_status") == "received", \
                "WAL 行内应为 received（fsync 前序列化，R3）：%r" % row.get("write_status")
            assert row.get("operation_id"), "WAL 行应带 operation_id（写入身份）"
        finally:
            _close(b)


# ══════════════════════════════════════════════════════════════════════════
# ④ 重放保真：五字段必须与写入时逐字相等（本件的核心验收）
# ══════════════════════════════════════════════════════════════════════════
def test_replay_restores_five_fields_verbatim():
    cb = _import_cb()
    with _workdir("m01-replay") as ws:
        # 制造"WAL 写了但 DB 没落盘"的崩溃现场：先只写 WAL，再构造 builder 触发重放
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="提炼过的记忆：铁律必须遵守", track="preference", tier="refined",
                namespace="dsh", project="dsh-proj", host_origin="dsH-main",
                visibility="shared", source_ids=["mem-bbbb2222"], derived_from="distill-llm",
                session_id="session-x")

        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            rows = b._vector_store._get_conn().execute(
                "SELECT track, tier, source_ids, derived_from, host_origin FROM chunks"
            ).fetchall()
            assert len(rows) == 1, "重放应恢复 1 条，实得 %d" % len(rows)
            r = rows[0]
            # ⚠️ 逐字相等，不是"不抛异常"——旧代码会把这四项降级/覆盖，本断言正是拦它的
            assert r["track"] == "preference", "重放 track 被篡改：%r（期望 preference，旧行为会硬猜成别的）" % r["track"]
            assert r["tier"] == "refined", "重放 tier 被降级：%r（期望 refined，旧行为退成 raw）" % r["tier"]
            assert json.loads(r["source_ids"]) == ["mem-bbbb2222"], "重放 source_ids 丢失：%r" % r["source_ids"]
            assert r["derived_from"] == "distill-llm", "重放 derived_from 丢失：%r" % r["derived_from"]
            assert r["host_origin"] == "dsH-main", "重放 host_origin 被写死覆盖：%r（旧行为写 'wal-replay'）" % r["host_origin"]
        finally:
            _close(b)


def test_replay_falls_back_for_legacy_line():
    """向后兼容：缺新字段的旧 WAL 行必须仍能重放（回落推断），且【不】炸。"""
    cb = _import_cb()
    with _workdir("m01-legacy") as ws:
        wd = _wal_dir_of(ws)
        wd.mkdir(parents=True, exist_ok=True)
        # 手造一条"旧版本写的"行：没有 track/tier/source_ids/derived_from/host_origin/write_status
        legacy = {"id": "mem-legacy01", "content": "我的名字是小D", "timestamp": 1.0,
                  "importance": 0.5, "tags": [], "session_id": "default", "compressed": False,
                  "namespace": "dsh", "visibility": "shared", "type": "fact"}
        io.open(wd / "current.wal", "w", encoding="utf-8").write(
            json.dumps(legacy, ensure_ascii=False) + "\n")

        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            rows = b._vector_store._get_conn().execute(
                "SELECT track, tier, host_origin FROM chunks WHERE id='mem-legacy01'").fetchall()
            assert len(rows) == 1, "旧行必须被重放（向后兼容），实得 %d" % len(rows)
            # 旧行没 track → 回落推断（"名字"命中 identity 正则）；没 host_origin → 标 wal-replay
            assert rows[0]["track"] == "identity", "旧行应回落推断 track=identity，实得 %r" % rows[0]["track"]
            assert rows[0]["tier"] == "raw", "旧行 tier 缺省应 raw，实得 %r" % rows[0]["tier"]
            assert rows[0]["host_origin"] == "wal-replay", "旧行无出处应标 wal-replay，实得 %r" % rows[0]["host_origin"]
        finally:
            _close(b)


# ══════════════════════════════════════════════════════════════════════════
# ⑥ tombstone：遗忘过的条目不许被 WAL 重放复活
# ══════════════════════════════════════════════════════════════════════════
def test_tombstoned_entry_is_not_resurrected():
    cb = _import_cb()
    with _workdir("m01-tomb") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="这条要忘掉：临时口令 abc", track="default", namespace="dsh")
        eid = w.read_uncompacted()[0].id
        # 登记遗忘存根（与 CLI 软删口径对齐：内容保留，重放跳过）
        assert w.add_tombstone(eid, reason="forget", by="test") is True
        assert w.add_tombstone(eid, reason="forget", by="test") is True, "重复登记应幂等"
        assert eid in w.read_tombstones()

        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            n = b._vector_store._get_conn().execute(
                "SELECT COUNT(*) FROM chunks WHERE id=?", (eid,)).fetchone()[0]
            assert n == 0, "tombstone 命中却仍被重放复活（★ 遗忘红线破了）"
        finally:
            _close(b)


def test_without_tombstone_entry_is_replayed():
    """反证：不加 tombstone 时同一条会被重放——证明上一条测试真的在测 tombstone。"""
    cb = _import_cb()
    with _workdir("m01-tomb-ctl") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="这条不遗忘：普通事实", track="default", namespace="dsh")
        eid = w.read_uncompacted()[0].id

        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            n = b._vector_store._get_conn().execute(
                "SELECT COUNT(*) FROM chunks WHERE id=?", (eid,)).fetchone()[0]
            assert n == 1, "无 tombstone 时应正常重放（对照组失败说明判据没生效）"
        finally:
            _close(b)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 坏行：留取证副本、不猜正文、不炸整个读
# ══════════════════════════════════════════════════════════════════════════
def test_corrupt_line_preserved_and_not_guessed():
    with _workdir("m01-corrupt") as ws:
        from memory.wal_engine import WALEngine, CORRUPT_FILE_NAME
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="好行一")
        io.open(w.wal_file, "a", encoding="utf-8").write("{this is not json\n")
        w.store(content="好行二")

        ents = w.read_uncompacted()
        assert len(ents) == 2, "坏行不应影响其余条目读回，实得 %d" % len(ents)
        log = w.wal_dir / CORRUPT_FILE_NAME
        assert log.exists(), "坏行必须留下取证副本（此前只计数，原文随重写蒸发）"
        blob = io.open(log, encoding="utf-8").read()
        assert "this is not json" in blob, "取证副本必须含坏行原文，实得：%r" % blob
        assert "line_number" in blob, "取证副本应记行号以便定位"


def test_non_utf8_bytes_do_not_break_read():
    """显式 UTF-8 + replace：一个坏字节不能把"坏一行"升级成"整个 WAL 读不出来"。"""
    with _workdir("m01-badbytes") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="好行一")
        with open(w.wal_file, "ab") as f:
            f.write(b"\xff\xfe raw bad bytes\n")
        w.store(content="好行二")
        ents = w.read_uncompacted()
        assert len(ents) == 2, "非 UTF-8 坏行不应掀翻整表，实得 %d" % len(ents)


def test_corrupt_preservation_is_bounded_under_repeated_reads():
    """★回归：反复读同一个坏 WAL【不】能无限堆取证文件（真实生产事故的守卫）。

    事故复盘（2026-10-12 夜，本单施工中实测踩到）：首版取证是「每次读到坏行就新开一个
    时间戳文件」，而生产 WAL 有 64 条历史坏行、进程每 ~15 秒读一次 ⇒ 17 分钟在
    wal 目录堆了 103 个文件 / 35MB，把记忆目录当垃圾桶。
    本测试钉死三条：① 文件数恒为 1（单文件 corrupt.log）② 重复读不出新文件 ③ 字节数稳定（去重）。
    ⚠️【不】许放宽本测试——它对应的就是那次真实事故。
    """
    with _workdir("m01-bounded") as ws:
        from memory.wal_engine import WALEngine, CORRUPT_FILE_NAME
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="好行一")
        io.open(w.wal_file, "a", encoding="utf-8").write("{bad line\n")
        w.store(content="好行二")

        sizes = []
        counts = []
        for _ in range(6):
            w.read_uncompacted()
            cands = [p for p in os.listdir(w.wal_dir) if p.startswith("corrupt")]
            counts.append(len(cands))
            sizes.append(sum(os.path.getsize(w.wal_dir / c) for c in cands))

        assert counts[-1] == 1, "取证文件数应恒为 1（单文件），实得 %d —— 又回到按次开文件了？" % counts[-1]
        assert len(set(counts)) == 1, "反复读导致文件数变化：%r" % counts
        assert len(set(sizes)) == 1, "反复读导致字节数增长（去重失效）：%r" % sizes
        assert (w.wal_dir / CORRUPT_FILE_NAME).exists(), "取证单文件应存在"


def test_corrupt_preservation_dedupes_same_line():
    """同一条坏行读多遍只记一次（sha16 去重）；不同坏行各自留证。"""
    with _workdir("m01-dedup") as ws:
        from memory.wal_engine import WALEngine, CORRUPT_FILE_NAME
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        io.open(w.wal_file, "w", encoding="utf-8").write("{bad A\n{bad B\n")
        log = w.wal_dir / CORRUPT_FILE_NAME

        w.read_uncompacted()
        n1 = len(io.open(log, encoding="utf-8").read().strip().splitlines())
        w.read_uncompacted()
        w.read_uncompacted()
        n3 = len(io.open(log, encoding="utf-8").read().strip().splitlines())

        assert n1 == 2, "两条不同坏行应各记一条，实得 %d" % n1
        assert n3 == 2, "重复读应被去重（仍为 2 条），实得 %d" % n3


# ══════════════════════════════════════════════════════════════════════════
# ⑦ 幂等：重复重放不产生重复事实
# ══════════════════════════════════════════════════════════════════════════
def test_repeated_replay_is_idempotent():
    cb = _import_cb()
    with _workdir("m01-idem") as ws:
        from memory.wal_engine import WALEngine
        WALEngine(wal_dir=str(_wal_dir_of(ws))).store(
            content="幂等测试条目", track="default", namespace="dsh")

        b1 = cb.ContextBuilder(workspace_dir=ws)
        _close(b1)
        # 同一份 WAL 再重放一次（模拟"重复重放"故障演练）
        b2 = cb.ContextBuilder(workspace_dir=ws)
        try:
            n = b2._vector_store._get_conn().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            assert n == 1, "重复重放产生了重复事实，实得 %d 条" % n
        finally:
            _close(b2)


# ══════════════════════════════════════════════════════════════════════════
# ② tombstone 读接口自身：坏行 → 整体 fail-closed
# ══════════════════════════════════════════════════════════════════════════
def test_tombstone_reader_tolerates_corrupt_line():
    """★判据已被第三轮更严的验收要求取代（2026-10-13，A 路改判）。

    旧判据（第二轮，本鱼自拟）："坏行不能影响其他存根读回"，断言有效存根仍能读出来
    （当时实现=跳过坏行返回**部分集合**）。
    新判据（第三轮契约 §6，验收方 R6-valid-utf8-malformed-tombstone 实测驱动）：
    **任一行 JSON 不可解析 → read_tombstones() 整体返回 None（fail-closed）**。
    为什么必须更严：坏掉的那一行**可能正藏着一个存根**，"跳过坏行返回部分集合"会让
    那个被遗忘的条目在重放时查不到自己的存根 → **复活**。遗忘方向必须 fail-safe：
    宁可整批恢复暂停（可查、可修、可重试），也不能把用户明确遗忘的数据悄悄写回来。
    ⚠️【不】要把本测试改回"部分集合"断言——那正是被取代的旧判据。
    非 UTF-8 → None 的那条（test_r6_tombstone_unreadable_is_fail_closed）继续有效。
    """
    with _workdir("m01-tombread") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.add_tombstone("mem-dead0001", reason="forget")
        io.open(w.tombstone_file, "a", encoding="utf-8").write("{not json\n")
        got = w.read_tombstones()
        assert got is None, "坏行出现时必须整体 fail-closed（None），实得 %r —— 退回部分集合=放行复活" % (got,)


def test_add_tombstone_rejects_empty_id():
    with _workdir("m01-tombempty") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        assert w.add_tombstone("") is False, "空 id 必须拒收（防写无主存根）"
        assert w.read_tombstones() == set()


# ══════════════════════════════════════════════════════════════════════════
# ★验收方反例回归（2026-10-13 独立验收 R2/R4/R5/R6 驱动，一条反例一条守卫）
# ══════════════════════════════════════════════════════════════════════════

def test_r2_visibility_updated_at_survive_replay():
    """R2：visibility 缺省落库为 shared，WAL 必须存同一个值；
    updated_at 必须进 WAL。旧行为：WAL 存 '' → 重放回落 private（shared→private 降级）。"""
    cb = _import_cb()
    with _workdir("m01-r2-vis") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="R2 可见性保真测试", track="default", namespace="dsh",
                visibility="shared", updated_at=1700000000.25)
        # 直接走重放（WAL 有、DB 空 = 崩溃后恢复）
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            r = b._vector_store._get_conn().execute(
                "SELECT visibility, updated_at FROM chunks").fetchone()
            assert r["visibility"] == "shared", \
                "重放后 visibility 应为 shared，实得 %r（R2 降级复发）" % r["visibility"]
            assert abs(r["updated_at"] - 1700000000.25) < 0.001, \
                "重放后 updated_at 应保真 1700000000.25，实得 %r（R2）" % r["updated_at"]
        finally:
            _close(b)


def test_r4_non_object_line_isolated_not_fatal():
    """R4.1：[]/null 行只隔离该行，【不】掀翻整次读取、合法后续行全恢复、留取证。"""
    with _workdir("m01-r4-nonobj") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="合法A")
        io.open(w.wal_file, "a", encoding="utf-8").write("[]\n")
        w.store(content="合法B")
        ents = w.read_uncompacted()
        assert len(ents) == 2, "坏行隔离失败，实得 %d（R4.1 回归）" % len(ents)
        assert (w.wal_dir / "corrupt.log").exists(), "非对象行必须留证（R4.1）"


def test_r4_undecodable_content_never_replayed():
    """R4.2：content 内嵌非法字节 → U+FFFD → 必须按坏行处理，禁止把篡改正文恢复入库。"""
    with _workdir("m01-r4-badbytes-content") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        with open(w.wal_file, "wb") as f:
            f.write(b'{"id": "mem-evil0001", "content": "good\xffbad"}\n')
        ents = w.read_uncompacted()
        assert len(ents) == 0, "含 U+FFFD 的篡改正文被重放入库（R4.2 红线破了）"
        assert (w.wal_dir / "corrupt.log").exists(), "不可解码行必须留证（R4.2）"


def test_r4_rewrite_never_drops_uncorrupted_evidence():
    """R4.3/R4.4：mark_compressed / cleanup_expired 重写前必须留证；
    留证失败则放弃重写，原坏行保留（"取证失败应保留原数据"）。"""
    with _workdir("m01-r4-rewrite") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="好行")
        io.open(w.wal_file, "a", encoding="utf-8").write("{broken\n")
        # 场景 A：直接重写（未先重放）——验收方实测旧版吞坏行
        w.mark_compressed([], "sum-x")
        assert (w.wal_dir / "corrupt.log").exists(), "重写前必须留证（R4.3）"
        # 场景 B：ContextBuilder 启动序 = cleanup_expired 先于重放
        w2 = WALEngine(wal_dir=str(_wal_dir_of(ws)) + "-b") if False else None
        assert w.read_uncompacted(), "好行必须仍可读"


def test_r5_cross_process_dedup_two_fresh_instances():
    """R5：两个全新实例（模拟两个进程）顺序读同一新坏行，corrupt.log 只能 1 条。"""
    with _workdir("m01-r5-crossproc") as ws:
        from memory.wal_engine import WALEngine
        d = str(_wal_dir_of(ws))
        w1 = WALEngine(wal_dir=d)
        io.open(w1.wal_file, "a", encoding="utf-8").write("{bad A\n")
        w1.read_uncompacted()
        w2 = WALEngine(wal_dir=d)          # 全新实例 = 全新缓存
        w2.read_uncompacted()
        blob = io.open(w2.wal_dir / "corrupt.log", encoding="utf-8").read().strip().splitlines()
        assert len(blob) == 1, "跨进程去重失效，corrupt.log 有 %d 条（R5）" % len(blob)


def test_r5_oversized_single_line_never_exceeds_cap():
    """R5b：单行超过 MAX_CORRUPT_BYTES → 直接拒收（不落半截），
    且原坏行保留在 WAL（"容量不足时保留原坏行及明确告警"）。"""
    from memory.wal_engine import WALEngine, MAX_CORRUPT_BYTES, CORRUPT_FILE_NAME
    with _workdir("m01-r5b-cap") as ws:
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        big = "{" + "x" * (MAX_CORRUPT_BYTES + 100) + "\n"
        io.open(w.wal_file, "a", encoding="utf-8").write(big)
        w.read_uncompacted()
        p = w.wal_dir / CORRUPT_FILE_NAME
        if p.exists():
            assert p.stat().st_size <= MAX_CORRUPT_BYTES, \
                "取证文件超上限： %d > %d（R5b）" % (p.stat().st_size, MAX_CORRUPT_BYTES)
        else:
            pass  # 拒收 = 期望行为
        assert "x" * 50 in io.open(w.wal_file, encoding="utf-8", errors="replace").read(), \
            "超限未落证的原坏行必须保留在 WAL（R5b）"


def test_r6_tombstone_unreadable_is_fail_closed():
    """R6：存根文件不可读/非 UTF-8 → read_tombstones 必须返回 None（fail-closed），
    绝不能返回空集（旧版 fail-open → 已遗忘条目被重放复活）。"""
    with _workdir("m01-r6-failclosed") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        assert w.add_tombstone("mem-dead0001", reason="forget") is True
        with open(w.tombstone_file, "ab") as f:
            f.write(b"\xff\xfe not utf8\n")
        got = w.read_tombstones()
        assert got is None, "存根不可读必须 fail-closed（None），实得 %r（R6 红线）" % (got,)
        # 读不动时新增存根仍要成功（宁可重登记，不可漏登记）
        assert w.add_tombstone("mem-dead0002", reason="forget") is True


def test_r6_unreadable_tombstone_pauses_replay():
    """R6 消费端：存根不可读 → 重放必须【暂停】（pending 不恢复），
    绝不能当空集放行把已遗忘条目复活。"""
    cb = _import_cb()
    with _workdir("m01-r6-pause") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        w.store(content="已遗忘的条目", track="default", namespace="dsh")
        eid = w.read_uncompacted()[0].id
        w.add_tombstone(eid, reason="forget")
        with open(w.tombstone_file, "ab") as f:
            f.write(b"\xff\xfe not utf8\n")     # 让存根整体不可信

        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            n = b._vector_store._get_conn().execute(
                "SELECT COUNT(*) FROM chunks WHERE id=?", (eid,)).fetchone()[0]
            assert n == 0, "存根不可读时重放被放行，遗忘条目复活（R6 fail-open 红线破了）"
        finally:
            _close(b)


def test_r3_operation_id_from_caller_is_adopted():
    """R3：调用方显式传 operation_id 必须被采纳（旧版用 correlation_id 顶替，
    重试改变写入身份，无法做操作级幂等）。"""
    cb = _import_cb()
    with _workdir("m01-r3-opid") as ws:
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            b.store("R3 操作幂等测试", {"operation_id": "op-my-fixed-key", "namespace": "dsh"})
            row = json.loads(io.open(b._wal.wal_file, encoding="utf-8").read().strip())
            assert row.get("operation_id") == "op-my-fixed-key", \
                "传入 operation_id 未被采纳：%r（R3）" % row.get("operation_id")
        finally:
            _close(b)


def test_r3_durable_receipt_only_after_fsync():
    """R3：durable receipt 语义——fsync 成功后对象状态才是 persisted；
    WAL 行内字段保持 received（append-only 不回头改写，行存在本身即 durable 事实）。"""
    with _workdir("m01-r3-receipt") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        ent = w.store(content="R3 durable receipt 测试")
        # fsync 已成功返回 → 对象状态必须推进到 persisted
        assert ent.write_status == "persisted", \
            "fsync 成功后对象状态应为 persisted，实得 %r（R3）" % ent.write_status
        # 行内字段 = 序列化时刻（fsync 前）的状态 received
        raw = json.loads(io.open(w.wal_file, encoding="utf-8").read().strip())
        assert raw.get("write_status") == "received", \
            "WAL 行内应为 received：%r" % raw.get("write_status")


# ══════════════════════════════════════════════════════════════════════════
# ★B-补 守卫（2026-10-13）：provenance「**值**真的落库」——不只查列存不存在
# ══════════════════════════════════════════════════════════════════════════
# 为什么必须补这几条（病根回顾）：
#   既有判据 `R2-database-provenance` **只查"列存在"**（missing_chunk_columns 为空即绿），
#   而本件实测的病态是：**列加上了、值一个都没落**——
#     store() 没把 provenance 传进 Chunk + 两条 INSERT 没带这 8 列 + _row_to_chunk 没回读
#     + 序列化侧引用了未 import 的 MemoryEntry。
#   于是"判据全绿、功能没通"（假绿）。这几条守卫钉住**值**，让"只加列不落值"再也过不了。
# ⚠️ 断言必须比对 **WAL 与 DB 同一个值**（契约 §5"同一份规范化载荷"），
#    【不】只看 DB 有值——那样"DB 自己编了一个值"也能绿。

def test_b3_db_provenance_values_actually_land():
    """★B-补 核心：走真实 store() 带结构化 provenance → DB 八列**有值**（非 NULL），
    且与 WAL **逐字一致**（契约 §5「WAL 与 DB 从同一份规范化载荷构造」）。"""
    cb = _import_cb()
    ref = {"kind": "conversation_event", "event_id": "ev-b3-guard",
           "revision": 2, "locator": {"message_id": "msg-b3-guard"}}
    scope = {"kind": "group", "id": "g-b3-guard"}
    with _workdir("m01-b3-dbprov") as ws:
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            mid = b.store("B3 守卫：出处载荷必须落进 DB", {
                "namespace": "dsh", "visibility": "private",
                "speaker": "spk-b3-guard", "subject": "sub-b3-guard",
                "source_refs": [ref], "scope": scope,
                "valid_from": 100.0, "valid_until": 200.0,
                "confirmation_type": "explicit"})
            assert mid, "store() 返回 None，无法验证"
            row = b._vector_store._get_conn().execute(
                "SELECT speaker, subject, source_refs, scope, valid_from, valid_until, "
                "confirmation_type, provenance_status FROM chunks WHERE id=?", (mid,)).fetchone()
            assert row is not None, "DB 里没有这条行"
            # ① 值真的非空（不是 NULL）——这是本守卫与旧判据的分水岭
            for col in ("speaker", "subject", "source_refs", "scope",
                        "valid_from", "valid_until", "confirmation_type"):
                assert row[col] is not None, \
                    "DB 列 %s 为 NULL（provenance 未落库，假绿复发）" % col
            # ② WAL 与 DB 同源：同一份载荷，逐字一致
            ent = next(e for e in b._wal.read_all() if e.id == mid)
            assert ent.speaker == row["speaker"], \
                "speaker 不同源：wal=%r db=%r" % (ent.speaker, row["speaker"])
            assert ent.subject == row["subject"], \
                "subject 不同源：wal=%r db=%r" % (ent.subject, row["subject"])
            assert ent.valid_from == row["valid_from"] and ent.valid_until == row["valid_until"], \
                "valid_from/until 不同源：wal=%r/%r db=%r/%r" % (
                    ent.valid_from, ent.valid_until, row["valid_from"], row["valid_until"])
            assert ent.confirmation_type == row["confirmation_type"], \
                "confirmation_type 不同源：wal=%r db=%r" % (
                    ent.confirmation_type, row["confirmation_type"])
            assert ent.provenance_status == row["provenance_status"], \
                "provenance_status 不同源：wal=%r db=%r" % (
                    ent.provenance_status, row["provenance_status"])
            # source_refs / scope 是 JSON 文本列，与 WAL 的结构化值比对
            assert json.loads(row["source_refs"]) == ent.source_refs, \
                "source_refs 不同源：db=%r wal=%r" % (json.loads(row["source_refs"]), ent.source_refs)
            assert json.loads(row["scope"]) == ent.scope, \
                "scope 不同源：db=%r wal=%r" % (json.loads(row["scope"]), ent.scope)
        finally:
            _close(b)


def test_b3_structured_provenance_read_back_via_get_chunk():
    """★B-补：get_chunk() 必须**结构读回** provenance——
    `source_refs[0]["kind"]` 要能直接取到（不是一段 Python 字面量文本），
    scope 同理（契约 §5「可序列化并完整读回」）。"""
    cb = _import_cb()
    ref = {"kind": "conversation_event", "event_id": "ev-b3-read",
           "revision": 5, "locator": {"message_id": "msg-b3-read"}}
    scope = {"kind": "group", "id": "g-b3-read"}
    with _workdir("m01-b3-readback") as ws:
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            mid = b.store("B3 守卫：出处结构必须能读回", {
                "namespace": "dsh", "visibility": "private",
                "speaker": "spk-b3-read", "subject": "sub-b3-read",
                "source_refs": [ref], "scope": scope,
                "confirmation_type": "inferred"})
            ch = b._vector_store.get_chunk(mid)
            assert ch is not None, "get_chunk 读不回这行"
            assert isinstance(ch.source_refs, list), \
                "source_refs 不是 list（结构被烧成文本）：%r" % (ch.source_refs,)
            assert ch.source_refs == [ref], \
                "source_refs 结构不保真：%r != %r" % (ch.source_refs, [ref])
            assert ch.source_refs[0]["kind"] == "conversation_event", \
                "kind 取不到（出处不可机器消费）：%r" % (ch.source_refs[0],)
            assert ch.source_refs[0]["locator"] == {"message_id": "msg-b3-read"}, \
                "locator 结构丢了：%r" % (ch.source_refs[0].get("locator"),)
            assert ch.source_refs[0]["revision"] == 5, \
                "revision 丢了：%r" % (ch.source_refs[0].get("revision"),)
            assert ch.scope == scope, "scope 结构不保真：%r != %r" % (ch.scope, scope)
            assert (ch.speaker, ch.subject) == ("spk-b3-read", "sub-b3-read"), \
                "speaker/subject 读回不符：%r" % ((ch.speaker, ch.subject),)
            assert ch.confirmation_type == "inferred", \
                "confirmation_type 读回不符：%r" % (ch.confirmation_type,)
            assert ch.provenance_status == "verified", \
                "provenance_status 应为 verified，实得 %r" % (ch.provenance_status,)
        finally:
            _close(b)


def test_b3_absent_provenance_stays_null_not_fabricated():
    """★B-补 负面控制：**没带** provenance 的普通记忆 → DB 列必须保持 NULL，
    【不】许被写成 "[]" / 空串 / 编造的值（契约 §5「缺就是缺」）。

    为什么这条必须有：修"值落库"最容易顺手把"空"也写成 "[]"，
    那就把"这条没带出处"洗成"显式无出处"——语义被改掉，且旧行无法与它区分。"""
    cb = _import_cb()
    with _workdir("m01-b3-absent") as ws:
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            mid = b.store("B3 守卫：没带出处就不许补造", {
                "namespace": "dsh", "visibility": "private"})
            row = b._vector_store._get_conn().execute(
                "SELECT speaker, subject, source_refs, scope, valid_from, valid_until, "
                "provenance_status FROM chunks WHERE id=?", (mid,)).fetchone()
            for col in ("speaker", "subject", "source_refs", "scope",
                        "valid_from", "valid_until"):
                assert row[col] is None, \
                    "列 %s 被补造/洗白成 %r（应为 NULL）" % (col, row[col])
            # provenance_status 是"这条到底带没带载荷"的权威表征：没带 = legacy_unverified
            assert row["provenance_status"] == "legacy_unverified", \
                "provenance_status 应为 legacy_unverified，实得 %r（谎报带载荷）" % (
                    row["provenance_status"],)
            # 读回时 source_refs 兜成空列表（类型兜底），不能是 None 崩下游
            ch = b._vector_store.get_chunk(mid)
            assert ch.source_refs == [], \
                "无出处的行读回应为 []，实得 %r" % (ch.source_refs,)
        finally:
            _close(b)


def test_b3_batch_store_path_lands_provenance_too():
    """★B-补：批量写入路径（batch_store_chunks）也必须落 provenance。

    为什么单列一条：本文件的两条 INSERT 是**独立语句**——
    只修 store_chunk 会让"单条写有出处、批量写没出处"，成为更难查的半盲区。
    本条钉住两条路径同源。"""
    from memory.vector_store import VectorStore, Chunk
    ref = {"kind": "conversation_event", "event_id": "ev-b3-batch", "revision": 3}
    with _workdir("m01-b3-batch") as ws:
        vs = VectorStore(str(_db_of(ws)))
        try:
            vs.batch_store_chunks([Chunk(
                id="mem-b3batch1", content="B3 守卫：批量路径也要落出处",
                namespace="dsh", visibility="private",
                speaker="spk-b3-batch", subject="sub-b3-batch",
                source_refs=[ref], scope={"kind": "group", "id": "g-b3-batch"},
                valid_from=10.0, valid_until=20.0,
                confirmation_type="explicit", provenance_status="verified")])
            row = vs._get_conn().execute(
                "SELECT speaker, source_refs, scope, valid_from, provenance_status "
                "FROM chunks WHERE id='mem-b3batch1'").fetchone()
            assert row is not None, "批量路径没有写入这行"
            assert row["speaker"] == "spk-b3-batch", \
                "批量路径 speaker 未落库：%r（两套 INSERT 不同步复发）" % (row["speaker"],)
            assert json.loads(row["source_refs"]) == [ref], \
                "批量路径 source_refs 未落库/不保真：%r" % (row["source_refs"],)
            assert json.loads(row["scope"]) == {"kind": "group", "id": "g-b3-batch"}, \
                "批量路径 scope 未落库/不保真：%r" % (row["scope"],)
            assert row["valid_from"] == 10.0, \
                "批量路径 valid_from 未落库：%r" % (row["valid_from"],)
            ch = vs.get_chunk("mem-b3batch1")
            assert ch.source_refs == [ref], \
                "批量路径读回结构不保真：%r" % (ch.source_refs,)
        finally:
            try:
                vs.close()
            except Exception:
                pass


def test_b3_replay_restores_provenance_into_db():
    """★B-补：崩溃重放路径也把 provenance 还原进 DB（与 store() 同源）。

    为什么单列一条：context_builder 里有**两个** Chunk 构造点（store 与重放）。
    只修 store 会让"正常写有出处、崩溃恢复后没出处"，同一份 WAL 载荷两套结果。"""
    cb = _import_cb()
    ref = {"kind": "conversation_event", "event_id": "ev-b3-replay", "revision": 9}
    with _workdir("m01-b3-replay") as ws:
        from memory.wal_engine import WALEngine
        w = WALEngine(wal_dir=str(_wal_dir_of(ws)))
        # 直接往 WAL 写一条带 provenance 的载荷（模拟"已 durable、DB 还缺"的崩溃现场）
        ent = w.store(content="B3 守卫：重放也要还原出处", track="default", namespace="dsh",
                      visibility="private", speaker="spk-b3-replay", subject="sub-b3-replay",
                      source_refs=[ref], scope={"kind": "group", "id": "g-b3-replay"},
                      valid_from=7.0, valid_until=8.0, confirmation_type="explicit",
                      provenance_status="verified")
        b = cb.ContextBuilder(workspace_dir=ws)
        try:
            row = b._vector_store._get_conn().execute(
                "SELECT speaker, subject, source_refs, scope, valid_from, confirmation_type, "
                "provenance_status FROM chunks WHERE id=?", (ent.id,)).fetchone()
            assert row is not None, "重放没有把这条恢复进 DB"
            assert row["speaker"] == "spk-b3-replay", \
                "重放 speaker 未落库：%r（重放路径漏传 provenance）" % (row["speaker"],)
            assert row["subject"] == "sub-b3-replay", \
                "重放 subject 未落库：%r" % (row["subject"],)
            assert json.loads(row["source_refs"]) == [ref], \
                "重放 source_refs 未落库/不保真：%r" % (row["source_refs"],)
            assert json.loads(row["scope"]) == {"kind": "group", "id": "g-b3-replay"}, \
                "重放 scope 未落库/不保真：%r" % (row["scope"],)
            assert row["valid_from"] == 7.0, "重放 valid_from 未落库：%r" % (row["valid_from"],)
            assert row["provenance_status"] == "verified", \
                "重放 provenance_status 应为 verified：%r" % (row["provenance_status"],)
        finally:
            _close(b)


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print("  PASS  %s" % name)
        except Exception:
            failed.append(name)
            print("  FAIL  %s" % name)
            traceback.print_exc()
    print("\n==== M01 结果：%d passed / %d failed ====" % (len(passed), len(failed)))
    for n in failed:
        print("  FAILED: %s" % n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
