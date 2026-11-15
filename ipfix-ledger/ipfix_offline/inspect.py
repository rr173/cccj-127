"""只解码、不落盘的接入侦察（演练/诊断用）。

返回每个捕获行的消息概况、每条 Data Record 的字段解码与模板依据，
不持有全部输入行、不写结果库。用于在正式 import_capture 之前核对夹具。
"""
from __future__ import annotations

from typing import Iterator, Optional

from .capture import iter_capture
from .protocol import MessageDecoder, ScopeRegistry
from . import values


def _field_json(f) -> dict:
    value = f.decoded_value
    if isinstance(value, bytes):
        value = value.hex()
    elif isinstance(value, int):
        value = str(value)
    prefix = f.var_prefix
    return {
        "field_ordinal": f.ordinal,
        "element_id": f.element_id,
        "name": values.name_for(f.element_id, f.enterprise),
        "enterprise": f.enterprise,
        "declared_length": f.declared_length,
        "actual_length": f.actual_length,
        "value_type": f.value_type,
        "value": value,
        "value_hex": f.value_hex,
        "value_range": [f.value_offset, f.value_offset + f.actual_length],
        "var_prefix_range": (
            [prefix.offset, prefix.offset + prefix.length] if prefix else None),
        "template_field_range": [
            f.template_field_offset,
            f.template_field_offset + f.template_field_length],
    }


def inspect_capture(path: str, *,
                    max_templates_per_scope: int = 1024,
                    max_lines: Optional[int] = None) -> list[dict]:
    registry = ScopeRegistry(max_templates_per_scope)
    decoder = MessageDecoder(registry)
    out: list[dict] = []
    for line in iter_capture(path):
        if max_lines is not None and line.line_no > max_lines:
            break
        msg = decoder.decode(line.line_no, line.exporter_id, line.transport_id,
                             line.message)
        out.append({
            "capture_line": msg.capture_line,
            "exporter_id": msg.exporter_id,
            "transport_id": msg.transport_id,
            "domain_id": msg.domain_id,
            "export_time": msg.export_time,
            "sequence_number": msg.sequence_number,
            "message_length": msg.length,
            "message_hex": msg.message_hex,
            "definitions": [
                {"template_id": d.template_id, "generation": d.generation,
                 "set_ordinal": d.set_ordinal, "record_offset": d.record_offset,
                 "fields": [
                     {"field_ordinal": f.ordinal,
                      "element_id": f.element_id,
                      "enterprise": f.enterprise,
                      "declared_length": f.length,
                      "spec_offset": f.spec_offset,
                      "spec_length": f.spec_length}
                     for f in d.fields]}
                for d in msg.definitions],
            "revocations": [
                {"template_id": r.template_id, "set_ordinal": r.set_ordinal,
                 "record_offset": r.record_offset}
                for r in msg.revocations],
            "records": [
                {"set_ordinal": r.set_ordinal,
                 "record_in_set": r.record_in_set,
                 "template_id": r.template_id,
                 "template_generation": r.template_generation,
                 "record_offset": r.record_offset,
                 "record_length": r.record_length,
                 "fields": [_field_json(f) for f in r.fields]}
                for r in msg.records],
        })
    return out


def iterate_capture(path: str, *,
                    max_templates_per_scope: int = 1024) -> Iterator[dict]:
    """流式版本，供大捕获逐行诊断；不缓存已产出的行。"""
    registry = ScopeRegistry(max_templates_per_scope)
    decoder = MessageDecoder(registry)
    for line in iter_capture(path):
        msg = decoder.decode(line.line_no, line.exporter_id, line.transport_id,
                             line.message)
        yield {
            "capture_line": msg.capture_line,
            "sequence_number": msg.sequence_number,
            "record_count": msg.record_count,
            "records": [
                {"template_id": r.template_id,
                 "template_generation": r.template_generation,
                 "set_ordinal": r.set_ordinal,
                 "record_in_set": r.record_in_set,
                 "fields": [_field_json(f) for f in r.fields]}
                for r in msg.records],
        }
