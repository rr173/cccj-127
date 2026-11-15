#!/usr/bin/env python3
"""-I 隔离下的终端入口：显式注入工程根后调用 hidden_partition.cli。

用法：
    python3 -I bin/hpq.py plan --manifest M.json --query Q.json --output plan.json
    python3 -I bin/hpq.py scan --manifest M.json --query Q.json \
        --output rows.jsonl --report report.json [--projection 2,1]
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hidden_partition.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
