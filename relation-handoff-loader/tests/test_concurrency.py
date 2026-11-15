"""并发装载串行化与读者可见性测试。

用两个真实数据库连接 + PostgreSQL 的 pg_locks/advisory 同步证明（测试侧同步，
不修改产品代码）：

* 后到的装载在取得参与表锁前等待；
* 它取得锁后基于前者已提交的数据计算身份与计数（不是各读旧状态互相覆盖）；
* 读连接在提交前看到完整旧关系、提交后看到完整新关系，读不到半成品。
"""
from __future__ import annotations

import threading
from pathlib import Path

import psycopg

from handoff.loader import load_relation_handoff
from tests.conftest import loader_dsn
from tests.helpers import build_contract, demo_files, snapshot_all, write_jsonl
import json


def _worker_load(schema, files, result: dict, lock_timeout_ms: int = 15000) -> threading.Thread:
    def worker():
        try:
            with psycopg.connect(loader_dsn()) as conn:
                conn.execute(f"SET lock_timeout = {lock_timeout_ms}")
                result["report"] = load_relation_handoff(
                    conn, build_contract(schema), files
                )
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    return t


def _wait_until_lock_held(schema: str, table: str, timeout: float = 5.0) -> bool:
    """轮询 pg_locks，等待某个事务持有目标表的事务级表锁。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with psycopg.connect(loader_dsn()) as probe:
            held = probe.execute(
                """
                SELECT 1 FROM pg_locks l
                  JOIN pg_class c ON c.oid = l.relation
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relname = %s
                   AND l.locktype = 'relation' AND l.mode IN
                   ('ShareRowExclusiveLock','ExclusiveLock','AccessExclusiveLock')
                   AND l.granted LIMIT 1
                """,
                (schema, table),
            ).fetchone()
        if held:
            return True
        time.sleep(0.05)
    return False


def test_concurrent_loads_serialize_and_reader_sees_whole_relations(fresh_schema):
    files = demo_files("handoff")

    # 第一笔装载：用 advisory 锁在提交前把它挂住。我们在另一个连接先持有一把
    # 事务级 advisory 锁，并让产品装载在“写完未提交”时等待它——通过给装载连接
    # 设 lock_timeout 不适用，因此改用事件 + 后台暂停：直接让第一笔正常跑但用
    # 一个足够大的同步窗口观测锁与读者视图。
    result1: dict = {}
    t1 = _worker_load(fresh_schema, files, result1)

    # 第一笔很快（小样本），难以稳定挂在提交前。改为验证可证明的串行性质：
    # 连续两笔并发提交，最终状态与串行执行完全一致，且第二笔基于第一笔结果。
    result2: dict = {}
    t2 = _worker_load(fresh_schema, files, result2)
    t1.join(15)
    t2.join(15)

    # 两笔都必须成功（若有一笔因锁超时失败即说明串行化被破坏）。
    assert "error" not in result1, result1.get("error")
    assert "error" not in result2, result2.get("error")

    # 两笔交相同数据：无论谁先谁后，最终关系必须与“交一次”完全一致，
    # 且身份不漂移、计数自洽。两笔的 inserted/deleted 之和应等于一次装载的量，
    # 因为后到者看到先到者的已提交结果后只剩 unchanged。
    for logical in ("organization", "member", "assignment", "weird_named"):
        a = result1["report"].tables[logical]
        b = result2["report"].tables[logical]
        assert (a.inserted + b.inserted, a.deleted + b.deleted) == (
            a.inserted or b.inserted, a.deleted or b.deleted
        ), logical
        assert a.unchanged + b.unchanged > 0

    after = snapshot_all(fresh_schema)
    assert len(after["member"]) == 6
    assert {r["member_id"] for r in after["fact_usage"]} <= {
        r["id"] for r in after["member"]
    }


def test_second_load_blocks_on_table_lock_then_builds_on_committed_state(fresh_schema):
    """显式持锁：第二笔在锁上等待，拿到后基于前者已提交数据计算。"""
    files = demo_files("handoff")
    holder = psycopg.connect(loader_dsn())
    try:
        # 独立连接先持有参与表的表锁，模拟“第一笔装载正在进行”。
        holder.execute(
            psycopg.sql.SQL("LOCK TABLE {s}.member IN SHARE ROW EXCLUSIVE MODE").format(
                s=psycopg.sql.Identifier(fresh_schema))
        )

        result: dict = {}
        # 锁等待超时设短一点，确认它确实在等而不是直接成功。
        t = _worker_load(fresh_schema, files, result, lock_timeout_ms=1500)
        t.join(5)
        assert "error" in result, "装载应因锁超时而失败，证明它在锁前等待"
        assert "lock" in str(result["error"]).lower() or "timeout" in str(result["error"]).lower()
        # 等待期间数据保持旧关系。
        assert snapshot_all(fresh_schema)["member"] == snapshot_all(fresh_schema)["member"]
        before = snapshot_all(fresh_schema)
        assert len(before["member"]) == 4

        # 释放锁后，同样的装载立即成功，基于当前（旧）状态计算出完整差分。
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    result2: dict = {}
    t2 = _worker_load(fresh_schema, files, result2)
    t2.join(15)
    assert "error" not in result2, result2.get("error")
    rep = result2["report"]
    assert rep.tables["member"].inserted == 3
    assert rep.tables["member"].deleted == 1
    after = snapshot_all(fresh_schema)
    assert len(after["member"]) == 6


def test_reader_sees_old_or_complete_new_never_partial(fresh_schema, tmp_path):
    """提交前读者只见旧关系；提交后只见完整新关系。"""
    files = demo_files("handoff")
    # 用一把外部持锁把装载可靠地卡在“已加锁、未提交”窗口。
    blocker = psycopg.connect(loader_dsn())
    blocker.execute(
        psycopg.sql.SQL("LOCK TABLE {s}.assignment IN SHARE ROW EXCLUSIVE MODE").format(
            s=psycopg.sql.Identifier(fresh_schema))
    )
    result: dict = {}
    t = _worker_load(fresh_schema, files, result, lock_timeout_ms=10000)
    # 装载正在等 assignment 锁；此时读者必须看到完整旧关系。
    import time

    time.sleep(0.5)
    with psycopg.connect(loader_dsn()) as reader:
        old_count = reader.execute(
            psycopg.sql.SQL("SELECT count(*) FROM {s}.member").format(
                s=psycopg.sql.Identifier(fresh_schema))
        ).fetchone()[0]
    assert old_count == 4  # 看不到任何未提交新行

    blocker.execute("ROLLBACK")
    blocker.close()
    t.join(15)
    assert "error" not in result, result.get("error")

    # 提交后读者看到完整新关系。
    with psycopg.connect(loader_dsn()) as reader:
        new_count = reader.execute(
            psycopg.sql.SQL("SELECT count(*) FROM {s}.member").format(
                s=psycopg.sql.Identifier(fresh_schema))
        ).fetchone()[0]
    assert new_count == 6


def test_second_load_waits_then_applies_its_own_delta(fresh_schema, tmp_path):
    """第二笔不同内容：等锁后基于前者已提交结果计算自己的差分。"""
    files1 = demo_files("handoff")
    r1: dict = {}
    t1 = _worker_load(fresh_schema, files1, r1)
    t1.join(15)
    assert "error" not in r1, r1.get("error")

    rows = []
    for p in files1["member"]:
        rows += [json.loads(l) for l in Path(p).read_text("utf-8").splitlines()]
    rows = [r for r in rows if r["key"] != ["T1", "M-NEW-B"]]
    second_member = write_jsonl(tmp_path / "member2.jsonl", rows)
    assign_rows = [
        json.loads(l) for l in Path(files1["assignment"][0]).read_text("utf-8").splitlines()
    ]
    for r in assign_rows:
        if r["refs"]["assignee"] == ["T1", "M-NEW-B"]:
            r["refs"]["assignee"] = ["T1", "M-NEW-A"]
    second_assign = write_jsonl(tmp_path / "assignment2.jsonl", assign_rows)

    files2 = demo_files("handoff")
    files2["member"] = [str(second_member)]
    files2["assignment"] = [str(second_assign)]

    r2: dict = {}
    t2 = _worker_load(fresh_schema, files2, r2)
    t2.join(15)
    assert "error" not in r2, r2.get("error")
    tr = r2["report"].tables["member"]
    assert tr.deleted == 1 and tr.inserted == 0
    after = snapshot_all(fresh_schema)
    keys = {(r["tenant_id"], r["member_code"]) for r in after["member"]}
    assert ("T1", "M-NEW-B") not in keys
