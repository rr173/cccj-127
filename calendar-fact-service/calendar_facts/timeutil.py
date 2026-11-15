"""时间值解析与 IANA 时区换算。

时区换算全部委托标准库 zoneinfo（系统 tzdata），而“本地墙上时刻是否存在、
是否歧义、跳过后如何取点、回拨取哪一次”这些本子集口径，在本模块显式实现，
不直接采用任何日历库给出的展开列表。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DUR_RE = re.compile(
    r"^\+?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


class TimeParseError(ValueError):
    pass


def get_zone(tzname: str) -> ZoneInfo:
    try:
        return ZoneInfo(tzname)
    except Exception as exc:  # ZoneInfoNotFoundError 等
        raise TimeParseError(f"未知 IANA 时区 {tzname!r}: {exc}") from exc


def parse_date_value(text: str) -> date:
    m = _DATE_RE.match(text)
    if not m:
        raise TimeParseError(f"非法 DATE 值 {text!r}（应为 YYYYMMDD）")
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise TimeParseError(f"非法日历日期 {text!r}: {exc}") from exc


def parse_datetime_value(text: str, tzid: str | None,
                         zone: ZoneInfo) -> tuple[datetime, str]:
    """解析 YYYYMMDDTHHMMSS[Z]。

    返回 (datetime, kind)：带 Z 得到 UTC 感知时间（kind="utc"）；
    带 TZID 得到设备时区感知的墙上时间（kind="tz"，fold 先置 0，
    歧义/不存在的判定交给 local_to_utc 的调用方）。
    无 Z 且无 TZID 的浮动时间按子集拒绝。
    """

    m = _DT_RE.match(text)
    if not m:
        raise TimeParseError(
            f"非法日期时间值 {text!r}（应为 YYYYMMDDTHHMMSS 或 YYYYMMDDTHHMMSSZ）")
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    try:
        naive = datetime(y, mo, d, hh, mm, ss)
    except ValueError as exc:
        raise TimeParseError(f"非法日期时间 {text!r}: {exc}") from exc

    if text.endswith("Z"):
        if tzid is not None:
            raise TimeParseError(f"UTC 值 {text!r} 不允许同时携带 TZID")
        return naive.replace(tzinfo=UTC), "utc"
    if tzid is None:
        raise TimeParseError(
            f"浮动时间 {text!r} 不被接收：定时事件必须带 Z 或 TZID")
    return naive.replace(tzinfo=zone), "tz"


def parse_duration(text: str) -> int:
    """DURATION 只接受正的 PT 小时/分钟/秒组合，返回真实经过秒数。"""

    m = _DUR_RE.match(text)
    if not m:
        raise TimeParseError(
            f"不支持的 DURATION {text!r}：只接受正的 PT 小时/分钟/秒组合")
    if m.group("w") or m.group("d"):
        raise TimeParseError(
            f"不支持的 DURATION {text!r}：不接受日/周分量，定时事件只用 PTnHnMnS")
    parts = {k: int(v) for k, v in m.groupdict().items()
             if v is not None and k in ("h", "m", "s")}
    if not parts:
        raise TimeParseError(f"DURATION {text!r} 没有任何时间分量")
    seconds = parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)
    if seconds <= 0:
        raise TimeParseError(f"DURATION {text!r} 必须为正")
    return seconds


def local_wall_exists(wall: datetime, zone: ZoneInfo) -> bool:
    """墙上时刻在该时区是否真实存在（春季跳变缺口内为 False）。

    判定方法：fold=0 投影到 UTC 再转回该时区，墙上字段不能重合才算存在。
    歧义时刻（回拨两次）的早次投影能回到同一墙上时间，因此仍为存在。
    """

    return _roundtrip_ok(wall.replace(fold=0), zone)


def _roundtrip_ok(wall: datetime, zone: ZoneInfo) -> bool:
    as_utc = wall.astimezone(UTC)
    back = as_utc.astimezone(zone)
    return back.replace(tzinfo=None) == wall.replace(tzinfo=None)


def local_to_utc(wall: datetime, zone: ZoneInfo) -> tuple[datetime, bool, bool]:
    """设备时区墙上时刻 -> UTC。

    返回 (utc_datetime, is_ambiguous, is_missing)。fold0/fold1 偏移不同在
    两种场景都会出现，必须再用 fold0 的 UTC 回环区分：
    * 正常时刻：回环一致且两 fold 偏移相同；
    * 歧义（回拨出现两次）：两 fold 偏移不同，但 fold0 回环仍等于原墙上时间，
      取较早的 UTC，即 fold=0；
    * 不存在（春季缺口）：fold0 投影到 UTC 再转回得到的是另一个墙上时间，
      返回 is_missing=True，调用方必须跳过该发生，不能把它计入 COUNT。
    """

    early = wall.replace(fold=0)
    if not _roundtrip_ok(early, zone):
        return early.astimezone(UTC), False, True
    late = wall.replace(fold=1)
    if early.utcoffset() != late.utcoffset():
        # fold=0 偏移较大（较早）的那一次，对应较早 UTC
        return early.astimezone(UTC), True, False
    return early.astimezone(UTC), False, False


def wall_to_utc_actual(wall: datetime, zone: ZoneInfo) -> datetime:
    """把一个“实际要发生”的本地起点换算成 UTC（歧义取早，缺口不得进入这里）。"""

    utc, _amb, missing = local_to_utc(wall, zone)
    if missing:
        raise TimeParseError(
            f"本地时刻 {wall.isoformat()} 在 {zone.key} 不存在，不能作为实际区间起点")
    return utc


def date_midnight_utc(d: date, zone: ZoneInfo) -> datetime:
    """全天事件的日期边界 -> UTC。

    午夜若在回拨中出现两次，取较早 UTC（fold=0）；午夜落在缺口的时区极罕见，
    遇到则按缺口报错，由上层拒绝，而不是静默写死 86400 秒。
    """

    wall = datetime(d.year, d.month, d.day, tzinfo=zone)
    utc, _amb, missing = local_to_utc(wall, zone)
    if missing:
        raise TimeParseError(
            f"全天日期 {d.isoformat()} 的午夜在 {zone.key} 不存在，无法换算边界")
    return utc


def wall_key(value) -> tuple:
    """原定时刻的去重/比对键：按“表示类型 + 墙上字段”，不做 UTC 归一。

    UTC 与带时区的两种定时值靠各自的 kind（语义层单独校验表示类型一致），
    键本身只区分定时/全天以及墙上日历字段。
    """

    if isinstance(value, datetime):
        return (0, value.year, value.month, value.day,
                value.hour, value.minute, value.second)
    return (1, value.year, value.month, value.day)


def unescape_text(value: str) -> str:
    """TEXT 值反转义：\\\\ \\, \\; \\n（含 \\N）。"""

    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("\\", ",", ";"):
                out.append(nxt)
            elif nxt in ("n", "N"):
                out.append("\n")
            else:
                # 子集里没有其他合法转义；原样保留，交给上层决定是否报错
                out.append(ch)
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def iso_utc(dt: datetime) -> str:
    assert dt.tzinfo is not None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: str) -> datetime:
    """CLI 入参：接受 ...Z 或 +00:00 的 UTC 时间窗。"""

    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        raise TimeParseError(f"查询窗时间 {text!r} 必须带 UTC 偏移（用 Z 或 +00:00）")
    return dt.astimezone(UTC)
