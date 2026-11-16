#!/usr/bin/env python3
"""终端入口：离线 JSONL 契约分流。

用法：python3 -I bin/cgate.py --manifest M --root URI [--out DIR] 输入.jsonl ...
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from contract_gate.cli import main

if __name__ == "__main__":
    sys.exit(main())
