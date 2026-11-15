"""装载契约的定义与解析。

契约是一份 JSON 文档，声明参与本次整表交接的全部关系表：

* ``schema`` / ``table``：目标物理表（标识符允许空格、引号、点、中文，按
  PostgreSQL 标识符规则引用，绝不与数据值拼接）；
* ``natural_key``：有序的一列或多列，全部为非空 ``text``，按原样文本比较
  （``"007"`` 与 ``"7"`` 不同）；
* ``columns``：普通值列，只允许 ``text`` / ``bigint`` / ``boolean`` 及
  各自的可空属性；
* ``refs``：具名引用，文件里给目标自然键元组或 ``null``，落库为 bigint 外键；
* ``unique_constraints``：自然键之外的额外业务唯一约束（有序列集合）；
* ``id_sequence``：为代理主键 ``id`` 分配新值的独立序列。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import HandoffError

SUPPORTED_TYPES = ("text", "bigint", "boolean")


@dataclass(frozen=True)
class RefSpec:
    """一条具名引用的声明。"""

    name: str
    column: str
    target_logical_table: str
    nullable: bool
    deferrable: bool
    deferred: bool

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise HandoffError("引用名必须是非空字符串", field=self.name)
        if not isinstance(self.column, str) or not self.column:
            raise HandoffError("引用外键列名必须是非空字符串", field=self.name)
        if not isinstance(self.target_logical_table, str) or not self.target_logical_table:
            raise HandoffError("引用目标逻辑表名必须是非空字符串", field=self.name)
        if not isinstance(self.nullable, bool):
            raise HandoffError("引用 nullable 必须是布尔值", field=self.name)
        if not isinstance(self.deferrable, bool) or not isinstance(self.deferred, bool):
            raise HandoffError("引用 deferrable/deferred 必须是布尔值", field=self.name)
        if self.deferred and not self.deferrable:
            raise HandoffError(
                "引用声明 INITIALLY DEFERRED 时必须同时 DEFERRABLE", field=self.name
            )


@dataclass(frozen=True)
class TableSpec:
    """一张参与表的契约声明。"""

    logical_name: str
    schema: str
    table: str
    natural_key: tuple[str, ...]
    columns: dict[str, str]  # 列名 -> 类型
    nullable_columns: frozenset[str]
    refs: tuple[RefSpec, ...]
    unique_constraints: tuple[tuple[str, ...], ...]
    id_sequence: str  # 序列名（与表同 schema）

    @property
    def all_value_columns(self) -> tuple[str, ...]:
        """契约声明的普通列，保持声明顺序。"""
        return tuple(self.columns)

    @property
    def ref_by_name(self) -> dict[str, RefSpec]:
        return {r.name: r for r in self.refs}

    @property
    def ref_columns(self) -> tuple[str, ...]:
        return tuple(r.column for r in self.refs)

    @property
    def all_declared_columns(self) -> tuple[str, ...]:
        return (*self.natural_key, *self.columns, *self.ref_columns)


@dataclass(frozen=True)
class Contract:
    """一次装载的完整契约。"""

    schema: str
    tables: tuple[TableSpec, ...]

    def table(self, logical_name: str) -> TableSpec:
        for t in self.tables:
            if t.logical_name == logical_name:
                return t
        raise HandoffError(f"契约中不存在逻辑表 {logical_name!r}", logical_table=logical_name)

    def has_table(self, logical_name: str) -> bool:
        return any(t.logical_name == logical_name for t in self.tables)

    @property
    def lock_order(self) -> tuple[TableSpec, ...]:
        """固定全局加锁顺序：物理 (schema, table) 字典序，与输入文件顺序无关。"""
        return tuple(sorted(self.tables, key=lambda t: (t.schema, t.table)))


def _require_str(obj: Any, key: str, ctx: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val:
        raise HandoffError(f"{ctx} 的 {key!r} 必须是非空字符串", field=key, detail=val)
    return val


def _parse_refs(raw: Any, ctx: str) -> tuple[RefSpec, ...]:
    if not isinstance(raw, list):
        raise HandoffError(f"{ctx} 的 refs 必须是数组", field="refs")
    seen: set[str] = set()
    out: list[RefSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HandoffError(f"{ctx} 的 refs 每项必须是对象", field="refs")
        name = _require_str(item, "name", ctx)
        if name in seen:
            raise HandoffError(f"{ctx} 引用名重复: {name!r}", field=name)
        seen.add(name)
        ref = RefSpec(
            name=name,
            column=_require_str(item, "column", ctx),
            target_logical_table=_require_str(item, "target", ctx),
            nullable=bool(item.get("nullable", False)),
            deferrable=bool(item.get("deferrable", False)),
            deferred=bool(item.get("initially_deferred", False)),
        )
        ref.validate()
        out.append(ref)
    return tuple(out)


def _parse_columns(raw: Any, ctx: str) -> tuple[dict[str, str], frozenset[str]]:
    if not isinstance(raw, list):
        raise HandoffError(f"{ctx} 的 columns 必须是数组", field="columns")
    cols: dict[str, str] = {}
    nullable: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise HandoffError(f"{ctx} 的 columns 每项必须是对象", field="columns")
        name = _require_str(item, "name", ctx)
        typ = _require_str(item, "type", ctx)
        if typ not in SUPPORTED_TYPES:
            raise HandoffError(
                f"{ctx} 普通列 {name!r} 类型 {typ!r} 不受支持，"
                f"仅支持 {SUPPORTED_TYPES}",
                field=name,
            )
        if name in cols:
            raise HandoffError(f"{ctx} 普通列重复声明: {name!r}", field=name)
        cols[name] = typ
        if bool(item.get("nullable", False)):
            nullable.add(name)
    return cols, frozenset(nullable)


def _parse_str_tuple(raw: Any, ctx: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or any(
        not isinstance(x, str) or not x for x in raw
    ):
        raise HandoffError(
            f"{ctx} 的 {field_name} 必须是非空字符串组成的非空有序数组",
            field=field_name,
        )
    return tuple(raw)


def parse_contract(doc: Any) -> Contract:
    """把已解码的 JSON 文档解析成 :class:`Contract`，并做自洽性检查。"""
    if not isinstance(doc, dict):
        raise HandoffError("契约文档顶层必须是对象")
    schema = _require_str(doc, "schema", "契约")
    raw_tables = doc.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise HandoffError("契约 tables 必须是非空数组", field="tables")

    tables: list[TableSpec] = []
    logical_seen: set[str] = set()
    physical_seen: set[tuple[str, str]] = set()
    column_seen_global: set[tuple[str, str]] = set()
    for raw in raw_tables:
        if not isinstance(raw, dict):
            raise HandoffError("契约 tables 每项必须是对象", field="tables")
        ctx = "契约表"
        logical = _require_str(raw, "logical_name", ctx)
        if logical in logical_seen:
            raise HandoffError(f"逻辑表名重复: {logical!r}", logical_table=logical)
        logical_seen.add(logical)
        tbl_schema = raw.get("schema", schema)
        if not isinstance(tbl_schema, str) or not tbl_schema:
            raise HandoffError("表 schema 必须是非空字符串", logical_table=logical)
        if tbl_schema != schema:
            raise HandoffError(
                "本工具一次只允许操作一个指定 schema，所有参与表必须位于该 schema",
                logical_table=logical,
            )
        table = _require_str(raw, "table", ctx)
        if (tbl_schema, table) in physical_seen:
            raise HandoffError(f"物理表重复声明: {tbl_schema}.{table}", logical_table=logical)
        physical_seen.add((tbl_schema, table))

        nk = _parse_str_tuple(raw.get("natural_key"), logical, "natural_key")
        cols, nullable = _parse_columns(raw.get("columns", []), logical)
        refs = _parse_refs(raw.get("refs", []), logical)
        uks_raw = raw.get("unique_constraints", [])
        if not isinstance(uks_raw, list):
            raise HandoffError("unique_constraints 必须是数组", logical_table=logical)
        uks = tuple(
            _parse_str_tuple(item, logical, "unique_constraints") for item in uks_raw
        )

        declared = set(nk) | set(cols) | {r.column for r in refs}
        if len(declared) != len(nk) + len(cols) + len(refs):
            raise HandoffError(
                "自然键列、普通列、引用外键列之间不得重名", logical_table=logical
            )
        if "id" in declared:
            raise HandoffError(
                "id 是代理主键保留列，不得在 natural_key/columns/refs 中声明",
                logical_table=logical,
            )
        for uk in (nk, *uks):
            bad = [c for c in uk if c not in declared]
            if bad:
                raise HandoffError(
                    f"唯一约束含未声明列 {bad}", logical_table=logical, field=str(uk)
                )
            if len(set(uk)) != len(uk):
                raise HandoffError("唯一约束列集合内存在重复列", logical_table=logical)
        id_sequence = raw.get("id_sequence")
        if not isinstance(id_sequence, str) or not id_sequence:
            raise HandoffError(
                "每张参与表必须显式声明独立的 id_sequence", logical_table=logical
            )

        spec = TableSpec(
            logical_name=logical,
            schema=tbl_schema,
            table=table,
            natural_key=nk,
            columns=cols,
            nullable_columns=nullable,
            refs=refs,
            unique_constraints=uks,
            id_sequence=id_sequence,
        )
        tables.append(spec)
        column_seen_global.add((tbl_schema, table))

    contract = Contract(schema=schema, tables=tuple(tables))

    # 引用只能落在声明的参与表上。
    for t in contract.tables:
        for r in t.refs:
            if not contract.has_table(r.target_logical_table):
                raise HandoffError(
                    f"引用 {r.name!r} 指向未参与装载的逻辑表 {r.target_logical_table!r}；"
                    "契约引用只能落到声明的参与表",
                    logical_table=t.logical_name,
                    field=r.name,
                )
    # 物理列名冲突（同一物理表两张逻辑表等）。
    phys: dict[tuple[str, str], str] = {}
    for t in contract.tables:
        key = (t.schema, t.table)
        if key in phys:
            raise HandoffError(f"物理表 {key} 被多个逻辑表声明")
        phys[key] = t.logical_name
    return contract


def load_contract(path: str | Path) -> Contract:
    """从磁盘读取契约 JSON（拒绝重复 JSON 成员）。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HandoffError(f"契约文件不是合法 UTF-8: {p}", detail=str(exc)) from exc
    except OSError as exc:
        raise HandoffError(f"无法读取契约文件: {p}", detail=str(exc)) from exc
    try:
        doc = _json_loads_strict(text, str(p))
    except HandoffError:
        raise
    return parse_contract(doc)


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook``：对象内重复键直接拒绝，不静默取后者。"""
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise HandoffError(f"JSON 对象存在重复成员 {k!r}", field=k)
        out[k] = v
    return out


def _json_loads_strict(text: str, source: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except HandoffError:
        raise
    except json.JSONDecodeError as exc:
        raise HandoffError(
            f"JSON 解析失败: {source}", line=exc.lineno, detail=exc.msg
        ) from exc
