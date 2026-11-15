# 多业务系统代码表映射服务

维护多个源业务系统（CRM / ERP / POS）的代码表到统一分析口径之间的版本化映射，
支持一对一、多源码合并、暂时无对应项，记录业务原因与批准人；调用方按**事件业务
时间**和**指定目标版本**查询，所有查询结果都注明所采用的映射版本。

技术栈：Python 3.11 + psycopg 3 + PostgreSQL 18（本机以 conda 免 root 运行）。

## 1. 领域模型与表结构

| 表 | 作用 |
| --- | --- |
| `source_system` | 源系统登记（CRM/ERP/POS） |
| `source_code` | 各源系统自有代码表，SCD2 半开区间 `[effective_from, effective_to)` |
| `target_dimension` / `target_version` / `target_code` | 目标分析口径及其发布版本、版本内代码清单 |
| `mapping_version` | 映射自身的版本，`draft → published`，同样带生效区间 |
| `mapping_row` | 映射明细：源码、目标码（可空）、血缘分组、组类型、区间、业务原因、批准人 |
| `report_snapshot` | 已封存报表及其引用的映射版本 |

关键约定：

- 所有时间区间都是**半开区间 `[from, to)`**，`to = NULL` 表示至今。交界当天
  归新区间所有（测试 `TestIntervalBoundaries` 验证）。
- `mapping_row.group_type`：`ONE_TO_ONE` / `MERGE` / `SPLIT` / `UNMAPPED`。
  合并用稳定的 `mapping_group_id` 跨版本保留血缘；`UNMAPPED` 行 `target_code`
  为空，但**必须**填写业务原因（CHECK 约束强制）。
- 映射行同样可以有自己的生效区间，因此一个映射版本内能表达源码换码衔接
  （如 CRM 在 m2025 中途 A/F/C → ACT/FRO/CLS）。

## 2. 发布前校验（SQL 函数 `validate_mapping_publish`）

发布在单事务内执行，以下冲突**一次性全部返回**，任一存在即拒绝发布：

1. `OVERLAPPING_MAPPING` —— 同一源系统同一源码在同一时段存在多条有效映射；
2. `INVALID_TARGET_CODE` —— 引用了目标版本代码清单中不存在的目标码；
3. `INCOMPLETE_MERGE_SPLIT` —— 与上一已发布版本相比，同一血缘分组的源码集合
   发生变化（合并/拆分流程不完整，须显式走全量成员迁移）；
4. `MISSING_MAPPING` —— 版本生效时点仍有效的源码没有任何映射行覆盖
   （无对应项也必须以 `UNMAPPED` 显式登记，不允许“沉默缺口”）。

发布事务使用 `pg_advisory_xact_lock` 串行化，避免并发发布互相绕过校验。
校验失败时发布事务回滚，草稿保留为 `draft` 便于修正后重试。

## 3. 封存不可改写

`report_snapshot` 记录报表封存时使用的映射版本；行级触发器
`trg_sealed_mapping_immutable` 禁止 UPDATE/DELETE 任何被封存版本的映射行，
数据库直接抛 `check_violation`。新版本映射照常发布，历史报表答案因此可复现。

## 4. 查询 API（`service.MappingService`）

| 方法 | 说明 |
| --- | --- |
| `forward_convert(system, domain, source_code, biz_date, target_version_label=None)` | 正向转换；返回目标码、组类型、业务原因、批准人及**采用的映射/目标版本**；无映射行返回 `NO_MAPPING`，显式无对应项返回 `UNMAPPED`，并附带源码在事件时点是否有效 |
| `reverse_lookup(target_code, biz_date, target_version_label=None)` | 反向候选；合并口径会返回多个来源候选，同样注明映射版本 |
| `coverage(mapping_version_label)` | 覆盖率核验：生效源码总数、已映射、显式无对应项、缺失明细与覆盖率 |
| `create_draft / add_mapping_row / publish` | 草稿起草与发布 |
| `seal_report(report, period, mapping_version_label)` | 报表封存 |
| `validate_only(mapping_version_id)` | 只校验不发布 |

## 5. 三套示例代码表（`sql/02_seed.sql`）

- **CRM**：2025-07-01 整体换码 A/F/C → ACT/FRO/CLS；
- **ERP**：E10 在用 / E20 停用 / E30 挂起（2026 停用）/ E40 观察（2026 新增）；
- **POS**：数字码 1/0/2，2026-01-01 换字母码 ACTIVE/INACTIVE，`2 睡眠` 全周期有效。

目标口径三版本：v2024（3 码）→ v2025（新增 DORMANT）→ v2026（新增 PENDING）。
映射三版本 m2024/m2025/m2026 覆盖：g1 合并组（CRM:A + POS:1 → ACTIVE）、
两个历史 `UNMAPPED`（POS:2、ERP:E30）在 v2025 转正为 DORMANT、换码衔接与
ERP:E40 → PENDING。

## 6. 运行方式

```bash
# 一次性环境（本机已装好；venv 在项目内，PostgreSQL 在 ~/miniforge3 的 pg 环境）
bin/start_db.sh                         # 启动 PG（socket /tmp，端口 55432，用户 app）
.venv/bin/python db.py                  # 建表 + 种子（幂等，会重建 public schema）
.venv/bin/python demo.py                # 端到端演示，打印各场景真实 JSON 输出
.venv/bin/python -m pytest tests/ -v    # 20 个测试，覆盖六类要求场景
bin/stop_db.sh                          # 停止
```

连接串默认 `host=/tmp port=55432 user=app dbname=postgres`，可用环境变量
`MAPPING_DSN` 覆盖。

## 7. 验证场景对照

| 要求场景 | 测试 |
| --- | --- |
| 区间交界（半开区间、换码当天归属、映射版本边界） | `TestIntervalBoundaries`（4 个） |
| 映射合并 / 拆分（合并正向折叠、拆分不完整拦截、干净草稿可发布） | `TestMergeAndSplit`（5 个） |
| 无对应项（原因/批准人、后续版本转正且血缘可追溯） | `TestUnmapped`（2 个） |
| 版本封存（封存行 UPDATE/DELETE 被触发器拒绝、历史答案可复现） | `TestSealedVersion`（2 个） |
| 冲突发布（重叠、目标码无效、缺失、拆分不完整，一次返回全部） | `TestMergeAndSplit` 各拒绝用例 + `demo.py` |
| 反向多候选（合并组多来源、口径演进前后候选变化） | `TestReverseLookup`（3 个） |
| 覆盖率核验（显式无对应项计入覆盖、缺失拉低覆盖率） | `TestCoverage`（3 个） |

## 8. 目录结构

```
code-mapping-service/
├── sql/01_schema.sql          # 表结构、发布校验函数、封存触发器
├── sql/02_seed.sql            # 三套源代码表 + 目标三版本 + 映射三版本
├── service.py                 # MappingService（发布/封存/正向/反向/覆盖率）
├── db.py                      # 数据库初始化
├── demo.py                    # 可运行演示
├── tests/test_mapping_service.py
├── bin/start_db.sh / stop_db.sh
└── requirements.txt
```
