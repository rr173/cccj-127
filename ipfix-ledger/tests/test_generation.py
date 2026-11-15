"""模板换代、作用域隔离、序列号计数与 wrap 的逐字段核对。

期望值全部手算（见每条测试的注释与 tests/fixtures/README.md 的偏移表），
不调用被测解码器生成期望。
"""
from __future__ import annotations

import json
import os
import unittest

from ipfix_offline import ResultStore, import_capture

from ._helpers import TempDirTestCase

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tests", "fixtures", "generation.jsonl")


class GenerationTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.path("generation.db")
        self.report = import_capture(FIXTURE, self.db)
        self.store = ResultStore(self.db)

    # --------------------------------------------------------------- 逐字段
    def test_record_one_uses_generation_one_and_uint64_max(self) -> None:
        rec = self.store.get_record(1)
        self.assertEqual(rec["capture_line"], 1)
        self.assertEqual(rec["set_ordinal"], 1)
        self.assertEqual(rec["record_in_set"], 0)
        self.assertEqual(rec["template_id"], 256)
        self.assertEqual(rec["template_generation"], 1)
        self.assertEqual(rec["record_offset"], 36)
        self.assertEqual(rec["record_length"], 12)
        self.assertEqual(rec["sequence_number"], 1000)
        self.assertEqual(rec["domain_id"], 7)
        fields = rec["fields"]
        self.assertEqual(len(fields), 2)
        self.assertEqual(
            [f["element_id"] for f in fields], [8, 1])
        self.assertEqual(fields[0]["value"], "192.0.2.1")
        self.assertEqual(fields[0]["value_hex"], "c0000201")
        self.assertEqual(fields[0]["value_range"], [36, 40])
        self.assertEqual(fields[0]["template_field_range"], [24, 28])
        self.assertEqual(fields[1]["value_type"], "uint64")
        self.assertEqual(fields[1]["value"], "18446744073709551615")
        self.assertEqual(fields[1]["value_hex"], "ffffffffffffffff")
        self.assertEqual(fields[1]["value_range"], [40, 48])
        self.assertEqual(fields[1]["template_field_range"], [28, 32])

    def test_generation_two_records_are_not_backfilled_by_final_template(self) -> None:
        records = self.store.query_records(capture_line=1)
        self.assertEqual(len(records), 3)
        second, third = records[1], records[2]
        self.assertEqual(second["template_generation"], 2)
        self.assertEqual(third["template_generation"], 2)
        self.assertEqual(
            [f["element_id"] for f in second["fields"]], [8, 12, 2])
        self.assertEqual(
            [f["value"] for f in second["fields"]],
            ["192.0.2.2", "198.51.100.1", "10"])
        self.assertEqual(
            [f["value_range"] for f in second["fields"]],
            [[72, 76], [76, 80], [80, 84]])
        self.assertEqual(
            [f["template_field_range"] for f in second["fields"]],
            [[56, 60], [60, 64], [64, 68]])
        self.assertEqual(
            [f["value"] for f in third["fields"]],
            ["192.0.2.3", "198.51.100.2", "11"])
        # packetDeltaCount 是 4 字节，不能按代次 1 的 8 字节 octetDeltaCount 回解
        self.assertEqual([f["declared_length"] for f in second["fields"]],
                         [4, 4, 4])
        # 反例：若用最终模板（8/4,12/4,2/4）回头解释第一条记录，
        # 8 字节计数字段会消失、代次也会变成 2
        first = self.store.get_record(1)
        self.assertEqual(first["template_generation"], 1)
        self.assertNotEqual(first["template_generation"],
                            second["template_generation"])
        self.assertEqual(len(first["fields"]), 2)
        self.assertNotEqual(first["fields"][1]["declared_length"], 4)

    def test_sequence_advances_by_decoded_record_count_only(self) -> None:
        # 第 1 行解出 3 条记录（不是 4 个 Set，也不是模板记录数），
        # 第 2 行序列号必须恰好是 1000+3。
        msg1 = self.store.get_message(1)
        msg2 = self.store.get_message(2)
        self.assertEqual(msg1["sequence_number"], 1000)
        self.assertEqual(msg1["record_count"], 3)
        self.assertEqual(msg2["sequence_number"], 1003)
        # 反例：把 Set 数（4）或模板消息的字节/记录数计入会得到 1004/1005
        self.assertNotEqual(msg2["sequence_number"], 1000 + 4)
        self.assertNotEqual(msg2["sequence_number"], 1001)
        self.assertNotIn(msg2["sequence_number"], (1004, 1005, 1006))
        self.assertEqual(msg2["record_count"], 1)
        self.assertEqual(
            [r["capture_line"] for r in self.store.query_records()][:4],
            [1, 1, 1, 2])
        # 记录里带消息头与所用代次
        self.assertEqual(msg2["domain_id"], 7)
        self.assertEqual(msg2["export_time"], 1700000000)

    def test_message_with_wrong_sequence_is_rejected(self) -> None:
        from ._helpers import write_jsonl
        tail = [json.loads(line) for line in open(FIXTURE, encoding="utf-8")]
        tail = tail[:2]
        # 下一行本应 1003+1=1004，改成 1005
        third = json.loads(json.dumps(tail[1]))
        raw = bytearray.fromhex(third["message_hex"])
        raw[8:12] = (1005).to_bytes(4, "big")
        third["message_hex"] = raw.hex()
        capture = self.path("bad_seq.jsonl")
        write_jsonl(capture, tail + [third])
        err = self.fail_with(capture)
        self.assertEqual(err.failure.phase, "sequence")
        self.assertEqual(err.failure.capture_line, 3)

    # ----------------------------------------------------- 作用域/会话隔离
    def test_same_template_id_isolated_by_transport_and_domain(self) -> None:
        transport_b = self.store.query_records(transport_id="transport-B")
        self.assertEqual(len(transport_b), 2)
        self.assertEqual(
            [f["element_id"] for f in transport_b[0]["fields"]], [8, 2])
        self.assertEqual(
            [f["value"] for f in transport_b[0]["fields"]], ["10.0.0.1", "1"])
        # transport-B 的 256 与 transport-A 的 256 代次各自从 1 开始
        self.assertEqual(transport_b[0]["template_generation"], 1)
        domain9 = self.store.query_records(domain_id=9)
        self.assertEqual(len(domain9), 1)
        self.assertEqual(domain9[0]["exporter_id"], "exporter-1")
        self.assertEqual(
            [f["element_id"] for f in domain9[0]["fields"]], [8, 4, 11])
        self.assertEqual(domain9[0]["sequence_number"], 0)

    def test_template_a_not_reused_across_scope(self) -> None:
        """transport-B 的第 4 行只定义了 (8,2)；不能用 transport-A 的 (8,1)。"""
        rec = self.store.query_records(transport_id="transport-B")[0]
        self.assertNotIn(1, [f["element_id"] for f in rec["fields"]])

    # ------------------------------------------------------- 撤销/重定义
    def test_revoke_then_redefine_new_generation(self) -> None:
        events = self.store.list_template_events(
            exporter_id="exporter-1", transport_id="transport-A", domain_id=7)
        actions = [(e["capture_line"], e["action"], e["generation"])
                   for e in events]
        self.assertEqual(actions, [
            (1, "define", 1), (1, "define", 2),
            (7, "revoke", None),
            (8, "define", 3), (9, "define", 4)])
        # 相同布局再次定义也产生新代次；第 10 行的记录使用代次 4
        rec = self.store.query_records(capture_line=10)[0]
        self.assertEqual(rec["template_generation"], 4)
        self.assertEqual(
            [f["element_id"] for f in rec["fields"]], [8, 152, 153])
        self.assertEqual(rec["fields"][1]["value"], "1700000000123")
        self.assertEqual(rec["fields"][1]["value_type"], "utc_milliseconds")
        self.assertEqual(rec["fields"][2]["value"], "1700000000584")

    def test_old_records_keep_template_basis_after_revoke(self) -> None:
        trace = self.store.trace_field(1, 1)
        self.assertEqual(trace["template_generation"], 1)
        self.assertEqual(trace["template_event"]["template_id"], 256)
        self.assertEqual(trace["template_event"]["capture_line"], 1)
        self.assertEqual(trace["sliced_hex"], "ffffffffffffffff")
        self.assertEqual(trace["template_capture_line"], 1)
        # 模板字段说明位置也真的能切回该字段的 element/length 编码
        self.assertEqual(trace["template_sliced_hex"], "00010008")
        self.assertEqual(trace["template_field_spec"],
                         {"spec_offset": 28, "spec_length": 4})
        # 代次 1 的事件仍可单独查到，不被后面的定义覆盖
        gen1 = self.store.get_template(
            "exporter-1", "transport-A", 7, 256, generation=1)
        self.assertEqual(
            [f["element_id"] for f in gen1["fields"]], [8, 1])

    def test_pure_template_message_produces_no_record(self) -> None:
        self.assertEqual(self.store.get_message(3)["record_count"], 0)
        self.assertEqual(self.store.get_message(3)["record_ordinals"], [])

    # ------------------------------------------------------ 2^32 序列号绕回
    def test_sequence_wraps_mod_2_pow_32(self) -> None:
        rows = self.store.query_records(exporter_id="exporter-2")
        self.assertEqual(
            [r["sequence_number"] for r in rows],
            [4294967290, 4294967290,
             4294967292, 4294967292,
             4294967294, 4294967294,
             0])
        # 4294967290 + 2 条 = 4294967292；... + 2 = 4294967294；+ 2 绕回 0
        self.assertEqual(
            [f["value"] for f in rows[-1]["fields"]], ["10.0.0.16", "7"])

    # ---------------------------------------------------------- 查询顺序
    def test_order_is_capture_line_set_and_record(self) -> None:
        rows = self.store.query_records()
        keys = [(r["capture_line"], r["set_ordinal"], r["record_in_set"])
                for r in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(rows[0]["record_ordinal"], 1)
        self.assertEqual(keys[:3], [(1, 1, 0), (1, 3, 0), (1, 3, 1)])


if __name__ == "__main__":
    unittest.main()
