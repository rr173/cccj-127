"""端到端验证：区间交界、合并/拆分、无对应项、版本封存、冲突发布、反向多候选。

运行方式（venv 已装 psycopg；数据库需已启动并完成 db.init_database）：
    .venv/bin/python -m pytest -v tests/test_mapping_service.py
"""
from __future__ import annotations

from datetime import date

import psycopg
import pytest

from db import DEFAULT_DSN, init_database
from service import MappingError, MappingService


@pytest.fixture(scope="session")
def svc() -> MappingService:
    init_database(DEFAULT_DSN)
    return MappingService(DEFAULT_DSN)


def _draft(svc: MappingService, target_label: str, label: str,
           effective_from: date, effective_to=None) -> int:
    with svc.connect() as conn:
        return svc.create_draft(conn, target_label, label,
                                effective_from, effective_to)


def _row(system, src, tgt, group, gtype, reason, approver="测试人",
         ef=date(2026, 1, 1), et=None):
    return {
        "system_code": system, "code_domain": "CUSTOMER_STATUS",
        "source_code": src, "target_code": tgt, "mapping_group_id": group,
        "group_type": gtype, "effective_from": ef, "effective_to": et,
        "business_reason": reason, "approver": approver,
    }


# 一套与 m2026 等价、可通过全部校验的干净行
def _good_2026_rows():
    return [
        _row("CRM", "ACT", "ACTIVE", "g10", "ONE_TO_ONE", "延续"),
        _row("CRM", "FRO", "INACTIVE", "g11", "ONE_TO_ONE", "延续"),
        _row("CRM", "CLS", "CHURNED", "g12", "ONE_TO_ONE", "延续"),
        _row("POS", "ACTIVE", "ACTIVE", "g13", "ONE_TO_ONE", "换码承接"),
        _row("POS", "INACTIVE", "INACTIVE", "g14", "ONE_TO_ONE", "换码承接"),
        _row("POS", "2", "DORMANT", "g6", "ONE_TO_ONE", "沉睡延续"),
        _row("ERP", "E10", "ACTIVE", "g7", "ONE_TO_ONE", "延续"),
        _row("ERP", "E20", "INACTIVE", "g8", "ONE_TO_ONE", "延续"),
        _row("ERP", "E40", "PENDING", "g15", "ONE_TO_ONE", "观察态待定"),
    ]


# ---------------------------------------------------------------- 1. 区间交界
class TestIntervalBoundaries:
    def test_crm_code_switch_on_2025_07_01(self, svc):
        # 交界前一天：旧码 A 仍有效
        before = svc.forward_convert("CRM", "CUSTOMER_STATUS", "A",
                                     date(2025, 6, 30), "v2025")
        assert before["status"] == "MAPPED"
        assert before["target_code"] == "ACTIVE"
        assert before["mapping_version"] == "m2025"
        assert before["source_code_valid_at_biz_date"] is True

        # 交界当天（半开区间 [from,to)）：旧码失效、新码 ACT 生效
        boundary = svc.forward_convert("CRM", "CUSTOMER_STATUS", "ACT",
                                       date(2025, 7, 1), "v2025")
        assert boundary["target_code"] == "ACTIVE"
        assert boundary["mapping_version"] == "m2025"

        old_expired = svc.forward_convert("CRM", "CUSTOMER_STATUS", "A",
                                          date(2025, 7, 1), "v2025")
        assert old_expired["source_code_valid_at_biz_date"] is False

    def test_mapping_version_boundary(self, svc):
        # 2024-12-31 落在 m2024；2025-01-01 落在 m2025，目标码口径随之切换
        r1 = svc.forward_convert("POS", "CUSTOMER_STATUS", "2",
                                 date(2024, 12, 31), "v2024")
        assert r1["mapping_version"] == "m2024"
        assert r1["status"] == "UNMAPPED" and r1["target_code"] is None

        r2 = svc.forward_convert("POS", "CUSTOMER_STATUS", "2",
                                 date(2025, 1, 1), "v2025")
        assert r2["mapping_version"] == "m2025"
        assert r2["status"] == "MAPPED" and r2["target_code"] == "DORMANT"

    def test_pos_and_erp_switch_on_2026_01_01(self, svc):
        old = svc.forward_convert("POS", "CUSTOMER_STATUS", "1",
                                  date(2025, 12, 31))
        new = svc.forward_convert("POS", "CUSTOMER_STATUS", "ACTIVE",
                                  date(2026, 1, 1))
        assert old["mapping_version"] == "m2025"
        assert new["mapping_version"] == "m2026"
        assert old["target_code"] == new["target_code"] == "ACTIVE"

        e30_gone = svc.forward_convert("ERP", "CUSTOMER_STATUS", "E30",
                                       date(2026, 1, 1))
        assert e30_gone["source_code_valid_at_biz_date"] is False
        e40 = svc.forward_convert("ERP", "CUSTOMER_STATUS", "E40",
                                  date(2026, 1, 1))
        assert e40["target_code"] == "PENDING"

    def test_explicit_target_version_is_honored(self, svc):
        r = svc.forward_convert("CRM", "CUSTOMER_STATUS", "F",
                                date(2025, 6, 1), target_version_label="v2025")
        assert r["target_version"] == "v2025"
        assert r["mapping_version"] == "m2025"


# ------------------------------------------------------- 2. 合并与拆分血缘
class TestMergeAndSplit:
    def test_merge_forward_collapses_two_sources(self, svc):
        crm = svc.forward_convert("CRM", "CUSTOMER_STATUS", "A",
                                  date(2024, 6, 1), "v2024")
        pos = svc.forward_convert("POS", "CUSTOMER_STATUS", "1",
                                  date(2024, 6, 1), "v2024")
        assert crm["group_type"] == "MERGE" and pos["group_type"] == "MERGE"
        assert crm["mapping_group_id"] == pos["mapping_group_id"] == "g1"
        assert crm["target_code"] == pos["target_code"] == "ACTIVE"

    def test_clean_draft_with_consistent_groups_publishes(self, svc):
        vid = _draft(svc, "v2026", "m2026_clean", date(2026, 1, 1))
        with svc.connect() as conn:
            for r in _good_2026_rows():
                svc.add_mapping_row(conn, vid, r)
        out = svc.publish(vid)
        assert out["version_label"] == "m2026_clean"

    def test_incomplete_split_is_rejected(self, svc):
        # 把 CRM:ACT 并入 g13（上一版本 g13 仅含 POS:ACTIVE）-> 分组源码集合变化
        vid = _draft(svc, "v2026", "m2026_split_bad", date(2026, 1, 1))
        rows = _good_2026_rows()
        rows[0] = _row("CRM", "ACT", "PENDING", "g13", "MERGE",
                       "拆分/合并未走完整流程：g13 成员集合与上一版本不一致")
        with svc.connect() as conn:
            for r in rows:
                svc.add_mapping_row(conn, vid, r)
        with pytest.raises(MappingError) as ei:
            svc.publish(vid)
        codes = {v["violation_code"] for v in ei.value.violations}
        assert "INCOMPLETE_MERGE_SPLIT" in codes

    def test_overlapping_mapping_is_rejected(self, svc):
        vid = _draft(svc, "v2026", "m2026_overlap_bad", date(2026, 1, 1))
        rows = _good_2026_rows()
        # 同一源码再来一条重叠区间的不同映射
        rows.append(_row("CRM", "ACT", "INACTIVE", "g99", "ONE_TO_ONE",
                         "重复生效的冲突映射",
                         ef=date(2026, 2, 1), et=date(2026, 3, 1)))
        with svc.connect() as conn:
            for r in rows:
                svc.add_mapping_row(conn, vid, r)
        with pytest.raises(MappingError) as ei:
            svc.publish(vid)
        assert any(v["violation_code"] == "OVERLAPPING_MAPPING"
                   for v in ei.value.violations)

    def test_invalid_target_code_is_rejected(self, svc):
        vid = _draft(svc, "v2026", "m2026_target_bad", date(2026, 1, 1))
        rows = _good_2026_rows()
        rows[0] = _row("CRM", "ACT", "GHOST_CODE", "g10", "ONE_TO_ONE",
                       "引用了目标版本里不存在的码")
        with svc.connect() as conn:
            for r in rows:
                svc.add_mapping_row(conn, vid, r)
        with pytest.raises(MappingError) as ei:
            svc.publish(vid)
        assert any(v["violation_code"] == "INVALID_TARGET_CODE"
                   for v in ei.value.violations)

    def test_missing_mapping_is_rejected(self, svc):
        vid = _draft(svc, "v2026", "m2026_missing_bad", date(2026, 1, 1))
        rows = [r for r in _good_2026_rows()
                if not (r["system_code"] == "ERP" and r["source_code"] == "E40")]
        with svc.connect() as conn:
            for r in rows:
                svc.add_mapping_row(conn, vid, r)
        with pytest.raises(MappingError) as ei:
            svc.publish(vid)
        assert any(v["violation_code"] == "MISSING_MAPPING"
                   and "E40" in v["message"]
                   for v in ei.value.violations)


# ------------------------------------------------------------- 3. 无对应项
class TestUnmapped:
    def test_unmapped_returns_reason_and_approver(self, svc):
        r = svc.forward_convert("POS", "CUSTOMER_STATUS", "2",
                                date(2024, 3, 1), "v2024")
        assert r["status"] == "UNMAPPED"
        assert r["target_code"] is None
        assert "沉睡码" in r["business_reason"]
        assert r["approver"] == "张敏"
        assert r["mapping_version"] == "m2024"

    def test_unmapped_becomes_mapped_in_later_version(self, svc):
        r = svc.forward_convert("ERP", "CUSTOMER_STATUS", "E30",
                                date(2025, 6, 1), "v2025")
        assert r["status"] == "MAPPED"
        assert r["target_code"] == "DORMANT"
        # 血缘分组保持 g9，可追溯它在 m2024 中是无对应项
        assert r["mapping_group_id"] == "g9"


# ------------------------------------------------------------- 4. 版本封存
class TestSealedVersion:
    def test_sealed_mapping_rows_are_immutable(self, svc):
        svc.seal_report("R1_CUSTOMER_MONTHLY", date(2025, 3, 31), "m2025")
        update_sql = (
            "UPDATE mapping_row SET target_code='CHURNED' "
            "WHERE mapping_version_id="
            " (SELECT id FROM mapping_version WHERE version_label='m2025')")
        delete_sql = (
            "DELETE FROM mapping_row WHERE mapping_version_id="
            " (SELECT id FROM mapping_version WHERE version_label='m2025')")
        # 每条预期失败的语句使用独立连接，避免前一次回滚污染事务
        with psycopg.connect(DEFAULT_DSN) as raw:
            with pytest.raises(psycopg.errors.CheckViolation):
                raw.execute(update_sql)
        with psycopg.connect(DEFAULT_DSN) as raw:
            with pytest.raises(psycopg.errors.CheckViolation):
                raw.execute(delete_sql)
        # 未封存版本仍可正常维护
        with psycopg.connect(DEFAULT_DSN) as raw:
            with raw.cursor() as cur:
                cur.execute("SELECT 1 FROM mapping_row "
                            "WHERE mapping_version_id="
                            "(SELECT id FROM mapping_version WHERE version_label='m2024') "
                            "LIMIT 1")
                assert cur.fetchone() is not None

    def test_sealed_report_keeps_historical_answer(self, svc):
        # 即便后续有 m2026，封存报表所依据的 m2025 结果保持不变
        r = svc.forward_convert("POS", "CUSTOMER_STATUS", "1",
                                date(2025, 3, 15), "v2025")
        assert r["mapping_version"] == "m2025"
        assert r["target_code"] == "ACTIVE"


# ------------------------------------------------------------- 5. 反向候选
class TestReverseLookup:
    def test_reverse_active_returns_multiple_candidates(self, svc):
        out = svc.reverse_lookup("ACTIVE", date(2024, 6, 1), "v2024")
        pairs = {(c["system_code"], c["source_code"]) for c in out["candidates"]}
        assert out["candidate_count"] >= 2
        assert ("CRM", "A") in pairs and ("POS", "1") in pairs
        assert ("ERP", "E10") in pairs
        assert out["mapping_version"] == "m2024"

    def test_reverse_dormant_follows_version_evolution(self, svc):
        # v2024 口径下 DORMANT 根本不存在 -> 无候选
        out_old = svc.reverse_lookup("DORMANT", date(2024, 6, 1), "v2024")
        assert out_old["candidate_count"] == 0
        # v2025 下有两个来源（POS:2 与 ERP:E30）
        out_new = svc.reverse_lookup("DORMANT", date(2025, 6, 1), "v2025")
        pairs = {(c["system_code"], c["source_code"]) for c in out_new["candidates"]}
        assert pairs == {("POS", "2"), ("ERP", "E30")}
        assert out_new["mapping_version"] == "m2025"

    def test_reverse_on_2026_version(self, svc):
        out = svc.reverse_lookup("PENDING", date(2026, 2, 1))
        assert out["mapping_version"] == "m2026"
        assert [(c["system_code"], c["source_code"])
                for c in out["candidates"]] == [("ERP", "E40")]


# ------------------------------------------------------------- 6. 覆盖率
class TestCoverage:
    def test_coverage_of_2024_includes_explicit_unmapped(self, svc):
        cov = svc.coverage("m2024")
        # 2024-01-01 生效源码：CRM 3 + ERP 3 + POS 3 = 9
        assert cov["active_source_codes"] == 9
        assert cov["explicit_unmapped"] == 2          # POS:2、ERP:E30
        assert cov["missing_mapping"] == 0
        assert cov["coverage_rate"] == 1.0
        reasons = {u["source_code"] for u in cov["unmapped_details"]}
        assert reasons == {"2", "E30"}

    def test_coverage_full_in_2026(self, svc):
        cov = svc.coverage("m2026")
        # 2026-01-01 生效源码：CRM 3 + ERP 3 + POS 3 = 9，全部有目标码
        assert cov["active_source_codes"] == 9
        assert cov["mapped"] == 9
        assert cov["explicit_unmapped"] == 0
        assert cov["missing_mapping"] == 0

    def test_bad_draft_shows_missing_in_coverage(self, svc):
        vid = _draft(svc, "v2026", "m2026_cov_bad", date(2026, 1, 1))
        rows = [r for r in _good_2026_rows()
                if not (r["system_code"] == "POS" and r["source_code"] == "2")]
        with svc.connect() as conn:
            for r in rows:
                svc.add_mapping_row(conn, vid, r)
        cov = svc.coverage("m2026_cov_bad")
        assert cov["missing_mapping"] == 1
        assert cov["missing_details"][0]["source_code"] == "2"
        assert cov["coverage_rate"] < 1.0
