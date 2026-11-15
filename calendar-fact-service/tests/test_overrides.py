"""例外归属与稳定身份测试。

夹具 overrides.ics（窗口 2024-03-11T00:00Z ~ 2024-03-25T00:00Z，半开）：
主系列 weekly-maint：每周一 10:00Z 起、PT2H，原定 3/4、3/11、3/18、3/25…
例外（物理上写在主组件之前）：
  * 3/4（窗外原定）挪到 3/12 10:00Z（移入窗口）；
  * 3/11（窗内原定）挪到 3/25 10:00Z（移出窗口，边界外不产出事实）；
  * 3/18 取消；
  * 3/25（窗外原定）挪到 3/20 15:00Z（移入窗口）。
另有 daily-other：3/12~3/14 每天 01:00Z PT1H。
身份稳定口径：UID + 原定时刻；3/25 的 weekly 与他人即使同一实际时间也是两条事实。
"""
from __future__ import annotations

import unittest

from calendar_facts import CalendarFactService

from tests._helpers import TZ, read_fixture, utc, window

WINDOW = window(2024, 3, 11, 2024, 3, 25)


class TestOverrides(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)
        self.facts, self.report = self.svc.expand_text(
            read_fixture("overrides.ics"), "overrides.ics", *WINDOW)
        self.by_id = {f.fact_id: f for f in self.facts}

    def test_outside_recurrence_moved_into_window_is_found(self):
        # 原定 3/4（在窗外），改期到 3/12，仍必须被找到并产出
        key = "weekly-maint#2024-03-04T10:00:00Z"
        self.assertIn(key, self.by_id)
        f = self.by_id[key]
        self.assertEqual(f.status, "rescheduled")
        self.assertEqual(f.actual_start_utc, "2024-03-12T10:00:00Z")
        self.assertEqual(f.actual_end_utc, "2024-03-12T12:00:00Z")
        self.assertEqual(f.overlap_seconds, 7200)
        # 身份仍是原定 3/4，没有按实际起点 3/12 换身份
        self.assertEqual(f.recurrence_utc, "2024-03-04T10:00:00Z")
        self.assertTrue(f.is_override)
        self.assertEqual(f.override_component_index, 0)
        self.assertEqual(f.master_component_index, 4)

    def test_inside_recurrence_moved_out_produces_no_fact(self):
        # 3/11 挪到窗口末端 3/25 10:00Z（== window_end，半开不相交）
        keys = [f.fact_id for f in self.facts if f.uid == "weekly-maint"]
        self.assertNotIn("weekly-maint#2024-03-11T10:00:00Z", keys)
        # 但改期审计仍记录该例外的归属
        audit = {o["recurrence_utc"]: o for o in self.report.overrides}
        self.assertIn("2024-03-11T10:00:00Z", audit)
        self.assertEqual(audit["2024-03-11T10:00:00Z"]["status"], "rescheduled")

    def test_cancelled_recurrence_listed_as_excluded(self):
        cancelled = [e for e in self.report.excluded if e.reason == "cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].uid, "weekly-maint")
        self.assertEqual(cancelled[0].recurrence_utc, "2024-03-18T10:00:00Z")
        self.assertEqual(cancelled[0].component_index, 2)
        # 取消不产生事实
        self.assertNotIn("weekly-maint#2024-03-18T10:00:00Z", self.by_id)

    def test_two_identities_can_share_actual_time(self):
        # 3/25 原定（窗外）挪到 3/20 15:00Z PT1H；daily-other 不含该时刻，
        # 这里验证同一 UID 系列内不同原定身份不会被实际起点去重：
        # weekly 的 3/4 与 3/25 是两条独立事实，fact_id 不同
        weekly = sorted(f.fact_id for f in self.facts
                        if f.uid == "weekly-maint")
        self.assertEqual(weekly, [
            "weekly-maint#2024-03-04T10:00:00Z",
            "weekly-maint#2024-03-25T10:00:00Z",
        ])
        moved = self.by_id["weekly-maint#2024-03-25T10:00:00Z"]
        self.assertEqual(moved.actual_start_utc, "2024-03-20T15:00:00Z")
        self.assertEqual(moved.actual_end_utc, "2024-03-20T16:00:00Z")
        self.assertEqual(moved.overlap_seconds, 3600)
        self.assertEqual(moved.recurrence_utc, "2024-03-25T10:00:00Z")

    def test_shared_actual_slot_two_facts_when_cross_series(self):
        # 构造两个不同原定时刻挪到同一实际时间，验证仍是两条事实
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20240311T090000Z\r\nDURATION:PT1H\r\n"
            "RRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:x\r\nRECURRENCE-ID:20240312T090000Z\r\n"
            "DTSTART:20240315T080000Z\r\nDURATION:PT1H\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:y\r\nDTSTART:20240315T080000Z\r\nDURATION:PT1H\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        facts, _ = self.svc.expand_text(
            ics, "shared.ics", *window(2024, 3, 11, 2024, 3, 20))
        slot = [f for f in facts if f.actual_start_utc == "2024-03-15T08:00:00Z"]
        self.assertEqual(len(slot), 2)  # 同实际时间、不同身份，两条事实
        self.assertEqual({f.uid for f in slot}, {"x", "y"})

    def test_override_written_before_master_is_resolved(self):
        # 主组件序号 4，例外序号 0~3，全部正确归属
        audit_uids = {(o["uid"], o["component_index"]) for o in self.report.overrides}
        for idx in (0, 1, 2, 3):
            self.assertIn(("weekly-maint", idx), audit_uids)

    def test_window_boundary_half_open(self):
        # daily-other 3/12~3/14 全部在窗内；窗口起点 3/11 00:00Z 不含更早事件
        daily = sorted(f.recurrence_utc for f in self.facts
                       if f.uid == "daily-other")
        self.assertEqual(daily, [
            "2024-03-12T01:00:00Z",
            "2024-03-13T01:00:00Z",
            "2024-03-14T01:00:00Z",
        ])
        # 3/14 01:00->02:00 完整在窗内（窗到 3/25），重叠 3600
        for f in self.facts:
            if f.uid == "daily-other":
                self.assertEqual(f.overlap_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
