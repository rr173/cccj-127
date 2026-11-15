"""离线日历展开服务：计划数据 -> 可追查的区间事实。

处理顺序（任一步发现故障都不会触碰结果文件）：
  1. 物理/词法解析（parser），组件级白名单与值语义（semantics）；
  2. 主/例外组件结构校验：重复主组件、重复例外、例外无主、类型混用；
  3. 原定集合生成（recur，口径见该模块），RDATE/EXDATE 去重与归属、
     RECURRENCE-ID 必须确实属于原定集合、EXDATE 与例外冲突一次性报错；
  4. 实际区间换算（zoneinfo；歧义取早、缺口跳过）、窗口相交裁剪；
  5. 全部校验通过后，临时文件 + os.replace 原子替换结果文件。

时区换算依赖标准库 zoneinfo（系统 IANA tzdata）；子集验证、重复生成、
例外归属全部在本包代码内显式实现，库的展开列表从未被直接当作结果。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .errors import CalendarInputError, ProblemCollector, loc
from .model import (
    ExcludedItem,
    ExpandReport,
    Fact,
    ParsedEvent,
)
from .parser import parse_calendar
from .recur import _naive_wall as _naive
from .recur import dedupe_walls, iter_rule
from .semantics import check_calendar_props, parse_event
from .timeutil import (
    UTC,
    date_midnight_utc,
    get_zone,
    iso_utc,
    local_wall_exists,
    parse_iso_utc,
    wall_key,
    wall_to_utc_actual,
)


@dataclass
class Occurrence:
    """一次检修发生（窗口过滤前的内部对象）。"""

    uid: str
    wall: object                       # datetime（带 tz）或 date
    recurrence_utc: Optional[datetime]
    start_utc: datetime
    end_utc: datetime
    kind: str                          # "timed" | "all_day"
    summary: Optional[str]
    status: str                        # scheduled/rescheduled/cancelled
    master: ParsedEvent
    override: Optional[ParsedEvent]
    source_event: ParsedEvent          # 实际提供时间的组件


class CalendarFactService:
    def __init__(self, tzname: str):
        self.tzname = tzname
        self.zone: ZoneInfo = get_zone(tzname)

    # ------------------------------------------------------------ 入口
    def expand_file(self, input_path: str | os.PathLike,
                    window_start_utc: str | datetime,
                    window_end_utc: str | datetime,
                    facts_path: str | os.PathLike,
                    report_path: str | os.PathLike) -> ExpandReport:
        input_path = os.fspath(input_path)
        facts_path = os.fspath(facts_path)
        report_path = os.fspath(report_path)
        # newline="" 保留 CRLF，解析器自行剥 \r，避免通用换行改写物理行
        with open(input_path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        ws = parse_iso_utc(window_start_utc) if isinstance(window_start_utc, str) \
            else window_start_utc.astimezone(UTC)
        we = parse_iso_utc(window_end_utc) if isinstance(window_end_utc, str) \
            else window_end_utc.astimezone(UTC)
        if not ws < we:
            raise CalendarInputError(
                [f"查询窗非法：开始 {iso_utc(ws)} 必须严格早于结束 {iso_utc(we)}"])

        facts, report = self.expand_text(text, input_path, ws, we, facts_path)
        # 全部成功后才原子落盘；失败路径在 expand_text 内抛出，旧文件不动
        self._atomic_write_jsonl(facts_path, facts)
        self._atomic_write_json(report_path, report.to_json())
        return report

    def expand_text(self, text: str, input_name: str,
                    window_start: datetime, window_end: datetime,
                    facts_path: str = "<memory>") -> tuple[list[Fact], ExpandReport]:
        problems = ProblemCollector()
        cal = parse_calendar(text, problems)
        if cal is not None:
            check_calendar_props(cal, problems)
        if cal is None:
            problems.check()
            raise CalendarInputError(problems.problems)  # pragma: no cover

        parsed = [parse_event(ve, self.zone, problems) for ve in cal.events]
        parsed = [p for p in parsed if p is not None]
        problems.check()  # 组件级故障先全部抛出

        masters, overrides = self._structure(parsed, problems)
        problems.check()

        all_occurrences: list[Occurrence] = []
        excluded: list[ExcludedItem] = []
        override_audit: list[dict] = []
        master_audit: list[dict] = []

        for uid, master in masters.items():
            occ, exc, audit = self._expand_series(
                uid, master, overrides.get(uid, []), window_start, window_end,
                problems)
            all_occurrences.extend(occ)
            excluded.extend(exc)
            override_audit.extend(audit)
            master_audit.append(self._master_summary(master))
        problems.check()

        facts = self._build_facts(all_occurrences, window_start, window_end)
        facts.sort(key=lambda f: (f.actual_start_utc, f.uid,
                                  f.recurrence_utc or f.recurrence_date or ""))
        excluded.sort(key=lambda e: (e.uid,
                                     e.recurrence_utc or e.recurrence_date or ""))
        report = ExpandReport(
            input_file=input_name,
            tzname=self.tzname,
            window_start_utc=iso_utc(window_start),
            window_end_utc=iso_utc(window_end),
            fact_count=len(facts),
            fact_file=os.fspath(facts_path),
            excluded=excluded,
            overrides=override_audit,
            masters=master_audit,
        )
        return facts, report

    # ----------------------------------------------------- 结构校验
    def _structure(self, parsed: list[ParsedEvent],
                   problems: ProblemCollector):
        masters: dict[str, ParsedEvent] = {}
        overrides: dict[str, list[ParsedEvent]] = {}

        for p in parsed:
            where = loc(p.ve.component_index, p.ve.line_start, p.ve.line_end)
            if p.is_master:
                if p.uid in masters:
                    other = masters[p.uid]
                    problems.add(
                        f"{where}：UID {p.uid!r} 的主组件重复"
                        f"（另见组件#{other.ve.component_index}）")
                    continue
                masters[p.uid] = p
            else:
                if p.recurrence_id is None:
                    continue  # 解析层已记录故障
                overrides.setdefault(p.uid, []).append(p)

        # 例外必须找到主组件；同一原定时刻只能有一个例外
        for uid, items in overrides.items():
            if uid not in masters:
                for p in items:
                    problems.add(
                        f"{loc(p.ve.component_index, p.ve.line_start, p.ve.line_end)}："
                        f"例外组件 UID {uid!r} 找不到同 UID 的主组件")
                continue
            seen: set = set()
            for p in items:
                key = wall_key(p.recurrence_id.value)
                if key in seen:
                    problems.add(
                        f"{loc(p.ve.component_index, p.ve.line_start, p.ve.line_end)}："
                        f"UID {uid!r} 在同一原定时刻 "
                        f"{self._fmt_wall(p.recurrence_id.value)} 存在多个例外组件")
                seen.add(key)
        return masters, overrides

    # ----------------------------------------------------- 单系列展开
    def _expand_series(self, uid: str, master: ParsedEvent,
                       overrides: list[ParsedEvent],
                       window_start: datetime, window_end: datetime,
                       problems: ProblemCollector):
        where = loc(master.ve.component_index, master.ve.line_start,
                    master.ve.line_end)
        kind = self._validate_master_shape(master, problems)
        if kind is None:
            return [], [], []

        # 例外组件的时间表示必须与主 DTSTART 同类型/同时区
        for ov in overrides:
            self._validate_override_shape(ov, master, kind, problems)

        # 1) 规则原定时刻；无 RRULE 的系列，原定集合至少包含 DTSTART 本身
        generated: list[Generated] = []
        if master.rrule is not None:
            rule_limit, needed = self._hard_limit(
                master, overrides, window_start, window_end, kind)
            try:
                generated = list(iter_rule(master.rrule, master.dtstart.value,
                                           self.zone, rule_limit))
            except RuntimeError as exc:
                problems.add(f"{where}：{exc}")
                return [], [], []
            rule_walls = [g.wall for g in generated]
        else:
            needed = self._needed_walls(master, overrides, kind)
            rule_walls = [master.dtstart.value]
        rule_set = {wall_key(w) for w in rule_walls}

        # 2) RDATE：表示类型/时区一致性、去重、存在性
        rdate_walls, rdate_dups = self._check_listed_times(
            master, master.rdates, kind, "RDATE", problems)
        for w in rdate_walls:
            if wall_key(w) in rule_set:
                continue  # 与规则重复，集合去重，不算故障
            if master.dtstart.kind == "tz" and isinstance(w, datetime) and \
                    not local_wall_exists(w, self.zone):
                problems.add(
                    f"{where}：RDATE 时刻 {self._fmt_wall(w)} 在 {self.tzname} "
                    f"不存在（春季缺口），无法作为原定发生")
                continue
            if self._limit_key(_naive(w)) < self._limit_key(_naive(master.dtstart.value)):
                problems.add(
                    f"{where}：RDATE 时刻 {self._fmt_wall(w)} 早于 DTSTART，"
                    f"原定集合不允许回退")
                continue
            if master.rrule and master.rrule.until is not None and \
                    self._limit_key(_naive(w)) > \
                    self._limit_key(_naive(master.rrule.until)):
                problems.add(
                    f"{where}：RDATE 时刻 {self._fmt_wall(w)} 越过 RRULE UNTIL")
                continue
            rule_walls.append(w)

        # 3) EXDATE：一致性、去重、必须确实属于原定集合
        ex_walls, _ = self._check_listed_times(
            master, master.exdates, kind, "EXDATE", problems)
        wall_map = {wall_key(w): w for w in rule_walls}
        ex_set: set = set()
        for w in ex_walls:
            key = wall_key(w)
            if key not in wall_map:
                problems.add(
                    f"{where}：EXDATE {self._fmt_wall(w)} 不属于该系列的原定集合")
                continue
            ex_set.add(key)

        # 4) 例外归属：RECURRENCE-ID 必须确实属于原定集合（含 COUNT/跳过约束）
        override_by_key: dict = {}
        for ov in overrides:
            rid = ov.recurrence_id.value
            key = wall_key(rid)
            ov_where = loc(ov.ve.component_index, ov.ve.line_start, ov.ve.line_end)
            if key not in wall_map:
                problems.add(
                    f"{ov_where}：RECURRENCE-ID {self._fmt_wall(rid)} 不属于 "
                    f"UID {uid!r} 的原定集合（超出 COUNT、落在跳过日期或本就不存在）")
                continue
            if master.dtstart.kind == "tz" and isinstance(rid, datetime) and \
                    not local_wall_exists(rid, self.zone):
                problems.add(
                    f"{ov_where}：RECURRENCE-ID {self._fmt_wall(rid)} 是本地不存在时刻")
                continue
            if key in ex_set:
                problems.add(
                    f"UID {uid!r} 的原定时刻 {self._fmt_wall(rid)} 同时被 EXDATE "
                    f"排除且存在例外组件（组件#{ov.ve.component_index}），冲突不猜优先级")
                continue
            override_by_key[key] = ov

        # 5) 组装发生（取消 / 替换 / 常规）
        occurrences: list[Occurrence] = []
        excluded: list[ExcludedItem] = []
        audits: list[dict] = []
        ordered_walls = sorted(rule_walls, key=self._sort_key)

        for wall in ordered_walls:
            key = wall_key(wall)
            if key in ex_set:
                excluded.append(ExcludedItem(
                    uid=uid,
                    recurrence_utc=iso_utc(self._wall_to_utc(wall))
                    if kind == "timed" else None,
                    recurrence_date=wall.isoformat() if kind == "all_day" else None,
                    reason="exdate", component_index=None, lines=None))
                continue
            ov = override_by_key.get(key)
            if ov is not None:
                audits.append(self._override_audit(uid, wall, ov, kind))
                if ov.status == "CANCELLED":
                    excluded.append(ExcludedItem(
                        uid=uid,
                        recurrence_utc=iso_utc(self._wall_to_utc(wall))
                        if kind == "timed" else None,
                        recurrence_date=wall.isoformat() if kind == "all_day" else None,
                        reason="cancelled",
                        component_index=ov.ve.component_index,
                        lines=f"{ov.ve.line_start}-{ov.ve.line_end}"))
                    continue
                occ = self._replacement_occurrence(master, ov, wall, kind, problems)
                if occ is not None:
                    occurrences.append(occ)
                continue
            occurrences.append(
                self._regular_occurrence(master, wall, kind))

        return occurrences, excluded, audits

    # ----------------------------------------------------- 形状校验
    def _validate_master_shape(self, master: ParsedEvent,
                               problems: ProblemCollector) -> Optional[str]:
        where = loc(master.ve.component_index, master.ve.line_start,
                    master.ve.line_end)
        if master.dtstart is None:
            problems.add(f"{where}：主组件缺少 DTSTART")
            return None
        kind = "all_day" if master.dtstart.kind == "date" else "timed"

        if kind == "all_day":
            if master.dtend is None:
                problems.add(f"{where}：全天事件必须用 DTEND;VALUE=DATE 给出半开日期区间")
                return None
            if master.dtend.kind != "date":
                problems.add(f"{where}：全天事件 DTEND 必须同为 VALUE=DATE")
                return None
            if master.duration_seconds is not None:
                problems.add(f"{where}：全天事件使用 DTEND，不允许 DURATION")
                return None
            if not master.dtend.value > master.dtstart.value:
                problems.add(f"{where}：DTEND 必须晚于 DTSTART（左闭右开）")
                return None
        else:
            if master.duration_seconds is None:
                problems.add(
                    f"{where}：定时事件必须给出正的 PTnHnMnS 形式 DURATION；"
                    f"本子集不接收 DTEND 形式的定时事件")
                return None
            if master.dtend is not None:
                problems.add(f"{where}：定时事件使用 DURATION，不允许 DTEND")
                return None
            if master.dtstart.kind == "tz" and \
                    not local_wall_exists(master.dtstart.value, self.zone):
                problems.add(
                    f"{where}：DTSTART {self._fmt_wall(master.dtstart.value)} "
                    f"在 {self.tzname} 不存在，系列无法开始")
                return None

        # 列表时间与主 DTSTART 的表示类型/时区一致性在 _expand_series 统一校验，
        # 避免同一故障被收集两次
        return kind

    def _validate_override_shape(self, ov: ParsedEvent, master: ParsedEvent,
                                 kind: str, problems: ProblemCollector) -> None:
        where = loc(ov.ve.component_index, ov.ve.line_start, ov.ve.line_end)
        if ov.recurrence_id.kind != master.dtstart.kind or \
                ov.recurrence_id.tzid != master.dtstart.tzid:
            problems.add(
                f"{where}：RECURRENCE-ID 的表示类型/时区必须与主 DTSTART 一致")
        if ov.status == "CANCELLED":
            if ov.dtstart is not None or ov.duration_seconds is not None \
                    or ov.dtend is not None or ov.rrule is not None \
                    or ov.rdates or ov.exdates:
                problems.add(
                    f"{where}：STATUS:CANCELLED 例外只承载取消语义，"
                    f"不得再带 DTSTART/DURATION/RRULE/RDATE/EXDATE")
            return
        if ov.dtstart is None:
            problems.add(f"{where}：非取消例外必须给出自己的 DTSTART")
            return
        if ov.dtstart.kind != master.dtstart.kind or \
                ov.dtstart.tzid != master.dtstart.tzid:
            problems.add(
                f"{where}：例外 DTSTART 的表示类型/时区必须与主 DTSTART 一致")
        if kind == "all_day":
            if ov.dtend is None or ov.dtend.kind != "date":
                problems.add(f"{where}：全天例外必须给出 VALUE=DATE 的 DTEND")
            elif not ov.dtend.value > ov.dtstart.value:
                problems.add(f"{where}：全天例外 DTEND 必须晚于 DTSTART")
            if ov.duration_seconds is not None:
                problems.add(f"{where}：全天例外不允许 DURATION")
        else:
            if ov.duration_seconds is None:
                problems.add(f"{where}：定时例外必须给出自己的 DURATION")
            if ov.dtend is not None:
                problems.add(f"{where}：定时例外不允许 DTEND")
            if ov.dtstart.kind == "tz" and \
                    not local_wall_exists(ov.dtstart.value, self.zone):
                problems.add(
                    f"{where}：例外 DTSTART {self._fmt_wall(ov.dtstart.value)} "
                    f"在 {self.tzname} 不存在")
        if ov.rrule is not None or ov.rdates or ov.exdates:
            problems.add(f"{where}：例外组件不允许携带 RRULE/RDATE/EXDATE")

    def _check_listed_times(self, owner: ParsedEvent, values, kind: str,
                            label: str, problems: ProblemCollector):
        """校验 RDATE/EXDATE 与主 DTSTART 同表示类型/同时区，并集合去重。"""

        where = loc(owner.ve.component_index, owner.ve.line_start,
                    owner.ve.line_end)
        walls = [dv.value for dv in values]
        for dv in values:
            if dv.kind != owner.dtstart.kind or \
                    dv.tzid != owner.dtstart.tzid:
                problems.add(
                    f"{where}：{label} 的表示类型/时区（{dv.kind}/"
                    f"{dv.tzid}）必须与主 DTSTART（{owner.dtstart.kind}/"
                    f"{owner.dtstart.tzid}）一致")
        unique, dups = dedupe_walls(walls)
        for d in dups:
            problems.add(
                f"{where}：{label} 含重复原定时刻 {self._fmt_wall(d)}，集合须去重")
        return unique, dups

    # ----------------------------------------------------- 区间换算
    def _regular_occurrence(self, master: ParsedEvent, wall, kind: str) -> Occurrence:
        if kind == "all_day":
            start_utc, end_utc = self._all_day_interval(wall, master)
        else:
            start_utc = self._wall_to_utc(wall)
            end_utc = start_utc + timedelta(seconds=master.duration_seconds)
        return Occurrence(
            uid=master.uid, wall=wall,
            recurrence_utc=start_utc if kind == "timed" else None,
            start_utc=start_utc, end_utc=end_utc, kind=kind,
            summary=master.summary, status="scheduled",
            master=master, override=None, source_event=master)

    def _replacement_occurrence(self, master: ParsedEvent, ov: ParsedEvent,
                                wall, kind: str,
                                problems: ProblemCollector) -> Optional[Occurrence]:
        where = loc(ov.ve.component_index, ov.ve.line_start, ov.ve.line_end)
        if ov.dtstart is None:
            # 形状校验已记录“非取消例外缺少 DTSTART”，此处不能继续换算
            return None
        if kind == "all_day":
            start_utc, end_utc = self._all_day_interval(ov.dtstart.value, ov)
        elif ov.dtstart.kind == "utc":
            start_utc = ov.dtstart.value.astimezone(UTC)
            end_utc = start_utc + timedelta(seconds=ov.duration_seconds)
        else:
            try:
                start_utc = wall_to_utc_actual(ov.dtstart.value, self.zone)
            except ValueError as exc:
                problems.add(f"{where}：{exc}")
                return None
            end_utc = start_utc + timedelta(seconds=ov.duration_seconds)
        return Occurrence(
            uid=master.uid, wall=wall,
            recurrence_utc=self._wall_to_utc(wall) if kind == "timed" else None,
            start_utc=start_utc, end_utc=end_utc, kind=kind,
            summary=ov.summary if ov.summary is not None else master.summary,
            status="rescheduled", master=master, override=ov, source_event=ov)

    def _all_day_interval(self, start_date: date, owner: ParsedEvent):
        """全天区间：按设备时区的日期边界转 UTC。

        每次发生保留 owner 组件跨越的日历天数（DTEND-DTSTART），结束日期 =
        本次起点日期 + 该跨度；因此即便某一天是 23/25 小时也不会固定 86400 秒。
        例外组件自带 DTEND，跨度由它自己决定，可改变长度。
        """

        if owner.dtend is not None:
            span_days = (owner.dtend.value - owner.dtstart.value).days
        else:  # 理论不可达，形状校验已拦截
            span_days = 1
        if span_days <= 0:
            span_days = 1
        end_date = start_date + timedelta(days=span_days)
        start_utc = date_midnight_utc(start_date, self.zone)
        end_utc = date_midnight_utc(end_date, self.zone)
        return start_utc, end_utc

    def _wall_to_utc(self, wall) -> datetime:
        if isinstance(wall, datetime):
            # UTC 型值（timezone.utc，无 ZoneInfo.key）本身即 UTC
            if not hasattr(wall.tzinfo, "key"):
                return wall.astimezone(UTC)
            return wall_to_utc_actual(wall, self.zone)
        return date_midnight_utc(wall, self.zone)

    # ----------------------------------------------------- 事实与窗口
    def _build_facts(self, occurrences: list[Occurrence],
                     window_start: datetime, window_end: datetime) -> list[Fact]:
        facts: list[Fact] = []
        for occ in occurrences:
            if occ.end_utc <= window_start or occ.start_utc >= window_end:
                continue  # 左闭右开，边界相切不算相交
            clipped_start = max(occ.start_utc, window_start)
            clipped_end = min(occ.end_utc, window_end)
            overlap = int((clipped_end - clipped_start).total_seconds())
            if overlap <= 0:
                continue  # 相切（含回拨日折叠为零长度投影）不产生事实
            master = occ.master
            ov = occ.override
            rid_part: str
            if occ.kind == "timed":
                rid_part = iso_utc(occ.recurrence_utc)
            else:
                rid_part = occ.wall.isoformat()
            facts.append(Fact(
                fact_id=f"{occ.uid}#{rid_part}",
                uid=occ.uid,
                recurrence_utc=iso_utc(occ.recurrence_utc)
                if occ.kind == "timed" else None,
                recurrence_date=occ.wall.isoformat()
                if occ.kind == "all_day" else None,
                kind=occ.kind,
                summary=occ.summary,
                status=occ.status,
                actual_start_utc=iso_utc(occ.start_utc),
                actual_end_utc=iso_utc(occ.end_utc),
                clipped_start_utc=iso_utc(clipped_start),
                clipped_end_utc=iso_utc(clipped_end),
                overlap_seconds=overlap,
                source_component_index=occ.source_event.ve.component_index,
                source_lines=f"{occ.source_event.ve.line_start}-"
                             f"{occ.source_event.ve.line_end}",
                master_component_index=master.ve.component_index,
                master_lines=f"{master.ve.line_start}-{master.ve.line_end}",
                is_override=ov is not None,
                override_component_index=ov.ve.component_index if ov else None,
                override_lines=f"{ov.ve.line_start}-{ov.ve.line_end}"
                if ov else None,
            ))
        return facts

    # ----------------------------------------------------- 审计信息
    def _master_summary(self, master: ParsedEvent) -> dict:
        return {
            "uid": master.uid,
            "component_index": master.ve.component_index,
            "lines": f"{master.ve.line_start}-{master.ve.line_end}",
            "kind": "all_day" if master.dtstart.kind == "date" else "timed",
            "dtstart": self._fmt_wall(master.dtstart.value),
            "has_rrule": master.rrule is not None,
            "rdate_count": len(master.rdates),
            "exdate_count": len(master.exdates),
        }

    def _override_audit(self, uid: str, wall, ov: ParsedEvent, kind: str) -> dict:
        return {
            "uid": uid,
            "recurrence_utc": iso_utc(self._wall_to_utc(wall))
            if kind == "timed" else None,
            "recurrence_date": wall.isoformat() if kind == "all_day" else None,
            "status": "cancelled" if ov.status == "CANCELLED" else "rescheduled",
            "component_index": ov.ve.component_index,
            "lines": f"{ov.ve.line_start}-{ov.ve.line_end}",
        }

    # ----------------------------------------------------- 无界展开边界
    def _window_wall_limit(self, master: ParsedEvent, window_end: datetime,
                           kind: str):
        """查询窗推导出的规则推进墙上截止（naive 字段）。"""

        if kind == "all_day":
            return window_end.astimezone(self.zone).date()
        if master.dtstart.kind == "utc":
            # 多取 8 天：WEEKLY 周期内最晚候选（周日）相对周一可达 6 天
            return window_end.replace(tzinfo=None) + timedelta(days=8)
        local_end = window_end.astimezone(self.zone)
        return datetime(local_end.year, local_end.month, local_end.day) \
            + timedelta(days=8)

    def _needed_walls(self, master: ParsedEvent, overrides: list[ParsedEvent],
                      kind: str) -> list:
        """归属校验必须能命中的全部墙上时刻（例外/RDATE/EXDATE，naive 字段）。"""

        walls = [_naive(ov.recurrence_id.value) for ov in overrides]
        walls.extend(_naive(dv.value) for dv in master.rdates)
        walls.extend(_naive(dv.value) for dv in master.exdates)
        return walls

    def _hard_limit(self, master: ParsedEvent, overrides: list[ParsedEvent],
                    window_start: datetime, window_end: datetime,
                    kind: str) -> tuple[object, list]:
        """无 COUNT/UNTIL 系列的墙上截止（含端点）与必须覆盖的墙上时刻。

        截止取“窗口推导下限”和“全部例外/列表时刻”的最大值：不能取近窗
        RDATE 的小值，否则窗外原定时刻的例外会被截断而误判为无主。
        """

        rule_limit = self._window_wall_limit(master, window_end, kind)
        needed = self._needed_walls(master, overrides, kind)
        if needed:
            # 类型不匹配的输入在形状校验已报错；这里用统一元组键避免
            # date/datetime 直接比较抛 TypeError，保证诊断路径不中断
            rule_limit = max([rule_limit, *needed], key=self._limit_key)
        return rule_limit, needed

    @staticmethod
    def _limit_key(value):
        # 定长元组：date 与 datetime 也能安全互相比较（异构输入在形状校验已报错，
        # 这里只为保证诊断路径不抛 TypeError）
        if isinstance(value, datetime):
            return (value.year, value.month, value.day,
                    value.hour, value.minute, value.second)
        return (value.year, value.month, value.day, 0, 0, 0)

    # ----------------------------------------------------- 工具
    def _fmt_wall(self, wall) -> str:
        if isinstance(wall, datetime):
            if wall.tzinfo is UTC or wall.utcoffset() == timedelta(0):
                return wall.strftime("%Y-%m-%dT%H:%M:%SZ")
            return wall.strftime("%Y-%m-%dT%H:%M:%S") + f" [{self.tzname}]"
        return wall.isoformat()

    def _sort_key(self, wall):
        if isinstance(wall, datetime):
            return (0, wall.replace(tzinfo=None))
        return (1, datetime(wall.year, wall.month, wall.day))

    def _atomic_write(self, path: str, write_body) -> None:
        """临时文件写完后原子替换；统一结果文件权限为 0644。

        先在同目录建临时文件（保证 os.replace 是同文件系统的原子改名），
        规范化权限后再替换，失败时清理临时文件且绝不触碰旧结果。
        """

        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".cf-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                write_body(fh)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _atomic_write_jsonl(self, path: str, facts: list[Fact]) -> None:
        def body(fh) -> None:
            for fact in facts:
                fh.write(json.dumps(fact.to_json(), ensure_ascii=False) + "\n")
        self._atomic_write(path, body)

    def _atomic_write_json(self, path: str, payload: dict) -> None:
        def body(fh) -> None:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        self._atomic_write(path, body)
