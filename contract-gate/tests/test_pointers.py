"""JSON Pointer 与 URI 片段测试。"""
from __future__ import annotations

import unittest

from contract_gate.pointers import (PointerError, escape_token,
                                    fragment_tokens, parse_pointer,
                                    percent_decode, unescape_token)


class TestPointer(unittest.TestCase):
    def test_tilde_order(self):
        # RFC 6901：~01 解码为 ~1（先 ~1→/，再 ~0→~）
        self.assertEqual(unescape_token("~01"), "~1")
        self.assertEqual(unescape_token("~00"), "~0")
        self.assertEqual(unescape_token("~1"), "/")
        self.assertEqual(unescape_token("~0"), "~")
        self.assertEqual(unescape_token("a~1b~0c"), "a/b~c")

    def test_escape_roundtrip(self):
        for token in ("a/b", "m~n", "~/", "plain", ""):
            self.assertEqual(unescape_token(escape_token(token)), token)

    def test_parse(self):
        self.assertEqual(parse_pointer(""), [])
        self.assertEqual(parse_pointer("/a/b"), ["a", "b"])
        self.assertEqual(parse_pointer("/a~1b/0"), ["a/b", "0"])

    def test_bad_tilde(self):
        for bad in ("~", "~2", "a~", "~x"):
            with self.assertRaises(PointerError, msg=bad):
                unescape_token(bad)

    def test_pointer_must_start_with_slash(self):
        with self.assertRaises(PointerError):
            parse_pointer("a/b")


class TestFragment(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(fragment_tokens(""), [])

    def test_pointer_fragment(self):
        self.assertEqual(fragment_tokens("/$defs/a~1b"), ["$defs", "a/b"])
        self.assertEqual(fragment_tokens("/$defs/m~0n"), ["$defs", "m~n"])

    def test_percent_decoding(self):
        self.assertEqual(percent_decode("sp%20ace"), "sp ace")
        self.assertEqual(fragment_tokens("/$defs/sp%20ace"), ["$defs", "sp ace"])
        # %7E 先百分号解码为 ~，再按波浪号转义处理
        self.assertEqual(fragment_tokens("/a%7E0b"), ["a~b"])
        self.assertEqual(fragment_tokens("/a%7E1b"), ["a/b"])
        # 多字节 UTF-8 百分号编码
        self.assertEqual(fragment_tokens("/%E5%90%8D%E7%A7%B0"), ["名称"])

    def test_bad_percent(self):
        for bad in ("%", "%2", "%zz", "a%"):
            with self.assertRaises(PointerError, msg=bad):
                percent_decode(bad)

    def test_bad_utf8_in_fragment(self):
        with self.assertRaises(PointerError):
            percent_decode("%ff%fe")

    def test_plain_name_fragment_unsupported(self):
        with self.assertRaises(PointerError):
            fragment_tokens("$defs")


if __name__ == "__main__":
    unittest.main()
