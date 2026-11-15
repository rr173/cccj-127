"""测试公共工具：夹具路径、UTC 时间窗构造、-I 下的工程根注入。

注意：所有期望值都在各测试文件里以手写常量给出（明确的 UTC 时间与秒数），
不调用生产展开函数来“生成”期望值。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIX = os.path.join(ROOT, "tests", "fixtures")
ERR = os.path.join(FIX, "errors")
TZ = "America/New_York"


def fixture(name: str) -> str:
    return os.path.join(FIX, name)


def error_fixture(name: str) -> str:
    return os.path.join(ERR, name)


def read_fixture(name: str) -> str:
    # 用 newline="" 保留 CRLF，解析器自己负责剥 \r
    with open(fixture(name), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def read_error(name: str) -> str:
    with open(error_fixture(name), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def utc(y, mo, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)


def window(y1, m1, d1, y2, m2, d2, h1=0, mi1=0, h2=0, mi2=0):
    return utc(y1, m1, d1, h1, mi1), utc(y2, m2, d2, h2, mi2)
