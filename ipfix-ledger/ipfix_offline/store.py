"""结果库的只读查询：消息、记录、字段追查。

所有查询只使用结果库本身：设备/会话/domain、捕获行号、模板号都在库中可直接读，
不依赖导入进程内的任何状态。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from . import values
from .errors import IpfixDatabaseError
from .storage import SCHEMA_VERSION


class ResultStore:
    """以只读方式打开结果库；每个公开方法打开自己的短连接。"""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"结果库不存在：{self.path}")
        self._verify()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _verify(self) -> None:
        with self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise IpfixDatabaseError(
                    f"不是本模块的结果库：{self.path}（{exc}）") from None
            if row is None or row["value"] != SCHEMA_VERSION:
                raise IpfixDatabaseError(
                    f"结果库结构版本不符：{self.path}"
                    f"（期望 {SCHEMA_VERSION}）")

    # ------------------------------------------------------------ 记录查询
    def query_records(self,
                      *,
                      exporter_id: Optional[str] = None,
                      transport_id: Optional[str] = None,
                      domain_id: Optional[int] = None,
                      capture_line: Optional[int] = None,
                      template_id: Optional[int] = None,
                      limit: Optional[int] = None,
                      offset: int = 0) -> list[dict]:
        clauses, params = [], []
        for column, value in (
                ("e.exporter_id", exporter_id),
                ("e.transport_id", transport_id),
                ("e.domain_id", domain_id),
                ("e.capture_line", capture_line),
                ("e.template_id", template_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT e.record_ordinal, e.capture_line, e.exporter_id, "
            "e.transport_id, e.domain_id, e.export_time, e.sequence_number, "
            "e.message_length, e.set_ordinal, e.record_in_set, e.template_id, "
            "e.template_event_id, e.template_generation, e.record_offset, "
            "e.record_length FROM record_view e" + where +
            " ORDER BY e.capture_line, e.set_ordinal, e.record_in_set")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._record_dict(conn, row) for row in rows]

    def _record_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        fields = self._fields_for(conn, row["record_ordinal"])
        return {
            "record_ordinal": row["record_ordinal"],
            "capture_line": row["capture_line"],
            "exporter_id": row["exporter_id"],
            "transport_id": row["transport_id"],
            "domain_id": row["domain_id"],
            "export_time": row["export_time"],
            "sequence_number": row["sequence_number"],
            "message_length": row["message_length"],
            "set_ordinal": row["set_ordinal"],
            "record_in_set": row["record_in_set"],
            "template_id": row["template_id"],
            "template_event_id": row["template_event_id"],
            "template_generation": row["template_generation"],
            "record_offset": row["record_offset"],
            "record_length": row["record_length"],
            "fields": fields,
        }

    def _fields_for(self, conn: sqlite3.Connection,
                    record_ordinal: int) -> list[dict]:
        rows = conn.execute(
            "SELECT field_ordinal, element_id, enterprise, declared_length, "
            "actual_length, value_offset, value_hex, var_prefix_offset, "
            "var_prefix_length, var_prefix_extended, value_type, "
            "template_field_offset, template_field_length "
            "FROM record_fields WHERE record_ordinal=? "
            "ORDER BY field_ordinal", (record_ordinal,)).fetchall()
        out = []
        for f in rows:
            value_type = f["value_type"]
            raw = bytes.fromhex(f["value_hex"])
            if value_type == "raw_bytes":
                decoded = f["value_hex"]
            elif value_type == "ipv4":
                decoded = ".".join(str(b) for b in raw)
            elif value_type == "ipv6":
                decoded = values.format_ipv6(raw)
            else:
                # 整数（含 UTC 毫秒、2^64-1）用十进制字符串
                decoded = str(int.from_bytes(raw, "big"))
            out.append({
                "field_ordinal": f["field_ordinal"],
                "element_id": f["element_id"],
                "name": values.name_for(f["element_id"], f["enterprise"]),
                "enterprise": f["enterprise"],
                "declared_length": f["declared_length"],
                "actual_length": f["actual_length"],
                "value_offset": f["value_offset"],
                "value_range": [f["value_offset"],
                                f["value_offset"] + f["actual_length"]],
                "value_hex": f["value_hex"],
                "value_type": value_type,
                "value": decoded,
                "var_prefix_offset": f["var_prefix_offset"],
                "var_prefix_length": f["var_prefix_length"],
                "var_prefix_extended": f["var_prefix_extended"],
                "var_prefix_range": (
                    [f["var_prefix_offset"],
                     f["var_prefix_offset"] + f["var_prefix_length"]]
                    if f["var_prefix_offset"] is not None else None),
                "template_field_offset": f["template_field_offset"],
                "template_field_length": f["template_field_length"],
                "template_field_range": [
                    f["template_field_offset"],
                    f["template_field_offset"] + f["template_field_length"]],
            })
        return out

    # ------------------------------------------------------------ 单记录
    def get_record(self, record_ordinal: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT e.* FROM record_view e WHERE e.record_ordinal=?",
                (record_ordinal,)).fetchone()
            return self._record_dict(conn, row) if row else None

    # ------------------------------------------------------------ 原始消息
    def get_message(self, capture_line: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT capture_line, exporter_id, transport_id, domain_id, "
                "export_time, sequence_number, message_length, record_count, "
                "message_hex FROM messages WHERE capture_line=?",
                (capture_line,)).fetchone()
            if row is None:
                return None
            record_ordinals = [
                r["record_ordinal"] for r in conn.execute(
                    "SELECT record_ordinal FROM records WHERE capture_line=? "
                    "ORDER BY set_ordinal, record_in_set", (capture_line,))]
            return {**dict(row), "record_ordinals": record_ordinals}

    # ------------------------------------------------------------ 模板
    def get_template(self, exporter_id: str, transport_id: str,
                     domain_id: int, template_id: int,
                     generation: Optional[int] = None) -> Optional[dict]:
        with self._connect() as conn:
            return self._template_dict(conn, exporter_id, transport_id,
                                       domain_id, template_id, generation)

    def _template_dict(self, conn, exporter_id, transport_id, domain_id,
                       template_id, generation=None) -> Optional[dict]:
        sql = ("SELECT * FROM template_events WHERE exporter_id=? AND "
               "transport_id=? AND domain_id=? AND template_id=? "
               "AND action='define'")
        params = [exporter_id, transport_id, domain_id, template_id]
        if generation is not None:
            sql += " AND generation=?"
            params.append(generation)
        sql += " ORDER BY capture_line, set_ordinal, record_offset"
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return None
        row = rows[-1] if generation is None else rows[0]
        return self._template_event_dict(conn, row)

    def _template_event_dict(self, conn, row) -> dict:
        fields = conn.execute(
            "SELECT ordinal, element_id, enterprise, declared_length, "
            "spec_offset, spec_length FROM template_fields "
            "WHERE event_id=? ORDER BY ordinal", (row["event_id"],)).fetchall()
        return {
            "event_id": row["event_id"],
            "action": row["action"],
            "capture_line": row["capture_line"],
            "set_ordinal": row["set_ordinal"],
            "record_offset": row["record_offset"],
            "exporter_id": row["exporter_id"],
            "transport_id": row["transport_id"],
            "domain_id": row["domain_id"],
            "template_id": row["template_id"],
            "generation": row["generation"],
            "fields": [
                {"field_ordinal": f["ordinal"],
                 "element_id": f["element_id"],
                 "name": values.name_for(f["element_id"], f["enterprise"]),
                 "enterprise": f["enterprise"],
                 "declared_length": f["declared_length"],
                 "spec_offset": f["spec_offset"],
                 "spec_length": f["spec_length"]}
                for f in fields],
        }

    def list_template_events(self, *, exporter_id: Optional[str] = None,
                             transport_id: Optional[str] = None,
                             domain_id: Optional[int] = None,
                             template_id: Optional[int] = None) -> list[dict]:
        clauses, params = ["1=1"], []
        for column, value in (
                ("exporter_id", exporter_id),
                ("transport_id", transport_id),
                ("domain_id", domain_id),
                ("template_id", template_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM template_events WHERE " + " AND ".join(clauses) +
                " ORDER BY capture_line, set_ordinal, record_offset", params
            ).fetchall()
            return [self._template_event_dict(conn, r) for r in rows]

    # ------------------------------------------------------ 字段追查
    def trace_field(self, record_ordinal: int, field_ordinal: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT e.record_ordinal, e.capture_line, e.exporter_id, "
                "e.transport_id, e.domain_id, e.template_id, "
                "e.template_event_id, e.template_generation, e.message_hex, "
                "f.field_ordinal, f.element_id, f.enterprise, "
                "f.declared_length, f.actual_length, f.value_offset, "
                "f.value_hex, f.var_prefix_offset, f.var_prefix_length, "
                "f.var_prefix_extended, f.value_type, "
                "f.template_field_offset, f.template_field_length "
                "FROM record_view e JOIN record_fields f "
                "ON f.record_ordinal=e.record_ordinal "
                "WHERE e.record_ordinal=? AND f.field_ordinal=?",
                (record_ordinal, field_ordinal)).fetchone()
            if row is None:
                raise LookupError(
                    f"记录 {record_ordinal} 的第 {field_ordinal} 个字段不存在")
            message = bytes.fromhex(row["message_hex"])
            start = row["value_offset"]
            end = start + row["actual_length"]
            sliced = message[start:end]
            if sliced.hex() != row["value_hex"]:
                raise IpfixDatabaseError(
                    f"追查切片与保存的字段字节不一致："
                    f"记录 {record_ordinal} 字段 {field_ordinal}")
            prefix_offset = row["var_prefix_offset"]
            prefix_length = row["var_prefix_length"]
            if prefix_offset is None:
                prefix_range = None
            else:
                prefix_range = [prefix_offset, prefix_offset + prefix_length]
            # 模板依据：该记录所用代次的定义事件 + 该字段的说明位置
            template = self._event_by_id(conn, row["template_event_id"])
            spec = conn.execute(
                "SELECT spec_offset, spec_length FROM template_fields "
                "WHERE event_id=? AND ordinal=?",
                (row["template_event_id"], field_ordinal)).fetchone()
            template_message = conn.execute(
                "SELECT message_hex FROM messages WHERE capture_line=?",
                (template["capture_line"],)).fetchone()
            template_hex = (template_message["message_hex"]
                            if template_message else None)
            template_slice = None
            if spec is not None and template_hex is not None:
                tbytes = bytes.fromhex(template_hex)
                template_slice = tbytes[
                    spec["spec_offset"]:spec["spec_offset"] + spec["spec_length"]].hex()
            return {
                "record_ordinal": row["record_ordinal"],
                "capture_line": row["capture_line"],
                "exporter_id": row["exporter_id"],
                "transport_id": row["transport_id"],
                "domain_id": row["domain_id"],
                "template_id": row["template_id"],
                "template_event_id": row["template_event_id"],
                "template_generation": row["template_generation"],
                "template_event": template,
                "template_capture_line": template["capture_line"],
                "template_sliced_hex": template_slice,
                "field_ordinal": row["field_ordinal"],
                "element_id": row["element_id"],
                "name": values.name_for(row["element_id"], row["enterprise"]),
                "enterprise": row["enterprise"],
                "declared_length": row["declared_length"],
                "actual_length": row["actual_length"],
                "value_type": row["value_type"],
                "value_hex": row["value_hex"],
                "value_range": [start, end],
                "var_prefix_range": prefix_range,
                "var_prefix_extended": row["var_prefix_extended"],
                "template_field_range": [
                    row["template_field_offset"],
                    row["template_field_offset"] + row["template_field_length"]],
                "template_field_spec": dict(spec) if spec else None,
                "sliced_hex": sliced.hex(),
            }

    def _event_by_id(self, conn, event_id: int) -> dict:
        row = conn.execute(
            "SELECT * FROM template_events WHERE event_id=?",
            (event_id,)).fetchone()
        return self._template_event_dict(conn, row)

    # ------------------------------------------------------------ 摘要
    def summary(self) -> dict:
        with self._connect() as conn:
            one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
            return {
                "messages": one("SELECT count(*) FROM messages"),
                "records": one("SELECT count(*) FROM records"),
                "fields": one("SELECT count(*) FROM record_fields"),
                "template_definitions": one(
                    "SELECT count(*) FROM template_events WHERE action='define'"),
                "template_revocations": one(
                    "SELECT count(*) FROM template_events WHERE action='revoke'"),
                "scopes": one(
                    "SELECT count(*) FROM (SELECT DISTINCT exporter_id, "
                    "transport_id, domain_id FROM messages)"),
                "first_record_ordinal": one(
                    "SELECT min(record_ordinal) FROM records"),
                "last_record_ordinal": one(
                    "SELECT max(record_ordinal) FROM records"),
                "first_record": self._edge_record(conn, "ASC"),
                "last_record": self._edge_record(conn, "DESC"),
            }

    def _edge_record(self, conn, direction: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT record_ordinal FROM records ORDER BY record_ordinal " +
            direction + " LIMIT 1").fetchone()
        if row is None:
            return None
        full = conn.execute(
            "SELECT * FROM record_view WHERE record_ordinal=?",
            (row["record_ordinal"],)).fetchone()
        if full is None:
            return None
        fields = self._fields_for(conn, row["record_ordinal"])
        return {"record_ordinal": row["record_ordinal"],
                "capture_line": full["capture_line"],
                "sequence_number": full["sequence_number"],
                "template_id": full["template_id"],
                "template_generation": full["template_generation"],
                "fields": fields}
