"""捕获 JSONL 严格性、目标库不可覆盖、批次/缓存参数与资源上限。"""
from __future__ import annotations

import json
import os
import unittest

from ipfix_offline import (
    ConfigError,
    IpfixInputError,
    ResultStore,
    TargetExistsError,
    import_capture,
)

from ._helpers import TempDirTestCase, raw_lines, write_jsonl

GOOD_ROW = {
    "exporter_id": "e1",
    "transport_id": "t1",
    "message_hex": "000a001c6553f10000000000000000070002000c0100000100080004",
}
# 上一行：完整消息头（16 字节，length=28）+ 一个 Template Set，
# 256:(sourceIPv4Address/4, fieldCount=1)，sequence=0，domain=7；不产生记录。


class CaptureStrictnessTestCase(TempDirTestCase):
    def _capture(self, text: str) -> str:
        path = self.path("bad.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def _assert_capture_fails(self, text: str, *, line: int,
                              db_name: str = "bad.db") -> IpfixInputError:
        path = self._capture(text)
        db = self.path(db_name)
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(path, db)
        self.assertEqual(ctx.exception.failure.capture_line, line)
        self.assertEqual(ctx.exception.failure.phase, "capture")
        self.assertFalse(os.path.exists(db))
        return ctx.exception

    def _good_line(self) -> str:
        return json.dumps(GOOD_ROW, separators=(",", ":"))

    def test_empty_line_rejected(self) -> None:
        err = self._assert_capture_fails(self._good_line() + "\n\n", line=2)
        self.assertIn("空行", err.failure.reason)

    def test_blank_only_line_rejected_even_with_crlf(self) -> None:
        self._assert_capture_fails(
            self._good_line() + "\r\n \r\n", line=2)

    def test_duplicate_json_key_rejected(self) -> None:
        row = ('{"exporter_id":"e1","exporter_id":"e2",'
               '"transport_id":"t1","message_hex":"00"}')
        err = self._assert_capture_fails(row + "\n", line=1)
        self.assertIn("重复 JSON 键", err.failure.reason)

    def test_missing_field_rejected(self) -> None:
        row = {k: v for k, v in GOOD_ROW.items() if k != "transport_id"}
        self._assert_capture_fails(json.dumps(row) + "\n", line=1)

    def test_extra_field_rejected(self) -> None:
        row = dict(GOOD_ROW, note="extra")
        self._assert_capture_fails(json.dumps(row) + "\n", line=1)

    def test_empty_identifier_rejected(self) -> None:
        row = dict(GOOD_ROW, exporter_id="")
        self._assert_capture_fails(json.dumps(row) + "\n", line=1)

    def test_non_string_identifier_rejected(self) -> None:
        row = dict(GOOD_ROW, transport_id=17)
        self._assert_capture_fails(json.dumps(row) + "\n", line=1)

    def test_bad_hex_rejected(self) -> None:
        for bad in ("0g", "00a", "00 0a"):
            with self.subTest(hex=bad):
                row = dict(GOOD_ROW, message_hex=bad)
                err = self._assert_capture_fails(json.dumps(row) + "\n",
                                                 line=1)
                self.assertIn("message_hex", err.failure.reason)

    def test_non_utf8_rejected(self) -> None:
        path = self.path("bad.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe\x00")
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(path, self.path("x.db"))
        self.assertEqual(ctx.exception.failure.capture_line, 1)
        self.assertFalse(os.path.exists(self.path("x.db")))

    def test_duplicate_exporter_strings_preserved(self) -> None:
        """exporter_id 原样保留：前后空格也是不同作用域，不做 strip。"""
        db = self.path("ok.db")
        path = self.path("ok.jsonl")
        row = json.dumps(GOOD_ROW)
        write_jsonl(path, [GOOD_ROW])
        import_capture(path, db)
        store = ResultStore(db)
        self.assertEqual(store.get_message(1)["exporter_id"], "e1")


class TargetProtectionTestCase(TempDirTestCase):
    def test_existing_database_is_not_overwritten(self) -> None:
        db = self.path("existing.db")
        with open(db, "wb") as fh:
            fh.write(b"keep-me")
        before = open(db, "rb").read()
        capture = self.path("cap.jsonl")
        write_jsonl(capture, [GOOD_ROW])
        with self.assertRaises(TargetExistsError) as ctx:
            import_capture(capture, db)
        self.assertEqual(ctx.exception.path, db)
        self.assertEqual(open(db, "rb").read(), before)
        with open(db, "rb") as fh:
            self.assertEqual(fh.read(), b"keep-me")

    def test_success_creates_db_atomically(self) -> None:
        db = self.path("ok.db")
        capture = self.path("cap.jsonl")
        write_jsonl(capture, [GOOD_ROW])
        report = import_capture(capture, db)
        self.assertEqual(report.messages, 1)
        self.assertTrue(os.path.isfile(db))
        self.assertEqual(ResultStore(db).summary()["records"], 0)

    def test_input_is_opened_read_only(self) -> None:
        capture = self.path("cap.jsonl")
        write_jsonl(capture, [GOOD_ROW])
        before = open(capture, "rb").read()
        import_capture(capture, self.path("ok.db"))
        self.assertEqual(open(capture, "rb").read(), before)


class LimitsTestCase(TempDirTestCase):
    def _template_capture(self, count: int) -> str:
        """一行里定义 count 个不同模板号（256..），每个布局 (8/4)。"""
        fields = b"\x00\x08\x00\x04"
        body = b""
        for template_id in range(256, 256 + count):
            body += template_id.to_bytes(2, "big") + (1).to_bytes(2, "big") \
                + fields
        set_bytes = (2).to_bytes(2, "big") + (4 + len(body)).to_bytes(2, "big") \
            + body
        length = 16 + len(set_bytes)
        message = (b"\x00\x0a" + length.to_bytes(2, "big") + b"\x00" * 8
                   + (7).to_bytes(4, "big") + set_bytes)
        path = self.path("many.jsonl")
        write_jsonl(path, [{"exporter_id": "e", "transport_id": "t",
                            "message_hex": message.hex()}])
        return path

    def test_template_limit_configured_and_explicit(self) -> None:
        capture = self._template_capture(5)
        db = self.path("limit.db")
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(capture, db, max_templates_per_scope=4)
        self.assertIn("模板数超过可配置上限", ctx.exception.failure.reason)
        self.assertEqual(ctx.exception.failure.capture_line, 1)
        self.assertFalse(os.path.exists(db))
        report = import_capture(capture, self.path("ok.db"),
                                max_templates_per_scope=5)
        self.assertEqual(report.template_definitions, 5)

    def test_redefinition_does_not_count_as_new_limit_slot(self) -> None:
        t = (b"\x01\x00\x00\x01\x00\x08\x00\x04")
        body = t + (b"\x01\x00\x00\x01\x00\x08\x00\x04")
        set_bytes = (2).to_bytes(2, "big") + (4 + len(body)).to_bytes(2, "big") \
            + body
        length = 16 + len(set_bytes)
        message = (b"\x00\x0a" + length.to_bytes(2, "big") + b"\x00" * 8
                   + (7).to_bytes(4, "big") + set_bytes)
        path = self.path("redef.jsonl")
        write_jsonl(path, [{"exporter_id": "e", "transport_id": "t",
                            "message_hex": message.hex()}])
        report = import_capture(path, self.path("ok.db"),
                                max_templates_per_scope=1)
        self.assertEqual(report.template_definitions, 2)

    def test_bad_batch_and_cache_settings(self) -> None:
        capture = self.path("cap.jsonl")
        write_jsonl(capture, [GOOD_ROW])
        for kwargs in ({"batch_size": 0}, {"batch_size": -1},
                       {"cache_kb": 0}, {"max_templates_per_scope": 0},
                       {"batch_size": True}):
            with self.subTest(**kwargs):
                with self.assertRaises(ConfigError):
                    import_capture(capture, self.path("x.db"), **kwargs)
                self.assertFalse(os.path.exists(self.path("x.db")))


if __name__ == "__main__":
    unittest.main()
