"""DST 与重复口径测试。

所有期望 UTC 时间均手工推导（推导写在注释里），不取自生产展开输出：

纽约 2024 年夏令时：
  * 春季 2024-03-10 02:00 -> 03:00，02:30 落在缺口，本地不存在；
  * 秋季 2024-11-03 02:00 -> 01:00，01:30 出现两次，取较早 UTC（EDT，-04:00）。
EST=-05:00，EDT=-04:00。
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from calendar_facts import CalendarFactService
from calendar_facts.model import RRule
from calendar_facts.recur import iter_rule
from calendar_facts.timeutil import local_to_utc, local_wall_exists

from tests._helpers import TZ, fixture, read_fixture, utc, window

ZONE = ZoneInfo(TZ)


def wall(y, mo, d, hh, mm=0):
    return datetime(y, mo, d, hh, mm, tzinfo=ZONE)


class TestGapAndFallback(unittest.TestCase):
    def test_spring_gap_is_missing(self):
        # 2024-03-10 02:30 NY 不存在；03-09/03-11 的 02:30 存在
        self.assertFalse(local_wall_exists(wall(2024, 3, 10, 2, 30), ZONE))
        self.assertTrue(local_wall_exists(wall(2024, 3, 9, 2, 30), ZONE))
        self.assertTrue(local_wall_exists(wall(2024, 3, 11, 2, 30), ZONE))
        _, amb, missing = local_to_utc(wall(2024, 3, 10, 2, 30), ZONE)
        self.assertTrue(missing)
        self.assertFalse(amb)

    def test_fall_back_ambiguous_takes_early_utc(self):
        # 01:30 两次：EDT(-04) -> 05:30Z（较早）；EST(-05) -> 06:30Z（较晚）
        u, amb, missing = local_to_utc(wall(2024, 11, 3, 1, 30), ZONE)
        self.assertTrue(amb)
        self.assertFalse(missing)
        self.assertEqual(u, utc(2024, 11, 3, 5, 30))

    def test_spring_series_skips_gap_and_count_filled(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("dst.ics"), "dst.ics",
            *window(2024, 1, 1, 2025, 1, 1))
        spring = sorted(f.recurrence_utc for f in facts
                        if f.uid == "spring-gap-0230")
        # COUNT=5 真实生成 5 次：03-09，跳过 03-10 缺口后由 03-11/12/13/14 补足
        self.assertEqual(spring, [
            "2024-03-09T07:30:00Z",  # EST 02:30 -> 07:30Z
            "2024-03-11T06:30:00Z",  # EDT 02:30 -> 06:30Z
            "2024-03-12T06:30:00Z",
            "2024-03-13T06:30:00Z",
            "2024-03-14T06:30:00Z",
        ])
        # 没有任何一次落在缺口日 03-10
        self.assertFalse(any("2024-03-10" in s for s in spring))
        # DURATION PT1H = 3600 秒，按真实经过计算
        for f in facts:
            if f.uid == "spring-gap-0230":
                self.assertEqual(f.overlap_seconds, 3600)

    def test_fall_back_series_single_early_occurrence(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("dst.ics"), "dst.ics",
            *window(2024, 1, 1, 2025, 1, 1))
        fall = {f.recurrence_utc: f for f in facts
                if f.uid == "fall-back-0130"}
        # COUNT=4：11-01/02 是 EST 01:30->06:30Z；11-03 歧义取早 05:30Z；
        # 11-04 回到 EST 06:30Z。11-03 只出现一次（不重复）。
        self.assertEqual(sorted(fall), [
            "2024-11-01T05:30:00Z",
            "2024-11-02T05:30:00Z",
            "2024-11-03T05:30:00Z",  # 较早一次；较晚的 06:30Z 不生成
            "2024-11-04T06:30:00Z",
        ])
        self.assertEqual(len(fall), 4)
        for f in fall.values():
            self.assertEqual(f.overlap_seconds, 1800)  # PT30M


class TestMonthlyRules(unittest.TestCase):
    def test_bymonthday_31_skips_short_months(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("dst.ics"), "dst.ics",
            *window(2024, 1, 1, 2025, 1, 1))
        m31 = sorted(f.recurrence_utc for f in facts if f.uid == "monthly-31")
        # BYMONTHDAY=31 COUNT=3：1/31 存在；2 月无 31 跳过且不占 COUNT；
        # 3/31、5/31（4 月只有 30 天，跳过）。09:00 EST=14:00Z，EDT=13:00Z。
        self.assertEqual(m31, [
            "2024-01-31T14:00:00Z",
            "2024-03-31T13:00:00Z",
            "2024-05-31T13:00:00Z",
        ])

    def test_negative_monthday_is_last_calendar_day(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("dst.ics"), "dst.ics",
            *window(2024, 1, 1, 2025, 1, 1))
        neg = sorted(f.recurrence_utc for f in facts if f.uid == "monthly-neg-1")
        # BYMONTHDAY=-1 COUNT=3，起点 2024-01-31 20:00 EST：
        # 1/31(20:00 EST=01:00Z 次日)、2/29(闰年)、3/31。
        self.assertEqual(neg, [
            "2024-02-01T01:00:00Z",
            "2024-03-01T01:00:00Z",
            "2024-04-01T00:00:00Z",  # 3/31 20:00 EDT -> 00:00Z
        ])
        for f in facts:
            if f.uid == "monthly-neg-1":
                self.assertEqual(f.overlap_seconds, 5400)  # PT1H30M


class TestCalendarAdvance(unittest.TestCase):
    def test_advances_by_calendar_day_not_utc_seconds(self):
        # 跨春季缺口的本地 09:00 系列：每天 UTC 时刻在 03-10 后从 14:00Z 变 13:00Z，
        # 若错误地用 UTC+86400 推进，墙上时间会漂移。
        rule = RRule(freq="DAILY", interval=1, count=3)
        gens = list(iter_rule(rule, wall(2024, 3, 9, 9), ZONE))
        walls = [g.wall.replace(tzinfo=None) for g in gens]
        self.assertEqual(walls, [
            datetime(2024, 3, 9, 9),
            datetime(2024, 3, 10, 9),
            datetime(2024, 3, 11, 9),
        ])
        # UTC：03-09 09:00 EST=14:00Z；03-10/11 09:00 EDT=13:00Z
        self.assertEqual([g.utc for g in gens], [
            utc(2024, 3, 9, 14), utc(2024, 3, 10, 13), utc(2024, 3, 11, 13)])

    def test_weekly_byday_monday_origin_and_order(self):
        # 周从周一开始；BYDAY=MO,WE 在每个周期内按周一->周三排序
        rule = RRule(freq="WEEKLY", interval=1, byday=(0, 2))
        gens = list(iter_rule(rule, wall(2024, 3, 4, 8), ZONE,
                              hard_limit_wall=datetime(2024, 3, 14, 0)))
        days = [g.wall.day for g in gens]
        self.assertEqual(days, [4, 6, 11, 13])  # 3/4 周一、3/6 周三、3/11、3/13

    def test_monthly_nonexistent_day_skipped_no_count(self):
        rule = RRule(freq="MONTHLY", interval=1, count=3, bymonthday=(31,))
        gens = list(iter_rule(rule, wall(2024, 1, 31, 9), ZONE))
        self.assertEqual([g.wall.month for g in gens], [1, 3, 5])


if __name__ == "__main__":
    unittest.main()
