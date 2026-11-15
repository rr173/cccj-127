"""目标数据库目录核对。

写入前，契约必须与真实的 PostgreSQL 目录（``pg_catalog``）逐项核对：
列名/类型/可空性、主键、自然键唯一约束、额外业务唯一约束、外键目标与
可延迟性都要匹配；目标表除 ``id`` 外只能含契约声明的列。

明确拒绝、绝不假装支持的布局：

* 自定义触发器、生成列、分区表/分区子表、继承、视图/外部表；
* 额外的 CHECK 约束、部分/表达式唯一索引、契约未声明的唯一约束；
* 非 NO ACTION 的外键动作（级联删除、置空、改值、RESTRICT 均拒绝）；
* 入向外键带 CASCADE / SET NULL / SET DEFAULT，防止未参与表被连带改动；
* 非独立序列、非 nextval 默认值、IDENTITY 列。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from .contract import Contract, TableSpec
from .errors import HandoffError

_PLAIN_COLUMN = ""
_ALLOWED_FK_ACTION = "a"  # pg_constraint.conf*type: a=NO ACTION
_PLAIN_RELKIND = ("r",)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    pg_type: str
    nullable: bool
    identity: str
    generated: str
    default: str | None


@dataclass(frozen=True)
class ForeignKeyInfo:
    name: str
    # 约束所在表（持有外键列的一方）
    own_schema: str
    own_table: str
    columns: tuple[str, ...]
    # 被引用表（父表）
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]
    update_action: str
    delete_action: str
    deferrable: bool
    deferred: bool


@dataclass(frozen=True)
class TableCatalog:
    oid: int
    columns: dict[str, ColumnInfo]
    primary_key: tuple[str, ...] | None
    unique_indexes: tuple[tuple[str, ...], ...]
    # 可延迟的唯一/排他约束名（含自然键与业务唯一约束），用于事务内统一延迟。
    deferrable_unique_constraints: tuple[str, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    incoming_foreign_keys: tuple[ForeignKeyInfo, ...]
    id_sequence_schema: str | None
    id_sequence_name: str | None


def verify_catalog(conn: Connection[Any], contract: Contract) -> dict[str, TableCatalog]:
    """在当前连接上核对全部参与表，返回每张表的目录快照（只读）。"""
    _check_schema_exists(conn, contract.schema)
    catalogs: dict[str, TableCatalog] = {}
    targets = {
        t.logical_name: (t.schema, t.table) for t in contract.tables
    }
    for spec in contract.tables:
        oid = _check_relation_shape(conn, spec)
        columns = _read_columns(conn, oid)
        _verify_columns(spec, columns)
        pk, unique_indexes, deferrable_uk = _read_indexes(conn, oid)
        _verify_primary_key(spec, pk)
        _verify_unique_constraints(spec, unique_indexes)
        fks, incoming = _read_foreign_keys(conn, oid)
        _verify_declared_foreign_keys(spec, fks, targets)
        _verify_incoming_foreign_keys(spec, incoming)
        seq = _read_id_sequence(conn, oid, columns["id"])
        _verify_id_sequence(spec, seq)
        catalogs[spec.logical_name] = TableCatalog(
            oid=oid,
            columns=columns,
            primary_key=pk,
            unique_indexes=unique_indexes,
            deferrable_unique_constraints=deferrable_uk,
            foreign_keys=fks,
            incoming_foreign_keys=incoming,
            id_sequence_schema=seq[0],
            id_sequence_name=seq[1],
        )
    return catalogs


def _check_schema_exists(conn: Connection[Any], schema: str) -> None:
    row = conn.execute(
        "SELECT oid FROM pg_namespace WHERE nspname = %s", (schema,)
    ).fetchone()
    if row is None:
        raise HandoffError(f"目标 schema {schema!r} 不存在", detail=schema)
    if schema in ("pg_catalog", "information_schema"):
        raise HandoffError(f"不允许操作系统 schema: {schema!r}")


def _check_relation_shape(conn: Connection[Any], spec: TableSpec) -> int:
    row = conn.execute(
        """
        SELECT c.oid, c.relkind, c.relispartition,
               EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid) AS is_child,
               EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhparent = c.oid) AS is_parent
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s
        """,
        (spec.schema, spec.table),
    ).fetchone()
    if row is None:
        raise HandoffError(
            f"目标表不存在: {spec.schema}.{spec.table}", logical_table=spec.logical_name
        )
    oid, relkind, is_partition, is_child, is_parent = row
    if relkind not in _PLAIN_RELKIND:
        raise HandoffError(
            f"目标对象不是普通表（relkind={relkind!r}），视图/外部表/分区表等均不支持",
            logical_table=spec.logical_name,
        )
    if is_partition or is_child:
        raise HandoffError(
            "目标表是分区或继承子表，本工具不支持分区/继承布局",
            logical_table=spec.logical_name,
        )
    if is_parent:
        raise HandoffError(
            "目标表存在继承子表，本工具不支持继承布局", logical_table=spec.logical_name
        )
    trig = conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal ORDER BY 1",
        (oid,),
    ).fetchall()
    if trig:
        raise HandoffError(
            f"目标表存在用户自定义触发器 {[r[0] for r in trig]}，明确不支持",
            logical_table=spec.logical_name,
        )
    rules = conn.execute(
        """
        SELECT r.rulename FROM pg_rewrite r
         WHERE r.ev_class = %s AND r.rulename <> '_RETURN'
         ORDER BY 1
        """,
        (oid,),
    ).fetchall()
    if rules:
        raise HandoffError(
            f"目标表存在自定义规则 {[r[0] for r in rules]}，明确不支持",
            logical_table=spec.logical_name,
        )
    checks = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s AND contype = 'c' ORDER BY 1",
        (oid,),
    ).fetchall()
    if checks:
        raise HandoffError(
            f"目标表存在额外 CHECK 约束 {[r[0] for r in checks]}，明确不支持",
            logical_table=spec.logical_name,
        )
    return int(oid)


def _read_columns(conn: Connection[Any], oid: int) -> dict[str, ColumnInfo]:
    rows = conn.execute(
        """
        SELECT a.attname,
               format_type(a.atttypid, a.atttypmod) AS pg_type,
               a.attnotnull, a.attidentity, a.attgenerated,
               pg_get_expr(ad.adbin, ad.adrelid) AS default_expr
          FROM pg_attribute a
          LEFT JOIN pg_attrdef ad
            ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
         WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (oid,),
    ).fetchall()
    out: dict[str, ColumnInfo] = {}
    for name, pg_type, not_null, identity, generated, default_expr in rows:
        out[name] = ColumnInfo(
            name=name,
            pg_type=pg_type,
            nullable=not not_null,
            identity=identity or _PLAIN_COLUMN,
            generated=generated or _PLAIN_COLUMN,
            default=default_expr,
        )
    return out


def _verify_columns(spec: TableSpec, columns: dict[str, ColumnInfo]) -> None:
    if "id" not in columns:
        raise HandoffError("目标表缺少代理主键列 id", logical_table=spec.logical_name)
    id_col = columns["id"]
    if id_col.pg_type != "bigint":
        raise HandoffError(
            f"id 列必须是 bigint，实际 {id_col.pg_type!r}",
            logical_table=spec.logical_name,
            field="id",
        )
    if id_col.nullable:
        raise HandoffError("id 列必须 NOT NULL", logical_table=spec.logical_name, field="id")
    if id_col.identity != _PLAIN_COLUMN:
        raise HandoffError(
            "不支持 IDENTITY 列，id 必须由独立序列的 nextval 默认值分配",
            logical_table=spec.logical_name,
        )
    expected = set(spec.all_declared_columns) | {"id"}
    actual = set(columns)
    if extra := actual - expected:
        raise HandoffError(
            f"目标表存在契约未声明的额外列 {sorted(extra)}",
            logical_table=spec.logical_name,
        )
    if missing := expected - actual:
        raise HandoffError(
            f"目标表缺少契约声明列 {sorted(missing)}", logical_table=spec.logical_name
        )
    for col in spec.all_value_columns:
        info = columns[col]
        if info.generated != _PLAIN_COLUMN or info.identity != _PLAIN_COLUMN:
            raise HandoffError(
                f"列 {col!r} 是生成列/标识列，不支持",
                logical_table=spec.logical_name,
                field=col,
            )
        want_type = spec.columns[col]
        if info.pg_type != want_type:
            raise HandoffError(
                f"列 {col!r} 类型不匹配：契约 {want_type}，目录 {info.pg_type}",
                logical_table=spec.logical_name,
                field=col,
            )
        want_nullable = col in spec.nullable_columns
        if info.nullable != want_nullable:
            raise HandoffError(
                f"列 {col!r} nullability 不匹配：契约 nullable={want_nullable}，"
                f"目录 nullable={info.nullable}",
                logical_table=spec.logical_name,
                field=col,
            )
    for col in spec.natural_key:
        info = columns[col]
        if info.pg_type != "text":
            raise HandoffError(
                f"自然键列 {col!r} 必须是 text，实际 {info.pg_type}",
                logical_table=spec.logical_name,
                field=col,
            )
        if info.nullable:
            raise HandoffError(
                f"自然键列 {col!r} 必须 NOT NULL",
                logical_table=spec.logical_name,
                field=col,
            )
    for ref in spec.refs:
        info = columns[ref.column]
        if info.pg_type != "bigint":
            raise HandoffError(
                f"外键列 {ref.column!r} 必须是 bigint，实际 {info.pg_type}",
                logical_table=spec.logical_name,
                field=ref.column,
            )
        if info.nullable != ref.nullable:
            raise HandoffError(
                f"外键列 {ref.column!r} nullability 与契约不符",
                logical_table=spec.logical_name,
                field=ref.column,
            )
        if info.generated != _PLAIN_COLUMN:
            raise HandoffError(
                f"外键列 {ref.column!r} 是生成列，不支持",
                logical_table=spec.logical_name,
                field=ref.column,
            )


def _read_indexes(
    conn: Connection[Any], oid: int
) -> tuple[
    tuple[str, ...] | None,
    tuple[tuple[str, ...], ...],
    tuple[str, ...],
]:
    rows = conn.execute(
        """
        SELECT i.indisprimary, i.indisunique,
               i.indpred IS NOT NULL AS is_partial,
               i.indexprs IS NOT NULL AS is_expression,
               array_agg(a.attname ORDER BY k.ord) AS cols
          FROM pg_index i
          JOIN pg_class idx ON idx.oid = i.indexrelid
          CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
         WHERE i.indrelid = %s AND idx.relkind = 'i'
         GROUP BY i.indexrelid, idx.relname, i.indisprimary, i.indisunique,
                  i.indpred, i.indexprs
         ORDER BY idx.relname
        """,
        (oid,),
    ).fetchall()
    pk: tuple[str, ...] | None = None
    uniques: list[tuple[str, ...]] = []
    for is_pk, is_unique, is_partial, is_expr, cols in rows:
        if is_partial or is_expr:
            raise HandoffError(
                "存在部分索引或表达式索引，明确不支持（含部分/表达式唯一索引）",
            )
        if is_pk:
            pk = tuple(cols)
        elif is_unique:
            uniques.append(tuple(cols))
    # 可延迟的唯一/排他约束（由 pg_constraint 支撑的命名约束），供事务内延迟。
    deferrable_uk = tuple(
        r[0]
        for r in conn.execute(
            """
            SELECT conname FROM pg_constraint
             WHERE conrelid = %s AND contype IN ('u', 'x') AND condeferrable
             ORDER BY conname
            """,
            (oid,),
        ).fetchall()
    )
    return pk, tuple(uniques), deferrable_uk


def _verify_primary_key(spec: TableSpec, pk: tuple[str, ...] | None) -> None:
    if pk != ("id",):
        raise HandoffError(
            f"主键必须恰好是 (id)，目录主键为 {pk}", logical_table=spec.logical_name
        )


def _verify_unique_constraints(
    spec: TableSpec, unique_indexes: tuple[tuple[str, ...], ...]
) -> None:
    declared = {spec.natural_key, *spec.unique_constraints}
    actual = set(unique_indexes)
    if missing := [list(c) for c in declared - actual]:
        raise HandoffError(
            f"库内缺少契约声明的普通 UNIQUE 约束/索引 {missing}",
            logical_table=spec.logical_name,
        )
    if extra := [list(c) for c in actual - declared]:
        raise HandoffError(
            f"库内存在契约未声明的唯一约束/索引 {extra}，不接受与声明不同的库内约束",
            logical_table=spec.logical_name,
        )


_FK_SQL = """
SELECT con.conname,
       on_ns.nspname AS own_schema, on_cl.relname AS own_table,
       con.condeferrable, con.condeferred,
       con.confupdtype, con.confdeltype,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
       ) AS cols,
       fn.nspname AS ref_schema, fc.relname AS ref_table,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
       ) AS ref_cols,
       con.conrelid = %s AS outgoing
  FROM pg_constraint con
  JOIN pg_class on_cl ON on_cl.oid = con.conrelid
  JOIN pg_namespace on_ns ON on_ns.oid = on_cl.relnamespace
  JOIN pg_class fc ON fc.oid = con.confrelid
  JOIN pg_namespace fn ON fn.oid = fc.relnamespace
 WHERE con.contype = 'f'
   AND (con.conrelid = %s OR con.confrelid = %s)
 ORDER BY con.conname
"""


def _read_foreign_keys(
    conn: Connection[Any], oid: int
) -> tuple[tuple[ForeignKeyInfo, ...], tuple[ForeignKeyInfo, ...]]:
    rows = conn.execute(_FK_SQL, (oid, oid, oid)).fetchall()
    outgoing: list[ForeignKeyInfo] = []
    incoming: list[ForeignKeyInfo] = []
    for (
        name,
        own_schema,
        own_table,
        deferrable,
        deferred,
        upd,
        dele,
        cols,
        ref_schema,
        ref_table,
        ref_cols,
        is_outgoing,
    ) in rows:
        info = ForeignKeyInfo(
            name=name,
            own_schema=own_schema,
            own_table=own_table,
            columns=tuple(cols),
            ref_schema=ref_schema,
            ref_table=ref_table,
            ref_columns=tuple(ref_cols),
            update_action=upd,
            delete_action=dele,
            deferrable=bool(deferrable),
            deferred=bool(deferred),
        )
        (outgoing if is_outgoing else incoming).append(info)
    return tuple(outgoing), tuple(incoming)


def _verify_declared_foreign_keys(
    spec: TableSpec,
    fks: tuple[ForeignKeyInfo, ...],
    targets: dict[str, tuple[str, str]],
) -> None:
    by_col: dict[str, ForeignKeyInfo] = {}
    for fk in fks:
        if len(fk.columns) != 1 or len(fk.ref_columns) != 1:
            raise HandoffError(
                f"外键 {fk.name!r} 必须是单列 bigint -> id，实际列 {fk.columns}",
                logical_table=spec.logical_name,
            )
        if fk.ref_columns != ("id",):
            raise HandoffError(
                f"外键 {fk.name!r} 必须引用目标表 id，实际引用 {fk.ref_columns}",
                logical_table=spec.logical_name,
            )
        if fk.update_action != _ALLOWED_FK_ACTION or fk.delete_action != _ALLOWED_FK_ACTION:
            raise HandoffError(
                f"外键 {fk.name!r} 只允许 NO ACTION（含可延迟），发现 "
                f"ON UPDATE={fk.update_action!r} ON DELETE={fk.delete_action!r}；"
                "级联/置空/改值/RESTRICT 均拒绝",
                logical_table=spec.logical_name,
            )
        by_col[fk.columns[0]] = fk
    declared = {r.column: r for r in spec.refs}
    if set(by_col) != set(declared):
        raise HandoffError(
            f"库内外键列 {sorted(by_col)} 与契约引用列 {sorted(declared)} 不一致",
            logical_table=spec.logical_name,
        )
    for col, fk in by_col.items():
        ref = declared[col]
        want_target = targets[ref.target_logical_table]
        if (fk.ref_schema, fk.ref_table) != want_target:
            raise HandoffError(
                f"外键列 {col!r} 目标表不匹配：契约 {want_target}，目录 "
                f"{(fk.ref_schema, fk.ref_table)}",
                logical_table=spec.logical_name,
                field=col,
            )
        if fk.deferrable != ref.deferrable or fk.deferred != ref.deferred:
            raise HandoffError(
                f"外键列 {col!r} 可延迟属性不匹配：契约 deferrable={ref.deferrable} "
                f"deferred={ref.deferred}，目录 deferrable={fk.deferrable} "
                f"deferred={fk.deferred}",
                logical_table=spec.logical_name,
                field=col,
            )


def _verify_incoming_foreign_keys(
    spec: TableSpec, incoming: tuple[ForeignKeyInfo, ...]
) -> None:
    for fk in incoming:
        if fk.update_action != _ALLOWED_FK_ACTION or fk.delete_action != _ALLOWED_FK_ACTION:
            raise HandoffError(
                f"入向外键 {fk.name!r}（{fk.ref_schema}.{fk.ref_table} -> 本表）"
                f"带 ON UPDATE={fk.update_action!r}/ON DELETE={fk.delete_action!r}；"
                "级联删除、置空或改值会让未参与表跟着变化，拒绝该目标布局",
                logical_table=spec.logical_name,
            )


def _read_id_sequence(
    conn: Connection[Any], oid: int, id_col: ColumnInfo
) -> tuple[str | None, str | None]:
    if not id_col.default:
        return None, None
    row = conn.execute(
        """
        SELECT n.nspname, s.relname
          FROM pg_attrdef ad
          JOIN pg_depend d ON d.objid = ad.oid AND d.deptype = 'n'
          JOIN pg_class s ON s.oid = d.refobjid
          JOIN pg_namespace n ON n.oid = s.relnamespace
         WHERE ad.adrelid = %s
           AND ad.adnum = (SELECT attnum FROM pg_attribute
                            WHERE attrelid = %s AND attname = 'id')
           AND s.relkind = 'S'
        """,
        (oid, oid),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _verify_id_sequence(spec: TableSpec, seq: tuple[str | None, str | None]) -> None:
    seq_schema, seq_name = seq
    if seq_schema is None or seq_name is None:
        raise HandoffError(
            "id 列缺少 nextval(...) 默认表达式或其序列不是独立序列对象",
            logical_table=spec.logical_name,
            field="id",
        )
    if seq_name != spec.id_sequence:
        raise HandoffError(
            f"id 序列不匹配：契约 {spec.id_sequence!r}，目录 {seq_schema}.{seq_name}",
            logical_table=spec.logical_name,
            field="id_sequence",
        )
    if seq_schema != spec.schema:
        raise HandoffError(
            f"id 序列必须位于目标 schema {spec.schema!r} 内，实际 {seq_schema!r}",
            logical_table=spec.logical_name,
        )
