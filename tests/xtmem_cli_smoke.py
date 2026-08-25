# -*- coding: utf-8 -*-
"""xtmem_cli_smoke.py — XTMEM CLI 回归冒烟（阶段 2.1 永久测试资产）。

覆盖：migrate / store(去重) / lookup --cross / forget / export / import(防循环) /
      agents / agents.md / reembed(--limit 1，Ollama 不在线则 WARN 不 fail)。
数据保护：全部写操作走临时 DB 副本（从 XTMEM_CANONICAL_DB 只读拷贝，默认生产基准库），
          绝不写生产库。zcode 文件轨用临时目录。

用法：python tests/xtmem_cli_smoke.py   （可选 env：XTMEM_CLI / XTMEM_CANONICAL_DB）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PYTHON = sys.executable
CLI = os.environ.get("XTMEM_CLI", r"E:\2222\3333\mimox-data\xtmem\xtmem.py")
CANONICAL = os.environ.get(
    "XTMEM_CANONICAL_DB",
    r"E:\2222\3333\mimox-data\.hermes\memory\vector_store.db",
)
CHECK = {"migrate": False, "store": False, "lookup": False, "forget": False,
         "export": False, "import": False, "dedupe": False, "agents": False,
         "agents_md": False, "reembed": "skip", "whisper": False, "visibility": False, "relations": False, "messages": False, "reflect": False, "reveal": False}


def run(*argv, timeout=120):
    proc = subprocess.run([PYTHON, CLI, *argv], capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout, check=False)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {"ok": False, "error": "bad output: " + (proc.stdout or "")[:200]}


def main() -> int:
    if not os.path.exists(CANONICAL):
        print("SKIP: canonical DB missing:", CANONICAL)
        return 0
    with tempfile.TemporaryDirectory(prefix="xtmem-cli-") as td:
        td = Path(td)
        db = td / "vector_store.db"
        shutil.copyfile(CANONICAL, db)
        zroot = td / "zcode"
        # fake zcode project: name demo + hash16
        import hashlib
        name = "demo"
        h16 = hashlib.sha256(name.lower().encode()).hexdigest()[:16]
        memdir = zroot / f"{name}-{h16}" / "memory"
        memdir.mkdir(parents=True)
        env = {**os.environ, "XTMEM_DB_PATH": str(db), "XTMEM_ZCODE_MEMORIES": str(zroot)}
        import os as _os
        old = dict(_os.environ)
        _os.environ.update(env)
        try:
            r = run("migrate")
            CHECK["migrate"] = bool(r.get("ok"))
            r = run("store", "--text", "CLI回归：玄铁喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6")
            store_id = r.get("id")
            CHECK["store"] = bool(r.get("ok") and store_id)
            r2 = run("store", "--text", "CLI回归：玄铁喜欢桃子汽水", "--agent", "dsh", "--importance", "0.6")
            CHECK["dedupe"] = bool(r2.get("ok") and r2.get("skipped"))
            r = run("lookup", "--query", "桃子汽水", "--top", "3", "--cross")
            CHECK["lookup"] = bool(r.get("ok") and r.get("count", 0) >= 1)
            r = run("forget", "--id", str(store_id))
            CHECK["forget"] = bool(r.get("ok") and r.get("removed", 0) >= 1)
            # 文件轨：先 export（含已存行），再造新文件 import
            r = run("export")
            CHECK["export"] = bool(r.get("ok"))
            memfile = memdir / "mem-zc-fresh.md"
            memfile.write_text(
                "---\nid: mem-zc-fresh\nnamespace: zcode\nproject: demo\n"
                "importance: 0.5\ntimestamp: 1787100000\ntags: fresh\ntrack: default\n---\n\n"
                "文件轨导入回归：Z老师记住 [[部署偏好]] 托管在 E 盘。\n", encoding="utf-8")
            r = run("import")
            CHECK["import"] = bool(r.get("ok") and r.get("added", 0) >= 1)
            r2 = run("import")
            CHECK["dedupe"] = CHECK["dedupe"] and bool(r2.get("duplicates", 0) >= 1)
            r = run("agents")
            CHECK["agents"] = bool(r.get("ok"))
            agents_md = td / "agents.md"
            r = run("agents.md", "--out", str(agents_md))
            CHECK["agents_md"] = bool(r.get("ok") and agents_md.exists())
            try:
                r = run("reembed", "--limit", "1", timeout=180)
                CHECK["reembed"] = "done" if r.get("ok") else "warn"
            except Exception:
                CHECK["reembed"] = "warn"
            # ── 玩法增强框架（E1/E2/E3/E6）冒烟 ──
            r = run("whisper", "--agent", "zhanglaoshi", "--namespace", "zcode",
                    "--text", "框架冒烟：Z老师的秘密只有自己可见")
            CHECK["whisper"] = bool(r.get("ok") and r.get("visibility") == "private")
            wid = r.get("id")
            r = run("lookup", "--query", "框架冒烟", "--top", "5", "--as", "xuantie_lark")
            CHECK["visibility"] = bool(r.get("ok") and r.get("count", 0) == 0)
            r = run("lookup", "--query", "框架冒烟", "--top", "5", "--as", "zcode")
            CHECK["visibility"] = CHECK["visibility"] and bool(r.get("count", 0) >= 1)
            r = run("relations", "set", "--agent-a", "xuantie_lark", "--agent-b", "zhanglaoshi",
                    "--sentiment", "-0.4", "--trust", "0.3", "--note", "框架冒烟：骗过一次")
            CHECK["relations"] = bool(r.get("ok"))
            r = run("relations", "summary", "--agent", "xuantie_lark")
            CHECK["relations"] = CHECK["relations"] and bool(r.get("count", 0) >= 1)
            r = run("msg", "send", "--to", "xuantie_lark", "--from", "zhanglaoshi",
                    "--text", "框架冒烟：你的代码又出bug了")
            CHECK["messages"] = bool(r.get("ok"))
            r = run("msg", "inbox", "--as", "xuantie_lark")
            CHECK["messages"] = CHECK["messages"] and bool(r.get("count", 0) >= 1)
            r = run("reflect", "--agent", "xuantie_lark", "--text", "框架冒烟：被diss后我该更谨慎")
            CHECK["reflect"] = bool(r.get("ok") and r.get("visibility") == "private")
            CHECK["reveal"] = False
            if wid:
                r = run("reveal", "--id", str(wid), "--to", "xuantie_lark")
                CHECK["reveal"] = bool(r.get("ok") and r.get("visibility") == "shared-with:xuantie_lark")
        finally:
            _os.environ.clear()
            _os.environ.update(old)
        all_ok = all(v is True for k, v in CHECK.items() if k != "reembed")
        print(json.dumps({"ok": all_ok, "checks": CHECK}, ensure_ascii=False))
        return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
