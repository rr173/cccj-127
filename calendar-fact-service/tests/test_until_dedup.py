"""UNTIL 端点、去重、无界系列窗口约束等补充口径测试。"""
from __future__ import annotations

import unittest

from calendar_facts import CalendarFactService, CalendarInputError

from tests._helpers import TZ, utc, window


def ics(body: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + body +
            "\r\nEND:VCALENDAR\r\n")


class TestUntilAndDedup(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)

    def test_until_is_inclusive(self):
        # 每天 09:00Z，UNTIL 含 3/13 端点 -> 3/11、3/12、3/13 共 3 次
        text = ics(
            "BEGIN:VEVENT\r\nUID:u\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;UNTIL=20240313T090000Z\r\n"
            "END:VEVENT")
        facts, _ = self.svc.expand_text(
            text, "u.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertEqual(sorted(f.recurrence_utc for f in facts), [
            "2024-03-11T09:00:00Z",
            "2024-03-12T09:00:00Z",
            "2024-03-13T09:00:00Z",
        ])

    def test_unbounded_series_constrained_by_window(self):
        # 无 COUNT/UNTIL 不生成无限日历，只在窗口相关范围内产出
        text = ics(
            "BEGIN:VEVENT\r\nUID:inf\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT")
        facts, _ = self.svc.expand_text(
            text, "inf.ics", *window(2024, 3, 11, 2024, 3, 15))
        # 窗口 4 天，每天一次（硬限制内的窗外候选不会产出相交事实）
        self.assertEqual(len(facts), 4)
        self.assertEqual(facts[0].recurrence_utc, "2024-03-11T09:00:00Z")
        self.assertEqual(facts[-1].recurrence_utc, "2024-03-14T09:00:00Z")

    def test_rdate_duplicate_rejected(self):
        # RDATE 集合内重复原定时刻必须报错，不能静默去重后假装完整
        text = ics(
            "BEGIN:VEVENT\r\nUID:rd\r\nDTSTART;TZID=America/New_York:20240311T090000\r\n"
            "DURATION:PT1H\r\n"
            "RDATE;TZID=America/New_York:20240312T090000,20240312T090000\r\n"
            "END:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "rd.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertTrue(any("RDATE 含重复" in p for p in ctx.exception.problems))

    def test_rdate_equal_to_rule_occurrence_is_merged_not_duplicate(self):
        # RDATE 与规则时刻重合：集合去重，只产一条，不算故障
        text = ics(
            "BEGIN:VEVENT\r\nUID:rm\r\nDTSTART;TZID=America/New_York:20240311T090000\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2\r\n"
            "RDATE;TZID=America/New_York:20240312T090000\r\nEND:VEVENT")
        facts, report = self.svc.expand_text(
            text, "rm.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertEqual(len(facts), 2)  # 3/11、3/12，不重复

    def test_exdate_not_in_set_rejected(self):
        text = ics(
            "BEGIN:VEVENT\r\nUID:ex\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2\r\n"
            "EXDATE:20240320T090000Z\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "ex.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertTrue(
            any("EXDATE" in p and "不属于" in p for p in ctx.exception.problems))

    def test_all_day_until_uses_date(self):
        # 全天系列 UNTIL 是 DATE；含端点
        text = ics(
            "BEGIN:VEVENT\r\nUID:ad\r\nDTSTART;VALUE=DATE:20240311\r\n"
            "DTEND;VALUE=DATE:20240312\r\n"
            "RRULE:FREQ=DAILY;UNTIL=20240313\r\nEND:VEVENT")
        facts, _ = self.svc.expand_text(
            text, "ad.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertEqual(sorted(f.recurrence_date for f in facts),
                         ["2024-03-11", "2024-03-12", "2024-03-13"])

    def test_all_day_exdate_type_conflict(self):
        # 全天主组件挂定时 EXDATE -> 类型冲突
        text = ics(
            "BEGIN:VEVENT\r\nUID:bad\r\nDTSTART;VALUE=DATE:20240311\r\n"
            "DTEND;VALUE=DATE:20240312\r\nEXDATE:20240312T090000Z\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "bad.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertTrue(
            any("表示类型/时区" in p for p in ctx.exception.problems))

    def test_all_day_dtstart_must_satisfy_byday_filter(self):
        # 2024-03-11 是周一，BYDAY=WE（周三）不含它 -> 拒绝
        text = ics(
            "BEGIN:VEVENT\r\nUID:bw\r\nDTSTART;VALUE=DATE:20240311\r\n"
            "DTEND;VALUE=DATE:20240312\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=2\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "bw.ics", *window(2024, 3, 1, 2024, 4, 1))
        self.assertTrue(any("BYDAY" in p for p in ctx.exception.problems))

    def test_all_day_dtstart_must_satisfy_bymonthday_filter(self):
        # DTSTART 是 3/15，BYMONTHDAY=16 不含它 -> 拒绝
        text = ics(
            "BEGIN:VEVENT\r\nUID:bm\r\nDTSTART;VALUE=DATE:20240315\r\n"
            "DTEND;VALUE=DATE:20240316\r\n"
            "RRULE:FREQ=MONTHLY;BYMONTHDAY=16;COUNT=2\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "bm.ics", *window(2024, 3, 1, 2024, 6, 1))
        self.assertTrue(any("BYMONTHDAY" in p for p in ctx.exception.problems))


if __name__ == "__main__":
    unittest.main()
