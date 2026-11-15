"""列值域约束：一个文件里某列所有合法源行的取值集合。

每个文件、每列的约束 = 一个半开区间 ``[lo, hi)``（``hi=None`` 表示无界）
加上“null 是否可能”。int64 的全域是 ``[INT64_MIN, INT64_MAX+1)``，
字符串的全域是 ``["", +∞)``。分区字段给出原像，多个同源分区字段的原像
求交集（不是任选其一）；交集为空且 row_count>0 的目录是矛盾目录。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from .intervals import IntervalList, non_empty

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

TYPE_INT64 = "int64"
TYPE_STRING = "string"


@dataclass(frozen=True)
class ColumnConstraint:
    """某列在一个文件内的可能取值：区间 [lo, hi) + null 是否可能。"""

    type: str
    lo: Any
    hi: Optional[Any]
    null_allowed: bool

    def interval_empty(self) -> bool:
        return not non_empty(self.lo, self.hi)

    def domain_empty(self) -> bool:
        """没有任何合法源值（区间空且 null 也不可能）。"""
        return self.interval_empty() and not self.null_allowed

    def interval_list(self) -> IntervalList:
        if self.interval_empty():
            return []
        return [(self.lo, self.hi)]


def universe(type_name: str, nullable: bool) -> ColumnConstraint:
    if type_name == TYPE_INT64:
        return ColumnConstraint(TYPE_INT64, INT64_MIN, INT64_MAX + 1, nullable)
    return ColumnConstraint(TYPE_STRING, "", None, nullable)


def null_only(type_name: str) -> ColumnConstraint:
    """前三种变换把源 null 映为分区 null：原像恰为 {null}。"""
    if type_name == TYPE_INT64:
        return ColumnConstraint(TYPE_INT64, 0, 0, True)
    return ColumnConstraint(TYPE_STRING, "", "", True)


def exact(type_name: str, value: Any) -> ColumnConstraint:
    """单点原像 {value}。字符串单点的最小上界是 value 后接一个 U+0000。"""
    if type_name == TYPE_INT64:
        return ColumnConstraint(TYPE_INT64, value, value + 1, False)
    return ColumnConstraint(TYPE_STRING, value, value + "\x00", False)


def intersect(a: ColumnConstraint, b: ColumnConstraint) -> ColumnConstraint:
    """同源多个分区字段的原像交集。"""
    if a.type != b.type:  # pragma: no cover - 合同校验保证同型
        raise TypeError(f"约束类型不一致：{a.type} vs {b.type}")
    lo = max(a.lo, b.lo)
    hi = _min_hi(a.hi, b.hi)
    return ColumnConstraint(a.type, lo, hi, a.null_allowed and b.null_allowed)


def _min_hi(a: Optional[Any], b: Optional[Any]) -> Optional[Any]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def string_prefix_upper_bound(prefix: str) -> Optional[str]:
    """大于所有以 prefix 开头字符串的最小字符串；不存在则 None（无界）。

    码点层面操作，不触碰 UTF-8 字节：去掉尾部连续的 U+10FFFF 后把最后一个
    码点加一；整个串都是 U+10FFFF 时上界无界。
    """
    chars = list(prefix)
    while chars and chars[-1] == "\U0010ffff":
        chars.pop()
    if not chars:
        return None
    chars[-1] = chr(ord(chars[-1]) + 1)
    return "".join(chars)


def render_interval(type_name: str, lo: Any, hi: Optional[Any]) -> str:
    """人类可读的区间写法，供计划理由核对。"""
    if not non_empty(lo, hi):
        return "∅"
    if type_name == TYPE_INT64:
        return f"[{lo}, {hi - 1}]"
    hi_text = json.dumps(hi, ensure_ascii=False) if hi is not None else "+∞"
    return f"[{json.dumps(lo, ensure_ascii=False)}, {hi_text})"


def render_constraint(c: ColumnConstraint) -> str:
    interval = render_interval(c.type, c.lo, c.hi)
    null_text = "null 可能" if c.null_allowed else "null 不可能"
    return f"{interval}，{null_text}"


def constraint_to_json(c: ColumnConstraint) -> dict:
    return {
        "range": render_interval(c.type, c.lo, c.hi),
        "null_possible": c.null_allowed,
    }
