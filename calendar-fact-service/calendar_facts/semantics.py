"""组件级语义解析与白名单校验。

凡是“可能影响时间口径”的属性都在这里过白名单：子集之外的属性名直接报错，
而不是静默丢弃。RRULE 的允许组合、COUNT/UNTIL 互斥、过滤项与 DTSTART 的
一致性、DURATION 的形式、时间值类型（UTC/TZID/DATE）也都在本模块保证。
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from .errors import ProblemCollector, loc
from .model import DateTimeValue, ParsedEvent, Property, RRule, VCalendar, VEvent
from .timeutil import (
    parse_date_value,
    parse_datetime_value,
    parse_duration,
    unescape_text,
)

# 组件内允许出现的全部属性（子集白名单）
ALLOWED_PROPS = {
    "UID", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE",
    "RECURRENCE-ID", "STATUS", "SUMMARY",
}
# 各属性允许携带的参数名白名单
ALLOWED_PARAMS = {
    "DTSTART": {"VALUE", "TZID"},
    "DTEND": {"VALUE", "TZID"},
    "RDATE": {"VALUE", "TZID"},
    "EXDATE": {"VALUE", "TZID"},
    "RECURRENCE-ID": {"VALUE", "TZID"},
    "SUMMARY": {"LANGUAGE"},
}
CALENDAR_ALLOWED = {"VERSION", "PRODID", "CALSCALE"}
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_MONTH_DAY = 31
MAX_ITER_SAFETY = 200_000  # 无界系列展开的循环兜底，防病态输入


def _check_params(prop: Property, problems: ProblemCollector) -> None:
    allowed = ALLOWED_PARAMS.get(prop.name, set())
    for pname in prop.params:
        if pname not in allowed:
            problems.add(
                f"{loc(None, prop.line_start, prop.line_end)}：属性 {prop.name} "
                f"携带子集不支持的参数 {pname}")


def parse_time_prop(prop: Property, zone: ZoneInfo,
                    problems: ProblemCollector) -> DateTimeValue | None:
    """按 VALUE/TZID 参数解析单个时间属性。"""

    value_param = prop.params.get("VALUE", "").upper()
    tzid = prop.params.get("TZID")
    where = loc(None, prop.line_start, prop.line_end)
    try:
        if value_param == "DATE":
            if tzid is not None:
                problems.add(f"{where}：DATE 型 {prop.name} 不允许 TZID")
            return DateTimeValue(parse_date_value(prop.value), "date", None)
        if value_param not in ("", "DATE-TIME"):
            problems.add(f"{where}：{prop.name} 不支持 VALUE={value_param}")
            return None
        dt, kind = parse_datetime_value(prop.value, tzid, zone)
        return DateTimeValue(dt, kind, tzid)
    except ValueError as exc:
        problems.add(f"{where}：{exc}")
        return None


def parse_rrule(prop: Property, master_dt: DateTimeValue, zone: ZoneInfo,
                problems: ProblemCollector) -> RRule | None:
    where = loc(None, prop.line_start, prop.line_end)
    raw_parts = [p for p in prop.value.split(";") if p != ""]
    kv: dict[str, str] = {}
    for part in raw_parts:
        if "=" not in part:
            problems.add(f"{where}：RRULE 片段 {part!r} 缺少 '='")
            continue
        k, v = part.split("=", 1)
        ku = k.upper()
        if ku in kv:
            problems.add(f"{where}：RRULE 键 {ku} 重复")
        kv[ku] = v
    if problems.problems and any(where in p for p in problems.problems):
        return None

    if "FREQ" not in kv:
        problems.add(f"{where}：RRULE 缺少 FREQ")
        return None
    freq = kv.pop("FREQ").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        problems.add(f"{where}：不支持的 FREQ={freq}（仅 DAILY/WEEKLY/MONTHLY）")
        return None

    rule_kwargs: dict = {"freq": freq}
    interval = 1
    if "INTERVAL" in kv:
        raw = kv.pop("INTERVAL")
        if not raw.isdigit() or int(raw) < 1:
            problems.add(f"{where}：INTERVAL 必须为正整数，收到 {raw!r}")
        else:
            interval = int(raw)
    rule_kwargs["interval"] = interval

    if "COUNT" in kv and "UNTIL" in kv:
        problems.add(f"{where}：COUNT 与 UNTIL 不能同时出现")
    count = None
    if "COUNT" in kv:
        raw = kv.pop("COUNT")
        if not raw.isdigit() or int(raw) < 1:
            problems.add(f"{where}：COUNT 必须为正整数，收到 {raw!r}")
        else:
            count = int(raw)
    rule_kwargs["count"] = count

    until = None
    if "UNTIL" in kv:
        raw = kv.pop("UNTIL")
        try:
            if master_dt.kind == "date":
                if "T" in raw or raw.endswith("Z"):
                    raise ValueError("全天系列的 UNTIL 必须是 DATE（YYYYMMDD）")
                until = parse_date_value(raw)
            else:
                # 本地定时重复的 UNTIL 必须写 UTC
                if not raw.endswith("Z"):
                    raise ValueError("定时系列的 UNTIL 必须为 UTC（YYYYMMDDTHHMMSSZ）")
                dt, kind = parse_datetime_value(raw, None, zone=zone)
                until = dt
        except ValueError as exc:
            problems.add(f"{where}：{exc}")
    rule_kwargs["until"] = until

    byday: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    if "BYDAY" in kv:
        raw = kv.pop("BYDAY")
        tokens = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if freq != "WEEKLY":
            problems.add(f"{where}：BYDAY 只允许出现在 FREQ=WEEKLY")
        days: list[int] = []
        for tok in tokens:
            if tok not in WEEKDAYS:
                problems.add(f"{where}：非法 BYDAY 项 {tok!r}")
                continue
            days.append(WEEKDAYS[tok])
        if len(days) != len(set(days)):
            problems.add(f"{where}：BYDAY 星期集合不能重复：{raw}")
        byday = tuple(sorted(set(days)))

    if "BYMONTHDAY" in kv:
        raw = kv.pop("BYMONTHDAY")
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if freq != "MONTHLY":
            problems.add(f"{where}：BYMONTHDAY 只允许出现在 FREQ=MONTHLY")
        mdays: list[int] = []
        for tok in tokens:
            neg = tok.startswith("-")
            digits = tok[1:] if neg else tok
            if not digits.isdigit():
                problems.add(f"{where}：非法 BYMONTHDAY 项 {tok!r}")
                continue
            n = int(digits)
            if not 1 <= n <= MAX_MONTH_DAY:
                problems.add(f"{where}：BYMONTHDAY 绝对值须在 1..31：{tok}")
                continue
            mdays.append(-n if neg else n)
        if len(mdays) != len(set(mdays)):
            problems.add(f"{where}：BYMONTHDAY 不能重复：{raw}")
        bymonthday = tuple(mdays)

    leftover = set(kv) - {"WKST"}  # WKST 子集固定周一；显式写出只接受 MO
    if "WKST" in kv:
        wkst = kv.pop("WKST").upper()
        if wkst != "MO":
            problems.add(f"{where}：本子集周固定从周一开始，WKST={wkst} 不支持")
    if leftover:
        problems.add(f"{where}：禁止的 RRULE 组合/部件：{sorted(leftover)}")

    if problems.problems and any(where in p for p in problems.problems):
        return None

    # 有过滤项时 DTSTART 必须满足它，作为第一次发生。
    # datetime 与 date 都带 weekday()/day 属性，定时与全天统一处理。
    wall = master_dt.value
    if byday and wall.weekday() not in byday:
        problems.add(f"{where}：DTSTART 的星期不在 BYDAY 集合内，不能作为第一次发生")
    if bymonthday:
        allowed_days = _monthday_candidates(wall.year, wall.month, bymonthday)
        if wall.day not in allowed_days:
            problems.add(f"{where}：DTSTART 的月日不在 BYMONTHDAY 集合内")
    return RRule(freq=freq, interval=interval, count=count, until=until,
                 byday=byday, bymonthday=bymonthday)


def _monthday_candidates(year: int, month: int,
                         bymonthday: tuple[int, ...]) -> list[int]:
    """把 BYMONTHDAY（含负数）落到具体年月的实际日序；不存在的日期跳过。"""

    import calendar
    last = calendar.monthrange(year, month)[1]
    out: list[int] = []
    for md in bymonthday:
        if md > 0:
            if md <= last:
                out.append(md)
        else:
            real = last + md + 1  # -1 -> 月末
            if real >= 1:
                out.append(real)
    return sorted(set(out))


def _require_single(ve: VEvent, name: str, problems: ProblemCollector) -> Property | None:
    props = ve.get_all(name)
    if len(props) > 1:
        problems.add(
            f"{loc(ve.component_index, ve.line_start, ve.line_end)}："
            f"属性 {name} 至多出现一次，实际 {len(props)} 次")
        return props[0]
    return props[0] if props else None


def parse_event(ve: VEvent, zone: ZoneInfo,
                problems: ProblemCollector) -> ParsedEvent | None:
    where = loc(ve.component_index, ve.line_start, ve.line_end)
    parsed = ParsedEvent(ve=ve, uid="", is_master=False)

    for prop in ve.props:
        if prop.name not in ALLOWED_PROPS:
            problems.add(f"{where}：子集不支持（可能影响时间口径）的属性 {prop.name}")
            continue
        _check_params(prop, problems)

    # UID
    uid_prop = _require_single(ve, "UID", problems)
    if uid_prop is None:
        problems.add(f"{where}：VEVENT 缺少 UID")
        return None
    parsed.uid = uid_prop.value  # UID 值区分大小写，原样保留

    # RECURRENCE-ID 决定主/例外身份
    rid_prop = _require_single(ve, "RECURRENCE-ID", problems)
    parsed.is_master = rid_prop is None
    if rid_prop is not None:
        parsed.recurrence_id = parse_time_prop(rid_prop, zone, problems)

    # DTSTART
    dtstart_prop = _require_single(ve, "DTSTART", problems)
    if dtstart_prop is not None:
        parsed.dtstart = parse_time_prop(dtstart_prop, zone, problems)

    # 全天 vs 定时的表示一致性在 service 层结合主组件校验；这里先解析各自部件
    dtend_prop = _require_single(ve, "DTEND", problems)
    if dtend_prop is not None:
        parsed.dtend = parse_time_prop(dtend_prop, zone, problems)
    dur_prop = _require_single(ve, "DURATION", problems)
    if dur_prop is not None:
        try:
            parsed.duration = dur_prop.value
            parsed.duration_seconds = parse_duration(dur_prop.value)
        except ValueError as exc:
            problems.add(f"{loc(None, dur_prop.line_start, dur_prop.line_end)}：{exc}")

    # RRULE（主组件才能有，归属在 service 层统一报；这里先解析）
    rrule_prop = _require_single(ve, "RRULE", problems)
    if rrule_prop is not None and parsed.dtstart is not None:
        parsed.rrule = parse_rrule(rrule_prop, parsed.dtstart, zone, problems)

    # RDATE / EXDATE（可多条，每条可含逗号分隔的多个值）
    for prop in ve.get_all("RDATE"):
        for item in _split_list_values(prop):
            dv = _parse_listed_time(prop, item, zone, problems)
            if dv is not None:
                parsed.rdates.append(dv)
    for prop in ve.get_all("EXDATE"):
        for item in _split_list_values(prop):
            dv = _parse_listed_time(prop, item, zone, problems)
            if dv is not None:
                parsed.exdates.append(dv)

    # STATUS
    status_prop = _require_single(ve, "STATUS", problems)
    if status_prop is not None:
        parsed.status = status_prop.value.upper()
        if parsed.status not in ("CANCELLED", "CONFIRMED", "TENTATIVE"):
            problems.add(f"{where}：不支持的 STATUS {status_prop.value!r}")

    # SUMMARY（TEXT 反转义）
    summary_prop = _require_single(ve, "SUMMARY", problems)
    if summary_prop is not None:
        parsed.summary = unescape_text(summary_prop.value)

    return parsed


def _split_list_values(prop: Property) -> list[str]:
    # RDATE/EXDATE 的值之间是未转义逗号；TEXT 转义规则不适用日期时间值
    return [v.strip() for v in prop.value.split(",") if v.strip()]


def _parse_listed_time(prop: Property, item: str, zone: ZoneInfo,
                       problems: ProblemCollector):
    synthetic = Property(name=prop.name, params=dict(prop.params), value=item,
                         line_start=prop.line_start, line_end=prop.line_end)
    return parse_time_prop(synthetic, zone, problems)


def check_calendar_props(cal: VCalendar, problems: ProblemCollector) -> None:
    for prop in cal.properties:
        if prop.name not in CALENDAR_ALLOWED:
            problems.add(
                f"{loc(None, prop.line_start, prop.line_end)}："
                f"VCALENDAR 上不支持的属性 {prop.name}")
