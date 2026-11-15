"""拒绝演练：数据在模板前、撤销后仍发送、非法长度、Set 越界、扩展长度截断、
非零填充、序列缺口、最后一行坏消息等。

每例同时断言：
  * 坏捕获返回非零错误并定位捕获行/消息偏移/Set/字段；
  * 目标结果库不可打开（不存在），不留下本次半份记录库；
  * 修正同一捕获后可以成功接入。
"""
from __future__ import annotations

import json
import os
import unittest

from ipfix_offline import IpfixInputError, ResultStore, import_capture

from ._helpers import TempDirTestCase

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tests", "fixtures", "rejections")
MANIFEST = os.path.join(FIXTURES, "manifest.json")


class RejectionTestCase(TempDirTestCase):
    def test_every_bad_capture_fails_with_location_and_leaves_no_db(self) -> None:
        manifest = json.load(open(MANIFEST, encoding="utf-8"))
        for name, expect in manifest.items():
            with self.subTest(case=name):
                bad = os.path.join(FIXTURES, f"{name}.bad.jsonl")
                db = self.path(f"{name}.db")
                with self.assertRaises(IpfixInputError) as ctx:
                    import_capture(bad, db)
                exc = ctx.exception
                self.assertFalse(os.path.exists(db), "失败后目标库不得存在")
                self.assertFalse(
                    [p for p in os.listdir(self.dir)
                     if p.startswith(".ipfix-staging-")],
                    "失败后不得留下临时库")
                self.assertTrue(os.path.exists(bad))
                with open(bad, "rb") as fh:
                    self.assertGreater(len(fh.read()), 0)
                failure = exc.failure
                self.assertEqual(failure.phase, expect["phase"], name)
                self.assertEqual(failure.capture_line, expect["capture_line"],
                                 name)
                if "field_ordinal" in expect:
                    self.assertEqual(failure.field_ordinal,
                                     expect["field_ordinal"], name)
                self.assertTrue(failure.reason)

    def test_each_bad_capture_database_is_not_openable(self) -> None:
        manifest = json.load(open(MANIFEST, encoding="utf-8"))
        for name in manifest:
            with self.subTest(case=name):
                db = self.path(f"{name}.db")
                with self.assertRaises(IpfixInputError):
                    import_capture(
                        os.path.join(FIXTURES, f"{name}.bad.jsonl"), db)
                with self.assertRaises(FileNotFoundError):
                    ResultStore(db)

    def test_corrected_capture_succeeds(self) -> None:
        manifest = json.load(open(MANIFEST, encoding="utf-8"))
        expected_records = {
            "data_before_template": 3,
            "send_after_revoke": 3,
            "revoke_unregistered": 0,
            "illegal_known_length": 0,
            "illegal_ipv4_length": 0,
            "field_length_zero": 0,
            "set_beyond_message": 1,
            "message_length_lies": 1,
            "extended_length_truncated": 1,
            "variable_payload_truncated": 1,
            "extended_length_below_255": 1,
            "nonzero_padding": 1,
            "four_byte_nonzero_tail": 1,
            "sequence_gap": 6,
            "sequence_template_only_counts_zero": 3,
            "last_line_bad": 6,
            "options_template_set": 0,
            "reserved_set_id": 0,
            "template_id_below_256": 0,
            "template_before_domain_definition": 3,
            "short_record_tail": 2,
        }
        self.assertEqual(set(expected_records), set(manifest))
        for name in manifest:
            with self.subTest(case=name):
                db = self.path(f"{name}.good.db")
                report = import_capture(
                    os.path.join(FIXTURES, f"{name}.good.jsonl"), db)
                self.assertEqual(report.records, expected_records[name], name)
                store = ResultStore(db)
                self.assertEqual(store.summary()["records"],
                                 expected_records[name])

    # -------------------------------------------------------- 具体错误语义
    def test_sequence_gap_message_says_expected_and_actual(self) -> None:
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(
                os.path.join(FIXTURES, "sequence_gap.bad.jsonl"),
                self.path("gap.db"))
        detail = ctx.exception.failure.detail
        self.assertIn("期望 13", detail)
        self.assertIn("实际 14", detail)

    def test_unknown_template_not_guessed_from_later_definition(self) -> None:
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(
                os.path.join(FIXTURES, "template_before_domain_definition.bad.jsonl"),
                self.path("scope.db"))
        self.assertEqual(ctx.exception.failure.phase, "data")
        self.assertEqual(ctx.exception.failure.capture_line, 2)

    def test_bad_last_line_still_fails_whole_capture(self) -> None:
        db = self.path("last.db")
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(os.path.join(FIXTURES, "last_line_bad.bad.jsonl"),
                           db)
        self.assertEqual(ctx.exception.failure.capture_line, 3)
        self.assertEqual(ctx.exception.failure.phase, "header")
        self.assertFalse(os.path.exists(db))


if __name__ == "__main__":
    unittest.main()
