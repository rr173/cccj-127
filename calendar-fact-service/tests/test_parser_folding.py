"""物理解析、折叠、转义解码与来源组件定位测试。"""
from __future__ import annotations

import unittest

from calendar_facts import CalendarFactService
from calendar_facts.parser import parse_calendar, unfold_lines
from calendar_facts.errors import ProblemCollector
from calendar_facts.timeutil import unescape_text

from tests._helpers import TZ, read_fixture, window


class TestFolding(unittest.TestCase):
    def test_crlf_and_space_tab_continuation(self):
        # folded_crlf.ics 是 CRLF；含 SPACE 续行（TZID 折行）和 TAB 续行（RRULE）
        text = read_fixture("folded_crlf.ics")
        self.assertIn("\r\n", text)  # 确认夹具确实是 CRLF
        pc = ProblemCollector()
        cal = parse_calendar(text, pc)
        self.assertEqual(pc.problems, [])
        # 第一个事件的 DTSTART 由两行折成，TZID=America/New_York 被拼回
        ev0 = cal.events[0]
        dt = ev0.get("DTSTART")
        self.assertEqual(dt.params["TZID"], "America/New_York")
        self.assertEqual(dt.value, "20240312T090000")
        # 物理行范围覆盖折叠段（BEGIN 在第 4 行，END 在第 10 行）
        self.assertEqual((ev0.line_start, ev0.line_end), (4, 10))
        # RRULE 用 TAB 续行：FREQ=WEEK 与 LY;BYDAY=... 拼回
        ev1 = cal.events[1]
        self.assertEqual(ev1.get("RRULE").value, "FREQ=WEEKLY;BYDAY=MO,WE")

    def test_unfold_strips_only_first_whitespace(self):
        # 续行只去掉首个空白，第二个字符保留
        lines = unfold_lines("SUMMARY:AB\r\n \tCD\n")
        # 第二行以 SPACE 开头，剥 SPACE 后剩 "\tCD"
        joined = [c for c, _, _ in lines if c.startswith("SUMMARY")][0]
        self.assertEqual(joined, "SUMMARY:AB\tCD")

    def test_lf_also_accepted(self):
        text = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:u\n" \
               "DTSTART:20240312T090000Z\nDURATION:PT1H\nEND:VEVENT\n" \
               "END:VCALENDAR\n"
        pc = ProblemCollector()
        cal = parse_calendar(text, pc)
        self.assertEqual(pc.problems, [])
        self.assertEqual(len(cal.events), 1)

    def test_case_insensitive_names_case_sensitive_uid(self):
        text = (
            "begin:vcalendar\nversion:2.0\nbegin:vevent\n"
            "uid:MixedUID\ndtstart:20240312T090000Z\nduration:PT1H\n"
            "end:vevent\nend:vcalendar\n")
        pc = ProblemCollector()
        cal = parse_calendar(text, pc)
        self.assertEqual(pc.problems, [])
        self.assertEqual(cal.events[0].get("UID").value, "MixedUID")


class TestTextUnescape(unittest.TestCase):
    def test_unescape_all_forms(self):
        self.assertEqual(unescape_text("a\\,b\\;c\\\\d"), "a,b;c\\d")
        self.assertEqual(unescape_text("line1\\nline2\\Nend"),
                         "line1\nline2\nend")

    def test_chinese_summary_decoded(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("folded_crlf.ics"), "folded_crlf.ics",
            *window(2024, 3, 1, 2024, 4, 1))
        s1 = next(f.summary for f in facts if f.uid == "folded-cn-1")
        self.assertEqual(s1, "检修,一车间;二号机\\夜间\n第二批（中文）")
        s2 = next(f.summary for f in facts if f.uid == "folded-cn-2")
        self.assertEqual(s2, "折行\n续接,测试")


class TestSourceProvenance(unittest.TestCase):
    def test_fact_carries_component_and_line_range(self):
        svc = CalendarFactService(TZ)
        facts, _ = svc.expand_text(
            read_fixture("folded_crlf.ics"), "folded_crlf.ics",
            *window(2024, 3, 1, 2024, 4, 1))
        f = next(x for x in facts if x.uid == "folded-cn-1")
        self.assertEqual(f.source_component_index, 0)
        self.assertEqual(f.source_lines, "4-10")
        self.assertEqual(f.master_component_index, 0)
        self.assertFalse(f.is_override)
        self.assertIsNone(f.override_component_index)


class TestStructuralRejection(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)

    def _reject(self, text):
        with self.assertRaises(Exception) as ctx:
            self.svc.expand_text(text, "x", *window(2024, 3, 1, 2024, 4, 1))
        return ctx.exception.problems

    def test_event_without_vcalendar_wrapper(self):
        problems = self._reject(
            "BEGIN:VEVENT\nUID:x\nDTSTART:20240312T090000Z\n"
            "DURATION:PT1H\nEND:VEVENT\n")
        self.assertTrue(any("VCALENDAR" in p for p in problems))

    def test_empty_input(self):
        problems = self._reject("")
        self.assertTrue(any("空日历" in p for p in problems))

    def test_unclosed_component(self):
        problems = self._reject(
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:x\n"
            "DTSTART:20240312T090000Z\nDURATION:PT1H\n")
        self.assertTrue(any("未闭合" in p for p in problems))

    def test_uid_value_is_case_sensitive(self):
        text = (
            "BEGIN:VCALENDAR\nVERSION:2.0\n"
            "BEGIN:VEVENT\nUID:Abc\nDTSTART:20240312T090000Z\n"
            "DURATION:PT1H\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:abc\nDTSTART:20240313T090000Z\n"
            "DURATION:PT1H\nEND:VEVENT\nEND:VCALENDAR\n")
        facts, _ = self.svc.expand_text(
            text, "uid", *window(2024, 3, 1, 2024, 4, 1))
        self.assertEqual({f.uid for f in facts}, {"Abc", "abc"})


if __name__ == "__main__":
    unittest.main()
