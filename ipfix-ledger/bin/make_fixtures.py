#!/usr/bin/env python3
"""生成演练夹具（结构仅做二进制拼接，不调用正式解码器推导答案）。

* generation.jsonl：模板 256 两次换代（1 条 + 2 条记录，下一消息序列号 +3）、
  跨 transport/domain 同号不同布局、纯模板消息、撤销后重定义、同布局再定义、
  接近 2^32 的序列号绕回。
* variable.jsonl：零长 / 254 / 255 扩展长度 / 非零企业号变长字段，前后夹已知字段；
  重复元素（element 8 出现两次）、IPv6、1 字节精简计数、0..3 字节填充。

生成后 tests 里另有逐字段期望值与手算偏移（不读取本脚本的输出当答案）。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def H(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", "").replace("\n", ""))


def msg(seq: int, domain: int, sets: list[bytes], export_time: int = 1700000000) -> bytes:
    body = b"".join(sets)
    length = 16 + len(body)
    return ((10).to_bytes(2, "big") + length.to_bytes(2, "big")
            + export_time.to_bytes(4, "big") + seq.to_bytes(4, "big")
            + domain.to_bytes(4, "big") + body)


def st(set_id: int, payload: bytes) -> bytes:
    return set_id.to_bytes(2, "big") + (4 + len(payload)).to_bytes(2, "big") + payload


def tmpl(template_id: int, fields: list[bytes]) -> bytes:
    return (template_id.to_bytes(2, "big")
            + len(fields).to_bytes(2, "big") + b"".join(fields))


def f(element_id: int, length: int) -> bytes:
    return element_id.to_bytes(2, "big") + length.to_bytes(2, "big")


def ent(element_id: int, enterprise: int, length: int) -> bytes:
    return ((element_id | 0x8000).to_bytes(2, "big")
            + length.to_bytes(2, "big") + enterprise.to_bytes(4, "big"))


def windows(*names: str) -> str:
    return "\n".join(json.dumps({"name": n}, ensure_ascii=False) for n in names)


def generation_rows() -> list[tuple[str, str, bytes]]:
    tset1 = tmpl(256, [f(8, 4), f(1, 8)])
    ds1 = H("c0000201 ffffffffffffffff")
    tset2 = tmpl(256, [f(8, 4), f(12, 4), f(2, 4)])
    ds2 = H("c0000202 c6336401 0000000a  c0000203 c6336402 0000000b")
    rows = [
        ("exporter-1", "transport-A",
         msg(1000, 7, [st(2, tset1), st(256, ds1),
                       st(2, tset2), st(256, ds2)])),
        ("exporter-1", "transport-A",
         msg(1003, 7, [st(256, H("cb007109 cb00710a 00000063"))])),
        ("exporter-1", "transport-B",
         msg(500, 7, [st(2, tmpl(256, [f(8, 4), f(2, 4)]))])),
        ("exporter-1", "transport-B",
         msg(500, 7, [st(256, H("0a000001 00000001  0a000002 00000002"))])),
        ("exporter-1", "transport-A",
         msg(0, 9, [st(2, tmpl(256, [f(8, 4), f(4, 1), f(11, 2)]))])),
        ("exporter-1", "transport-A",
         msg(0, 9, [st(256, H("0a000003 06 1f90"))])),
        ("exporter-1", "transport-A",
         msg(1004, 7, [st(2, tmpl(256, []))])),
        ("exporter-1", "transport-A",
         msg(1004, 7, [st(2, tmpl(256, [f(8, 4), f(152, 8), f(153, 8)]))])),
        ("exporter-1", "transport-A",
         msg(1004, 7, [st(2, tmpl(256, [f(8, 4), f(152, 8), f(153, 8)]))])),
        ("exporter-1", "transport-A",
         msg(1004, 7, [st(256, H(
             "0a000001 0000018bcfe5687b 0000018bcfe56a48"))])),
        ("exporter-2", "transport-A",
         msg(4294967290, 7, [st(2, tmpl(256, [f(12, 4), f(2, 4)]))])),
        ("exporter-2", "transport-A",
         msg(4294967290, 7, [st(256, H("0a00000a 00000001  0a00000b 00000002"))])),
        ("exporter-2", "transport-A",
         msg(4294967292, 7, [st(256, H("0a00000c 00000003  0a00000d 00000004"))])),
        ("exporter-2", "transport-A",
         msg(4294967294, 7, [st(256, H("0a00000e 00000005  0a00000f 00000006"))])),
        ("exporter-2", "transport-A",
         msg(0, 7, [st(256, H("0a000010 00000007"))])),
    ]
    return rows


VARIABLE_FIELDS = [
    f(8, 4),                 # 0 sourceIPv4Address
    f(999, 65535),           # 1 unknown, variable
    f(500, 2),               # 2 unknown fixed, right after variable
    ent(4000, 9, 65535),     # 3 enterprise variable
    f(2, 4),                 # 4 packetDeltaCount
    f(27, 16),               # 5 sourceIPv6Address
    f(8, 4),                 # 6 repeated sourceIPv4Address
    f(4, 1),                 # 7 protocolIdentifier
    f(777, 65535),           # 8 unknown variable, carries 8-byte max integer
    ent(8, 4242, 4),         # 9 enterprise 8/4242 must stay raw bytes
]


def variable_rows() -> list[tuple[str, str, bytes]]:
    def rec_v1(src: str, var1: bytes, mid: bytes, evar: bytes, packet: int,
               v6: str, src2: str, proto: int, tail: bytes, ent8: bytes) -> bytes:
        return (H(src) + var1 + H(mid) + evar + packet.to_bytes(4, "big")
                + H(v6) + H(src2) + bytes([proto]) + tail + H(ent8))

    # 记录 0：零长未知字段、255 字节企业字段、8 字节无符号最大值（尾字段）
    r0 = rec_v1("c0000201", b"\x00", "0007",
                b"\xff\x00\xff" + b"\x41" * 255, 10,
                "20010db8000000000000000000000001", "c0000202", 6,
                b"\x08" + H("ffffffffffffffff"), "0000000a")
    # 记录 1：254 字节未知字段、零长企业字段、1 字节变长
    r1 = rec_v1("c0000203", b"\xfe" + b"\x42" * 254, "0008",
                b"\x00", 11,
                "00000000000000000000000000000001", "c0000204", 17,
                b"\x01\xff", "0000000b")
    # 记录 2：255 字节扩展长度未知字段、3 字节企业字段
    r2 = rec_v1("c0000205", b"\xff\x00\xff" + b"\x43" * 255, "0009",
                b"\x03\xaa\xbb\xcc", 12,
                "20010db8000000000000000000000002", "c0000206", 1,
                b"\x08" + H("0000000000000001"), "0000000c")
    v1 = msg(0, 1, [st(2, tmpl(300, VARIABLE_FIELDS) + b"\x00\x00\x00"),
                    st(300, r0 + r1 + r2 + b"\x00\x00\x00")])
    v2 = msg(3, 1, [])
    r3 = rec_v1("de000001", b"\x00", "000a", b"\x00", 13,
                "20010db8000000000000000000000003", "de000002", 58,
                b"\x08" + H("0000000000000002"), "0000000d")
    v3 = msg(3, 1, [st(300, b""), st(300, r3)])

    # 一个 Template Set 内：定义 -> 新布局 -> 撤销 -> 再定义（代次 1,2,3）
    multi = (tmpl(302, [f(8, 4), f(2, 4)])
             + tmpl(302, [f(8, 4), f(4, 1), f(11, 2)])
             + tmpl(302, [])
             + tmpl(302, [f(8, 4), f(152, 8), f(153, 8)]))
    v4 = msg(4, 1, [st(2, multi + b"\x00")])
    v5 = msg(4, 1, [st(302, H("c0000210 0000018bcfe5687c 0000018bcfe56a49"))])
    v6 = msg(5, 1, [])

    # 1 字节精简计数 + 3 字节计数；同一模板下 0/1/2/3 字节填充
    compact = tmpl(301, [f(8, 4), f(1, 1), f(2, 3), f(4, 1)])
    c = lambda src, octets, packets, proto: (H(src) + bytes([octets])
                                             + packets.to_bytes(3, "big")
                                             + bytes([proto]))
    v7 = msg(5, 1, [st(2, compact)])
    v8 = msg(5, 1, [st(301, c("c0000211", 0, 1, 1)
                          + c("c0000212", 255, 0xFFFFFF, 6) + b"\x00\x00\x00")])
    v9 = msg(7, 1, [st(301, c("c0000213", 7, 300, 17))])
    v10 = msg(8, 1, [st(301, c("c0000214", 8, 301, 6) + b"\x00")])
    v11 = msg(9, 1, [st(301, c("c0000215", 9, 302, 6) + b"\x00\x00")])

    rows = [("exporter-3", "transport-V", m)
            for m in (v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11)]
    return rows


def write(path: str, rows: list[tuple[str, str, bytes]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for exporter, transport, message in rows:
            fh.write(json.dumps({
                "exporter_id": exporter,
                "transport_id": transport,
                "message_hex": message.hex(),
            }, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    write(os.path.join(FIXTURES, "generation.jsonl"), generation_rows())
    write(os.path.join(FIXTURES, "variable.jsonl"), variable_rows())
    print("fixtures written to", FIXTURES)


if __name__ == "__main__":
    main()
