# relation-handoff-loader（关系装载工具）

把供应商一次多表交接的 UTF-8 JSONL，按**整表交接语义**原子写进已有 PostgreSQL
仓库。它回答的问题是“这次交接之后目标关系应该是什么”，而不是给记录分好坏、
裁取子库，也不是另建一个离线结果库。

- 自然键（有序、一个或多个非空 `text` 列，原样比较，`"007" ≠ "7"`）在表间互相
  引用；目标库用独立序列分配的 `bigint` 代理主键 `id` 与外键。
- 一次 `load` 对所有参与表的插入、更新、删除、引用重接是**同一笔事务**：读者
  只能看到完整旧关系或完整新关系；任何输入/目录/引用/约束/外部引用错误都整体
  回滚，绝不留下一半新一半旧。
- 重状态全部落在数据库临时表（可溢出磁盘），Python 侧只保留契约与有界批次，
  支持超过 Python 内存的输入规模。

## 目录结构

```
relation-handoff-loader/
├── handoff/                 # 产品包（Python API + 终端命令）
│   ├── contract.py          # 装载契约解析与自洽性校验
│   ├── parser.py            # UTF-8 JSONL 严格流式解析（逐行、有界批次）
│   ├── catalog.py           # 写入前与 pg_catalog 逐项核对
│   ├── staging.py           # 事务内临时表、身份计划、引用解析、差分应用
│   ├── loader.py            # 事务/锁编排（产品主入口）
│   ├── cli.py               # handoff-load 终端命令
│   └── errors.py            # 带逻辑表/文件/物理行/字段定位的诊断
├── demo/
│   ├── sql/                 # 演示 schema 与种子（organization/member/assignment/fact_usage）
│   ├── contract.json        # 演示装载契约
│   ├── data/handoff/        # 主交接数据（成员拆两个文件）
│   ├── data/fail_*/         # 五类真实失败场景的自洽文件集
│   ├── pg_env.py            # 离线 PostgreSQL 演示环境管理（init/start/stop/reset/destroy）
│   └── verify_demo.py       # 独立进程核验实际表、外键、事实连接与幂等
└── tests/                   # 48 个 pytest（含独立朴素预言模型、并发、十万行规模）
```

## 安装

需要 Python ≥ 3.11 与一个可连接的 PostgreSQL（演示在 15 上验证）。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
# 或安装为包，获得 handoff-load 命令
.venv/bin/pip install -e .
```

驱动用 psycopg3。产品代码不依赖任何超级用户权限，演示用非超级用户
`handoff_loader`（`NOSUPERUSER NOREPLICATION NOBYPASSRLS`）执行装载。

## 离线演示环境（不使用宿主已有服务）

演示集群在项目目录下维护独立数据目录、独立端口（默认 55432）与 unix socket，
不监听 TCP，不触碰宿主任已存在的 PostgreSQL。PostgreSQL 二进制从 Debian 的
deb 解包到本地目录即可，无需 root；路径用环境变量 `PG_HOME` 指向。

```bash
# 指向解压后的 PostgreSQL（含 bin/、lib/、share/）
export PG_HOME=/workspace/.pglocal/usr/lib/postgresql/15

python demo/pg_env.py init      # 初始化独立数据目录（仅首次）
python demo/pg_env.py start     # 启动并建专用数据库 handoff_demo_db 与装载角色
python demo/pg_env.py reset     # 建 schema + 种子，回到交接前状态
python demo/pg_env.py status    # 查看 DSN
python demo/pg_env.py psql      # 用演示连接打开 psql
python demo/pg_env.py stop      # 停止
python demo/pg_env.py destroy   # 删除整个本地数据目录（清理演示环境）
```

## 运行装载

### 终端命令

```bash
python demo/pg_env.py reset
DSN=$(python demo/pg_env.py status | grep DSN | cut -d= -f2-)

python -m handoff.cli \
  --dsn "$DSN" \
  --contract demo/contract.json \
  --files organization=demo/data/handoff/organization.jsonl \
  --files member=demo/data/handoff/member-1.jsonl \
  --files member=demo/data/handoff/member-2.jsonl \
  --files assignment=demo/data/handoff/assignment.jsonl \
  --files weird_named=demo/data/handoff/weird.jsonl
```

成功时 stdout 打印逐表 `inserted/updated/deleted/unchanged` 的 JSON，退出码 0；
失败时 stderr 打印带定位的诊断，退出码 1，且数据库整体回滚。

### Python API

```python
import psycopg
from handoff.contract import load_contract
from handoff.loader import load_relation_handoff

contract = load_contract("demo/contract.json")
files = {
    "organization": ["demo/data/handoff/organization.jsonl"],
    "member": ["demo/data/handoff/member-1.jsonl", "demo/data/handoff/member-2.jsonl"],
    "assignment": ["demo/data/handoff/assignment.jsonl"],
    "weird_named": ["demo/data/handoff/weird.jsonl"],
}
with psycopg.connect(dsn) as conn:          # 普通会话角色，非 autocommit
    report = load_relation_handoff(conn, contract, files, batch_size=1000)
print(report.to_dict())
```

## 契约

契约是一份 JSON，声明参与表、稳定逻辑表名、物理 schema/table、有序自然键、
普通值列、具名引用、额外业务唯一约束与每表独立的 `id` 序列。

- 普通列只支持 `text` / `bigint` / `boolean` 及各自的可空性。
- 引用给“目标自然键元组或 null”，落库为 `bigint` 外键指向目标表 `id`，
  可指向同表或其他参与表，允许非空循环；契约自己规定是否可空。
- 自然键与额外业务唯一约束都按有序列元组生效，含 null 按 PostgreSQL 普通
  `UNIQUE` 语义处理（null 不参与冲突）。
- 契约引用只能落到声明的参与表；物理标识符允许空格、引号、点、中文，全部经
  `Identifier` 安全引用；数据值始终走绑定参数，绝不拼成 SQL 片段。

### 输入行格式

每行一个 JSON 对象，且只能含 `key` / `values` / `refs` 三个成员：

```json
{"key": ["T1", "M-NEW-A"],
 "values": {"display_name": "新成员A", "alias": "alias-new-a",
            "score": 9223372036854775807, "vip": false},
 "refs": {"manager": ["T1", "M-NEW-B"], "org": ["ORG", "C"]}}
```

列名与引用名必须与契约齐全对应，禁止缺漏或夹带额外字段。一张表可拆多个文件；
表内相同自然键重复出现即整次拒绝（内容相同也不合并）。空文件允许把表交成空表。

严格拒绝：重复 JSON 成员、空行（含纯空白行）、非法 UTF-8、坏结构、bool 冒充
bigint、浮点冒充整数、超出 bigint 范围、非空引用给 null、引用分量数不符或为空串。

## 整表交接语义与身份

对每张参与表，交接内容就是它完成后的全部行；不参与的表原样保留。

- 自然键仍存在 → **保留原 id**（无论普通字段是否变化、文件在什么位置）；
- 新自然键 → 从独立序列取新 id；
- 目标里有、本次没交来的旧自然键 → 删除。
- 引用按目标自然键在**本次交接的最终集合**里解析 id，绝不连旧表，因此即将
  删除的旧行不可能充当父记录；也不能为破环把非空引用改成 null。
- 同一自然键值在不同逻辑表中完全无关；自然键分量永不做分隔符拼接。
- 现有同自然键行最终所有业务字段与引用都相同时不发 UPDATE，计入 `unchanged`。
- 被未参与表（如事实表 `fact_usage`）外键引用的身份不允许删除：显式预检查给出
  清晰诊断，数据库外键作为最终防线；不级联删事实。

## 事务、锁与序列空洞

- 所有参与表按固定全局顺序（物理 `(schema, table)` 字典序）逐表取事务级
  `SHARE ROW EXCLUSIVE` 表锁，**锁定后才读旧状态**。同库两次装载因此严格串行，
  不可能各读一份旧状态后互相覆盖（见 `tests/test_concurrency.py`）。
- 参与表上的可延迟外键（含出向与入向）与可延迟唯一约束在事务内统一置为延迟，
  在提交点由数据库真正核验；`id` 主键不可延迟，保持即时。产品不重建/替换表、
  不删索引或约束、不 `ALTER` 约束、不关触发器、不用超级用户/复制角色绕过检查。
- 应用阶段全局分三步：先删所有表的废弃身份，再插所有新身份，最后更新保留身份，
  避免跨表暂态外键/唯一冲突；循环引用靠可延迟约束在提交时统一成立。
- 失败整体 `ROLLBACK`。序列已 `nextval` 取走的号码**允许有空洞**，不承诺回滚
  序列值（这是 PostgreSQL 序列的标准行为）。

## 目录核对（写入前强制）

契约必须与真实 `pg_catalog` 匹配后才允许写入，核对项：列名/类型/可空性、
`id` 主键、自然键唯一约束、额外业务唯一约束、外键目标与可延迟性、独立序列。
目标表除 `id` 外只能含契约声明的列。

明确拒绝、绝不假装支持的布局：用户自定义触发器、生成列/标识列、分区/继承、
视图/外部表、额外 CHECK 约束、部分/表达式唯一索引、契约未声明的唯一约束、
非 NO ACTION 的外键（级联删除/置空/改值/RESTRICT）。**入向外键若带级联删除、
置空或改值动作也拒绝该目标布局**，避免未参与表被连带改动。连接只允许操作
指定 schema，不会清空其他 schema，也不会关闭数据库的约束检查。

## 批次与缓存界限

- JSONL 逐行读取，按 `batch_size`（默认 1000）切成有界批次写入临时表；
  Python 任意时刻只保留契约 + 当前批次，不缓存全量表或循环依赖图。
- 行暂存表、引用暂存表、身份/差分计划表都是会话临时表（`ON COMMIT DROP`），
  由 PostgreSQL 管理，必要时溢出到磁盘。
- 重复自然键、唯一约束冲突、引用解析、身份差分、外部引用核查都在数据库内用
  集合 SQL 完成。
- 实测（`tests/test_scale.py`，批次 500）：100,000 行装载约 15 秒，进程峰值
  RSS 增量约 1.4 MB（全表未进入 Python 字典）；具体数值随机器而异，测试会打印
  当次耗时与峰值 RSS。

## 测试

```bash
python -m pytest tests/ -q
```

- `test_parser_contract.py`：严格解析、类型/边界、契约自洽。
- `test_catalog.py`：各类目录不匹配与不支持布局的明确拒绝。
- `test_end_to_end.py`：身份保留、非空循环、别名交换、计数、幂等、顺序无关、
  空表、批次变化、重复自然键回滚。预期由 `tests/naive_oracle.py` 的独立朴素
  小样本关系模型计算，不调用产品的映射/差分代码。
- `test_failures.py`：缺失最终父键、业务唯一冲突、末尾坏行、删除被事实引用的
  成员、目录不匹配；每次失败前后用独立连接逐表快照证明字节级不变，修正后成功。
- `test_concurrency.py`：真实双连接的锁等待、基于已提交数据计算、读者只见完整
  旧关系或完整新关系。
- `test_cli.py`：终端命令与独立进程入口。
- `test_scale.py`：十万行流式、总量/身份/首尾引用、幂等与内存界限。

## 演示场景预期（自然键 → id）

种子显式写入旧 id：member `M1=1001, M2=1002, M3=1003, M4=1004`，
organization `ORG/A=101, ORG/B=102`，assignment `P1/R1=5001, P1/R2=5002`。

交接后：`M1/M2/M4` 与 `ORG/A` 保留原 id；`M3`、`ORG/B`、`P1/R2` 删除；
新成员 `M-NEW-A/M-NEW-B`、新组织 `ORG/C`、新任务 `P2/R9` 取序列新 id（具体值
运行后核验，但全局一致）。新成员 A、B 互为负责人形成非空循环，两人同属新组织
`ORG/C`，两个组织的联系人都指向 A。`fact_usage` 不参与装载，仍通过保留 id
连接到 M2/M4。运行 `python demo/verify_demo.py` 可在独立进程里核验以上全部关系。
