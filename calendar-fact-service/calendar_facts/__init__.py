"""离线日历展开：iCalendar 检修计划 -> 数仓区间事实（JSONL）。

公开 API：
    CalendarFactService(tzname).expand_file(...)
    CalendarFactService(tzname).expand_text(...)
    CalendarInputError
"""
from .errors import CalendarInputError
from .model import ExcludedItem, ExpandReport, Fact
from .service import CalendarFactService

__all__ = [
    "CalendarFactService",
    "CalendarInputError",
    "Fact",
    "ExpandReport",
    "ExcludedItem",
]
