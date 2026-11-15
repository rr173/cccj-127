# 离线 IPFIX 接入（ipfix-ledger）

把设备侧按接收顺序保存的 IPFIX 采样报文（UTF-8 JSONL），离线转成**有字段值、
有模板依据、能追回原始字节**的 SQLite 记录库。数据仓库只给模板编号，同一模板号
稍后可能更换布局；本模块按报文到达位置使用当时的模板代次，绝不用文件末尾的
模板回头解释早先的数据集。

技术栈：Python 3.11 标准库 + SQLite（无第三方依赖）。不监听端口，不做 TCP 重组、
攻击识别、流量可视化或设备管理。仓库中原有的 `calendar-fact-service` 与
`code-mapping-service` 两个服务入口未改动。

---

## 1. 支持子集（明确不是完整 IPFIX 实现）

本模块只接受题目约定的 v10 字节子集，不宣称支持 RFC 7011 全量：

| 项目 | 行为 |
| --- | --- |
| 消息头 | 16 字节大端 `version:u16 length:u16 exportTime:u32 sequenceNumber:u32 observationDomainId:u32`；`version` 必须 10，`length` 必须等于本行真实字节数 |
| Set | `setId:u16 setLength:u16`，setLength 含 4 字节头、≥4、完全落在消息内；不跨消息 |
| `setId=2` | Template Set：`templateId:u16 fieldCount:u16` + 字段说明 |
| `setId=3` | Options Template Set：**明确拒绝** |
| `setId` 0/1 或 4..255 | 保留/未知：**明确拒绝**，不扫描后续字节猜边界 |
| `setId≥256` | Data Set，只能用该位置之前同作用域仍有效的模板 |
| 模板号 | ≥256；`fieldCount=0` 撤销这个确切模板号，撤销未登记编号报错 |
| 字段说明 | `elementId:u16 fieldLength:u16`；elementId 最高位为 1 时清除并读 `enterpriseNumber:u32`（否则 0）；`fieldLength` 1..65534 固定或 65535 变长 |
| 最小记录长度 | 各固定长度与各变长字段 1 字节前缀之和 ≥4，否则模板定义报错 |
| 变长字段 | 首字节 0..254 为长度；255 后跟大端 u16 且扩展值须 ≥255；零长合法 |
| Set 填充 | 仅 Set 末尾 0..3 字节且全零可为填充；Data Set、Template Set 同样规则，其它残余/半条记录报错 |
| 消息内容 | 允许纯消息头、纯模板消息；一条消息可含多个 Data Set |
| 序列号 | 每个 `(exporter_id, transport_id, observationDomainId)` 第一条任意起值；后继 = 前一条 sequenceNumber + 前一条实际解码出的 Data Record 条数（mod 2^32）。模板、撤销、Set 数、字节、填充都不计数 |

### 已知元素解释（仅 enterpriseNumber=0）

| elementId | 名称 | 长度 | 解释 |
| --- | --- | --- | --- |
| 1 | octetDeltaCount | 1..8 | 大端无符号整数（含 2^64-1，无损） |
| 2 | packetDeltaCount | 1..8 | 同上 |
| 7 | sourceTransportPort | 恰 2 | u16 |
| 11 | destinationTransportPort | 恰 2 | u16 |
| 4 | protocolIdentifier | 恰 1 | u8 |
| 8 | sourceIPv4Address | 恰 4 | 点分十进制 |
| 12 | destinationIPv4Address | 恰 4 | 点分十进制 |
| 27 | sourceIPv6Address | 恰 16 | RFC 5952 文本（`::` 压缩） |
| 28 | destinationIPv6Address | 恰 16 | 同上 |
| 152 | flowStartMilliseconds | 恰 8 | UTC 毫秒整数，不经浮点/本地时区 |
| 153 | flowEndMilliseconds | 恰 8 | 同上 |

* 已知元素声明了不合上表的长度（如 IPv4 声明 5、octetDeltaCount 声明 65535）
  **报错**，不降格成未知字段；
* 非零 enterpriseNumber 即使元素号相同也保存原始字节（`raw_bytes`），不套 IANA；
* 未知元素按声明长度完整消费并保存原始 hex，后续字段不会错位；
* 每条记录同时保存“消息内字段载荷区间”与“模板字段说明位置”；重复元素按
  `field_ordinal` 各占一行，追查不会只剩最后一项；
* 整数（包括超过 SQLite 有符号 64 位范围的值）在库内以原始 hex + `value_type`
  保存，查询时用 `int.from_bytes` 无损还原；JSON 中整数写成**十进制字符串**并
  带 `value_type`。

### 模板状态与隔离

* 作用域 = `(exporter_id, transport_id, observationDomainId)`；不同设备、会话、
  domain 的模板定义互不共享，即使模板号相同；
* `transport_id` 标识一次已重组的可靠传输会话；**只有新的 transport_id 才是新会话**，
  序列号变小不会清空模板；
* 模板定义从所在位置起生效；同号新定义立即替换布局并产生新的代次；相同布局再次
  定义也产生新代次；撤销后重新定义继续递增代次；
* 一个 Template Set 内可以多次定义/撤销同号模板，按记录顺序处理；
* 撤销不会改写此前记录的依据：每条记录引用其定义事件的 `event_id`，历史事件
  永不被更新或删除；
* 每个作用域的有效模板数有可配置上限（默认 1024，`--max-templates-per-scope`），
  超限明确失败，不静默淘汰仍有效模板；同一模板号重定义/撤销不额外占名额。

### 明确不做

不接收监听流量、不做 TCP 重组（输入必须是已重组的 `transport_id`）、不做
IPFIX 选项模板、企业字典解释、攻击识别、可视化或设备管理；不新增增量修订账本、
后台任务或断点续办。

---

## 2. 输入约定

UTF-8 JSONL，每行恰含三个键：

```json
{"exporter_id":"exporter-1","transport_id":"transport-A","message_hex":"000a0060..."}
```

* `exporter_id`、`transport_id` 为非空原样字符串（不 strip、不改大小写）；
* `message_hex` 是一个完整 IPFIX Message 的十六进制字节（偶数长度、仅 0-9a-fA-F）；
* 行序 = 接收顺序，不按 exportTime 排序；
* 拒绝：重复 JSON 键、多余或缺少字段、空行、坏十六进制、非 UTF-8、非字符串标识、
  空标识、非标准 JSON 常量。

读取器逐行消费（`ipfix_offline/capture.py:iter_capture`），单条消息大小由协议
`length:u16` 约束（≤65535 字节）。

---

## 3. 命令（终端三个操作）

所有命令在 `-I` 隔离下运行，脚本自己注入工程根，避免夹具目录被植入模块。

```bash
cd ipfix-ledger

# 导入：把捕获严格接入为新结果库；目标已存在则拒绝覆盖
python3 -I bin/ipfixctl.py import \
  --input tests/fixtures/generation.jsonl --db /tmp/result.db \
  [--batch-size 2000] [--max-templates-per-scope 1024] [--cache-kb 8192]

# 查记录：按作用域 / 捕获行 / 模板号过滤，固定按 捕获行→Set→Set内记录 排序
python3 -I bin/ipfixctl.py list --db /tmp/result.db \
  --exporter exporter-1 --transport transport-A --domain 7 \
  --capture-line 1 --template 256 --limit 100 --offset 0

# 追字段：给出载荷区间、变长前缀区间、模板字段说明位置；
# 值切片与模板说明切片都从原始消息现场切出并校验
python3 -I bin/ipfixctl.py trace --db /tmp/result.db --record 2 --field 2
```

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | 捕获/参数错误、结果库不可读、目标目录不存在等（stderr 输出 JSON 定位） |
| 3 | 目标结果库已存在，拒绝覆盖（原文件原字节不动） |

导入失败输出示例（stderr）：

```json
{
  "ok": false,
  "failures": [{
    "phase": "sequence", "reason": "sequenceNumber 缺口",
    "capture_line": 3, "message_offset": 8,
    "set_ordinal": null, "record_ordinal": null, "field_ordinal": null,
    "detail": "期望 13，实际 14（上一条消息 sequenceNumber + 已解码记录数）"
  }]
}
```

`phase` 取值：`capture`（JSONL 行）、`header`、`set`、`template`、`data`、
`sequence`、`internal`。`message_offset` 是该消息内的零起点字节偏移。

### Python API

```python
from ipfix_offline import import_capture, ResultStore, IpfixInputError

report = import_capture("capture.jsonl", "result.db")       # 失败抛 IpfixInputError
store = ResultStore("result.db")                            # 只读
records = store.query_records(exporter_id="e1", template_id=256)
trace = store.trace_field(record_ordinal=2, field_ordinal=2)
```

`report.to_json()` 给出消息数、记录数、模板定义/撤销数、耗时、峰值 RSS、结果库
大小和批次/缓存参数。

---

## 4. 结果库结构

单文件 SQLite：

| 表 | 内容 |
| --- | --- |
| `meta` | 结构版本、计数、批次/缓存/模板上限参数 |
| `messages` | 每行一条原始消息：hex、exportTime、sequenceNumber、长度、记录数 |
| `template_events` | 每次定义/撤销：捕获行、Set 序号、记录偏移、代次、字段数 |
| `template_fields` | 定义事件内每个字段：序号、enterprise、元素号、声明长度、**说明在消息内的偏移与长度** |
| `records` | 每条 Data Record：捕获行、Set 序号、Set 内序号、模板号、代次、记录偏移/长度 |
| `record_fields` | 每个字段：声明/实际长度、原始 hex、值类型与解码值、**载荷区间**、**变长前缀区间**、模板字段说明位置 |
| `record_view` | 连接 messages 的只读视图 |

`record_ordinal` 从 1 自增，顺序固定为“捕获行 → Set 位置 → Set 内记录序号”。
同一模板的重复元素保留独立 `field_ordinal`；旧记录始终引用其当时的
`template_event_id`，替换或撤销模板后仍可追到依据。

---

## 5. 失败语义与原子落盘

* 任一行失败即整份捕获失败，返回非零并定位捕获行 + 消息偏移（+ Set/记录/字段）；
  不会报告“正常零条流量”；
* 导入先写同目录临时库 `.ipfix-staging-*.db`，全部通过后 `os.link` 到目标路径
  （同目录原子且**不覆盖**已有文件），再删除临时文件；
* 失败路径删除临时库：**目标库不存在**，`ResultStore` 打不开，查记录入口报错；
* 目标库已存在时不做任何写入，原字节不动；
* 输入只以只读方式打开，不修改字节/mtime。

序列号核实基于**完整解码后的记录数**：一条消息的模板和数据集先全部解析成功，
才把 `sequenceNumber + 实际记录数` 记为期望进度；模板消息的序列号因此可以连续
相等。

---

## 6. 资源开销与内存界限

导入过程不把业务记录或原始消息累积到 Python 集合：

* 捕获逐行读取（`iter_capture`），每行只持有当前消息的解码结果；
* 模板状态按作用域保留“当前有效模板 + 每个模板号的下一个代次”，大小受
  `--max-templates-per-scope` 限制；历史定义/撤销位置随行落盘，不在内存累积；
* 每条记录立即 `INSERT`（需要自增 `record_ordinal`），字段行攒到
  `--batch-size`（默认 2000）后 `executemany`；
* SQLite 页缓存上限 `--cache-kb`（默认 8192 KiB），`temp_store=MEMORY`。

`tests/test_boundaries.py` 用 `weakref` 断言导入结束后历史 `DecodedMessage`
不再被持有；`bin/bench.py` 用只读游标逐行核对全部记录而不建列表。

### 十万条实测（本机，Linux，Python 3.11）

命令：`python3 -I bin/bench.py --records 120000 --workdir /tmp/ipfix-bench`

| 指标 | 值 |
| --- | --- |
| 记录数 | 120,000（逐行核对 120,000，首尾字段一致） |
| 捕获 | 20,048 行 / 11,929,024 字节（约 11.4 MiB） |
| 导入耗时 | 7.4–8.3 s（多次运行 14,400–16,200 条/秒） |
| 峰值 RSS（导入后） | 23.3–23.4 MiB；导入前基线约 13.8 MiB（增量约 9.6–9.8 MiB，主要为 SQLite 页缓存与解释器常驻） |
| 结果库大小 | 100,810,752 字节（约 96 MiB，约 840 字节/条，含原始消息与逐字段区间） |
| 模板作用域 | 8 个（2 exporter × 2 transport × 2 domain，同号 400 各自定义） |

RSS 不随记录数线性增长：`bench.py` 逐行查询用只读游标，不把 12 万条放进内存。
每次运行的结果写入 `<workdir>/bench-report.json`。

---

## 7. 演练与验证

### 模板换代（`tests/fixtures/generation.jsonl`，手算字节与偏移见
`tests/fixtures/README.md`）

| 行 | 消息 | 手算结果 |
| --- | --- | --- |
| 1 | sequence=1000，Template Set 定义 256=(IPv4源/4, octetDelta/8)，Data Set 1 条；同一消息随后定义 256=(IPv4源/4, IPv4目/4, packetDelta/4)，Data Set 2 条 | 记录 1 代次 1，octet=`ffffffffffffffff`=18446744073709551615；记录 2/3 代次 2，packet=10/11 |
| 2 | sequence=**1003**（=1000+3 条记录） | 使用代次 2，源=203.0.113.9、目=203.0.113.10、packet=99 |
| 3–4 | transport-B 定义 256=(IPv4源, packetDelta) | 同号不同布局，代次从 1 起，记录 10.0.0.1/1、10.0.0.2/2 |
| 5–6 | domain=9 定义 256=(IPv4源, protocol, 目的端口) | `10.0.0.3 / 6 / 8080` |
| 7 | fieldCount=0 撤销 256 | 不产生记录，模板消息序列号不变 |
| 8–10 | 重定义（代次3）、同布局再定义（代次4）、数据 | 记录使用代次 4，毫秒 1700000000123/1700000000584；记录 1 仍可追到代次 1 |
| 11–15 | exporter-2 从 4294967290 起，每消息 2 条 | 4294967292 → 4294967294 → **0**（2^32 绕回） |

被断言能发现的反例：

* 把 Set 数（4）或模板记录数（2）当记录数：第 2 行 1003 会被判成缺口；
* 用最终模板回解第 1 行第一条记录：`octetDeltaCount` 的 8 字节会被读成两个
  4 字节字段，`template_generation` 断言失败；
* 丢掉 8 字节无符号最大值：`fields[1]["value"] == "18446744073709551615"` 失败。

### 变长/企业/重复元素/填充（`tests/fixtures/variable.jsonl`）

零长与 254/255 字节变长字段、非零企业号变长、紧邻的已知字段位置、重复元素、
IPv6、1 字节精简计数、0..3 字节填充均有逐字段断言；`trace` 的前缀区间和载荷
区间必须首尾相接，切片必须等于保存的 hex。

### 拒绝演练（`tests/fixtures/rejections/`，21 对坏/好捕获）

覆盖：数据在模板之前、撤销后仍发送、撤销未登记模板、非法已知字段长度、
fieldLength=0、Set 越界、消息 length 不符、扩展长度截断、扩展值 <255、
变长载荷截断、非零填充、4 字节非零残余、序列缺口、模板消息序列号计 0、
最后一行坏消息、Options Template Set、保留 setId、模板号 <256、
跨 domain 借用模板、短记录残余。每例都断言：错误定位、目标库不存在且
`ResultStore` 打不开、同一场景改正后能成功接入。

---

## 8. 运行

```bash
cd ipfix-ledger

# 重新生成手写夹具（生成器只拼字节，不用正式解码器推导答案）
python3 -I bin/make_fixtures.py
python3 -I bin/make_rejection_fixtures.py

# 全部单测
python3 -I bin/run_tests.py -v

# 端到端演示（模板换代、追字段、拒绝与改正）
python3 -I bin/demo.py

# 十万条性能/内存实测
python3 -I bin/bench.py --records 120000 --workdir /tmp/ipfix-bench
```

---

## 9. 目录结构

```
ipfix-ledger/
├── ipfix_offline/
│   ├── capture.py     # JSONL 严格逐行读取、hex 校验
│   ├── protocol.py    # 消息/Set/模板/记录字节级解码、作用域状态、序列号
│   ├── values.py      # IANA 子集解码、IPv6 文本、整数价值类型
│   ├── storage.py     # SQLite 结构、导入编排、批次、原子落盘、ImportReport
│   ├── store.py       # 只读查询：记录、消息、模板事件、字段追查
│   ├── inspect.py     # 只解码不落盘的侦察工具
│   ├── errors.py      # Failure 定位与错误类型
│   ├── cli.py         # import/list/trace 子命令
│   └── __init__.py
├── bin/
│   ├── ipfixctl.py    # -I 隔离终端入口
│   ├── run_tests.py
│   ├── demo.py
│   ├── make_fixtures.py
│   ├── make_rejection_fixtures.py
│   ├── make_bench_capture.py
│   └── bench.py
└── tests/             # 68 个 unittest + fixtures（含手算偏移 README）
```

---

## 10. 验证边界说明

本模块的验证覆盖上面列出的字节子集与业务约束；它**不是**完整 IPFIX 实现的
证明：Options Template、IANA 全量信息元素、企业字典、模板持久化/重传语义、
TCP/SCTP 重组、传输层安全等均不在范围内。超出子集的输入会明确失败，而不是
被猜测解释。
