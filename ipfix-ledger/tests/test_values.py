"""已知元素解码：整数宽度、IPv6 文本、UTC 毫秒、企业字段保持原始字节。"""
from __future__ import annotations

import unittest

from ipfix_offline import ResultStore, import_capture
from ipfix_offline.values import format_ipv6

from ._helpers import TempDirTestCase, line, write_jsonl


def message(seq: int, domain: int, sets: list[bytes]) -> bytes:
    body = b"".join(sets)
    return (b"\x00\x0a" + (16 + len(body)).to_bytes(2, "big")
            + (1700000000).to_bytes(4, "big") + seq.to_bytes(4, "big")
            + domain.to_bytes(4, "big") + body)


def st(set_id: int, payload: bytes) -> bytes:
    return (set_id.to_bytes(2, "big")
            + (4 + len(payload)).to_bytes(2, "big") + payload)


def template(template_id: int, fields: list[tuple[int, int, int]]) -> bytes:
    body = b""
    for element_id, length, enterprise in fields:
        if enterprise:
            body += ((element_id | 0x8000).to_bytes(2, "big")
                     + length.to_bytes(2, "big")
                     + enterprise.to_bytes(4, "big"))
        else:
            body += (element_id.to_bytes(2, "big")
                     + length.to_bytes(2, "big"))
    return (template_id.to_bytes(2, "big")
            + len(fields).to_bytes(2, "big") + body)


class KnownValueTestCase(TempDirTestCase):
    def _import(self, template_fields, payload: bytes) -> ResultStore:
        capture = self.path("values.jsonl")
        write_jsonl(capture, [
            line("e", "t", message(0, 1, [
                st(2, template(256, template_fields)),
                st(256, payload)])),
        ])
        db = self.path("values.db")
        import_capture(capture, db)
        return ResultStore(db)

    def test_every_known_element_type(self) -> None:
        fields = [
            (1, 8, 0),    # octetDeltaCount
            (2, 4, 0),    # packetDeltaCount
            (7, 2, 0),    # sourceTransportPort
            (11, 2, 0),   # destinationTransportPort（恰 2 字节）
            (4, 1, 0),    # protocolIdentifier
            (8, 4, 0),    # sourceIPv4Address
            (12, 4, 0),   # destinationIPv4Address
            (27, 16, 0),  # sourceIPv6Address
            (28, 16, 0),  # destinationIPv6Address
            (152, 8, 0),  # flowStartMilliseconds
            (153, 8, 0),  # flowEndMilliseconds
        ]
        payload = (bytes.fromhex("ffffffffffffffff")
                   + bytes.fromhex("7fffffff")
                   + bytes.fromhex("c350")
                   + bytes.fromhex("0016")
                   + bytes.fromhex("2f")
                   + bytes.fromhex("0a000001")
                   + bytes.fromhex("c0000201")
                   + bytes.fromhex("00000000000000000000000000000001")
                   + bytes.fromhex("fe800000000000000000000000000001")
                   + (1_700_000_000_123).to_bytes(8, "big")
                   + (1_700_000_000_584).to_bytes(8, "big"))
        store = self._import(fields, payload)
        rec = store.query_records()[0]
        got = [(f["element_id"], f["value_type"], f["value"])
               for f in rec["fields"]]
        self.assertEqual(got, [
            (1, "uint64", "18446744073709551615"),
            (2, "uint64", str(0x7FFFFFFF)),
            (7, "uint16", "50000"),
            (11, "uint16", "22"),
            (4, "uint8", "47"),
            (8, "ipv4", "10.0.0.1"),
            (12, "ipv4", "192.0.2.1"),
            (27, "ipv6", "::1"),
            (28, "ipv6", "fe80::1"),
            (152, "utc_milliseconds", "1700000000123"),
            (153, "utc_milliseconds", "1700000000584"),
        ])
        # 整数保存为十进制字符串，避免 JSON/JS 精度损失；SQLite 内不存 2^64-1
        self.assertIsInstance(rec["fields"][0]["value"], str)

    def test_octet_delta_count_accepts_one_through_eight_bytes(self) -> None:
        # 模板至少 4 字节 → 1/2/3 字节计数字段必须搭配别的 >=4 字节字段；
        # 这里用 65535 变长前缀（最小 1 字节）+ 3 字节 protocol 垫片，
        # 保证模板最小记录长度 >= 4，同时验证 1..3 字节整数解码。
        for length in range(4, 9):
            with self.subTest(length=length):
                db = self.path(f"octets-{length}.db")
                capture = self.path(f"octets-{length}.jsonl")
                raw = b"\xff" * length
                write_jsonl(capture, [
                    line("e", "t", message(0, 1, [
                        st(2, template(256, [(1, length, 0)])),
                        st(256, raw)])),
                ])
                import_capture(capture, db)
                rec = ResultStore(db).query_records()[0]
                self.assertEqual(rec["fields"][0]["value_type"], "uint64")
                self.assertEqual(rec["fields"][0]["value"],
                                 str(int.from_bytes(raw, "big")))
                self.assertEqual(rec["fields"][0]["actual_length"], length)

    def test_compact_one_byte_count_with_other_fields(self) -> None:
        # 8/4 + 1/1 + 2/3 = 8 字节最小记录；1 字节计数照常解码
        fields = [(8, 4, 0), (1, 1, 0), (2, 3, 0), (999, 65535, 0)]
        payload = (bytes.fromhex("c0000201") + b"\xff"
                   + (0xFFFFFF).to_bytes(3, "big") + b"\x00")
        rec = self._import(fields, payload).query_records()[0]
        self.assertEqual(rec["fields"][1]["value"], "255")
        self.assertEqual(rec["fields"][1]["declared_length"], 1)
        self.assertEqual(rec["fields"][2]["value"], "16777215")

    def test_octet_delta_count_variable_length_is_illegal(self) -> None:
        capture = self.path("bad.jsonl")
        write_jsonl(capture, [line("e", "t", message(0, 1, [
            st(2, template(256, [(1, 65535, 0)]))]))])
        from ipfix_offline import IpfixInputError
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(capture, self.path("bad.db"))
        self.assertIn("octetDeltaCount", ctx.exception.failure.detail)

    def test_enterprise_same_element_id_is_raw(self) -> None:
        fields = [(8, 4, 0), (8, 4, 4242), (1, 8, 4242)]
        payload = (bytes.fromhex("c0000201")
                   + bytes.fromhex("c0000202")
                   + bytes.fromhex("ffffffffffffffff"))
        rec = self._import(fields, payload).query_records()[0]
        self.assertEqual(rec["fields"][1]["value_type"], "raw_bytes")
        self.assertEqual(rec["fields"][1]["value"], "c0000202")
        self.assertIsNone(rec["fields"][1]["name"])
        self.assertEqual(rec["fields"][2]["value_type"], "raw_bytes")
        self.assertEqual(rec["fields"][2]["value"], "ffffffffffffffff")
        # 同名 IANA 字段的第一个仍按 IPv4 解释
        self.assertEqual(rec["fields"][0]["value"], "192.0.2.1")

    def test_unknown_element_kept_as_raw_and_consumed_fully(self) -> None:
        fields = [(600, 5, 0), (8, 4, 0)]
        payload = bytes.fromhex("0102030405c0000201")
        rec = self._import(fields, payload).query_records()[0]
        self.assertEqual(rec["fields"][0]["value"], "0102030405")
        self.assertEqual(rec["fields"][0]["value_type"], "raw_bytes")
        self.assertEqual(rec["fields"][1]["value"], "192.0.2.1")

    def test_ipv6_textual_forms(self) -> None:
        cases = {
            "20010db8000000000000000000000001": "2001:db8::1",
            "00000000000000000000000000000000": "::",
            "00000000000000000000000000000001": "::1",
            "fe800000000000000000000000000001": "fe80::1",
            "20010db8000000010000000000000001": "2001:db8:0:1::1",
            "20010db8000100020003000400050006": "2001:db8:1:2:3:4:5:6",
            "00010002000300040005000600070008": "1:2:3:4:5:6:7:8",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(format_ipv6(bytes.fromhex(raw)), expected)

    def test_integer_values_round_trip_without_float(self) -> None:
        big = 2 ** 63 + 12345
        fields = [(1, 8, 0)]
        rec = self._import(fields, big.to_bytes(8, "big")).query_records()[0]
        self.assertEqual(rec["fields"][0]["value"], str(big))


if __name__ == "__main__":
    unittest.main()
