"""解析后的日历内存模型。

刻意只建模接入子集里出现的东西：一个 VCALENDAR、若干 VEVENT；
每个 VEVENT 保留它在原文件中的组件序号与物理行范围，便于数仓追查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Union

# 墙上时间：定时事件用 datetime（带 tzinfo=UTC 或设备 IANA 时区），
# 全天事件用 date。
WallTime = Union[datetime, date]


@dataclass(frozen=True)
class Property:
    """一条已折叠的逻辑属性行。

    raw_name / raw_param_name 保留原大小写；比较口径在解析/校验层统一小写化。
    """

    name: str                      # 已大写的属性名
    params: dict[str, str]         # 参数名已大写；值保留原样（含引号语义已剥离）
    value: str
    line_start: int                # 1 起，物理行范围（折叠段的首行/末行）
    line_end: int


@dataclass
class VEvent:
    component_index: int           # 0 起，按文件出现顺序的组件序号
    line_start: int                # BEGIN:VEVENT 所在物理行（1 起）
    line_end: int = 0              # END:VEVENT 所在物理行（1 起），闭合时回填
    props: list[Property] = field(default_factory=list)

    def get(self, name: str) -> Optional[Property]:
        name = name.upper()
        found = [p for p in self.props if p.name == name]
        return found[0] if found else None

    def get_all(self, name: str) -> list[Property]:
        name = name.upper()
        return [p for p in self.props if p.name == name]


@dataclass
class VCalendar:
    properties: list[Property]
    events: list[VEvent]


@dataclass(frozen=True)
class DateTimeValue:
    """DTSTART / DTEND / RDATE 等时间值的解析结果。"""

    value: WallTime                # UTC datetime、带 IANA tz 的 datetime 或 date
    kind: str                      # "utc" | "tz" | "date"
    tzid: Optional[str] = None     # kind == "tz" 时的 IANA 名


@dataclass(frozen=True)
class RRule:
    freq: str                      # "DAILY" | "WEEKLY" | "MONTHLY"
    interval: int
    count: Optional[int] = None
    until: Optional[WallTime] = None
    byday: tuple[int, ...] = ()        # 0=周一 … 6=周日，已去重排序
    bymonthday: tuple[int, ...] = ()  # 含负数，已去重


@dataclass
class ParsedEvent:
    """语义解析后的单个组件（主组件或例外组件）。"""

    ve: VEvent
    uid: str
    is_master: bool
    dtstart: Optional[DateTimeValue] = None
    duration: Optional[str] = None          # 原始 PT 串，秒数另算
    duration_seconds: Optional[int] = None
    dtend: Optional[DateTimeValue] = None   # 仅全天事件
    rrule: Optional[RRule] = None
    rdates: list[DateTimeValue] = field(default_factory=list)
    exdates: list[DateTimeValue] = field(default_factory=list)
    recurrence_id: Optional[DateTimeValue] = None
    status: Optional[str] = None
    summary: Optional[str] = None


@dataclass(frozen=True)
class Fact:
    """一条可追查的检修区间事实。"""

    fact_id: str
    uid: str
    recurrence_utc: Optional[str]           # 定时事件的原定 UTC；全天事件为 None
    recurrence_date: Optional[str]          # 全天事件的原定设备时区日期
    kind: str                               # "timed" | "all_day"
    summary: Optional[str]
    status: str                             # "scheduled" | "rescheduled" | "cancelled"
    actual_start_utc: str
    actual_end_utc: str
    clipped_start_utc: str
    clipped_end_utc: str
    overlap_seconds: int
    source_component_index: int
    source_lines: str                       # "12-20"
    master_component_index: int
    master_lines: str
    is_override: bool
    override_component_index: Optional[int]
    override_lines: Optional[str]

    def to_json(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "uid": self.uid,
            "recurrence_utc": self.recurrence_utc,
            "recurrence_date": self.recurrence_date,
            "kind": self.kind,
            "summary": self.summary,
            "status": self.status,
            "actual_start_utc": self.actual_start_utc,
            "actual_end_utc": self.actual_end_utc,
            "clipped_start_utc": self.clipped_start_utc,
            "clipped_end_utc": self.clipped_end_utc,
            "overlap_seconds": self.overlap_seconds,
            "source_component_index": self.source_component_index,
            "source_lines": self.source_lines,
            "master_component_index": self.master_component_index,
            "master_lines": self.master_lines,
            "is_override": self.is_override,
            "override_component_index": self.override_component_index,
            "override_lines": self.override_lines,
        }


@dataclass(frozen=True)
class ExcludedItem:
    uid: str
    recurrence_utc: Optional[str]
    recurrence_date: Optional[str]
    reason: str                             # "exdate" | "cancelled"
    component_index: Optional[int]          # CANCELLED 例外组件序号；EXDATE 为 None
    lines: Optional[str]


@dataclass
class ExpandReport:
    input_file: str
    tzname: str
    window_start_utc: str
    window_end_utc: str
    fact_count: int
    fact_file: str
    excluded: list[ExcludedItem]
    overrides: list[dict]
    masters: list[dict]

    def to_json(self) -> dict:
        return {
            "input_file": self.input_file,
            "tzname": self.tzname,
            "window_start_utc": self.window_start_utc,
            "window_end_utc": self.window_end_utc,
            "fact_count": self.fact_count,
            "fact_file": self.fact_file,
            "excluded": [
                {
                    "uid": e.uid,
                    "recurrence_utc": e.recurrence_utc,
                    "recurrence_date": e.recurrence_date,
                    "reason": e.reason,
                    "component_index": e.component_index,
                    "lines": e.lines,
                }
                for e in self.excluded
            ],
            "overrides": self.overrides,
            "masters": self.masters,
        }
