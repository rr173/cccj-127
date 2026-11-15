"""RDATE/EXDATE、窗口边界裁剪与全天事件测试。

RDATE 夹具（窗口 2024-03-11T00:00Z ~ 2024-03-25T00:00Z）：
主系列本地 09:00 PT2H，DAILY COUNT=3 -> 原定 3/10、3/11、3/12；
RDATE 追加 3/13、3/14；EXDATE 排除 3/10、3/14。
EDT(-04:00)：本地 09:00 = 13:00Z；PT2H = 7200 秒。
"""
from __future__ import annotations

import unittest

from calendar_facts import CalendarFactService

from tests._helpers import TZ, read_fixture, utc, window

WINDOW = window(2024, 3, 11, 2024, 3, 25)


class TestRdateExdate(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)
        self.facts, self.report = self.svc.expand_text(
            read_fixture("rdate.ics"), "rdate.ics", *WINDOW)

    def test_exdate_removed_and_listed(self):
        # 3/10 与 3/14 被 EXDATE 排除，不产生事实但进入 excluded
        excluded = sorted(e.recurrence_utc for e in self.report.excluded
                          if e.reason == "exdate")
        self.assertEqual(excluded, [
            "2024-03-10T13:00:00Z",
            "2024-03-14T13:00:00Z",
        ])
        ids = [f.fact_id for f in self.facts]
        self.assertNotIn("rdate-line#2024-03-10T13:00:00Z", ids)
        self.assertNotIn("rdate-line#2024-03-14T13:00:00Z", ids)

    def test_rdate_adds_occurrences(self):
        # 规则 3/11、3/12 + RDATE 3/13（3/14 被 EXDATE），共 3 条
        ids = sorted(f.fact_id for f in self.facts)
        self.assertEqual(ids, [
            "rdate-line#2024-03-11T13:00:00Z",
            "rdate-line#2024-03-12T13:00:00Z",
            "rdate-line#2024-03-13T13:00:00Z",
        ])

    def test_local_time_to_utc_and_duration(self):
        for f in self.facts:
            self.assertEqual(f.actual_start_utc[11:], "13:00:00Z")  # EDT 09:00
            self.assertEqual(f.actual_end_utc[11:], "15:00:00Z")
            self.assertEqual(f.overlap_seconds, 7200)

    def test_window_clipping_half_open(self):
        # 自定义：区间跨越窗口起点，验证裁剪后的起止与重叠秒数
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:clip\r\nDTSTART:20240311T230000Z\r\n"
            "DURATION:PT2H\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        facts, _ = self.svc.expand_text(
            ics, "clip.ics",
            utc(2024, 3, 12), utc(2024, 3, 13))
        self.assertEqual(len(facts), 1)
        f = facts[0]
        # 完整 23:00->01:00；裁剪到窗口 3/12 00:00 -> 01:00，重叠 3600
        self.assertEqual(f.actual_start_utc, "2024-03-11T23:00:00Z")
        self.assertEqual(f.actual_end_utc, "2024-03-12T01:00:00Z")
        self.assertEqual(f.clipped_start_utc, "2024-03-12T00:00:00Z")
        self.assertEqual(f.clipped_end_utc, "2024-03-12T01:00:00Z")
        self.assertEqual(f.overlap_seconds, 3600)

    def test_touching_boundary_not_included(self):
        # 区间终点恰好等于窗口起点（相切），半开区间不相交
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:touch\r\nDTSTART:20240311T220000Z\r\n"
            "DURATION:PT2H\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        facts, _ = self.svc.expand_text(
            ics, "touch.ics",
            utc(2024, 3, 12), utc(2024, 3, 13))
        self.assertEqual(facts, [])


class TestAllDayDst(unittest.TestCase):
    def test_spring_two_day_block_is_47_hours(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("allday.ics"), "allday.ics",
            *window(2024, 3, 9, 2024, 3, 15))
        spring = {f.recurrence_date: f for f in facts
                  if f.uid == "allday-spring"}
        # 3/9 起点块：3/9 05:00Z(EST 午夜) -> 3/11 04:00Z(EDT 午夜) = 47h
        f0 = spring["2024-03-09"]
        self.assertEqual(f0.actual_start_utc, "2024-03-09T05:00:00Z")
        self.assertEqual(f0.actual_end_utc, "2024-03-11T04:00:00Z")
        self.assertEqual(f0.overlap_seconds, 47 * 3600)
        # 3/10 起点块跨缺口：3/10 05:00Z -> 3/12 04:00Z，同样 47h（23+24）
        f1 = spring["2024-03-10"]
        self.assertEqual(f1.actual_start_utc, "2024-03-10T05:00:00Z")
        self.assertEqual(f1.actual_end_utc, "2024-03-12T04:00:00Z")
        self.assertEqual(f1.overlap_seconds, 47 * 3600)

    def test_fall_day_is_25_hours_and_override_changes_length(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("allday.ics"), "allday.ics",
            *window(2024, 11, 1, 2024, 11, 10))
        fall = {f.recurrence_date: f for f in facts if f.uid == "allday-fall"}
        # 回拨当天 11/03：04:00Z(EST 午夜前一天口径) -> 11/04 05:00Z = 25h
        self.assertEqual(fall["2024-11-03"].actual_start_utc,
                         "2024-11-03T04:00:00Z")
        self.assertEqual(fall["2024-11-03"].actual_end_utc,
                         "2024-11-04T05:00:00Z")
        self.assertEqual(fall["2024-11-03"].overlap_seconds, 25 * 3600)
        # 例外把 11/04 改成两天：11/04 05:00Z -> 11/06 05:00Z = 48h
        ov = fall["2024-11-04"]
        self.assertTrue(ov.is_override)
        self.assertEqual(ov.actual_start_utc, "2024-11-04T05:00:00Z")
        self.assertEqual(ov.actual_end_utc, "2024-11-06T05:00:00Z")
        self.assertEqual(ov.overlap_seconds, 48 * 3600)

    def test_normal_day_not_hardcoded_86400(self):
        # 普通 1 天全天块在非切换日就是 86400，但来自真实时区换算而非写死
        svc = CalendarFactService(TZ)
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:plain\r\nDTSTART;VALUE=DATE:20240603\r\n"
            "DTEND;VALUE=DATE:20240604\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        facts, _ = svc.expand_text(
            ics, "plain.ics", *window(2024, 6, 1, 2024, 6, 10))
        self.assertEqual(facts[0].overlap_seconds, 86400)
        self.assertEqual(facts[0].actual_start_utc, "2024-06-03T04:00:00Z")


if __name__ == "__main__":
    unittest.main()
