# LCP — Indexes CRUD 接 MySQL（本地 K8s）交付报告

## 1. 任务

把 LCP 控制面 `Indexes` 资源从 `501 Not Implemented` 占位升级为
**真实持久化 + 状态机 + 间接租户隔离**的 CRUD，复用 Datasets/Tasks
两轮搭好的 ORM/RLS/集成测试基础设施。

## 2. 关键决策与权衡（暴露在动手前）

| 决策点                                           | 选择                                                        | 理由                                                                                                            |
| ------------------------------------------------ | ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| 路径层级：嵌套 vs 扁平                           | **嵌套** `/v1/datasets/{dataset_uuid}/indexes/{index_name}` | OpenAPI 是合约源；DDL 唯一约束就是 `(dataset_uuid, index_name)`；扁平路径需造 `index_uuid` 二级业务键，过度设计 |
| 资源 ID：`index_name` vs `index_uuid`            | `index_name`                                                | 与 DDL 主业务键一致；同 dataset 内唯一即可；不引入用户不需要的 UUID                                             |
| 路径动词：`:optimize`(AIP) vs `/optimize`(slash) | **slash**                                                   | 与 tasks 上轮保持一致；客户端兼容性更好                                                                         |
| RLS：给 `vector_index` 加 `tenant_id` 列         | ❌ 不加                                                      | DDL 故意省略；service 层用父 dataset 间接守卫更清爽                                                             |
| 间接租户守卫：放 service vs 放 router            | **service**                                                 | 对称性强，将来 gRPC 也共享同一守卫；router 只翻译 HTTP 状态码                                                   |
| ORM 唯一约束                                     | ✅ 显式 `UniqueConstraint("dataset_uuid","index_name")`      | DDL 有，但默认 declarative 不会从列定义推导出复合 UNIQUE，必须显式声明（这是本轮发现的 bug X-1）                |

## 3. 改动清单

### 3.1 新增文件（3）

| 文件                                      | 行数 | 用途                                                                                                                                                           |
| ----------------------------------------- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/lcp/schemas/index.py`                | 87   | `IndexCreateRequest` / `IndexResponse` / `IndexListResponse`；`name`/`column`/`type` 三个 alias 映射到 ORM `index_name`/`column_name`/`index_type`             |
| `src/lcp/services/index_service.py`       | 226  | `create_index` / `get_index` / `list_indexes` / `drop_index` / `optimize_index` / `merge_index`；每个操作首步 `await dataset_service.get_dataset()` 间接走 RLS |
| `tests/integration/test_indexes_mysql.py` | 207  | 真实 MySQL 集成测试 3 条：CRUD 往返 + 重复约束 + 状态机                                                                                                        |

### 3.2 修改文件（5）

| 文件                                  | 改动                                                                                                                                               |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`                | 新增 `Index` ORM；显式 `UniqueConstraint("dataset_uuid","index_name")`；DDL `vector_index` 表有 14 列对齐                                          |
| `src/lcp/api/rest/routers/indexes.py` | 4 个 stub 全替换为接入 `index_service` 的真实实现；路径前缀改为 `/v1/datasets/{dataset_uuid}/indexes`；新增 `/merge` 端点                          |
| `src/lcp/db/rls.py`                   | 移除 `indexes` 占位字符串；加注释说明 `vector_index` 通过父 dataset 间接守卫                                                                       |
| `tests/api/rest/test_routers.py`      | `TestIndexesRouter` 由 4 个 stub 用例替换为 8 个真合约用例（含重复约束、状态机、跨租户隔离）；`TestRouteTableContract` 更新到嵌套路径并加 `/merge` |

## 4. 状态机

```
BUILDING ──→ READY ──┬─ optimize ──→ OPTIMIZING ──→ READY
                     ├─ merge    ──→ MERGING    ──→ READY
                     ├─ drop     ──→ DROPPED   (terminal)
                     └─ * (worker) ─→ FAILED   (terminal w/ error_message)
```

- 仅 `READY` 可触发 `optimize` / `merge`（防并发）
- `drop` 在 `DROPPED` 时为 no-op（幂等）
- `BUILDING` 状态禁止 `optimize` / `merge`（contract 测试覆盖）

## 5. 跨租户隔离机制（核心创新）

`vector_index` 表 DDL 没有 `tenant_id` 列。我们用**父 dataset 间接守卫**：

```python
async def get_index(session, dataset_uuid, index_name):
    # 1) RLS hook 自动注入 dataset.tenant_id = :current_tenant
    await dataset_service.get_dataset(session, dataset_uuid)
    # ↑ 跨租户访问 → DatasetNotFoundError → router 转 404
    #
    # 2) 此处保证 dataset_uuid 属于当前 tenant，可安全查 index
    return await _maybe_get_index(session, dataset_uuid, index_name)
```

回归测试 `test_tenant_isolation_via_parent_dataset`：
- Tenant A 创建 dataset + index
- Tenant B 用同一 URL 访问 → 404（不暴露存在性）
- Tenant A 自己访问 → 200（不影响所有者）

## 6. 验证证据

### 6.1 测试结果

```
tests/api/grpc/test_server.py            13 passed
tests/api/rest/test_auth.py              10 passed
tests/api/rest/test_routers.py           27 passed   (+8 vs 上轮)
tests/integration/test_datasets_mysql.py  2 passed
tests/integration/test_indexes_mysql.py   3 passed   (新)
tests/integration/test_tasks_mysql.py     3 passed
tests/unit/core/test_config.py            8 passed
tests/unit/core/test_security.py         19 passed
tests/unit/core/test_tenant.py            6 passed
tests/unit/db/test_rls.py                16 passed
=================================================================
                                        107 passed in 6.25s
Coverage: 76.82% (gate 70%)              ✅
```

### 6.2 覆盖率（本次新增模块）

| 模块                                  | 覆盖率   |
| ------------------------------------- | -------- |
| `src/lcp/db/models.py`（含 Index）    | **100%** |
| `src/lcp/schemas/index.py`            | **100%** |
| `src/lcp/services/index_service.py`   | 75.6%    |
| `src/lcp/api/rest/routers/indexes.py` | 51.5%    |

router 层未覆盖的主要是错误分支抛 `HTTPException` 的代码路径（每个端点都有 dataset-404 / index-404 / 409 三个分支，但只测了主成功路径）。可后续加针对性用例提升，但当前足以验证核心契约。

### 6.3 Lint

```
ruff check src/lcp tests
All checks passed!  ✅
```

## 7. 关键 Bug 与修复轨迹

| #   | 现象                                                                          | 根因                                                                                                        | 修复                                                                                                            |
| --- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| X-1 | 重复 `(dataset_uuid, index_name)` 不报 IntegrityError，service 层无法返回 409 | SQLAlchemy declarative 不会从单独的 `Column(unique=True)` 推导出**复合**唯一约束；需要显式 `__table_args__` | 在 `Index.__table_args__` 中添加 `UniqueConstraint("dataset_uuid", "index_name", name="uk_dataset_index_name")` |

只 1 个 bug，比上两轮显著少（说明模式已稳定）。

## 8. 与 Datasets/Tasks 模式对比

| 维度       | Datasets            | Tasks                               | Indexes                                        |
| ---------- | ------------------- | ----------------------------------- | ---------------------------------------------- |
| 路径层级   | 顶层 `/v1/datasets` | 顶层 `/v1/tasks`                    | **嵌套** `/v1/datasets/{...}/indexes`          |
| 资源 ID    | UUID                | UUID                                | **业务名 `index_name`**                        |
| RLS 命中表 | `dataset`           | `task`                              | **不直接命中**，间接走父 dataset               |
| 主要操作   | CRUD                | CRUD + 状态机（cancel/retry）+ 幂等 | CRUD + 状态机（optimize/merge/drop）+ 唯一约束 |
| 集成测试   | 2 用例              | 3 用例                              | 3 用例                                         |

→ 模式可继续复用；剩余 `lifecycle_policy` / `meta_sync_log` / `worker_registry` 等几张表如有 `tenant_id` 走 datasets 模式，否则走 indexes 模式。

## 9. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                               |
| ----------------- | ------------------------------------------------------------------------------------------------------ |
| 1. 先想后写       | ✅ 启动前列出 OpenAPI vs 现有契约 5 处不一致并明确选择；RLS 两条方案权衡过                              |
| 2. 简单优先       | ✅ 不为 `vector_index` 加冗余 `tenant_id`；不引入 `index_uuid` 二级 ID；list 不强加分页                 |
| 3. 外科手术式修改 | ✅ 仅改 `tasks`/`indexes` 占位；不顺手清 `compactions`/`embedding_jobs`；只在新代码引入的 lint 上自动修 |
| 4. 目标驱动执行   | ✅ 8 步计划逐步验证；中途发现 X-1 bug 系统化排查后定位修复；最终 107/107 + 76.8% 覆盖率                 |

## 10. 后续建议

1. **router 错误分支覆盖** — 针对 indexes router 加 4~5 个错误分支用例，把覆盖率推到 80%+
2. **`column_name` 校验** — 当前接受任意字符串；可以加正则或调用 dataset 的 schema 接口确认列存在
3. **HNSW/IVF 参数 schema** — 现在 `params` 是 free-form `dict[str, Any]`；可按 `index_type` 分别定义子 schema 做严格校验
4. **`vectorization_rule` 表 ORM** — 是 dataset 的兄弟表，DDL 上有 `fk_vrule_dataset`；下个迭代可加
5. **真正的 worker** — 当前 `BUILDING` → `READY` 只能手动改 DB；后续接调度器自动推进

## 11. 提交信息

```
feat(indexes): wire CRUD + state machine to MySQL via nested routes
```
