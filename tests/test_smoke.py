"""XTMEM 快速自测冒烟套件。

验证基础 CLI 命令是否正常工作，且在无外部环境污染下可跑通。
强制固定 UTF-8 编码，防止在 Windows GBK 环境下解码崩溃。
"""
import os
import subprocess
import sys
from pathlib import Path

def test_cli_help():
    root = Path(__file__).parents[1]
    cli_py = root / "src" / "cli" / "xtmem.py"
    cmd = [sys.executable, str(cli_py), "--help"]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env
    )
    assert res.returncode == 0, f"xtmem --help failed: {res.stderr}"
    assert "usage:" in res.stdout.lower() or "commands:" in res.stdout.lower()
    print("Smoke check passed: xtmem --help is responsive.")

if __name__ == "__main__":
    test_cli_help()
