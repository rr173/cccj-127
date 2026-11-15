"""离线可复跑的测试夹具生成器。

所有 .ics 内容在此手写（不调用生产展开函数），并显式构造：
  * 物理续行（行首 SPACE/TAB，且第二字符保留）；
  * CRLF 与 LF 两种换行；
  * SUMMARY 的反斜杠/逗号/分号/换行转义与中文。

运行：python3 -I bin/make_fixtures.py
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "..", "tests", "fixtures")


def crlf(text_lf: str) -> bytes:
    return text_lf.replace("\n", "\r\n").encode("utf-8")


def lf(text_lf: str) -> bytes:
    return text_lf.encode("utf-8")


# 1) 纽约春秋 DST：春季 02:30 缺口跳过且 COUNT 补足；秋季 01:30 取较早一次
DST_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Warehouse//Maintenance Fixtures//CN
BEGIN:VEVENT
UID:spring-gap-0230
DTSTART;TZID=America/New_York:20240309T023000
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Spring gap 02:30 daily
END:VEVENT
BEGIN:VEVENT
UID:fall-back-0130
DTSTART;TZID=America/New_York:20241101T013000
DURATION:PT30M
RRULE:FREQ=DAILY;COUNT=4
SUMMARY:Fall back 01:30 daily
END:VEVENT
BEGIN:VEVENT
UID:monthly-31
DTSTART;TZID=America/New_York:20240131T090000
DURATION:PT2H
RRULE:FREQ=MONTHLY;BYMONTHDAY=31;COUNT=3
SUMMARY:Monthly 31st
END:VEVENT
BEGIN:VEVENT
UID:monthly-neg-1
DTSTART;TZID=America/New_York:20240131T200000
DURATION:PT1H30M
RRULE:FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3
SUMMARY:Monthly last day
END:VEVENT
END:VCALENDAR
"""

# 2) 例外归属：窗外原定挪入、窗内原定挪出、取消、两身份挪到同一实际时间；
#    例外写在主组件之前
OVERRIDES_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Warehouse//Overrides//CN
BEGIN:VEVENT
UID:weekly-maint
RECURRENCE-ID:20240304T100000Z
DTSTART:20240312T100000Z
DURATION:PT2H
SUMMARY:Moved into window from outside
END:VEVENT
BEGIN:VEVENT
UID:weekly-maint
RECURRENCE-ID:20240311T100000Z
DTSTART:20240325T100000Z
DURATION:PT2H
SUMMARY:Moved out of window
END:VEVENT
BEGIN:VEVENT
UID:weekly-maint
RECURRENCE-ID:20240318T100000Z
STATUS:CANCELLED
SUMMARY:Cancelled occurrence
END:VEVENT
BEGIN:VEVENT
UID:weekly-maint
RECURRENCE-ID:20240325T100000Z
DTSTART:20240320T150000Z
DURATION:PT1H
SUMMARY:Identity A moved to shared slot
END:VEVENT
BEGIN:VEVENT
UID:weekly-maint
DTSTART:20240304T100000Z
DURATION:PT2H
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Weekly maintenance
END:VEVENT
BEGIN:VEVENT
UID:daily-other
DTSTART:20240312T010000Z
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Daily other line
END:VEVENT
END:VCALENDAR
"""

# 3) RDATE/EXDATE 与窗口边界相交
RDATE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Warehouse//Rdate//CN
BEGIN:VEVENT
UID:rdate-line
DTSTART;TZID=America/New_York:20240310T090000
DURATION:PT2H
RRULE:FREQ=DAILY;COUNT=3
RDATE;TZID=America/New_York:20240313T090000,20240314T090000
EXDATE;TZID=America/New_York:20240310T090000,20240314T090000
SUMMARY:RDATE EXDATE boundary
END:VEVENT
END:VCALENDAR
"""

# 4) 全天事件跨春秋切换（UTC 日长 23/25 小时），含跨天长度与例外改长度
ALLDAY_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Warehouse//AllDay//CN
BEGIN:VEVENT
UID:allday-spring
DTSTART;VALUE=DATE:20240309
DTEND;VALUE=DATE:20240311
RRULE:FREQ=DAILY;COUNT=2
SUMMARY:Spring all-day two-day blocks
END:VEVENT
BEGIN:VEVENT
UID:allday-fall
DTSTART;VALUE=DATE:20241102
DTEND;VALUE=DATE:20241103
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Fall all-day
END:VEVENT
BEGIN:VEVENT
UID:allday-fall
RECURRENCE-ID;VALUE=DATE:20241104
DTSTART;VALUE=DATE:20241104
DTEND;VALUE=DATE:20241106
SUMMARY:Override lengthens to two days
END:VEVENT
END:VCALENDAR
"""

# 5) 折行（SPACE 与 TAB）、CRLF、中文与转义
FOLDED_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Warehouse//Folded\\, 转义//CN
BEGIN:VEVENT
UID:folded-cn-1
DTSTART;TZID=America/
 New_York:20240312T090000
DURATION:PT1H
SUMMARY:检修\\,一车间\\;二号机\\\\夜间\\n第二批（中文）
END:VEVENT
BEGIN:VEVENT
UID:folded-cn-2
DTSTART;TZID=America/New_York:20240313T090000
DURATION:PT30M
RRULE:FREQ=WEEK
 LY;BYDAY=MO,WE
SUMMARY:折行\\n续接\\,测试
END:VEVENT
END:VCALENDAR
"""


def main() -> None:
    os.makedirs(FIX, exist_ok=True)
    files = {
        "dst.ics": (DST_ICS, "lf"),
        "overrides.ics": (OVERRIDES_ICS, "lf"),
        "rdate.ics": (RDATE_ICS, "lf"),
        "allday.ics": (ALLDAY_ICS, "lf"),
        "folded_crlf.ics": (FOLDED_ICS, "crlf"),
    }
    for name, (text, mode) in files.items():
        path = os.path.join(FIX, name)
        with open(path, "wb") as fh:
            fh.write(crlf(text) if mode == "crlf" else lf(text))
        print(f"wrote {path} ({mode})")


if __name__ == "__main__":
    main()
