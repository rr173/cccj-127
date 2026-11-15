"""manifest 输入合同校验：结构、类型、引用、分区值、矛盾目录。"""
from __future__ import annotations

import copy
import unittest

from hidden_partition import CatalogError, parse_manifest
from hidden_partition.constraints import INT64_MAX


def base_manifest() -> dict:
    """一个最小但合法的 manifest，测试用例在它上面做单点破坏。"""
    return {
        "schema": {"fields": [
            {"field_id": 1, "name": "account", "type": "int64",
             "nullable": True},
            {"field_id": 2, "name": "region", "type": "string",
             "nullable": True},
            {"field_id": 3, "name": "seq", "type": "int64",
             "nullable": False},
        ]},
        "specs": [
            {"spec_id": "s1", "partition_fields": [
                {"partition_id": 1, "name": "账户区间", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 10}},
                {"partition_id": 2, "name": "地区", "source_id": 2,
                 "transform": {"kind": "prefix", "length": 2}},
            ]},
            {"spec_id": "s2", "partition_fields": [
                {"partition_id": 1, "name": "账户", "source_id": 1,
                 "transform": {"kind": "identity"}},
                {"partition_id": 2, "name": "账户再分区", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 4}},
                {"partition_id": 3, "name": "地区", "source_id": 2,
                 "transform": {"kind": "void"}},
            ]},
        ],
        "files": [
            {"file_id": "f1", "path": "data/f1.jsonl", "spec_id": "s1",
             "row_count": 2, "partitions": {"1": -10, "2": "京𠮷"}},
            {"file_id": "f2", "path": "data/f2.jsonl", "spec_id": "s2",
             "row_count": 0, "partitions": {"1": 5, "2": 8, "3": None}},
        ],
    }


def expect_error(obj: dict, needle: str) -> CatalogError:
    try:
        parse_manifest(obj)
    except CatalogError as exc:
        text = str(exc)
        assert needle in text, f"期望错误包含 {needle!r}，实际：{text}"
        return exc
    raise AssertionError(f"期望 CatalogError（{needle!r}），但通过了校验")


def mutate(obj: dict, fn) -> dict:
    obj = copy.deepcopy(obj)
    fn(obj)
    return obj


class TestSchemaContract(unittest.TestCase):
    def test_base_manifest_is_valid(self) -> None:
        manifest = parse_manifest(base_manifest())
        self.assertEqual(len(manifest.files), 2)
        self.assertEqual(manifest.fields_by_id[1].name, "account")

    def test_field_id_must_be_positive_int(self) -> None:
        for bad in (0, -1, True, "1", 1.0):
            expect_error(mutate(base_manifest(),
                                lambda m: m["schema"]["fields"][0]
                                .update(field_id=bad)),
                         "field_id")

    def test_duplicate_field_id_rejected(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][1]
                            .update(field_id=1)),
                     "field_id 重复")

    def test_name_unique_nonempty(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][1]
                            .update(name="account")),
                     "name 重复")
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][1]
                            .update(name="")),
                     "非空字符串")

    def test_type_enum(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][0]
                            .update(type="int32")),
                     "int64 或 string")

    def test_nullable_must_be_bool(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][0]
                            .update(nullable=1)),
                     "nullable 必须是布尔")

    def test_unknown_keys_rejected(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m.update(version=1)),
                     "不允许的键")
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"]["fields"][0]
                            .update(comment="x")),
                     "不允许的键")

    def test_empty_fields_rejected(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["schema"].update(fields=[])),
                     "非空列表")


class TestSpecContract(unittest.TestCase):
    def test_duplicate_spec_id(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][1].update(spec_id="s1")),
                     "spec_id 重复")

    def test_partition_id_unique_within_spec_only(self) -> None:
        # 不同 spec 使用相同 partition_id 合法（基线 manifest 已是如此）。
        parse_manifest(base_manifest())
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][1]["partition_fields"][1]
                            .update(partition_id=1)),
                     "本 spec 内重复")

    def test_source_id_must_reference_field(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][0]["partition_fields"][0]
                            .update(source_id=99)),
                     "不存在的 field_id")

    def test_transform_param_positive_int(self) -> None:
        for bad in (0, -3, True, "10", 2.5):
            expect_error(mutate(base_manifest(),
                                lambda m: m["specs"][0]["partition_fields"][0]
                                .update(transform={"kind": "truncate",
                                                   "width": bad})),
                         "正整数")

    def test_truncate_only_for_int64(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][0]["partition_fields"][1]
                            .update(transform={"kind": "truncate",
                                               "width": 2})),
                     "只对 int64")

    def test_prefix_only_for_string(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][0]["partition_fields"][0]
                            .update(transform={"kind": "prefix",
                                               "length": 2})),
                     "只对 string")

    def test_unknown_transform(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["specs"][0]["partition_fields"][0]
                            .update(transform={"kind": "hash"})),
                     "未知变换")

    def test_descriptive_names_may_repeat(self) -> None:
        # 说明名称相同/改名都合法：关联只靠 field_id 与 partition_id。
        manifest = parse_manifest(base_manifest())
        names = [pf.name for spec in manifest.specs
                 for pf in spec.partition_fields]
        self.assertIn("账户区间", names)
        self.assertIn("地区", names)


class TestFileContract(unittest.TestCase):
    def test_duplicate_file_id(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1].update(file_id="f1")),
                     "file_id 重复")

    def test_unknown_spec_reference(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0].update(spec_id="nope")),
                     "不存在的 spec")

    def test_row_count_non_negative_int(self) -> None:
        for bad in (-1, True, "3", 1.5):
            expect_error(mutate(base_manifest(),
                                lambda m: m["files"][0]
                                .update(row_count=bad)),
                         "非负整数")

    def test_path_must_be_relative(self) -> None:
        for bad in ("/abs/path.jsonl", "../escape.jsonl", "a/../b.jsonl",
                    "", "./x.jsonl"):
            expect_error(mutate(base_manifest(),
                                lambda m: m["files"][0].update(path=bad)),
                         "相对路径")

    def test_partitions_must_be_complete(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"].pop("2")),
                     "缺少分区字段")

    def test_partitions_no_extra_keys(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"]
                            .update({"9": 0})),
                     "不存在该 partition_id")

    def test_partition_key_must_be_canonical_decimal(self) -> None:
        def bad(m):
            m["files"][0]["partitions"]["01"] = -10
            del m["files"][0]["partitions"]["1"]
        expect_error(mutate(base_manifest(), bad), "十进制")


class TestPartitionValues(unittest.TestCase):
    def test_identity_value_type_and_range(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1]["partitions"]
                            .update({"1": "abc"})),
                     "int64 整数")
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1]["partitions"]
                            .update({"1": INT64_MAX + 1})),
                     "int64 整数")
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1]["partitions"]
                            .update({"1": True})),
                     "int64 整数")

    def test_truncate_multiple_of_width(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"]
                            .update({"1": -7})),
                     "整数倍")

    def test_truncate_may_dip_below_int64_min(self) -> None:
        # W*floor(INT64_MIN/10) = -9223372036854775810 < INT64_MIN：合法。
        obj = mutate(base_manifest(),
                     lambda m: m["files"][0]["partitions"]
                     .update({"1": -9223372036854775810}))
        parse_manifest(obj)

    def test_truncate_preimage_must_touch_int64(self) -> None:
        # 9223372036854775810 是 10 的倍数但 > INT64_MAX：原像为空。
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"]
                            .update({"1": 9223372036854775810})),
                     "没有交集")
        # -9223372036854775830 是 10 的倍数，但整个桶都在 int64 之下。
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"]
                            .update({"1": -9223372036854775830})),
                     "没有交集")

    def test_prefix_value_length_in_codepoints(self) -> None:
        # 𠮷 是一个码点（非 BMP），"京𠮷" 长度为 2，合法。
        parse_manifest(base_manifest())
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][0]["partitions"]
                            .update({"2": "京𠮷照"})),
                     "最多 2 个码点")

    def test_void_partition_must_be_null(self) -> None:
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1]["partitions"]
                            .update({"3": "京"})),
                     "void 分区值必须为 null")

    def test_non_void_null_for_non_nullable_source(self) -> None:
        obj = mutate(base_manifest(),
                     lambda m: (m["specs"][0]["partition_fields"][0]
                                .update(source_id=3),
                                m["files"][0]["partitions"].update({"1": None})))
        expect_error(obj, "不可空源列")

    def test_zero_row_file_still_structurally_validated(self) -> None:
        # 零行文件的分区值仍要符合变换（这里 truncate 值不是 4 的倍数）。
        expect_error(mutate(base_manifest(),
                            lambda m: m["files"][1]["partitions"]
                            .update({"2": 7})),
                     "整数倍")


class TestContradictoryCatalog(unittest.TestCase):
    def test_empty_intersection_with_rows_rejected(self) -> None:
        # identity=5 与 truncate(4)=8 → [8,11] 与 {5} 交集为空，row_count>0。
        obj = mutate(base_manifest(),
                     lambda m: m["files"][1].update(row_count=3))
        expect_error(obj, "矛盾目录")

    def test_empty_intersection_zero_rows_allowed(self) -> None:
        # 同样的空交集，row_count=0：结构合法，规划时直接跳过。
        parse_manifest(base_manifest())

    def test_null_partition_for_nullable_source_ok(self) -> None:
        obj = mutate(base_manifest(),
                     lambda m: m["files"][0]["partitions"]
                     .update({"1": None, "2": None}))
        parse_manifest(obj)

    def test_all_issues_reported_together(self) -> None:
        def break_many(m):
            m["schema"]["fields"][0]["field_id"] = 0
            m["files"][0]["partitions"]["1"] = -7
            m["files"][1]["spec_id"] = "ghost"
        exc = expect_error(mutate(base_manifest(), break_many), "")
        self.assertGreaterEqual(len(exc.issues), 3)


if __name__ == "__main__":
    unittest.main()
