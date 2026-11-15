"""失败类型与定位信息。

所有摄入失败都带捕获行号以及（消息内）字节偏移；能定位到 Set/记录/字段时也一并给出。
失败是“首错即停”：第一个无法解释的位置就是整份捕获失败的原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Failure:
    phase: str
    reason: str
    capture_line: Optional[int] = None
    message_offset: Optional[int] = None
    set_ordinal: Optional[int] = None
    record_ordinal: Optional[int] = None
    field_ordinal: Optional[int] = None
    detail: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "capture_line": self.capture_line,
            "message_offset": self.message_offset,
            "set_ordinal": self.set_ordinal,
            "record_ordinal": self.record_ordinal,
            "field_ordinal": self.field_ordinal,
            "detail": self.detail,
        }


class IpfixError(Exception):
    """本模块错误的基类。"""


class IpfixInputError(IpfixError):
    """捕获内容无法接入；携带第一处失败的精确定位。"""

    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(_format_failure(failure))

    @property
    def failures(self) -> list[Failure]:
        return [self.failure]

    def to_json(self) -> dict:
        return {"ok": False, "failures": [self.failure.to_json()]}


class TargetExistsError(IpfixError):
    """目标结果库已经存在；按约定拒绝覆盖。"""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"目标结果库已存在，拒绝覆盖：{path}")


class ConfigError(IpfixError, ValueError):
    """可配置上限/批次参数不是正整数。"""


class IpfixDatabaseError(IpfixError):
    """结果库结构或完整性不符合本模块约定。"""


def _format_failure(f: Failure) -> str:
    parts = [f"阶段={f.phase}"]
    if f.capture_line is not None:
        parts.append(f"捕获行={f.capture_line}")
    if f.message_offset is not None:
        parts.append(f"消息偏移={f.message_offset}")
    if f.set_ordinal is not None:
        parts.append(f"Set序号={f.set_ordinal}")
    if f.record_ordinal is not None:
        parts.append(f"Set内记录={f.record_ordinal}")
    if f.field_ordinal is not None:
        parts.append(f"字段序号={f.field_ordinal}")
    parts.append(f"原因：{f.reason}")
    if f.detail:
        parts.append(f"明细：{f.detail}")
    return "；".join(parts)


def failure(phase: str, reason: str, *,
            capture_line: Optional[int] = None,
            message_offset: Optional[int] = None,
            set_ordinal: Optional[int] = None,
            record_ordinal: Optional[int] = None,
            field_ordinal: Optional[int] = None,
            detail: Optional[str] = None) -> IpfixInputError:
    return IpfixInputError(Failure(
        phase=phase, reason=reason, capture_line=capture_line,
        message_offset=message_offset, set_ordinal=set_ordinal,
        record_ordinal=record_ordinal, field_ordinal=field_ordinal,
        detail=detail))
