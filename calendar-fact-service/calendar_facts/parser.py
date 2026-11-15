"""iCalendar 子集的物理/词法解析（不依赖任何第三方日历库）。

本子集的行处理口径在此处保证，而不是委托给库：

* 文件为 UTF-8（允许 BOM），换行接受 CRLF 或 LF；
* 物理续行：以空格或制表符开头的物理行，去掉换行与“首个空白”后拼接到
  上一逻辑行；续行的第二个及以后字符原样保留；
* 属性名、参数名比较时大小写不敏感（内部统一大写），UID 的值区分大小写；
* 组件序号按 VEVENT 在文件中的出现顺序从 0 编号，物理行号从 1 计，
  含 BEGIN/END 行，全部保留到模型里供事实追查。

本模块只做结构解析，RRULE/DTSTART 等的值语义在 semantics/service 层校验。
"""
from __future__ import annotations

import re

from .errors import ProblemCollector, loc
from .model import Property, VCalendar, VEvent

_BEGIN = "BEGIN"
_END = "END"
_VEVENT = "VEVENT"
_VCALENDAR = "VCALENDAR"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


def unfold_lines(text: str) -> list[tuple[str, int, int]]:
    """拆物理行并做折叠，返回 (逻辑行内容, 起始物理行, 结束物理行)。

    行号从 1 开始。续行符（行首一个 SPACE/TAB）被剥除，换行本身不保留。
    空物理行（非末尾）会以空字符串返回，由结构校验拒绝。
    """

    if text.startswith("﻿"):
        text = text[1:]
    # 只按 \n 切；CRLF 的 \r 留在行尾，统一剥掉。单独的 CR 不支持（子集只收 CRLF/LF）。
    physical = text.split("\n")
    if physical and physical[-1] == "":
        physical.pop()

    lines: list[tuple[str, int, int]] = []
    for idx, raw in enumerate(physical, start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line[:1] in (" ", "\t"):
            if not lines:
                # 续行出现在文件开头：没有可依附的逻辑行
                lines.append((line[1:], idx, idx))
                continue
            content, start, _end = lines[-1]
            lines[-1] = (content + line[1:], start, idx)
        else:
            lines.append((line, idx, idx))
    return lines


def _split_property(line: str) -> tuple[str, list[tuple[str, str]], str]:
    """拆 'NAME;PARAM=v;P2="a;b":VALUE'。参数值支持双引号包裹（引号内不切分）。"""

    # 找到第一个不在引号内的 ':'，它之前是名字+参数，之后是值
    in_quotes = False
    colon = None
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon is None:
        raise ValueError("属性行缺少 ':'")
    head = line[:colon]
    value = line[colon + 1:]
    parts = head.split(";")
    name = parts[0]
    params: list[tuple[str, str]] = []
    for raw_param in parts[1:]:
        if "=" not in raw_param:
            raise ValueError(f"参数缺少 '=': {raw_param!r}")
        pname, pvalue = raw_param.split("=", 1)
        if len(pvalue) >= 2 and pvalue.startswith('"') and pvalue.endswith('"'):
            pvalue = pvalue[1:-1]
        params.append((pname, pvalue))
    return name, params, value


def parse_calendar(text: str, problems: ProblemCollector) -> VCalendar | None:
    """把整份文本解析成 VCalendar；结构问题进 problems，致命结构问题后返回 None。"""

    unfolded = unfold_lines(text)
    cal_props: list[Property] = []
    events: list[VEvent] = []

    stack: list[str] = []
    current: VEvent | None = None
    current_valid = False        # 当前栈顶 VEVENT 是否为受支持的合法开启
    event_index = -1
    saw_vcalendar = False

    for content, line_start, line_end in unfolded:
        if content == "":
            problems.add(f"物理行{line_start}：日历内部不允许空物理行")
            continue
        try:
            raw_name, raw_params, raw_value = _split_property(content)
        except ValueError as exc:
            problems.add(f"物理行{line_start}：{exc}")
            continue
        if not _NAME_RE.match(raw_name):
            problems.add(f"物理行{line_start}：非法属性名 {raw_name!r}")
            continue

        name = raw_name.upper()
        params: dict[str, str] = {}
        for pn, pv in raw_params:
            upn = pn.upper()
            if upn in params:
                problems.add(f"物理行{line_start}：参数 {upn} 重复")
            params[upn] = pv
        prop = Property(name=name, params=params, value=raw_value,
                        line_start=line_start, line_end=line_end)

        if name == _BEGIN:
            comp = raw_value.upper()
            if not stack and comp == _VCALENDAR:
                stack.append(comp)
                saw_vcalendar = True
            elif stack == [_VCALENDAR] and comp == _VEVENT:
                event_index += 1
                current = VEvent(component_index=event_index, line_start=line_start)
                current_valid = True
                stack.append(comp)
            else:
                problems.add(
                    f"{loc(event_index if current is not None else None, line_start)}："
                    f"不支持的组件嵌套 BEGIN:{raw_value}（根必须是 VCALENDAR，"
                    f"其内只允许 VEVENT）")
                stack.append(comp)  # 继续吃掉对应 END，避免级联误报
                if comp == _VEVENT:
                    current_valid = False
            continue

        if name == _END:
            comp = raw_value.upper()
            if not stack or stack[-1] != comp:
                problems.add(f"物理行{line_start}：END:{raw_value} 与组件栈不匹配")
                continue
            stack.pop()
            if comp == _VEVENT:
                if current is not None and current_valid:
                    current.line_end = line_end
                    events.append(current)
                current = None
                current_valid = False
            continue

        if current is not None:
            current.props.append(prop)
        else:
            cal_props.append(prop)

    if stack:
        problems.add(f"文件结束仍有未闭合组件：{stack}")
        return None
    if not events and not cal_props:
        problems.add("空日历：没有任何内容")
        return None
    if not saw_vcalendar:
        problems.add("日历根组件必须是 VCALENDAR，且 VEVENT 只能位于其内")
        return None
    return VCalendar(properties=cal_props, events=events)
