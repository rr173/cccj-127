#!/usr/bin/env python3
"""-I 隔离下的终端入口：显式注入工程根后调用 ipfix_offline.cli。

用法：
    python3 -I bin/ipfixctl.py import --input cap.jsonl --db result.db
    python3 -I bin/ipfixctl.py list --db result.db --template 256
    python3 -I bin/ipfixctl.py trace --db result.db --record 2 --field 1
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ipfix_offline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
