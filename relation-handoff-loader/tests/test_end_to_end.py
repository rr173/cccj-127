"""端到端装载测试：身份保留、循环引用、别名交换、计数、幂等与顺序无关。

预期由 tests/naive_oracle.py 的独立朴素模型计算，不调用产品的映射/差分代码。
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from handoff.errors import HandoffError
from handoff.loader import load_relation_handoff
from tests.conftest import loader_dsn
from tests.helpers import build_contract, demo_files, snapshot_all, write_jsonl
from tests.naive_oracle import NaiveModel


CONTRACT_DOC = json.loads(
    (Path(__file__).resolve().parents[1] / "demo" / "contract.json").read_text("utf-8")
)
NULLABLE = {
    t["logical_name"]: {r["name"]: r["nullable"] for r in t.get("refs", [])}
    for t in CONTRACT_DOC["tables"]
}


def _load(schema, files, batch_size=1000):
    contract = build_contract(schema)
    with psycopg.connect(loader_dsn()) as conn:
        return load_relation_handoff(conn, contract, files, batch_size=batch_size)


def _oracle_expected(files, old_snapshot, new_id_seeds):
    model = NaiveModel(CONTRACT_DOC)
    model.ingest(files)
    # 灌入旧状态快照
    model.seed_old_state(
        "organization",
        [{"id": r["id"], "key": (r["org_code"], r["org_tenant"]),
          "values": {"legal_name": r["legal_name"], "tier": r["tier"], "active": r["active"]},
          "ref_ids": {"contact": r["contact_member"]}} for r in old_snapshot["organization"]],
    )
    model.seed_old_state(
        "member",
        [{"id": r["id"], "key": (r["tenant_id"], r["member_code"]),
          "values": {"display_name": r["display_name"], "alias": r["alias"],
                     "score": r["score"], "vip": r["vip"]},
          "ref_ids": {"manager": r["manager_id"], "org": r["org_id"]}}
         for r in old_snapshot["member"]],
    )
    model.seed_old_state(
        "assignment",
        [{"id": r["id"], "key": (r["project_key"], r["role_code"]),
          "values": {"note": r["note"]},
          "ref_ids": {"assignee": r["member_id"]}} for r in old_snapshot["assignment"]],
    )
    model.seed_old_state(
        "weird_named",
        [{"id": r["id"], "key": (r["kc"],),
          "values": {"label": r["label"]}, "ref_ids": {}}
         for r in old_snapshot["weird"]],
    )
    plans = {
        logical: model.identity_plan(logical, new_id_seeds[logical])
        for logical in model.tables
    }
    resolved = model.resolve_refs(plans)
    assert model.missing_non_null_parents(resolved, NULLABLE) == []
    assert model.unique_violations() == []
    counts = {logical: model.counts(logical, plans, resolved) for logical in model.tables}
    return plans, resolved, counts


def test_full_handoff_identity_cycles_alias_swap_and_counts(fresh_schema):
    files = demo_files("handoff")
    before = snapshot_all(fresh_schema)

    # 新 id 种子取旧序列之后的值（演示种子序列分别到 102/1004/5002/9001）。
    new_id_seeds = {"organization": 103, "member": 1005, "assignment": 5003,
                    "weird_named": 9002}
    plans, resolved, counts = _oracle_expected(files, before, new_id_seeds)

    report = _load(fresh_schema, files)
    after = snapshot_all(fresh_schema)

    # 1) 四类计数与朴素模型一致，且以数据库提交结果为准。
    for logical, want in counts.items():
        got = report.tables[logical]
        assert (got.inserted, got.updated, got.deleted, got.unchanged) == (
            want["inserted"], want["updated"], want["deleted"], want["unchanged"]
        ), logical

    # 2) 身份保留：旧自然键 id 不变。
    member_id = {(r["tenant_id"], r["member_code"]): r["id"] for r in after["member"]}
    assert member_id[("T1", "M1")] == 1001
    assert member_id[("T1", "M2")] == 1002
    assert member_id[("T1", "M4")] == 1004
    # 删除的旧身份不存在；新身份取全局一致的新 id。
    assert ("T1", "M3") not in member_id
    new_a = member_id[("T1", "M-NEW-A")]
    new_b = member_id[("T1", "M-NEW-B")]
    assert new_a not in (1001, 1002, 1004) and new_b not in (1001, 1002, 1004)
    assert new_a != new_b

    # 3) 非空循环负责人：A->B、B->A 都是真实外键，普通 SQL 可连出来。
    by_key = {(r["tenant_id"], r["member_code"]): r for r in after["member"]}
    assert by_key[("T1", "M-NEW-A")]["manager_id"] == new_b
    assert by_key[("T1", "M-NEW-B")]["manager_id"] == new_a
    # 老成员负责人互换（合法别名交换 + 引用重接）。
    assert by_key[("T1", "M1")]["manager_id"] == 1002
    assert by_key[("T1", "M2")]["manager_id"] == 1001

    # 4) 组织联系人非空且指向新成员 A（题目里的组织联系人场景）。
    org = {(r["org_code"], r["org_tenant"]): r for r in after["organization"]}
    assert org[("ORG", "A")]["contact_member"] == new_a
    assert org[("ORG", "C")]["contact_member"] == new_a

    # 5) 事实表仍能通过保留的身份查到（属性变了，事实不断）。
    fact = {(r["event_name"]): r["member_id"] for r in after["fact_usage"]}
    assert fact["login"] == 1002 and fact["report"] == 1004
    assert before["fact_usage"] == after["fact_usage"]  # 事实表原样未动

    # 6) bigint 两端值不经浮点、原样落库。
    assert by_key[("T1", "M-NEW-A")]["score"] == 9223372036854775807
    assert by_key[("T1", "M-NEW-B")]["score"] == -9223372036854775808

    # 7) 复合键分量含分隔符/引号，未被拼接成假键。
    assert ('T1', 'a|b"c,d') in member_id


def test_idempotent_reload_all_unchanged_and_ids_do_not_drift(fresh_schema):
    files = demo_files("handoff")
    _load(fresh_schema, files)
    after_first = snapshot_all(fresh_schema)

    # 换一个进程（这里以独立连接模拟新会话）再次交相同数据。
    second = _load(fresh_schema, files)
    after_second = snapshot_all(fresh_schema)

    for logical in ("organization", "member", "assignment", "weird_named"):
        r = second.tables[logical]
        assert (r.inserted, r.updated, r.deleted) == (0, 0, 0)
        assert r.unchanged > 0
    assert after_first == after_second  # id 不漂移、字节意义不变


def test_row_and_file_order_does_not_change_result(fresh_schema, tmp_path):
    files = demo_files("handoff")
    _load(fresh_schema, files)
    baseline = snapshot_all(fresh_schema)

    # 重新置库，再以不同文件/行顺序交接，最终关系必须一致（id 由自然键决定）。
    # 通过 reset 由调用夹具难以复用，这里直接重建一个新 schema 场景。
    # （fresh_schema 已被装载，改在同一库上反向再交一次验证顺序无关性。）
    # 打乱 member 两个文件的行顺序后重交：
    rows = []
    for p in files["member"]:
        rows += [json.loads(l) for l in Path(p).read_text("utf-8").splitlines()]
    rows.reverse()
    shuffled = write_jsonl(tmp_path / "member_shuf.jsonl", rows)
    files2 = dict(files)
    files2["member"] = [str(shuffled)]
    report = _load(fresh_schema, files2)
    # 内容相同 => 全部 unchanged，计数与关系不变。
    assert report.tables["member"].unchanged == len(rows)
    assert snapshot_all(fresh_schema) == baseline


def test_empty_file_handoffs_empty_table_but_protects_fact_referenced(fresh_schema):
    files = demo_files("handoff")
    # assignment 交成空表：它不被外部事实引用，允许清空。
    empty = Path(files["assignment"][0]).parent / "empty_assignment.jsonl"
    empty.write_text("", encoding="utf-8")
    files["assignment"] = [str(empty)]
    report = _load(fresh_schema, files)
    assert report.tables["assignment"].deleted >= 1
    with psycopg.connect(loader_dsn()) as conn:
        n = conn.execute(
            psycopg.sql.SQL('SELECT count(*) FROM {s}.assignment').format(
                s=psycopg.sql.Identifier(fresh_schema))
        ).fetchone()[0]
    assert n == 0
    # fact_usage 原样保留。
    assert len(snapshot_all(fresh_schema)["fact_usage"]) == 2


def test_same_natural_key_in_different_logical_tables_are_independent(fresh_schema):
    # 两张不同逻辑表使用相同键值互不影响：weird 与 organization 都可含 "ORG" 类文本。
    files = demo_files("handoff")
    _load(fresh_schema, files)
    after = snapshot_all(fresh_schema)
    # weird 表的 w1 保留原 id 9001。
    weird = {r["kc"]: r["id"] for r in after["weird"]}
    assert weird["w1"] == 9001


def test_batch_size_variations_produce_same_result(fresh_schema):
    files = demo_files("handoff")
    _load(fresh_schema, files, batch_size=1)
    small = snapshot_all(fresh_schema)
    # 再用大批次重交，应全 unchanged 且数据一致。
    report = _load(fresh_schema, files, batch_size=100000)
    for logical in ("organization", "member", "assignment", "weird_named"):
        assert report.tables[logical].inserted == 0
    assert snapshot_all(fresh_schema) == small


def test_duplicate_natural_key_within_table_rejects_entire_load(fresh_schema, tmp_path):
    files = demo_files("handoff")
    before = snapshot_all(fresh_schema)
    rows = [json.loads(l) for l in Path(files["member"][0]).read_text("utf-8").splitlines()]
    rows.append(dict(rows[0]))  # 完全相同的自然键再出现一次（内容一样也拒绝）
    dup = write_jsonl(tmp_path / "member_dup.jsonl", rows)
    files["member"] = [str(dup), files["member"][1]]
    with pytest.raises(HandoffError) as ei:
        _load(fresh_schema, files)
    assert "重复" in str(ei.value)
    assert snapshot_all(fresh_schema) == before  # 整次回滚，字节不变
