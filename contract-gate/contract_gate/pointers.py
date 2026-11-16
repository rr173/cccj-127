"""JSON Pointer（RFC 6901）与 URI 片段处理。

- 波浪号转义顺序固定：先 ~1 → /，再 ~0 → ~（"~01" 解码为 "~1"）；
- URI 片段先严格百分号解码（必须落成合法 UTF-8），再按 JSON Pointer 解析；
- 只支持空片段与 JSON Pointer 片段，其余片段形态一律拒绝。
"""
from __future__ import annotations

__all__ = ["PointerError", "escape_token", "unescape_token", "parse_pointer",
           "build_pointer", "percent_decode", "fragment_tokens"]


class PointerError(ValueError):
    pass


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    out = []
    i = 0
    n = len(token)
    while i < n:
        c = token[i]
        if c == "~":
            if i + 1 >= n or token[i + 1] not in "01":
                raise PointerError(f"非法波浪号转义: {token!r}")
            out.append("~" if token[i + 1] == "0" else "/")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_pointer(text: str):
    if text == "":
        return []
    if not text.startswith("/"):
        raise PointerError(f"JSON Pointer 必须为空或以 '/' 开头: {text!r}")
    return [unescape_token(t) for t in text.split("/")[1:]]


def build_pointer(tokens) -> str:
    return "".join("/" + escape_token(t) for t in tokens)


_HEXDIG = frozenset("0123456789abcdefABCDEF")


def percent_decode(text: str) -> str:
    raw = bytearray()
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "%":
            if i + 2 >= n or text[i + 1] not in _HEXDIG or text[i + 2] not in _HEXDIG:
                raise PointerError(f"非法百分号编码: {text!r}")
            raw.append(int(text[i + 1:i + 3], 16))
            i += 3
        else:
            raw.extend(c.encode("utf-8"))
            i += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PointerError(f"片段百分号解码后不是合法 UTF-8: {exc.reason}") from exc


def fragment_tokens(fragment: str):
    """URI 片段 → JSON Pointer 记号列表。空片段返回 []。"""
    return parse_pointer(percent_decode(fragment))
