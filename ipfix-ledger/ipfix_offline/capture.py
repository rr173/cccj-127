"""捕获 JSONL 的严格逐行读取。

每行必须恰含 exporter_id、transport_id、message_hex 三个键：
  * UTF-8 解码失败、空行、重复 JSON 键、多余/缺少字段、类型错误、
    空字符串标识、坏十六进制都拒绝；
  * exporter_id / transport_id 原样保留（不做 strip/大小写/编码转换）；
  * message_hex 为一个完整 IPFIX Message 的十六进制字节，偶数长度且只含
    0-9a-fA-F；
  * 行号从 1 开始，行序即接收顺序。

读取器一次只保留一行（外加本轮消息），捕获文件可以远大于内存。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterator, Optional

from .errors import Failure, IpfixInputError

REQUIRED_KEYS = frozenset({"exporter_id", "transport_id", "message_hex"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class CaptureLine:
    line_no: int
    exporter_id: str
    transport_id: str
    message_hex: str
    message: bytes


def _fail(line_no: int, reason: str, detail: Optional[str] = None) -> IpfixInputError:
    return IpfixInputError(Failure(phase="capture", reason=reason,
                                   capture_line=line_no, detail=detail))


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


def _constant(value):
    raise ValueError(f"不允许非标准 JSON 常量 {value}")


def decode_hex(text: str, line_no: int) -> bytes:
    if not isinstance(text, str):
        raise _fail(line_no, "message_hex 不是字符串",
                    detail=type(text).__name__)
    if len(text) % 2 != 0:
        raise _fail(line_no, "message_hex 长度为奇数，不是完整字节",
                    detail=f"{len(text)} 个十六进制字符")
    bad = [ch for ch in text if ch not in _HEX_DIGITS]
    if bad:
        sample = "".join(sorted(set(bad)))[:20]
        raise _fail(line_no, "message_hex 含非十六进制字符", detail=sample)
    return bytes.fromhex(text)


def parse_line(raw: bytes, line_no: int) -> CaptureLine:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(line_no, "行不是合法 UTF-8", detail=str(exc)) from None
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if text == "":
        raise _fail(line_no, "空行")
    try:
        obj = json.loads(text, object_pairs_hook=_strict_object,
                         parse_constant=_constant)
    except _DuplicateKey as exc:
        raise _fail(line_no, "重复 JSON 键", detail=str(exc)) from None
    except json.JSONDecodeError as exc:
        raise _fail(line_no, "不是合法 JSON",
                    detail=f"第 {exc.colno} 列：{exc.msg}") from None
    except ValueError as exc:
        raise _fail(line_no, "不是合法 JSON", detail=str(exc)) from None
    if not isinstance(obj, dict):
        raise _fail(line_no, "行顶层不是 JSON 对象",
                    detail=type(obj).__name__)
    keys = set(obj)
    missing = REQUIRED_KEYS - keys
    extra = keys - REQUIRED_KEYS
    if missing or extra:
        raise _fail(line_no, "字段集合不符",
                    detail=f"缺 {sorted(missing)}，多 {sorted(extra)}")
    exporter_id = obj["exporter_id"]
    transport_id = obj["transport_id"]
    for name, value in (("exporter_id", exporter_id),
                        ("transport_id", transport_id)):
        if not isinstance(value, str):
            raise _fail(line_no, f"{name} 不是字符串",
                        detail=type(value).__name__)
        if value == "":
            raise _fail(line_no, f"{name} 为空字符串")
    message_hex = obj["message_hex"]
    if not isinstance(message_hex, str):
        raise _fail(line_no, "message_hex 不是字符串",
                    detail=type(message_hex).__name__)
    message = decode_hex(message_hex, line_no)
    return CaptureLine(line_no=line_no, exporter_id=exporter_id,
                       transport_id=transport_id, message_hex=message_hex,
                       message=message)


def iter_capture(path: str | os.PathLike) -> Iterator[CaptureLine]:
    """逐行读取捕获；遇到第一处问题即抛出 IpfixInputError（带行号）。"""
    with open(path, "rb") as fh:
        for line_no, raw in enumerate(fh, start=1):
            yield parse_line(raw, line_no)


def count_lines(path: str | os.PathLike) -> int:
    with open(path, "rb") as fh:
        return sum(1 for _ in fh)
