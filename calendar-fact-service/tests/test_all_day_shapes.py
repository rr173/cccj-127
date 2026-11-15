"""全天 RDATE/EXDATE/RECURRENCE-ID 与取消例外形状的补充测试。"""
from __future__ import annotations

import unittest

from calendar_facts import CalendarFactService, CalendarInputError

from tests._helpers import TZ, utc, window


def ics(body: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + body +
            "\r\nEND:VCALENDAR\r\n")


class TestAllDayRecurrence(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)
        self.w = window(2024, 3, 1, 2024, 4, 1)

    def test_all_day_rdate_exdate_recurrence_id(self):
        # 全天主系列：3/11 起每天、COUNT=2 -> 3/11、3/12；RDATE 加 3/14；
        # EXDATE 排除 3/12；例外把 3/14 改到 3/15 且仍为一天
        text = ics(
            "BEGIN:VEVENT\r\nUID:ad\r\nDTSTART;VALUE=DATE:20240311\r\n"
            "DTEND;VALUE=DATE:20240312\r\nRRULE:FREQ=DAILY;COUNT=2\r\n"
            "RDATE;VALUE=DATE:20240314\r\nEXDATE;VALUE=DATE:20240312\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:ad\r\nRECURRENCE-ID;VALUE=DATE:20240314\r\n"
            "DTSTART;VALUE=DATE:20240315\r\nDTEND;VALUE=DATE:20240316\r\n"
            "END:VEVENT")
        facts, report = self.svc.expand_text(text, "ad.ics", *self.w)
        dates = sorted(f.recurrence_date for f in facts)
        # 3/11 常规；3/12 被 EXDATE；3/14 改期到 3/15（身份仍是 3/14）
        self.assertEqual(dates, ["2024-03-11", "2024-03-14"])
        moved = next(f for f in facts if f.recurrence_date == "2024-03-14")
        self.assertEqual(moved.actual_start_utc, "2024-03-15T04:00:00Z")
        self.assertTrue(moved.is_override)
        self.assertTrue(any(e.reason == "exdate" and
                            e.recurrence_date == "2024-03-12"
                            for e in report.excluded))

    def test_all_day_cancelled_override(self):
        text = ics(
            "BEGIN:VEVENT\r\nUID:ac\r\nDTSTART;VALUE=DATE:20240311\r\n"
            "DTEND;VALUE=DATE:20240312\r\nRRULE:FREQ=DAILY;COUNT=2\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:ac\r\nRECURRENCE-ID;VALUE=DATE:20240312\r\n"
            "STATUS:CANCELLED\r\nEND:VEVENT")
        facts, report = self.svc.expand_text(text, "ac.ics", *self.w)
        self.assertEqual([f.recurrence_date for f in facts], ["2024-03-11"])
        self.assertEqual(report.excluded[0].reason, "cancelled")
        self.assertEqual(report.excluded[0].recurrence_date, "2024-03-12")


class TestCancelledShape(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)
        self.w = window(2024, 3, 1, 2024, 4, 1)

    def test_cancelled_with_extra_time_props_rejected(self):
        text = ics(
            "BEGIN:VEVENT\r\nUID:c\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:c\r\nRECURRENCE-ID:20240312T090000Z\r\n"
            "STATUS:CANCELLED\r\nDTSTART:20240312T100000Z\r\n"
            "DURATION:PT1H\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "c.ics", *self.w)
        self.assertTrue(any("CANCELLED" in p for p in ctx.exception.problems))

    def test_interval_greater_than_one(self):
        # INTERVAL=2 的 DAILY：3/11、3/13、3/15
        text = ics(
            "BEGIN:VEVENT\r\nUID:i2\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;INTERVAL=2;COUNT=3\r\n"
            "END:VEVENT")
        facts, _ = self.svc.expand_text(text, "i2.ics", *self.w)
        self.assertEqual([f.recurrence_utc[8:10] for f in facts],
                         ["11", "13", "15"])

    def test_invalid_interval_rejected(self):
        text = ics(
            "BEGIN:VEVENT\r\nUID:ib\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;INTERVAL=0;COUNT=2\r\n"
            "END:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "ib.ics", *self.w)
        self.assertTrue(any("INTERVAL" in p for p in ctx.exception.problems))

    def test_override_must_not_carry_rrule(self):
        text = ics(
            "BEGIN:VEVENT\r\nUID:m\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:m\r\nRECURRENCE-ID:20240312T090000Z\r\n"
            "DTSTART:20240312T100000Z\r\nDURATION:PT1H\r\n"
            "RRULE:FREQ=DAILY\r\nEND:VEVENT")
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(text, "m.ics", *self.w)
        self.assertTrue(any("RRULE/RDATE/EXDATE" in p
                            for p in ctx.exception.problems))

    def test_utc_unbounded_weekly_constrained_by_window(self):
        # UTC 型 DTSTART 的无界 WEEKLY，只在窗口内产出周一
        text = ics(
            "BEGIN:VEVENT\r\nUID:uinf\r\nDTSTART:20240311T090000Z\r\n"
            "DURATION:PT1H\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT")
        facts, _ = self.svc.expand_text(
            text, "uinf.ics",
            utc(2024, 3, 11), utc(2024, 4, 8))
        self.assertEqual([f.recurrence_utc for f in facts], [
            "2024-03-11T09:00:00Z", "2024-03-18T09:00:00Z",
            "2024-03-25T09:00:00Z", "2024-04-01T09:00:00Z",
        ])


if __name__ == "__main__":
    unittest.main()
