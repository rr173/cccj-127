#!/usr/bin/env python3
"""离线展开入口包装：在 -I 隔离模式下显式注入工程根目录后调用 CLI。

为什么需要它：python -I 不把当前目录/PYTHONPATH 加入 sys.path（防止夹具目录
里被植入的 json.py 等被 import），而本工程又没有可安装的第三方包。因此由这个
与夹具分目录存放的可信脚本显式注入工程根。

用法：
    python3 -I bin/run_expand.py --input <ics> --tz <IANA> \
        --window-start <UTC> --window-end <UTC> --out <jsonl> --report <json>
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calendar_facts.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
