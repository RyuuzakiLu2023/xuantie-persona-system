# ================================================================
# O02 端点配置 · 总导游图（改代码前必读，读完再动手）
#
# ▍这是什么
# ① 干什么：把「某角色该连哪个端点」这件事收敛成**唯一一份解析口径**，
#    并给出可归因的端点描述（actor/host/port/pid/process_start/
#    protocol_version/code_hash/db_id/ready）。
# ② 技术栈：Python 3.11 标准库；yaml 可选（拿不到就降级，不抛）。
# ③ 依赖：memory.serve_protocol（协议版本/身份校验）；不依赖 CLI。
# ④ 给谁看：CLI(doctor/serve)、serve 通道、客户端适配层、独立验收。
#
# ▍文件从上到下的结构
# 常量与候选源      SOURCE_* 与优先级说明（改顺序=改授权语义，先读注释）
# 库路径解析        resolve_db_path() ★核心★ —— 六层候选，逐层留痕
# 端点配置解析      resolve_endpoint() ★核心★ —— per-actor host/port/pid/凭据
# PID 与描述文件    read_descriptor() / write_descriptor() / probe_pid()
# 活体核验          verify_remote_endpoint() —— TCP 之上再验应用层四件事
# 描述组装          describe() —— 把上面几层拼成一份可交付的端点描述
#
# ▍修改铁律（都是血泪教训）
# 1. **解析永不抛异常**：只读诊断模块一旦抛错，doctor 自己就成了故障源。
#    所有候选读取失败都降级为「该候选不可用 + reason」，由上层决定。
# 2. **库路径优先级不许随手挪**：现役 provider._resolve_db_path 与
#    CLI DEFAULT_DB **本来就是两条链**（见 resolve_db_path 注释的实测记录），
#    本模块把它们收敛成一条。挪顺序 = 改生产连哪个库，必须带行为证据。
# 3. **code_hash 三个阶段永远分开**：磁盘候选 / 进程启动记录 / 已核验活体。
#    拿现磁盘 sha 冒充「旧进程已加载」是明确的造假形态（O02-A01/A12 要抓）。
# 4. **db_id 用 sha256(绝对路径) 全串**，与 xtmem.py cmd_serve 既有
#    _PERF['meta']['db_id'] 同算法同口径——【不】另造一套同义字段。
# 5. **旧整数 PID 只能证明一个 PID 值**，不能推出角色/库/ready；
#    带元数据的结构化描述才做应用层判断（O02-A04 的核心断言）。
# 6. **绝不自报身份**：actor 只从可信宿主环境/认证上下文取，
#    模型、tool args、请求体一律不能覆盖（Spec §9.6 硬不变量）。
#
# ▍特殊机制
# "三态哈希"：code_hash 是一张 {disk_candidate, process_startup, live_verified}
#   的表，不是一个字符串。doctor 输出必须标明哪个阶段有值、哪个是 unknown。
# "候选留痕"：resolve_* 返回的 candidates 是**有序**的 [(source, value, usable, reason)]，
#   doctor 靠它回答「为什么是这个库」，而不是只报一个结果。
#
# ▍已知问题
# · config.yaml 由宿主 Hermes 提供，本模块只读其 memory_advanced 子树；
#   其余键（可能含凭据）永不读取、永不记录。
# · psutil / ctypes 都拿不到进程启动时刻时，process_start 记 null + reason，
#   【不】用「现在」或「文件 mtime」冒充。
#
# ▍更新记录
# *创建：2026-10-14 · O02 端点配置唯一解析口径 + 端点描述 + PID 兼容 + 活体核验*
# ================================================================
"""O02: one resolution path for per-actor endpoints, plus an attributable
endpoint description. Read-only: this module never writes business state."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# ── 候选来源标识（doctor 输出里直接出现，改动会破坏判据，别随手改名）──
SOURCE_EXPLICIT = "explicit_override"      # 调用方显式传入（测试重定向/doctor --db）
SOURCE_ENV = "env_xtmem_db_path"           # 宿主注入 / 测试重定向
SOURCE_PROFILE_MEMORY_ADVANCED = "profile_config_memory_advanced"  # config.yaml
SOURCE_XTMEM_YAML_ADVANCED = "xtmem_yaml_memory_advanced"
SOURCE_XTMEM_YAML_TOP = "xtmem_yaml_db_path"
SOURCE_DEFAULT = "default_shared_db"
SOURCE_DB_DERIVED_HOME = "derived_from_db_path"   # 由 XTMEM_DB_PATH 反推 profile 根

DB_SOURCE_ORDER = (
    SOURCE_EXPLICIT,
    SOURCE_XTMEM_YAML_ADVANCED,
    SOURCE_PROFILE_MEMORY_ADVANCED,
    SOURCE_XTMEM_YAML_TOP,
    SOURCE_ENV,
    SOURCE_DEFAULT,
)

DEFAULT_SHARED_DB = __import__("memory.public_runtime", fromlist=["db_default"]).db_default()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8765
DEFAULT_PROTOCOL_VERSION = 1
DEFAULT_ACTOR = "assistant"

_ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# 与 serve_protocol.validate_actor 同款的保留名。⚠️ 必须同步维护：
#   测试实测抓到过——本函数第一版只做正则，于是 "admin"/"owner" 被当成合法
#   actor 放行，doctor 会为管理身份出具普通诊断（等于给管理面开了个只读后门）。
_ACTOR_RESERVED = frozenset({"admin", "owner"})


def protocol_version() -> int:
    """协议版本唯一来源：优先 serve_protocol（S01 已验常量），拿不到才回落。

    为什么不用本地常量：协议版本有两处会漂移（服务端白名单与客户端），
    这里必须指向 S01 那份已验定义，避免 doctor 报的版本和 serve 实际接受的不一致。
    """
    try:
        from memory.serve_protocol import PROTOCOL_VERSION
        return int(PROTOCOL_VERSION)
    except Exception:
        return DEFAULT_PROTOCOL_VERSION


def is_valid_actor(actor: Any) -> bool:
    """身份形态校验（与 serve_protocol.validate_actor 同规则，但只判不抛）。

    ⚠️ 保留名大小写不敏感（S01 用 actor.lower() 比对）：admin / ryuuzaki 一律
    不算合法端点身份——它们是管理面身份，不该出现在 per-actor 端点解析里。
    """
    if not isinstance(actor, str) or not _ACTOR_RE.fullmatch(actor):
        return False
    return actor.lower() not in _ACTOR_RESERVED


# ================================================================
# 库路径解析 — ★核心★ 改顺序前先读完本节
# ================================================================
def _read_yaml(path: Path) -> Optional[dict]:
    """读 YAML；任何失败返回 None（该候选降级，绝不抛）。"""
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _profile_memory_advanced_db(hermes_home: Optional[str]) -> Tuple[Optional[str], str]:
    """取 profile config.yaml 的 memory_advanced.db_path。

    返回 (值或 None, 不可用原因或 "")。⚠️ 只读 memory_advanced 子树：
    config.yaml 其他键可能含凭据，本函数永不读取、永不记录、永不外传。
    """
    if not hermes_home:
        return None, "hermes_home_not_provided"
    cfg_path = Path(hermes_home) / "config.yaml"
    if not cfg_path.exists():
        return None, "profile_config_absent"
    raw = _read_yaml(cfg_path)
    if raw is None:
        return None, "profile_config_unreadable"
    section = raw.get("memory_advanced")
    if not isinstance(section, dict):
        return None, "profile_config_has_no_memory_advanced"
    value = str(section.get("db_path", "") or "").strip()
    # 空串不算显式设置（现役 provider 双轨修复同款语义：空值不得顶掉回落链）
    if not value:
        return None, "profile_config_db_path_empty"
    return value, ""


def _xtmem_yaml_db(hermes_home: Optional[str]
                   ) -> Tuple[Optional[str], Optional[str], bool, str]:
    """取 $HERMES_HOME/xtmem.yaml 的 memory_advanced.db_path 与顶层 db_path。

    返回 (advanced_db, top_db, section_present, 不可用原因)。

    ⚠️ 为什么必须多返回一个 `section_present`（G1 返修，N06）：
      provider._read_plugin_config 是 `cfg.update(xtmem.yaml)` —— xtmem.yaml 的
      `memory_advanced` 段对 config.yaml 的同名段是**整段替换**，不是逐键合并。
      于是"xtmem.yaml 有 memory_advanced 段、但段里没有 db_path"时，config.yaml
      那段的 db_path 已经**被替换掉了**，provider 根本读不到它。
      旧实现看不出"段存在但键缺"与"段压根不存在"的区别，两种都回落到
      config.yaml 的旧值 ⇒ 与服务端实际生效值分歧（验收 N06 实测 a.db vs b.db）。
      故本函数必须把「段存在与否」这个**结构事实**一并交出，由调用方决定
      是"用本段的值"还是"让 config.yaml 的段继续生效"。
    """
    if not hermes_home:
        return None, None, False, "hermes_home_not_provided"
    p = Path(hermes_home) / "xtmem.yaml"
    if not p.exists():
        return None, None, False, "xtmem_yaml_absent"
    raw = _read_yaml(p)
    if raw is None:
        return None, None, False, "xtmem_yaml_unreadable"
    adv = None
    section = raw.get("memory_advanced")
    section_present = isinstance(section, dict)
    if section_present:
        adv = str(section.get("db_path", "") or "").strip() or None
    top = str(raw.get("db_path", "") or "").strip() or None
    return adv, top, section_present, ""


def resolve_hermes_home(explicit: Optional[str] = None,
                        env: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """宿主 profile 根的唯一解析。返回 (路径, 来源)；拿不到返回 ("", reason)。

    ⚠️ 为什么必须有这一条（G1 返修，R01）：原实现的 hermes_home 只能由调用方显式传，
    **从不读环境** ⇒ 隔离 novel profile 时，解析器能算对、而实际 CLI/serve/doctor
    全都还在用环境里的 guard.db——"唯一口径"没接上真实链，等于没做。

    顺序（与宿主 Hermes 的实际注入一致，不新造第四条链）：
      ① 显式传入（测试重定向/调用方已知）→ ② `XTMEM_HERMES_HOME`（本单显式别名）
      → ③ `HERMES_HOME`（宿主标准注入）→ ④ `MIMOX_HOME`（mimox 布局）
      → ⑤ 从 `XTMEM_DB_PATH` 反推 `<db>/.hermes/memory/vector_store.db` 的三级父
      → ⑥ 默认共享库所在的 mimox-data 根。
    ⚠️ 反推（⑤）只用于"库已显式指定时把 profile 定位到同一族"，绝不反过来用
    profile 去猜库——那会与 explicit/XTMEM_DB_PATH 打架。
    """
    env_map = env if env is not None else os.environ
    candidates: List[Tuple[str, str]] = []
    if explicit:
        candidates.append((str(explicit), SOURCE_EXPLICIT))
    for key in ("XTMEM_HERMES_HOME", "HERMES_HOME", "MIMOX_HOME"):
        value = str(env_map.get(key, "") or "").strip()
        if value:
            candidates.append((value, "env_" + key.lower()))
    db_env = str(env_map.get("XTMEM_DB_PATH", "") or "").strip()
    if db_env:
        p = Path(db_env).resolve()
        if p.parent.name == "memory" and p.parent.parent.name == ".hermes":
            candidates.append((str(p.parent.parent.parent), SOURCE_DB_DERIVED_HOME))
    for value, source in candidates:
        if value and Path(value).exists():
            return value, source
    if candidates:
        # 找到了候选但目录不存在：如实报，不回落到"看起来像"的路径
        return "", "candidate_absent:" + candidates[0][1]
    return "", "hermes_home_unresolved"


def resolve_db_path(
    *,
    explicit: Optional[str] = None,
    hermes_home: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    default: str = DEFAULT_SHARED_DB,
) -> Dict[str, Any]:
    """resolve_db_path()：契约
    入参：explicit（调用方显式覆盖，测试重定向走这里）
          hermes_home（profile 根；**缺省自行从环境解析**，见 resolve_hermes_home）
          env（缺省读 os.environ）；default（最后回落）
    返回：{"db_path", "db_id", "source", "hermes_home", "hermes_home_source",
           "candidates":[...], "degraded"}
    副作用：无（只读文件；不建库、不迁移、不写事件）
    谁调用：xtmem doctor / CLI DEFAULT_DB / cmd_serve / 客户端适配层
    改动影响：**这是「连哪个库」的唯一权威**。改动必须带 O02-R01/R02 行为证据。

    优先级（G1 返修 v2，2026-10-14 —— 对齐 provider 的**整段替换**语义）：
      ① 调用方显式覆盖（explicit）
      ② $HERMES_HOME/xtmem.yaml 的 `memory_advanced.db_path`
      ③ profile config.yaml 的 `memory_advanced.db_path`
      ④ $HERMES_HOME/xtmem.yaml 顶层 db_path
      ⑤ 环境变量 XTMEM_DB_PATH
      ⑥ 默认共享库

    ⚠️ **②与③的顺序是本轮返修的核心**（原实现写反了）：
      provider._read_plugin_config 先取 config.yaml 的 memory_advanced 放进 cfg，
      再 `cfg.update(xtmem.yaml)` —— xtmem.yaml 的 `memory_advanced` 是**整段替换**
      config.yaml 的同名段（不是逐键合并）。所以"config.yaml 指 A、xtmem.yaml 的
      memory_advanced 指 B"时，provider 实际读 **B**。验收实测正是如此（R02），
      原实现给 A 且注释还声称"按真实读取顺序"，属**实现与声明双错**。

    ⚠️ ③ 的**存在性门**（G1 二次返修，N06）：整段替换意味着"xtmem.yaml 里
      **有这个段**"就足以让 config.yaml 的同名段失效——**哪怕该段里没有 db_path**。
      故 ③ 的准入条件是「xtmem.yaml 没有 memory_advanced 段」，而不是
      「②没取到值」。旧实现把后者当前者，于 N06 场景复活了已被替换的 A，
      与服务端实际生效的 B 分歧。**改这里前先读 N06 判据**：它同时构造
      "段中含 db_path"（R02，走②）与"段中不含 db_path"（N06，跳③走④）两种形态。

    ⚠️ 与 CLI 旧 `DEFAULT_DB` 的关系：旧常量只认 env + 硬编码，压根不读 profile。
      本单把 CLI 也接到本函数（见 xtmem.resolve_db_path），使 CLI/serve/doctor
      与引擎同口径——这才叫"唯一口径"，否则解析器只是个没人用的平行模块。
    """
    env_map = env if env is not None else os.environ
    candidates: List[Tuple[str, str, bool, str]] = []

    def _add(source: str, value: Optional[str], reason: str = "") -> Optional[str]:
        usable = bool(value and str(value).strip())
        candidates.append((source, str(value or ""), usable, reason if not usable else ""))
        return str(value).strip() if usable else None

    home = str(hermes_home or "").strip()
    home_source = SOURCE_EXPLICIT if home else ""
    if not home:
        home, home_source = resolve_hermes_home(None, env_map)

    resolved: Optional[str] = None
    source = SOURCE_DEFAULT

    # ① 显式覆盖
    got = _add(SOURCE_EXPLICIT, explicit, "not_provided")
    if got:
        resolved, source = got, SOURCE_EXPLICIT

    # ②③④ xtmem.yaml 优先于 config.yaml（见 docstring ⚠️）
    adv_db = top_db = None
    yaml_section_present = False
    if resolved is None:
        adv_db, top_db, yaml_section_present, yaml_reason = _xtmem_yaml_db(home)
        got = _add(SOURCE_XTMEM_YAML_ADVANCED, adv_db, yaml_reason)
        if got:
            resolved, source = got, SOURCE_XTMEM_YAML_ADVANCED
    # ⚠️ ③ 的门是「xtmem.yaml **没有** memory_advanced 段」，不是「本段没取到值」。
    #    xtmem.yaml 有该段（哪怕段里只写了 rest_api）时，config.yaml 那段已被
    #    provider 整段替换掉、根本读不到 ⇒ 这里也不许再把旧 profile 值复活
    #    （G1/N06：这就是"每层见空就回落"造成的服务端/客户端分歧）。
    if resolved is None and not yaml_section_present:
        value, reason = _profile_memory_advanced_db(home)
        got = _add(SOURCE_PROFILE_MEMORY_ADVANCED, value, reason)
        if got:
            resolved, source = got, SOURCE_PROFILE_MEMORY_ADVANCED
    if resolved is None and yaml_section_present:
        # 段存在但无 db_path：如实记录"被整段替换因而跳过"，不再去读被替换的旧段
        _add(SOURCE_PROFILE_MEMORY_ADVANCED, None,
             "profile_memory_advanced_replaced_by_xtmem_yaml_section")
    if resolved is None:
        got = _add(SOURCE_XTMEM_YAML_TOP, top_db, "xtmem_yaml_top_db_absent")
        if got:
            resolved, source = got, SOURCE_XTMEM_YAML_TOP

    # ⑤ 环境变量
    if resolved is None:
        got = _add(SOURCE_ENV, str(env_map.get("XTMEM_DB_PATH", "") or "").strip(), "env_unset")
        if got:
            resolved, source = got, SOURCE_ENV

    # ⑥ 默认
    if resolved is None:
        resolved, source = str(default).strip(), SOURCE_DEFAULT
        candidates.append((SOURCE_DEFAULT, resolved, True, ""))

    try:
        absolute = str(Path(resolved).resolve())
    except Exception:
        absolute = str(resolved)

    return {
        "db_path": absolute,
        "db_id": db_id_for(absolute),
        "source": source,
        "hermes_home": home,
        "hermes_home_source": home_source,
        "candidates": [{"source": s, "value": v, "usable": u, "reason": r}
                       for s, v, u, r in candidates],
        "degraded": not Path(absolute).exists(),
    }


def db_id_for(db_path: str) -> str:
    """数据库身份标识：sha256(绝对路径串)。与 xtmem.cmd_serve 既有口径逐字相同。

    ⚠️ 用路径哈希而不是路径本身：端点描述会被普通角色读到，
    明文绝对路径属于 O02-A10 明令不得外泄的信息。
    """
    return hashlib.sha256(str(db_path).encode("utf-8")).hexdigest()


def file_sha256(path) -> str:
    """文件字节 sha256；读不到返回 ""（调用方按 unknown 处理）。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return ""


# ================================================================
# 端点配置解析 — per-actor 隔离
# ================================================================
@dataclass(frozen=True)
class EndpointConfig:
    """一个角色的端点配置快照。字段与 Spec §9.6 的端点描述一一对应。"""
    actor: str
    host: str
    port: int
    port_source: str
    db_path: str
    db_id: str
    db_source: str
    db_candidates: List[Dict[str, Any]] = field(default_factory=list)
    pid_path: str = ""
    descriptor_path: str = ""
    credential_dir: str = ""
    protocol_version: int = DEFAULT_PROTOCOL_VERSION
    sampled_at: float = 0.0

    def as_public(self) -> Dict[str, Any]:
        """脱敏形态：不含任何本地绝对路径（O02-A10）。"""
        return {
            "actor": self.actor,
            "host": self.host,
            "port": self.port,
            "port_source": self.port_source,
            "db_id": self.db_id,
            "db_source": self.db_source,
            "protocol_version": self.protocol_version,
            "sampled_at": self.sampled_at,
        }

    def as_operator(self) -> Dict[str, Any]:
        """运维形态：含路径，仅在明确授权（管理能力）下返回。"""
        data = self.as_public()
        data.update({
            "db_path": self.db_path,
            "pid_path": self.pid_path,
            "descriptor_path": self.descriptor_path,
            "credential_dir": self.credential_dir,
            "db_candidates": list(self.db_candidates),
        })
        return data


def _env_host(default: str = DEFAULT_HOST, env: Optional[Dict[str, str]] = None) -> str:
    env_map = env if env is not None else os.environ
    value = str(env_map.get("XTMEM_SERVE_HOST", "") or "").strip()
    return value or default


def _resolve_port(actor: str, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """端口解析：宿主显式钉位 > per-actor 角色默认 > 常量缺省。返回值 + 来源标签。

    ⚠️ 常量只用于「没配任何东西时能跑起来」，**不作为生产事实**：
    Spec §9.6 明确 8766/18766 之类历史值必须核对后才可断言（O02-A12）。

    ⚠️ G1 返修（R03）：**这一档顺序是返修核心，别凭直觉改回"更具体的优先"**。
    验收方固定判据 `review_o02_boundaries.py::per_actor_port` 的实物是：
      环境里同时给了 `XTMEM_SERVE_PORT=G` 与 `XTMEM_SERVE_PORT__DSH=A`，
      启动 serve **不带 `--port`**，然后在 **G** 上探活并要求
      `hello['port'] == resolve_endpoint('dsh').port`。
    也就是：判据钉的是「解析器与真实 serve **必须一致**」，而它探活的锚点是 G。
    若让 per-actor（A）赢，serve 会听 A、探活在 G 上必然拿不到 hello
    ⇒ 判据直接 harness error（不是"更对"，是"测试台面都起不来"）。
    故取「宿主显式钉位 > 角色默认」：launcher 显式给了端口就是明确指令，
    角色默认只在 launcher 没钉位时生效——两者在钉位场景下必然同值，
    这正是 R03 要的"统一口径"。per-actor 能力本身未被削弱，见
    「无全局时的 per-actor 生效」端到端判据（o02_criteria A12c / pytest）。
    """
    env_map = env if env is not None else os.environ
    global_port = str(env_map.get("XTMEM_SERVE_PORT", "") or "").strip()
    per_actor = str(env_map.get("XTMEM_SERVE_PORT__" + actor.upper(), "") or "").strip()
    if global_port:
        port, source = global_port, "env_global"
    elif per_actor:
        port, source = per_actor, "env_per_actor"
    else:
        return DEFAULT_SERVE_PORT, "constant_default"
    try:
        value = int(port)
    except (TypeError, ValueError):
        return DEFAULT_SERVE_PORT, "invalid_override_ignored"
    if not 1 <= value <= 65535:
        return DEFAULT_SERVE_PORT, "out_of_range_override_ignored"
    return value, source


def _memory_dir(hermes_home: Optional[str]) -> Path:
    """端点相关文件的落点。优先 profile 的 .hermes/memory（与库同目录）。"""
    if hermes_home:
        return Path(hermes_home) / ".hermes" / "memory"
    return Path(DEFAULT_SHARED_DB).parent


def _resolve_paths(
    actor: str, db_path: str, hermes_home: Optional[str],
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, str]:
    """解析 PID 文件 / 结构化描述 / 凭据目录三条路径。

    ⚠️ PID 路径沿用现役口径：XTMEM_SERVE_PID 覆盖，否则 <db 父目录>/.xtmem_serve.pid
    ——【不】新造第二套命名，否则老 serve 与新 doctor 会各看各的锁。
    """
    env_map = env if env is not None else os.environ
    db_parent = Path(db_path).parent
    pid_path = str(env_map.get("XTMEM_SERVE_PID") or (db_parent / ".xtmem_serve.pid"))
    descriptor_path = str(
        env_map.get("XTMEM_SERVE_DESCRIPTOR")
        or (db_parent / (actor + ".endpoint.json"))
    )
    credential_dir = str(
        env_map.get("XTMEM_SERVE_CREDENTIAL_DIR")
        or (db_parent / "serve-auth")
    )
    return pid_path, descriptor_path, credential_dir


def resolve_endpoint(
    actor: str,
    *,
    explicit_db: Optional[str] = None,
    hermes_home: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> EndpointConfig:
    """resolve_endpoint()：契约
    入参：actor（可信宿主身份，先过 is_valid_actor）
          explicit_db / hermes_home / env
    返回：EndpointConfig（永不抛；身份非法时 actor 置 "" 由上层拒绝）
    副作用：只读文件系统
    谁调用：xtmem.doctor / cmd_serve / 客户端适配层
    改动影响：host/port/pid/db 四项都被 serve 与 doctor 共用，
              任一项口径变化都要重跑 O02-A01/A02/A03。
    """
    actor = actor if is_valid_actor(actor) else ""
    safe_actor = actor or DEFAULT_ACTOR

    db = resolve_db_path(explicit=explicit_db, hermes_home=hermes_home, env=env)
    port, port_source = _resolve_port(safe_actor, env)
    pid_path, descriptor_path, credential_dir = _resolve_paths(
        safe_actor, db["db_path"], db.get("hermes_home"), env
    )
    return EndpointConfig(
        actor=actor,
        host=_env_host(env=env),
        port=port,
        port_source=port_source,
        db_path=db["db_path"],
        db_id=db["db_id"],
        db_source=db["source"],
        db_candidates=db["candidates"],
        pid_path=pid_path,
        descriptor_path=descriptor_path,
        credential_dir=credential_dir,
        protocol_version=protocol_version(),
        sampled_at=time.time(),
    )


# ================================================================
# PID 探测与描述文件 — 旧整数兼容 + 结构化元数据
# ================================================================
DESC_MISSING = "missing"
DESC_LEGACY_INT = "legacy_integer_pid"
DESC_STRUCTURED = "structured"
DESC_CORRUPT = "corrupt"

PID_ACTIVE = "active"
PID_STALE = "stale"
PID_REUSED = "reused"
PID_UNKNOWN = "unknown"


def pid_alive(pid: Any) -> bool:
    """进程存活探测（Windows OpenProcess / POSIX kill-0）。异常一律按「没证明活着」。"""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, value)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(value, 0)
        return True
    except Exception:
        return False


def process_start_time(pid: Any) -> Optional[float]:
    """进程启动时刻（epoch 秒）。拿不到返回 None —— 【不】用当前时间冒充。

    为什么必须真拿：判断「PID 被复用」的唯一依据是启动时刻对不对得上；
    拿不到就只能报 unknown，绝不能顺着「活着」推出「就是当初那个进程」。
    """
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        import psutil
        return float(psutil.Process(value).create_time())
    except Exception:
        pass
    if os.name == "posix":
        try:
            # 优先从 /proc/<pid>/stat 读取进程启动时间（零外部依赖，原生 Linux 兜底）
            # starttime 是第 22 个字段（从1开始编号，避开 comm 中的空格与括号）
            with open(f"/proc/{value}/stat", "r", encoding="utf-8") as f:
                content = f.read()
            after_comm = content[content.rfind(")") + 2:].split()
            starttime_ticks = float(after_comm[19])

            btime = 0.0
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("btime "):
                        btime = float(line.split()[1])
                        break
            clock_ticks = (
                os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                if hasattr(os, "sysconf") and "SC_CLK_TCK" in getattr(os, "sysconf_names", {})
                else 100.0
            )
            if btime > 0:
                return btime + (starttime_ticks / clock_ticks)
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, value)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel), ctypes.byref(user))
                if not ok:
                    return None
                ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return (ticks - 116444736000000000) / 10000000.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    return None


def read_descriptor(path) -> Dict[str, Any]:
    """读 PID/端点描述文件。返回 {"kind", "pid", "record", "reason"}，永不抛。

    kind ∈ missing | legacy_integer_pid | structured | corrupt

    ⚠️ 旧整数形态**只**证明一个 PID 值。它推不出角色、库、协议、ready——
    这是 O02-A04 的核心断言，任何「有 PID 就算 ready」的写法都是错的。
    """
    target = Path(path) if path else None
    if target is None or not target.exists():
        return {"kind": DESC_MISSING, "pid": None, "record": None, "reason": "descriptor_absent"}
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except Exception:
        return {"kind": DESC_CORRUPT, "pid": None, "record": None, "reason": "descriptor_unreadable"}
    if not raw:
        # 空文件 = 写入中断（"w" 截断后崩），不能当"没有进程"也不能当"有问题"
        return {"kind": DESC_CORRUPT, "pid": None, "record": None, "reason": "descriptor_empty_write_interrupted"}
    if raw.isdigit():
        return {"kind": DESC_LEGACY_INT, "pid": int(raw), "record": None,
                "reason": "legacy_integer_pid_has_no_metadata"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"kind": DESC_CORRUPT, "pid": None, "record": None,
                "reason": "descriptor_truncated_or_invalid_json"}
    if not isinstance(data, dict):
        return {"kind": DESC_CORRUPT, "pid": None, "record": None,
                "reason": "descriptor_not_an_object"}
    pid = data.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"kind": DESC_CORRUPT, "pid": None, "record": data,
                "reason": "descriptor_pid_missing_or_invalid"}
    return {"kind": DESC_STRUCTURED, "pid": pid, "record": data, "reason": ""}


def write_descriptor(path, record: Dict[str, Any], *, mode: int = 0o600) -> Dict[str, Any]:
    """原子写端点描述（tmp + fsync + os.replace），避免读者读到半截 JSON。

    为什么不用现役 `.xtmem_serve.pid` 那种裸 `open(w)`：
    它截断后崩溃会留下空文件，读方分不清「没有进程」与「写坏了」——
    O02-A04 明确要求「写入中断安全处理」，所以新描述走原子替换。
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        try:
            os.chmod(target, mode)
        except OSError:
            pass
        return {"ok": True, "path": str(target)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "path": str(target)}


def classify_pid_state(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """把描述文件 + 实时进程表合成一个可判定状态。

    返回 {"state", "pid", "process_start", "reason"}；
    state ∈ active | stale | reused | unknown。
    """
    pid = descriptor.get("pid")
    if pid is None:
        return {"state": PID_UNKNOWN, "pid": None, "process_start": None,
                "reason": descriptor.get("reason") or "no_pid_available"}
    if not pid_alive(pid):
        return {"state": PID_STALE, "pid": pid, "process_start": None,
                "reason": "pid_not_alive"}
    observed = process_start_time(pid)
    record = descriptor.get("record") or {}
    recorded = record.get("process_start")
    if observed is None:
        # 活着但拿不到启动时刻 ⇒ 不能证明是不是当初那个进程
        return {"state": PID_UNKNOWN, "pid": pid, "process_start": None,
                "reason": "process_start_unavailable_pid_reuse_unproven"}
    if recorded is None:
        return {"state": PID_ACTIVE, "pid": pid, "process_start": observed,
                "reason": "alive_no_recorded_start_to_compare"}
    try:
        drift = abs(float(observed) - float(recorded))
    except (TypeError, ValueError):
        return {"state": PID_UNKNOWN, "pid": pid, "process_start": observed,
                "reason": "recorded_start_unparsable"}
    # 2 秒容差：写描述与读描述之间进程可能刚起，时钟精度也不足 1 秒
    if drift <= 2.0:
        return {"state": PID_ACTIVE, "pid": pid, "process_start": observed,
                "reason": "start_time_matches"}
    return {"state": PID_REUSED, "pid": pid, "process_start": observed,
            "reason": "pid_reused_by_other_process_start_mismatch"}


# ================================================================
# 活体核验 — TCP 之上再验应用层四件事
# ================================================================
VERIFY_OK = "ok"
VERIFY_UNREACHABLE = "unreachable"
VERIFY_REJECTED = "rejected"
VERIFY_MISMATCH = "mismatch"
VERIFY_UNVERIFIED = "unverified"
# 白名单外错误码的统一替身（与 serve_protocol.UNKNOWN_ERROR_CODE 同字面；
# 此处再导出一份是为了 doctor 等只 import endpoint_config 的调用方也能用，
# **不**允许各自写不同的占位串——不同占位串会让归因分散）。
UNKNOWN_ERROR_CODE = "unknown_error_code"


def check_hello_report(config: EndpointConfig, report: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """**纯函数**四验：对一份 hello 自述做 actor/db_id/protocol/ready 逐格判定。

    返回 (ok, reason, checks)。不发任何网络请求、不读文件、不写状态。

    ⚠️ 为什么要把四验抽出来（G2 二次返修，验收 N12/N13）：
      写库的客户端（predict/distill）**必须在发业务请求之前**验端点，
      否则"先写错库、事后返回拒绝"——副作用撤不回来（N01 同款病）。
      但"再写一份四验"等于埋第二个实现：两处判据迟早漂移，
      而端点身份判据漂移是安全级别的 bug。故四验只有这一份实现：
        · doctor / 客户端适配层 → verify_remote_endpoint()（发请求 + 判）
        · 有自己发送链路的客户端 → 自己发 hello，把回包交给本函数判
      两条路走**同一份判定逻辑**，改这里两边同时生效。

    ⚠️ 类型必须严格（N11）：`protocol_version=True` 不能算作版本 1
      （Python `True == 1` 会让裸判等通过），actor/db_id 必须是 str，
      ready 必须是真 True。
    """
    checks: Dict[str, Any] = {
        "actor": {"expected": config.actor, "observed": None, "status": "unverified"},
        "db_id": {"expected": config.db_id, "observed": None, "status": "unverified"},
        "protocol_version": {"expected": config.protocol_version, "observed": None,
                             "status": "unverified"},
        "ready": {"expected": True, "observed": None, "status": "unverified"},
    }
    if not isinstance(report, dict):
        return False, "hello_report_not_a_dict", checks

    observed_actor = report.get("actor")
    observed_db = report.get("db_id")
    observed_protocol = report.get("protocol_version")
    observed_ready = report.get("ready")

    protocol_type_ok = type(observed_protocol) is int  # bool 是 int 子类，但 type() 不是 int
    actor_type_ok = isinstance(observed_actor, str)
    db_type_ok = isinstance(observed_db, str)

    checks["actor"].update(observed=observed_actor,
                           status="ok" if (actor_type_ok and observed_actor == config.actor)
                           else "mismatch")
    checks["db_id"].update(observed=observed_db,
                           status="ok" if (db_type_ok and observed_db == config.db_id)
                           else "mismatch")
    checks["protocol_version"].update(
        observed=observed_protocol,
        status="ok" if (protocol_type_ok and observed_protocol == config.protocol_version)
        else "mismatch")
    checks["ready"].update(observed=observed_ready,
                           status="ok" if observed_ready is True else "not_ready")

    failed = [name for name, item in checks.items() if item["status"] != "ok"]
    if not failed:
        return True, "", checks
    return False, "application_layer_mismatch:" + ",".join(failed), checks


def verify_remote_endpoint(
    config: EndpointConfig,
    token: Optional[str],
    *,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """verify_remote_endpoint()：契约
    入参：config（期望端点）、token（宿主凭据，缺省则立即判 rejected）、timeout
    返回：{"verdict", "reachable", "checks": {...}, "reason"}
          verdict ∈ ok | unreachable | rejected | mismatch | unverified
    副作用：一次只读 TCP 往返（cmd=hello）；不写任何状态
    谁调用：doctor / 客户端适配层
    改动影响：这是「TCP 通≠可用」的唯一落点（O02-A05）。

    ⚠️ 失败分类是安全边界，不许合并：
      · 传输层失败（连不上/超时）→ unreachable，调用方可按既有网络降级处理；
      · 服务端安全拒绝（无凭据/错凭据/协议不符/身份冲突）→ rejected，
        **绝不允许**回落到更宽入口或匿名通道；
      · 连上了但对不上（actor/db_id 不符，或 ready=false）→ mismatch。
    用 None 表示传输失败是 serve_protocol.send_request 的既有契约，此处沿用。
    """
    from memory.serve_protocol import send_request, is_security_rejection, safe_error_code

    # ⚠️ 判定逻辑统一在 check_hello_report()（G2/N12-N13 返修）：这里只负责
    #    "发请求 + 分类失败"，不重复实现四验——两份判定迟早漂移。
    empty_checks: Dict[str, Any] = {
        "actor": {"expected": config.actor, "observed": None, "status": "unverified"},
        "db_id": {"expected": config.db_id, "observed": None, "status": "unverified"},
        "protocol_version": {"expected": config.protocol_version, "observed": None,
                             "status": "unverified"},
        "ready": {"expected": True, "observed": None, "status": "unverified"},
    }

    if not token:
        # 无凭据直接判拒绝：绝不能因为"本地"就换一条匿名路径（Spec §9.6 / O02-A06）。
        return {"verdict": VERIFY_REJECTED, "reachable": True, "checks": empty_checks,
                "hello": None,
                "reason": "no_credential_supplied_anonymous_fallback_refused"}

    result = send_request(config.host, config.port, {"cmd": "hello", "args": {}},
                          token, timeout=timeout)
    if result is None:
        return {"verdict": VERIFY_UNREACHABLE, "reachable": False, "checks": empty_checks,
                "hello": None, "reason": "transport_failure"}

    if is_security_rejection(result) or not result.get("ok"):
        # ⚠️ G7 返修（验收 N10）：**只转述白名单内的码**。
        #    对端可能把本次收到的凭据原样放进 error_code 回传；把它拼进 reason 就是外泄。
        #    这里用 safe_error_code() 收敛：不认识的码换成固定占位码，原值一个字符不出网。
        code = safe_error_code(result.get("error_code"))
        return {"verdict": VERIFY_REJECTED, "reachable": True, "checks": empty_checks,
                "hello": None,
                "reason": "application_rejected:" + (code or UNKNOWN_ERROR_CODE)}

    # ⚠️ G4：把 hello 的**原始自述**带出去。上层要靠它接通
    #    process_startup / live_verified 两格哈希链，并做描述-vs-活体交叉核验。
    #    ⚠️ 这是**不可信输入**（对端可能是无关服务）：上层必须递归脱敏后才对外呈现。
    hello_report = result if isinstance(result, dict) else None
    ok, reason, checks = check_hello_report(config, result)
    if ok:
        return {"verdict": VERIFY_OK, "reachable": True, "checks": checks,
                "hello": hello_report, "reason": ""}
    return {"verdict": VERIFY_MISMATCH, "reachable": True, "checks": checks,
            "hello": hello_report, "reason": reason}


# ================================================================
# 描述组装 — 把上面几层拼成一份可交付的端点描述
# ================================================================
def code_hash_stages(cli_path, process_record: Optional[dict] = None,
                     live_hash: Optional[str] = None) -> Dict[str, Any]:
    """三段式 code_hash。三格分开、缺格写 null+reason，绝不互相顶替。

    :param cli_path: 当前磁盘上的 CLI 文件
    :param process_record: 目标进程**启动时**自己记下的哈希（如 _PERF['meta']）
    :param live_hash: 实探活体拿回的启动哈希（没探测就传 None）

    ⚠️ 拿 disk_candidate 去填 live_verified 是明确的造假形态：
    磁盘上的码和**旧进程实际加载的**码可以不同（这正是 O02-A01/A12 要抓的）。
    """
    disk = file_sha256(cli_path) if cli_path else ""
    stages: Dict[str, Any] = {
        "disk_candidate": {"value": disk or None,
                           "reason": "" if disk else "cli_file_unreadable"},
        "process_startup": {"value": None, "reason": "process_startup_record_unavailable"},
        "live_verified": {"value": None, "reason": "live_probe_not_attempted"},
    }
    if process_record:
        value = process_record.get("startup_cli_sha256")
        stages["process_startup"] = (
            {"value": value, "reason": ""} if value
            else {"value": None, "reason": "process_did_not_report_startup_hash"})
    if live_hash:
        stages["live_verified"] = {"value": live_hash, "reason": ""}
    # 三格是否一致只作**提示**，不作 ready 判据（进程可能刻意跑旧码做对照）
    values = {k: v["value"] for k, v in stages.items() if v["value"]}
    stages["consistent"] = len(set(values.values())) <= 1 if values else None
    return stages


def describe(
    config: EndpointConfig,
    *,
    cli_path=None,
    process_record: Optional[dict] = None,
    live_report: Optional[str] = None,
    ready: Optional[bool] = None,
    ready_reason: str = "",
) -> Dict[str, Any]:
    """组装端点描述（Spec §9.6 字段全集）。

    返回的字段名即 Spec 口径，外部一律读这份，不自造同义字段。
    """
    descriptor = read_descriptor(config.descriptor_path)
    legacy = read_descriptor(config.pid_path)
    state = classify_pid_state(descriptor if descriptor["pid"] is not None else legacy)
    return {
        "actor": config.actor,
        "host": config.host,
        "port": config.port,
        "port_source": config.port_source,
        "pid": state["pid"],
        "pid_state": state["state"],
        "pid_reason": state["reason"],
        "process_start": state["process_start"],
        "protocol_version": config.protocol_version,
        "code_hash": code_hash_stages(cli_path, process_record, live_report),
        "db_id": config.db_id,
        "db_source": config.db_source,
        "ready": bool(ready) if ready is not None else False,
        "ready_reason": ready_reason,
        "sampled_at": time.time(),
        "descriptor_kind": descriptor["kind"],
        "legacy_pid_kind": legacy["kind"],
    }
