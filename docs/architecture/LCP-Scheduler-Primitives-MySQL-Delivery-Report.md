# LCP — Scheduler Primitives 接 MySQL（本地 K8s）交付报告

## 1. 任务

把 LCP 控制面**首次引入运行时执行能力**：实现调度器原语 — `WorkerRegistry`
ORM + 注册/心跳/抢任务/完成/失败/回收 service。让前 5 轮搭好的 5 张元数据
表（dataset / task / index / lifecycle_policy / vectorization_rule）真正"动起来"。

> 这是项目自启动以来**首个有状态、跨租户、有并发要求的运行时模块**，
> 复杂度显著高于前 5 轮的纯 CRUD。

## 2. 关键决策与权衡（动手前已暴露）

### 2.1 范围收敛 — 极其关键

| 范围项                                                             | 是否做 | 理由                                                                                     |
| ------------------------------------------------------------------ | ------ | ---------------------------------------------------------------------------------------- |
| `WorkerRegistry` ORM + 注册/心跳/注销 service                      | ✅      | 调度器基础                                                                               |
| Task 状态机扩展：`claim_next_task` / `complete_task` / `fail_task` | ✅      | 核心目标                                                                                 |
| At-most-once dispatch：`SELECT FOR UPDATE SKIP LOCKED`             | ✅      | 否则多 worker 抢同 task                                                                  |
| 心跳超时回收（reap_dead_workers）                                  | ✅      | 否则 worker 死掉 task 永久卡住                                                           |
| 调度器进程主循环                                                   | ❌      | 长进程运维（信号/shutdown/metrics）属独立议题；本期只暴露 service 原语，让用户决定怎么跑 |
| Worker 注册 REST API                                               | ❌      | Worker 在内网信任边界；走 service / gRPC 更合理                                          |
| `task_event` 审计表                                                | ❌      | 留给下一轮                                                                               |
| `distributed_lock` 表                                              | ❌      | `FOR UPDATE SKIP LOCKED` 已够；distributed_lock 是 leader 选举用，本期单 scheduler       |

→ **本期 = 让 service 原语真实跑通**；long-running loop 是另一个独立议题。

### 2.2 跨租户问题 — 核心创新

调度器是**系统级进程**，不属于任何 tenant，但要：
- 读所有 tenant 的 task → 与 RLS hook 直接冲突
- 更新 task 时不能丢失原 `tenant_id`
- worker_registry 表本身无 tenant_id

| 方案                                                                           | 选择                                     |
| ------------------------------------------------------------------------------ | ---------------------------------------- |
| A. RLS hook 加特殊值检查                                                       | ❌ 多个特殊分支                           |
| **B. `TenantPrincipal.is_system` 标志 + `with_system_context()` 上下文管理器** | ✅ 与现有 `set_current_tenant` 同一套机制 |
| C. 调度器用单独 engine（不挂 RLS）                                             | ❌ 双 engine 配置浪费                     |

最终：RLS hook 检查 `principal.is_system`，为 True 时**跳过 WHERE 注入但保持
fail-closed 语义**。代码仍需手动保留每行原 tenant_id 不被覆写（在 service
中体现）。

### 2.3 并发原语

`SELECT ... FOR UPDATE SKIP LOCKED`：
- MySQL 8.0 原生支持（已是项目目标版本）
- SQLite 自动忽略 FOR UPDATE 子句 → 单元测试不验证并发；改由集成测试覆盖
- SQLAlchemy `with_for_update(skip_locked=True)` 在两边都接受

### 2.4 Worker 注册语义

| 决策                   | 选择                                           |
| ---------------------- | ---------------------------------------------- |
| `lease_id` 由谁生成    | client（worker 自己 UUID）                     |
| 重复注册               | UPSERT（worker 重启场景常见）                  |
| `in_flight` 维护       | 显式 +/-（行锁保证并发安全）                   |
| 心跳超时阈值           | 30s 默认 + 可配置                              |
| 回收时是否消耗 attempt | 不消耗（不能因 worker 故障扣 user 的 attempt） |

## 3. 改动清单

### 3.1 新增文件（3）

| 文件                                            | 行数 | 用途                                                                                     |
| ----------------------------------------------- | ---- | ---------------------------------------------------------------------------------------- |
| `src/lcp/services/scheduler_service.py`         | 384  | 调度器原语：register/heartbeat/drain/claim/complete/fail/reap_dead_workers + system 校验 |
| `tests/unit/db/test_rls_system.py`              | 98   | RLS hook 在 system principal 下跳过过滤的回归测试                                        |
| `tests/unit/services/test_scheduler_service.py` | 333  | 15 个单元测试覆盖系统守卫、worker 生命周期、抢任务、状态机、容量、优先级、reaper         |
| `tests/unit/services/__init__.py`               | 0    | 新目录的 package marker                                                                  |
| `tests/integration/test_scheduler_mysql.py`     | 270  | 真实 MySQL 集成测试 4 条，包括**真实并发抢锁**                                           |

### 3.2 修改文件（3）

| 文件                     | 改动                                                                                                       |
| ------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`   | 新增 `WorkerRegistry` ORM；声明为 system-level（无 tenant_id）                                             |
| `src/lcp/core/tenant.py` | `TenantPrincipal` 加 `is_system` 字段；新增 `with_system_context()` 上下文管理器和 `SYSTEM_TENANT_ID` 常量 |
| `src/lcp/db/rls.py`      | hook 检查 `principal.is_system`，为 True 时跳过 WHERE 注入但保留其他守卫                                   |

## 4. 状态机（扩展）

```
                     ┌────────────────────────────────┐
                     │  worker_registry               │
                     │   ALIVE → DRAINING → DEAD      │
                     └────────────────────────────────┘
                                    │
       claim_next_task              │  reap_dead_workers
       (FOR UPDATE SKIP LOCKED)     ▼  (heartbeat timeout)
                                                       
PENDING ──claim──→ RUNNING ──complete──→ SUCCEEDED
                      │   ╲
                      │    └─fail────→ FAILED
                      │
                      └─reaper──→ PENDING (worker_id cleared)
                                         │
                                         ▼
                                 next worker can pick
```

关键并发不变量：
- `claim_next_task` 内部 `SELECT FOR UPDATE SKIP LOCKED` + UPDATE 在**同一事务**完成 → 行锁保证 at-most-once
- `complete_task` / `fail_task` 验证 `task.worker_id == worker_id`（防止跨 worker 误转换）
- `reap_dead_workers` 是 idempotent（DEAD worker 跳过；多次调用结果稳定）

## 5. 验证证据

### 5.1 测试结果

```
tests/api/grpc/test_server.py                  13 passed
tests/api/rest/test_auth.py                    10 passed
tests/api/rest/test_routers.py                 45 passed
tests/integration/test_datasets_mysql.py        2 passed
tests/integration/test_indexes_mysql.py         3 passed
tests/integration/test_lifecycle_mysql.py       3 passed
tests/integration/test_scheduler_mysql.py       4 passed   (新)
tests/integration/test_tasks_mysql.py           3 passed
tests/integration/test_vectorization_mysql.py   3 passed
tests/unit/core/test_config.py                  8 passed
tests/unit/core/test_security.py               19 passed
tests/unit/core/test_tenant.py                  6 passed
tests/unit/db/test_rls.py                      16 passed
tests/unit/db/test_rls_system.py                4 passed   (新)
tests/unit/services/test_scheduler_service.py  15 passed   (新)
======================================================================
                                              154 passed in 10.53s
Coverage: 79.28% (gate 70%)                    ✅
```

### 5.2 覆盖率（本轮新增 / 修改模块）

| 模块                                          | 覆盖率    |
| --------------------------------------------- | --------- |
| `src/lcp/db/models.py`（含 WorkerRegistry）   | **100%**  |
| `src/lcp/core/tenant.py`（含 system context） | **97.6%** |
| `src/lcp/db/rls.py`（含 system bypass）       | **95.7%** |
| `src/lcp/services/scheduler_service.py`       | **85.8%** |

未覆盖的 14% 主要是错误重试分支（`register_worker` 内部 IntegrityError 自递归
那条路径，需要专门构造的并发场景才能触发）和反向 fail-closed 分支。

### 5.3 真实并发回归（关键证据）

`test_concurrent_workers_do_not_double_claim` 用**两个独立 engine 同时调用**
`claim_next_task`，断言**只有一个会拿到任务，另一个 None 返回**。在真实 MySQL
8.0 上通过 → `FOR UPDATE SKIP LOCKED` 工作正常。

### 5.4 Lint

```
ruff check src/lcp tests
All checks passed!  ✅
```

## 6. 关键 Bug 与修复轨迹

| #   | 现象                                            | 根因                                                                                                      | 修复                                            |
| --- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Y-1 | `SyntaxError: unterminated string literal`      | edit_file 把 docstring 的 `"""` 错误转义成了 `\"\"\"`                                                     | multi_replace 修复两处 docstring                |
| Y-2 | `Re - registration` 出现在源代码（多余空格）    | IDE 插件干扰文本                                                                                          | multi_replace 修复                              |
| Y-3 | `assert first.lease_id != second.lease_id` 失败 | SQLAlchemy identity map：两次 register_worker 返回**同一 Python 对象**；`first.lease_id` 是 mutation 后值 | 测试中提前把 `first.lease_id` 复制到字符串变量  |
| Y-4 | `assert "tenant_id" not in sql` 误报            | 列名 `tenant_id` 本来就在 SELECT 列里；断言写得太宽                                                       | 改成 `assert "WHERE" not in sql.upper()` 更准确 |

4 个 bug 都在测试编写期发现并立即修复，没遗留到运行时。Y-1 / Y-2 是工具问题；Y-3 / Y-4 是真实测试设计学习。

## 7. 与前五轮模式的延续与突破

| 维度     | 前五轮（CRUD）    | 本轮（Scheduler）                              |
| -------- | ----------------- | ---------------------------------------------- |
| 资源类型 | 元数据            | **运行时执行**                                 |
| 租户模型 | 单 tenant 上下文  | **System 跨租户上下文（首次）**                |
| 并发模型 | 无（单连接）      | **真实多连接抢锁**                             |
| 状态变化 | 用户驱动          | **Worker 驱动（首次）**                        |
| 失败恢复 | N/A               | **Reaper 重新入队（首次）**                    |
| OpenAPI  | ✅ 必有            | ❌ 故意不做（运行时模块在内网信任边界）         |
| Router   | ✅ 必有            | ❌ 故意不做（worker 走 service / gRPC）         |
| Bug 数   | 6 / 0 / 1 / 0 / 0 | **4（全部测试设计 / 工具问题；运行时零 bug）** |

→ 这一轮的"零运行时 bug"建立在前五轮夯实的 ORM/RLS 基础之上；
模式高度成熟。

## 8. 项目整体状态

| 资源                                               | ORM   | Service | Router             | OpenAPI | 集成测试 |
| -------------------------------------------------- | ----- | ------- | ------------------ | ------- | -------- |
| Dataset / Task / Index / Lifecycle / Vectorization | ✅     | ✅       | ✅                  | ✅       | ✅        |
| **WorkerRegistry**                                 | **✅** | **✅**   | **N/A（无 REST）** | **N/A** | **✅**    |
| TaskEvent / MetaSyncLog                            | ❌     | ❌       | ❌                  | ❌       | ❌        |

DDL 8 张表已落地 6 张（**75%**）；剩下 2 张都是审计/操作日志类（task_event, meta_sync_log）。

## 9. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                                                      |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 1. 先想后写       | ✅ 启动前列出 4 个核心决策类（范围/跨租户/并发原语/注册语义）；明确暴露权衡再推进                                              |
| 2. 简单优先       | ✅ **不**做长进程主循环；**不**做 distributed_lock；**不**做 worker REST API；用 `is_system: bool` 而非新建 SystemPrincipal 类 |
| 3. 外科手术式修改 | ✅ 仅在 RLS hook 加 6 行 system bypass；TenantPrincipal 加 1 个字段；不顺手清其他占位                                          |
| 4. 目标驱动执行   | ✅ 6 步计划逐步验证；每步都有测试佐证；最关键的并发不变量有真实 MySQL 回归测试                                                 |

## 10. 后续建议

1. **`task_event` 表 ORM** — 与 scheduler 状态变更原子化追加事件，做审计 trail
2. **长进程主循环 + CLI** — `python -m lcp.scheduler` 入口，把这些原语接到信号/日志/metrics
3. **Worker gRPC API** — `RegisterWorker` / `Heartbeat` / `ClaimTask` 等 RPC，让 worker 在内网走 mTLS 调用
4. **Lifecycle worker 接入** — 读 `lifecycle_policy` 表 + 投放 Task；让 ttl/compaction 真正生效（端到端业务闭环）
5. **`scripts/bootstrap-mysql.sh`** — 一键 DDL + 用户搭建本地环境

## 11. 提交信息

```
feat(scheduler): add worker registry + at-most-once task dispatch
```
