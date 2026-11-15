"""整表关系装载的事务编排（产品主入口）。

一次 ``load`` 对所有参与表的插入、更新、删除、引用重接是同一笔事务：

1. 固定全局顺序（物理 schema/table 字典序）逐表取事务级
   ``SHARE ROW EXCLUSIVE`` 表锁，锁住后才读旧状态；同库两次装载因此严格
   串行，不可能各读一份旧状态后互相覆盖；
2. 契约与真实目录逐项核对（见 :mod:`handoff.catalog`）；
3. 参与表的可延迟约束在事务内统一置为延迟，约束在提交前由数据库真正核验；
4. JSONL 逐行解析、按有界批次写入临时表（重状态落库，不入 Python 字典）；
5. 在最终集合上查重、验唯一约束、定身份、解析引用（含非空循环）、算差分；
6. 先显式检查外部事实表引用，再由数据库外键做最终防线，提交或整体回滚。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import Connection
from psycopg.sql import SQL, Identifier

from . import catalog as catalog_mod
from . import staging
from .contract import Contract, TableSpec
from .errors import HandoffError
from .parser import DEFAULT_BATCH_SIZE, iter_batches


@dataclass
class TableReport:
    logical_table: str
    schema_table: str
    inserted: int
    updated: int
    deleted: int
    unchanged: int


@dataclass
class LoadReport:
    schema: str
    tables: dict[str, TableReport] = field(default_factory=dict)
    committed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "committed": self.committed,
            "tables": {
                name: {
                    "target": r.schema_table,
                    "inserted": r.inserted,
                    "updated": r.updated,
                    "deleted": r.deleted,
                    "unchanged": r.unchanged,
                }
                for name, r in self.tables.items()
            },
        }


def load_relation_handoff(
    conn: Connection[Any],
    contract: Contract,
    files: dict[str, Iterable[str | Path]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> LoadReport:
    """把一次多表交接写进已有仓库。

    :param conn: 已打开的 psycopg 连接（普通会话角色，禁止 superuser/复制角色）。
    :param contract: 已解析并自洽的装载契约。
    :param files: 逻辑表名 -> 该表的一个或多个 JSONL 文件路径。
    :returns: 以数据库实际提交结果为准的逐表四类计数报告。
    :raises HandoffError: 任何输入/目录/引用/约束/外部引用错误，全部改动回滚。
    """
    _validate_file_map(contract, files)
    _reject_privileged_roles(conn)

    if conn.autocommit:
        raise HandoffError(
            "连接不能处于 autocommit 模式：整次装载必须包在一笔事务里"
        )

    report = LoadReport(schema=contract.schema)
    # psycopg 非 autocommit 模式下首条语句即开启事务；这里显式负责提交与回滚，
    # 确保任何失败都主动 ROLLBACK，不留半成品状态。
    try:
        _run_load(conn, contract, files, batch_size, report)
        # 提交点：数据库在此真正核验全部可延迟约束。
        conn.execute("COMMIT")
        report.committed = True
        return report
    except HandoffError:
        conn.execute("ROLLBACK")
        raise
    except psycopg.Error as exc:
        conn.execute("ROLLBACK")
        ctx = ""
        if getattr(exc, "cursor", None) is not None and exc.cursor.query is not None:
            ctx = f"；出错语句: {exc.cursor.query.decode('utf-8', 'replace')[:500]}"
        raise HandoffError(
            f"数据库写入或约束核验失败，整次装载已回滚: "
            f"{exc.diag.message_primary or str(exc)}{ctx}",
            detail=exc.sqlstate,
        ) from exc


def _run_load(
    conn: Connection[Any],
    contract: Contract,
    files: dict[str, Iterable[Any]],
    batch_size: int,
    report: LoadReport,
) -> None:
    _lock_tables_in_order(conn, contract)
    catalogs = catalog_mod.verify_catalog(conn, contract)
    # 先限定 search_path，再 SET CONSTRAINTS ALL：否则 ALL 无法解析目标 schema 下
    # 的约束名，延迟会静默落空。
    _set_search_path(conn, contract.schema)
    _defer_constraints(conn, contract, catalogs)

    specs_by_name = {t.logical_name: t for t in contract.tables}
    for spec in contract.tables:
        staging.create_stage_tables(conn, spec)
        staging.create_plan_table(conn, spec)
        _ingest_files(conn, spec, contract, [Path(p) for p in files[spec.logical_name]],
                      batch_size)
        _reject_duplicate_natural_keys(conn, spec)
        _reject_stage_unique_violations(conn, spec)

    for spec in contract.tables:
        cat = catalogs[spec.logical_name]
        staging.build_plan(conn, spec, cat.id_sequence_name)

    for spec in contract.tables:
        staging.resolve_refs(conn, spec, specs_by_name)

    _check_external_fact_references(conn, contract, catalogs)

    for spec in contract.tables:
        staging.mark_unchanged(conn, spec)

    # 全局分阶段应用，避免跨表暂态外键冲突：
    # 先删所有参与表的废弃身份，再插入所有新身份，最后更新保留身份。
    # 约束已在本事务延迟，提交点由数据库统一核验最终关系。
    counts_by_table: dict[str, dict[str, int]] = {}
    for spec in contract.lock_order:
        counts_by_table[spec.logical_name] = staging.apply_deletes(conn, spec)
    for spec in contract.lock_order:
        counts_by_table[spec.logical_name].update(staging.apply_inserts(conn, spec))
    for spec in contract.lock_order:
        counts_by_table[spec.logical_name].update(staging.apply_updates(conn, spec))
    for spec in contract.lock_order:
        counts = counts_by_table[spec.logical_name]
        counts["unchanged"] = staging.count_unchanged(conn, spec)
        report.tables[spec.logical_name] = TableReport(
            logical_table=spec.logical_name,
            schema_table=f"{spec.schema}.{spec.table}",
            **counts,
        )
    # 可延迟外键与唯一约束在上面的 COMMIT 提交点由数据库真正核验；任何约束失败
    # 都会被外层捕获、整体回滚并以 HandoffError 重新抛出。


def _validate_file_map(contract: Contract, files: dict[str, Iterable[Any]]) -> None:
    if not isinstance(files, dict):
        raise HandoffError("files 必须是 逻辑表名 -> 文件路径列表 的映射")
    for spec in contract.tables:
        if spec.logical_name not in files:
            raise HandoffError(
                f"逻辑表 {spec.logical_name!r} 没有提供任何交接文件；"
                "空表交接也要显式给空文件",
                logical_table=spec.logical_name,
            )
        paths = list(files[spec.logical_name])
        if not paths:
            raise HandoffError(
                f"逻辑表 {spec.logical_name!r} 文件列表为空；交成空表请提供空文件",
                logical_table=spec.logical_name,
            )
        for p in paths:
            if not Path(p).is_file():
                raise HandoffError(
                    f"交接文件不存在或不是普通文件: {p}",
                    logical_table=spec.logical_name,
                    file=str(p),
                )
    extra = set(files) - {t.logical_name for t in contract.tables}
    if extra:
        raise HandoffError(f"files 出现契约未声明的逻辑表: {sorted(extra)}")


def _reject_privileged_roles(conn: Connection[Any]) -> None:
    row = conn.execute(
        "SELECT rolsuper, rolreplication FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None:
        raise HandoffError("无法确定当前数据库角色")
    is_super, is_replication = row
    if is_super or is_replication:
        raise HandoffError(
            "禁止使用 superuser 或复制角色执行装载，拒绝以特权绕过检查"
        )


def _lock_tables_in_order(conn: Connection[Any], contract: Contract) -> None:
    # 固定全局顺序加事务级表锁，足以排除其他写者，且不会死锁。
    for spec in contract.lock_order:
        conn.execute(
            SQL("LOCK TABLE {tbl} IN SHARE ROW EXCLUSIVE MODE").format(
                tbl=Identifier(spec.schema, spec.table)
            )
        )


def _defer_constraints(
    conn: Connection[Any],
    contract: Contract,
    catalogs: dict[str, catalog_mod.TableCatalog],
) -> None:
    # 显式列出参与 schema 内全部可延迟约束（出向/入向外键 + 自然键/业务唯一约束），
    # 用 schema 限定名设置，不依赖 search_path 的隐式解析。id 主键不可延迟，保持
    # 即时；不修改任何约束定义，只调整本事务的检查时机。其他 schema 不在列表中。
    names: list[SQL] = []
    seen: set[tuple[str, str]] = set()
    for spec in contract.tables:
        cat = catalogs[spec.logical_name]
        for fk in (*cat.foreign_keys, *cat.incoming_foreign_keys):
            if fk.deferrable and (fk.own_schema, fk.name) not in seen:
                seen.add((fk.own_schema, fk.name))
                names.append(Identifier(fk.own_schema, fk.name))
        for con_name in cat.deferrable_unique_constraints:
            if (spec.schema, con_name) not in seen:
                seen.add((spec.schema, con_name))
                names.append(Identifier(spec.schema, con_name))
    if names:
        conn.execute(
            SQL("SET CONSTRAINTS {all} DEFERRED").format(all=SQL(", ").join(names))
        )


def _set_search_path(conn: Connection[Any], schema: str) -> None:
    # 只允许操作指定 schema；pg_temp 保留给临时表，不暴露任何其他 schema。
    conn.execute(
        SQL("SET search_path TO {schema}, pg_temp").format(schema=Identifier(schema))
    )


def _ingest_files(
    conn: Connection[Any],
    spec: TableSpec,
    contract: Contract,
    paths: list[Path],
    batch_size: int,
) -> None:
    stage_sql = staging.stage_insert_sql(spec)
    ref_sql = staging.ref_insert_sql(spec)
    for batch in iter_batches(spec, contract, [str(p) for p in paths], batch_size):
        stage_args: list[tuple[Any, ...]] = []
        for row in batch:
            stage_args.append(
                (
                    *row.key,
                    *(row.values[c] for c in spec.all_value_columns),
                    row.file,
                    row.line,
                )
            )
        # 逐行插入暂存表并取回 row_no（批次有界；psycopg3 的 executemany
        # 对 RETURNING 的支持依实现而异，显式逐行取回保证与输入严格一一对应）。
        row_nos: list[int] = []
        with conn.cursor() as cur:
            for args in stage_args:
                cur.execute(stage_sql, args)
                row = cur.fetchone()
                if row is None:
                    raise HandoffError(
                        "暂存插入未取回 row_no（内部错误）",
                        logical_table=spec.logical_name,
                    )
                row_nos.append(row[0])
        if len(row_nos) != len(batch):
            raise HandoffError(
                "暂存 row_no 取回数量与批次行数不一致（内部错误）",
                logical_table=spec.logical_name,
            )
        ref_args = [
            (
                row_no,
                *(
                    list(row.refs[r.name]) if row.refs[r.name] is not None else None
                    for r in spec.refs
                ),
            )
            for row_no, row in zip(row_nos, batch)
        ]
        if spec.refs:
            with conn.cursor() as cur:
                cur.executemany(ref_sql, ref_args)


def _reject_duplicate_natural_keys(conn: Connection[Any], spec: TableSpec) -> None:
    row = conn.execute(staging.duplicate_natural_key_query(spec)).fetchone()
    if row is not None:
        src_file, src_line = row
        raise HandoffError(
            "表内相同自然键重复出现，整次拒绝（内容一样也不合并）",
            logical_table=spec.logical_name,
            file=src_file,
            line=src_line,
            field="key",
        )


def _reject_stage_unique_violations(conn: Connection[Any], spec: TableSpec) -> None:
    for uk in (spec.natural_key, *spec.unique_constraints):
        row = conn.execute(staging.stage_unique_violation_query(spec, uk)).fetchone()
        if row is not None:
            src_file, src_line = row
            raise HandoffError(
                f"交接数据违反业务唯一约束 {list(uk)}（含自然键），整次拒绝",
                logical_table=spec.logical_name,
                file=src_file,
                line=src_line,
                field=str(list(uk)),
            )


def _check_external_fact_references(
    conn: Connection[Any],
    contract: Contract,
    catalogs: dict[str, catalog_mod.TableCatalog],
) -> None:
    """删除前显式核查：未参与表是否仍引用即将删除的 id。

    数据库外键（NO ACTION，提交时核验）是最终防线；这里提前给出带定位的清晰
    诊断，且绝不级联删除事实数据。参与表之间的引用由各自计划与提交时约束核验。
    """
    participating = {(t.schema, t.table) for t in contract.tables}
    for spec in contract.tables:
        cat = catalogs[spec.logical_name]
        plan = Identifier(staging.plan_name(spec))
        for fk in cat.incoming_foreign_keys:
            if (fk.own_schema, fk.own_table) in participating:
                continue  # 引用方也是参与表，交给其计划与数据库约束
            child = Identifier(fk.own_schema, fk.own_table)
            fk_col = Identifier(fk.columns[0])
            row = conn.execute(
                SQL(
                    "SELECT c.{fk_col} FROM {child} c "
                    "WHERE c.{fk_col} IN "
                    "(SELECT id FROM {plan} WHERE action = 'delete') LIMIT 1"
                ).format(child=child, fk_col=fk_col, plan=plan)
            ).fetchone()
            if row is not None:
                raise HandoffError(
                    f"无法删除被外部表 {fk.own_schema}.{fk.own_table} 通过外键 "
                    f"{fk.name!r} 引用的身份 id={row[0]}；被事实引用的身份删除将"
                    "整次拒绝，不能级联删事实",
                    logical_table=spec.logical_name,
                    field=fk.columns[0],
                )
