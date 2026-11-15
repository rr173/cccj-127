"""事务内暂存与应用 SQL 的构建。

所有重状态都放在数据库临时表里（可溢出到磁盘），Python 侧只保留契约和
有界批次，不把全量表或循环依赖图载入字典。临时表在事务提交时自动销毁
（``ON COMMIT DROP``）。

标识符一律走 :class:`psycopg.sql.Identifier` 引用，数据值全部走绑定参数，
数据永远不会被拼成 SQL 片段。
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.sql import SQL, Identifier

from .contract import TableSpec


def qtable(spec: TableSpec) -> Identifier:
    return Identifier(spec.schema, spec.table)


def _safe(logical_name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in logical_name)


def _comma_join(parts: list[SQL]) -> SQL:
    """拼接列定义/列名片段，自动跳过空片段，避免无引用/无普通列时出现尾逗号。"""
    return SQL(", ").join(p for p in parts if p.as_string(None).strip())


def stage_name(spec: TableSpec) -> str:
    return f"_stage_{_safe(spec.logical_name)}"


def ref_stage_name(spec: TableSpec) -> str:
    return f"_stageref_{_safe(spec.logical_name)}"


def plan_name(spec: TableSpec) -> str:
    return f"_plan_{_safe(spec.logical_name)}"


def stage_nk_expr(spec: TableSpec) -> SQL:
    """暂存/计划表自然键元组表达式（text[]，原样保留每个分量，不做拼接）。"""
    parts = [SQL("nk{}").format(SQL(str(i))) for i in range(len(spec.natural_key))]
    return SQL("ARRAY[{}]::text[]").format(SQL(", ").join(parts))


def target_nk_expr(spec: TableSpec, alias: str) -> SQL:
    # alias 是本工具生成的内部别名（如 s/t/tp），不是用户标识符，按普通名拼接。
    parts = [
        SQL("{}.{}").format(SQL(alias), Identifier(c)) for c in spec.natural_key
    ]
    return SQL("ARRAY[{}]::text[]").format(SQL(", ").join(parts))


def create_stage_tables(conn: Connection[Any], spec: TableSpec) -> None:
    """创建行暂存表与引用暂存表（临时表，事务结束自动删除）。"""
    stage = Identifier(stage_name(spec))
    ref = Identifier(ref_stage_name(spec))
    nk_cols = SQL(", ").join(
        SQL("nk{} text NOT NULL").format(SQL(str(i))) for i in range(len(spec.natural_key))
    )
    val_cols = SQL(", ").join(
        SQL("{} {}").format(Identifier(c), SQL(spec.columns[c])) for c in spec.columns
    )
    stage_parts = _comma_join([
        SQL("row_no bigserial PRIMARY KEY"),
        nk_cols,
        val_cols,
        SQL("src_file text NOT NULL"),
        SQL("src_line integer NOT NULL"),
    ])
    conn.execute(
        SQL("CREATE TEMP TABLE {stage} ({parts}) ON COMMIT DROP").format(
            stage=stage, parts=stage_parts
        )
    )
    ref_col_defs = SQL(", ").join(
        SQL("{} text[]").format(Identifier(r.name)) for r in spec.refs
    )
    inner = SQL("row_no bigint PRIMARY KEY REFERENCES {stage}(row_no)").format(stage=stage)
    if ref_col_defs.as_string(None).strip():
        inner = SQL("{inner}, {ref_cols}").format(inner=inner, ref_cols=ref_col_defs)
    conn.execute(
        SQL("CREATE TEMP TABLE {ref} ({inner}) ON COMMIT DROP").format(
            stage=stage, ref=ref, inner=inner
        )
    )


def stage_insert_sql(spec: TableSpec) -> SQL:
    stage = Identifier(stage_name(spec))
    nk = SQL(", ").join(SQL("nk{}").format(SQL(str(i))) for i in range(len(spec.natural_key)))
    vals = SQL(", ").join(Identifier(c) for c in spec.columns)
    cols = _comma_join([nk, vals, Identifier("src_file"), Identifier("src_line")])
    params = SQL(", ").join(
        SQL("%s") for _ in range(len(spec.natural_key) + len(spec.columns) + 2)
    )
    return SQL("INSERT INTO {stage} ({cols}) VALUES ({params}) RETURNING row_no").format(
        stage=stage, cols=cols, params=params
    )


def ref_insert_sql(spec: TableSpec) -> SQL:
    ref = Identifier(ref_stage_name(spec))
    cols = SQL(", ").join(
        [Identifier("row_no"), *[Identifier(r.name) for r in spec.refs]]
    )
    params = SQL(", ").join(SQL("%s") for _ in range(len(spec.refs) + 1))
    return SQL("INSERT INTO {ref} ({cols}) VALUES ({params})").format(
        ref=ref, cols=cols, params=params
    )


def create_plan_table(conn: Connection[Any], spec: TableSpec) -> None:
    """计划表：最终行的身份（id）、动作与全部落库值，引用列先置空后回填。"""
    plan = Identifier(plan_name(spec))
    val_cols = SQL(", ").join(
        SQL("{} {}").format(Identifier(c), SQL(spec.columns[c])) for c in spec.columns
    )
    ref_cols = SQL(", ").join(
        SQL("{} bigint").format(Identifier(r.column)) for r in spec.refs
    )
    plan_parts = _comma_join([
        SQL("row_no bigint"),  # delete 行没有暂存行，故可空；其余动作非空
        SQL("id bigint"),
        SQL("nk text[] NOT NULL"),
        SQL("action text NOT NULL CHECK (action IN "
            "('insert','update','unchanged','delete'))"),
        val_cols,
        ref_cols,
        SQL("src_file text"),
        SQL("src_line integer"),
    ])
    conn.execute(
        SQL("CREATE TEMP TABLE {plan} ({parts}) ON COMMIT DROP").format(
            plan=plan, parts=plan_parts
        )
    )
    # row_no 对暂存行唯一；delete 行为 NULL，PostgreSQL 唯一索引允许多个 NULL。
    conn.execute(
        SQL("CREATE UNIQUE INDEX ON {plan} (row_no) WHERE row_no IS NOT NULL").format(
            plan=plan
        )
    )


def build_plan(conn: Connection[Any], spec: TableSpec, seq_name: str) -> None:
    """根据暂存最终集合与目标旧状态确定每行身份与动作。

    * 自然键仍存在 -> 保留原 id（先标 update，之后再判 unchanged）；
    * 新自然键 -> 取独立序列新 id；
    * 目标里有、暂存里没有 -> delete。
    """
    plan = Identifier(plan_name(spec))
    stage = Identifier(stage_name(spec))
    target = qtable(spec)
    nk_stage = stage_nk_expr(spec)
    nk_target = target_nk_expr(spec, "t")
    value_cols = tuple(spec.columns)
    ref_cols = tuple(r.column for r in spec.refs)

    val_insert = SQL(", ").join(Identifier(c) for c in value_cols)
    val_select = SQL(", ").join(
        SQL("s.{}").format(Identifier(c)) for c in value_cols
    )
    ref_decl = SQL(", ").join(Identifier(c) for c in ref_cols)
    ref_nulls = SQL(", ").join(
        SQL("NULL::bigint AS {}").format(Identifier(c)) for c in ref_cols
    )
    # 无普通列/无引用列时不能留下空片段或孤立逗号。
    head_insert = _comma_join([
        SQL("row_no, id, nk, action"),
        val_insert,
        ref_decl,
        SQL("src_file, src_line"),
    ])
    head_update = _comma_join([
        SQL("s.row_no, t.id, {nk}, 'update'").format(nk=nk_stage),
        val_select,
        ref_nulls,
        SQL("s.src_file, s.src_line"),
    ])
    # 序列名可能含空格/引号/中文：用连接的引用器生成 schema 限定的安全字符串，
    # 作为文本参数绑定后再转 regclass；数据值始终走参数，不拼 SQL 片段。
    qualified_seq = Identifier(spec.schema, seq_name).as_string(conn)
    head_insert_new = _comma_join([
        SQL("s.row_no, nextval(%s::regclass), {nk}, 'insert'").format(nk=nk_stage),
        val_select,
        ref_nulls,
        SQL("s.src_file, s.src_line"),
    ])

    # 1) 暂存行能在目标找到同自然键 -> 保留身份。
    conn.execute(
        SQL(
            "INSERT INTO {plan} ({head_insert}) "
            "SELECT {head_update} "
            "FROM {stage} s JOIN {target} t ON {nk_stage} = {nk_target}"
        ).format(
            plan=plan, stage=stage, target=target,
            nk_stage=nk_stage, nk_target=nk_target,
            head_insert=head_insert, head_update=head_update,
        )
    )
    # 2) 新自然键 -> 独立序列取号（序列空洞允许，不承诺回滚序列）。
    conn.execute(
        SQL(
            "INSERT INTO {plan} ({head_insert}) "
            "SELECT {head_insert_new} "
            "FROM {stage} s "
            "WHERE NOT EXISTS (SELECT 1 FROM {target} t WHERE {nk_stage} = {nk_target})"
        ).format(
            plan=plan, stage=stage, target=target,
            nk_stage=nk_stage, nk_target=nk_target,
            head_insert=head_insert, head_insert_new=head_insert_new,
        ),
        (qualified_seq,),
    )
    # 3) 目标里未被本次交接覆盖的旧自然键 -> delete。
    conn.execute(
        SQL(
            "INSERT INTO {plan} (row_no, id, nk, action) "
            "SELECT NULL, t.id, {nk_target}, 'delete' FROM {target} t "
            "WHERE NOT EXISTS (SELECT 1 FROM {stage} s WHERE {nk_stage} = {nk_target})"
        ).format(
            plan=plan, stage=stage, target=target,
            nk_stage=nk_stage, nk_target=nk_target,
        )
    )


def resolve_refs(
    conn: Connection[Any],
    spec: TableSpec,
    all_specs: dict[str, TableSpec],
) -> None:
    """按每条引用的目标自然键，在本次交接的最终集合里解析 id。

    只连目标表的计划表（最终集合），绝不连目标旧表，因此即将删除的旧行
    不可能充当父记录。一次性集合解析，无需拓扑排序，天然支持非空循环。
    """
    plan = Identifier(plan_name(spec))
    refstage = Identifier(ref_stage_name(spec))
    stage = Identifier(stage_name(spec))
    for ref in spec.refs:
        target_spec = all_specs[ref.target_logical_table]
        target_plan = Identifier(plan_name(target_spec))
        # 只连目标计划表中真正属于最终集合的行（insert/update/unchanged）。
        # delete 行是即将消失的旧身份，绝不能充当父记录。
        conn.execute(
            SQL(
                "UPDATE {plan} p SET {fkcol} = tp.id "
                "FROM {refstage} rs "
                "JOIN {target_plan} tp "
                "  ON tp.nk = rs.{refname} AND tp.action <> 'delete' "
                "WHERE p.row_no = rs.row_no"
            ).format(
                plan=plan,
                refstage=refstage,
                target_plan=target_plan,
                fkcol=Identifier(ref.column),
                refname=Identifier(ref.name),
            )
        )
        if not ref.nullable:
            # 非空引用：暂存里给了自然键元组，但在目标最终集合里解析不到 id。
            # 直接以引用暂存表为准（rs.<ref> 非空），左连计划表找未命中项，
            # 避免内连把缺失父键的行漏掉。
            row = conn.execute(
                SQL(
                    "SELECT s.src_file, s.src_line "
                    "FROM {refstage} rs "
                    "JOIN {stage} s ON s.row_no = rs.row_no "
                    "LEFT JOIN {plan} p ON p.row_no = rs.row_no "
                    "WHERE rs.{refname} IS NOT NULL "
                    "AND (p.{fkcol} IS NULL OR p.row_no IS NULL) LIMIT 1"
                ).format(
                    refstage=refstage,
                    stage=stage,
                    plan=plan,
                    refname=Identifier(ref.name),
                    fkcol=Identifier(ref.column),
                )
            ).fetchone()
            if row is not None:
                from .errors import HandoffError

                raise HandoffError(
                    f"非空引用 {ref.name!r} 在本次交接的最终集合中找不到目标自然键"
                    "（不能引用即将删除的旧行，也不能置空）",
                    logical_table=spec.logical_name,
                    file=row[0],
                    line=row[1],
                    field=ref.name,
                )


def mark_unchanged(conn: Connection[Any], spec: TableSpec) -> None:
    """保留身份的行，业务字段与引用全部相同时标 unchanged，不发 UPDATE。"""
    plan = Identifier(plan_name(spec))
    target = qtable(spec)
    checks = [
        SQL("({p}.{c} IS NOT DISTINCT FROM {t}.{c})").format(
            p=plan, t=target, c=Identifier(c)
        )
        for c in (*spec.columns, *(r.column for r in spec.refs))
    ]
    where = SQL(" AND ").join(checks) if checks else SQL("TRUE")
    conn.execute(
        SQL(
            "UPDATE {plan} SET action = 'unchanged' "
            "FROM {target} WHERE {plan}.action = 'update' AND {plan}.id = {target}.id "
            "AND {where}"
        ).format(plan=plan, target=target, where=where)
    )


def apply_deletes(conn: Connection[Any], spec: TableSpec) -> dict[str, int]:
    plan = Identifier(plan_name(spec))
    target = qtable(spec)
    deleted = conn.execute(
        SQL(
            "DELETE FROM {target} WHERE id IN "
            "(SELECT id FROM {plan} WHERE action = 'delete')"
        ).format(plan=plan, target=target)
    ).rowcount
    return {"deleted": deleted}


def apply_inserts(conn: Connection[Any], spec: TableSpec) -> dict[str, int]:
    plan = Identifier(plan_name(spec))
    target = qtable(spec)
    ins_cols = SQL(", ").join(
        [
            Identifier("id"),
            *[Identifier(c) for c in spec.natural_key],
            *[Identifier(c) for c in spec.columns],
            *[Identifier(r.column) for r in spec.refs],
        ]
    )
    ins_select = SQL(", ").join(
        [
            SQL("p.id"),
            *[SQL("p.nk[{}]").format(SQL(str(i + 1))) for i in range(len(spec.natural_key))],
            *[SQL("p.{}").format(Identifier(c)) for c in spec.columns],
            *[SQL("p.{}").format(Identifier(r.column)) for r in spec.refs],
        ]
    )
    inserted = conn.execute(
        SQL(
            "INSERT INTO {target} ({cols}) "
            "SELECT {sel} FROM {plan} p WHERE p.action = 'insert'"
        ).format(target=target, cols=ins_cols, sel=ins_select, plan=plan)
    ).rowcount
    return {"inserted": inserted}


def apply_updates(conn: Connection[Any], spec: TableSpec) -> dict[str, int]:
    plan = Identifier(plan_name(spec))
    target = qtable(spec)
    set_parts = [
        SQL("{c} = p.{c}").format(c=Identifier(c))
        for c in (*spec.columns, *(r.column for r in spec.refs))
    ]
    if not set_parts:
        return {"updated": 0}
    updated = conn.execute(
        SQL(
            "UPDATE {target} t SET {sets} FROM {plan} p "
            "WHERE p.action = 'update' AND t.id = p.id"
        ).format(target=target, plan=plan, sets=SQL(", ").join(set_parts))
    ).rowcount
    return {"updated": updated}


def count_unchanged(conn: Connection[Any], spec: TableSpec) -> int:
    plan = Identifier(plan_name(spec))
    return conn.execute(
        SQL("SELECT count(*) FROM {plan} WHERE action = 'unchanged'").format(plan=plan)
    ).fetchone()[0]


def duplicate_natural_key_query(spec: TableSpec) -> SQL:
    """查暂存内重复自然键，返回第二处及之后的出现位置（整次拒绝，不合并）。"""
    stage = Identifier(stage_name(spec))
    nk = stage_nk_expr(spec)
    return SQL(
        "SELECT src_file, src_line FROM ("
        " SELECT {nk} AS nk, src_file, src_line,"
        " row_number() OVER (PARTITION BY {nk} ORDER BY row_no) AS rn"
        " FROM {stage}) q WHERE rn > 1 LIMIT 1"
    ).format(stage=stage, nk=nk)


def stage_unique_violation_query(spec: TableSpec, columns: tuple[str, ...]) -> SQL:
    """在暂存最终集合上模拟某个唯一约束（按 PostgreSQL 普通 UNIQUE 的 null 语义）。

    自然键在暂存表里存为 nk0..nkN；普通列才使用物理列名。
    """
    stage = Identifier(stage_name(spec))
    if columns == spec.natural_key:
        expr = stage_nk_expr(spec)
        null_check = SQL(" OR ".join("FALSE" for _ in columns))  # 自然键非空
    else:
        expr = SQL("ARRAY[{}]::text[]").format(
            SQL(", ").join(Identifier(c) for c in columns)
        )
        null_check = SQL(" OR ").join(
            SQL("{}.{} IS NULL").format(stage, Identifier(c)) for c in columns
        )
    return SQL(
        "SELECT q.src_file, q.src_line FROM ("
        " SELECT {expr} AS key_tuple, src_file, src_line,"
        " count(*) OVER (PARTITION BY {expr}) AS cnt,"
        " bool_or({null_check}) OVER (PARTITION BY {expr}) AS has_null"
        " FROM {stage}) q WHERE q.cnt > 1 AND NOT q.has_null LIMIT 1"
    ).format(expr=expr, null_check=null_check, stage=stage)
