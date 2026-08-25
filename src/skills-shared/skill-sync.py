# -*- coding: utf-8 -*-
"""skill-sync.py —— XTMEM 资产单源分发：技能母本 → 多端投放（v1）。

- 母本库：<PROJECT>/skills-shared（git 管理，唯一权威），只扫母本声明的 skill。
- 方向：单向（母本 → 各端），各端改动不回写。
- 台账：<母本>/.sync-state.json（每 skill 每端的 dir_hash，增量投放，无变更不写）。
- 保护：目标同名目录若不在台账管理内 → 跳过+警告；管理内 → 按母本覆盖（首次先自动备份）。
- 零依赖 Python 3.11；环境可覆盖：XTMEM_SKILLS_SHARED / XTMEM_SKILL_ROOTS_JSON。
  XTMEM_SKILL_ROOTS_JSON 形如 {"workbuddy":"path","hermes":"path","dsh":"path"}（测试用）。

命令：
  python skill-sync.py status
  python skill-sync.py sync [--target T1,T2] [--dry-run]
  python skill-sync.py pull <target> <skill>
  python skill-sync.py add <skill>          # 母本已有、台账补登记（无需，sync 自动登记）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SHARED = Path(os.environ.get("XTMEM_SKILLS_SHARED", "skills-shared"))
STATE = SHARED / ".sync-state.json"
DEFAULT_ROOTS = {
    "workbuddy": os.environ.get("XTMEM_SKILL_ROOT_WORKBUDDY", "~/.workbuddy/skills"),
    "hermes": os.environ.get("XTMEM_SKILL_ROOT_HERMES", "<HERMES_HOME>/skills"),
    "dsh": os.environ.get("XTMEM_SKILL_ROOT_DSH", "~/.dsh/skills"),
    "hermes-profile": os.environ.get("XTMEM_SKILL_ROOT_HERMES_PROFILE", "<HERMES_HOME>/profiles/xuantie/skills"),
}
ROOTS = {**DEFAULT_ROOTS, **(json.loads(os.environ.get("XTMEM_SKILL_ROOTS_JSON", "{}")) if os.environ.get("XTMEM_SKILL_ROOTS_JSON") else {})}


def dir_hash(root: Path) -> str:
    h = hashlib.sha256()
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith((".pyc", ".swp")):
            files.append(p)
    for p in files:
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {"version": 1, "created": datetime.now().strftime("%Y-%m-%d"), "master": str(SHARED),
            "targets": {k: {"path": v, "status": "pending"} for k, v in ROOTS.items() if k != "zcode"},
            "skills": {}}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def skills() -> list[Path]:
    out = []
    for d in sorted(SHARED.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists() and d.name != ".git":
            out.append(d)
    return out


def backup_target(root: Path, name: str) -> Path:
    bak = SHARED / ".backup" / datetime.now().strftime("%Y%m%d")
    dst = bak / (root.name + "-" + name)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(root / name, dst)
    return dst


def sync_one(prev: dict, name: str, target: str, root: Path, *, dry: bool, do_backup: bool = True, force: bool = False) -> dict:
    src = SHARED / name
    tgt = root / name
    src_h = dir_hash(src)
    prev_h = prev.get(target)
    if src_h == prev_h and tgt.exists():
        return {"skill": name, "target": target, "action": "noop", "hash": src_h[:12]}
    if tgt.exists():
        managed = bool(prev and prev.get(target))
        if not managed and not force:
            return {"skill": name, "target": target, "action": "skip-existing-unmanaged", "hash": src_h[:12]}
        if do_backup:
            backup_target(root, name)
    copies = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_file() and not p.name.endswith((".pyc", ".swp")):
            copies.append((src / rel, tgt / rel))
    if dry:
        return {"skill": name, "target": target, "action": "would-sync", "hash": src_h[:12], "files": len(copies)}
    if tgt.exists():
        shutil.rmtree(tgt)
    for s, d in copies:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return {"skill": name, "target": target, "action": "synced", "hash": src_h[:12], "files": len(copies)}


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    st = load_state()
    all_skills = [d.name for d in skills()]
    if not all_skills:
        print(json.dumps({"ok": False, "error": "母本库无 skill（需要 SKILL.md）"}, ensure_ascii=False))
        return 1
    if cmd == "status":
        rows = []
        for name in all_skills:
            src_h = dir_hash(SHARED / name)[:12]
            for target, root_s in ROOTS.items():
                root = Path(root_s)
                tgt = root / name
                if not tgt.exists():
                    rows.append({"skill": name, "target": target, "state": "missing"})
                else:
                    cur = dir_hash(tgt)[:12]
                    rows.append({"skill": name, "target": target,
                                 "state": "aligned" if cur == src_h else "drift",
                                 "hash": cur})
        print(json.dumps({"ok": True, "skills": all_skills, "rows": rows}, ensure_ascii=False))
        return 0
    if cmd in ("sync", "add"):
        targets = [t.strip() for t in args[args.index("--target") + 1].split(",")] if "--target" in args else list(ROOTS.keys())
        dry = "--dry-run" in args
        results = []
        for name in all_skills:
            entry = st.setdefault(name, {})
            skill_meta = st.setdefault("skills", {}).setdefault(name, {"version": "1.0.0", "master_mtime": None, "deployed": {}})
            skill_meta.setdefault("deployed_hashes", {})
            for target in targets:
                if target not in ROOTS:
                    results.append({"skill": name, "target": target, "action": "unknown-target"})
                    continue
                r = sync_one(entry, name, target, Path(ROOTS[target]), dry=dry)
                if r["action"] == "skip-existing-unmanaged" and not dry:
                    skill_meta["deployed"].setdefault(target, False)
                if r["action"] in ("synced", "noop") and not dry:
                    entry[target] = dir_hash(SHARED / name)
                    skill_meta["deployed"][target] = True
                    skill_meta["deployed_hashes"][target] = entry[target]
                    st.setdefault("targets", {}).setdefault(target, {}).update({"path": str(ROOTS[target]), "status": "synced"})
                results.append(r)
        save_state(st)
        print(json.dumps({"ok": True, "dry": dry, "results": results}, ensure_ascii=False))
        return 0
    if cmd == "pull":
        target, name = args[1], args[2]
        if name not in all_skills or target not in ROOTS:
            print(json.dumps({"ok": False, "error": f"bad pull: {target}/{name}"}, ensure_ascii=False))
            return 1
        entry = st.setdefault(name, {})
        skill_meta = st.setdefault("skills", {}).setdefault(name, {"version": "1.0.0", "master_mtime": None, "deployed": {}})
        skill_meta.setdefault("deployed_hashes", {})
        r = sync_one(entry, name, target, Path(ROOTS[target]), dry=False, force=True)
        if r["action"] in ("synced", "noop"):
            entry[target] = dir_hash(SHARED / name)
            skill_meta["deployed"][target] = True
            skill_meta["deployed_hashes"][target] = entry[target]
            st.setdefault("targets", {}).setdefault(target, {}).update({"path": str(ROOTS[target]), "status": "synced"})
        save_state(st)
        print(json.dumps({"ok": True, "result": r}, ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "error": f"unknown cmd {cmd}"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
