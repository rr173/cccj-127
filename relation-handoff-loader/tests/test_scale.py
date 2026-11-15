"""大规模流式装载测试（十万级行）。

验证：
* 文件逐行读、批次有界，Python 不把全量表载入字典（峰值 RSS 受控）；
* 装载后总量、身份保留、首行/尾行引用在数据库中正确；
* 记录耗时与峰值 RSS。
"""
from __future__ import annotations

import json
import resource
import time

import psycopg

from handoff.loader import load_relation_handoff
from tests.conftest import admin_dsn, loader_dsn

N = 100_000


def _build_scale_schema(schema: str):
    """一张大表 big_t（单列自然键 + bigint 值 + 自引用可空外键），一张父表。"""
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(
            f"""
            CREATE SEQUENCE {schema}.parent_id_seq;
            CREATE SEQUENCE {schema}.big_id_seq;
            CREATE TABLE {schema}.parent_t (
                id bigint PRIMARY KEY DEFAULT nextval('{schema}.parent_id_seq'),
                pk text NOT NULL,
                CONSTRAINT parent_nk UNIQUE (pk) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE {schema}.big_t (
                id bigint PRIMARY KEY DEFAULT nextval('{schema}.big_id_seq'),
                nk text NOT NULL,
                val bigint NOT NULL,
                parent bigint,
                prev bigint,
                CONSTRAINT big_nk UNIQUE (nk) DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT big_parent_fk FOREIGN KEY (parent)
                    REFERENCES {schema}.parent_t(id) ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED,
                CONSTRAINT big_prev_fk FOREIGN KEY (prev)
                    REFERENCES {schema}.big_t(id) ON DELETE NO ACTION
                    DEFERRABLE INITIALLY DEFERRED
            );
            GRANT USAGE ON SCHEMA {schema} TO handoff_loader;
            GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {schema} TO handoff_loader;
            GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA {schema} TO handoff_loader;
            """
        )


def _scale_contract(schema: str):
    doc = {
        "schema": schema,
        "tables": [
            {
                "logical_name": "parent_t", "schema": schema, "table": "parent_t",
                "natural_key": ["pk"], "columns": [], "refs": [],
                "unique_constraints": [], "id_sequence": "parent_id_seq",
            },
            {
                "logical_name": "big_t", "schema": schema, "table": "big_t",
                "natural_key": ["nk"],
                "columns": [{"name": "val", "type": "bigint", "nullable": False}],
                "refs": [
                    {"name": "parent", "column": "parent", "target": "parent_t",
                     "nullable": True, "deferrable": True, "initially_deferred": True},
                    {"name": "prev", "column": "prev", "target": "big_t",
                     "nullable": True, "deferrable": True, "initially_deferred": True},
                ],
                "unique_constraints": [], "id_sequence": "big_id_seq",
            },
        ],
    }
    from handoff.contract import parse_contract

    return parse_contract(doc)


def test_hundred_thousand_rows_streaming(tmp_path):
    import secrets

    schema = f"scale_{secrets.token_hex(4)}"
    _build_scale_schema(schema)
    parent_file = tmp_path / "parent.jsonl"
    big_file = tmp_path / "big.jsonl"

    # 父表 10 行；大表 N 行，每行 prev 指向前一行（首行为 null），parent 轮指。
    with open(parent_file, "w", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(json.dumps({"key": [f"P{i}"], "values": {}, "refs": {}}) + "\n")

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    start = time.perf_counter()
    with open(big_file, "w", encoding="utf-8") as fh:
        for i in range(N):
            refs = {"parent": [f"P{i % 10}"], "prev": [f"k{i - 1}"] if i > 0 else None}
            fh.write(json.dumps({
                "key": [f"k{i}"], "values": {"val": i}, "refs": refs,
            }) + "\n")
    gen_seconds = time.perf_counter() - start

    contract = _scale_contract(schema)
    files = {"parent_t": [str(parent_file)], "big_t": [str(big_file)]}

    start = time.perf_counter()
    # 小批次（500）强制多批落库，证明有界批次路径正确。
    with psycopg.connect(loader_dsn()) as conn:
        report = load_relation_handoff(conn, contract, files, batch_size=500)
    load_seconds = time.perf_counter() - start
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # 报告计数。
    assert report.tables["big_t"].inserted == N
    assert report.tables["parent_t"].inserted == 10

    # 数据库总量、首行/尾行身份与引用。
    with psycopg.connect(loader_dsn()) as conn:
        total = conn.execute(
            f"SELECT count(*) FROM {schema}.big_t"
        ).fetchone()[0]
        assert total == N
        first = conn.execute(
            f"SELECT id, val, parent, prev FROM {schema}.big_t WHERE nk='k0'"
        ).fetchone()
        last = conn.execute(
            f"SELECT id, val, parent, prev FROM {schema}.big_t WHERE nk=%s",
            (f"k{N - 1}",),
        ).fetchone()
        # 首行 prev 为 null；尾行 prev 指向倒数第二行的真实 id。
        assert first[2] is not None and first[3] is None
        prev_row = conn.execute(
            f"SELECT id FROM {schema}.big_t WHERE nk=%s", (f"k{N - 2}",)
        ).fetchone()
        assert last[3] == prev_row[0]
        # 外键完整性由数据库约束保证，再显式查一次悬空引用。
        dangling = conn.execute(
            f"SELECT count(*) FROM {schema}.big_t b WHERE b.prev IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {schema}.big_t x WHERE x.id=b.prev)"
        ).fetchone()[0]
        assert dangling == 0

    # 幂等重交：全 unchanged，id 不漂移。
    with psycopg.connect(loader_dsn()) as conn:
        again = load_relation_handoff(conn, contract, files, batch_size=2000)
    assert again.tables["big_t"].unchanged == N
    assert again.tables["big_t"].inserted == 0

    # 清理。
    with psycopg.connect(admin_dsn()) as admin:
        admin.autocommit = True
        admin.execute(f"DROP SCHEMA {schema} CASCADE")

    # 记录规模指标（ru_maxrss 单位 KB，Linux）。
    print(
        f"\n[scale] N={N} 生成={gen_seconds:.2f}s 装载={load_seconds:.2f}s "
        f"峰值RSS={rss_after}KB（装载前={rss_before}KB，增量={rss_after - rss_before}KB）"
    )
    # 内存边界：增量不应随行数线性膨胀到把全表载入 Python（宽松上限，留出 DB 驱动）。
    # 全表若入 Python 字典，N=10万行会达数百 MB；流式增量应远低于该量级。
    assert (rss_after - rss_before) < 200_000  # KB ≈ 200MB 上限，实测通常远低
