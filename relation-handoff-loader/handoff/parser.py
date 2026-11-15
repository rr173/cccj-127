"""UTF-8 JSONL 交接文件的严格流式解析。

设计边界：

* 逐行读取，绝不把整个文件读进内存；
* 非法 UTF-8、空行、重复 JSON 成员、坏结构一律拒绝，诊断带文件与物理行号；
* 每行必须且只能有 ``key`` / ``values`` / ``refs`` 三个成员，字段名与引用名
  必须与契约齐全对应，禁止夹带额外字段；
* 自然键为有序非空文本元组，原样比较（``"007"`` 不等于 ``"7"``），不做任何
  分隔符拼接；
* bigint 只接受 JSON 整数且范围校验，bool 不允许冒充 bigint，全程不经浮点。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from .contract import Contract, RefSpec, TableSpec
from .errors import HandoffError

# 单批落库行数。Python 侧任何时刻只保留这一批 + 契约，不缓存全量表。
DEFAULT_BATCH_SIZE = 1000

# bigint 边界，显式按整数比较，不经过 float。
BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1


def _strict_json(line: str, file: str, line_no: int) -> Any:
    try:
        return json.loads(line, object_pairs_hook=_hook(file, line_no))
    except HandoffError:
        raise
    except json.JSONDecodeError as exc:
        raise HandoffError(
            "JSON 结构解析失败",
            file=file,
            line=line_no,
            detail=exc.msg,
        ) from exc


def _hook(file: str, line_no: int):
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise HandoffError(
                    "JSON 对象存在重复成员",
                    file=file,
                    line=line_no,
                    field=k,
                )
            out[k] = v
        return out

    return hook


def _check_text(value: Any, spec: TableSpec, file: str, line_no: int, field: str) -> str:
    if not isinstance(value, str):
        raise HandoffError(
            f"字段 {field!r} 必须是 text 字符串",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field=field,
            detail=type(value).__name__,
        )
    return value


def _check_bigint(value: Any, spec: TableSpec, file: str, line_no: int, field: str) -> int:
    # 必须是真正的 JSON 整数：bool 是 int 的子类，先排除；float 直接拒绝，
    # 避免大整数经过浮点转换丢精度。
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandoffError(
            f"字段 {field!r} 必须是 bigint 整数（禁止布尔或浮点）",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field=field,
            detail=value,
        )
    if value < BIGINT_MIN or value > BIGINT_MAX:
        raise HandoffError(
            f"字段 {field!r} 超出 bigint 范围",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field=field,
            detail=value,
        )
    return value


def _check_bool(value: Any, spec: TableSpec, file: str, line_no: int, field: str) -> bool:
    if not isinstance(value, bool):
        raise HandoffError(
            f"字段 {field!r} 必须是 boolean",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field=field,
            detail=type(value).__name__,
        )
    return value


def _check_value(
    value: Any, col: str, spec: TableSpec, file: str, line_no: int
) -> str | int | bool | None:
    typ = spec.columns[col]
    nullable = col in spec.nullable_columns
    if value is None:
        if not nullable:
            raise HandoffError(
                f"普通列 {col!r} 不允许 null",
                logical_table=spec.logical_name,
                file=file,
                line=line_no,
                field=col,
            )
        return None
    if typ == "text":
        return _check_text(value, spec, file, line_no, col)
    if typ == "bigint":
        return _check_bigint(value, spec, file, line_no, col)
    return _check_bool(value, spec, file, line_no, col)


def _check_ref(
    value: Any, ref: RefSpec, contract: Contract, file: str, line_no: int
) -> tuple[str, ...] | None:
    if value is None:
        if not ref.nullable:
            raise HandoffError(
                f"非空引用 {ref.name!r} 不允许 null，不能为破环把引用置空",
                file=file,
                line=line_no,
                field=ref.name,
            )
        return None
    if not isinstance(value, list):
        raise HandoffError(
            f"引用 {ref.name!r} 必须是目标自然键元组数组或 null",
            file=file,
            line=line_no,
            field=ref.name,
        )
    target = contract.table(ref.target_logical_table)
    if len(value) != len(target.natural_key):
        raise HandoffError(
            f"引用 {ref.name!r} 的自然键元组分量数应为 {len(target.natural_key)}，"
            f"实际 {len(value)}",
            file=file,
            line=line_no,
            field=ref.name,
        )
    out: list[str] = []
    for idx, component in enumerate(value):
        if not isinstance(component, str):
            raise HandoffError(
                f"引用 {ref.name!r} 的第 {idx + 1} 个自然键分量必须是 text",
                file=file,
                line=line_no,
                field=ref.name,
                detail=type(component).__name__,
            )
        if component == "":
            raise HandoffError(
                f"引用 {ref.name!r} 的自然键分量不允许空串（目标自然键非空）",
                file=file,
                line=line_no,
                field=ref.name,
            )
        out.append(component)
    return tuple(out)


class NormalizedRow:
    """一行交接数据的规范化结果（仅在批次内存活）。"""

    __slots__ = ("key", "values", "refs", "file", "line")

    def __init__(
        self,
        key: tuple[str, ...],
        values: dict[str, Any],
        refs: dict[str, tuple[str, ...] | None],
        file: str,
        line: int,
    ) -> None:
        self.key = key
        self.values = values
        self.refs = refs
        self.file = file
        self.line = line


def parse_row(obj: Any, spec: TableSpec, contract: Contract, file: str, line_no: int) -> NormalizedRow:
    """校验并规范化单个 JSON 对象。"""
    if not isinstance(obj, dict):
        raise HandoffError(
            "每行必须是 JSON 对象", logical_table=spec.logical_name, file=file, line=line_no
        )
    expected = {"key", "values", "refs"}
    got = set(obj)
    missing = expected - got
    extra = got - expected
    if missing:
        raise HandoffError(
            f"行缺少必需部分 {sorted(missing)}",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
        )
    if extra:
        raise HandoffError(
            f"行夹带未声明的额外字段 {sorted(extra)}，只允许 key/values/refs",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field=sorted(extra)[0],
        )

    raw_key = obj["key"]
    if not isinstance(raw_key, list) or len(raw_key) != len(spec.natural_key):
        raise HandoffError(
            f"key 必须是含 {len(spec.natural_key)} 个分量的有序数组",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field="key",
        )
    key: list[str] = []
    for idx, component in enumerate(raw_key):
        if not isinstance(component, str):
            raise HandoffError(
                f"自然键第 {idx + 1} 个分量必须是 text",
                logical_table=spec.logical_name,
                file=file,
                line=line_no,
                field=spec.natural_key[idx],
                detail=type(component).__name__,
            )
        if component == "":
            raise HandoffError(
                "自然键分量不允许空串（自然键为非空 text 列）",
                logical_table=spec.logical_name,
                file=file,
                line=line_no,
                field=spec.natural_key[idx],
            )
        key.append(component)

    raw_values = obj["values"]
    if not isinstance(raw_values, dict):
        raise HandoffError(
            "values 必须是对象",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field="values",
        )
    if set(raw_values) != set(spec.columns):
        raise HandoffError(
            "values 的列名必须与契约普通列完全齐全对应，不能缺漏或夹带",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field="values",
            detail={"expected": list(spec.columns), "got": sorted(raw_values)},
        )
    values = {
        col: _check_value(raw_values[col], col, spec, file, line_no)
        for col in spec.all_value_columns
    }

    raw_refs = obj["refs"]
    if not isinstance(raw_refs, dict):
        raise HandoffError(
            "refs 必须是对象",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field="refs",
        )
    if set(raw_refs) != {r.name for r in spec.refs}:
        raise HandoffError(
            "refs 的引用名必须与契约具名引用完全齐全对应，不能缺漏或夹带",
            logical_table=spec.logical_name,
            file=file,
            line=line_no,
            field="refs",
            detail={"expected": [r.name for r in spec.refs], "got": sorted(raw_refs)},
        )
    refs = {
        r.name: _check_ref(raw_refs[r.name], r, contract, file, line_no)
        for r in spec.refs
    }
    return NormalizedRow(tuple(key), values, refs, file, line_no)


def iter_jsonl(
    spec: TableSpec, contract: Contract, file_path: str | Path
) -> Iterator[NormalizedRow]:
    """逐行产出规范化行；物理行号对空行同样计数。"""
    file = str(file_path)
    fh: BinaryIO
    try:
        fh = open(file, "rb")  # noqa: SIM115 - 手动关闭以精确定位编码错误
    except OSError as exc:
        raise HandoffError(f"无法打开交接文件: {file}", detail=str(exc)) from exc
    with fh:
        for line_no, raw_bytes in enumerate(fh, start=1):
            try:
                line = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HandoffError(
                    "非法 UTF-8 字节序列",
                    logical_table=spec.logical_name,
                    file=file,
                    line=line_no,
                    detail=str(exc),
                ) from exc
            stripped = line.strip()
            if not stripped:
                raise HandoffError(
                    "不允许空行（含纯空白行）",
                    logical_table=spec.logical_name,
                    file=file,
                    line=line_no,
                )
            obj = _strict_json(stripped, file, line_no)
            yield parse_row(obj, spec, contract, file, line_no)


def iter_batches(
    spec: TableSpec,
    contract: Contract,
    file_paths: list[str] | tuple[str, ...],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[list[NormalizedRow]]:
    """把一张表的多个文件按顺序切成有界批次，调用方逐批落库。"""
    if batch_size <= 0:
        raise HandoffError("batch_size 必须为正整数")
    batch: list[NormalizedRow] = []
    for path in file_paths:
        for row in iter_jsonl(spec, contract, path):
            batch.append(row)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch
