# 榆梯检点·证书哈希目录

复核工位用证书编号检出归档记录，追查桶分裂及缺失、到期、撤销的证书。

## 业务流程

1. 检证接班
2. 资质卷宗
3. 目录重放
4. 检证与桶证据

Python 依据哈希低位寻址，按局部深度拆桶，必要时倍增目录。

榆梯维保复核组收维保档案员交来的证书卷宗，按点检计划复核编号、失效及撤销状态，再交给维保调度。旧表反复全表找号，导入量上升时桶深不可见，退件与过期无法在同一交接表解释。

08:30 收 YT-1124-东梯检的 9 条证书，08:35 核查编号，08:40 保存 6 条点查项，08:45 设单桶 2 件，08:46 后台插入并重放目录，08:50 核对缺失 1、到期 2、撤销 1，08:55 导出回执和桶证据。

以 SHA-256 前 64 位的低位索引目录，桶局部深度等于全局深度时先扩容目录，再按新增区分位迁移槽位和记录。深度上限为 12，封顶桶允许溢出且显式报告。点查显式返回有效、到期、撤销与未登记，失效日零点起即过期。索引为可重复的 Python 存储实验，PostgreSQL 保存输入、任务及结果。本实验未模拟磁盘页、并发写入与删除后缩容。

## 启动

```sh
cp -n .env.example .env
docker compose config --quiet
docker compose up -d --build --wait
```

打开 <http://localhost:18254>。OpenAPI 在 <http://localhost:19254/docs>，数据库映射到本机 17754。所有暴露端口只绑定回环地址。Compose 启动 frontend、backend、worker、db、redis，各自通过健康探针检查。数据库、Redis 和共享产物目录各有命名卷；任务状态及导出内容以数据库中的证据为准。

首次启动时幂等载入演示数据。手动载入可运行 `docker compose exec -T backend python -m scripts.seed_demo`。常规停止可用 `docker compose down`。验收环境清理才加 `-v`，它会删除本项目的数据卷。

## 角色与交接

演示账号共用口令 `Demo-DbWorkbench-2026`。

| 登录名 | 角色 | 可执行操作 |
|---|---|---|
| operator | 资料操作员 | 登记业务资料、设置「目标单桶容量」、提交、查看和下载结果 |
| reviewer | 业务复核员 | 查看执行证据、签署单次复核、下载交接单 |
| viewer | 只读观察员 | 查看资料、执行结果及审计，下载证据 |

选用「YT-1124-东梯检」对应的演示资料，保存一份复演资料，再以默认参数 2 提交。worker 保存结果摘要、业务明细和 SHA-256。复核员登录后在「检证与桶证据」签署。

## 桶证据完整性与修复恢复

已签批次的桶证据（桶记录 + 检证结论）有时会缺行。批次完成时 worker 会冻结一份**同批留存件**（`evidence_snapshots`），并以结果规范化 JSON 的 SHA-256 作为**原摘要**（`runs.digest`）。

- **读取即核对**：打开检证页或导出时，按原摘要实时核对当前桶证据。对不上就把该批标为「待修·暂停下载」，只停当前批次，其他批次照常下载。导出接口在核验未过时返回 409。
- **两路恢复**：
  - **同批留存件**：取回冻结的完整证据，同步比对原摘要，桶记录和结论都验过才恢复，通过即重新开放下载。
  - **原始卷宗重放**：按原卷宗与原参数由 worker 异步重放并按原摘要核验，通过后开放。重放有模拟耗时，便于演示两路并发。
- **并发口径**：两路同时发起时，留存件先核验通过即可恢复下载，即使原始卷宗还在重放；后完成的重放照常登记，但不回滚已开放的下载。
- **幂等与留痕**：同一批次同一来源的修复只登记一次（`(run_id, source)` 唯一约束），后到的修复件和重试都不重复登记。恢复只更新证据状态与证据内容，**原签核和原摘要照旧**。修复登记全程写审计。

证据状态独立于执行状态：`intact`（证据完整）/ `repairing`（待修，暂停下载）/ `repaired`（已恢复）。修复登记状态：`replaying` / `verified` / `rejected`。

| 接口 | 方法 | 角色 | 说明 |
|---|---|---|---|
| `/runs/{id}/evidence` | GET | 登录用户 | 证据状态、是否可下载、修复登记 |
| `/runs/{id}/evidence/snapshot` | GET | 登录用户 | 取回同批留存件 |
| `/runs/{id}/evidence/gap` | POST | 资料操作员 | 演示用：制造桶证据缺行 |
| `/runs/{id}/evidence/repairs/retained` | POST | 资料操作员 | 用同批留存件恢复 |
| `/runs/{id}/evidence/repairs/replay` | POST | 资料操作员 | 发起原始卷宗重放 |

## 开发说明

| 部分 | 实现 |
|---|---|
| 前端 | 原生 HTML、CSS、JavaScript ES Modules、Nginx |
| 后端 | Python 3.11、FastAPI、SQLAlchemy、JWT |
| 数据库 | PostgreSQL 16 |
| 后台执行 | Redis 通知、数据库持久任务队列和行锁、Python worker |

从 `backend/src/domain/scenario.py` 修改业务入口和演示口径，运行 `docker compose up -d --build --wait` 查看变更。数据库表结构在 `backend/src/models` 和 `backend/migrations`，输入模型在 `backend/src/schemas`。业务页和请求接口在 `frontend/src/pages`、`frontend/src/api`。资料、执行、复核三个工作流实体各有独立表和 API；场景输入及结果保留在所属资料和批次中。

角色枚举在 `backend/src/constants/roles.py`、`frontend/src/constants/roles.js`，用于服务端访问控制、账号初始化、前端导航与签署。批次状态在 `backend/src/constants/job_state.py`、`frontend/src/constants/states.js`，用于队列、状态 API、状态标记与轮询。登记、提交、完成和签署事件写入审计表。

`COMPOSE_PROJECT_NAME` 设置栈名，`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT` 设置端口。通过 `DB_PASSWORD`、`JWT_SECRET`、`DEMO_PASSWORD` 配置本地凭据。设置 `SEED_DEMO=false` 可关闭演示数据载入，此时数据库初始没有演示账号。数据库密码调整后，已有数据卷需在数据库内同步修改用户密码。

## 验收范围

在内存计算目录，容量与最大深度可复核。卷宗和证据由 PostgreSQL 持久化。 当前界面用 JSON 编辑业务资料，以 JSON 下载证据。表结构经 `migrations/v001_workbench.py` 与 `migrations/v002_evidence_repair.py` 声明式升级，v002 对旧库增量补列、补表且可重复执行。后端测试（SQLite 内存库，无需 Docker）：

```sh
cd backend
PYTHONPATH=. python3 tests/test_evidence_repair.py   # 缺行/两路恢复/并发/幂等领域流程
SEED_DEMO=false PYTHONPATH=. python3 tests/test_evidence_api.py  # 路由、下载门禁与权限
```

过程和截图保存在批次的 [验收记录](../../test-artifacts/gb-6105/TESTING.md)。

[MIT License](LICENSE)
