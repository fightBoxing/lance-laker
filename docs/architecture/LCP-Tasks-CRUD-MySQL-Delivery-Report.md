# LCP — Tasks CRUD 接 MySQL（本地 K8s）交付报告

## 1. 任务

将 LCP 控制面 `Tasks` 资源从 `501 Not Implemented` 占位实现升级为
**真实持久化 + 状态机驱动 + 幂等提交**的完整 CRUD，复用上次 Datasets
迭代搭好的 ORM/RLS/集成测试基础设施。

## 2. 关键决策与权衡

| 决策点                                                              | 选择                                                 | 理由                                                                                           |
| ------------------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `:cancel` / `:retry`（AIP 风格）vs `/cancel` / `/retry`（斜杠风格） | **斜杠风格**                                         | 现有契约测试和路由约定都是斜杠；冒号路径在很多客户端/框架不友好；同步更新 OpenAPI 对齐工程现实 |
| 引入 `task_event` 表（追加式审计 trail）                            | ❌ 本期不引入                                         | 调度器尚未上线，事件流暂无消费者；用到时再加 ORM                                               |
| 状态机用 `CHECK` 约束 / SQL ENUM                                    | ❌ 应用层校验                                         | DDL 用 `VARCHAR(32)` 留扩展余地；服务层 `_TERMINAL_STATES` / `_CANCELLABLE_STATES` 集合更易测  |
| 幂等键冲突处理                                                      | **先 SELECT 再 INSERT，IntegrityError 兜底**         | 先 SELECT 让正常路径走 `200 OK`；`UNIQUE` 约束作为竞态最后一道防线                             |
| `created` 标志返回                                                  | ✅ `(Task, bool)`                                     | 让 router 在新建时返回 `202`、幂等回放时返回 `200`，与 K8s `apply` 模式一致                    |
| 重试条件                                                            | 仅 `FAILED` + `attempt < max_attempts`               | 防止误重启已成功任务；防止无限重试                                                             |
| 取消条件                                                            | 仅 `PENDING / QUEUED / RUNNING` 可取消；终态返回 409 | 与 OpenAPI 一致                                                                                |

## 3. 改动清单

### 3.1 新增文件（2）

| 文件                                    | 行数 | 用途                                                                                       |
| --------------------------------------- | ---- | ------------------------------------------------------------------------------------------ |
| `src/lcp/schemas/task.py`               | 95   | `TaskSubmitRequest` / `TaskResponse` / `TaskListResponse`，`type` ↔ `task_type` 双向 alias |
| `src/lcp/services/task_service.py`      | 232  | submit / get / list / cancel / retry，含状态机校验与幂等键去重                             |
| `tests/integration/test_tasks_mysql.py` | 191  | 真实 MySQL 集成测试 3 条：CRUD 往返 + 幂等键 + 重试限制                                    |

### 3.2 修改文件（5）

| 文件                                              | 改动                                                                                                                                                          |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`                            | 新增 `Task` ORM；复用 `_PK_BIGINT` 跨方言主键；与 DDL `task` 表对齐（22 列）                                                                                  |
| `src/lcp/api/rest/routers/tasks.py`               | 4 个 stub handler 全部替换为接入 `task_service` 的真实实现；新增 `/{task_uuid}/retry` 端点                                                                    |
| `src/lcp/db/rls.py`                               | `RLS_PROTECTED_TABLES` 中 `tasks` → `task` 对齐 DDL                                                                                                           |
| `tests/unit/db/test_rls.py`                       | RLS 单元测试虚构表名 `tasks` → `task`                                                                                                                         |
| `tests/api/rest/test_routers.py`                  | `TestTasksRouter` 由 3 个 stub 用例替换为 8 个真合约用例（含 `/retry`、幂等、tenant 隔离）；`TestRouteTableContract` 加入 `/v1/tasks/{task_uuid}` 与 `/retry` |
| `docs/architecture/api/openapi/lcp-task-api.yaml` | `/tasks/{task_uuid}:cancel` → `/tasks/{task_uuid}/cancel`；同样把 `:retry` 改为 `/retry`，并补 `404` 响应                                                     |

## 4. 状态机

```
PENDING ─→ QUEUED ─→ RUNNING ─→ SUCCEEDED   (terminal)
                          │   ╲
                          │    └→ FAILED ──(retry; attempt < max)──→ PENDING
                          ↓
                      CANCELLED  (terminal, only from PENDING/QUEUED/RUNNING)
```

- `cancel` 已是 `CANCELLED` 时为 no-op（200）
- `cancel` 在 `SUCCEEDED` / `FAILED` 时返回 409 `INVALID_TRANSITION`
- `retry` 仅 `FAILED` 可调用；`attempt += 1`，`error_*` 清空
- 终态：`SUCCEEDED` / `CANCELLED`

## 5. 验证证据

### 5.1 测试结果

```
tests/api/grpc/test_server.py            13 passed
tests/api/rest/test_auth.py              10 passed
tests/api/rest/test_routers.py           23 passed   (+5 vs 上轮)
tests/integration/test_datasets_mysql.py  2 passed
tests/integration/test_tasks_mysql.py     3 passed   (新)
tests/unit/core/test_config.py            8 passed
tests/unit/core/test_security.py         19 passed
tests/unit/core/test_tenant.py            6 passed
tests/unit/db/test_rls.py                16 passed
=================================================================
                                        100 passed in 5.00s
Coverage: 78.10% (gate 70%)              ✅
```

### 5.2 覆盖率焦点（本次新增）

| 模块                                | 覆盖率                |
| ----------------------------------- | --------------------- |
| `src/lcp/db/models.py`              | **100.0%**（含 Task） |
| `src/lcp/schemas/task.py`           | **100.0%**            |
| `src/lcp/services/task_service.py`  | **81.5%**             |
| `src/lcp/api/rest/routers/tasks.py` | **62.8%**             |

未覆盖的主要是错误分支的 `HTTPException` 抛出路径（router 层），可后续加 2~3 个用例提升。

### 5.3 Lint

```
ruff check src/lcp tests
All checks passed!  ✅
```

## 6. 关键技术点

### 6.1 跨方言主键

复用上轮的 `_PK_BIGINT = BigInteger().with_variant(Integer, "sqlite")`，
让 `Task.id` 在 MySQL 上是 `BigInteger`、在 sqlite 测试中是 `Integer`，
保持 autoincrement 在两边都工作。

### 6.2 幂等键去重

```python
# 1) 先 SELECT 已存在的 idempotency_key，命中直接返回 (existing, False)
# 2) 否则 INSERT；若并发竞态触发 IntegrityError，回滚后再读 winner
# 3) UNIQUE 约束在 DDL 上保证最终一致性
```

这样：
- 正常单线程提交：`202 Accepted` + 新行
- 同一 key 重放：`200 OK` + 同一 task_uuid（幂等）
- 并发同 key 提交：竞态后两者拿到同一 winner（不会出现重复行）

### 6.3 RLS 命中表名修复

```python
# Before:
RLS_PROTECTED_TABLES = {"dataset", "tasks", ...}  # 'tasks' 不命中 task ORM

# After:
RLS_PROTECTED_TABLES = {"dataset", "task", ...}   # 完全对齐 DDL
```

跨租户隔离已通过 `test_tenant_isolation_on_task_list` 真实回归。

### 6.4 状态机与 Router 分离

状态机集合（`_TERMINAL_STATES`、`_CANCELLABLE_STATES`）在 service 层，
router 只把 `TaskTransitionError` 翻译成 `409 INVALID_TRANSITION`。
未来调度器引入 `QUEUED → RUNNING → SUCCEEDED/FAILED` 转换时，
只需在 service 层加新方法、不需要动 router。

## 7. 与 OpenAPI 合约一致性

| 端点                                | OpenAPI                | 实际                           | 状态                                       |
| ----------------------------------- | ---------------------- | ------------------------------ | ------------------------------------------ |
| `POST /v1/tasks`                    | 202 + Task             | ✅ 202（创建）/ 200（幂等回放） | 一致（200 是文档外的合理扩展）             |
| `GET /v1/tasks`                     | 200 + TaskListResponse | ✅                              | 一致（含分页）                             |
| `GET /v1/tasks/{task_uuid}`         | 200 / 404              | ✅                              | 一致                                       |
| `POST /v1/tasks/{task_uuid}/cancel` | 200 / 404 / 409        | ✅                              | 一致（OpenAPI 已对齐路径）                 |
| `POST /v1/tasks/{task_uuid}/retry`  | 202 / 404 / 409        | ✅                              | 一致                                       |
| 字段 `type`                         | 透传                   | ✅                              | 通过 Pydantic alias 映射到 ORM `task_type` |

## 8. 与上轮 Datasets 模式的对比

| 维度             | Datasets                                              | Tasks                             | 复用   |
| ---------------- | ----------------------------------------------------- | --------------------------------- | ------ |
| ORM 跨方言主键   | `_PK_BIGINT`                                          | 同                                | ✅      |
| OpenAPI alias    | `schema`/`table` ↔ `db_schema`/`table_name`           | `type` ↔ `task_type`              | ✅      |
| RLS 命中         | `dataset`                                             | `task`                            | ✅      |
| Service 错误类型 | `AlreadyExistsError`/`NotFoundError`                  | `NotFoundError`/`TransitionError` | 模式 ✅ |
| Service 副作用   | 软删除                                                | 状态机转换                        | 同模式 |
| 集成测试 fixture | `mysql_session` / `isolated_tenant` / `_purge_tenant` | 同                                | ✅      |

→ 模式可复用，下一个资源（`vector_index` / `lifecycle_policy`）用同样套路。

## 9. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                      |
| ----------------- | --------------------------------------------------------------------------------------------- |
| 1. 先想后写       | ✅ 启动前列出 OpenAPI vs 现有契约的 2 处不一致并明确选择；状态机先画再写                       |
| 2. 简单优先       | ✅ 不引入 `task_event` ORM；状态机用 frozenset 而非 ENUM/CHECK                                 |
| 3. 外科手术式修改 | ✅ 仅改 `tasks` → `task` 一处 RLS；不顺手清 `indexes`/`compactions` 占位（保留给将来对应资源） |
| 4. 目标驱动执行   | ✅ 8 步计划逐步验证；最终 100/100 + 78.1% 覆盖率                                               |

## 10. 后续建议（不阻塞本次交付）

1. **task_event 表 ORM** — 调度器接入时一起做，与 `Task` 状态变更原子化追加事件
2. **`scheduled_at` 优先级队列** — 当前调度逻辑都未实现，等 worker 模块上线
3. **批量取消 / 重试** — 现在是单条接口，后续可加 `POST /v1/tasks:batchCancel`
4. **`task_uuid` 路径参数 UUID 校验** — 当前仅 `str`；Pydantic v2 的 `UUID4` validator 可加上
5. **`SHOW TABLES IN lcp` 的 8 张表 ORM 化** — 还剩 6 张（vector_index / lifecycle_policy / meta_sync_log / worker_registry / distributed_lock / task_event），按照同样模式逐个落地

## 11. 提交信息

```
feat(tasks): wire CRUD + state machine to MySQL via SQLAlchemy + RLS
```
