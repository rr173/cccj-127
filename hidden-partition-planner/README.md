# 离线隐藏分区查询规划器（hidden-partition-planner）

数仓的历史文件按各自时代的分区规则落盘：账户列先按 `truncate(10)` 宽区间分文件，
后来收窄为 `truncate(4)`，再后来又把地区全名（`identity`）改为前缀分区
（`prefix(1)`/`prefix(2)`），期间还出现过同源双分区字段和 `void`。**历史文件不能
重写**，查询却仍应一次读全，而且不能为查一个账户打开所有文件。

本模块是这个问题的离线规划器：用户只写**业务列条件**，工具从**每个文件自己的
spec** 推导哪些文件不用读（`drop`）、哪些文件可直接交付（`pass`）、哪些还要
逐行过滤（`filter`），然后真正执行扫描并交付行。

技术栈：Python 3.11 标准库（无第三方依赖）。纯离线：输入只读，不启动服务、
不联网。仓库中原有的 `code-mapping-service`、`calendar-fact-service`、
`ipfix-ledger` 三个入口未改动；本模块不新增接收账本或数据编码格式，也不改动
先前模块。

---

## 1. 输入合同：manifest（有样例的 JSON）

manifest 是输入方已经核对过的数据目录：**规划时相信其中的行数与分区值，
不靠打开数据反推**。样例见 `examples/catalog/manifest.json`（由
`bin/make_fixtures.py` 幂等生成）。

```json
{
  "schema": {
    "fields": [
      {"field_id": 1, "name": "account", "type": "int64", "nullable": true},
      {"field_id": 2, "name": "region", "type": "string", "nullable": true}
    ]
  },
  "specs": [
    {"spec_id": "old-2023", "partition_fields": [
      {"partition_id": 1, "name": "账户区间", "source_id": 1,
       "transform": {"kind": "truncate", "width": 10}},
      {"partition_id": 2, "name": "地区", "source_id": 2,
       "transform": {"kind": "identity"}}
    ]}
  ],
  "files": [
    {"file_id": "f-old-wide", "path": "data/f-old-wide.jsonl",
     "spec_id": "old-2023", "row_count": 3,
     "partitions": {"1": -10, "2": "京𠮷照"}}
  ]
}
```

### schema（当前逻辑 schema）

每列四个键，不多不少：

| 键 | 约定 |
| --- | --- |
| `field_id` | 正整数（`bool` 不算整数），全局唯一 |
| `name` | 唯一非空字符串；scan 输出对象以它为键 |
| `type` | `"int64"` 或 `"string"`；int64 是真正的有符号 64 位整数 |
| `nullable` | 布尔 |

### spec（分区规则）

* `spec_id`：唯一非空字符串。
* `partition_fields`：每个分区字段有**本 spec 内唯一**的正整数
  `partition_id`、说明名称 `name`、`source_id` 与 `transform`。
* `source_id` 引用逻辑 `field_id`。**说明名称可以重复、可以改名，绝不用于
  关联业务列**——验收目录里 `"账户区间"` 同时出现在三个 spec 里、`"地区"`
  一会儿是 identity 一会儿是 prefix，关联只靠 `field_id`/`partition_id`。
* 不同 spec 可以给同一源列不同变换；一个 spec 也可给同一源列设多个分区
  字段（语义是它们原像的**交集**，不是任选其一）。

### 文件条目

* `file_id`：唯一非空字符串。
* `path`：相对数据路径（拒绝绝对路径与 `.`/`..` 段），相对于 manifest
  所在目录解析；规划阶段绝不 stat 或打开它。
* `spec_id`：必须引用已声明的 spec；**每个文件只用它自己的 spec 解释，
  绝不借用另一个 spec 或“最新” spec**。
* `row_count`：非负整数。
* `partitions`：以 `partition_id` 的十进制字符串为键的**完整**分区值，
  不多不少。

### 变换与分区值校验

| 变换 | 源类型 | 语义 | 分区值校验 |
| --- | --- | --- | --- |
| `identity` | 任意 | 原样保留非 null 值 | 同类型合法值或 null |
| `truncate(W)` | int64 | `W*floor(x/W)`，W 正整数，负数向**负无穷**取整 | W 的整数倍，且原像与 int64 有交集（允许略低于 int64 下界，因为 `W*floor(INT64_MIN/W)` 本身可以小于 INT64_MIN） |
| `prefix(L)` | string | 前 L 个 Unicode 码点（不按 UTF-8 字节截断） | 最多 L 个码点的字符串 |
| `void` | 任意 | 不论源值是否为 null 都给 null | 必须为 null |

前三种变换把源 null 映为分区 null。因此：

* 对**不可空源**给出非 void 的 null 分区 → 矛盾目录，拒绝；
* `void` 的 null **不能**推出源列为空（它对源列一无所知）；
* 同源多个分区字段的原像交集为空且 `row_count>0` → 矛盾目录，拒绝；
* 零行文件的结构仍要验证（分区值本身必须合法），但可直接跳过，空交集
  在零行时不构成矛盾。

所有目录问题一次性收集、带 JSON 位置报出（`$.files[2].partitions["1"]`
这类），**在输出任何计划之前**拒绝。

## 2. 原像（preimage）

规划的核心是把“分区值”翻译回“源值集合”：

| 分区 | 原像 |
| --- | --- |
| `identity = v` | 恰为 `{v}` |
| `truncate(W) = v` | 整数区间 `[max(v, INT64_MIN), min(v+W-1, INT64_MAX)]` |
| `prefix(L) = s`，`len(s) < L` | 恰为 `{s}`（短于 L 意味着源值恰等于它；空串是值） |
| `prefix(L) = s`，`len(s) = L` | 以 s 开头的全部字符串，即码点区间 `[s, succ(s))` |
| `void` | 全域（不能推出源列为空） |
| 非 void 变换的 `null` | 恰为 `{null}` |

`succ(s)` 是“大于所有以 s 开头字符串的最小字符串”：去掉尾部连续的
U+10FFFF 后把最后一个码点加一；全是 U+10FFFF 时上界无界。例如
`succ("京𠮷") = "京𠮸"`（𠮷=U+20BB7，𠮸=U+20BB8，都在 BMP 之外，
按码点而不是 UTF-8 字节计算）。字符串比较全程按原 Unicode 码点顺序，
不归一化。

一个文件某列的**取值约束** = 该列所有分区字段原像的交集（无分区字段则
为全域），外加“null 是否可能”。

## 3. 查询表达式（不接任意 SQL）

```json
{"op": "and", "children": [e1, e2, ...]}     至少两个子式
{"op": "or",  "children": [e1, e2, ...]}     至少两个子式
{"op": "not", "child": e}                    恰一个子式
{"op": "eq|ne|lt|le|gt|ge", "field_id": 1, "value": 常量}
{"op": "is_null"|"is_not_null", "field_id": 1}
```

* 比较叶的常量必须同类型、非 null（查 null 用 `is_null`）；
* 未知列、坏类型、空逻辑列表、未支持节点都会被拒绝，并给出表达式位置
  （`$.children[1].child.value` 这类路径）；
* 样例见 `examples/queries/*.json`。

## 4. 三值逻辑（规划与行求值同一合同）

* 普通比较遇到 null 得 `UNKNOWN`；`is_null`/`is_not_null` 总为
  `TRUE`/`FALSE`；
* `not UNKNOWN` 仍是 `UNKNOWN`；
* `and`：任一 `FALSE` 则 `FALSE`，全部 `TRUE` 才 `TRUE`，其余 `UNKNOWN`；
* `or`：任一 `TRUE` 则 `TRUE`，全部 `FALSE` 才 `FALSE`，其余 `UNKNOWN`；
* **只有最终 `TRUE` 的行能交付**。把 null 等价于 `FALSE` 再做 `not` 会
  错误地保留空值——本模块在规划（`hidden_partition/analyze.py`）与实际
  过滤（`hidden_partition/evaluator.py`，公开行求值器）中保持同一合同：
  全 null 文件对 `not(x=7)` 是 `UNKNOWN` → drop，对 `not(is_null(x))`
  是 `FALSE` → 同样 drop，**不能把“不能证明匹配”取反成“全都匹配”**。

## 5. drop / pass / filter 的推导与保守边界

对每个文件，在取值约束上计算表达式的**可能真值集合**：

* `drop`：可能真值不含 `TRUE` —— 不存在能让条件为 `TRUE` 的合法源行；
* `pass`：可能真值恰为 `{TRUE}` —— 每一条合法源行都让条件为 `TRUE`；
* `filter`：其余 —— 给出可由公开行求值器执行的剩余条件。

精确的部分：

* 每个叶子在列约束上的可能真值是**精确**的（比较叶把约束区间与叶子
  真值区间求交/求差；null 叶看 null 是否可能）；
* **同一列**的比较与空值叶子组成的纯 `and` 会把各叶真值区间与列约束
  一起求交：截断区间 `[-10,-1]` 上的 `x>=-3 and x<-3` 直接判空；
  同源 `identity=-1` 已把宽区间收成一个值时也同样识别；
* `not` 逐值取反；跨列 `and` 按列分组后合并（列间独立，合并是精确的）。

保守的部分（**只会多保留文件，绝不漏行、绝不错误 pass**）：

* 跨 `or` 的关联、以及 `and` 之下嵌套子式之间的关联，按独立假设合并
  可能真值——可能把本可 `drop`/`pass` 的保守成 `filter`，但方向永远
  安全：多读的代价，不是漏行或错放行。

剩余条件可以保留原式（不要求最短），但对**该文件**所有可能源行必须
保持“是否最终 `TRUE`”的等价性；本模块只做一种化简——删掉对本文件
恒 `TRUE` 的 `and` 子式、恒 `FALSE` 的 `or` 子式——不会省掉仍会改变
结果的条件。剩余条件本身仍是合法查询表达式（测试里会重新校验并在
该文件全部行上与原式对拍）。

## 6. 验收目录与手写推导

`examples/catalog/` 混放三代 spec（说明名称故意相同或改名）：

| spec | 账户（field 1） | 地区（field 2） |
| --- | --- | --- |
| `old-2023` | `truncate(10)`，名“账户区间” | `identity`，名“地区” |
| `narrow-2024` | `truncate(4)`，名“账户区间” | `prefix(2)`，名“地区” |
| `prefix-2025` | `identity`，名“账户” | `prefix(1)`，名“地区前缀” |
| `dual-2025` | `truncate(10)`+`identity` 双分区，名“账户区间”/“账户快照” | `void`，名“地区” |
| `void-2025` | `void`，名“账户” | `prefix(2)`，名“地区” |

12 个文件覆盖 -11、-10、-1、0、3、4、int64 两端（含分区值
`-9223372036854775810`，即 `truncate(10)` 略低于 int64 下界的桶）、
null、空串、中文与非 BMP 字符（𠮷）。关键联动（手写期望见
`tests/test_plan.py` 的 `EXPECTED_DECISIONS`）：

查询 `not(account<-3)`（`q1`）：

| 文件 | 约束推导 | 结论 |
| --- | --- | --- |
| f-old-wide | `truncate(10)=-10` → `[-10,-1]`，跨 -3 | **filter** |
| f-new-narrow | `truncate(4)=-4` → `[-4,-1]`，跨 -3 | **filter** |
| f-same-source | `[-10,-1] ∩ {-1}` = `{-1}`，恒非 `<-3` | **pass** |
| f-all-null | 仅 null → `not(UNKNOWN)=UNKNOWN` | **drop** |
| f-void | 一无所知 | **filter**（保守） |

查询 `account>=-3 and account<-3`（`q2`）：同列纯 `and` 真值区间交集为
空，全部 12 个文件 **drop**；`not(…)`（`q3`）下全 null 文件仍为
`UNKNOWN` → **drop**（null 不会穿过 not 被交付）。

地区前缀（`q4`/`q5`/`q6`）：`prefix(2)="京𠮷"` 是完整前缀 → 等值
`region="京𠮷"` 必须保留剩余过滤（filter）；`prefix(2)="京"` 是短值 →
源值恰为 `"京"`；`region>="京𠮷" and region<"京𠮸"` 恰好圈出该前缀，
`prefix(2)="京𠮷"` 的文件直接 **pass**，而 `prefix(1)="京"` 的文件界内
界外都有可能 → **filter**。

## 7. 终端操作：plan 与 scan

```bash
cd hidden-partition-planner

# 规划：只读 manifest 与查询，绝不触碰数据路径
python3 -I bin/hpq.py plan \
  --manifest examples/catalog/manifest.json \
  --query examples/queries/q1_not_account_lt_neg3.json \
  --output /tmp/plan.json

# 执行同一计划：只打开 pass/filter 文件，逐行交付
python3 -I bin/hpq.py scan \
  --manifest examples/catalog/manifest.json \
  --query examples/queries/q1_not_account_lt_neg3.json \
  --output /tmp/rows.jsonl --report /tmp/report.json \
  --projection 2,1
```

退出码：`0` 成功；`2` 目录/查询/数据/上限/参数错误（stderr 输出 JSON
定位）；`3` 目标路径已存在、拒绝覆盖（原文件字节不动）。

### plan 输出

按 manifest 文件顺序给出每个文件的 `decision`、使用的 `spec_id`、
可核对的分区推导理由（`reasons`：每个分区字段的原像、同源交集、
每个叶子的可能真值、根节点结论）、机读约束摘要（`constraints`）与
可能真值（`outcomes`）；`filter` 另带 `residual`。目录级错误在输出
任何计划之前报出；已有计划文件不被覆盖。

### scan 行为

* 数据是 UTF-8 JSONL，每行一个对象，以十进制 `field_id` 字符串为键，
  列齐全、值符合 schema（`bool` 不算整数；int64 必须在界内）；
* 只打开 `pass`/`filter` 文件，逐行处理；`drop` 文件即便路径不存在或
  内容损坏也绝不触碰；零行文件（规划为 `drop`）同样不打开；
* 投影是有序、非重复的逻辑 `field_id` 列表（`--projection 2,1`；缺省为
  schema 声明顺序）；输出对象以当前 schema 的 `name` 为键、按投影顺序；
  过滤需要但未投影的列参与判断，不混入返回列；
* 保持文件顺序、文件内行序、重复行与 null；不因分区命中凭空生成行；
  仅有 `UNKNOWN` 时输出空结果，空结果也在报告中给出投影列名和类型；
* 报告给出真实打开的 `file_id`、每文件读入行数/交付行数、投影列名与
  类型、耗时与峰值 RSS；
* 选中文件缺失、JSON 重复键、非法 UTF-8、坏字段、实际行数与
  `row_count` 不符 → 退出码 2，**不交付半份结果**。

### 输出与临时文件安排

结果与报告都写到**新路径**：先在目标同目录写临时文件
`.<目标名>.tmp.<pid>`，`fsync` 后用硬链接原子落位（`os.link`，已存在则
失败；不支持硬链接的文件系统退回先检查后改名）。任何失败都会清理临时
文件——目标路径要么完整出现，要么根本不存在；已存在的同名文件字节
不动。报告在结果落位之后写出；计划文件同理。输入（manifest、查询、
数据）全程只读。

### 明示上限

| 上限 | 默认 | 说明 |
| --- | --- | --- |
| `max_line_bytes` | 1 MiB | 单个数据行字节数（含换行符） |
| `max_expr_depth` | 64（硬顶 256） | 表达式嵌套深度 |
| `max_expr_nodes` | 10000 | 表达式节点数 |
| `max_manifest_bytes` | 256 MiB | manifest 文件大小 |
| `max_query_bytes` | 16 MiB | 查询文件大小 |

目录与表达式可放内存；数据文件逐行处理，只保留当前行，不全量收集
结果（峰值 RSS 与行数无关，见第 9 节）。

## 8. Python API

```python
from hidden_partition import (
    load_manifest, load_query, build_plan, run_plan, run_scan,
    evaluate, row_matches, Limits,
)

manifest = load_manifest("examples/catalog/manifest.json")
query = load_query("examples/queries/q7_account_eq_neg1.json",
                   manifest.fields_by_id)
plan = build_plan(manifest, query)          # 纯内存，不碰磁盘
report = run_scan("examples/catalog/manifest.json",
                  "examples/queries/q7_account_eq_neg1.json",
                  "/tmp/rows.jsonl", "/tmp/report.json",
                  projection=[2, 1])
# 公开行求值器：filter 的剩余条件由它执行
assert row_matches(query, {1: -1, 2: "京"}) is True
```

## 9. 可重现的验证

```bash
cd hidden-partition-planner
python3 -I bin/make_fixtures.py        # 重新生成验收目录（幂等）
python3 -I bin/run_tests.py -v         # 125 个测试
python3 -I bin/demo.py /tmp/hpq-demo   # 端到端演示：计划理由 + 真实交付行
```

### 文件访问监测（plan 零数据访问、scan 不碰 drop 路径）

`tests/test_access.py` 用 CPython 审计钩子记录进程内全部 `open` 事件，
并用 mock 包裹 `os.stat`/`os.path.exists`/`os.listdir`/`os.scandir`：
`run_plan` 期间任何对数据目录的打开或 stat 都会直接炸掉测试。同时在
drop 路径上故意放置损坏文件与不存在路径：窄查询（`account=3`）照常
成功且监测证明那两个路径从未被打开；改成必须读取它们的条件
（`account<10`）则真实失败（退出码 2，无输出文件）。

### 规模基准（逐行交付的证据）

```bash
python3 -I bin/bench.py --dir /tmp/b1 --files 200 --rows-per-file 1000
python3 -I bin/bench.py --dir /tmp/b2 --files 200 --rows-per-file 100000
python3 -I bin/bench.py --dir /tmp/b3 --files 2000 --rows-per-file 1000
```

本机一次实测（等值查询只命中一个桶）：

| 规模 | plan 耗时 | plan 峰值 RSS | scan 打开文件 | scan 读行 | scan 峰值 RSS |
| --- | --- | --- | --- | --- | --- |
| 200 文件 × 1 千行 | 0.009 s | 13.2 MB | 1 / 200 | 1 000 | 13.7 MB |
| 200 文件 × 10 万行 | 0.009 s | 13.2 MB | 1 / 200 | 100 000 | 13.5 MB |
| 2000 文件 × 1 千行 | 0.078 s | 20.4 MB | 1 / 2000 | 1 000 | 13.7 MB |

行数放大 100 倍，scan 峰值 RSS 基本不动——逐行交付、不全量收集；
文件数放大 10 倍，打开文件数仍是 1——分区裁剪生效，不为查一个账户
打开所有文件。

### 测试对照

| 要求场景 | 测试 |
| --- | --- |
| manifest 合同（结构/类型/引用/分区值/矛盾目录/零行文件） | `tests/test_contract.py` |
| 查询合同（叶子/逻辑节点/位置/深度与节点上限） | `tests/test_query.py` |
| 原像、区间运算、每种单叶的 drop/pass、同列纯 and 交集 | `tests/test_analyze.py` |
| 手写计划期望（11 查询 × 12 文件）、剩余条件等价性 | `tests/test_plan.py` |
| scan 交付行（手写 + 独立参考对拍）、投影、失败交付状态 | `tests/test_scan.py` |
| 必测联动（not/空交集 and/前缀码点边界/混合逻辑） | `tests/test_linkage.py` |
| 文件访问监测（plan 零数据访问、scan 不碰 drop） | `tests/test_access.py` |
| 终端退出码 0/2/3 | `tests/test_cli.py` |

独立行过滤参考在 `tests/_helpers.py`：它直接对**原始查询 JSON** 逐行
求三值，不使用规划器的剩余条件；scan 的全部查询结果都与它对拍。

## 10. 目录结构

```
hidden-partition-planner/
├── hidden_partition/       # 独立模块（Python API）
│   ├── contract.py         # manifest 输入合同校验
│   ├── query.py            # 查询表达式树与校验
│   ├── constraints.py      # 列取值约束（区间 + null 可能性）
│   ├── transforms.py       # 变换定义、分区值校验、原像
│   ├── intervals.py        # 半开区间列表运算
│   ├── analyze.py          # 三值可能真值推导（drop/pass/filter）
│   ├── evaluator.py        # 公开行求值器（同一三值合同）
│   ├── planner.py          # plan 构建与落盘
│   ├── scanner.py          # scan 执行（只开 pass/filter，逐行）
│   ├── jsutil.py           # 严格 JSON 读写 + 原子落位
│   ├── limits.py           # 明示上限
│   ├── errors.py           # 错误类型与定位
│   └── cli.py              # 终端 plan/scan
├── bin/
│   ├── hpq.py              # 终端入口（-I 隔离）
│   ├── run_tests.py        # 测试入口
│   ├── make_fixtures.py    # 生成验收目录（幂等）
│   ├── bench.py            # 规模基准（文件数/行数可调）
│   └── demo.py             # 端到端演示
├── examples/
│   ├── catalog/            # 验收目录：manifest.json + data/*.jsonl
│   └── queries/            # 11 个样例查询
└── tests/                  # unittest 套件（125 个）
```
