# -*- coding: utf-8 -*-
"""test_xtmem_o6_distill_sync.py — O6 蒸馏链路守卫（P6.15，v1.3.1 §三 O6，2026-09-05，assistant-c）。

蒸馏空转根因（2026-09-05 查实）：profile cron 副本缺脚本（xtmem_distill_units_hourly.py /
xtmem_distill_llm_xtmem.py 未同步 → 每小时造窗任务 Script not found 连 fail；
xtmem-distill 跑旧版幂等锁死）→ judge 链路断供，personal 域 177 窗堆积。

守卫：jobs.json 在册 distill 系列脚本必须存在于 profile scripts 且与主树 sha256 EQ。
防复发——任何人改了主树脚本忘了同步 profile 副本，这里立刻红。
"""
from pathlib import Path as _P
_REPO = _P(__file__).resolve().parents[1]
import hashlib
import json
import os
from pathlib import Path

# 主树与 profile 副本路径（与生产一致）
MAIN_SCRIPTS = Path(_REPO / "scripts")
PROFILE_SCRIPTS = Path(_REPO / "profiles"/ "personal"/ "scripts")
JOBS_JSON = Path(_REPO / "profiles"/ "personal"/ "cron"/ "jobs.json")

# distill 家族必须在册且双树 EQ（主树新增/改名时同步改这里）
DISTILL_FAMILY = [
    "xtmem_distill.py",
    "xtmem_distill_units.py",
    "xtmem_distill_units_hourly.py",
    "xtmem_distill_llm.py",
    "xtmem_distill_llm_xtmem.py",
]


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def test_distill_family_present_in_profile():
    """distill 家族 5 件必须都在 profile scripts（缺 = 造窗/judge 断供复发）。"""
    for fn in DISTILL_FAMILY:
        assert (PROFILE_SCRIPTS / fn).exists(), f"profile 副本缺 {fn}——cron 会 Script not found"


def test_distill_family_sha_eq_main():
    """5 件双树 sha256 全等（主树改过没同步 = 跑旧码）。"""
    for fn in DISTILL_FAMILY:
        m = _sha(MAIN_SCRIPTS / fn)
        p = _sha(PROFILE_SCRIPTS / fn)
        assert m == p, f"{fn} 双树不一致 main={m[:10]} prof={p[:10]}"


def test_jobs_reference_only_existing_scripts():
    """jobs.json 引用的 distill 脚本必须都存在（防注册了跑不了）。"""
    with open(JOBS_JSON, encoding="utf-8") as f:
        jobs = json.load(f).get("jobs", [])
    for job in jobs:
        script = job.get("script") or ""
        if "distill" not in job.get("name", ""):
            continue
        assert (PROFILE_SCRIPTS / script).exists(), (
            f"job {job.get('name')} 引用 {script} 但 profile scripts 不存在"
        )
