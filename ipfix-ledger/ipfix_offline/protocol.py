"""IPFIX v10 约定子集的字节级编解码。

只实现任务约定的子集：
  * 16 字节消息头（大端 version/length/exportTime/sequenceNumber/
    observationDomainId），version=10，length 必须等于本行真实字节数；
  * Set 头 4 字节，setLength 含头、>=4、完全落在消息内且不跨消息；
  * setId 2 = Template Set；setId 3（Options Template）与 0/1 明确拒绝；
  * Set 末尾只允许 0..3 个零字节填充；Template Set 同样适用。

模板定义是作用域内、位置相关的状态：
  * 作用域 = (exporter_id, transport_id, observationDomainId)；
  * 定义从所在位置起生效，同号新定义立即替换并产生新代次；
  * fieldCount=0 撤销确切模板号；
  * Data Set 只能使用此前同作用域仍有效的模板。

本模块不扫描“后面的字节猜边界”，未知 setId/未知模板/截断都在发现处报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import values
from .errors import Failure, IpfixInputError

HEADER_LEN = 16
VERSION = 10

SET_HEADER_LEN = 4
TEMPLATE_SET_ID = 2
OPTIONS_TEMPLATE_SET_ID = 3
FIRST_DATA_SET_ID = 256

VAR_LENGTH = 65535


@dataclass(frozen=True)
class FieldSpec:
    ordinal: int
    element_id: int
    enterprise: int
    length: int
    spec_offset: int = 0
    spec_length: int = 4

    @property
    def variable(self) -> bool:
        return self.length == VAR_LENGTH


@dataclass(frozen=True)
class TemplateDef:
    template_id: int
    generation: int
    fields: tuple[FieldSpec, ...]
    capture_line: int
    set_ordinal: int
    set_offset: int
    record_offset: int
    template_set_id: int = TEMPLATE_SET_ID

    @property
    def minimum_record_length(self) -> int:
        return sum(1 if f.variable else f.length for f in self.fields)


@dataclass(frozen=True)
class Revocation:
    template_id: int
    capture_line: int
    set_ordinal: int
    set_offset: int
    record_offset: int


@dataclass
class VarPrefix:
    offset: int
    length: int
    extended: bool
    actual_length: int


@dataclass
class DecodedField:
    ordinal: int
    element_id: int
    enterprise: int
    declared_length: int
    actual_length: int
    value_offset: int
    value_hex: str
    var_prefix: Optional[VarPrefix]
    value_type: str
    decoded_value: object
    template_field_offset: int = 0
    template_field_length: int = 4


@dataclass
class DecodedRecord:
    set_ordinal: int
    record_in_set: int
    template_id: int
    template_generation: int
    record_offset: int
    record_length: int
    fields: list[DecodedField]

    @property
    def record_ordinal(self) -> int:
        return self.record_in_set


@dataclass
class DecodedMessage:
    capture_line: int
    exporter_id: str
    transport_id: str
    domain_id: int
    export_time: int
    sequence_number: int
    length: int
    message_hex: str
    records: list[DecodedRecord] = field(default_factory=list)
    definitions: list[TemplateDef] = field(default_factory=list)
    revocations: list[Revocation] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.records)


class ScopeState:
    """一个 (exporter, transport, domain) 作用域的模板状态。

    只保存当前有效模板和每个模板号的下一个代次；历史定义/撤销位置随行落盘，
    业务记录与原始消息不在此常驻。
    """

    __slots__ = ("_active", "_generations", "max_templates")

    def __init__(self, max_templates: int):
        self._active: dict[int, TemplateDef] = {}
        # 模板号 -> 下一个代次。历史定义/撤销位置已随每行落盘，不在这里累积。
        self._generations: dict[int, int] = {}
        self.max_templates = max_templates

    # ------------------------------------------------------------ 查询
    def active(self, template_id: int) -> Optional[TemplateDef]:
        return self._active.get(template_id)

    def active_count(self) -> int:
        return len(self._active)

    def has_template(self, template_id: int) -> bool:
        return template_id in self._active

    def next_generation(self, template_id: int) -> int:
        """该模板号在本作用域的下一个代次；从 1 起，撤销后也不重置。"""
        return self._generations.get(template_id, 1)

    # ------------------------------------------------------------ 变更
    def define(self, t: TemplateDef) -> None:
        self._active[t.template_id] = t
        self._generations[t.template_id] = t.generation + 1

    def revoke(self, r: Revocation) -> None:
        self._active.pop(r.template_id, None)
        if r.template_id not in self._generations:
            self._generations[r.template_id] = 1


class ScopeRegistry:
    """按 (exporter_id, transport_id, observationDomainId) 隔离的模板状态。"""

    def __init__(self, max_templates_per_scope: int = 1024):
        if (not isinstance(max_templates_per_scope, int)
                or isinstance(max_templates_per_scope, bool)
                or max_templates_per_scope <= 0):
            from .errors import ConfigError
            raise ConfigError("max_templates_per_scope 必须是正整数")
        self.max_templates = max_templates_per_scope
        self._scopes: dict[tuple[str, str, int], ScopeState] = {}

    def get(self, exporter_id: str, transport_id: str,
            domain_id: int) -> ScopeState:
        key = (exporter_id, transport_id, domain_id)
        scope = self._scopes.get(key)
        if scope is None:
            scope = ScopeState(self.max_templates)
            self._scopes[key] = scope
        return scope

    def all_scopes(self):
        return self._scopes.items()


def _u16(buf: bytes, at: int) -> int:
    return (buf[at] << 8) | buf[at + 1]


def _u32(buf: bytes, at: int) -> int:
    return int.from_bytes(buf[at:at + 4], "big")


class MessageDecoder:
    """增量消息解码器。

    调用方逐行送入 (capture_line, exporter_id, transport_id, message_bytes)，
    每个作用域保留模板状态与期望序列号；这里不缓存已解码记录。

    单条消息的输出在内存中也只保留一个数据类列表，调用方落盘后即可释放；
    模块本身不保存任一历史消息。
    """

    def __init__(self, registry: ScopeRegistry):
        self.registry = registry
        self._expected_sequence: dict[tuple[str, str, int], int] = {}

    def decode(self, capture_line: int, exporter_id: str, transport_id: str,
               message: bytes) -> DecodedMessage:
        if len(message) < HEADER_LEN:
            raise self._err(capture_line, "header", "消息短于 16 字节头",
                            offset=0, detail=f"实际 {len(message)} 字节")
        if _u16(message, 0) != VERSION:
            raise self._err(capture_line, "header", "version 不是 10",
                            offset=0, detail=f"实际 {_u16(message, 0)}")
        declared_length = _u16(message, 2)
        if declared_length != len(message):
            raise self._err(capture_line, "header", "length 与真实字节数不符",
                            offset=2,
                            detail=f"头部 length={declared_length}，"
                                   f"真实 {len(message)} 字节")
        if len(message) > 0xFFFF:
            raise self._err(capture_line, "header", "消息超过 65535 字节")
        export_time = _u32(message, 4)
        sequence_number = _u32(message, 8)
        domain_id = _u32(message, 12)

        out = DecodedMessage(
            capture_line=capture_line,
            exporter_id=exporter_id,
            transport_id=transport_id,
            domain_id=domain_id,
            export_time=export_time,
            sequence_number=sequence_number,
            length=declared_length,
            message_hex=message.hex(),
        )
        scope = self.registry.get(exporter_id, transport_id, domain_id)
        pos = HEADER_LEN
        set_ordinal = 0
        while pos < len(message):
            if len(message) - pos < SET_HEADER_LEN:
                raise self._err(capture_line, "set",
                                "消息尾部残余不足 4 字节，不能构成 Set",
                                offset=pos,
                                detail=f"剩余 {len(message) - pos} 字节")
            set_id = _u16(message, pos)
            set_length = _u16(message, pos + 2)
            if set_length < SET_HEADER_LEN:
                raise self._err(capture_line, "set", "setLength 小于 4",
                                offset=pos + 2,
                                detail=f"setId={set_id} setLength={set_length}")
            set_end = pos + set_length
            if set_end > len(message):
                raise self._err(capture_line, "set",
                                "Set 越界：setLength 超出消息",
                                offset=pos + 2,
                                detail=f"setId={set_id} 起 {pos} 长 {set_length} "
                                       f"超出消息长度 {len(message)}")
            if set_id == 0 or set_id == 1:
                raise self._err(capture_line, "set",
                                "未知保留 setId",
                                offset=pos, set_ordinal=set_ordinal,
                                detail=f"setId={set_id}")
            if set_id == OPTIONS_TEMPLATE_SET_ID:
                raise self._err(capture_line, "set",
                                "不支持 Options Template Set",
                                offset=pos, set_ordinal=set_ordinal)
            if set_id == TEMPLATE_SET_ID:
                self._parse_template_set(
                    message, pos + SET_HEADER_LEN, set_end, capture_line,
                    set_ordinal, scope, out)
            elif set_id >= FIRST_DATA_SET_ID:
                self._parse_data_set(
                    message, set_id, pos + SET_HEADER_LEN, set_end,
                    capture_line, set_ordinal, scope, out)
            else:
                # 4..255：保留 setId，按约定明确拒绝，不扫描后面字节猜边界
                raise self._err(capture_line, "set", "未知保留 setId",
                                offset=pos, set_ordinal=set_ordinal,
                                detail=f"setId={set_id}")
            pos = set_end
            set_ordinal += 1

        # 序列号核实：第一条接受任意起值，后继必须精确加上一条消息实际解出的
        # Data Record 条数（模板/撤销/Set 数/字节/填充都不计数）。
        # 新 transport_id 只是新会话，不因序列号变小清空模板。
        key = (exporter_id, transport_id, domain_id)
        expected = self._expected_sequence.get(key)
        if expected is not None and sequence_number != expected:
            raise self._err(capture_line, "sequence", "sequenceNumber 缺口",
                            offset=8,
                            detail=f"期望 {expected}，实际 {sequence_number}"
                                   f"（上一条消息 sequenceNumber + 已解码记录数）")
        self._expected_sequence[key] = (
            sequence_number + len(out.records)) & 0xFFFFFFFF
        return out

    # -------------------------------------------------------- Template Set
    def _parse_template_set(self, message: bytes, start: int, end: int,
                            capture_line: int, set_ordinal: int,
                            scope: ScopeState, out: DecodedMessage) -> None:
        pos = start
        while pos < end:
            remaining = end - pos
            if remaining <= 3:
                if message[pos:end] == b"\x00" * remaining:
                    return  # 0..3 字节零填充
                raise self._err(
                    capture_line, "template",
                    "Template Set 末尾 0..3 字节不是全零填充",
                    offset=pos, set_ordinal=set_ordinal,
                    detail=message[pos:end].hex())
            if remaining < 4:
                raise self._err(capture_line, "template",
                                "Template Set 末尾残余非零且不足一条记录",
                                offset=pos, set_ordinal=set_ordinal,
                                detail=message[pos:end].hex())
            record_offset = pos
            template_id = _u16(message, pos)
            field_count = _u16(message, pos + 2)
            pos += 4
            if template_id < FIRST_DATA_SET_ID:
                raise self._err(capture_line, "template",
                                "templateId 小于 256",
                                offset=record_offset, set_ordinal=set_ordinal,
                                detail=f"templateId={template_id}")
            if field_count == 0:
                if scope.active(template_id) is None:
                    raise self._err(
                        capture_line, "template", "撤销未登记的模板号",
                        offset=record_offset, set_ordinal=set_ordinal,
                        detail=f"templateId={template_id}")
                rev = Revocation(
                    template_id=template_id, capture_line=capture_line,
                    set_ordinal=set_ordinal, set_offset=record_offset,
                    record_offset=record_offset)
                scope.revoke(rev)
                out.revocations.append(rev)
                continue
            fields: list[FieldSpec] = []
            for index in range(field_count):
                spec_offset = pos
                if end - pos < 4:
                    raise self._err(capture_line, "template",
                                    "模板字段说明被 Set 截断",
                                    offset=pos, set_ordinal=set_ordinal,
                                    record_ordinal=len(fields),
                                    field_ordinal=index)
                raw_id = _u16(message, pos)
                length = _u16(message, pos + 2)
                pos += 4
                enterprise = 0
                if raw_id & 0x8000:
                    element_id = raw_id & 0x7FFF
                    if end - pos < 4:
                        raise self._err(capture_line, "template",
                                        "企业号被 Set 截断",
                                        offset=pos, set_ordinal=set_ordinal,
                                        field_ordinal=index)
                    enterprise = _u32(message, pos)
                    pos += 4
                else:
                    element_id = raw_id
                if length == 0:
                    raise self._err(capture_line, "template",
                                    "fieldLength 为 0（合法固定长度从 1 开始，"
                                    "65535 表示变长）",
                                    offset=pos - 2, set_ordinal=set_ordinal,
                                    field_ordinal=index)
                spec = values.known(element_id, enterprise)
                if spec is not None and not spec.length_ok(length):
                    want = spec.length_text
                    raise self._err(
                        capture_line, "template",
                        "已知元素声明了不允许的长度",
                        offset=pos - 2, set_ordinal=set_ordinal,
                        field_ordinal=index,
                        detail=f"元素 {element_id}（{spec.name}）"
                               f"声明 {length}，应为 {want}")
                fields.append(FieldSpec(
                    ordinal=index, element_id=element_id,
                    enterprise=enterprise, length=length,
                    spec_offset=spec_offset, spec_length=pos - spec_offset))

            minimum = sum(1 if f.variable else f.length for f in fields)
            if minimum < 4:
                raise self._err(
                    capture_line, "template",
                    "模板的最小数据记录长度小于 4",
                    offset=record_offset, set_ordinal=set_ordinal,
                    detail=f"templateId={template_id} 最小记录 {minimum} 字节；"
                           "无歧义区分短填充要求至少 4 字节")
            generation = scope.next_generation(template_id)
            definition = TemplateDef(
                template_id=template_id, generation=generation,
                fields=tuple(fields), capture_line=capture_line,
                set_ordinal=set_ordinal, set_offset=start,
                record_offset=record_offset)
            if (not scope.has_template(template_id)
                    and scope.active_count() >= self.registry.max_templates):
                raise self._err(
                    capture_line, "template", "模板数超过可配置上限",
                    offset=record_offset, set_ordinal=set_ordinal,
                    detail=f"同一作用域有效模板已达 "
                           f"{self.registry.max_templates} 个；不会静默淘汰"
                           "仍有效模板，请显式撤销或调高上限")
            scope.define(definition)
            out.definitions.append(definition)
            if end - pos <= 3 and message[pos:end] == b"\x00" * (end - pos):
                # 记录恰好结束：剩余的 0..3 个零字节就是 Set 填充
                return

    # ----------------------------------------------------------- Data Set
    def _parse_data_set(self, message: bytes, set_id: int, start: int,
                        end: int, capture_line: int, set_ordinal: int,
                        scope: ScopeState, out: DecodedMessage) -> None:
        template = scope.active(set_id)
        if template is None:
            raise self._err(capture_line, "data", "未知模板（此前同作用域未定义）",
                            offset=start - SET_HEADER_LEN,
                            set_ordinal=set_ordinal,
                            detail=f"templateId={set_id}")
        minimum = template.minimum_record_length
        record_ordinal = 0
        pos = start
        while pos < end:
            remaining = end - pos
            if remaining <= 3:
                if message[pos:end] == b"\x00" * remaining:
                    return
                raise self._err(
                    capture_line, "data",
                    "Data Set 末尾 0..3 字节不是全零填充",
                    offset=pos, set_ordinal=set_ordinal,
                    record_ordinal=record_ordinal,
                    detail=f"template={set_id} 残余 {remaining} 字节："
                           f"{message[pos:end].hex()}")
            if remaining < minimum:
                raise self._err(
                    capture_line, "data", "Data Set 尾部残余无法构成完整记录",
                    offset=pos, set_ordinal=set_ordinal,
                    record_ordinal=record_ordinal,
                    detail=f"template={set_id} 最少 {minimum} 字节，"
                           f"剩余 {remaining} 字节"
                           + ("" if any(message[pos:end]) else
                              "（全为零且不足 4 字节的尾部只允许是填充，"
                              "这里已有字段无法完成，检查长度前缀/字段边界）"))
            decoded, pos = self._decode_record(
                message, pos, end, template, capture_line, set_ordinal,
                record_ordinal)
            out.records.append(decoded)
            record_ordinal += 1
        # pos == end：最后一个字段恰好结束，没有填充

    def _decode_record(self, message: bytes, pos: int, end: int,
                       template: TemplateDef, capture_line: int,
                       set_ordinal: int, record_ordinal: int):
        record_offset = pos
        fields: list[DecodedField] = []
        for spec in template.fields:
            if spec.variable:
                if pos >= end:
                    raise self._err(capture_line, "data", "变长长度前缀被截断",
                                    offset=pos, set_ordinal=set_ordinal,
                                    record_ordinal=record_ordinal,
                                    field_ordinal=spec.ordinal,
                                    detail="本 Set 内无长度前缀字节")
                prefix_offset = pos
                first = message[pos]
                pos += 1
                if first < 255:
                    actual = first
                    extended = False
                else:
                    if end - pos < 2:
                        raise self._err(capture_line, "data",
                                        "扩展变长长度被截断",
                                        offset=pos, set_ordinal=set_ordinal,
                                        record_ordinal=record_ordinal,
                                        field_ordinal=spec.ordinal,
                                        detail="255 之后需要 2 字节大端长度")
                    actual = _u16(message, pos)
                    pos += 2
                    if actual < 255:
                        raise self._err(capture_line, "data",
                                        "扩展变长长度小于 255",
                                        offset=prefix_offset + 1,
                                        set_ordinal=set_ordinal,
                                        record_ordinal=record_ordinal,
                                        field_ordinal=spec.ordinal,
                                        detail=f"扩展值 {actual}")
                    extended = True
                if end - pos < actual:
                    raise self._err(capture_line, "data", "变长字段载荷越界",
                                    offset=pos, set_ordinal=set_ordinal,
                                    record_ordinal=record_ordinal,
                                    field_ordinal=spec.ordinal,
                                    detail=f"声明 {actual} 字节，本 Set 仅剩 "
                                           f"{end - pos}；长度前缀 "
                                           f"{pos - prefix_offset} 字节"
                                           f"（{'扩展' if extended else '直接'}）")
                value_offset = pos
                prefix = VarPrefix(offset=prefix_offset,
                                   length=pos - prefix_offset,
                                   extended=extended, actual_length=actual)
                raw = message[pos:pos + actual]
                pos += actual
            else:
                length = spec.length
                if end - pos < length:
                    raise self._err(capture_line, "data", "定长字段载荷越界",
                                    offset=pos, set_ordinal=set_ordinal,
                                    record_ordinal=record_ordinal,
                                    field_ordinal=spec.ordinal,
                                    detail=f"声明 {length} 字节，本 Set 仅剩 "
                                           f"{end - pos}")
                value_offset = pos
                raw = message[pos:pos + length]
                pos += length
                prefix = None
                actual = length

            known = values.known(spec.element_id, spec.enterprise)
            if known is None:
                value_type = "raw_bytes"
                decoded_value = raw
            else:
                value_type = known.value_type
                decoded_value = values.decode(known, raw)
            fields.append(DecodedField(
                ordinal=spec.ordinal,
                element_id=spec.element_id,
                enterprise=spec.enterprise,
                declared_length=spec.length,
                actual_length=actual,
                value_offset=value_offset,
                value_hex=raw.hex(),
                var_prefix=prefix,
                value_type=value_type,
                decoded_value=decoded_value,
                template_field_offset=spec.spec_offset,
                template_field_length=spec.spec_length))
        return (DecodedRecord(
            set_ordinal=set_ordinal, record_in_set=record_ordinal,
            template_id=template.template_id,
            template_generation=template.generation,
            record_offset=record_offset, record_length=pos - record_offset,
            fields=fields), pos)

    def _err(self, capture_line: int, phase: str, reason: str, *,
             offset: Optional[int] = None, set_ordinal: Optional[int] = None,
             record_ordinal: Optional[int] = None,
             field_ordinal: Optional[int] = None,
             detail: Optional[str] = None) -> IpfixInputError:
        return IpfixInputError(Failure(
            phase=phase, reason=reason, capture_line=capture_line,
            message_offset=offset, set_ordinal=set_ordinal,
            record_ordinal=record_ordinal, field_ordinal=field_ordinal,
            detail=detail))
