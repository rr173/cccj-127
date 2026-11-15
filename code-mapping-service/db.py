"""数据库初始化：建表 + 种子数据（幂等，重复执行会重建）。"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

DEFAULT_DSN = "host=/tmp port=55432 user=app dbname=postgres"
SQL_DIR = Path(__file__).parent / "sql"


def init_database(dsn: str | None = None) -> None:
    dsn = dsn or os.environ.get("MAPPING_DSN", DEFAULT_DSN)
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        # 干净重建，保证脚本可重复执行
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.autocommit = False
        for name in ("01_schema.sql", "02_seed.sql"):
            conn.execute((SQL_DIR / name).read_text(encoding="utf-8"))
        conn.commit()
    print(f"database initialized: {dsn}")


if __name__ == "__main__":
    init_database()
