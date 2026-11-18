# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 可追溯字段清单

元数据服务与湖仓血缘共用同一份「可追溯字段清单」，规则落在
`backend/app/services/field_registry.py`（字段版本仓库）、
`backend/app/services/field_audit.py`（字段审计台账）和
`backend/app/services/lineage.py`（血缘引用）。

- **一个当前版本**：每张元数据表任意时刻只有一个 `当前` 字段版本；刷新会生成新版本，
  旧版本降为 `历史` 并保留，不会删除或覆盖。
- **刷新不丢依据**：刷新结果与当前版本一致时不生成重复版本；并发刷新只在
  `store.lock` 内切换版本，同一幂等键复用同一版本，携带过期基线会被拒绝并提示
  基于当前版本重试。
- **注销保留清单**：注销把当前版本标记为已注销并保留为「最后有效字段清单」，
  注销后禁止刷新和切换版本，血缘仍可回查注销前任意历史版本。
- **血缘引用两步走**：先在字段审计台账声明「可追溯字段清单」兼容目标，声明成功
  后才读取字段清单，并把字段版本、字段快照、字段指纹与注销状态固化到血缘记录；
  引用失败只登记未完成项，血缘继续使用上一稳定口径，`重试引用` 只重试未完成项。
- **台账对齐**：元数据侧在登记、注册、刷新、注销、切换版本后写台账；血缘侧在
  声明并引用后写台账。`/api/metadata/audit/consistency/{id}` 用字段指纹比对两侧结果。

示例数据中的 `lake.orders` 有两个字段版本：当前版本 `META-20261115-04`，
历史版本 `META-20261115-03`（`order_id`、`dt`、`amount`、`customer_id`）继续供
血缘记录引用。

### 相关接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/metadata` | 列表，返回当前字段版本与字段清单摘要 |
| `GET /api/metadata/{id}?version=` | 明细，可按字段版本回查历史清单 |
| `GET /api/metadata/{id}/versions` | 字段版本清单（当前 + 历史） |
| `GET /api/metadata/{id}/audit` | 单表字段审计台账与一致性结论 |
| `GET /api/metadata/audit?entry_id=&version=&source=&result=` | 字段审计台账查询 |
| `GET /api/metadata/audit/consistency/{id}?version=` | 元数据与血缘口径比对 |
| `POST /api/metadata/{id}/actions` | `注册表`、`刷新元数据`、`注销表`、`切换字段版本` |
| `GET /api/lineage/{id}?version=` | 血缘明细，可回查引用的字段版本与注销前最后有效清单 |
| `POST /api/lineage/{id}/actions` | `关联血缘`、`标记过期`、`清理记录`、`重试引用` |

动作接口的请求体统一为 `{ "values": { "action": "...", ... } }`；刷新可带
`字段列表`（或 `字段清单`）、`基线版本`、`request_id` 以保证并发幂等。

### 验证

```bash
cd backend
python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数仓与数据湖
