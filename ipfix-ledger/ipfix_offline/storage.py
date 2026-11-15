"""结果库（SQLite）的写入与查询。

结果库保存：
  * messages：每行一条原始 IPFIX Message（原 hex）及消息头；
  * template_events / template_fields：每次定义/撤销的位置、代次与字段说明；
  * records / record_fields：每条 Data Record 及每个字段的声明/实际长度、
    原始 hex、解码值、载荷在消息内的字节区间、模板字段说明位置。

模板状态只在导入过程中存在于 Python（当前有效模板 + 每号下一个代次）；
已解码记录和原始消息逐行落盘，不常驻内存。导入先写同目录临时库，全部校验
通过后原子链接为最终文件；失败删除临时库，目标库不可打开。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass

from .errors import (
    ConfigError,
    IpfixDatabaseError,
    TargetExistsError,
    failure,
)
from .protocol import ScopeRegistry

SCHEMA_VERSION = "1"
DEFAULT_BATCH_SIZE = 2000
DEFAULT_CACHE_KB = 8192

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE messages (
    capture_line    INTEGER PRIMARY KEY,
    exporter_id     TEXT NOT NULL,
    transport_id    TEXT NOT NULL,
    domain_id       INTEGER NOT NULL,
    export_time     INTEGER NOT NULL,
    sequence_number INTEGER NOT NULL,
    message_length  INTEGER NOT NULL,
    record_count    INTEGER NOT NULL,
    message_hex     TEXT NOT NULL
);
CREATE INDEX messages_scope ON messages(exporter_id, transport_id, domain_id);

CREATE TABLE template_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_line  INTEGER NOT NULL,
    set_ordinal   INTEGER NOT NULL,
    record_offset INTEGER NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('define', 'revoke')),
    exporter_id   TEXT NOT NULL,
    transport_id  TEXT NOT NULL,
    domain_id     INTEGER NOT NULL,
    template_id   INTEGER NOT NULL,
    generation    INTEGER,
    field_count   INTEGER NOT NULL
);
CREATE INDEX template_events_scope
    ON template_events(exporter_id, transport_id, domain_id, template_id);
CREATE INDEX template_events_id ON template_events(template_id);

CREATE TABLE template_fields (
    event_id        INTEGER NOT NULL REFERENCES template_events(event_id),
    ordinal         INTEGER NOT NULL,
    element_id      INTEGER NOT NULL,
    enterprise      INTEGER NOT NULL,
    declared_length INTEGER NOT NULL,
    spec_offset     INTEGER NOT NULL,
    spec_length     INTEGER NOT NULL,
    PRIMARY KEY (event_id, ordinal)
);

CREATE TABLE records (
    record_ordinal      INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_line        INTEGER NOT NULL,
    exporter_id         TEXT NOT NULL,
    transport_id        TEXT NOT NULL,
    domain_id           INTEGER NOT NULL,
    set_ordinal         INTEGER NOT NULL,
    record_in_set       INTEGER NOT NULL,
    template_id         INTEGER NOT NULL,
    template_event_id   INTEGER NOT NULL REFERENCES template_events(event_id),
    template_generation INTEGER NOT NULL,
    record_offset       INTEGER NOT NULL,
    record_length       INTEGER NOT NULL
);
CREATE INDEX records_capture ON records(capture_line, set_ordinal, record_in_set);
CREATE INDEX records_scope ON records(exporter_id, transport_id, domain_id);
CREATE INDEX records_template ON records(template_id);

CREATE VIEW record_view AS
SELECT r.record_ordinal, r.capture_line, r.exporter_id, r.transport_id,
       r.domain_id, m.export_time, m.sequence_number, m.message_length,
       m.message_hex, r.set_ordinal, r.record_in_set, r.template_id,
       r.template_event_id, r.template_generation, r.record_offset,
       r.record_length
  FROM records r JOIN messages m ON m.capture_line = r.capture_line;

CREATE TABLE record_fields (
    record_ordinal        INTEGER NOT NULL REFERENCES records(record_ordinal),
    field_ordinal         INTEGER NOT NULL,
    element_id            INTEGER NOT NULL,
    enterprise            INTEGER NOT NULL,
    declared_length       INTEGER NOT NULL,
    actual_length         INTEGER NOT NULL,
    value_offset          INTEGER NOT NULL,
    value_hex             TEXT NOT NULL,
    var_prefix_offset     INTEGER,
    var_prefix_length     INTEGER,
    var_prefix_extended   INTEGER,
    value_type            TEXT NOT NULL,
    template_field_offset INTEGER NOT NULL,
    template_field_length INTEGER NOT NULL,
    PRIMARY KEY (record_ordinal, field_ordinal)
);
CREATE INDEX record_fields_element
    ON record_fields(element_id, enterprise);
"""


def _positive_int(name: str, value, allow_zero: bool = False) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < (0 if allow_zero else 1)):
        raise ConfigError(f"{name} 必须是{'非负' if allow_zero else '正'}整数")
    return value


@dataclass
class ImportReport:
    db_path: str
    capture_path: str
    capture_lines: int
    messages: int
    records: int
    template_definitions: int
    template_revocations: int
    template_scopes: int
    distinct_templates: int
    capture_bytes: int
    db_bytes: int
    batch_size: int
    cache_kb: int
    sqlite_version: str
    elapsed_seconds: float
    peak_rss_kb: int
    rss_baseline_kb: int

    def to_json(self) -> dict:
        return {
            "ok": True,
            "db_path": self.db_path,
            "capture_path": self.capture_path,
            "capture_lines": self.capture_lines,
            "messages": self.messages,
            "records": self.records,
            "template_definitions": self.template_definitions,
            "template_revocations": self.template_revocations,
            "template_scopes": self.template_scopes,
            "distinct_templates": self.distinct_templates,
            "capture_bytes": self.capture_bytes,
            "db_bytes": self.db_bytes,
            "batch_size": self.batch_size,
            "cache_kb": self.cache_kb,
            "sqlite_version": self.sqlite_version,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "peak_rss_kb": self.peak_rss_kb,
            "rss_baseline_kb": self.rss_baseline_kb,
        }


def _rss_kb() -> int:
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _new_staging_path(final_path: str) -> str:
    directory = os.path.dirname(os.path.abspath(final_path)) or "."
    fd, path = tempfile.mkstemp(prefix=".ipfix-staging-", suffix=".db",
                                dir=directory)
    os.close(fd)
    os.unlink(path)  # sqlite 需要自己创建；写路径仍然由本进程独占
    return path


def import_capture(input_path: str | os.PathLike,
                   db_path: str | os.PathLike,
                   *,
                   batch_size: int = DEFAULT_BATCH_SIZE,
                   max_templates_per_scope: int = 1024,
                   cache_kb: int = DEFAULT_CACHE_KB) -> ImportReport:
    """把一份捕获严格接入为新结果库。失败抛 IpfixInputError，且不留下目标库。"""
    batch_size = _positive_int("batch_size", batch_size)
    max_templates_per_scope = _positive_int(
        "max_templates_per_scope", max_templates_per_scope)
    cache_kb = _positive_int("cache_kb", cache_kb)

    input_path = os.fspath(input_path)
    db_path = os.fspath(db_path)
    if os.path.exists(db_path):
        raise TargetExistsError(db_path)
    directory = os.path.dirname(os.path.abspath(db_path))
    if not os.path.isdir(directory):
        raise ConfigError(f"目标目录不存在：{directory}")

    capture_bytes = os.path.getsize(input_path)
    staging = _new_staging_path(db_path)
    rss_baseline = _rss_kb()
    started = _now()
    try:
        report = _run_import(
            input_path, staging, batch_size, max_templates_per_scope,
            cache_kb, capture_bytes)
        # 硬链接在同目录内原子创建且不覆盖已有名字；即使检查后有人抢先创建
        # 目标文件，这里仍会失败而不是覆盖。
        try:
            os.link(staging, db_path)
        except FileExistsError:
            raise TargetExistsError(db_path) from None
        os.unlink(staging)
        staging = None
        report.db_path = db_path
        report.elapsed_seconds = _now() - started
        report.db_bytes = os.path.getsize(db_path)
        report.peak_rss_kb = _rss_kb()
        report.rss_baseline_kb = rss_baseline
        return report
    finally:
        if staging is not None:
            try:
                os.unlink(staging)
            except FileNotFoundError:
                pass


def _now() -> float:
    import time
    return time.monotonic()


def _run_import(input_path: str, staging: str, batch_size: int,
                max_templates: int, cache_kb: int,
                capture_bytes: int) -> ImportReport:
    from .capture import iter_capture
    from .protocol import MessageDecoder

    rss_baseline = _rss_kb()
    conn = sqlite3.connect(staging)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(f"PRAGMA cache_size=-{int(cache_kb)}")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(_SCHEMA)
        for key, value in (
                ("schema_version", SCHEMA_VERSION),
                ("batch_size", str(batch_size)),
                ("cache_kb", str(cache_kb)),
                ("max_templates_per_scope", str(max_templates))):
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)",
                         (key, value))
        conn.commit()

        registry = ScopeRegistry(max_templates)
        decoder = MessageDecoder(registry)
        # (scope, template_id) -> 当前有效模板在本库中的 event_id
        active_events: dict[tuple, int] = {}
        line_events: dict[tuple, int] = {}
        counters = {"lines": 0, "messages": 0, "records": 0,
                    "definitions": 0, "revocations": 0}
        # 字段插入批次：一批最多 batch_size 个字段行；每条记录本身立即写入
        # （需要自增 record_ordinal），字段行攒到 batch_size 后 executemany。
        field_batch: list[tuple] = []

        def flush_fields() -> None:
            if field_batch:
                conn.executemany(
                    "INSERT INTO record_fields(record_ordinal, field_ordinal, "
                    "element_id, enterprise, declared_length, actual_length, "
                    "value_offset, value_hex, var_prefix_offset, "
                    "var_prefix_length, var_prefix_extended, value_type, "
                    "template_field_offset, template_field_length) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", field_batch)
                field_batch.clear()

        for line in iter_capture(input_path):
            counters["lines"] += 1
            message = decoder.decode(line.line_no, line.exporter_id,
                                     line.transport_id, line.message)
            counters["messages"] += 1
            scope_key = (line.exporter_id, line.transport_id,
                         message.domain_id)

            # 1. 本行模板事件先落库，使记录可引用 event_id，历史位置永不被改写。
            changes: list[tuple[int, int, str, object]] = []
            for d in message.definitions:
                changes.append((d.set_ordinal, d.record_offset, "define", d))
            for r in message.revocations:
                changes.append((r.set_ordinal, r.record_offset, "revoke", r))
            changes.sort(key=lambda item: (item[0], item[1], item[2]))
            for _set_ord, _offset, action, obj in changes:
                if action == "define":
                    d = obj  # type: TemplateDef
                    cur = conn.execute(
                        "INSERT INTO template_events(capture_line, set_ordinal, "
                        "record_offset, action, exporter_id, transport_id, "
                        "domain_id, template_id, generation, field_count) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (d.capture_line, d.set_ordinal, d.record_offset,
                         "define", line.exporter_id, line.transport_id,
                         message.domain_id, d.template_id, d.generation,
                         len(d.fields)))
                    event_id = cur.lastrowid
                    if d.fields:
                        conn.executemany(
                            "INSERT INTO template_fields(event_id, ordinal, "
                            "element_id, enterprise, declared_length, "
                            "spec_offset, spec_length) VALUES(?,?,?,?,?,?,?)",
                            [(event_id, f.ordinal, f.element_id, f.enterprise,
                              f.length, f.spec_offset, f.spec_length)
                             for f in d.fields])
                    active_events[(scope_key, d.template_id)] = event_id
                    line_events[(scope_key, d.template_id,
                                 d.generation)] = event_id
                    counters["definitions"] += 1
                else:
                    r = obj  # type: Revocation
                    conn.execute(
                        "INSERT INTO template_events(capture_line, set_ordinal, "
                        "record_offset, action, exporter_id, transport_id, "
                        "domain_id, template_id, generation, field_count) "
                        "VALUES(?,?,?,?,?,?,?,?,NULL,0)",
                        (r.capture_line, r.set_ordinal, r.record_offset,
                         "revoke", line.exporter_id, line.transport_id,
                         message.domain_id, r.template_id))
                    active_events.pop((scope_key, r.template_id), None)
                    counters["revocations"] += 1

            # 2. 原始消息；记录逐条落盘、字段按批次执行 executemany。
            conn.execute(
                "INSERT INTO messages(capture_line, exporter_id, transport_id, "
                "domain_id, export_time, sequence_number, message_length, "
                "record_count, message_hex) VALUES(?,?,?,?,?,?,?,?,?)",
                (line.line_no, line.exporter_id, line.transport_id,
                 message.domain_id, message.export_time,
                 message.sequence_number, message.length,
                 message.record_count, message.message_hex))
            for rec in message.records:
                key = (scope_key, rec.template_id, rec.template_generation)
                event_id = line_events.get(key)
                if event_id is None:
                    event_id = active_events.get(
                        (scope_key, rec.template_id))
                if event_id is None:
                    raise failure("internal", "记录找不到模板事件",
                                  capture_line=line.line_no,
                                  detail=f"template={rec.template_id} "
                                         f"generation={rec.template_generation}")
                record_ordinal = _insert_record(conn, (
                    line.line_no, line.exporter_id, line.transport_id,
                    message.domain_id, rec.set_ordinal, rec.record_in_set,
                    rec.template_id, event_id, rec.template_generation,
                    rec.record_offset, rec.record_length))
                if rec.fields:
                    field_batch.extend(
                        (record_ordinal, f.ordinal, f.element_id,
                         f.enterprise, f.declared_length, f.actual_length,
                         f.value_offset, f.value_hex,
                         f.var_prefix.offset if f.var_prefix else None,
                         f.var_prefix.length if f.var_prefix else None,
                         int(f.var_prefix.extended) if f.var_prefix else None,
                         f.value_type, f.template_field_offset,
                         f.template_field_length)
                        for f in rec.fields)
                    if len(field_batch) >= batch_size:
                        flush_fields()
                counters["records"] += 1
            flush_fields()
            conn.commit()
            line_events.clear()

        flush_fields()

        # 3. 摘要与完整性检查；任何失败都删除临时库，目标库不会被创建。
        for key, value in (
                ("capture_lines", counters["lines"]),
                ("record_count", counters["records"]),
                ("message_count", counters["messages"]),
                ("template_scopes", len(registry.all_scopes()))):
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)",
                         (key, str(value)))
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise IpfixDatabaseError(f"结果库完整性检查失败：{integrity}")
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM records), "
            "(SELECT count(*) FROM template_events WHERE action='define'), "
            "(SELECT count(*) FROM template_events WHERE action='revoke')"
        ).fetchone()
        if counts != (counters["records"], counters["definitions"],
                      counters["revocations"]):
            raise IpfixDatabaseError(
                f"落盘计数不一致：{counts} vs {counters}")
        distinct = conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT exporter_id, transport_id, "
            "domain_id, template_id FROM template_events "
            "WHERE action='define')").fetchone()[0]
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    return ImportReport(
        db_path=staging, capture_path=input_path,
        capture_lines=counters["lines"], messages=counters["messages"],
        records=counters["records"],
        template_definitions=counters["definitions"],
        template_revocations=counters["revocations"],
        template_scopes=len(registry.all_scopes()),
        distinct_templates=distinct,
        capture_bytes=capture_bytes, db_bytes=0,
        batch_size=batch_size, cache_kb=cache_kb,
        sqlite_version=sqlite3.sqlite_version,
        elapsed_seconds=0.0, peak_rss_kb=0,
        rss_baseline_kb=rss_baseline)


def _insert_record(conn: sqlite3.Connection, row: tuple) -> int:
    cur = conn.execute(
        "INSERT INTO records(capture_line, exporter_id, transport_id, "
        "domain_id, set_ordinal, record_in_set, template_id, "
        "template_event_id, template_generation, record_offset, "
        "record_length) VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    return cur.lastrowid
