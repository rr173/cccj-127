"""输入拒绝、冲突诊断与原子落盘测试。

口径：任何输入故障都必须整份拒绝（CalendarInputError 携带全部问题），
不能静默忽略后产出看似完整的结果；失败时结果文件原字节不动。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from calendar_facts import CalendarFactService, CalendarInputError

from tests._helpers import TZ, read_error, read_fixture, window

WINDOW = window(2024, 3, 1, 2024, 4, 1)


def sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class TestRejections(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)

    def _expect(self, name, fragments):
        with self.assertRaises(CalendarInputError) as ctx:
            self.svc.expand_text(read_error(name), name, *WINDOW)
        joined = " | ".join(ctx.exception.problems)
        for frag in fragments:
            self.assertIn(frag, joined,
                          msg=f"{name} 缺少诊断片段 {frag!r}：{joined}")
        return ctx.exception.problems

    def test_duplicate_master(self):
        self._expect("dup_master.ics", ["主组件重复"])

    def test_duplicate_override_same_recurrence(self):
        self._expect("dup_override.ics", ["同一原定时刻", "多个例外"])

    def test_orphan_override(self):
        self._expect("orphan_override.ics", ["找不到同 UID 的主组件"])

    def test_override_outside_count(self):
        self._expect("rid_out_of_count.ics", ["不属于", "原定集合"])

    def test_override_on_skipped_gap_day(self):
        self._expect("rid_skipped_day.ics", ["不属于", "原定集合"])

    def test_exdate_and_override_conflict(self):
        self._expect("exdate_conflict.ics", ["同时被 EXDATE", "冲突"])

    def test_date_type_mismatch(self):
        problems = self._expect("type_mismatch.ics",
                                ["表示类型/时区", "一致"])
        # 同一文件里 RDATE 与 RECURRENCE-ID 两处类型冲突都要报，不静默
        self.assertGreaterEqual(
            sum("表示类型/时区" in p for p in problems), 2)

    def test_unsupported_rrules_all_reported(self):
        problems = self._expect(
            "bad_rrules.ics",
            ["FREQ=YEARLY", "BYMONTH", "COUNT 与 UNTIL", "WKST", "BYDAY"])
        self.assertGreaterEqual(len(problems), 5)  # 一次性返回，不只报第一个

    def test_unknown_property_floating_and_bad_duration(self):
        self._expect("unknown_props.ics", [
            "EXRULE",            # 未知影响时间属性
            "浮动时间",           # 无 Z 无 TZID
            "PT0S",              # 非正 DURATION
            "P1D",               # 不接受日分量
        ])

    def test_incomplete_override(self):
        self._expect("incomplete_override.ics", ["非取消例外必须给出自己的 DTSTART"])

    def test_timed_until_must_be_utc(self):
        self._expect("bad_until.ics", ["UNTIL 必须为 UTC"])

    def test_count_and_until_mutually_exclusive(self):
        # 已在 bad_rrules 覆盖，这里单独确认错误文本
        self._expect("bad_rrules.ics", ["COUNT 与 UNTIL 不能同时出现"])


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.svc = CalendarFactService(TZ)
        self.tmp = tempfile.mkdtemp(prefix="cf-test-")
        self.facts_path = os.path.join(self.tmp, "facts.jsonl")
        self.report_path = os.path.join(self.tmp, "report.json")

    def test_failure_keeps_existing_result_bytes_identical(self):
        # 1) 合法输入先成功
        r = self.svc.expand_file(
            _abspath("dst.ics"), *WINDOW, self.facts_path, self.report_path)
        self.assertGreater(r.fact_count, 0)
        facts_hash = sha(self.facts_path)
        report_hash = sha(self.report_path)

        # 2) 末尾故障输入必须失败
        with self.assertRaises(CalendarInputError):
            self.svc.expand_file(
                _abspath_error("eof_error.ics"), *WINDOW,
                self.facts_path, self.report_path)

        # 3) 旧结果原字节不动，且没有临时文件残留
        self.assertEqual(sha(self.facts_path), facts_hash)
        self.assertEqual(sha(self.report_path), report_hash)
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_success_replaces_result(self):
        self.svc.expand_file(
            _abspath("dst.ics"), *WINDOW, self.facts_path, self.report_path)
        first = sha(self.facts_path)
        # 用另一个合法输入重跑，结果应被替换
        self.svc.expand_file(
            _abspath("rdate.ics"), *WINDOW, self.facts_path, self.report_path)
        self.assertNotEqual(sha(self.facts_path), first)

    def test_jsonl_one_fact_per_line_and_valid(self):
        import json
        self.svc.expand_file(
            _abspath("dst.ics"), *WINDOW, self.facts_path, self.report_path)
        lines = open(self.facts_path, encoding="utf-8").read().splitlines()
        self.assertTrue(lines)
        for line in lines:
            obj = json.loads(line)  # 每行都是合法 JSON
            for key in ("fact_id", "uid", "actual_start_utc", "actual_end_utc",
                        "clipped_start_utc", "clipped_end_utc",
                        "overlap_seconds", "source_component_index",
                        "source_lines"):
                self.assertIn(key, obj)

    def test_result_files_have_normal_mode(self):
        self.svc.expand_file(
            _abspath("dst.ics"), *WINDOW, self.facts_path, self.report_path)
        # mkstemp 默认 0600，服务须把结果规范化为常规可读的 0644
        self.assertEqual(os.stat(self.facts_path).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.report_path).st_mode & 0o777, 0o644)


def _abspath(name: str) -> str:
    from tests._helpers import fixture
    return fixture(name)


def _abspath_error(name: str) -> str:
    from tests._helpers import error_fixture
    return error_fixture(name)


if __name__ == "__main__":
    unittest.main()
