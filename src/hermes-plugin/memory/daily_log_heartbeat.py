#!/usr/bin/env python3
"""每日日志心跳 — 每2小时写一条时间戳到当日日志，让agent知道时间在流逝。"""
import sys
from datetime import datetime

import os
memories_dir = os.path.join(os.environ.get("HERMES_HOME", ""), "memories")
today = datetime.now().strftime("%Y-%m-%d")
log_file = f"{memories_dir}/{today}.md"

now = datetime.now().strftime("%H:%M")
entry = f"- {now} 心跳\n"

try:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[{now}] heartbeat logged")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
