# 离线日历展开服务（calendar-fact-service）

把工厂设备检修的 iCalendar 计划，加工成数仓可用的**逐次检修区间事实（JSONL）**。
一条“每周检修”日历行不等于一条事实：跨夏令时、临时挪期、取消之后，本服务按
**事件原时区的日历**展开实际发生，给出可追查的稳定身份、实际 UTC 起止、与查询窗
相交的真实秒数，以及来源组件/物理行。

只处理计划数据到数仓明细的加工，不管理会议、预约或人员，无前端、无联网日历服务。

## 1. 依赖与库边界（重要）

* 纯 Python 标准库实现，**零第三方依赖**：解析器、RRULE 展开、例外归属、窗口裁剪
  全部在本包代码内显式实现并被 `tests/` 锁定；
* 时区换算只委托标准库 `zoneinfo`（读取系统 IANA tzdata）。

因此本子集的验证由本包自己保证，而不是把某个日历库返回的展开列表直接当作满足口径：

| 口径 | 保证位置 |
| --- | --- |
| 行折叠（CRLF/LF、SPACE/TAB 续行只去首个空白）、组件序号与物理行范围 | `calendar_facts/parser.py` |
| 属性白名单、参数/值类型、浮动时间拒绝、SUMMARY 反转义 | `calendar_facts/semantics.py`、`timeutil.py` |
| RRULE 子集（DAILY/WEEKLY/MONTHLY、INTERVAL、COUNT/UNTIL 互斥、BYDAY/BYMONTHDAY） | `semantics.parse_rrule` |
| 按原时区日历推进、缺口跳过不占 COUNT、回拨取较早 UTC、月末倒数 | `calendar_facts/recur.py` |
| RDATE/EXDATE 一致性与去重、RECURRENCE-ID 归属、EXDATE 与例外冲突 | `calendar_facts/service.py` |
| 全天 23/25 小时换算、窗口半开相交与裁剪、原子落盘 | `timeutil.py`、`service.py` |

## 2. 接入子集（明确不宣称覆盖整份 RFC 5545）

* UTF-8（允许 BOM），单个 `VCALENDAR` 内多个 `VEVENT`；CRLF 或 LF；物理续行以
  SPACE/TAB 开头，去掉换行与首个空白后拼接。
* 属性名、参数名大小写不敏感；**UID 值区分大小写**。
* 每系列一个同 UID 主组件，可有同 UID 例外组件；例外靠 `RECURRENCE-ID` 指向
  **原定发生时间**，不按改期后的 DTSTART 对齐。
* 定时 DTSTART：`...Z`（UTC）或带 `TZID` 的本地时间；无 Z 无 TZID 的浮动时间拒绝。
* `DURATION` 只接受正的 `PTnHnMnS`（真实经过秒数）；全天事件用
  `DTSTART;VALUE=DATE` + `DTEND;VALUE=DATE` 的左闭右开日期区间，不用 DURATION。
* RRULE 仅 `FREQ=DAILY/WEEKLY/MONTHLY`、正 `INTERVAL`、`COUNT` 或 `UNTIL`（互斥）；
  WEEKLY 的 `BYDAY` 不重复、周从周一开始；MONTHLY 的 `BYMONTHDAY` 支持月末倒数负数，
  不存在日期跳过；其余 RRULE 组合一律拒绝。有过滤项时 DTSTART 必须满足它。
* `RDATE`/`EXDATE` 的表示类型与时区必须与主 DTSTART 一致，集合去重；同一时刻既被
  EXDATE 排除又有例外组件，整份输入报冲突。
* 例外：`STATUS:CANCELLED` 取消该次；否则须自带 DTSTART 与 DURATION（全天为 DTEND），
  完整替换本次时间。RECURRENCE-ID 必须确实属于主规则或 RDATE 的原定集合。
* 稳定身份 = `UID + 原定时刻`；改期不换身份、不按实际起点去重；两个不同原定时刻
  允许挪到同一实际时间，仍是两条事实。
* 查询窗左闭右开；只输出实际区间与窗口有**非空**相交的发生，完整起止与裁剪区间都保留。

任何子集外且可能影响时间口径的属性（如 `EXRULE`）、重复主组件、重复例外、例外无主、
非法身份等，都收集为诊断并**整份拒绝**，不静默忽略。

## 3. 输出

事实 JSONL（每行一条），关键字段：

| 字段 | 含义 |
| --- | --- |
| `fact_id` | 稳定身份 `UID#原定UTC`（全天为 `UID#原定日期`） |
| `recurrence_utc` / `recurrence_date` | 原定发生时刻（定时 UTC / 全天设备时区日期） |
| `status` | `scheduled` / `rescheduled` / `cancelled` |
| `actual_start_utc` / `actual_end_utc` | 实际完整区间 UTC（改期后反映新时间） |
| `clipped_start_utc` / `clipped_end_utc` | 与查询窗相交后的裁剪区间 |
| `overlap_seconds` | 裁剪区间的 UTC 秒差（全天可能是 23/25 小时倍数，不写死 86400） |
| `source_component_index` / `source_lines` | 实际提供时间的组件序号与物理行范围 |
| `master_component_index` / `master_lines` | 主组件定位 |
| `is_override` / `override_component_index` / `override_lines` | 例外血缘 |

诊断 JSON 另列：输入与窗口、事实计数、`excluded`（EXDATE 排除与 CANCELLED 取消的
原定时刻）、`overrides` 审计、`masters` 摘要，用于解释“少了哪次检修”。

## 4. 失败语义与原子写入

先完整验证输入与全部例外归属，**通过后**才以临时文件 + `os.replace` 原子替换结果文件；
任一校验失败则退出码 2，已有的事实/诊断文件**原字节不动**，诊断打到 stderr，
不会冒充“零行成功”。

## 5. 运行方式（全部离线，`-I` 隔离防止夹具目录被植入模块）

```bash
# 重新生成手写夹具（含 CRLF、折行、中文转义、错误输入）
python3 -I bin/make_fixtures.py
python3 -I bin/make_error_fixtures.py

# 跑全部单测（67 个，期望值为手写 UTC 常量，不取自生产输出）
python3 -I bin/run_tests.py

# 端到端演示
python3 -I bin/demo.py

# 实际展开入口
python3 -I bin/run_expand.py \
  --input tests/fixtures/overrides.ics --tz America/New_York \
  --window-start 2024-03-11T00:00:00Z --window-end 2024-03-25T00:00:00Z \
  --out /tmp/facts.jsonl --report /tmp/report.json
```

Python API：

```python
from calendar_facts import CalendarFactService, CalendarInputError

svc = CalendarFactService("America/New_York")
report = svc.expand_file(
    "plan.ics",
    "2024-03-01T00:00:00Z", "2024-04-01T00:00:00Z",
    "out/facts.jsonl", "out/report.json")
# 或纯内存：facts, report = svc.expand_text(text, name, start_dt, end_dt)
```

## 6. 验证场景对照（期望值手工推导见各测试注释）

| 要求场景 | 测试 |
| --- | --- |
| 纽约春季 02:30 缺口跳过且 COUNT 补足、回拨 01:30 取较早一次 | `test_dst_and_recur.py::TestGapAndFallback` |
| 每月 31 日跳过短月、BYMONTHDAY 负数月末、按日历而非 UTC+86400 推进 | `TestMonthlyRules` / `TestCalendarAdvance` |
| 窗外原定挪入、窗内挪出、取消、同实际时间不同身份、例外写在主组件前 | `test_overrides.py` |
| RDATE/EXDATE 去重、归属、与窗口边界半开相交、裁剪秒数 | `test_rdate_allday.py` / `test_until_dedup.py` |
| 跨春秋切换的全天 23/25 小时区间、例外改长度 | `TestAllDayDst` |
| 不存在的例外身份（超 COUNT / 落跳过日）、重复例外、日期类型冲突 | `test_rejection_atomic.py` / `test_until_dedup.py` |
| CRLF + SPACE/TAB 折行、中文与 `\\ , ; n` 转义、组件序号与物理行 | `test_parser_folding.py` |
| 末尾故障时旧结果原字节保留、成功才原子替换、JSONL 合法 | `TestAtomicWrite` |
| UNTIL 含端点、无界系列受窗口约束、定时 UNTIL 必须 UTC | `test_until_dedup.py` |

## 7. 目录结构

```
calendar-fact-service/
├── calendar_facts/
│   ├── parser.py        # 物理/词法解析、折叠、组件与行范围
│   ├── semantics.py     # 属性白名单、RRULE 子集、值语义
│   ├── timeutil.py      # zoneinfo 换算、缺口/歧义判定、DURATION、反转义
│   ├── recur.py         # 原时区日历推进的重复引擎
│   ├── service.py       # 校验编排、例外归属、事实构建、原子落盘
│   ├── model.py         # 内存模型与 Fact/Report 数据类
│   ├── errors.py        # ProblemCollector 与 CalendarInputError
│   └── cli.py           # argparse 入口
├── bin/
│   ├── run_expand.py    # -I 离线展开包装入口
│   ├── run_tests.py     # -I 测试入口
│   ├── demo.py          # 端到端演示
│   ├── make_fixtures.py
│   └── make_error_fixtures.py
└── tests/               # 67 个 unittest + tests/fixtures（含 errors/）
```
