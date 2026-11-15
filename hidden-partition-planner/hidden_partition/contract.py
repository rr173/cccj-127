"""manifest 输入合同：逻辑 schema、多个分区 spec、文件列表。

manifest 是输入方已经核对过的数据目录：规划时相信其中的行数与分区值，
不靠打开数据反推。这里做的校验是“合同校验”——结构、类型、引用完整性、
分区值是否可能被对应变换产出、以及同源多分区字段的交集是否为空
（row_count>0 时交集为空即矛盾目录，整体拒绝）。

键的约定（全部严格，多余键即拒绝）：
- 顶层：``schema`` / ``specs`` / ``files``；
- schema 字段：``field_id``（正整数）/ ``name``（唯一非空）/ ``type``
  （``int64`` 或 ``string``）/ ``nullable``（布尔）；
- spec：``spec_id``（唯一非空字符串）+ ``partition_fields`` 列表；
- 分区字段：``partition_id``（本 spec 内唯一正整数）/ ``name``（说明名称，
  可以重复或改名，绝不用于关联业务列）/ ``source_id``（引用 field_id）/
  ``transform``；
- 文件：``file_id``（唯一非空字符串）/ ``path``（相对路径）/ ``spec_id`` /
  ``row_count``（非负整数）/ ``partitions``（以 partition_id 十进制字符串
  为键的完整分区值，不多不少）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .constraints import TYPE_INT64, TYPE_STRING
from .errors import CatalogError, Issue
from .jsutil import load_json_file
from .limits import DEFAULT_LIMITS, Limits
from .transforms import (
    Transform,
    compute_file_constraints,
    is_int,
    parse_transform,
    validate_partition_value,
)

_TYPES = (TYPE_INT64, TYPE_STRING)


@dataclass(frozen=True)
class Field:
    field_id: int
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class PartitionField:
    partition_id: int
    name: str
    source_id: int
    transform: Transform


@dataclass(frozen=True)
class Spec:
    spec_id: str
    partition_fields: Tuple[PartitionField, ...]

    @property
    def partition_ids(self) -> set:
        return {pf.partition_id for pf in self.partition_fields}


@dataclass(frozen=True)
class FileEntry:
    file_id: str
    path: str
    spec_id: str
    row_count: int
    partitions: Dict[int, Any]  # partition_id -> 分区值


@dataclass(frozen=True)
class Manifest:
    fields: Tuple[Field, ...]
    specs: Tuple[Spec, ...]
    files: Tuple[FileEntry, ...]
    fields_by_id: Dict[int, Field]
    specs_by_id: Dict[str, Spec]

    def spec_of(self, entry: FileEntry) -> Spec:
        return self.specs_by_id[entry.spec_id]


def _issue_unknown_keys(obj: dict, allowed: set, location: str,
                        issues: List[Issue]) -> None:
    for key in obj:
        if key not in allowed:
            issues.append(Issue(f"{location}.{key}", "不允许的键"))


def _parse_schema(obj: Any, issues: List[Issue]) -> Tuple[Field, ...]:
    fields: List[Field] = []
    if not isinstance(obj, dict):
        issues.append(Issue("$.schema", "schema 必须是对象"))
        return ()
    _issue_unknown_keys(obj, {"fields"}, "$.schema", issues)
    raw_fields = obj.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        issues.append(Issue("$.schema.fields", "fields 必须是非空列表"))
        return ()
    seen_ids: set = set()
    seen_names: set = set()
    for index, item in enumerate(raw_fields):
        loc = f"$.schema.fields[{index}]"
        if not isinstance(item, dict):
            issues.append(Issue(loc, "字段必须是对象"))
            continue
        _issue_unknown_keys(item, {"field_id", "name", "type", "nullable"},
                            loc, issues)
        field_id = item.get("field_id")
        name = item.get("name")
        type_name = item.get("type")
        nullable = item.get("nullable")
        ok = True
        if not is_int(field_id) or field_id < 1:
            issues.append(Issue(f"{loc}.field_id",
                                f"field_id 必须是正整数，实际：{field_id!r}"))
            ok = False
        elif field_id in seen_ids:
            issues.append(Issue(f"{loc}.field_id",
                                f"field_id 重复：{field_id}"))
            ok = False
        if not isinstance(name, str) or not name:
            issues.append(Issue(f"{loc}.name",
                                f"name 必须是非空字符串，实际：{name!r}"))
            ok = False
        elif name in seen_names:
            issues.append(Issue(f"{loc}.name", f"name 重复：{name!r}"))
            ok = False
        if type_name not in _TYPES:
            issues.append(Issue(f"{loc}.type",
                                f"type 必须是 int64 或 string，实际：{type_name!r}"))
            ok = False
        if not isinstance(nullable, bool):
            issues.append(Issue(f"{loc}.nullable",
                                f"nullable 必须是布尔，实际：{nullable!r}"))
            ok = False
        if ok:
            seen_ids.add(field_id)
            seen_names.add(name)
            fields.append(Field(field_id, name, type_name, nullable))
    return tuple(fields)


def _parse_specs(obj: Any, fields_by_id: Dict[int, Field],
                 issues: List[Issue]) -> Tuple[Spec, ...]:
    specs: List[Spec] = []
    if not isinstance(obj, list):
        issues.append(Issue("$.specs", "specs 必须是列表"))
        return ()
    seen_ids: set = set()
    for index, item in enumerate(obj):
        loc = f"$.specs[{index}]"
        if not isinstance(item, dict):
            issues.append(Issue(loc, "spec 必须是对象"))
            continue
        _issue_unknown_keys(item, {"spec_id", "partition_fields"}, loc, issues)
        spec_id = item.get("spec_id")
        ok = True
        if not isinstance(spec_id, str) or not spec_id:
            issues.append(Issue(f"{loc}.spec_id",
                                f"spec_id 必须是非空字符串，实际：{spec_id!r}"))
            ok = False
        elif spec_id in seen_ids:
            issues.append(Issue(f"{loc}.spec_id", f"spec_id 重复：{spec_id!r}"))
            ok = False
        raw_pfs = item.get("partition_fields")
        pfs: List[PartitionField] = []
        if not isinstance(raw_pfs, list):
            issues.append(Issue(f"{loc}.partition_fields",
                                "partition_fields 必须是列表"))
            ok = False
        else:
            seen_pids: set = set()
            for pf_index, pf_item in enumerate(raw_pfs):
                pf_loc = f"{loc}.partition_fields[{pf_index}]"
                pf = _parse_partition_field(pf_item, pf_loc, fields_by_id,
                                            issues)
                if pf is None:
                    continue
                if pf.partition_id in seen_pids:
                    issues.append(Issue(f"{pf_loc}.partition_id",
                                        f"partition_id 在本 spec 内重复："
                                        f"{pf.partition_id}"))
                    continue
                seen_pids.add(pf.partition_id)
                pfs.append(pf)
        if ok:
            seen_ids.add(spec_id)
            specs.append(Spec(spec_id, tuple(pfs)))
    return tuple(specs)


def _parse_partition_field(item: Any, loc: str,
                           fields_by_id: Dict[int, Field],
                           issues: List[Issue]) -> Optional[PartitionField]:
    if not isinstance(item, dict):
        issues.append(Issue(loc, "分区字段必须是对象"))
        return None
    _issue_unknown_keys(item, {"partition_id", "name", "source_id", "transform"},
                        loc, issues)
    partition_id = item.get("partition_id")
    name = item.get("name")
    source_id = item.get("source_id")
    ok = True
    if not is_int(partition_id) or partition_id < 1:
        issues.append(Issue(f"{loc}.partition_id",
                            f"partition_id 必须是正整数，实际：{partition_id!r}"))
        ok = False
    if not isinstance(name, str) or not name:
        issues.append(Issue(f"{loc}.name",
                            f"分区字段说明名称必须是非空字符串，实际：{name!r}"))
        ok = False
    field = None
    if not is_int(source_id) or source_id < 1:
        issues.append(Issue(f"{loc}.source_id",
                            f"source_id 必须是正整数 field_id，实际：{source_id!r}"))
        ok = False
    elif source_id not in fields_by_id:
        issues.append(Issue(f"{loc}.source_id",
                            f"source_id 引用了不存在的 field_id：{source_id}"))
        ok = False
    else:
        field = fields_by_id[source_id]
    transform = parse_transform(item.get("transform"),
                                field.type if field else TYPE_INT64,
                                f"{loc}.transform", issues)
    if not ok or transform is None:
        return None
    return PartitionField(partition_id, name, source_id, transform)


def _parse_files(obj: Any, manifest_parts, issues: List[Issue]) -> Tuple[FileEntry, ...]:
    fields_by_id, specs_by_id = manifest_parts
    files: List[FileEntry] = []
    if not isinstance(obj, list):
        issues.append(Issue("$.files", "files 必须是列表"))
        return ()
    seen_ids: set = set()
    for index, item in enumerate(obj):
        loc = f"$.files[{index}]"
        if not isinstance(item, dict):
            issues.append(Issue(loc, "文件条目必须是对象"))
            continue
        _issue_unknown_keys(
            item, {"file_id", "path", "spec_id", "row_count", "partitions"},
            loc, issues)
        file_id = item.get("file_id")
        path = item.get("path")
        spec_id = item.get("spec_id")
        row_count = item.get("row_count")
        partitions = item.get("partitions")
        ok = True
        if not isinstance(file_id, str) or not file_id:
            issues.append(Issue(f"{loc}.file_id",
                                f"file_id 必须是非空字符串，实际：{file_id!r}"))
            ok = False
        elif file_id in seen_ids:
            issues.append(Issue(f"{loc}.file_id",
                                f"file_id 重复：{file_id!r}"))
            ok = False
        if not _path_ok(path):
            issues.append(Issue(f"{loc}.path",
                                f"path 必须是留在目录内的相对路径，实际：{path!r}"))
            ok = False
        spec = None
        if not isinstance(spec_id, str) or not spec_id:
            issues.append(Issue(f"{loc}.spec_id",
                                f"spec_id 必须是非空字符串，实际：{spec_id!r}"))
            ok = False
        elif spec_id not in specs_by_id:
            issues.append(Issue(f"{loc}.spec_id",
                                f"spec_id 引用了不存在的 spec：{spec_id!r}"))
            ok = False
        else:
            spec = specs_by_id[spec_id]
        if not is_int(row_count) or row_count < 0:
            issues.append(Issue(f"{loc}.row_count",
                                f"row_count 必须是非负整数，实际：{row_count!r}"))
            ok = False
        parsed_partitions: Dict[int, Any] = {}
        if spec is not None:
            parsed_partitions = _parse_partitions(
                partitions, spec, fields_by_id, loc, issues)
        if ok:
            seen_ids.add(file_id)
            files.append(FileEntry(file_id, path, spec_id, row_count,
                                   parsed_partitions))
    return tuple(files)


def _path_ok(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if "\x00" in path:
        return False
    if os.path.isabs(path):
        return False
    parts = path.replace("\\", "/").split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _parse_partitions(obj: Any, spec: Spec, fields_by_id: Dict[int, Field],
                      loc: str, issues: List[Issue]) -> Dict[int, Any]:
    """校验分区值完整性与每个值本身；返回 partition_id -> 值。"""
    if not isinstance(obj, dict):
        issues.append(Issue(f"{loc}.partitions", "partitions 必须是对象"))
        return {}
    expected = {str(pid): pid for pid in spec.partition_ids}
    parsed: Dict[int, Any] = {}
    for key, value in obj.items():
        if key not in expected:
            issues.append(Issue(f'{loc}.partitions["{key}"]',
                                f"spec {spec.spec_id!r} 中不存在该 partition_id "
                                f"或键不是其十进制写法"))
            continue
        parsed[expected[key]] = value
    for key, pid in expected.items():
        if key not in obj:
            issues.append(Issue(f"{loc}.partitions",
                                f"缺少分区字段 partition_id={pid} 的值"))
    pf_by_id = {pf.partition_id: pf for pf in spec.partition_fields}
    for pid, value in parsed.items():
        pf = pf_by_id[pid]
        field = fields_by_id[pf.source_id]
        validate_partition_value(
            pf.transform, value,
            source_type=field.type, source_nullable=field.nullable,
            location=f'{loc}.partitions["{pid}"]', issues=issues)
    return parsed


def parse_manifest(obj: Any) -> Manifest:
    """校验并组装 manifest；任何合同问题都一次性收集后抛出 CatalogError。"""
    issues: List[Issue] = []
    if not isinstance(obj, dict):
        raise CatalogError([Issue("$", "manifest 必须是对象")])
    _issue_unknown_keys(obj, {"schema", "specs", "files"}, "$", issues)
    fields = _parse_schema(obj.get("schema"), issues)
    fields_by_id = {f.field_id: f for f in fields}
    specs = _parse_specs(obj.get("specs"), fields_by_id, issues)
    specs_by_id = {s.spec_id: s for s in specs}
    files = _parse_files(obj.get("files"), (fields_by_id, specs_by_id), issues)

    # 矛盾目录检查：同源多分区字段的原像交集为空且 row_count>0 → 拒绝。
    # 零行文件的结构已在上面验证，这里只对非空文件要求交集非空。
    # 结构问题已经记账时跳过本阶段（目录反正被拒，且残缺分区无法安全求交集）。
    if not issues:
        for index, entry in enumerate(files):
            if entry.row_count == 0:
                continue
            spec = specs_by_id.get(entry.spec_id)
            if spec is None:
                continue
            constraints = compute_file_constraints(
                fields_by_id, spec.partition_fields, entry.partitions)
            for field_id, constraint in constraints.items():
                if constraint.domain_empty():
                    field = fields_by_id[field_id]
                    issues.append(Issue(
                        f"$.files[{index}].partitions",
                        f"矛盾目录：列 {field.name}(field_id={field_id}) 的各分区"
                        f"原像交集为空，但 row_count={entry.row_count} > 0"))
    if issues:
        raise CatalogError(issues)
    return Manifest(fields, specs, files, fields_by_id, specs_by_id)


def load_manifest(path: str, limits: Limits = DEFAULT_LIMITS) -> Manifest:
    """从磁盘读取并校验 manifest（只读，不触碰任何数据文件）。"""
    limits.check_positive()
    obj = load_json_file(path, limits.max_manifest_bytes, what="manifest")
    return parse_manifest(obj)
