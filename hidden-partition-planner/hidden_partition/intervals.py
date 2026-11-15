"""半开区间列表运算。

区间写作 ``(lo, hi)``，含 lo 不含 hi；``hi is None`` 表示上界无界。
列表内区间按 lo 升序且互不相交。int64 与字符串共用同一套比较：
Python 的 ``<`` 对 int 是数值序、对 str 是 Unicode 码点序（不归一化），
与本模块合同一致。lo 不允许为 None；空串是最小字符串，所以字符串
区间的 lo 至少是 ``""``。
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

Interval = Tuple[Any, Optional[Any]]
IntervalList = List[Interval]


def non_empty(lo: Any, hi: Optional[Any]) -> bool:
    return hi is None or lo < hi


def _hi_le(a: Optional[Any], b: Optional[Any]) -> bool:
    """上界 a <= b；None 视为 +∞。"""
    if a is None:
        return b is None
    if b is None:
        return False
    return a <= b


def _min_hi(a: Optional[Any], b: Optional[Any]) -> Optional[Any]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def intersect(a: IntervalList, b: IntervalList) -> IntervalList:
    """两个有序不相交区间列表的交集。"""
    out: IntervalList = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])  # lo 都不是 None
        hi = _min_hi(a[i][1], b[j][1])
        if non_empty(lo, hi):
            out.append((lo, hi))
        if _hi_le(a[i][1], b[j][1]):
            i += 1
        else:
            j += 1
    return out


def subtract(a: IntervalList, b: IntervalList) -> IntervalList:
    """``a \ b``：从 a 的每个区间里挖掉与 b 重叠的部分。"""
    out: IntervalList = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi is not None and bhi <= cur:
                continue  # b 区间完全在 cur 之前
            if hi is not None and blo >= hi:
                break  # b 区间完全在本区间之后
            if blo > cur:
                out.append((cur, min(blo, hi) if hi is not None else blo))
            if bhi is None:
                cur = None  # 剩余部分被无界区间吞掉
                break
            cur = max(cur, bhi)
            if hi is not None and cur >= hi:
                cur = None
                break
        if cur is not None and non_empty(cur, hi):
            out.append((cur, hi))
    return out


def any_interval(ivs: IntervalList) -> bool:
    return len(ivs) > 0
