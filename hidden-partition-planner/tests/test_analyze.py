"""原像、区间运算与三值推导的单元测试。

覆盖每种单叶条件在真实分区原像上的 drop/pass 判定，以及同列纯 and
的交集识别（如截断区间 [-10,-1] 与 x>=-3 and x<-3）。
"""
from __future__ import annotations

import unittest

from hidden_partition.analyze import (
    FALSE,
    TRUE,
    UNKNOWN,
    analyze,
    decide,
)
from hidden_partition.constraints import (
    INT64_MAX,
    INT64_MIN,
    ColumnConstraint,
    exact,
    intersect,
    null_only,
    string_prefix_upper_bound,
    universe,
)
from hidden_partition.contract import Field
from hidden_partition.intervals import intersect as iv_intersect
from hidden_partition.intervals import subtract as iv_subtract
from hidden_partition.query import And, Leaf, Not, Or
from hidden_partition.transforms import Transform, preimage

ACCOUNT = Field(1, "account", "int64", True)
REGION = Field(2, "region", "string", True)
SEQ = Field(3, "seq", "int64", False)
FIELDS = {1: ACCOUNT, 2: REGION, 3: SEQ}


def constraints(account=None, region=None, seq=None):
    return {
        1: account if account is not None else universe("int64", True),
        2: region if region is not None else universe("string", True),
        3: seq if seq is not None else universe("int64", False),
    }


def int_range(lo, hi, null=False):
    """闭区间 [lo, hi] 的列约束。"""
    return ColumnConstraint("int64", lo, hi + 1, null)


def outcomes_of(node, cons):
    return analyze(node, FIELDS, cons).outcomes


class TestIntervals(unittest.TestCase):
    def test_intersect(self):
        self.assertEqual(iv_intersect([(0, 10)], [(5, None)]), [(5, 10)])
        self.assertEqual(iv_intersect([(0, 3)], [(5, 8)]), [])
        self.assertEqual(iv_intersect([("a", "c"), ("e", None)],
                                      [("b", "f")]),
                         [("b", "c"), ("e", "f")])

    def test_subtract(self):
        self.assertEqual(iv_subtract([(0, 10)], [(3, 5)]), [(0, 3), (5, 10)])
        self.assertEqual(iv_subtract([(0, 10)], [(3, None)]), [(0, 3)])
        self.assertEqual(iv_subtract([("", None)], [("京", "京\x00")]),
                         [("", "京"), ("京\x00", None)])
        self.assertEqual(iv_subtract([(0, 10)], [(0, 10)]), [])

    def test_prefix_upper_bound(self):
        self.assertEqual(string_prefix_upper_bound("京"), "亭")  # U+4EAC+1
        self.assertEqual(string_prefix_upper_bound("京𠮷"), "京𠮸")
        self.assertIsNone(string_prefix_upper_bound("\U0010ffff"))
        self.assertEqual(string_prefix_upper_bound("a\U0010ffff"), "b")


class TestPreimage(unittest.TestCase):
    def test_truncate_negative_floors_toward_minus_infinity(self):
        # -11 → truncate(10) = -20（向负无穷取整，不是向零）。
        piece = preimage(Transform("truncate", 10), -20,
                         source_type="int64", source_nullable=True)
        self.assertEqual((piece.lo, piece.hi), (-20, -10))
        self.assertFalse(piece.null_allowed)

    def test_truncate_bucket_may_dip_below_int64_min(self):
        piece = preimage(Transform("truncate", 10), -9223372036854775810,
                         source_type="int64", source_nullable=True)
        # 半开区间 [INT64_MIN, -9223372036854775800)，共 8 个值。
        self.assertEqual((piece.lo, piece.hi),
                         (INT64_MIN, -9223372036854775800))

    def test_prefix_shorter_than_L_means_exact(self):
        piece = preimage(Transform("prefix", 2), "京",
                         source_type="string", source_nullable=True)
        self.assertEqual((piece.lo, piece.hi), ("京", "京\x00"))

    def test_prefix_full_length_means_startswith(self):
        piece = preimage(Transform("prefix", 2), "京𠮷",
                         source_type="string", source_nullable=True)
        self.assertEqual((piece.lo, piece.hi), ("京𠮷", "京𠮸"))

    def test_prefix_empty_string_is_exact_empty(self):
        piece = preimage(Transform("prefix", 2), "",
                         source_type="string", source_nullable=True)
        self.assertEqual((piece.lo, piece.hi), ("", "\x00"))

    def test_void_gives_no_information(self):
        piece = preimage(Transform("void"), None,
                         source_type="int64", source_nullable=True)
        self.assertEqual(piece, universe("int64", True))

    def test_null_partition_means_source_null(self):
        piece = preimage(Transform("identity"), None,
                         source_type="string", source_nullable=True)
        self.assertEqual(piece, null_only("string"))

    def test_same_source_intersection(self):
        truncated = preimage(Transform("truncate", 10), -10,
                             source_type="int64", source_nullable=True)
        identified = preimage(Transform("identity"), -1,
                              source_type="int64", source_nullable=True)
        both = intersect(truncated, identified)
        self.assertEqual((both.lo, both.hi), (-1, 0))
        clash = intersect(truncated, exact("int64", 5))
        self.assertTrue(clash.domain_empty())


class TestSingleLeafOutcomes(unittest.TestCase):
    """每种单叶条件都要利用实际分区原像作出能确定的判定。"""

    def test_eq(self):
        cons = constraints(account=int_range(-10, -1))
        self.assertEqual(outcomes_of(Leaf("eq", 1, -5), cons), {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("eq", 1, 5), cons), {FALSE})
        self.assertEqual(outcomes_of(Leaf("eq", 1, -1),
                                     constraints(account=exact("int64", -1))),
                         {TRUE})

    def test_ne(self):
        cons = constraints(account=exact("int64", 5))
        self.assertEqual(outcomes_of(Leaf("ne", 1, 5), cons), {FALSE})
        self.assertEqual(outcomes_of(Leaf("ne", 1, 4), cons), {TRUE})
        both = constraints(account=int_range(4, 5))
        self.assertEqual(outcomes_of(Leaf("ne", 1, 5), both), {TRUE, FALSE})

    def test_ordering_leaves(self):
        cons = constraints(account=int_range(-10, -1))
        self.assertEqual(outcomes_of(Leaf("lt", 1, 0), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("lt", 1, -3), cons), {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("le", 1, -1), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("gt", 1, -1), cons), {FALSE})
        self.assertEqual(outcomes_of(Leaf("ge", 1, -10), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("ge", 1, -3), cons), {TRUE, FALSE})

    def test_null_leaves(self):
        with_null = constraints(account=int_range(-10, -1, null=True))
        no_null = constraints(account=int_range(-10, -1))
        self.assertEqual(outcomes_of(Leaf("is_null", 1), with_null),
                         {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("is_null", 1), no_null), {FALSE})
        self.assertEqual(outcomes_of(Leaf("is_not_null", 1), with_null),
                         {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("is_not_null", 1), no_null), {TRUE})
        only_null = constraints(account=null_only("int64"))
        self.assertEqual(outcomes_of(Leaf("is_null", 1), only_null), {TRUE})
        self.assertEqual(outcomes_of(Leaf("eq", 1, 7), only_null), {UNKNOWN})

    def test_string_boundaries(self):
        # prefix(2)="京𠮷" → ["京𠮷", "京𠮸")：码点边界精确。
        cons = constraints(region=ColumnConstraint("string", "京𠮷", "京𠮸",
                                                   False))
        self.assertEqual(outcomes_of(Leaf("lt", 2, "京𠮷"), cons), {FALSE})
        self.assertEqual(outcomes_of(Leaf("ge", 2, "京𠮷"), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("lt", 2, "京𠮸"), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("eq", 2, "京𠮷"), cons),
                         {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("eq", 2, "京𠮷照"), cons),
                         {TRUE, FALSE})
        self.assertEqual(outcomes_of(Leaf("eq", 2, "京𠮸"), cons), {FALSE})

    def test_string_exact_short_prefix(self):
        # prefix(2)="京"（短）→ 源值恰为 "京"。
        cons = constraints(region=exact("string", "京"))
        self.assertEqual(outcomes_of(Leaf("eq", 2, "京"), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("lt", 2, "京𠮷"), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("ge", 2, "京𠮷"), cons), {FALSE})

    def test_empty_string_value(self):
        cons = constraints(region=exact("string", ""))
        self.assertEqual(outcomes_of(Leaf("eq", 2, ""), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("lt", 2, "京"), cons), {TRUE})
        self.assertEqual(outcomes_of(Leaf("gt", 2, ""), cons), {FALSE})


class TestSameColumnAnd(unittest.TestCase):
    def test_impossible_range_pair(self):
        # 截断区间 [-10,-1] 与 x>=-3 and x<-3：非 null 不可能为 TRUE。
        cons = constraints(account=int_range(-10, -1))
        node = And((Leaf("ge", 1, -3), Leaf("lt", 1, -3)))
        self.assertEqual(outcomes_of(node, cons), {FALSE})
        self.assertEqual(decide(outcomes_of(node, cons)), "drop")

    def test_nullable_version_keeps_unknown(self):
        cons = constraints(account=int_range(-10, -1, null=True))
        node = And((Leaf("ge", 1, -3), Leaf("lt", 1, -3)))
        self.assertEqual(outcomes_of(node, cons), {FALSE, UNKNOWN})
        self.assertEqual(decide(outcomes_of(node, cons)), "drop")

    def test_identity_collapses_wide_bucket(self):
        # 同源 identity 已把 truncate(10) 的宽区间收成一个值。
        cons = constraints(account=exact("int64", -1))
        node = And((Leaf("ge", 1, -3), Leaf("lt", 1, -3)))
        self.assertEqual(outcomes_of(node, cons), {FALSE})
        negated = Not(node)
        self.assertEqual(outcomes_of(negated, cons), {TRUE})
        self.assertEqual(decide(outcomes_of(negated, cons)), "pass")

    def test_mixed_null_leaf_and_comparison(self):
        cons = constraints(account=int_range(-10, -1, null=True))
        node = And((Leaf("is_not_null", 1), Leaf("lt", 1, -3)))
        self.assertEqual(outcomes_of(node, cons), {TRUE, FALSE})
        node2 = And((Leaf("is_null", 1), Leaf("lt", 1, -3)))
        self.assertEqual(outcomes_of(node2, cons), {FALSE, UNKNOWN})

    def test_nested_and_flattens(self):
        cons = constraints(account=int_range(-10, -1))
        node = And((And((Leaf("ge", 1, -3), Leaf("lt", 1, -3))),
                    Leaf("is_not_null", 1)))
        self.assertEqual(outcomes_of(node, cons), {FALSE})


class TestNotAndMixedLogic(unittest.TestCase):
    def test_not_unknown_stays_unknown(self):
        # 全 null 文件对 not(x=7) 必须 drop，对 not(is_null(x)) 也 drop。
        cons = constraints(account=null_only("int64"))
        self.assertEqual(outcomes_of(Not(Leaf("eq", 1, 7)), cons), {UNKNOWN})
        self.assertEqual(decide(outcomes_of(Not(Leaf("eq", 1, 7)), cons)),
                         "drop")
        # not(is_null(x))：is_null 恒 TRUE，取反恒 FALSE —— 同样 drop，
        # 但原因是 FALSE 而非 UNKNOWN，两者都不能当成“全都匹配”。
        self.assertEqual(outcomes_of(Not(Leaf("is_null", 1)), cons),
                         {FALSE})
        self.assertEqual(decide(outcomes_of(Not(Leaf("is_null", 1)), cons)),
                         "drop")

    def test_not_over_determined_leaf(self):
        cons = constraints(account=int_range(-10, -1))
        self.assertEqual(outcomes_of(Not(Leaf("lt", 1, -3)), cons),
                         {TRUE, FALSE})
        self.assertEqual(decide(outcomes_of(Not(Leaf("lt", 1, -3)), cons)),
                         "filter")

    def test_or_conservative_but_sound(self):
        cons = constraints(account=int_range(-10, -1))
        node = Or((Leaf("lt", 1, -3), Leaf("ge", 1, -3)))
        # 两叶各自都可能 TRUE/FALSE；保守合并允许 {TRUE, FALSE}，
        # 但绝不能丢 TRUE（真实语义下非 null 行必有一边成立）。
        outcomes = outcomes_of(node, cons)
        self.assertIn(TRUE, outcomes)

    def test_cross_column_and(self):
        cons = constraints(account=int_range(-10, -1),
                           region=exact("string", "京"))
        node = And((Leaf("lt", 1, -3), Leaf("eq", 2, "京")))
        self.assertEqual(outcomes_of(node, cons), {TRUE, FALSE})
        node2 = And((Leaf("lt", 1, -3), Leaf("eq", 2, "沪")))
        self.assertEqual(outcomes_of(node2, cons), {FALSE})

    def test_void_file_stays_filter(self):
        # void 分区的文件对 not(account<-3) 保守过滤（不能证明就保留）。
        cons = constraints(account=universe("int64", True))
        node = Not(Leaf("lt", 1, -3))
        self.assertEqual(outcomes_of(node, cons), {TRUE, FALSE, UNKNOWN})
        self.assertEqual(decide(outcomes_of(node, cons)), "filter")


if __name__ == "__main__":
    unittest.main()
