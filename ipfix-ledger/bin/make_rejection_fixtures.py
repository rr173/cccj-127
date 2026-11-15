#!/usr/bin/env python3
"""生成拒绝演练夹具及其“改正版”捕获。

每个错误都只改一处，旁边给出同一捕获修正后的版本；测试断言两件事：
  1. 错误捕获返回非零并定位到捕获行/消息偏移/Set/字段；
  2. 目标结果库不可打开（不存在）；
  3. 修正后的捕获（同一场景只改坏点）能成功接入且记录数符合手算。

十六进制常量全部手写后拼接，不经正式编码器。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "rejections")


def H(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


def message(seq: int, domain: int, sets: list[bytes], *,
            length: int | None = None, version: int = 10) -> bytes:
    body = b"".join(sets)
    total = 16 + len(body) if length is None else length
    return (version.to_bytes(2, "big") + total.to_bytes(2, "big")
            + (1700000000).to_bytes(4, "big") + seq.to_bytes(4, "big")
            + domain.to_bytes(4, "big") + body)


def st(set_id: int, payload: bytes, *, length: int | None = None) -> bytes:
    total = 4 + len(payload) if length is None else length
    return set_id.to_bytes(2, "big") + total.to_bytes(2, "big") + payload


def tmpl(template_id: int, fields: list[bytes]) -> bytes:
    return (template_id.to_bytes(2, "big")
            + len(fields).to_bytes(2, "big") + b"".join(fields))


def f(element_id: int, length: int) -> bytes:
    return element_id.to_bytes(2, "big") + length.to_bytes(2, "big")


def ent(element_id: int, enterprise: int, length: int) -> bytes:
    return ((element_id | 0x8000).to_bytes(2, "big")
            + length.to_bytes(2, "big") + enterprise.to_bytes(4, "big"))


T256 = tmpl(256, [f(8, 4), f(2, 4)])
DS3 = st(256, H("c0000201 00000001  c0000202 00000002  c0000203 00000003"))


def rows(*messages: bytes, exporter: str = "exporter-r",
         transport: str = "transport-r") -> list[dict]:
    return [{"exporter_id": exporter, "transport_id": transport,
             "message_hex": m.hex()} for m in messages]


def cases() -> dict[str, tuple[list[dict], list[dict], dict]]:
    """name -> (坏捕获行, 改正捕获行, 期望错误定位片段)。"""
    out: dict[str, tuple[list[dict], list[dict], dict]] = {}

    out["data_before_template"] = (
        rows(message(0, 7, [DS3])),
        rows(message(0, 7, [st(2, T256), DS3])),
        {"phase": "data", "capture_line": 1})

    out["send_after_revoke"] = (
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(2, tmpl(256, []))]),
             message(0, 7, [DS3])),
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(2, tmpl(256, []))]),
             message(0, 7, [st(2, T256)]),
             message(0, 7, [DS3])),
        {"phase": "data", "capture_line": 3})

    out["revoke_unregistered"] = (
        rows(message(0, 7, [st(2, tmpl(256, []))])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "template", "capture_line": 1})

    out["illegal_known_length"] = (
        rows(message(0, 7, [st(2, tmpl(256, [f(8, 5), f(2, 4)]))])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "template", "capture_line": 1, "field_ordinal": 0})

    out["illegal_ipv4_length"] = (
        rows(message(0, 7, [st(2, tmpl(256, [f(12, 8)]))])),
        rows(message(0, 7, [st(2, tmpl(256, [f(12, 4)]))])),
        {"phase": "template", "capture_line": 1})

    out["field_length_zero"] = (
        rows(message(0, 7, [st(2, tmpl(256, [f(8, 4), f(2, 0)]))])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "template", "capture_line": 1})

    out["set_beyond_message"] = (
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001"), length=200)])),
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001"))])),
        {"phase": "set", "capture_line": 2})

    # 消息头 length 比真实字节数少 4：Set 头部 setLength 因此越界
    out["message_length_lies"] = (
        rows(message(0, 7, [st(2, T256), st(256, H("c0000201 00000001"))],
                     length=16 + 32 + 12 - 4)),
        rows(message(0, 7, [st(2, T256), st(256, H("c0000201 00000001"))])),
        {"phase": "header", "capture_line": 1})

    var_t = tmpl(300, [f(8, 4), f(999, 65535), f(2, 4)])
    # 变长前缀在 0xff 之后只剩 0 字节：模板最少记录 9 字节，记录体必须 >=9
    var_short = tmpl(304, [f(8, 4), f(999, 65535)])
    out["extended_length_truncated"] = (
        rows(message(0, 7, [st(2, var_short)]),
             message(0, 7, [st(304, H("c0000201") + b"\xff")])),
        rows(message(0, 7, [st(2, var_short)]),
             message(0, 7, [st(304, H("c0000201") + b"\xff\x01\x00"
                              + b"x" * 256)])),
        {"phase": "data", "capture_line": 2, "field_ordinal": 1})

    out["variable_payload_truncated"] = (
        rows(message(0, 7, [st(2, var_t)]),
             message(0, 7, [st(300, H("c0000201") + b"\x0aabcd")])),
        rows(message(0, 7, [st(2, var_t)]),
             message(0, 7, [st(300, H("c0000201") + b"\x03abc" + H("00000007"))])),
        {"phase": "data", "capture_line": 2, "field_ordinal": 1})

    out["extended_length_below_255"] = (
        rows(message(0, 7, [st(2, var_t)]),
             message(0, 7, [st(300, H("c0000201") + b"\xff\x00\x05hello"
                              + H("00000007"))])),
        rows(message(0, 7, [st(2, var_t)]),
             message(0, 7, [st(300, H("c0000201") + b"\xff\x01\x00"
                              + b"x" * 256 + H("00000007"))])),
        {"phase": "data", "capture_line": 2, "field_ordinal": 1})

    out["nonzero_padding"] = (
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001") + b"\x00\x01")])),
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001") + b"\x00\x00")])),
        {"phase": "data", "capture_line": 2})

    out["four_byte_nonzero_tail"] = (
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001") + b"\x00\x00\x00\x01")])),
        rows(message(0, 7, [st(2, T256)]),
             message(0, 7, [st(256, H("c0000201 00000001"))])),
        {"phase": "data", "capture_line": 2})

    out["sequence_gap"] = (
        rows(message(10, 7, [st(2, T256)]), message(10, 7, [DS3]),
             message(14, 7, [DS3])),
        rows(message(10, 7, [st(2, T256)]), message(10, 7, [DS3]),
             message(13, 7, [DS3])),
        {"phase": "sequence", "capture_line": 3})

    out["sequence_template_only_counts_zero"] = (
        rows(message(10, 7, [st(2, T256)]), message(11, 7, [DS3])),
        rows(message(10, 7, [st(2, T256)]), message(10, 7, [DS3])),
        {"phase": "sequence", "capture_line": 2})

    bad = bytearray(message(10, 7, [DS3]))
    bad[1] = 11  # version 11
    out["last_line_bad"] = (
        rows(message(10, 7, [st(2, T256)]), message(10, 7, [DS3]), bytes(bad)),
        rows(message(10, 7, [st(2, T256)]), message(10, 7, [DS3]),
             message(13, 7, [DS3])),
        {"phase": "header", "capture_line": 3})

    out["options_template_set"] = (
        rows(message(0, 7, [st(3, b"\x00\x01\x00\x01\x00\x04\x00\x04")])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "set", "capture_line": 1})

    out["reserved_set_id"] = (
        rows(message(0, 7, [st(4, b"\x00\x00\x00\x00")])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "set", "capture_line": 1})

    out["template_id_below_256"] = (
        rows(message(0, 7, [st(2, tmpl(255, [f(8, 4), f(2, 4)]))])),
        rows(message(0, 7, [st(2, T256)])),
        {"phase": "template", "capture_line": 1})

    out["template_before_domain_definition"] = (
        rows(message(0, 7, [st(2, T256)]),
             message(0, 8, [DS3])),
        rows(message(0, 7, [st(2, T256)]),
             message(0, 8, [st(2, T256)]),
             message(0, 8, [DS3])),
        {"phase": "data", "capture_line": 2})

    out["short_record_tail"] = (
        rows(message(0, 7, [st(2, tmpl(256, [f(8, 4), f(2, 4)]))]),
             message(0, 7, [st(256, H("c0000201 00000001 c0000202"))])),
        rows(message(0, 7, [st(2, tmpl(256, [f(8, 4), f(2, 4)]))]),
             message(0, 7, [st(256, H("c0000201 00000001 c0000202 00000002"))])),
        {"phase": "data", "capture_line": 2})

    return out


def write(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False,
                                separators=(",", ":")) + "\n")


def main() -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    manifest = {}
    for name, (bad, good, expect) in cases().items():
        write(os.path.join(FIXTURES, f"{name}.bad.jsonl"), bad)
        write(os.path.join(FIXTURES, f"{name}.good.jsonl"), good)
        manifest[name] = expect
    with open(os.path.join(FIXTURES, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"wrote {len(manifest)} rejection pairs to {FIXTURES}")


if __name__ == "__main__":
    main()
