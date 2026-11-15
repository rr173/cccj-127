"""输入故障的收集与一次性抛出。

口径要求：不能悄悄忽略问题后产出一份看似完整的结果。解析与校验阶段
把所有故障收集到 ProblemCollector，最后一次性抛出 CalendarInputError；
调用入口在异常路径上不触碰结果文件。
"""
from __future__ import annotations

from typing import Optional


class CalendarInputError(Exception):
    """整份输入不可用。携带全部故障，而不是只报第一个。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


class ProblemCollector:
    def __init__(self) -> None:
        self._problems: list[str] = []

    def add(self, message: str) -> None:
        self._problems.append(message)

    def extend(self, messages: list[str]) -> None:
        self._problems.extend(messages)

    @property
    def problems(self) -> list[str]:
        return self._problems

    def check(self) -> None:
        if self._problems:
            raise CalendarInputError(self._problems)


def loc(component_index: Optional[int], line_start: Optional[int],
        line_end: Optional[int] = None) -> str:
    """生成统一的组件/物理行定位串。"""

    where = f"组件#{component_index}" if component_index is not None else "日历"
    if line_start is not None:
        rng = str(line_start) if line_end in (None, line_start) else f"{line_start}-{line_end}"
        where += f" 物理行{rng}"
    return where
