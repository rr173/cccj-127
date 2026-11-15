"""多业务系统代码表映射服务。

职责：
  * 映射版本的起草、发布前校验与发布（冲突一次性全部返回）
  * 报表封存（封存后映射版本不可改写，由数据库触发器强制）
  * 正向转换 / 反向候选查询 / 覆盖率核验，结果均带所采用的映射版本
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "host=/tmp port=55432 user=app dbname=postgres"
INF = date(9999, 12, 31)


class MappingError(Exception):
    """发布校验失败，携带全部冲突明细。"""

    def __init__(self, violations: list[dict[str, str]]):
        self.violations = violations
        super().__init__("; ".join(f"[{v['violation_code']}] {v['message']}"
                                   for v in violations))


def _to_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


class MappingService:
    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.environ.get("MAPPING_DSN", DEFAULT_DSN)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 发布
    def create_draft(self, conn: psycopg.Connection, target_version_label: str,
                     label: str, effective_from: date | str,
                     effective_to: Optional[date | str] = None) -> int:
        """在给定连接/事务中创建草稿映射版本，返回其 id。"""
        row = conn.execute(
            """
            INSERT INTO mapping_version(target_version_id, version_label,
                                        effective_from, effective_to, status)
            SELECT id, %s, %s, %s, 'draft'
              FROM target_version WHERE version_label = %s
            RETURNING id
            """,
            (label, _to_date(effective_from),
             _to_date(effective_to) if effective_to else None,
             target_version_label),
        ).fetchone()
        if row is None:
            raise LookupError(f"目标版本 {target_version_label} 不存在")
        return row["id"]

    def add_mapping_row(self, conn: psycopg.Connection, mapping_version_id: int,
                        row: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO mapping_row(mapping_version_id, system_code, code_domain,
                                    source_code, target_code, mapping_group_id,
                                    group_type, effective_from, effective_to,
                                    business_reason, approver)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (mapping_version_id, row["system_code"], row["code_domain"],
             row["source_code"], row.get("target_code"), row["mapping_group_id"],
             row["group_type"], _to_date(row["effective_from"]),
             _to_date(row["effective_to"]) if row.get("effective_to") else None,
             row["business_reason"], row["approver"]),
        )

    def publish(self, mapping_version_id: int) -> dict[str, Any]:
        """发布映射版本。校验失败时发布事务回滚（状态仍为 draft，
        草稿及其映射行保留，可修正后重新调用本方法），并抛出全部冲突。"""
        with self.connect() as conn:
            # 事务级咨询锁：同一时刻只允许一个发布事务执行
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (8801,))
            with conn.transaction():
                violations = conn.execute(
                    "SELECT violation_code, message "
                    "FROM validate_mapping_publish(%s) ORDER BY 1,2",
                    (mapping_version_id,),
                ).fetchall()
                if violations:
                    raise MappingError([dict(v) for v in violations])
                row = conn.execute(
                    """
                    UPDATE mapping_version
                       SET status='published', published_at=now()
                     WHERE id=%s AND status='draft'
                    RETURNING id, version_label
                    """,
                    (mapping_version_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"映射版本 {mapping_version_id} 不是草稿或不存在")
                return dict(row)

    def validate_only(self, mapping_version_id: int) -> list[dict[str, str]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT violation_code, message FROM validate_mapping_publish(%s) "
                "ORDER BY 1,2", (mapping_version_id,))]

    # ------------------------------------------------------------- 报表封存
    def seal_report(self, report_code: str, report_period: date | str,
                    mapping_version_label: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO report_snapshot(report_code, report_period, mapping_version_id)
                SELECT %s, %s, id FROM mapping_version
                 WHERE version_label=%s AND status='published'
                RETURNING id, report_code, report_period, mapping_version_id
                """,
                (report_code, _to_date(report_period), mapping_version_label),
            ).fetchone()
            if row is None:
                raise LookupError(
                    f"映射版本 {mapping_version_label} 不存在或尚未发布，不能封存")
            return dict(row)

    # ----------------------------------------------------------- 版本解析
    def _resolve_mapping_version(self, conn: psycopg.Connection,
                                 target_version_label: Optional[str],
                                 biz_date: date) -> dict[str, Any]:
        """返回事件时间点生效、已发布的映射版本；可显式指定目标版本。"""
        if target_version_label:
            row = conn.execute(
                """
                SELECT mv.id, mv.version_label, tv.version_label AS target_version_label
                  FROM mapping_version mv
                  JOIN target_version tv ON tv.id = mv.target_version_id
                 WHERE tv.version_label=%s AND mv.status='published'
                   AND mv.effective_from <= %s
                   AND %s < coalesce(mv.effective_to, %s)
                 ORDER BY mv.effective_from DESC LIMIT 1
                """,
                (target_version_label, biz_date, biz_date, INF),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT mv.id, mv.version_label, tv.version_label AS target_version_label
                  FROM mapping_version mv
                  JOIN target_version tv ON tv.id = mv.target_version_id
                 WHERE mv.status='published'
                   AND mv.effective_from <= %s
                   AND %s < coalesce(mv.effective_to, %s)
                 ORDER BY mv.effective_from DESC LIMIT 1
                """,
                (biz_date, biz_date, INF),
            ).fetchone()
        if row is None:
            raise LookupError(
                f"事件日期 {biz_date} 没有已发布的映射版本"
                + (f"（目标版本 {target_version_label}）" if target_version_label else ""))
        return dict(row)

    # -------------------------------------------------------------- 正向转换
    def forward_convert(self, system_code: str, code_domain: str, source_code: str,
                        biz_date: date | str,
                        target_version_label: Optional[str] = None) -> dict[str, Any]:
        biz_date = _to_date(biz_date)
        with self.connect() as conn:
            mv = self._resolve_mapping_version(conn, target_version_label, biz_date)

            source_valid = conn.execute(
                """
                SELECT 1 FROM source_code
                 WHERE system_code=%s AND code_domain=%s AND source_code=%s
                   AND effective_from <= %s
                   AND %s < coalesce(effective_to, %s)
                """,
                (system_code, code_domain, source_code,
                 biz_date, biz_date, INF),
            ).fetchone() is not None

            row = conn.execute(
                """
                SELECT target_code, mapping_group_id, group_type, business_reason,
                       approver, effective_from, effective_to
                  FROM mapping_row
                 WHERE mapping_version_id=%s AND system_code=%s AND code_domain=%s
                   AND source_code=%s
                   AND effective_from <= %s
                   AND %s < coalesce(effective_to, %s)
                 ORDER BY effective_from DESC LIMIT 1
                """,
                (mv["id"], system_code, code_domain, source_code,
                 biz_date, biz_date, INF),
            ).fetchone()

            result: dict[str, Any] = {
                "system_code": system_code,
                "code_domain": code_domain,
                "source_code": source_code,
                "biz_date": biz_date.isoformat(),
                "source_code_valid_at_biz_date": source_valid,
                "mapping_version": mv["version_label"],
                "target_version": mv["target_version_label"],
            }
            if row is None:
                result.update(status="NO_MAPPING", target_code=None,
                              mapping_group_id=None, group_type=None,
                              business_reason=None, approver=None)
                return result
            row = dict(row)
            row["effective_from"] = row["effective_from"].isoformat()
            row["effective_to"] = (row["effective_to"].isoformat()
                                   if row["effective_to"] else None)
            result.update(row)
            result["status"] = ("UNMAPPED" if row["target_code"] is None
                                else "MAPPED")
            return result

    # ------------------------------------------------------------- 反向候选
    def reverse_lookup(self, target_code: str, biz_date: date | str,
                       target_version_label: Optional[str] = None
                       ) -> dict[str, Any]:
        biz_date = _to_date(biz_date)
        with self.connect() as conn:
            mv = self._resolve_mapping_version(conn, target_version_label, biz_date)
            rows = conn.execute(
                """
                SELECT system_code, source_code, mapping_group_id, group_type,
                       business_reason, approver, effective_from, effective_to
                  FROM mapping_row
                 WHERE mapping_version_id=%s AND target_code=%s
                   AND effective_from <= %s
                   AND %s < coalesce(effective_to, %s)
                 ORDER BY system_code, source_code
                """,
                (mv["id"], target_code, biz_date, biz_date, INF),
            ).fetchall()
            candidates = []
            for r in rows:
                r = dict(r)
                r["effective_from"] = r["effective_from"].isoformat()
                r["effective_to"] = (r["effective_to"].isoformat()
                                      if r["effective_to"] else None)
                candidates.append(r)
            return {
                "target_code": target_code,
                "biz_date": biz_date.isoformat(),
                "mapping_version": mv["version_label"],
                "target_version": mv["target_version_label"],
                "candidate_count": len(candidates),
                "candidates": candidates,
            }

    # -------------------------------------------------------------- 覆盖率
    def coverage(self, mapping_version_label: str) -> dict[str, Any]:
        with self.connect() as conn:
            mv = conn.execute(
                """
                SELECT mv.id, mv.version_label, mv.effective_from,
                       tv.version_label AS target_version_label
                  FROM mapping_version mv
                  JOIN target_version tv ON tv.id = mv.target_version_id
                 WHERE mv.version_label=%s
                """,
                (mapping_version_label,),
            ).fetchone()
            if mv is None:
                raise LookupError(f"映射版本 {mapping_version_label} 不存在")
            mv = dict(mv)
            at = mv["effective_from"]

            active_sources = conn.execute(
                """
                SELECT system_code, code_domain, source_code
                  FROM source_code
                 WHERE effective_from <= %s
                   AND %s < coalesce(effective_to, %s)
                 ORDER BY system_code, source_code
                """,
                (at, at, INF),
            ).fetchall()

            covered = conn.execute(
                """
                SELECT DISTINCT system_code, code_domain, source_code
                  FROM mapping_row
                 WHERE mapping_version_id=%s
                   AND effective_from < coalesce(effective_to, %s)
                   AND %s < coalesce(effective_to, %s)
                """,
                (mv["id"], INF, at, INF),
            ).fetchall()
            covered_set = {(r["system_code"], r["code_domain"], r["source_code"])
                           for r in covered}

            unmapped = conn.execute(
                """
                SELECT system_code, source_code, business_reason, approver
                  FROM mapping_row
                 WHERE mapping_version_id=%s AND group_type='UNMAPPED'
                   AND effective_from < coalesce(effective_to, %s)
                   AND %s < coalesce(effective_to, %s)
                 ORDER BY system_code, source_code
                """,
                (mv["id"], INF, at, INF),
            ).fetchall()

            missing = [dict(s) for s in active_sources
                       if (s["system_code"], s["code_domain"], s["source_code"])
                       not in covered_set]
            total = len(active_sources)
            mapped_count = total - len(missing) - len(unmapped)
            return {
                "mapping_version": mv["version_label"],
                "target_version": mv["target_version_label"],
                "evaluated_at": at.isoformat(),
                "active_source_codes": total,
                "mapped": mapped_count,
                "explicit_unmapped": len(unmapped),
                "missing_mapping": len(missing),
                "coverage_rate": round((total - len(missing)) / total, 4)
                                  if total else 1.0,
                "unmapped_details": [dict(u) for u in unmapped],
                "missing_details": missing,
            }
