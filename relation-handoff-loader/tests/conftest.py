"""pytest 夹具：管理离线 PostgreSQL 演示集群与每个测试的独立 schema。

测试只连接演示专用数据库，不访问任何宿主已有服务。每个测试函数获得一个
全新的、按演示 DDL 建好的 schema（含 organization/member/assignment/fact_usage
及引号中文表），测试结束自动 DROP。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO = PROJECT_ROOT / "demo"
PG_HOME = Path(os.environ.get("PG_HOME", "/workspace/.pglocal/usr/lib/postgresql/15"))
SOCKET = PROJECT_ROOT / ".demo-pgsocket"
PORT = int(os.environ.get("HANDOFF_PGPORT", "55432"))
ADMIN_USER = "handoff"
LOAD_USER = "handoff_loader"
DB = "handoff_demo_db"

# 让测试能 import 产品包，且不依赖工作目录。
sys.path.insert(0, str(PROJECT_ROOT))


def admin_dsn(db: str = DB) -> str:
    return f"host={SOCKET} port={PORT} dbname={db} user={ADMIN_USER}"


def loader_dsn(db: str = DB) -> str:
    return f"host={SOCKET} port={PORT} dbname={db} user={LOAD_USER}"


def _psql_args(db: str = DB) -> list[str]:
    return [str(PG_HOME / "bin" / "psql"), "-h", str(SOCKET), "-p", str(PORT),
            "-U", ADMIN_USER, "-d", db, "-v", "ON_ERROR_STOP=1"]


def _cluster_running() -> bool:
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(str(SOCKET / f".s.PGSQL.{PORT}"))
            return True
        except OSError:
            return False


def _ensure_cluster() -> None:
    if _cluster_running():
        return
    pg_env = PROJECT_ROOT / "demo" / "pg_env.py"
    subprocess.run([sys.executable, str(pg_env), "start"], check=True)


@pytest.fixture(scope="session")
def db_ready() -> str:
    _ensure_cluster()
    return loader_dsn()


@pytest.fixture()
def conn_factory(db_ready):
    """返回一个产生干净装载连接的工厂，并负责清理临时 schema。"""
    created: list[str] = []

    def make(admin: bool = False, db: str = DB):
        return psycopg.connect(admin_dsn(db) if admin else loader_dsn(db))

    yield make

    if created:
        with psycopg.connect(admin_dsn()) as admin:
            admin.autocommit = True
            for sch in created:
                admin.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')


def _load_sql(conn, sql_path: Path, schema: str) -> None:
    text = sql_path.read_text(encoding="utf-8").replace("handoff_demo", schema)
    with conn.cursor() as cur:
        cur.execute(text)


@pytest.fixture()
def fresh_schema(conn_factory):
    """建一个结构与演示完全一致、但名字唯一的 schema，并授予装载角色权限。"""
    import secrets

    schema = f"t_{secrets.token_hex(6)}"
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"CREATE SCHEMA {schema}")
        _load_sql(admin, DEMO / "sql" / "01_schema.sql", schema)
        _load_sql(admin, DEMO / "sql" / "02_seed.sql", schema)
        admin.execute(
            f"GRANT USAGE ON SCHEMA {schema} TO {LOAD_USER}; "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} "
            f"TO {LOAD_USER}; "
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {schema} TO {LOAD_USER};"
        )
    yield schema
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture(autouse=True)
def _cleanup_generated_fix_dirs():
    """每个测试后清理运行期生成的 fix_* 修正输入，保持 demo/data 干净。"""
    yield
    import shutil

    for d in (DEMO / "data").glob("fix_*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    for f in (DEMO / "data").rglob("cli_contract_*.json"):
        f.unlink(missing_ok=True)
    shuf = DEMO / "data/_verify_shuffled_member.jsonl"
    shuf.unlink(missing_ok=True)


@pytest.fixture()
def demo_contract_path() -> Path:
    return DEMO / "contract.json"


def handoff_files(base: str | Path) -> dict[str, list[str]]:
    """标准演示交接文件集合（路径基于 demo/data 下的某个目录）。"""
    b = Path(base)
    return {
        "organization": [str(b / "organization.jsonl")],
        "member": [str(b / "member-1.jsonl"), str(b / "member-2.jsonl")],
        "assignment": [str(b / "assignment.jsonl")],
        "weird_named": [str(b / "weird.jsonl")],
    }
