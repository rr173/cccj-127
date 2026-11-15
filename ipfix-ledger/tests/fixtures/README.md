# 夹具说明（手算报文字节与偏移）

本目录的 `.jsonl` 由 `bin/make_fixtures.py` 与
`bin/make_rejection_fixtures.py` 生成。生成脚本只做**二进制拼接**（把常量
`0008 0004` 这样的字段说明贴在一起、算长度），不调用正式解码器推导期望值，
也没有“解析后再回写 hex”的步骤。测试中的期望值来自下面这些手算表。

## 1. `generation.jsonl`：模板 256 换代

### 第 1 行（96 字节，sequenceNumber=1000，domain=7）

```
偏移  字节                        含义
 0    00 0a                        version=10
 2    00 60                        length=96
 4    65 53 f1 00                  exportTime=1700000000
 8    00 00 03 e8                  sequenceNumber=1000
12    00 00 00 07                  observationDomainId=7
16    00 02                        setId=2（Template Set）
18    00 10                        setLength=16
20    01 00                        templateId=256
22    00 02                        fieldCount=2
24    00 08 00 04                  字段0 element 8（IPv4 源地址）长 4
28    00 01 00 08                  字段1 element 1（octetDeltaCount）长 8
32    01 00                        setId=256（Data Set，模板世代1）
34    00 10                        setLength=16
36    c0 00 02 01                  192.0.2.1
40    ff ff ff ff ff ff ff ff      18446744073709551615（2^64-1）
48    00 02                        setId=2（Template Set，同号新布局）
50    00 14                        setLength=20
52    01 00                        templateId=256
54    00 03                        fieldCount=3
56    00 08 00 04                  字段0 element 8（IPv4 源）长 4
60    00 0c 00 04                  字段1 element 12（IPv4 目的）长 4
64    00 02 00 04                  字段2 element 2（packetDeltaCount）长 4
68    01 00                        setId=256（Data Set，模板世代2）
70    00 1c                        setLength=28
72    c0 00 02 02                  记录0 源 192.0.2.2
76    c6 33 64 01                  记录0 目的 198.51.100.1
80    00 00 00 0a                  记录0 packetDeltaCount=10
84    c0 00 02 03                  记录1 源 192.0.2.3
88    c6 33 64 02                  记录1 目的 198.51.100.2
92    00 00 00 0b                  记录1 packetDeltaCount=11
```

由此得到的手算结论：

| 记录 | 捕获行 | Set 序号 | Set内序号 | 模板代次 | 字段 0 | 字段 1 | 字段 2 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 0 | 1 | 偏移 36..40 = `c0000201` = 192.0.2.1 | 偏移 40..48 = `ffffffffffffffff` | — |
| 2 | 1 | 3 | 0 | 2 | 偏移 72..76 = 192.0.2.2 | 偏移 76..80 = 198.51.100.1 | 偏移 80..84 = 10 |
| 3 | 1 | 3 | 1 | 2 | 偏移 84..88 = 192.0.2.3 | 偏移 88..92 = 198.51.100.2 | 偏移 92..96 = 11 |

模板字段说明位置：世代 1 的字段 0 在 24..28、字段 1 在 28..32；
世代 2 的字段 0/1/2 分别在 56..60、60..64、64..68。

### 第 2 行（32 字节，sequenceNumber=1003）

`1000 + 3 条记录 = 1003`。若错把 Set 数（4）或模板记录数（2）计入，
下一行必然是 1004/1005 从而被断言发现。

```
16    01 00                        setId=256（沿用世代2）
18    00 20                        setLength=32
20    cb 00 71 09                  203.0.113.9
24    cb 00 71 0a                  203.0.113.10
28    00 00 00 63                  99
```

### 第 3/4 行：新 transport_id、同号不同布局

transport-B 只定义 256 =（源IPv4/4 + packetDeltaCount/4），record 从偏移 20 开始，
两条记录 `10.0.0.1/1` 与 `10.0.0.2/2`。transport-B 的序列号从 500 起，模板定义
消息本身不推进序列号（第 3 行 500、第 4 行仍 500）——这与“模板消息序列号可连续
相等”的要求对应。

### 第 5/6 行：domain=9 的 256 再换布局

256 =（源IPv4/4 + protocolIdentifier/1 + 目的端口/2）= 7 字节，记录
`10.0.0.3 / 6 / 8080`。它与 domain=7、transport-A 的 256 完全无关。

### 第 7–10 行：撤销与重定义

* 第 7 行：`00 02 00 08 01 00 00 00` 是 Template Set 中 fieldCount=0 的
  撤销记录，撤销 templateId=256；此消息不产生记录。
* 第 8 行：重新定义 256 =（源IPv4/4 + flowStartMilliseconds/8 +
  flowEndMilliseconds/8），代次 3。
* 第 9 行：相同布局**再次**定义，代次 4（每次定义都产生新的可追查代次）。
* 第 10 行：使用代次 4 解出的记录，毫秒时间戳为
  `0x0000018bcfe5687b = 1700000000123`、`0x0000018bcfe56a48 = 1700000000584`。
  记录 1 仍然引用第 1 行的世代 1 定义（撤销不会让旧记录失去依据）。

### 第 11–15 行：序列号接近 2^32 的绕回

exporter-2/transport-A/domain=7 从 4294967290 开始，每消息 2 条记录：

```
4294967290 + 2 = 4294967292
4294967292 + 2 = 4294967294
4294967294 + 2 = 4294967296 mod 2^32 = 0
```

最后一行的序列号是 0，且模板 256 在 exporter-2 的布局是（目的IPv4/4 +
packetDeltaCount/4），与 exporter-1 不同。

## 2. `variable.jsonl`：变长、零长、企业字段、重复元素与填充

模板 300 的字段列表（enterprise 均为 0 除特别标注）：

```
0  element 8   固定 4     sourceIPv4Address
1  element 999 变长       未知字段
2  element 500 固定 2    未知字段
3  element 4000 企业号 9  变长
4  element 2   固定 4     packetDeltaCount
5  element 27  固定 16    sourceIPv6Address
6  element 8   固定 4     重复的 sourceIPv4Address
7  element 4   固定 1     protocolIdentifier
8  element 777 变长       未知字段（8 字节全 f 的样例）
9  element 8 / enterprise 4242 固定 4（与 IANA 的 element 8 无关，保持原始字节）
```

手算结构（消息共 984 字节）：

```
消息头          0..16
Template Set   16..75    setId=2, setLength=59 = 4 + 52 + 3 字节零填充
  templateId=300 在 20..22，fieldCount=10，10 条字段说明占 52 字节
Data Set 头    75..79    setId=300, setLength=909
记录 0         79..382
记录 1        382..675
记录 2        675..981
Set 填充      981..984   3 字节零
```

模板字段说明（字段序号: 区间）：
`0:24..28, 1:28..32, 2:32..36, 3:36..44（企业号说明 8 字节）, 4:44..48,
5:48..52, 6:52..56, 7:56..60, 8:60..64, 9:64..72（企业号）`。

记录 0 的字段位置（全部由“前一段结束处”手推）：

| 字段 | 区间 | 值 | 说明 |
| --- | --- | --- | --- |
| 0 | 79..83 | 192.0.2.1 | 固定 4 |
| 1 前缀 | 83..84 | `00` | 零长：前缀 1 字节，载荷 0 字节 |
| 1 载荷 | 84..84 | 空 | |
| 2 | 84..86 | `0007` | 固定 2；紧接变长字段，位置不因前缀漂移 |
| 3 前缀 | 86..89 | `ff 00 ff` | 扩展长度 255 |
| 3 载荷 | 89..344 | 255 × `41` | 企业号 9 |
| 4 | 344..348 | 10 | |
| 5 | 348..364 | `2001:db8::1` | 16 字节 IPv6 |
| 6 | 364..368 | 192.0.2.2 | 重复元素 8 的第二次出现 |
| 7 | 368..369 | 6 | 1 字节精简字段 |
| 8 前缀 | 369..370 | `08` | 变长未知字段，长 8 |
| 8 载荷 | 370..378 | `ffffffffffffffff` | 2^64-1 原文 |
| 9 | 378..382 | `0000000a` | enterprise 4242 的 element 8，不套用 IANA |

记录 1 从 382 开始：字段 1 用 254 字节直接前缀 `fe`，字段 3 企业字段零长，
字段 8 为 1 字节 `ff`；记录 2 从 675 开始：字段 1 用 255 字节扩展前缀，
字段 3 为 3 字节 `aabbcc`。记录 0 的 Set 末尾（981..984）是 3 字节零填充。

第 2 行是纯消息头（无 Set，序列号不推进）；第 3 行先发一个空 Data Set，
再发一条记录（`de000001`/`de000002` 两次 element 8），用于核对空 Set 与
“重复元素、变长前缀不侵占后字段”在另一个消息中同样成立。
第 4 行在**同一个 Template Set** 内定义 302、换布局、撤销、再定义（代次 1/2/3），
第 5 行使用代次 3。

第 8–11 行是 1 字节精简计数（element 1，长度 1）、3 字节计数（element 2，长度 3）
与 Set 填充的组合：第 8 行两条记录后 3 字节零填充，第 9 行恰好无填充，
第 10 行 1 字节、第 11 行 2 字节零填充。

## 3. `rejections/`：一对坏/好捕获

`manifest.json` 记录每例期望失败在哪个阶段、哪一行、哪个字段。每对文件
只改一处：坏文件触发错误，好文件能被成功接入。21 对覆盖：

```
data_before_template              send_after_revoke
revoke_unregistered               illegal_known_length
illegal_ipv4_length               field_length_zero
set_beyond_message                message_length_lies
extended_length_truncated         variable_payload_truncated
extended_length_below_255         nonzero_padding
four_byte_nonzero_tail            sequence_gap
sequence_template_only_counts_zero last_line_bad
options_template_set              reserved_set_id
template_id_below_256             template_before_domain_definition
short_record_tail
```

## 4. 大捕获

`bin/make_bench_capture.py` 生成的十万条级捕获不放在仓库里（约 12 MB），
由 `bin/bench.py` 现场生成、接入、逐行核对并记录资源开销；manifest 由生成器
写出，`bench.py` 用它核对首尾记录。
