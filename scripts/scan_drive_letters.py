# -*- coding: utf-8 -*-
"""G-02 盘符/实机路径扫描门禁（XTMEM-public 推送前必跑）。

扫描仓库全部文本文件，禁止出现：
  - Windows 盘符绝对路径（形如 E:/2222/3333、F:/Ryuuzaki、C:/Users/<用户名>）——
    排除协议（http:// https://）与通用安装/系统路径（Program Files / Windows / ProgramData / Users/Public）；
  - 本机特征目录名（git项目源码 / 2222/3333）。

用法：
    python scripts/scan_drive_letters.py [<repo_root>]   # 默认本文件上一级
退出码：0=通过；1=发现残留（打印 file:line）。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 盘符绝对路径 X:\ 或 X:/ （(?!/) 排除 http:// https:// 协议）
_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)")
# 通用安装/系统路径（非本机私有），行内含即放行
_ALLOW_PREFIXES = (
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\Windows",
    "C:\\ProgramData",
    "C:\\Users\\Public",
)
# 本机特征目录名（带不带盘符都禁）
_FEATURE = re.compile(r"git项目源码|2222[\\/]3333")
_SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".eot", ".ttf",
              ".pyc", ".pdf", ".lock", ".db", ".sqlite", ".exe", ".zip", ".gz"}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "__pycache__", ".pytest_cache"}


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in _SKIP_EXTS:
                continue
            yield os.path.join(dirpath, fn)


def scan(root):
    hits = []
    self_fp = os.path.abspath(__file__)
    for fp in _walk(root):
        if os.path.abspath(fp) == self_fp:
            continue  # 门禁自身含示例盘符，跳过
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for ln, line in enumerate(f, 1):
                    if _DRIVE.search(line):
                        # 通用安装/系统路径（Program Files / Windows / ProgramData / Users/Public）放行
                        if any(k in line for k in ("Program Files", "ProgramData", "Users\\Public", "C:\\Windows", "C:\\WINDOWS")):
                            continue
                        hits.append((fp, ln, line.rstrip()[:160]))
                    elif _FEATURE.search(line):
                        hits.append((fp, ln, line.rstrip()[:160]))
        except OSError:
            continue
    return hits


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = sys.argv[1] if len(sys.argv) > 1 else ROOT
    hits = scan(root)
    if hits:
        print("G-02 FAIL: %d real-path residue(s) in %s" % (len(hits), root))
        for fp, ln, text in hits:
            print("  %s:%d  %s" % (os.path.relpath(fp, root), ln, text))
        return 1
    print("G-02 OK: no drive-letter / real-machine path residue (%s)" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
