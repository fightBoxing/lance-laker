# LCP — Lifecycle Policies CRUD 接 MySQL（本地 K8s）交付报告

## 1. 任务

把 LCP 控制面 `Lifecycle Policies` 资源**从零搭建**为完整 CRUD + enable/disable
开关，复用 Datasets/Tasks/Indexes 三轮已成熟的 ORM/RLS/集成测试模式。

> 与前三轮的差异：本轮**没有现成的 OpenAPI 合约和 router stub** —— 是从零设计、
> 从零落地。

## 2. 关键决策与权衡（动手前已暴露）

| 决策点                                     | 选项                                        | 选择                                                   | 理由                                                                               |
| ------------------------------------------ | ------------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| 是否先设计 OpenAPI                         | 先文档 / 直接 router                        | **先文档**                                             | 与前三轮保持一致，OpenAPI 是合约源                                                 |
| 路径层级                                   | 嵌套 / 顶层                                 | **嵌套** `/v1/datasets/{ds}/lifecycle-policies/{name}` | DDL 唯一约束 `(dataset_uuid, policy_name)`；与 Indexes 风格一致                    |
| 资源 ID                                    | `policy_name` / UUID                        | **`policy_name`**                                      | DDL 主业务键；同 dataset 内唯一                                                    |
| 单 dataset 多 policy？                     | 是 / 否                                     | **是**                                                 | DDL `UNIQUE (dataset_uuid, policy_name)` 显然支持多策略；不预设过度限制            |
| 行为类型                                   | 仅 CRUD / +run-now                          | **CRUD + enable/disable**                              | DDL 有 `enabled` 列；run-now 涉及调度器（属下一阶段）                              |
| `tier_rules` / `compaction_threshold` 校验 | 严格 schema / free-form                     | **free-form**                                          | 与 Indexes `params` 同处理；策略 schema 将来会变                                   |
| 租户隔离机制                               | 改 DDL 加 `tenant_id` / 父 dataset 间接守卫 | **父 dataset 间接守卫**                                | 与 Indexes 同模式；零改 DDL；服务层一行 `await dataset_service.get_dataset()` 即可 |

## 3. 改动清单

### 3.1 新增文件（4）

| 文件                                                   | 行数 | 用途                                                                                                                                                                                 |
| ------------------------------------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `docs/architecture/api/openapi/lcp-lifecycle-api.yaml` | 248  | 全新 OpenAPI 合约：CRUD + enable/disable + 嵌套路径                                                                                                                                  |
| `src/lcp/schemas/lifecycle.py`                         | 73   | `PolicyCreateRequest` / `PolicyUpdateRequest` / `PolicyResponse` / `PolicyListResponse`；OpenAPI 字段名与 ORM 列名一一对应，无需 alias                                               |
| `src/lcp/services/lifecycle_service.py`                | 195  | `create_policy` / `get_policy` / `list_policies` / `update_policy`（PATCH 部分更新）/ `delete_policy` / `set_enabled`；每个方法首步 `await dataset_service.get_dataset()` 间接走 RLS |
| `src/lcp/api/rest/routers/lifecycle.py`                | 222  | 7 个端点（list/create/get/patch/delete/enable/disable）；嵌套到 `/v1/datasets/{dataset_uuid}/lifecycle-policies`                                                                     |
| `tests/integration/test_lifecycle_mysql.py`            | 220  | 真实 MySQL 集成测试 3 条：CRUD 往返 + 重复约束 + enable/disable 切换                                                                                                                 |

### 3.2 修改文件（5）

| 文件                             | 改动                                                                                                           |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`           | 新增 `LifecyclePolicy` ORM；引入 `Boolean` 列；显式 `UniqueConstraint("dataset_uuid","policy_name")`           |
| `src/lcp/db/rls.py`              | 移除 `lifecycle_rules` 占位字符串；扩展注释说明 `vector_index` 与 `lifecycle_policy` 都通过父 dataset 间接守卫 |
| `src/lcp/api/rest/main.py`       | 注册 `lifecycle.router` 到 FastAPI app                                                                         |
| `tests/conftest.py`              | 测试 fixture 中也注册 `lifecycle.router`                                                                       |
| `tests/api/rest/test_routers.py` | 新增 `TestLifecycleRouter` 9 个 contract 用例；`TestRouteTableContract` 加入 4 条 lifecycle 路径               |

## 4. 关键设计：PATCH 的 "exclude_unset" 语义

`PolicyUpdateRequest` 所有字段 optional；service 层用：

```python
updates = payload.model_dump(exclude_unset=True)
for field, value in updates.items():
    setattr(obj, field, value)
```

这样：
- 字段**未传** → 保留原值（不动）
- 字段**显式传 `null`**（如 `{"ttl_days": null}`）→ 真的清空
- 字段**显式传新值** → 覆盖

回归测试 `test_patch_policy_only_supplied_fields` 验证：
- 修改 `ttl_days` 只动 `ttl_days`，不影响未传的 `index_optimize_cron`

这是 PATCH 与 PUT 的核心区别（PUT 应该全量替换；PATCH 是部分更新）。

## 5. 验证证据

### 5.1 测试结果

```
tests/api/grpc/test_server.py             13 passed
tests/api/rest/test_auth.py               10 passed
tests/api/rest/test_routers.py            36 passed   (+9 vs 上轮)
tests/integration/test_datasets_mysql.py   2 passed
tests/integration/test_indexes_mysql.py    3 passed
tests/integration/test_lifecycle_mysql.py  3 passed   (新)
tests/integration/test_tasks_mysql.py      3 passed
tests/unit/core/test_config.py             8 passed
tests/unit/core/test_security.py          19 passed
tests/unit/core/test_tenant.py             6 passed
tests/unit/db/test_rls.py                 16 passed
==================================================================
                                         119 passed in 6.74s
Coverage: 77.56% (gate 70%)               ✅
```

### 5.2 覆盖率（本轮新增模块）

| 模块                                         | 覆盖率   |
| -------------------------------------------- | -------- |
| `src/lcp/db/models.py`（含 LifecyclePolicy） | **100%** |
| `src/lcp/schemas/lifecycle.py`               | **100%** |
| `src/lcp/services/lifecycle_service.py`      | **100%** |
| `src/lcp/api/rest/routers/lifecycle.py`      | 56.2%    |

注：service 直接 100%（router 集成测试已穿透所有路径）；router 未覆盖的是错误分支（每个端点 2~3 个 `_not_found` / `_conflict` 调用，主成功路径以外的分支重复，可后续合并）。

### 5.3 Lint

```
ruff check src/lcp tests
All checks passed!  ✅
```

## 6. 关键 Bug 与修复轨迹

**0 个 bug** —— 模式已完全成熟，从零搭建一个新资源**首次跑测试就 119/119 全过**。

之前几轮总结的经验都内化到了实现中：
- `_PK_BIGINT` 跨方言主键（不再触发 `NOT NULL constraint failed: lifecycle_policy.id`）
- 复合 `UniqueConstraint` 显式声明（不再触发"重复插入不报 IntegrityError"）
- 直接 `select(func.count(Model.id))` 而非 `subquery()` 包裹（不再触发 RLS hook 失效）
- StaticPool 配置（不再触发 sqlite `:memory:` 多连接看到空 schema）

## 7. 与前三轮模式对比

| 维度         | Datasets     | Tasks               | Indexes             | **Lifecycle**              |
| ------------ | ------------ | ------------------- | ------------------- | -------------------------- |
| 路径层级     | 顶层         | 顶层                | 嵌套                | **嵌套**                   |
| 资源 ID      | UUID         | UUID                | 业务名              | **业务名**                 |
| RLS 命中表   | `dataset`    | `task`              | 间接（父 dataset）  | **间接（父 dataset）**     |
| 状态机       | 软删除       | submit/cancel/retry | optimize/merge/drop | **enable/disable + PATCH** |
| 字段 alias   | schema/table | type                | name/column/type    | **无（OpenAPI = ORM）**    |
| OpenAPI 文件 | 已有         | 已有                | 已有                | **新建**                   |
| Bug 数       | 6            | 0                   | 1                   | **0**                      |

→ 模式完全可复用，剩余 `meta_sync_log` / `worker_registry` / `task_event` 走同样套路。

## 8. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------------------------- |
| 1. 先想后写       | ✅ 启动前列出 8 处决策权衡；OpenAPI 合约先于代码完成                                                        |
| 2. 简单优先       | ✅ 不为 free-form JSON 定义校验；不引入 PUT（PATCH 已够）；不引入 run-now（属调度器）                       |
| 3. 外科手术式修改 | ✅ 仅修 RLS 中已有 `lifecycle_rules` 占位；不顺手清 `compactions`/`embedding_jobs`；只在新代码自动 lint fix |
| 4. 目标驱动执行   | ✅ 9 步计划逐步验证；每步独立可验证；**首跑 119/119 + 77.6% 覆盖率，零 bug**                                |

## 9. 后续建议

1. **router 错误分支测试** — 把 lifecycle/indexes/tasks 三个 router 覆盖率从 50~60% 推到 80%+
2. **`meta_sync_log` 资源** — 用同样模式落地（属于操作日志，可能直接关联调度器）
3. **`worker_registry` 资源 + 调度器** — 接入 task 状态机推进逻辑
4. **lifecycle worker** — 真正读 `lifecycle_policy` 表并产生 `Task` 行（DDL 级跨表协作的实战）
5. **`tier_rules` / `compaction_threshold` 校验** — 当前 free-form；将来稳定后可加 JSON Schema

## 10. 提交信息

```
feat(lifecycle): add lifecycle-policy CRUD + enable/disable on MySQL
```
