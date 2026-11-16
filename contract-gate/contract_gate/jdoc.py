"""逐字节 JSON 解析。

只处理单行 UTF-8 字节（调用方已去掉行尾 CRLF/LF）。特点：

- 每个值记录原行字节范围 [start, end)，对象成员另记键名范围；
- 重复对象成员在解析期发现，诊断同时给出两处键名字节范围；
- 数字一律解析为 decimal.Decimal，不经过浮点，整数性按数学值判断；
- 解析器迭代实现，深嵌套不依赖 Python 递归深度；
- 解析错误带类别（empty / utf8 / json / duplicate）与可确定的字节位置。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "JNode", "Member", "LineError", "EmptyLine", "Utf8LineError",
    "DuplicateMember", "parse_line", "node_type", "type_matches", "value_equal",
]


@dataclass
class Member:
    key: str
    key_start: int
    key_end: int
    value: "JNode"


@dataclass
class JNode:
    kind: str                # null | bool | num | str | arr | obj
    start: int               # 行内字节偏移，左闭
    end: int                 # 右开
    value: object = None     # bool | Decimal | str | list[JNode] | list[Member]
    index: dict = field(default=None, repr=False)  # obj: key -> Member


class LineError(Exception):
    """单行解析失败。span 为原行字节左闭右开范围。"""

    category = "json"

    def __init__(self, message, start, end=None, extra=None):
        super().__init__(message)
        self.message = message
        self.start = start
        self.end = start if end is None else end
        self.extra = dict(extra or {})

    def to_dict(self):
        d = {"message": self.message, "span": [self.start, self.end]}
        d.update(self.extra)
        return d


class EmptyLine(LineError):
    category = "empty"


class Utf8LineError(LineError):
    category = "utf8"


class DuplicateMember(LineError):
    category = "duplicate"


_WS = (0x20, 0x09, 0x0D, 0x0A)  # space tab cr lf
_HEXDIG = frozenset(b"0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {
    0x22: '"', 0x5C: "\\", 0x2F: "/", 0x62: "\b",
    0x66: "\f", 0x6E: "\n", 0x72: "\r", 0x74: "\t",
}


def _ws(body: bytes, i: int, n: int) -> int:
    while i < n and body[i] in _WS:
        i += 1
    return i


def parse_line(body: bytes) -> JNode:
    """解析单行字节为一棵 JNode 树；失败抛 LineError 子类。"""
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Utf8LineError(f"非法 UTF-8 字节序列（{exc.reason}）", exc.start, exc.end) from exc
    n = len(body)
    if n == 0:
        raise EmptyLine("空物理行，没有 JSON 值", 0, 0)
    i = _ws(body, 0, n)
    if i >= n:
        raise LineError("整行只有空白字符，没有 JSON 值", n, n)

    # 栈帧：["arr", start, items] 或 ["obj", start, members, seen, pending]
    stack = []
    while True:
        b = body[i]
        if b == 0x7B:  # {
            stack.append(["obj", i, [], {}, None])
            i += 1
            i = _ws(body, i, n)
            if i >= n:
                raise LineError("对象未闭合", i, i)
            if body[i] == 0x7D:  # }
                frame = stack.pop()
                node = JNode("obj", frame[1], i + 1, frame[2], frame[3])
                i += 1
            else:
                key, ks, ke, i = _parse_key(body, i, n)
                i = _ws(body, i, n)
                if i >= n or body[i] != 0x3A:  # :
                    raise LineError("对象键名后缺少 ':'", i, i + 1 if i < n else i)
                i += 1
                i = _ws(body, i, n)
                if i >= n:
                    raise LineError("':' 后缺少值", i, i)
                stack[-1][4] = (key, ks, ke)
                continue
        elif b == 0x5B:  # [
            stack.append(["arr", i, []])
            i += 1
            i = _ws(body, i, n)
            if i >= n:
                raise LineError("数组未闭合", i, i)
            if body[i] == 0x5D:  # ]
                frame = stack.pop()
                node = JNode("arr", frame[1], i + 1, frame[2])
                i += 1
            else:
                continue
        else:
            node, i = _parse_scalar(body, i, n)

        # 把完成的节点向上挂；栈空则整行解析完成
        while True:
            if not stack:
                i = _ws(body, i, n)
                if i != n:
                    raise LineError("JSON 值之后存在多余字节", i, i + 1)
                return node
            frame = stack[-1]
            if frame[0] == "arr":
                frame[2].append(node)
                i = _ws(body, i, n)
                if i >= n:
                    raise LineError("数组未闭合", i, i)
                c = body[i]
                if c == 0x2C:  # ,
                    i += 1
                    i = _ws(body, i, n)
                    if i >= n:
                        raise LineError("',' 后缺少值", i, i)
                    break
                if c == 0x5D:  # ]
                    stack.pop()
                    node = JNode("arr", frame[1], i + 1, frame[2])
                    i += 1
                    continue
                raise LineError("数组元素后应为 ',' 或 ']'", i, i + 1)
            key, ks, ke = frame[4]
            member = Member(key, ks, ke, node)
            seen = frame[3]
            first = seen.get(key)
            if first is not None:
                raise DuplicateMember(
                    f"对象成员 {key!r} 重复出现",
                    ks, ke,
                    {"key": key,
                     "spans": [[first.key_start, first.key_end], [ks, ke]]})
            seen[key] = member
            frame[2].append(member)
            frame[4] = None
            i = _ws(body, i, n)
            if i >= n:
                raise LineError("对象未闭合", i, i)
            c = body[i]
            if c == 0x2C:  # ,
                i += 1
                i = _ws(body, i, n)
                key, ks, ke, i = _parse_key(body, i, n)
                i = _ws(body, i, n)
                if i >= n or body[i] != 0x3A:
                    raise LineError("对象键名后缺少 ':'", i, i + 1 if i < n else i)
                i += 1
                i = _ws(body, i, n)
                if i >= n:
                    raise LineError("':' 后缺少值", i, i)
                frame[4] = (key, ks, ke)
                break
            if c == 0x7D:  # }
                stack.pop()
                node = JNode("obj", frame[1], i + 1, frame[2], frame[3])
                i += 1
                continue
            raise LineError("对象成员后应为 ',' 或 '}'", i, i + 1)


def _parse_scalar(body: bytes, i: int, n: int):
    b = body[i]
    if b == 0x22:  # "
        s, j = _parse_string(body, i, n)
        return JNode("str", i, j, s), j
    if b == 0x74:  # t
        if body[i:i + 4] == b"true":
            return JNode("bool", i, i + 4, True), i + 4
        raise LineError("无法识别的字面量", i, i + 1)
    if b == 0x66:  # f
        if body[i:i + 5] == b"false":
            return JNode("bool", i, i + 5, False), i + 5
        raise LineError("无法识别的字面量", i, i + 1)
    if b == 0x6E:  # n
        if body[i:i + 4] == b"null":
            return JNode("null", i, i + 4, None), i + 4
        raise LineError("无法识别的字面量", i, i + 1)
    if b == 0x2D or 0x30 <= b <= 0x39:  # - 或数字
        return _parse_number(body, i, n)
    raise LineError(f"此处不应出现字节 0x{b:02x}", i, i + 1)


def _parse_number(body: bytes, i: int, n: int):
    start = i
    if body[i] == 0x2D:  # -
        i += 1
        if i >= n:
            raise LineError("非法数字：负号后缺少数字", start, i)
    if body[i] == 0x30:  # 0
        i += 1
        if i < n and 0x30 <= body[i] <= 0x39:
            raise LineError("非法数字：整数部分前导零", i, i + 1)
    elif 0x31 <= body[i] <= 0x39:
        while i < n and 0x30 <= body[i] <= 0x39:
            i += 1
    else:
        raise LineError("非法数字：缺少整数部分", start, i + 1 if i < n else i)
    if i < n and body[i] == 0x2E:  # .
        i += 1
        if i >= n or not 0x30 <= body[i] <= 0x39:
            raise LineError("非法数字：小数点后缺少数字", i, i)
        while i < n and 0x30 <= body[i] <= 0x39:
            i += 1
    if i < n and body[i] in (0x65, 0x45):  # e E
        i += 1
        if i < n and body[i] in (0x2B, 0x2D):  # + -
            i += 1
        if i >= n or not 0x30 <= body[i] <= 0x39:
            raise LineError("非法数字：指数缺少数字", i, i)
        while i < n and 0x30 <= body[i] <= 0x39:
            i += 1
    raw = body[start:i].decode("ascii")
    return JNode("num", start, i, Decimal(raw)), i


def _parse_string(body: bytes, i: int, n: int):
    start = i
    i += 1
    parts = []
    run = i
    while True:
        if i >= n:
            raise LineError("字符串未闭合", start, n)
        b = body[i]
        if b == 0x22:  # "
            parts.append(body[run:i].decode("utf-8"))
            return "".join(parts), i + 1
        if b == 0x5C:  # \
            parts.append(body[run:i].decode("utf-8"))
            i += 1
            if i >= n:
                raise LineError("字符串未闭合", start, n)
            esc = body[i]
            simple = _SIMPLE_ESCAPES.get(esc)
            if simple is not None:
                parts.append(simple)
                i += 1
            elif esc == 0x75:  # u
                ch, i = _parse_unicode_escape(body, i, n)
                parts.append(ch)
            else:
                raise LineError("非法转义序列", i - 1, i + 1)
            run = i
        elif b < 0x20:
            raise LineError("字符串中存在未转义的控制字符", i, i + 1)
        else:
            i += 1


def _parse_unicode_escape(body: bytes, i: int, n: int):
    # body[i] == 'u'
    def hex4(j):
        if j + 4 > n:
            raise LineError("\\u 转义被截断", i - 1, n)
        for k in range(j, j + 4):
            if body[k] not in _HEXDIG:
                raise LineError("\\u 转义含非法十六进制字符", k, k + 1)
        return int(body[j:j + 4].decode("ascii"), 16)

    hi = hex4(i + 1)
    j = i + 5
    if 0xD800 <= hi <= 0xDBFF:
        if j + 1 < n and body[j] == 0x5C and body[j + 1] == 0x75:
            lo = hex4(j + 2)
            if 0xDC00 <= lo <= 0xDFFF:
                return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)), j + 6
            raise LineError("\\u 转义低代理项非法", j, min(j + 6, n))
        raise LineError("\\u 转义高代理项缺少配对", i - 1, j)
    if 0xDC00 <= hi <= 0xDFFF:
        raise LineError("\\u 转义出现孤立低代理项", i - 1, j)
    return chr(hi), j


def _parse_key(body: bytes, i: int, n: int):
    if i >= n or body[i] != 0x22:
        raise LineError("对象键名必须是字符串", i, i + 1 if i < n else i)
    s, j = _parse_string(body, i, n)
    return s, i, j, j


# ---------------------------------------------------------------- 类型与相等

_TYPE_NAME = {
    "null": "null", "bool": "boolean", "str": "string",
    "arr": "array", "obj": "object", "num": "number",
}


def node_type(node: JNode) -> str:
    if node.kind == "num":
        v = node.value
        return "integer" if v == v.to_integral_value() else "number"
    return _TYPE_NAME[node.kind]


def type_matches(node: JNode, tname: str) -> bool:
    if tname == "number":
        return node.kind == "num"
    if tname == "integer":
        return node.kind == "num" and node.value == node.value.to_integral_value()
    return _TYPE_NAME[node.kind] == tname


def value_equal(node: JNode, val) -> bool:
    """实例节点与契约常量（const/enum）按 JSON 结构比较。

    对象不看成员顺序，数组看顺序；数值按精确十进制数学值比较；
    true 与 1 不相等（布尔与数是不同类别）。
    """
    if val is None:
        return node.kind == "null"
    if isinstance(val, bool):
        return node.kind == "bool" and node.value is val
    if isinstance(val, (int, Decimal)):
        return node.kind == "num" and node.value == Decimal(val)
    if isinstance(val, str):
        return node.kind == "str" and node.value == val
    if isinstance(val, list):
        return (node.kind == "arr" and len(node.value) == len(val)
                and all(value_equal(c, v) for c, v in zip(node.value, val)))
    if isinstance(val, dict):
        if node.kind != "obj" or len(node.index) != len(val):
            return False
        for k, v in val.items():
            m = node.index.get(k)
            if m is None or not value_equal(m.value, v):
                return False
        return True
    return False
