"""可空唯一约束的 PostgreSQL 普通 UNIQUE 语义（含 null 不参与冲突）。"""
from __future__ import annotations

import json
import secrets

import psycopg
import pytest

from handoff.contract import parse_contract
from handoff.errors import HandoffError
from handoff.loader import load_relation_handoff
from tests.conftest import admin_dsn, loader_dsn


def _make_schema(schema: str) -> None:
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(
            f"""
            CREATE SEQUENCE {schema}.t_seq;
            CREATE TABLE {schema}.t (
                id bigint PRIMARY KEY DEFAULT nextval('{schema}.t_seq'),
                nk text NOT NULL,
                nick text,
                CONSTRAINT t_nk UNIQUE (nk) DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT t_nick_uk UNIQUE (nick) DEFERRABLE INITIALLY DEFERRED
            );
            GRANT USAGE ON SCHEMA {schema} TO handoff_loader;
            GRANT ALL ON ALL TABLES IN SCHEMA {schema} TO handoff_loader;
            GRANT ALL ON ALL SEQUENCES IN SCHEMA {schema} TO handoff_loader;
            """
        )


def _contract(schema: str):
    return parse_contract({
        "schema": schema,
        "tables": [{
            "logical_name": "t", "schema": schema, "table": "t",
            "natural_key": ["nk"],
            "columns": [{"name": "nick", "type": "text", "nullable": True}],
            "refs": [], "unique_constraints": [["nick"]],
            "id_sequence": "t_seq",
        }],
    })


def test_null_unique_values_do_not_conflict(tmp_path):
    schema = f"nul_{secrets.token_hex(4)}"
    _make_schema(schema)
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"key": ["a"], "values": {"nick": None}, "refs": {}}) + "\n"
        + json.dumps({"key": ["b"], "values": {"nick": None}, "refs": {}}) + "\n",
        encoding="utf-8",
    )
    with psycopg.connect(loader_dsn()) as conn:
        rep = load_relation_handoff(conn, _contract(schema), {"t": [str(p)]})
    assert rep.tables["t"].inserted == 2  # 两个 null 不冲突
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_non_null_duplicate_in_unique_column_rejected(tmp_path):
    schema = f"nul_{secrets.token_hex(4)}"
    _make_schema(schema)
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"key": ["a"], "values": {"nick": "x"}, "refs": {}}) + "\n"
        + json.dumps({"key": ["b"], "values": {"nick": "x"}, "refs": {}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HandoffError) as ei:
        with psycopg.connect(loader_dsn()) as conn:
            load_relation_handoff(conn, _contract(schema), {"t": [str(p)]})
    assert "唯一约束" in str(ei.value)
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
