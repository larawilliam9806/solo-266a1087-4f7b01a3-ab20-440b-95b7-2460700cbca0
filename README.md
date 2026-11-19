# 机场到达日报 API

按**计划起飞日**合并航班计划与跨午夜的落地报文，生成每日到达事实与待核结论；
按**后序实际起飞日**汇总同一机号从落地到下一段起飞的过站耗时。
FastAPI + SQLite（WAL）实现，支持 `docker compose` 一键启动。

## 1. 业务规则

### 文件与版号

- 两类文件均**按日整批上传、整体替换**，每个 `(文件类型, 文件日期)` 只保留所见的最新版号。
- 版号为正整数且必须**严格递增**：上传版号 ≤ 已存版号返回 `409`，旧数据与结论原样保留。
- 计划 CSV 列：`航班号, 目的地, 计划到达UTC分钟, 正常/取消`（兼容英文列名，状态支持 `normal/正常`、`cancelled/取消`）。
- 落地 CSV 列：`计划起飞日, 航班号, 实际机场, 实际到达UTC分钟`（兼容英文列名）。
- 落地日只允许是**计划日当天或次日**；实际到达分钟 `>= 1440` 表示跨午夜（次日）落地。
- **文件日期必须与到达时刻一致**：按分钟推算出的落地日若与上传 URL 中的日期不符，返回 `422`。
- 仅含表头的文件合法，表示该日清单为空（整批清空）。

### 事实判定（计划日 D 的结论依赖三份清单：D 计划、D 落地、D+1 落地）

| 情形 | 结论 `fact_type` | 说明 |
| --- | --- | --- |
| 取消且未落地 | `cancelled` | |
| 取消却落地 | `review` | 原因 `CANCELLED_BUT_LANDED` |
| 正常却缺落地 | `review` | 原因 `NORMAL_MISSING_LANDING` |
| 有落地但计划清单无此航班 | `review` | 原因 `ORPHAN_LANDING` |
| 同航班多次落地 | `review` | 原因 `MULTIPLE_LANDINGS`（取消航班同时带 `CANCELLED_BUT_LANDED`） |
| 正常、同目的地、单次落地 | `arrived` | 延误 = `max(0, 实际到达分钟 − 计划到达分钟)`，提前到达记 0 |
| 正常但实际机场 ≠ 计划目的地 | `diverted` | 记实际机场，**不计算**原目的地延误，`delay_min=null` |

跨午夜延误天然成立：次日落地分钟（如 `1500`）直接减当日计划分钟（如 `780`）= 720 分钟。

**缺清单（三份缺一）时不产出任何结论**，绝不判定取消或正常到达；查询返回 `status=incomplete` 与缺失项。

### 修订联动与一致性

- 落地日 L 的文件可能同时包含计划日 L 与 L−1 的航班；上传/修订后，**L−1 与 L 两个计划日的结论在同一事务内一起重算**。
- 每次上传在单个 `BEGIN IMMEDIATE` 事务内完成「替换源数据 → 重算受影响结论」；任何步骤失败整体回滚，**失败保留旧数据及旧结论，不存在混版**。
- 每条事实都记录生成它所用的三个源版号（`plan / landing_d0 / landing_d1`）。
- 进程内写锁串行化所有上传；SQLite WAL + busy_timeout 兜底。启动时执行幂等自愈重算，保证重启后不留混版结论。

## 2. 过站（turnaround）

### 衔接清单

按**后序实际起飞 UTC 日**（下称文件日）整批上传、整体替换，每个文件日只保留所见最新版号（版号规则同前）。

衔接 CSV 列：

| 列 | 含义 |
| --- | --- |
| `机号` | 同一架飞机。可给一处，也可分别给 `前序机号` / `后序机号` 两处，两处必须一致 |
| `前序计划起飞日` 、`前序航班号` | 前一段（落地）航班 |
| `后序计划起飞日` 、`后序航班号` | 后一段（起飞）航班，须与前序为两段不同航班 |
| `后序起飞机场` | 后一段起飞机场 |
| `后序实际起飞UTC分钟` | 自**文件日 UTC 零点**起算，`[0, 1439]` |

- 两段**计划起飞日**都只能是文件日或前一天。
- **同一航班至多作一次前序、一次后序**；该航班出现的前后两处机号必须一致。冲突检查覆盖同一文件内与跨文件（与其它日期的衔接清单），**任何冲突整批拒绝（409），不写入任何数据**。
- 允许仅含表头的文件，表示该起飞日没有衔接（整批清空）。

### 过站判定（每个后序航班一条结果）

| 情形 | 结果 |
| --- | --- |
| 前序日报已判定 `arrived` 或 `diverted`、实际落地机场 = 后序起飞机场、落地不晚于起飞 | `status=confirmed`，`turnaround_min = 后序实际起飞 − 前序实际落地` |
| 前序日报缺清单 | `status=review`，原因 `PREV_REPORT_INCOMPLETE`，另在 `missing_sources` 单独标记缺失项 |
| 前序日报未判定为到达或备降（取消、待核、缺落地等） | `status=review`，原因 `PREV_NOT_ARRIVED` |
| 实际落地机场 ≠ 后序起飞机场 | `status=review`，原因 `AIRPORT_MISMATCH` |
| 前序落地时刻晚于后序起飞时刻 | `status=review`，原因 `LANDING_AFTER_DEPARTURE` |

其余衔接一律**保留**并标明待核原因，不会丢弃。过站分钟跨午夜天然成立：前序落地分钟相对前序计划日零点，先换算到文件日零点再与起飞分钟相减。

### 预入库与一致性

- 过站结果在**写入时预计算并入库**（`turnaround_facts`），查询按后序实际起飞日只读一日结果。
- 衔接清单替换、前序计划修订、前序落地报文修订（含跨午夜落地日的修订）都会在**同一事务**内重算受影响的起飞日；受影响结果与源数据一起生效。
- 查询在只读事务快照内完成：更新期间只看到**完整的旧结果**或**完整的新结果**。
- 写入失败整体回滚，**旧数据与旧结果原样保留**；重启自愈按已生效的源数据重算，重启后仍按已生效数据供数。
- 每条过站结果记录所用源版号（`prev_plan / prev_landing_d0 / prev_landing_d1 / link`）。

## 3. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/plans/{plan_date}` | 上传/替换计划 CSV（form：`version`、`file`） |
| POST | `/api/landings/{landing_date}` | 上传/替换落地 CSV（form：`version`、`file`） |
| POST | `/api/turnarounds/{dep_date}` | 上传/替换衔接 CSV（form：`version`、`file`），`dep_date` 为后序实际起飞 UTC 日 |
| GET | `/api/reports/{plan_date}` | 查询到达事实、待核原因与所用源版号 |
| GET | `/api/turnarounds/{dep_date}` | 查询过站结果、待核原因、缺清单与所用源版号 |
| GET | `/api/sources` | 已接收文件的日期与版号 |
| GET | `/health` | 健康检查 |

日期均为 `YYYY-MM-DD`。错误码：`400` 日期/版号格式错、`409` 版号冲突或衔接冲突、`422` CSV 内容不合法、`413` 文件超 10MB。

### 示例

```bash
curl -F "version=1" -F "file=@samples/plan_2026-11-18_v1.csv" \
  http://localhost:8000/api/plans/2026-11-18
curl -F "version=1" -F "file=@samples/landing_2026-11-18_v1.csv" \
  http://localhost:8000/api/landings/2026-11-18
curl -F "version=1" -F "file=@samples/landing_2026-11-19_v1.csv" \
  http://localhost:8000/api/landings/2026-11-19
curl -F "version=1" -F "file=@samples/link_2026-11-19_v1.csv" \
  http://localhost:8000/api/turnarounds/2026-11-19
curl http://localhost:8000/api/reports/2026-11-18
curl http://localhost:8000/api/turnarounds/2026-11-19
```

待核原因同时返回机器码 `review_reasons` 与中文 `review_text`：

到达日报：

- `CANCELLED_BUT_LANDED` 取消却落地
- `NORMAL_MISSING_LANDING` 正常却缺落地
- `ORPHAN_LANDING` 落地清单中存在但计划清单无对应航班
- `MULTIPLE_LANDINGS` 同航班多次落地

过站：

- `PREV_REPORT_INCOMPLETE` 前序日报缺清单（缺失项见 `missing_sources`）
- `PREV_NOT_ARRIVED` 前序日报未判定到达或备降
- `AIRPORT_MISMATCH` 前序实际落地机场与后序起飞机场不一致
- `LANDING_AFTER_DEPARTURE` 前序落地时刻晚于后序起飞时刻

## 4. 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# 数据库路径默认 data/airport.db，可用环境变量 AIRPORT_DB_PATH 覆盖
```

## 5. Docker Compose

```bash
docker compose up --build
# 服务监听 http://localhost:8000，SQLite 数据持久化在 airport_data 卷（/data/airport.db）
docker compose down        # 停止
docker compose down -v     # 连同数据卷一起删除
```

## 6. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖：完整判定矩阵、缺清单不出结论、跨午夜延误、备降、四类待核、
版号严格递增与 409、文件日期/落地日校验、相邻两日联动修订、
并发同版号竞争、事务失败回滚保留旧结论、重启自愈无混版；
过站确认/待核全矩阵、缺清单单独标记、跨午夜过站、衔接文件内与跨文件冲突整批拒绝、
计划与落地修订联动重算过站、失败回滚保留旧结果、重启自愈过站结果。

## 7. 目录结构

```
app/
  config.py       配置（DB 路径、限额）
  db.py           SQLite 连接、建表、事务与持久化语句
  csv_parser.py   CSV 解析与字段校验（中英文列名）
  services.py     版号替换、到达事实与过站结果重算、查询、启动自愈（写锁）
  schemas.py      Pydantic 响应模型
  main.py         FastAPI 路由与异常处理
samples/          可直接 curl 上传的样例 CSV
tests/            pytest 端到端测试
```
