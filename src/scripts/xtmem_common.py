# -*- coding: utf-8 -*-
"""xtmem_common.py — 蒸馏/派生作业共用接线（库路径 / 引擎 / CLI / namespace / 时区）。

【这是什么】
把「脚本怎么找到库、怎么找到 memory 引擎、怎么调 xtmem CLI、默认对哪些 namespace 干活、
按哪个时区分日」这些接线问题收成一处。units / llm / commit / timeline / profile / wiki /
lineage 全从这里取，改口径只改这一份。

【为什么要有】
历史脚本各写各的生产绝对路径、各读各的 profile .env、各自硬编码域名单：
  - 换一台机器/装成 wheel 后，`import memory` 和 store 子进程全断；
  - 两个脚本对「默认域」和「时区」口径不一致，派生物互相打架；
  - 私有地址/密钥被当默认值写死在代码里，公开仓无法直接跑。
本模块只做「解析」不做「业务」：不建库、不迁移、不打印、不读配置文件。

【契约】
  resolve_db_path(explicit=None) -> str
      explicit > $XTMEM_DB_PATH > $XTMEM_HOME/vector_store.db > ~/.xtmem/vector_store.db
  ensure_engine() -> None
      保证 `import memory` 可用；仓库内直跑时按本文件位置补 sys.path；失败抛 RuntimeError
  cli_argv() -> list[str]
      $XTMEM_CLI（.py 用当前解释器）> 已安装的 xtmem 模块（python -m xtmem）> 仓库内 src/cli
  store_via_cli(...) -> dict
      统一走 CLI store；子进程环境强制 XTMEM_DB_PATH / XTMEM_ACTOR / PYTHONUTF8
  resolve_namespaces(conn, explicit=None) -> list[str]
      explicit > $XTMEM_DISTILL_NAMESPACE > $XTMEM_NAMESPACE > 库内 active 的全部 namespace
  tz() -> tzinfo
      $XTMEM_TZ（IANA 名，默认 Asia/Shanghai）> 固定偏移 $XTMEM_TZ_OFFSET

【修改铁律】
1. 【不】在本模块读 .env / 配置文件 / 凭据。密钥只由调用脚本从环境变量取（公开仓红线）。
2. 【不】把生产绝对路径写回来。默认值只能是用户目录或环境变量。
3. 【不】在这里做写库/建表/迁移——那会破坏各脚本自己的事务边界。
4. 子进程 CLI 必须继承当前环境后再覆盖 XTMEM_DB_PATH；否则测试重定向会被静默打回默认库。

【外号表】
  "正本"   → 本文件所在仓库的可运行源码树
  "引擎"   → memory 包（open_db / context_builder / vector_store）
  "CLI"    → xtmem.py（store/recall 等命令的入口）

【更新记录】
*创建：2026-10-22 · 公开蒸馏子树施工：接线统一，去掉生产路径/私有 .env/私有域硬编码*
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

try:  # 包内导入（wheel: xtmem_jobs）与脚本直跑（同目录）两态都要活
    from . import __name__ as _pkg_name  # noqa: F401
except ImportError:  # pragma: no cover - 直跑时没有包上下文
    pass


# ---------------------------------------------------------------- 库路径
def resolve_db_path(explicit: str | None = None) -> str:
    """返回数据库路径（只解析，不建库、不迁移）。优先级见模块头契约。"""
    if explicit and str(explicit).strip():
        return str(Path(str(explicit)).expanduser())
    env_db = os.environ.get("XTMEM_DB_PATH", "").strip()
    if env_db:
        return str(Path(env_db).expanduser())
    home = Path(os.environ.get("XTMEM_HOME", "~/.xtmem")).expanduser()
    return str((home / "vector_store.db").resolve())


# ---------------------------------------------------------------- 引擎
def _engine_candidates() -> list[Path]:
    """可能包含 memory 包的目录（父目录，不是 memory 本身）。"""
    out: list[Path] = []
    for env_key in ("XTMEM_PLUGIN_ROOT", "XTMEM_HERMES_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(val).expanduser()
            out.append(p.parent if p.name == "memory" else p)
    here = Path(__file__).resolve()
    # 仓库布局：<repo>/src/scripts/xtmem_common.py → <repo>/src/hermes-plugin
    out.append(here.parents[1] / "hermes-plugin")
    # 兼容把 scripts 放在别处的旧布局
    out.append(here.parents[2] / "src" / "hermes-plugin")
    return out


def ensure_engine() -> None:
    """确保 `import memory` 可用。找不到就抛 RuntimeError（不静默降级）。"""
    try:
        import memory  # noqa: F401
        return
    except ImportError:
        pass
    for cand in _engine_candidates():
        if (cand / "memory" / "open_db.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            import memory  # noqa: F401
            return
    raise RuntimeError(
        "xtmem: 找不到 memory 引擎。请安装本项目的 wheel（pip install xtmem）"
        "或设置 XTMEM_PLUGIN_ROOT 指向包含 memory/ 的目录。已尝试: "
        + ", ".join(str(p) for p in _engine_candidates())
    )


def open_db(path: str, timeout: float = 30.0):
    """打开库连接（统一走引擎 open_db：schema 版本闸 + 降级自兜底）。"""
    ensure_engine()
    from memory.open_db import open_db as _open_db

    return _open_db(path, timeout=timeout)


def vector_store(path: str):
    """构造 VectorStore（supersede 的唯一写路径）。"""
    ensure_engine()
    from memory.vector_store import VectorStore

    return VectorStore(path)


# ---------------------------------------------------------------- CLI
def cli_argv() -> list[str]:
    """返回调用 xtmem CLI 的 argv 前缀（不含子命令）。

    ⚠️【不】直接返回 "xtmem" 字符串：老脚本把它拼成 `[python, "xtmem", ...]`，
      在 PATH 上是 console script 时会被解释器当成 .py 文件 → 必挂。
    """
    env_cli = os.environ.get("XTMEM_CLI", "").strip()
    if env_cli:
        p = Path(env_cli).expanduser()
        if p.exists():
            return [sys.executable, str(p)] if p.suffix.lower() == ".py" else [str(p)]
        # 显式配置了但不存在：仍然按可执行文件试一次，失败信息更直观
        return [str(p)]
    try:
        if importlib.util.find_spec("xtmem") is not None:
            return [sys.executable, "-m", "xtmem"]
    except Exception:
        pass
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "cli" / "xtmem.py",
                 here.parents[2] / "src" / "cli" / "xtmem.py"):
        if cand.exists():
            return [sys.executable, str(cand)]
    # 安装态兜底：交给解释器按模块找
    return [sys.executable, "-m", "xtmem"]


def store_via_cli(
    *,
    db_path: str,
    agent: str,
    namespace: str,
    text: str,
    importance: float,
    derived_from: str,
    source_ids: list[str],
    tier: str = "refined",
    track: str = "",
    tags: list[str] | str = (),
    project: str = "",
    visibility: str | None = None,
    operation_id: str | None = None,
    timeout: int = 180,
) -> dict:
    """调用 CLI store 落一条派生记忆；返回 CLI 的 JSON（失败给 ok=False 而非抛）。"""
    env = dict(os.environ)
    env["XTMEM_DB_PATH"] = db_path
    env["XTMEM_ACTOR"] = agent
    env["PYTHONUTF8"] = "1"
    # 🔴 子进程只该看到"作业自己的身份"，不能继承宿主的提权/宿主目录变量：
    #   - XTMEM_ADMIN / XTMEM_ADMIN_TOKEN 会让 CLI 以 __admin__ 视角读写（读闸全开、
    #     能跨域写），作业只需要本域普通身份；继承等于把宿主的管理员权限漏给后台任务。
    #   - HERMES_HOME / MIMOX_HOME / XTMEM_HERMES_HOME 会在某些 CLI 版本里参与库路径解析，
    #     与上面显式设置的 XTMEM_DB_PATH 冲突时可能被配置覆盖 ⇒ 写错库。
    #     本作业已把库路径算成绝对值，子进程不该再有第二个来源。
    for _k in ("XTMEM_ADMIN", "XTMEM_ADMIN_TOKEN", "XTMEM_TOKEN",
               "HERMES_HOME", "MIMOX_HOME", "XTMEM_HERMES_HOME"):
        env.pop(_k, None)
    if isinstance(tags, (list, tuple)):
        tag_str = ",".join(str(t) for t in tags if str(t).strip())
    else:
        tag_str = str(tags or "")
    args = cli_argv() + [
        "store", "--agent", agent, "--namespace", namespace,
        "--project", project or "", "--text", text,
        "--importance", f"{float(importance):.2f}",
        "--derived-from", derived_from,
        "--source-ids", ",".join(str(s) for s in source_ids if str(s).strip()),
        "--tier", tier, "--track", track or "", "--tags", tag_str,
    ]
    if visibility:
        args += ["--visibility", visibility]
    if operation_id:
        # 幂等键：同一批产物的重试必须复用同一 operation_id，否则"提交成功但响应丢了"
        # 的重试会写出两条同内容记忆（重复计费/重复召回）。
        args += ["--operation-id", operation_id]
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except Exception as exc:  # 超时/文件缺失等传输层问题
        return {"ok": False, "error": f"cli invocation failed: {type(exc).__name__}: {exc}"[:300]}
    try:
        return __import__("json").loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False,
                "error": ("bad cli output: " + (proc.stdout or proc.stderr or "")[:200])}


def import_cli():
    """把 xtmem CLI 当模块 import 进来（供需要直接调 cmd_* 的作业用）。

    为什么要这个而不是 `sys.path.insert(生产目录)`：CLI 的落点因部署而异
    （仓库内 src/cli / 安装后的 site-packages）。这里复用 cli_argv() 的同一套发现逻辑，
    找到 .py 就把它的目录加进 sys.path，找不到就按模块名 import。
    """
    argv = cli_argv()
    for i, tok in enumerate(argv):
        if tok.endswith(".py") and Path(tok).exists():
            d = str(Path(tok).resolve().parent)
            if d not in sys.path:
                sys.path.insert(0, d)
            break
    import xtmem  # noqa: F401

    return xtmem


def plugin_root() -> str:
    """返回含 `memory/` 包的目录（给需要直接 import 引擎模块的作业用）。

    解析顺序：$XTMEM_PLUGIN_ROOT / $XTMEM_HERMES_ROOT → 已安装的 memory 包 → 仓库内布局。
    ⚠️【不】返回生产绝对路径：公开仓不知道部署在哪，写死就是给下一台机器埋雷。
    """
    for env_key in ("XTMEM_PLUGIN_ROOT", "XTMEM_HERMES_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(val).expanduser()
            return str(p.parent if p.name == "memory" else p)
    try:
        spec = importlib.util.find_spec("memory")
        if spec and spec.origin:
            return str(Path(spec.origin).resolve().parent.parent)
    except Exception:
        pass
    for cand in _engine_candidates():
        if (cand / "memory" / "open_db.py").exists():
            return str(cand)
    return ""


def ensure_importable(sub: str = "") -> str:
    """把插件根（可再拼一个子路径）加进 sys.path，返回实际加入的目录（空=没找到）。"""
    root = plugin_root()
    if not root:
        return ""
    target = str(Path(root) / sub) if sub else root
    if os.path.isdir(target) and target not in sys.path:
        sys.path.insert(0, target)
    return target


# ---------------------------------------------------------------- namespace
def resolve_namespaces(conn, explicit: str | None = None) -> list[str]:
    """要处理的 namespace 列表。

    默认 = 库里真实存在的 active 域（不写死任何私人域名单）；显式参数/环境变量优先。
    """
    if explicit and str(explicit).strip():
        return [str(explicit).strip()]
    for key in ("XTMEM_DISTILL_NAMESPACE", "XTMEM_NAMESPACE"):
        val = os.environ.get(key, "").strip()
        if val:
            return [val]
    rows = conn.execute(
        "SELECT DISTINCT namespace FROM chunks "
        "WHERE state='active' AND namespace IS NOT NULL AND namespace<>'' ORDER BY namespace"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- 时区
def tz():
    """派生日界/月界使用的时区。

    默认 Asia/Shanghai（保留既有"月/日给人看"的口径）；$XTMEM_TZ 可换。
    zoneinfo 不可用时退回固定偏移 $XTMEM_TZ_OFFSET（小时，默认 8）。
    """
    name = os.environ.get("XTMEM_TZ", "").strip() or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        try:
            offset = float(os.environ.get("XTMEM_TZ_OFFSET", "8"))
        except ValueError:
            offset = 8.0
        return timezone(timedelta(hours=offset))


def env_list(name: str) -> list[str]:
    """逗号/分号分隔的环境变量 → 去空列表。"""
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
