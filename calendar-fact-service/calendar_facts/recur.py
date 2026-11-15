"""重复时刻生成引擎（本子集口径在此显式实现，不采用库的展开列表）。

关键口径：

* 按事件原时区的“日历字段”推进（加天/周/月），绝不用上一次 UTC +86400；
* 本地不存在的墙上时刻（春季缺口）跳过且不占 COUNT；
* 秋季回拨出现两次的歧义时刻只生成一次，取较早 UTC（fold=0）；
* COUNT 计算真实生成（且真实存在）的重复时刻，UNTIL 含端点；
* MONTHLY 的 BYMONTHDAY 支持月末倒数，不存在的日期跳过；
* WEEKLY 的周从周一开始，BYDAY 集合在每个周期内按周一→周日排序；
* 全天事件推进的是 date，定时事件推进的是带时区 datetime 的墙上字段。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .model import RRule
from .timeutil import local_to_utc, wall_key

# 无界系列展开的循环兜底，防病态输入；与 semantics.MAX_ITER_SAFETY 一致
MAX_ITER_SAFETY = 200_000


def _naive_wall(value):
    """比较用：去掉 tzinfo 的墙上字段。

    规则只按墙上日历推进；硬限制也由调用方换算成同一设备时区的墙上时间。
    UTC 起点（kind="utc"）的硬限制同样在调用方按墙上字段给出，因此按
    naive 比较即可，绝不能把两个不同 tzinfo 的感知时间直接比较。
    """

    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return value


def _past_limit(wall, hard_limit_wall) -> bool:
    if hard_limit_wall is None:
        return False
    return _naive_wall(wall) > _naive_wall(hard_limit_wall)


class Generated:
    """一个生成出的原定发生。

    wall：设备时区墙上时刻（datetime）或全天 date；
    utc：定时事件的原定 UTC（歧义取早）；全天为 None；
    missing：该墙上时刻在本地不存在（春季缺口），应跳过且不占 COUNT。
    """

    __slots__ = ("wall", "utc", "missing", "ambiguous")

    def __init__(self, wall, utc, missing: bool, ambiguous: bool):
        self.wall = wall
        self.utc = utc
        self.missing = missing
        self.ambiguous = ambiguous


def _advance_months(y: int, m: int, delta: int) -> tuple[int, int]:
    index = y * 12 + (m - 1) + delta
    return index // 12, index % 12 + 1


def _month_days(year: int, month: int, bymonthday: tuple[int, ...]) -> list[int]:
    last = calendar.monthrange(year, month)[1]
    days: list[int] = []
    for md in bymonthday:
        if md > 0:
            if md <= last:
                days.append(md)
        else:
            real = last + md + 1
            if real >= 1:
                days.append(real)
    return sorted(set(days))


def _wall_datetime(base: datetime, y: int, mo: int, d: int) -> datetime:
    return datetime(y, mo, d, base.hour, base.minute, base.second,
                    tzinfo=base.tzinfo)


def iter_rule(rule: RRule, start, zone: ZoneInfo,
              hard_limit_wall=None) -> Iterable[Generated]:
    """按 RRULE 从 DTSTART 生成原定时刻。

    hard_limit_wall：无 COUNT/UNTIL 时由调用方给定的墙上截止（含），
    到达后停止；有 COUNT/UNTIL 时忽略该参数。返回顺序即日历顺序。
    """

    bounded = rule.count is not None or rule.until is not None
    produced = 0
    iterations = 0

    if rule.freq == "DAILY":
        period = 0
        while True:
            iterations += 1
            if iterations > MAX_ITER_SAFETY:
                raise RuntimeError("重复展开超出安全迭代上限，输入可能异常")
            wall = start + timedelta(days=period * rule.interval)
            if rule.until is not None and _naive_wall(wall) > _naive_wall(rule.until):
                return
            if not bounded and _past_limit(wall, hard_limit_wall):
                return
            gen = _make_generated(wall, zone)
            period += 1
            if gen.missing:
                continue  # 缺口跳过，不占 COUNT
            produced += 1
            yield gen
            if rule.count is not None and produced >= rule.count:
                return

    elif rule.freq == "WEEKLY":
        # 周期序号 k 从 0 起；每个周期内按周一→周日给出候选
        start_monday = _monday_of(start)
        # 无 BYDAY 的 WEEKLY：只在 DTSTART 所在星期几重复
        byday = rule.byday if rule.byday else (start.weekday(),)
        k = 0
        while True:
            iterations += 1
            if iterations > MAX_ITER_SAFETY:
                raise RuntimeError("重复展开超出安全迭代上限，输入可能异常")
            period_monday = start_monday + timedelta(weeks=k * rule.interval)
            for wd in byday:
                if isinstance(start, datetime):
                    cand_date = period_monday + timedelta(days=wd)
                    wall = _wall_datetime(start, cand_date.year, cand_date.month,
                                          cand_date.day)
                else:
                    wall = period_monday + timedelta(days=wd)
                if _naive_wall(wall) < _naive_wall(start):
                    continue  # 首周里早于 DTSTART 的过滤项不生成
                if rule.until is not None and \
                        _naive_wall(wall) > _naive_wall(rule.until):
                    return
                if not bounded and _past_limit(wall, hard_limit_wall):
                    return
                gen = _make_generated(wall, zone)
                if gen.missing:
                    continue
                produced += 1
                yield gen
                if rule.count is not None and produced >= rule.count:
                    return
            k += 1
            if not bounded and hard_limit_wall is not None:
                # 下一周期的周一已越过截止，则后续周期不必再试
                next_monday = start_monday + timedelta(weeks=k * rule.interval)
                if isinstance(start, datetime):
                    probe = _wall_datetime(start, next_monday.year,
                                           next_monday.month, next_monday.day)
                else:
                    probe = next_monday
                if _past_limit(probe, hard_limit_wall):
                    return

    elif rule.freq == "MONTHLY":
        period = 0
        while True:
            iterations += 1
            if iterations > MAX_ITER_SAFETY:
                raise RuntimeError("重复展开超出安全迭代上限，输入可能异常")
            y, mo = _advance_months(start.year, start.month,
                                    period * rule.interval)
            period += 1
            if rule.bymonthday:
                days = _month_days(y, mo, rule.bymonthday)
            else:
                days = [start.day] if start.day <= calendar.monthrange(y, mo)[1] \
                    else []
            stop_after_period = False
            for d in days:
                if isinstance(start, datetime):
                    wall = _wall_datetime(start, y, mo, d)
                else:
                    wall = date(y, mo, d)
                if _naive_wall(wall) < _naive_wall(start):
                    continue
                if rule.until is not None and \
                        _naive_wall(wall) > _naive_wall(rule.until):
                    stop_after_period = True
                    break
                if not bounded and _past_limit(wall, hard_limit_wall):
                    return
                gen = _make_generated(wall, zone)
                if gen.missing:
                    continue
                produced += 1
                yield gen
                if rule.count is not None and produced >= rule.count:
                    return
            if stop_after_period:
                return
            if not bounded and hard_limit_wall is not None:
                ny, nmo = _advance_months(start.year, start.month,
                                          period * rule.interval)
                probe = date(ny, nmo, 1)
                if _past_limit(probe, hard_limit_wall):
                    return
    else:  # pragma: no cover - 语义层已拦截
        raise ValueError(f"未知 FREQ {rule.freq}")


def _monday_of(wall) -> date:
    d = wall.date() if isinstance(wall, datetime) else wall
    return d - timedelta(days=d.weekday())


def _make_generated(wall, zone: ZoneInfo) -> Generated:
    if isinstance(wall, date) and not isinstance(wall, datetime):
        return Generated(wall, None, False, False)
    # UTC 型 DTSTART（kind="utc"）的时间本身就是 UTC，直接作为原定 UTC；
    # 只有带设备时区的本地墙上时间才做缺口/歧义判定。
    if wall.utcoffset() == timedelta(0) and not hasattr(wall.tzinfo, "key"):
        return Generated(wall, wall, False, False)
    utc, ambiguous, missing = local_to_utc(wall, zone)
    return Generated(wall, utc, missing, ambiguous)


def dedupe_walls(values: list) -> tuple[list, list]:
    """墙上时刻集合去重，返回 (去重后列表, 被去重的原值列表)。保持首次出现顺序。"""

    seen: set = set()
    unique: list = []
    duplicates: list = []
    for v in values:
        key = wall_key(v)
        if key in seen:
            duplicates.append(v)
        else:
            seen.add(key)
            unique.append(v)
    return unique, duplicates
