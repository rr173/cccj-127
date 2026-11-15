"""边界与上下文：UTF-8 捕获、Set/消息边界的严格解释、失败原子性、只读输入。"""
from __future__ import annotations

import builtins
import gzip
import json
import os
import sys
import unittest
from unittest import mock

from ipfix_offline import IpfixInputError, ResultStore, import_capture
from ipfix_offline.capture import iter_capture

from ._helpers import TempDirTestCase, write_jsonl


def H(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


def message(seq: int, domain: int, sets: list[bytes]) -> bytes:
    body = b"".join(sets)
    length = 16 + len(body)
    return (b"\x00\x0a" + length.to_bytes(2, "big")
            + (1700000000).to_bytes(4, "big") + seq.to_bytes(4, "big")
            + domain.to_bytes(4, "big") + body)


def st(set_id: int, payload: bytes) -> bytes:
    return (set_id.to_bytes(2, "big")
            + (4 + len(payload)).to_bytes(2, "big") + payload)


def tmpl(template_id: int, fields: list[tuple[int, int]]) -> bytes:
    body = b"".join(e.to_bytes(2, "big") + l.to_bytes(2, "big")
                    for e, l in fields)
    return template_id.to_bytes(2, "big") + len(fields).to_bytes(2, "big") + body


class BoundaryTestCase(TempDirTestCase):
    def test_utf8_non_ascii_identifiers_round_trip(self) -> None:
        capture = self.path("utf8.jsonl")
        write_jsonl(capture, [{
            "exporter_id": "设备-甲",
            "transport_id": "会话-1",
            "message_hex": message(0, 7, [st(2, tmpl(256, [(8, 4)]))]).hex(),
        }])
        db = self.path("utf8.db")
        import_capture(capture, db)
        store = ResultStore(db)
        self.assertEqual(store.get_message(1)["exporter_id"], "设备-甲")
        self.assertEqual(store.get_message(1)["transport_id"], "会话-1")

    def test_crlf_line_endings_accepted(self) -> None:
        capture = self.path("crlf.jsonl")
        row = json.dumps({"exporter_id": "e", "transport_id": "t",
                          "message_hex": message(
                              0, 7, [st(2, tmpl(256, [(8, 4)]))]).hex()})
        with open(capture, "wb") as fh:
            fh.write(row.encode() + b"\r\n")
        import_capture(capture, self.path("crlf.db"))

    def test_trailing_newline_is_not_an_extra_record(self) -> None:
        capture = self.path("one.jsonl")
        write_jsonl(capture, [{
            "exporter_id": "e", "transport_id": "t",
            "message_hex": message(0, 7, [st(2, tmpl(256, [(8, 4)]))]).hex(),
        }])
        report = import_capture(capture, self.path("one.db"))
        self.assertEqual(report.capture_lines, 1)

    def test_two_templates_in_same_set_processed_in_order(self) -> None:
        # 同一 Template Set 内定义 256 两次：第二条立即替换第一条。
        payload = tmpl(256, [(8, 4)]) + tmpl(256, [(8, 4), (2, 4)])
        capture = self.path("multi.jsonl")
        write_jsonl(capture, [
            {"exporter_id": "e", "transport_id": "t",
             "message_hex": message(0, 7, [st(2, payload)]).hex()},
            {"exporter_id": "e", "transport_id": "t",
             "message_hex": message(0, 7, [st(256, H("0a000001 00000005"))]).hex()},
        ])
        db = self.path("multi.db")
        report = import_capture(capture, db)
        self.assertEqual(report.records, 1)
        rec = ResultStore(db).query_records()[0]
        self.assertEqual(rec["template_generation"], 2)
        self.assertEqual([f["element_id"] for f in rec["fields"]], [8, 2])

    def test_data_set_cannot_borrow_bytes_from_next_set(self) -> None:
        # 记录需要 8 字节，本 Set 只有 6 字节；后面的 Set 头不能补
        payload = st(256, H("c0000201 0000")) + st(2, tmpl(257, [(8, 4)]))
        capture = self.path("borrow.jsonl")
        write_jsonl(capture, [
            {"exporter_id": "e", "transport_id": "t",
             "message_hex": message(0, 7, [
                 st(2, tmpl(256, [(8, 4), (2, 4)])), payload]).hex()},
        ])
        err = self.fail_with(capture)
        self.assertEqual(err.failure.phase, "data")
        self.assertEqual(err.failure.set_ordinal, 1)

    def test_record_minimum_4_bytes_required_at_definition(self) -> None:
        capture = self.path("short.jsonl")
        write_jsonl(capture, [{
            "exporter_id": "e", "transport_id": "t",
            "message_hex": message(0, 7, [
                st(2, H("0100 0001 0004 0001"))]).hex()}])
        err = self.fail_with(capture)
        self.assertEqual(err.failure.phase, "template")
        self.assertIn("最小数据记录长度", err.failure.reason)

    def test_empty_capture_is_accepted_as_zero_messages(self) -> None:
        capture = self.path("empty.jsonl")
        open(capture, "w").close()
        report = import_capture(capture, self.path("empty.db"))
        self.assertEqual(report.capture_lines, 0)
        self.assertEqual(report.records, 0)
        self.assertEqual(ResultStore(self.path("empty.db")).summary()["records"], 0)

    def test_streaming_reader_does_not_load_all_lines(self) -> None:
        """用生成器逐行消费：迭代器在读完前就能产出第一行。"""
        capture = self.path("stream.jsonl")
        with open(capture, "w", encoding="utf-8") as fh:
            for _ in range(5):
                fh.write(json.dumps({
                    "exporter_id": "e", "transport_id": "t",
                    "message_hex": message(0, 7, []).hex()}) + "\n")
        it = iter_capture(capture)
        first = next(it)
        self.assertEqual(first.line_no, 1)


class FailureAtomicityTestCase(TempDirTestCase):
    def test_partial_batches_removed_on_late_failure(self) -> None:
        """多行成功后才在最后一行报错：临时库被删，目标库不存在。"""
        capture = self.path("late.jsonl")
        rows = [{
            "exporter_id": "e", "transport_id": "t",
            "message_hex": message(0, 7, [st(2, tmpl(256, [(8, 4), (2, 4)]))]).hex(),
        }]
        for seq in range(0, 20):
            rows.append({
                "exporter_id": "e", "transport_id": "t",
                "message_hex": message(seq, 7, [st(256, H("0a000001 00000001"))]).hex(),
            })
        rows.append({"exporter_id": "e", "transport_id": "t",
                     "message_hex": "000a"})
        write_jsonl(capture, rows)
        db = self.path("late.db")
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(capture, db, batch_size=2)
        self.assertEqual(ctx.exception.failure.capture_line, len(rows))
        self.assertFalse(os.path.exists(db))
        staging = [p for p in os.listdir(self.dir)
                   if p.startswith(".ipfix-staging-")]
        self.assertEqual(staging, [])
        self.assertEqual(os.listdir(self.dir), ["late.jsonl"])

    def test_no_open_file_descriptor_leak_on_failure(self) -> None:
        capture = self.path("fds.jsonl")
        write_jsonl(capture, [{"exporter_id": "e", "transport_id": "t",
                               "message_hex": "000a"}])
        fd_dir = "/proc/self/fd"
        before = len(os.listdir(fd_dir))
        with self.assertRaises(IpfixInputError):
            import_capture(capture, self.path("fds.db"))
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after, before + 1)

    def test_import_does_not_hold_records_in_memory(self) -> None:
        """用弱引用确认历史 DecodedMessage 在导入过程中被释放。

        monkeypatch MessageDecoder.decode 只记录 weakref；若导入器把消息/记录
        累积在集合里，导入结束后仍会有大量对象存活。
        """
        import gc
        import weakref

        from ipfix_offline import protocol

        capture = self.path("stream.jsonl")
        rows = [{"exporter_id": "e", "transport_id": "t",
                 "message_hex": message(0, 7, [st(2, tmpl(256, [(8, 4), (2, 4)]))]).hex()}]
        for seq in range(0, 40):
            rows.append({"exporter_id": "e", "transport_id": "t",
                         "message_hex": message(seq, 7, [st(256, H("0a000001 00000001"))]).hex()})
        write_jsonl(capture, rows)

        refs: list[weakref.ref] = []
        original = protocol.MessageDecoder.decode

        def wrapped(self, *args, **kwargs):
            msg = original(self, *args, **kwargs)
            refs.append(weakref.ref(msg))
            return msg

        with mock.patch.object(protocol.MessageDecoder, "decode", wrapped):
            report = import_capture(capture, self.path("stream.db"))
        gc.collect()
        self.assertEqual(report.records, 40)
        self.assertEqual(len(refs), len(rows))
        alive = sum(1 for ref in refs if ref() is not None)
        self.assertLessEqual(alive, 1,
                             f"导入结束后仍有 {alive} 条消息被持有")


class ReadOnlyInputTestCase(TempDirTestCase):
    def test_input_file_mtime_and_bytes_unchanged(self) -> None:
        capture = self.path("ro.jsonl")
        write_jsonl(capture, [{"exporter_id": "e", "transport_id": "t",
                               "message_hex": message(0, 7, []).hex()}])
        before_stat = os.stat(capture)
        before = open(capture, "rb").read()
        import_capture(capture, self.path("ro.db"))
        after_stat = os.stat(capture)
        self.assertEqual(open(capture, "rb").read(), before)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_stat.st_size, after_stat.st_size)


if __name__ == "__main__":
    unittest.main()
