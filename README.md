# LIMS 历史报告补采与人工复核抽检系统

检验机构数据员把门户上的历史报告补录进 LIMS 的补采服务。门户按报告时间检索、
单次最多返回 100 条且不能翻页，本系统通过**时间区间二分**完成补采，支持失败续采、
换批重采和批次核对后的原子发布。质控员再从**已发布**报告里按 UTC 日期与检验结论
抽取人工复核名单，跨日期/跨结论的同一样品由系统联合协调配额。

## 组件

| 组件 | 说明 | 端口 |
| --- | --- | --- |
| `api` | 补采 + 抽检 API（FastAPI + SQLite）：补采、续采、进度、结果查询、复核名单抽取 | 8000 |
| `portal` | 本地模拟门户（FastAPI + SQLite，无需账号）：报告检索 + 故障/换批/造数管理接口 | 8001 |

```
├── docker-compose.yml      # 一键启动 api + portal
├── api/                    # 补采 + 抽检 API
│   └── app/{main,db,collector,sampling,timeutil}.py
├── portal/                 # 本地模拟门户
│   └── app/{main,db,timeutil}.py
└── scripts/
    ├── verify.py           # 补采端到端验证脚本（仅依赖标准库）
    └── verify_sampling.py  # 抽检端到端验证脚本（仅依赖标准库）
```

## 快速开始

```bash
docker compose up --build -d
```

启动后：

- API 文档：<http://localhost:8000/docs>
- 门户地址（API 容器视角）：`http://portal:8000`；宿主机视角：`http://localhost:8001`

## 端到端验证

```bash
# 补采基础场景（约 50 项断言）
python3 scripts/verify.py

# 加测“服务重启后按任务号续采”
RESTART_CMD='docker compose restart api' python3 scripts/verify.py

# 抽检场景：跨组样品协调、配额冲突、随机确定性、重采版本快照
python3 scripts/verify_sampling.py
```

补采脚本覆盖：参数校验、区间二分、[start, end) 语义、查询失败续采、换批暂停重采、
单秒超限上报、旧结果保留、重启续采、进度与结果查询。抽检脚本覆盖：跨日期/跨结论
共享样品的配额协调、同一样品只出一份报告、足额方案与配额冲突（409 不输出部分名单）、
种子可复现、重采发布后批次/任务号版本一致性。

## API 接口

### 创建补采任务（补采入口）

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"portal_url": "http://portal:8000",
       "start": "2024-01-01T00:00:00Z",
       "end":   "2024-01-02T00:00:00Z"}'
```

- 时间精确到秒（UTC），区间**含起点、不含终点**。
- 采集在请求内同步执行，响应即任务最终/当前状态；失败、暂停后可凭 `id` 续采。

### 续采

```bash
curl -X POST http://localhost:8000/tasks/1/resume
```

查询失败、服务重启、换批暂停（`paused`）、单秒超限（`incomplete`）后均可调用；
已完成任务返回 409。

### 进度查询

```bash
curl http://localhost:8000/tasks/1
```

返回状态、轮次、本轮区间统计（pending/done/unfillable）、已采记录数、
无法补齐的区间清单、开始/最近批次、发布信息等。

### 结果查询

```bash
curl 'http://localhost:8000/results?portal_url=http://portal:8000&start=2024-01-01T00:00:00Z&end=2024-01-02T00:00:00Z'
```

返回该门户该区间当前已发布的批次与全部记录；未发布返回 404。

### 人工复核名单抽取（抽检入口）

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"portal_url": "http://portal:8000",
       "start": "2024-01-01T00:00:00Z",
       "end":   "2024-01-03T00:00:00Z",
       "seed": 42,
       "quotas": [
         {"date": "2024-01-01", "qualified": 1, "unqualified": 2},
         {"date": "2024-01-02", "qualified": 1, "unqualified": 1}
       ]}'
```

- 仅从该门户该区间的**已发布**报告抽取，按 UTC 日期 × 检验结论（合格/不合格）
  分组配额；`date` 为 `YYYY-MM-DD`，各配额为非负整数，配额总和须大于 0。
- **同一样品整份名单最多出现一次**。同一样品在不同日期或两类结论下都有报告时，
  系统用带回溯的确定性搜索联合协调各组配额（不能逐组贪心）：
  - 存在足额方案 ⇒ 返回完整名单，每条报告带发布 `batch` 与 `task_id`；
  - 无法同时满足 ⇒ 返回 **409**，逐组给出配额、可用样品数与缺口，**不输出任何
    部分名单**。
- `seed` 决定每组内候选报告的随机顺序：同一发布版本（同候选集）与同一种子必然
  得到同一名单；换种子可改变入选报告。
- 抽取在单个数据库事务（显式 `BEGIN` 的只读快照）内完成候选读取、配额判定与版本
  信息采集；即使抽取期间发生重采发布，名单与批次/任务号仍来自同一发布快照。

### 抽检响应示例（成功）

```json
{
  "portal_url": "http://portal:8000",
  "start": "2024-01-01T00:00:00Z",
  "end": "2024-01-03T00:00:00Z",
  "seed": 42,
  "publication": {"id": 1, "batch": "BATCH-0001", "task_id": 1,
                  "record_count": 7, "published_at": "..."},
  "groups": [
    {"date": "2024-01-01", "result": "合格", "quota": 1, "selected": 1},
    {"date": "2024-01-01", "result": "不合格", "quota": 2, "selected": 2},
    {"date": "2024-01-02", "result": "合格", "quota": 1, "selected": 1},
    {"date": "2024-01-02", "result": "不合格", "quota": 1, "selected": 1}
  ],
  "selected_count": 5,
  "selected_reports": [
    {"report_no": "RPT-000001", "sample_no": "SAM-X1",
     "report_time": "2024-01-01T08:00:00Z", "date": "2024-01-01",
     "result": "合格", "batch": "BATCH-0001", "task_id": 1}
  ]
}
```

### 抽检冲突响应（409，无名单）

```json
{
  "detail": {
    "message": "配额冲突：共需 7 份，跨组去重后候选样品仅 5 个（或存在跨组样品争用），不存在足额方案；未输出任何名单，请调整配额后重试",
    "total_quota": 7,
    "total_available_samples": 5,
    "groups": [
      {"date": "2024-01-01", "result": "合格", "quota": 2,
       "available_samples": 2, "shortfall": 0}
    ]
  }
}
```

## 门户接口（模拟，无需账号）

| 接口 | 说明 |
| --- | --- |
| `GET /reports?start=&end=` | 返回 `{batch, total, items}`，`items` 最多 100 条 |
| `POST /admin/seed` | 造数：`{start,end,count,seed}` 区间内随机；`{time,count}` 同秒超限 |
| `POST /admin/seed-reports` | 按显式 `{reports:[{sample_no,report_time,result}]}` 造数，便于构造跨日期/跨结论的共享样品 |
| `POST /admin/switch-batch` | 换批：`{}` 立即换；`{"after_queries": N}` 第 N 次查询起生效 |
| `POST /admin/fail` | 注入失败：`{"times": N}` 接下来 N 次查询 500；`after_queries` 可延迟触发 |
| `POST /admin/reset` | 清空数据并复位批次/故障注入 |
| `GET /admin/state` | 查看批次、数据量、故障与换批计划 |

## 核心设计

- **半开区间二分**：原始区间 `[start, end)`；门户返回 `total > 100` 时把区间对半分
  继续查询，直到每个子区间 `total ≤ 100` 才落库记录。区间宽度降到 1 秒仍超限时，
  记为**无法补齐的区间**（`unfillable`），在任务详情中上报。
- **进度持久化**：每个子区间的记录与进度在**同一事务**落库，因此查询失败或服务
  重启（含采集中途崩溃）后，可按任务号从断点续采，不重复、不丢失。
- **换批处理**：本轮首次响应登记**开始核对批次**；任一后续响应批次不同即判定换批
  ——清除本轮全部进度并暂停（`paused`），下次续采轮次 +1、从原区间重新采集。
- **批次核对与原子发布**：所有子区间完成后再次查询原区间做**结束核对**；只有
  完整覆盖原区间（无 unfillable）且开始、结束批次一致，才在**单个事务**里删除并
  替换该门户及区间的已发布结果——任何失败都保留旧结果。
- **任务状态机**：`pending → running → completed / failed / paused / incomplete`。
  `failed`（查询失败）、`paused`（换批）、`incomplete`（单秒超限）均可续采；
  重启残留的 `running` 任务也可直接续采。
- **复核名单抽取**：把候选按 `(UTC 日期, 结论)` 分组，组内报告按由 `seed` 与分组
  身份派生的确定性 RNG 洗牌；再以深度优先 + 回溯为各组凑满配额，样品被一组占用后
  其他组只能改选，后续组凑不满时失败跨组回滚。DFS 完备性保证“只要存在足额方案就
  不报不足”，且找到的第一个方案由种子唯一确定。两道预检（单组配额超过本组去重
  样品数、总配额超过跨组去重样品总数）快速拦截硬性冲突。
- **抽取快照隔离**：Python `sqlite3` 默认仅在写语句时隐式开事务，纯 SELECT 会各自
  自动提交、读不到同一快照；因此抽取在读取前显式 `BEGIN` 建立只读事务，使候选报告、
  配额判定、批次与任务号在整个请求内来自同一数据库版本，重采发布不会造成名单与
  版本信息错位。

## 环境变量

| 变量 | 服务 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DB_PATH` | api | `/data/api.db` | SQLite 路径（compose 已挂卷持久化） |
| `DB_PATH` | portal | `/data/portal.db` | 同上 |

## 本地开发（不用 Docker）

```bash
pip install -r api/requirements.txt -r portal/requirements.txt
DB_PATH=/tmp/portal.db uvicorn app.main:app --port 8001 --app-dir portal &
DB_PATH=/tmp/api.db    uvicorn app.main:app --port 8000 --app-dir api &
API_BASE=http://localhost:8000 PORTAL_BASE=http://localhost:8001 \
PORTAL_URL_FOR_API=http://localhost:8001 python3 scripts/verify.py
API_BASE=http://localhost:8000 PORTAL_BASE=http://localhost:8001 \
PORTAL_URL_FOR_API=http://localhost:8001 python3 scripts/verify_sampling.py
```
