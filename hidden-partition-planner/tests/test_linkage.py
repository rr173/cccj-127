"""必测联动：新旧 spec 混放下 not / 空交集 and / 前缀码点边界。

- 旧 spec 账户区间 [-10,-1]、新 spec [-4,-1]、同源 truncate+identity=-1、
  全 null、void 五类文件对 not(account<-3) 的不同结论；
- account>=-3 and account<-3 排空非 null 可能性，并确认 null 不会穿过 not；
- 地区前缀含 "京𠮷" 时，短前缀值、完整前缀等值与相邻大小比较的
  码点边界行为，以及必须保留的剩余过滤。
"""
from __future__ import annotations

import json
import unittest

from hidden_partition import build_plan, load_manifest, load_query, run_scan

from ._helpers import (
    MANIFEST_PATH,
    TempDirTestCase,
    query_path,
)


def read_jsonl(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class LinkageCase(TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(MANIFEST_PATH)

    def decisions(self, query_name: str) -> dict:
        query = load_query(query_path(query_name),
                           self.manifest.fields_by_id)
        plan = build_plan(self.manifest, query)
        return {f["file_id"]: f["decision"] for f in plan["files"]}

    def scan(self, query_name: str, projection=None):
        out = self.path("out.jsonl")
        rep = self.path("report.json")
        report = run_scan(MANIFEST_PATH, query_path(query_name), out, rep,
                          projection=projection)
        return report, read_jsonl(out)


class TestNotAccountLinkage(LinkageCase):
    def test_five_file_kinds(self) -> None:
        decisions = self.decisions("q1_not_account_lt_neg3")
        # 旧 spec 宽区间 [-10,-1]：跨 -3，必须保留并逐行过滤。
        self.assertEqual(decisions["f-old-wide"], "filter")
        # 新 spec 窄区间 [-4,-1]：同样跨 -3，保留并过滤。
        self.assertEqual(decisions["f-new-narrow"], "filter")
        # 同源 truncate(10)=-10 与 identity=-1 的交集是 {-1}：直接 pass。
        self.assertEqual(decisions["f-same-source"], "pass")
        # 全 null：not(UNKNOWN) 仍是 UNKNOWN，不存在 TRUE 行 → drop。
        self.assertEqual(decisions["f-all-null"], "drop")
        # void：对账户一无所知，保守过滤。
        self.assertEqual(decisions["f-void"], "filter")

    def test_delivered_rows_match(self) -> None:
        _report, rows = self.scan("q1_not_account_lt_neg3")
        accounts = [r["account"] for r in rows]
        self.assertEqual(accounts,
                         [-1, -3, -1, -1, -1, -1, 0, 2 ** 63 - 1,
                          0, 1, 3, 4, 5, 3, 3, 3, 0, 4])
        self.assertNotIn(None, accounts, "null 不得穿过 not 被交付")

    def test_empty_and_drops_everything(self) -> None:
        decisions = self.decisions("q2_empty_and")
        self.assertTrue(all(d == "drop" for d in decisions.values()))
        _report, rows = self.scan("q2_empty_and")
        self.assertEqual(rows, [])

    def test_not_of_empty_and_keeps_non_null_drops_null(self) -> None:
        decisions = self.decisions("q3_not_empty_and")
        # 非 null 已排空的文件：not(FALSE)=TRUE → pass。
        self.assertEqual(decisions["f-old-wide"], "pass")
        self.assertEqual(decisions["f-same-source"], "pass")
        # 全 null 文件：not(UNKNOWN)=UNKNOWN → 仍然 drop（null 不穿过 not）。
        self.assertEqual(decisions["f-all-null"], "drop")
        # void 文件：非 null 行 TRUE、null 行 UNKNOWN → 保守过滤。
        self.assertEqual(decisions["f-void"], "filter")
        _report, rows = self.scan("q3_not_empty_and")
        self.assertNotIn(None, [r["account"] for r in rows])
        # f-void 是第 5 个文件（前 4 个交付 3+3+3+0 行）：它的 null 账户行
        # 被过滤掉，int64 两端与 0 三行按序交付在第 9..11 位。
        self.assertEqual(rows[9:12], [
            {"account": -(2 ** 63), "region": "京𠮷"},
            {"account": 0, "region": "京𠮷照"},
            {"account": 2 ** 63 - 1, "region": "京𠮷照"},
        ])


class TestRegionPrefixLinkage(LinkageCase):
    def test_short_prefix_value_means_exact(self) -> None:
        # f-short-prefix 的 prefix(2) 值 "京" 短于 2：源值恰为 "京"。
        decisions = self.decisions("q4_region_eq")
        self.assertEqual(decisions["f-short-prefix"], "drop")  # "京" ≠ "京𠮷"
        decisions = self.decisions("q6_region_lt")
        self.assertEqual(decisions["f-short-prefix"], "pass")  # "京" < "京𠮷"

    def test_full_prefix_equality(self) -> None:
        # f-new-narrow 的 prefix(2)="京𠮷"（恰 2 码点）：以 "京𠮷" 开头的
        # 全部字符串都可能，等值 "京𠮷" 必须保留剩余过滤。
        decisions = self.decisions("q4_region_eq")
        self.assertEqual(decisions["f-new-narrow"], "filter")
        _report, rows = self.scan("q4_region_eq")
        # f-new-narrow×2、f-void×2、f-pos-mixed×1、f-old-2×2，共 7 行。
        self.assertEqual(
            [r["region"] for r in rows],
            ["京𠮷"] * 7)
        # f-old-2 的 identity="京𠮷"：等值直接 pass。
        self.assertEqual(decisions["f-old-2"], "pass")

    def test_adjacent_comparisons_bound_prefix(self) -> None:
        # region >= "京𠮷" and region < "京𠮸" 恰好圈出 "京𠮷" 前缀：
        # 下一个码点 𠮸=U+20BB8 是 𠮷=U+20BB7 的后继。
        decisions = self.decisions("q5_region_adjacent")
        self.assertEqual(decisions["f-new-narrow"], "pass")
        self.assertEqual(decisions["f-void"], "pass")
        # prefix(1)="京" 的约束更宽：既有界内也有界外值 → 必须剩余过滤。
        self.assertEqual(decisions["f-pos-mixed"], "filter")
        _report, rows = self.scan("q5_region_adjacent")
        regions = [r["region"] for r in rows]
        self.assertIn("京𠮷照", regions)   # 前缀命中，长串也交付
        self.assertNotIn("京𠮸", regions)  # 后继码点开头，界外
        self.assertNotIn("京", regions)    # 短于前缀，界外

    def test_codepoint_boundary_lt(self) -> None:
        decisions = self.decisions("q6_region_lt")
        # ["京𠮷","京𠮸") 内没有 < "京𠮷" 的值 → drop。
        self.assertEqual(decisions["f-new-narrow"], "drop")
        self.assertEqual(decisions["f-void"], "drop")
        # identity "京𠮷照" ≥ "京𠮷" → drop。
        self.assertEqual(decisions["f-old-wide"], "drop")
        # "京" < "京𠮷"（短串是前缀则更小）→ pass。
        self.assertEqual(decisions["f-short-prefix"], "pass")
        # "" 是最小字符串 → pass。
        self.assertEqual(decisions["f-empty-str"], "pass")


class TestMixedLogicLinkage(LinkageCase):
    def test_mixed_and_or(self) -> None:
        decisions = self.decisions("q8_mixed")
        # (account<-3 or account>0) and region="京𠮷"
        self.assertEqual(decisions["f-old-wide"], "drop")   # 地区不符
        self.assertEqual(decisions["f-new-narrow"], "filter")
        self.assertEqual(decisions["f-void"], "filter")
        self.assertEqual(decisions["f-pos-mixed"], "filter")
        self.assertEqual(decisions["f-old-2"], "filter")
        _report, rows = self.scan("q8_mixed")
        self.assertEqual(rows, [
            {"account": -4, "region": "京𠮷"},
            {"account": -(2 ** 63), "region": "京𠮷"},
            {"account": 3, "region": "京𠮷"},
            {"account": 4, "region": "京𠮷"},
        ])

    def test_unprojected_filter_column(self) -> None:
        # 过滤列 account 不投影：判断照做，输出只有 region。
        report, rows = self.scan("q1_not_account_lt_neg3", projection=[2])
        self.assertTrue(all(set(r.keys()) == {"region"} for r in rows))
        self.assertEqual(len(rows), 18)
        self.assertEqual([p["name"] for p in report["projection"]],
                         ["region"])


if __name__ == "__main__":
    unittest.main()
