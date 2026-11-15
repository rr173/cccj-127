"""变长字段、企业字段、重复元素、IPv6、精简计数与 Set 填充。

全部期望值来自 tests/fixtures/variable.jsonl 的手算结构（偏移注释在各断言旁），
不调用正式编码器生成答案。
"""
from __future__ import annotations

import os
import unittest

from ipfix_offline import ResultStore, import_capture

from ._helpers import TempDirTestCase

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tests", "fixtures", "variable.jsonl")


class VariableFieldTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.path("variable.db")
        self.report = import_capture(FIXTURE, self.db)
        self.store = ResultStore(self.db)

    def _record(self, line: int) -> dict:
        rows = self.store.query_records(capture_line=line)
        self.assertTrue(rows, f"第 {line} 行没有记录")
        return rows[0]

    # -------------------------------------------------------------- 三记录
    def test_zero_254_and_255_byte_variable_payloads(self) -> None:
        first = self._record(1)
        fields = first["fields"]
        # 消息头 16，Template Set 16..59（含 3 字节 Set 级填充），Data Set 头 60..63
        # 记录 0 从 64 开始；Template Set 里 300 的字段说明结束于 64。
        self.assertEqual(first["record_offset"], 79)
        self.assertEqual(fields[0]["value"], "192.0.2.1")
        self.assertEqual(fields[1]["element_id"], 999)
        self.assertEqual(fields[1]["actual_length"], 0)
        self.assertEqual(fields[1]["value_hex"], "")
        self.assertEqual(fields[1]["value_range"], [84, 84])
        self.assertEqual(fields[1]["var_prefix_range"], [83, 84])
        # 非零企业号变长字段：255 字节走 0xff + u16 前缀，前缀 3 字节
        self.assertEqual(fields[3]["element_id"], 4000)
        self.assertEqual(fields[3]["enterprise"], 9)
        self.assertEqual(fields[3]["actual_length"], 255)
        self.assertEqual(fields[3]["value_range"], [89, 344])
        self.assertEqual(fields[3]["var_prefix_range"], [86, 89])
        # 变长字段后面的已知字段位置不能被前缀或载荷挤偏
        self.assertEqual(fields[4]["element_id"], 2)
        self.assertEqual(fields[4]["value"], "10")
        self.assertEqual(fields[4]["value_range"], [344, 348])
        self.assertEqual(fields[5]["value"], "2001:db8::1")
        self.assertEqual(fields[6]["value"], "192.0.2.2")
        self.assertEqual(fields[7]["value"], "6")
        # 元素 777 是变长未知字段，载荷是 2^64-1 的 8 字节原文
        self.assertEqual(fields[8]["value_hex"], "ffffffffffffffff")
        self.assertEqual(fields[8]["value"], "ffffffffffffffff")

        records = self.store.query_records(capture_line=1)
        self.assertEqual(len(records), 3)
        second = records[1]
        self.assertEqual(second["fields"][1]["actual_length"], 254)
        self.assertEqual(second["fields"][1]["var_prefix_range"][1] -
                         second["fields"][1]["var_prefix_range"][0], 1)
        self.assertEqual(second["fields"][3]["actual_length"], 0)
        self.assertEqual(second["fields"][4]["value"], "11")
        self.assertEqual(second["fields"][5]["value"], "::1")
        third = records[2]
        self.assertEqual(third["fields"][1]["actual_length"], 255)
        self.assertEqual(third["fields"][1]["var_prefix_range"][1] -
                         third["fields"][1]["var_prefix_range"][0], 3)
        self.assertEqual(third["fields"][3]["actual_length"], 3)
        self.assertEqual(third["fields"][3]["value_hex"], "aabbcc")

    def test_variable_prefix_does_not_enter_payload(self) -> None:
        for line in (1, 3):
            for rec in self.store.query_records(capture_line=line):
                for f in rec["fields"]:
                    if f["var_prefix_range"] is None:
                        continue
                    prefix_end = f["var_prefix_range"][1]
                    self.assertEqual(f["value_range"][0], prefix_end)
                    self.assertEqual(
                        len(bytes.fromhex(f["value_hex"])),
                        f["actual_length"])

    def test_enterprise_field_after_known_fields_and_zero_length_set(
            self) -> None:
        # 第 3 行：Data Set 头 16..19，记录 0 从 20 开始
        rec = self._record(3)
        self.assertEqual(rec["record_offset"], 24)
        fields = rec["fields"]
        self.assertEqual(fields[0]["value_range"], [24, 28])
        self.assertEqual(fields[1]["value_range"], [29, 29])
        self.assertEqual(fields[2]["value_range"], [29, 31])
        self.assertEqual(fields[3]["value_range"], [32, 32])
        self.assertEqual(fields[4]["value_range"], [32, 36])
        self.assertEqual(fields[6]["value"], "222.0.0.2")
        self.assertEqual(fields[7]["value"], "58")
        self.assertEqual(fields[8]["value_hex"], "0000000000000002")

    # ------------------------------------------------------------ 重复元素
    def test_repeated_element_keeps_both_positions(self) -> None:
        fields = self._record(1)["fields"]
        repeated = [f for f in fields if f["element_id"] == 8
                    and f["enterprise"] == 0]
        self.assertEqual(len(repeated), 2)
        self.assertEqual(repeated[0]["field_ordinal"], 0)
        self.assertEqual(repeated[0]["value"], "192.0.2.1")
        self.assertEqual(repeated[1]["field_ordinal"], 6)
        self.assertEqual(repeated[1]["value"], "192.0.2.2")
        trace = self.store.trace_field(1, 0)
        self.assertEqual(trace["value_range"], [79, 83])
        trace6 = self.store.trace_field(1, 6)
        self.assertEqual(trace6["value_range"], [364, 368])
        self.assertEqual(trace6["sliced_hex"], "c0000202")

    def test_enterprise_same_element_number_not_iana(self) -> None:
        fields = self._record(1)["fields"]
        ent8 = [f for f in fields if f["enterprise"] == 4242]
        self.assertEqual(len(ent8), 1)
        self.assertEqual(ent8[0]["element_id"], 8)
        self.assertEqual(ent8[0]["value_type"], "raw_bytes")
        self.assertEqual(ent8[0]["value"], "0000000a")
        self.assertIsNone(ent8[0]["name"])

    # ------------------------------------------------------- 同 Set 多代次
    def test_multiple_definitions_in_one_template_set(self) -> None:
        events = self.store.list_template_events(domain_id=1)
        self.assertEqual(
            [(e["capture_line"], e["template_id"], e["action"], e["generation"])
             for e in events],
            [(1, 300, "define", 1),
             (4, 302, "define", 1), (4, 302, "define", 2),
             (4, 302, "revoke", None), (4, 302, "define", 3),
             (7, 301, "define", 1)])
        rec = self._record(5)
        self.assertEqual(rec["template_id"], 302)
        self.assertEqual(rec["template_generation"], 3)
        self.assertEqual(rec["fields"][1]["name"], "flowStartMilliseconds")

    # ------------------------------------------------------------ Set 填充
    def test_zero_to_three_byte_set_padding(self) -> None:
        # 8 行带 3 字节填充，10 行 1 字节，11 行 2 字节；均不产出幽灵记录
        counts = {line: len(self.store.query_records(capture_line=line))
                  for line in (8, 9, 10, 11)}
        self.assertEqual(counts, {8: 2, 9: 1, 10: 1, 11: 1})
        # 9 行刚好无填充
        self.assertEqual(self.store.get_message(9)["record_count"], 1)

    def test_compact_one_and_three_byte_counts(self) -> None:
        fields = self._record(8)["fields"]
        self.assertEqual(fields[1]["declared_length"], 1)
        self.assertEqual(fields[1]["value"], "0")
        self.assertEqual(fields[2]["declared_length"], 3)
        self.assertEqual(fields[2]["value"], "1")
        second = self.store.query_records(capture_line=8)[1]
        self.assertEqual(second["fields"][1]["value"], "255")
        self.assertEqual(second["fields"][2]["value"], str(0xFFFFFF))

    # ---------------------------------------------------------------- 追查
    def test_trace_variable_field_slice_and_prefix(self) -> None:
        trace = self.store.trace_field(1, 3)
        self.assertEqual(trace["var_prefix_range"], [86, 89])
        self.assertEqual(trace["value_range"], [89, 344])
        self.assertEqual(trace["template_field_range"], [36, 44])
        self.assertEqual(trace["sliced_hex"], "41" * 255)
        self.assertEqual(trace["actual_length"], 255)
        self.assertTrue(trace["var_prefix_extended"])

    def test_trace_pure_header_message_has_no_records(self) -> None:
        self.assertEqual(self.store.get_message(2)["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
