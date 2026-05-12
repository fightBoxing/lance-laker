# LCP — Vectorization Rule CRUD 接 MySQL（本地 K8s）交付报告

## 1. 任务

把 LCP 控制面 `Vectorization Rule` 资源**从零落地**为完整 CRUD + enable/disable
开关，支持"自动向量化绑定"。复用 Datasets/Tasks/Indexes/Lifecycle 四轮已成熟模式。

> 与 Lifecycle 一样属于"从零搭建"（无 OpenAPI/无 router stub）；但**首次引入跨字段联合校验**：`trigger_type` 与 `cron_expr` 的依赖关系。

## 2. 关键决策与权衡（动手前已暴露）

| 决策点                            | 选项                    | 选择                                                             | 理由                                                                         |
| --------------------------------- | ----------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 路径层级                          | 嵌套 / 顶层             | **嵌套** `/v1/datasets/{ds}/vectorization-rules/{target_column}` | DDL 唯一约束 `(dataset_uuid, target_column)`；与 Lifecycle/Index 一致        |
| 资源 ID                           | `target_column` / UUID  | **`target_column`**                                              | 业务键，URL 表达力强                                                         |
| `trigger_type` 类型               | free-form / Literal     | **`Literal["ON_INSERT","SCHEDULED","MANUAL"]`**                  | DDL COMMENT 已定义；类型安全                                                 |
| `source_columns` 类型             | 任意 JSON / `list[str]` | **`list[str]` 严格**                                             | DDL COMMENT "Array of source column names"；前端会读，必须明确               |
| `cron_expr` × `trigger_type` 校验 | 不校验 / 严格联合校验   | **严格联合校验**                                                 | SCHEDULED 没 cron 是"非运行态"，应用启动后任务调度直接挂；早拒绝胜过运行时炸 |
| PATCH 校验时机                    | 仅请求模型 / 合并状态   | **合并状态**                                                     | PATCH 部分字段无法独立判定；service 层合并后再校验                           |
| 行为类型                          | CRUD / +apply           | **CRUD + enable/disable**                                        | apply 触发任务属调度器；与 Lifecycle 对齐                                    |
| `model_endpoint` 是否敏感         | 加密 / 明文             | **明文**                                                         | 与 dataset `storage_uri` 同处理；加密是独立议题                              |
| 租户隔离                          | 父 dataset 间接守卫     | **同上**                                                         | DDL 同样无 `tenant_id` 列                                                    |

## 3. 改动清单

### 3.1 新增文件（5）

| 文件                                                       | 行数 | 用途                                                                                      |
| ---------------------------------------------------------- | ---- | ----------------------------------------------------------------------------------------- |
| `docs/architecture/api/openapi/lcp-vectorization-api.yaml` | 305  | 全新 OpenAPI 合约：CRUD + enable/disable + 嵌套路径 + 联合校验语义                        |
| `src/lcp/schemas/vectorization.py`                         | 116  | Pydantic schemas + `_validate_cron_required_when_scheduled` 跨字段校验，复用到 service 层 |
| `src/lcp/services/vectorization_service.py`                | 207  | CRUD + `set_enabled` + 合并状态 PATCH 校验                                                |
| `src/lcp/api/rest/routers/vectorization.py`                | 235  | 7 个端点；service 异常 → 400/404/409                                                      |
| `tests/integration/test_vectorization_mysql.py`            | 245  | 真实 MySQL 集成测试 3 条                                                                  |

### 3.2 修改文件（4）

| 文件                             | 改动                                                                                                              |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`           | 新增 `VectorizationRule` ORM；显式 `UniqueConstraint("dataset_uuid","target_column")`                             |
| `src/lcp/api/rest/main.py`       | 注册 `vectorization.router`                                                                                       |
| `tests/conftest.py`              | 测试 fixture 注册 `vectorization.router`                                                                          |
| `tests/api/rest/test_routers.py` | 新增 `TestVectorizationRouter` 10 个 contract 用例（含 422 / 400 联合校验）；`TestRouteTableContract` 加 4 条路径 |

## 4. 关键设计：跨字段联合校验的双层防御

`SCHEDULED` 没有 `cron_expr` 是无效状态；本轮在两个层次都防御：

### 4.1 第一层：请求模型（CREATE）

```python
class RuleCreateRequest(BaseModel):
    @model_validator(mode="after")
    def _check_cron(self) -> "RuleCreateRequest":
        _validate_cron_required_when_scheduled(
            self.trigger_type, self.cron_expr,
        )
        return self
```

→ 客户端传入完整不合法 payload 时，FastAPI 直接 422 拒绝。

### 4.2 第二层：合并状态（PATCH）

```python
async def update_rule(...):
    obj = await get_rule(...)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(obj, field, value)

    # Re-validate the merged state
    try:
        validate_cron_required_when_scheduled(obj.trigger_type, obj.cron_expr)
    except ValueError as exc:
        await session.rollback()
        raise RuleValidationError(str(exc)) from exc
```

为什么不用 `RuleUpdateRequest` 上的 validator？
- PATCH 字段全 optional；单看请求体无法判定（用户可能只传 `cron_expr`，依赖现有 `trigger_type`）
- 必须在合并后再校验

回归测试 `test_patch_promoting_to_scheduled_without_cron_returns_400` 验证这层防御。

## 5. 验证证据

### 5.1 测试结果

```
tests/api/grpc/test_server.py                  13 passed
tests/api/rest/test_auth.py                    10 passed
tests/api/rest/test_routers.py                 45 passed   (+10 vs 上轮)
tests/integration/test_datasets_mysql.py        2 passed
tests/integration/test_indexes_mysql.py         3 passed
tests/integration/test_lifecycle_mysql.py       3 passed
tests/integration/test_tasks_mysql.py           3 passed
tests/integration/test_vectorization_mysql.py   3 passed   (新)
tests/unit/core/test_config.py                  8 passed
tests/unit/core/test_security.py               19 passed
tests/unit/core/test_tenant.py                  6 passed
tests/unit/db/test_rls.py                      16 passed
======================================================================
                                              131 passed in 8.86s
Coverage: 77.95% (gate 70%)                    ✅
```

### 5.2 覆盖率（本轮新增模块）

| 模块                                           | 覆盖率    |
| ---------------------------------------------- | --------- |
| `src/lcp/db/models.py`（含 VectorizationRule） | **100%**  |
| `src/lcp/schemas/vectorization.py`             | **96.6%** |
| `src/lcp/services/vectorization_service.py`    | **92.9%** |
| `src/lcp/api/rest/routers/vectorization.py`    | 53.2%     |

### 5.3 Lint

```
ruff check src/lcp tests
All checks passed!  ✅
```

## 6. 关键 Bug 与修复轨迹

**0 个 bug** —— 模式完全成熟，从零搭建首次跑测试即 131/131 全过。

跨字段联合校验从设计阶段就考虑到了 PATCH 不能独立校验的问题，采用双层防御方案，避免了运行时再发现。

## 7. 与前四轮模式对比

| 维度         | Datasets  | Tasks      | Indexes    | Lifecycle | **Vectorization**           |
| ------------ | --------- | ---------- | ---------- | --------- | --------------------------- |
| 路径层级     | 顶层      | 顶层       | 嵌套       | 嵌套      | **嵌套**                    |
| 资源 ID      | UUID      | UUID       | 业务名     | 业务名    | **业务名**                  |
| RLS 命中表   | `dataset` | `task`     | 间接       | 间接      | **间接**                    |
| 跨字段校验   | 无        | 状态机转换 | 状态机转换 | 无        | **请求层 + 合并层联合校验** |
| OpenAPI 起点 | 已有      | 已有       | 已有       | 新建      | **新建**                    |
| Bug 数       | 6         | 0          | 1          | 0         | **0**                       |

→ 前五轮中后四轮零回归（仅 Indexes 1 个）；模式高度成熟。

## 8. 项目整体状态

| 资源              | ORM   | Service | Router | OpenAPI | 集成测试 |
| ----------------- | ----- | ------- | ------ | ------- | -------- |
| Dataset           | ✅     | ✅       | ✅      | ✅       | ✅        |
| Task              | ✅     | ✅       | ✅      | ✅       | ✅        |
| Index             | ✅     | ✅       | ✅      | ✅       | ✅        |
| Lifecycle         | ✅     | ✅       | ✅      | ✅       | ✅        |
| **Vectorization** | **✅** | **✅**   | **✅**  | **✅**   | **✅**    |
| TaskEvent         | ❌     | ❌       | ❌      | ❌       | ❌        |
| MetaSyncLog       | ❌     | ❌       | ❌      | ❌       | ❌        |
| WorkerRegistry    | ❌     | ❌       | ❌      | ❌       | ❌        |

DDL 8 张表已落地 5 张（核心业务对象齐了 + Dataset 兄弟表）；剩下 3 张都是调度/审计/工作流类支撑表。

## 9. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| 1. 先想后写       | ✅ 启动前列出 9 处决策权衡；OpenAPI 合约先于代码完成；联合校验设计提前考虑 PATCH 场景                           |
| 2. 简单优先       | ✅ 不为 free-form `extra` JSON 定义子 schema；不引入 PUT；不引入 `apply` 端点（属调度器）                       |
| 3. 外科手术式修改 | ✅ 仅修 `models.py` / `main.py` / `conftest.py` / `test_routers.py` 必要位置；不顺手清 RLS 占位（已是上轮清过） |
| 4. 目标驱动执行   | ✅ 9 步计划逐步验证；每步独立可验；**首跑 131/131 + 78% 覆盖率，零 bug**                                        |

## 10. 后续建议

1. **router 错误分支测试** — 5 个 router 平均覆盖率 ~55%；可统一加错误分支测试推到 80%+
2. **`worker_registry` + 调度器** — 目前 5 个核心资源都"只有元数据"；接入调度器后 Tasks 状态机才能真正推进
3. **`task_event` 资源** — 任务事件流（Tasks 的子表），用于审计与调试
4. **`scripts/bootstrap-mysql.sh`** — 一键 DDL + 用户搭建本地环境
5. **GitHub Actions 加 MySQL service** — 让 14 个集成测试也在 CI 跑

## 11. 提交信息

```
feat(vectorization): add vectorization-rule CRUD + cross-field validation on MySQL
```
