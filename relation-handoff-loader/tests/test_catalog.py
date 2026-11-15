"""目录核对测试：契约与真实库不一致、或出现不支持布局时必须明确拒绝。"""
from __future__ import annotations

import psycopg
import pytest

from handoff import catalog as catalog_mod
from handoff.errors import HandoffError
from handoff.loader import load_relation_handoff
from tests.conftest import admin_dsn, loader_dsn
from tests.helpers import build_contract, demo_files


def _verify(schema: str, override=None):
    contract = build_contract(schema, override)
    with psycopg.connect(loader_dsn()) as conn:
        with conn.transaction():
            for spec in contract.lock_order:
                conn.execute(
                    psycopg.sql.SQL("LOCK TABLE {s}.{t} IN SHARE ROW EXCLUSIVE MODE").format(
                        s=psycopg.sql.Identifier(schema),
                        t=psycopg.sql.Identifier(spec.table),
                    )
                )
            return catalog_mod.verify_catalog(conn, contract)


def test_catalog_matches_demo_layout(fresh_schema):
    cats = _verify(fresh_schema)
    assert set(cats) == {"organization", "member", "assignment", "weird_named"}
    member = cats["member"]
    assert member.primary_key == ("id",)
    assert tuple(sorted(member.unique_indexes)) == (
        ("alias",), ("tenant_id", "member_code"),
    )


def test_mismatch_column_type_rejected(fresh_schema):
    # 契约把 score 改成 text，但库里是 bigint。
    override = {"member": {"columns": [
        {"name": "display_name", "type": "text", "nullable": False},
        {"name": "alias", "type": "text", "nullable": False},
        {"name": "score", "type": "text", "nullable": False},
        {"name": "vip", "type": "boolean", "nullable": True},
    ]}}
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema, override)
    assert "类型不匹配" in str(ei.value)


def test_mismatch_nullability_rejected(fresh_schema):
    override = {"member": {"columns": [
        {"name": "display_name", "type": "text", "nullable": False},
        {"name": "alias", "type": "text", "nullable": True},  # 库里 NOT NULL
        {"name": "score", "type": "bigint", "nullable": False},
        {"name": "vip", "type": "boolean", "nullable": True},
    ]}}
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema, override)
    assert "nullability" in str(ei.value)


def test_extra_undeclared_column_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'ALTER TABLE "{fresh_schema}".member ADD COLUMN rogue text')
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "额外列" in str(ei.value)


def test_extra_check_constraint_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'ALTER TABLE "{fresh_schema}".member ADD CONSTRAINT chk CHECK (score >= 0)')
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "CHECK" in str(ei.value)


def test_user_trigger_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'SET search_path TO "{fresh_schema}"')
        admin.execute(
            f'CREATE FUNCTION "{fresh_schema}".trg() RETURNS trigger LANGUAGE plpgsql '
            "AS $$ BEGIN RETURN NEW; END $$;"
        )
        admin.execute(
            f'CREATE TRIGGER t BEFORE INSERT ON "{fresh_schema}".member '
            f'FOR EACH ROW EXECUTE FUNCTION "{fresh_schema}".trg()'
        )
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "触发器" in str(ei.value)


def test_generated_column_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'ALTER TABLE "{fresh_schema}".member ADD COLUMN gen text GENERATED ALWAYS AS (alias) STORED')
    # 契约同步声明该列，但目录核对应因生成列而拒绝。
    override = {"member": {"columns": [
        {"name": "display_name", "type": "text", "nullable": False},
        {"name": "alias", "type": "text", "nullable": False},
        {"name": "score", "type": "bigint", "nullable": False},
        {"name": "vip", "type": "boolean", "nullable": True},
        {"name": "gen", "type": "text", "nullable": True},
    ]}}
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema, override)
    assert "生成列" in str(ei.value)


def test_partial_unique_index_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(
            f'CREATE UNIQUE INDEX ON "{fresh_schema}".member (alias) WHERE vip IS TRUE'
        )
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "部分索引" in str(ei.value)


def test_undeclared_plain_unique_index_rejected(fresh_schema):
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'CREATE UNIQUE INDEX ON "{fresh_schema}".member (display_name)')
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "未声明的唯一约束" in str(ei.value)


def test_fk_deferrability_mismatch_rejected(fresh_schema):
    # 契约声称 manager 外键不可延迟，但库里是 DEFERRABLE。
    override = {"member": {"refs": [
        {"name": "manager", "column": "manager_id", "target": "member",
         "nullable": True, "deferrable": False, "initially_deferred": False},
        {"name": "org", "column": "org_id", "target": "organization",
         "nullable": True, "deferrable": True, "initially_deferred": True},
    ]}}
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema, override)
    assert "可延迟属性不匹配" in str(ei.value)


def test_incoming_cascade_fk_rejected(fresh_schema):
    # 造一个带 ON DELETE CASCADE 的入向外键（来自同 schema 的另一张表）。
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(
            f'CREATE TABLE "{fresh_schema}".cascade_child ('
            "id bigint PRIMARY KEY, m bigint REFERENCES "
            f'"{fresh_schema}".member(id) ON DELETE CASCADE)'
        )
        admin.execute(
            f'GRANT SELECT ON "{fresh_schema}".cascade_child TO handoff_loader'
        )
    with pytest.raises(HandoffError) as ei:
        _verify(fresh_schema)
    assert "级联" in str(ei.value) or "入向外键" in str(ei.value)


def test_load_fails_when_catalog_mismatched(fresh_schema):
    # 端到端：目录不匹配时整次装载拒绝，且表数据保持原样。
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'ALTER TABLE "{fresh_schema}".member ADD COLUMN rogue2 text')
    contract = build_contract(fresh_schema)
    files = demo_files()
    with psycopg.connect(loader_dsn()) as conn:
        with pytest.raises(HandoffError):
            load_relation_handoff(conn, contract, files)
    # 旧数据仍在
    with psycopg.connect(loader_dsn()) as conn:
        n = conn.execute(
            psycopg.sql.SQL('SELECT count(*) FROM {s}.member').format(
                s=psycopg.sql.Identifier(fresh_schema))
        ).fetchone()[0]
    assert n == 4
