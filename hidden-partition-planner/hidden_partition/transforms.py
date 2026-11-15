"""分区变换与原像。

支持的变换：
- ``identity``           原样保留非 null 值；
- ``truncate(W)``        仅 int64，W 正整数，映射为 ``W*floor(x/W)``（向负无穷取整）；
- ``prefix(L)``          仅 string，L 正整数，取前 L 个 Unicode 码点（不按字节截断）；
- ``void``               不论源值是否为 null 都给 null。

前三种变换把源 null 映为分区 null。分区值在目录载入时严格校验：
truncate 值必须是 W 的整数倍且原像与 int64 有交集（允许略低于 int64 下界，
因为 ``W*floor(INT64_MIN/W)`` 本身可以小于 INT64_MIN）；prefix 值最多 L 个
码点；void 值必须为 null。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .constraints import (
    INT64_MAX,
    INT64_MIN,
    TYPE_INT64,
    TYPE_STRING,
    ColumnConstraint,
    exact,
    intersect,
    null_only,
    string_prefix_upper_bound,
    universe,
)
from .errors import Issue

KIND_IDENTITY = "identity"
KIND_TRUNCATE = "truncate"
KIND_PREFIX = "prefix"
KIND_VOID = "void"
_KINDS = (KIND_IDENTITY, KIND_TRUNCATE, KIND_PREFIX, KIND_VOID)


@dataclass(frozen=True)
class Transform:
    kind: str
    param: Optional[int] = None  # truncate 的 W / prefix 的 L

    def describe(self) -> str:
        if self.kind == KIND_TRUNCATE:
            return f"truncate({self.param})"
        if self.kind == KIND_PREFIX:
            return f"prefix({self.param})"
        return self.kind


def is_int(value: Any) -> bool:
    """JSON 整数；bool 不算整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_transform(obj: Any, source_type: str, location: str,
                    issues: list) -> Optional[Transform]:
    """校验变换定义；出错时往 issues 里记账并返回 None。"""
    if not isinstance(obj, dict):
        issues.append(Issue(location, "transform 必须是对象"))
        return None
    allowed_keys = {"kind", "width", "length"}
    for key in obj:
        if key not in allowed_keys:
            issues.append(Issue(f"{location}.{key}", "transform 不允许的键"))
    kind = obj.get("kind")
    if kind not in _KINDS:
        issues.append(Issue(f"{location}.kind",
                            f"未知变换：{kind!r}（支持 {list(_KINDS)}）"))
        return None
    if kind in (KIND_IDENTITY, KIND_VOID):
        if "width" in obj or "length" in obj:
            issues.append(Issue(location, f"{kind} 不接受 width/length 参数"))
            return None
        return Transform(kind)
    param_key = "width" if kind == KIND_TRUNCATE else "length"
    param = obj.get(param_key)
    if not is_int(param) or param < 1:
        issues.append(Issue(f"{location}.{param_key}",
                            f"{kind} 的参数必须是正整数，实际：{param!r}"))
        return None
    if kind == KIND_TRUNCATE and source_type != TYPE_INT64:
        issues.append(Issue(location, "truncate(W) 只对 int64 源列开放"))
        return None
    if kind == KIND_PREFIX and source_type != TYPE_STRING:
        issues.append(Issue(location, "prefix(L) 只对 string 源列开放"))
        return None
    return Transform(kind, param)


def validate_partition_value(transform: Transform, value: Any, *,
                             source_type: str, source_nullable: bool,
                             location: str, issues: list) -> None:
    """按变换严格校验一个分区值。"""
    if transform.kind == KIND_VOID:
        if value is not None:
            issues.append(Issue(location, "void 分区值必须为 null"))
        return
    if value is None:
        # 前三种变换把源 null 映为分区 null；不可空源不可能产生 null 分区。
        if not source_nullable:
            issues.append(Issue(location,
                                "不可空源列不允许出现非 void 的 null 分区值"))
        return
    if transform.kind == KIND_IDENTITY:
        if source_type == TYPE_INT64:
            if not is_int(value) or not (INT64_MIN <= value <= INT64_MAX):
                issues.append(Issue(location,
                                    f"identity 分区值必须是 int64 整数，实际：{value!r}"))
        elif not isinstance(value, str):
            issues.append(Issue(location,
                                f"identity 分区值必须是字符串，实际：{value!r}"))
        return
    if transform.kind == KIND_TRUNCATE:
        width = transform.param
        if not is_int(value):
            issues.append(Issue(location,
                                f"truncate 分区值必须是整数，实际：{value!r}"))
            return
        if value % width != 0:
            issues.append(Issue(location,
                                f"truncate({width}) 分区值必须是 {width} 的整数倍，"
                                f"实际：{value}"))
            return
        # 原像 [value, value+W) 必须与 int64 范围有交集。
        if value > INT64_MAX or value + width - 1 < INT64_MIN:
            issues.append(Issue(location,
                                f"truncate({width}) 分区值 {value} 的原像与 int64 "
                                f"范围没有交集"))
        return
    if transform.kind == KIND_PREFIX:
        length = transform.param
        if not isinstance(value, str):
            issues.append(Issue(location,
                                f"prefix 分区值必须是字符串，实际：{value!r}"))
            return
        if len(value) > length:
            issues.append(Issue(location,
                                f"prefix({length}) 分区值最多 {length} 个码点，"
                                f"实际 {len(value)} 个"))


def preimage(transform: Transform, value: Any, *,
             source_type: str, source_nullable: bool) -> ColumnConstraint:
    """分区值的原像：能映到该值的所有源值集合（含 null 可能性）。"""
    if transform.kind == KIND_VOID:
        # void 的 null 不能推出源列为空：源值任意。
        return universe(source_type, source_nullable)
    if value is None:
        return null_only(source_type)
    if transform.kind == KIND_IDENTITY:
        return exact(source_type, value)
    if transform.kind == KIND_TRUNCATE:
        width = transform.param
        lo = max(value, INT64_MIN)
        hi = min(value + width, INT64_MAX + 1)
        return ColumnConstraint(TYPE_INT64, lo, hi, False)
    if transform.kind == KIND_PREFIX:
        length = transform.param
        if len(value) < length:
            # 短于 L：源值恰等于它（否则前缀会凑满 L 个码点）。
            return exact(TYPE_STRING, value)
        # 恰为 L：代表以该前缀开头的全部字符串。
        return ColumnConstraint(TYPE_STRING, value,
                                string_prefix_upper_bound(value), False)
    raise ValueError(f"未知变换：{transform.kind}")  # pragma: no cover


def compute_file_constraints(fields_by_id: dict, partition_fields,
                             partitions: dict) -> dict:
    """一个文件每列的取值约束：同源多个分区字段的原像求交集。

    ``partitions`` 以 partition_id（int）为键。返回 field_id -> ColumnConstraint，
    未被任何分区字段引用的列保持全域。
    """
    constraints = {fid: universe(f.type, f.nullable)
                   for fid, f in fields_by_id.items()}
    for pf in partition_fields:
        field = fields_by_id[pf.source_id]
        piece = preimage(pf.transform, partitions[pf.partition_id],
                         source_type=field.type,
                         source_nullable=field.nullable)
        constraints[pf.source_id] = intersect(constraints[pf.source_id], piece)
    return constraints
