"""常用 IANA（enterpriseNumber=0）元素的类型解释。

只对“已约定”的元素解释；声明长度不合口径的已知元素直接报错，不降格成未知字段。
整数一律不经过浮点：定长走 int.from_bytes，变长（1..8 字节）直接迭代字节累加，
因此 2^64-1 也能无损保存（JSON 中用十进制字符串 + value_type）。
非零 enterpriseNumber 不套用 IANA 解释。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Known:
    name: str
    value_type: str      # uint8/uint16/uint64/ipv4/ipv6/utc_milliseconds
    min_length: int
    max_length: int
    decoder: str         # "uint" | "ipv4" | "ipv6" | "milliseconds"

    def length_ok(self, length: int) -> bool:
        return self.min_length <= length <= self.max_length

    @property
    def length_text(self) -> str:
        if self.min_length == self.max_length:
            return str(self.min_length)
        return f"{self.min_length}..{self.max_length}"


# 口径来自任务约定：
#   1/2 octet/packetDeltaCount     1..8 字节大端无符号
#   7/11 source/destination port   1..2 字节无符号
#   4 protocolIdentifier           恰 1
#   8/12 IPv4                      恰 4
#   27/28 IPv6                     恰 16
#   152/153 UTC milliseconds       恰 8
IANA: dict[int, Known] = {
    1: Known("octetDeltaCount", "uint64", 1, 8, "uint"),
    2: Known("packetDeltaCount", "uint64", 1, 8, "uint"),
    4: Known("protocolIdentifier", "uint8", 1, 1, "uint"),
    7: Known("sourceTransportPort", "uint16", 1, 2, "uint"),
    8: Known("sourceIPv4Address", "ipv4", 4, 4, "ipv4"),
    11: Known("destinationTransportPort", "uint16", 1, 2, "uint"),
    12: Known("destinationIPv4Address", "ipv4", 4, 4, "ipv4"),
    27: Known("sourceIPv6Address", "ipv6", 16, 16, "ipv6"),
    28: Known("destinationIPv6Address", "ipv6", 16, 16, "ipv6"),
    152: Known("flowStartMilliseconds", "utc_milliseconds", 8, 8,
               "milliseconds"),
    153: Known("flowEndMilliseconds", "utc_milliseconds", 8, 8,
               "milliseconds"),
}

VALUE_TYPES = (
    "uint8", "uint16", "uint32", "uint64",
    "ipv4", "ipv6", "utc_milliseconds", "raw_bytes",
)


def known(element_id: int, enterprise: int) -> Optional[Known]:
    if enterprise != 0:
        return None
    return IANA.get(element_id)


def name_for(element_id: int, enterprise: int) -> Optional[str]:
    spec = known(element_id, enterprise)
    return spec.name if spec else None


def value_type_for(element_id: int, enterprise: int) -> str:
    spec = known(element_id, enterprise)
    return spec.value_type if spec else "raw_bytes"


def decode(spec: Known, raw: bytes) -> object:
    """raw 长度已由调用方按 spec.length_ok 校验。"""
    if spec.decoder == "uint":
        n = 0
        for byte in raw:
            n = (n << 8) | byte
        return n
    if spec.decoder == "milliseconds":
        return int.from_bytes(raw, "big")
    if spec.decoder == "ipv4":
        return ".".join(str(b) for b in raw)
    if spec.decoder == "ipv6":
        return format_ipv6(raw)
    raise ValueError(f"未知已知类型 {spec.decoder}")


def format_ipv6(raw: bytes) -> str:
    """RFC 5952 规范文本：最长的一串 0 组（至少 2 组）压缩成 '::'，全零为 '::'。"""
    groups = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, 16, 2)]
    best_start, best_len = -1, 0
    i = 0
    while i < 8:
        if groups[i] == 0:
            j = i
            while j < 8 and groups[j] == 0:
                j += 1
            if j - i > best_len:
                best_start, best_len = i, j - i
            i = j
        else:
            i += 1
    if best_len < 2:
        return ":".join(f"{g:x}" for g in groups)
    head = ":".join(f"{g:x}" for g in groups[:best_start])
    tail = ":".join(f"{g:x}" for g in groups[best_start + best_len:])
    return f"{head}::{tail}"


def is_int_type(value_type: str) -> bool:
    return value_type in ("uint8", "uint16", "uint32", "uint64",
                          "utc_milliseconds")


def value_json(value, value_type: str):
    """公开 JSON/库内文本语义：整数用十进制字符串，地址用文本，原始字节用 hex。"""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, int):
        return str(value)
    return value
