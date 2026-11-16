# contract-gate（离线来件契约分流）

供应商用**组合式 JSON 契约**描述同一份来件：公共字段放在被引用的定义里，
不同业务分支各自认领字段，最后才禁止剩余字段。本工具是这个契约的**离线
数据质量入口**：操作员给出本地契约资源清单、根契约 URI 和若干 UTF-8
JSONL，得到原样保留的合格行、带精确定位的拒收行，以及能解释组合判定的
证据（错误树 + 覆盖集合）。

它不登记契约版本、不修改业务数据库、不替供应商补默认值或转换类型；
核心求值与覆盖信息传播由本项目自实现，**不把最终判定交给现成
JSON Schema 验证器**，也不通过网络取契约。仅依赖 Python 3.11 标准库。
仓库中其他既有工具（relation-handoff-loader、ipfix-ledger 等）不受影响。

## 1. 快速开始

```bash
cd contract-gate

# 一条命令完成样例分流及断言（74 项手工写出的预期）
python3 -I bin/demo.py

# 自己跑分流
python3 -I bin/cgate.py --manifest examples/manifest.json \
    --root https://acme.example/schemas/root.json \
    --out examples/out examples/data/orders.jsonl
# 或在仓库根目录：python3 -m contract_gate --manifest ... --root ... 输入...

# 全部单元测试（138 个，含与 jsonschema 库的可选交叉对照）
python3 -I bin/run_tests.py
```

产物固定三个文件，写在 `--out` 目录：

- `accepted.jsonl`：合格行的 **JSON 正文原始字节**，按输入顺序，仅去掉
  输入行尾 CRLF/LF 并追加一个 LF 分隔；不重排、不去重、不重新序列化。
- `rejected.jsonl`：每行一个拒收记录（见 §6），不含业务载荷本身。
- `summary.json`：各文件总行数、合格/拒收数与合计。

### 三个退出结果

| 退出码 | 含义 |
|---|---|
| 0 | 分流完成，全部合格 |
| 1 | 分流完成，存在拒收 |
| 2 | 契约无法执行或分流未能完成（预检失败、引用环、输入/输出不可读） |

**预检失败（退出码 2）不产出 accepted/rejected/summary 正式文件**；
来件中的坏行只影响该行本身，后续行照常分流。

### Python API

```python
from contract_gate import Contract, ContractError

contract = Contract.from_manifest("examples/manifest.json",
                                  "https://acme.example/schemas/root.json")
r = contract.validate_line(b'{"id":"A","kind":"order"}')
r.ok         # 是否合格
r.category   # ok / contract / json / utf8 / duplicate / empty
r.issues     # contract 类别的错误树（Issue 列表，见 §6）
r.coverage   # 合格时根对象/数组的覆盖集合 {"properties": [...], "items": [...]}
r.parse      # 解析类错误的类别与字节位置
```

## 2. 契约子集（JSON Schema 2020-12 的明确子集）

**不宣称完整兼容**。支持且仅支持：

- 布尔契约（`true` / `false`）；
- 结构：`$id`、`$defs`、`$ref`；
- 断言：`type`、`const`、`enum`；
- 对象：`required`、`properties`、`additionalProperties`、
  `dependentSchemas`、`unevaluatedProperties`；
- 数组：`prefixItems`、`items`、`contains`、`minContains`、`maxContains`、
  `unevaluatedItems`；
- 组合：`allOf`、`anyOf`、`oneOf`、`not`、`if`/`then`/`else`；
- 注解：`title`、`description`、`$comment`（只作说明，不参与判定）；
  另容忍 `$schema`（同样不参与判定）。

规则：

- `type` 支持 `null/boolean/object/array/string/number/integer` 及其
  **非空、不重复**类型数组；`true` 不算数；数学整数形式的 JSON 数值
  （如 `1.0`、`1e2`）属于 `integer`。
- `const`/`enum` 按 JSON 结构比较：对象不看成员顺序，数组看顺序；数值按
  **精确十进制数学值**比较（`1` 等于 `1.0`）；`true` 与 `1` 不相等。
- `minContains`/`maxContains` 只能与 `contains` 一起出现，且必须是非负整数；
  `contains` 默认至少命中 1 个，`minContains` 可为 0。
- **除此以外的约束关键字明确拒绝**（如 `minimum`、`pattern`、`format`、
  `definitions`）；任何关键字形状不合法（`type` 未知、`required` 重复、
  `enum` 为空、`prefixItems` 为空、子契约位置出现非布尔非对象等）都在
  **处理来件之前**报契约错误，绝不默默忽略。

## 3. 组合覆盖传播（本项目的核心语义）

判定的关键不是「哪些字段名出现在契约里」，而是「**哪些属性/数组位置已经
被成功的规则检查过**」。每个契约对象在同一实例位置上累积覆盖：

- `properties` 认领**实际存在且子契约判定成功**的同名属性；
  `additionalProperties` 只针对**本层** `properties` 没列出的名字——不能
  把其他 `allOf` 分支的 `properties` 偷借来当本层声明（要跨分支合并请用
  `unevaluatedProperties`）。
- `$ref`（**兄弟关键字同样生效**）、`allOf`、`dependentSchemas`（触发属性
  存在时对整个当前对象求值）、实际选中的 `then`/`else`：成功时把覆盖向外
  合并；失败时贡献为零。
- `anyOf` 合并**全部**成功分支，失败分支不贡献；`oneOf` **恰好一个**成功
  才可贡献；`not` 永不向外贡献。
- `if` 条件成功时按标准保留该条件产生的覆盖，条件失败时不保留；只执行并
  收集**实际选中**的 `then` 或 `else` 的贡献。`if` 缺席时 `then`/`else`
  都忽略；`if` 存在但没有对应分支时，不凭空增加该分支的覆盖。
- 同一实例位置上的全部有效贡献完成后，`unevaluatedProperties` /
  `unevaluatedItems` 才检查尚未覆盖的部分——与 JSON 成员顺序、契约关键字
  书写顺序**无关**（见 `examples/contracts/root.shuffled.json` 对照实验）。
- 某规则整体失败时，它的覆盖**不外泄**；失败证据（分支身份、实际违反的
  关键字）保留在错误树里，成功分支不会被输出成错误。

数组保留**实际位置**而非「已检查长度」：`prefixItems` 按位置检查前缀，
`items` 只处理前缀之后的尾部；`contains` 分别测试每个元素，成功时只认领
**命中的位置**（未匹配元素不因整条 `contains` 成功而被认领）；
`unevaluatedItems` 在组合后的覆盖上检查剩余位置，可以是布尔或子契约。

空对象、空数组、缺少属性、显式 `null` 严格区分；对象关键字对非对象、
数组关键字对非数组不施加约束，类型是否正确由 `type` 决定。

## 4. 离线资源组织

资源清单（manifest）把**绝对 URI** 绑定到本地文件，`https` URI 只作离线
标识，**不发起网络请求**；文件路径只负责取字节，不构成资源身份：

```json
{"resources": {"https://acme.example/schemas/root.json": "contracts/root.json"}}
```

- 清单相对路径基于清单所在目录解析；清单 URI 必须是绝对 URI、不带片段；
  清单 JSON 重复成员会被拒绝。
- 契约里的**嵌套 `$id` 创建新的解析基址和资源根**（见
  `examples/contracts/root.json` 里的 `node.json`）；同一绝对资源 URI 被
  两处不同定义占用 → 预检拒绝。
- `$ref` 按所在位置的基址解析，支持相对 URI、空片段（`""`、`"#"`）和
  JSON Pointer 片段；片段先严格百分号解码（必须落成合法 UTF-8），再按
  `~1→/`、`~0→~` 的顺序解波浪号。悬空引用、坏 Pointer、不支持的片段
  （如裸名字片段）都在预检时拒绝。
- 支持沿对象属性或数组元素**下降**的递归定义（如子节点列表
  `children.items.$ref`）；同一契约位置和同一实例位置**未经下降**又绕回
  自身的引用环在预检时静态拒绝（另设求值期保险），绝不靠递归溢出结束。
- 所有输入资源只读。

## 5. 来件处理与定位

- 文件**逐行**处理，整批记录不留在内存；每条记录单独完整解析。
- 重复成员、非法 UTF-8、坏 JSON、空物理行：拒收该行并继续后续行。
- 每个拒收原因定位到：实际契约资源 URI、契约 JSON Pointer、实例
  JSON Pointer，以及原行 UTF-8 字节的**左闭右开**范围（相对于去掉行尾
  CRLF/LF 后的行正文）。
- 缺少必需字段等没有对应值的错误定位到**所属容器**的范围。
- JSON 语法、重复成员、编码错误尚未进入契约求值：契约位置明确记为
  `null`，给出解析类别（`json`/`duplicate`/`utf8`/`empty`）和可确定的
  原始错误字节位置，不伪造契约来源。
- 转义键、中文等多字节键、同名字段在不同层出现时，范围都能切回正确原文
  （见 `examples/data/mixed.jsonl` 与 `bin/demo.py` 的逐项切回断言）；
  数值解析、相等判断、位置记录全程不经过浮点（`decimal.Decimal`）。
- 重复成员诊断同时给出**两处**成员名的字节范围。

## 6. rejected.jsonl 记录格式

```json
{
  "file": "examples/data/orders.jsonl",
  "line": 2,
  "category": "contract",
  "resource": "https://acme.example/schemas/root.json",
  "pointer": "/properties",
  "error": {"issues": [
    {"keyword": "properties", "resource": "…", "pointer": "/properties",
     "instance": "", "span": [1, 9], "message": "字段 'id' 判定失败",
     "branches": [{"name": "properties['id']", "valid": false,
                   "resource": "…", "pointer": "/properties/id",
                   "issues": [{"keyword": "enum", "instance": "/id",
                               "span": [6, 9], …}]}]}
  ]}
}
```

- 顶层 `resource`/`pointer` 为首个 issue 的契约位置；解析类错误
  （`category` 为 `json`/`utf8`/`duplicate`/`empty`）两者均为 `null`，
  `error` 内是 `{message, span}`（重复成员另有 `key` 与 `spans` 双位置）。
- 错误树里 `branches` 只装**失败**分支（`valid: false`），带分支身份
  （`anyOf[1]`、`then`、`properties['x']`、`$ref` 等）与各自的违反原因；
  `oneOf` 多支成功时违反信息写在 `message` 里，成功分支不进错误树。
- 诊断只含指针、范围与短消息，**不复制整份业务载荷**。

## 7. 目录结构

```
contract-gate/
├── contract_gate/          # 产品包（Python API）
│   ├── jdoc.py             # 逐字节 JSON 解析（范围/重复成员/Decimal）
│   ├── pointers.py         # JSON Pointer 与 URI 片段
│   ├── contract.py         # 清单、嵌套 $id、$ref 解析、形状与环预检
│   ├── evaluate.py         # 组合求值与覆盖传播（自实现）
│   ├── api.py              # Contract / LineResult 单条记录 API
│   └── cli.py              # 分流命令与三个退出码
├── bin/
│   ├── cgate.py            # 终端入口
│   ├── demo.py             # 一条命令完成样例分流及断言
│   └── run_tests.py        # 全部 unittest
├── examples/
│   ├── manifest.json       # 资源清单（https URI → 本地契约）
│   ├── contracts/          # 主契约、打乱顺序对照、anyOf/oneOf/contains、
│   │                       # 分支纠缠、多字节混合等九份契约
│   ├── data/               # 业务来件（含多字节/转义键/重复键/大数/坏行）
│   ├── bad/                # 预检失败样例（未知关键字 + 悬空引用）
│   └── out/                # demo 生成的分流产物（可删）
└── tests/                  # 138 个 unittest
```

## 8. 复跑与自检

```bash
python3 -I bin/demo.py         # 样例分流 + 74 项断言，退出码 0 为全过
python3 -I bin/run_tests.py    # 138 个 unittest（jsonschema 缺失时跳过交叉对照）
python3 -I bin/run_tests.py -v # 逐项输出
```

`tests/test_crosscheck.py` 在安装了 `jsonschema` 库的环境中会额外把支持
子集上的一批用例与参考实现做真假对照；本项目的核心求值与来源定位**不
依赖**该库，库缺失时该组测试自动跳过。

注意：求值器对实例做结构递归，CLI 已在大栈线程中放开递归余量
（2 万层嵌套实测通过）；API 调用方如遇极端深嵌套，请自行调整
`sys.setrecursionlimit` 并在足够大的线程栈中运行。
