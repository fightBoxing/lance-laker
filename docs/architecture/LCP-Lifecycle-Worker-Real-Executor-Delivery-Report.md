# LCP — Lifecycle Worker (Real Executor) 交付报告

## 1. 任务

把上一轮的 demo worker 升级为**真实 worker**：能从 task 表抢任务，路由到对应 executor，执行完毕后写回 SUCCEEDED/FAILED；并提供生产形态的 CLI 入口 `python -m lcp.workers.lifecycle_worker`。

## 2. 关键决策（动手前已暴露）

### 2.1 范围澄清 — 项目缺失数据 IO 层

研究阶段发现关键事实（[Karpathy 先想后写]）：

- `pyproject.toml` 没有 `pylance` / lance 依赖
- 项目从未真正打开过 lance 数据集（dataset 行只是元数据）
- `index_service.optimize_index` 存在但只翻状态机，注释明确写着"Real optimisation work happens asynchronously; this method only flips the state"

→ 用户描述的"调 lance 删分区/合并/调 `index_service.optimize`"暗含一个不存在的接口前提。**直接动手会写出 200+ 行推测性代码**。

### 2.2 暴露三种解读 + 用户决策

向用户暴露：

| 解读                                              | 选择                     |
| ------------------------------------------------- | ------------------------ |
| A. 完整 lance 集成（引入 pylance、真做事）        | ❌ 范围爆炸，是独立子工程 |
| **B. 可插拔 Executor 抽象 + INDEX_OPTIMIZE 真做** | **✅ 用户确认**           |
| C. 全 stub                                        | ❌ 价值低                 |

→ **B 方案胜出**：`LifecycleExecutor` ABC + 三个具体实现。TTL_DELETE / COMPACTION 是 stub（记录"如果有 lance 会调什么"），INDEX_OPTIMIZE **真做** LCP 控制面内的 OPTIMIZING → READY 状态机翻转。

### 2.3 Worker 形态 — A 方案（用户确认）

`python -m lcp.workers.lifecycle_worker` 真实 CLI 入口：
- asyncio 主循环
- SIGINT/SIGTERM 优雅退出
- `--max-iterations` 让 k8s Job / 烟测有界
- `--task-type` 让 worker 专精化（一种 task 类型一个 pod）
- 不做 metrics / heartbeat refresh（后续轮次）

### 2.4 INDEX_OPTIMIZE 真做事的范围

发现 gap：`index_service` 暴露了 `optimize_index`（READY→OPTIMIZING）但**没有** `finish_optimize_index`（OPTIMIZING→READY）。两个选择：

| 选择                                             | 决策                                                  |
| ------------------------------------------------ | ----------------------------------------------------- |
| A. 在 index_service 里新加 finish_optimize_index | ❌ 会扩散到 router/schemas，扩大本期范围               |
| **B. 在 IndexOptimizeExecutor 内联 UPDATE**      | ✅ [Karpathy 外科手术式]，留 TODO 注释指向未来重构路径 |

### 2.5 异常路径事务管理

executor 抛异常后必须 `session.rollback()` 清掉半完成的写。但**这个决策**触发了本期唯一的 bug（详见 §6）。

## 3. 改动清单

### 3.1 新增文件（5）

| 文件                                                   | 行数 | 用途                                                     |
| ------------------------------------------------------ | ---- | -------------------------------------------------------- |
| `src/lcp/workers/__init__.py`                          | 6    | 包说明                                                   |
| `src/lcp/workers/lifecycle_executors.py`               | 252  | LifecycleExecutor 抽象 + 三个具体实现 + 默认注册表构建器 |
| `src/lcp/workers/lifecycle_worker_service.py`          | 226  | run_iteration 原语：claim/dispatch/finalize 状态机       |
| `src/lcp/workers/lifecycle_worker.py`                  | 249  | CLI 入口：argparse + asyncio loop + 信号处理             |
| `tests/unit/workers/__init__.py`                       | 1    | 测试包                                                   |
| `tests/unit/workers/test_lifecycle_executors.py`       | 285  | 9 个单元测试（每个 executor + registry）                 |
| `tests/unit/workers/test_lifecycle_worker_service.py`  | 232  | 5 个单元测试（happy + 4 个失败路径）                     |
| `tests/integration/test_lifecycle_worker_e2e_mysql.py` | 287  | 3 个真实 MySQL E2E 测试                                  |

### 3.2 修改文件（0）

严格 [Karpathy 外科手术式]：本轮零修改既有代码。所有改动局限在新文件。

## 4. 端到端架构（更新版）

```
┌─────────────────────────────────────────────────────────────┐
│  USER (REST API)                                            │
│    POST /v1/datasets/{}/lifecycle-policies                  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  PLANNER (cron / k8s CronJob)        [上一轮]               │
│    plan_once() → 投放 task                                  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
                  task table (PENDING)
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  WORKER  python -m lcp.workers.lifecycle_worker  [本轮]     │
│    while not shutdown:                                      │
│        outcome = run_iteration(session, config, registry)   │
│            ├─ claim_next_task (FOR UPDATE SKIP LOCKED)      │
│            ├─ load Dataset                                  │
│            ├─ dispatch to LifecycleExecutor                 │
│            │     ├── TtlDeleteExecutor       (stub)         │
│            │     ├── CompactionExecutor      (stub)         │
│            │     └── IndexOptimizeExecutor  (REAL: 翻状态机)│
│            ├─ complete_task / fail_task                     │
│        sleep(idle | busy)                                   │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
                  task table (SUCCEEDED / FAILED)
                  vector_index (OPTIMIZING → READY)  ← 真改写！
```

## 5. 验证证据

### 5.1 完整测试结果

```
tests/api/grpc/test_server.py                            13 passed
tests/api/rest/test_auth.py                              10 passed
tests/api/rest/test_routers.py                           45 passed
tests/integration/test_datasets_mysql.py                  2 passed
tests/integration/test_indexes_mysql.py                   3 passed
tests/integration/test_lifecycle_e2e_mysql.py             4 passed
tests/integration/test_lifecycle_mysql.py                 3 passed
tests/integration/test_lifecycle_worker_e2e_mysql.py      3 passed   (新)
tests/integration/test_scheduler_mysql.py                 4 passed
tests/integration/test_tasks_mysql.py                     3 passed
tests/integration/test_vectorization_mysql.py             3 passed
tests/unit/core/test_config.py                            8 passed
tests/unit/core/test_security.py                         19 passed
tests/unit/core/test_tenant.py                            6 passed
tests/unit/db/test_rls.py                                16 passed
tests/unit/db/test_rls_system.py                          4 passed
tests/unit/services/test_lifecycle_planner_service.py     9 passed
tests/unit/services/test_scheduler_service.py            15 passed
tests/unit/workers/test_lifecycle_executors.py            9 passed   (新)
tests/unit/workers/test_lifecycle_worker_service.py       5 passed   (新)
=================================================================
                                                       184 passed in 10.00s
Coverage: 77.88% (gate 70%)                              ✅
```

### 5.2 覆盖率（本轮新增）

| 模块                                               | 覆盖率    |
| -------------------------------------------------- | --------- |
| `src/lcp/workers/lifecycle_executors.py`           | **100%**  |
| `src/lcp/workers/lifecycle_worker_service.py`      | **96.4%** |
| `src/lcp/workers/lifecycle_worker.py` (CLI daemon) | 0%*       |

\* CLI daemon 0% 是合理的工程权衡：mock asyncio 主循环 + 信号 handler 单元测试成本极高、价值低；用**真实 CLI 命令在 MySQL 上跑通**（见 §5.4）作为替代证据。

整体覆盖率 80.28% → 77.88% 的下降全部来自这 77 行 0% 覆盖的 CLI 代码。

### 5.3 真做事的硬证据 — INDEX_OPTIMIZE 在 MySQL 上翻状态机

```python
# tests/integration/test_lifecycle_worker_e2e_mysql.py
async def test_index_optimize_real_state_machine_on_mysql(...):
    # 1. Pre-create OPTIMIZING vector_index row
    idx = Index(..., status="OPTIMIZING")
    
    # 2. Plan task
    tick = await lifecycle_planner_service.plan_once(mysql_session)
    
    # 3. Real worker drains it
    outcome = await run_iteration(...)
    assert outcome.final_status == "SUCCEEDED"
    
    # 4. State machine truly flipped on MySQL
    await mysql_session.refresh(idx)
    assert idx.status == "READY"               # ← 真改了！
    assert idx.last_optimized_at is not None   # ← 真改了！
```

→ 这是项目首次有"真做事"的执行器在真 MySQL 上跑通。

### 5.4 CLI 真实启动证据

```bash
$ LCP_DB_DSN="mysql+aiomysql://..." python -m lcp.workers.lifecycle_worker \
    --max-iterations 2 --idle-seconds 0.3

INFO worker ROCKYYIN-MB0-3e549624 registered (lease=18b79645-..., task_type=ANY)
INFO max-iterations=2 reached; exiting
INFO worker exited cleanly after 2 iterations
```

→ 生产形态的 worker 真的能起来、能注册、能优雅退出。

### 5.5 Lint

```
$ ruff check src/lcp tests
All checks passed!  ✅
```

## 6. Bug 跟踪

### Bug #1 — `MissingGreenlet` on executor exception path

**症状**：`test_executor_exception_translates_to_fail_task` 在抛 `RuntimeError` 后，访问 `task.task_uuid` 触发 `sqlalchemy.exc.MissingGreenlet`。

**根因**：`session.rollback()` **expires** 所有 ORM 实例的属性。即使 rollback 异常被吞掉，访问已-expired 的 `task.task_uuid` 触发 lazy SELECT；aiosqlite 在 except 处理路径里跑这个 SELECT 触发 `MissingGreenlet`。

**修复**：在 try 块的 except 分支**最早**处先快照需要的字段到本地变量，再调 rollback：

```python
except Exception as exc:
    # MUST snapshot BEFORE rollback (rollback expires attributes).
    task_uuid = task.task_uuid
    task_type = task.task_type
    try:
        await session.rollback()
    except Exception:  # rollback failure must never mask original exc
        pass
    await scheduler_service.fail_task(...)
```

**学习**：这是个微妙但重要的 sqlalchemy + async 模式陷阱。注释里把根因和修复理由写清楚，避免未来"清理"时被改回去。

→ [Karpathy 系统调试]：每行修复都能直接追溯到根因。

## 7. 与上一轮的关系

| 维度       | 上一轮 (Lifecycle Planner) | 本轮 (Lifecycle Worker)                     |
| ---------- | -------------------------- | ------------------------------------------- |
| 角色       | 元数据 → task 投放         | task → 真改控制面状态                       |
| 真做事程度 | N/A（投放方）              | **INDEX_OPTIMIZE 真改 vector_index 状态机** |
| 部署形态   | service 函数（CronJob 调） | **CLI daemon**（k8s Deployment）            |
| Bug 数     | 0                          | 1（snapshot before rollback）               |
| 集成测试   | 4                          | 3（含真做事的状态机断言）                   |

## 8. 项目整体状态

| 资源                                               | ORM     | Service | Router  | OpenAPI | 集成测试 |
| -------------------------------------------------- | ------- | ------- | ------- | ------- | -------- |
| Dataset / Task / Index / Lifecycle / Vectorization | ✅       | ✅       | ✅       | ✅       | ✅        |
| WorkerRegistry                                     | ✅       | ✅       | N/A     | N/A     | ✅        |
| LifecyclePlanner                                   | ✅       | ✅       | N/A     | N/A     | ✅        |
| **LifecycleWorker**                                | **N/A** | **✅**   | **N/A** | **N/A** | **✅**    |

DDL 8 张表 6/8 = 75%；**业务能力**：从"元数据可写 + 调度可发"升级到"**worker 真在做事**"。

## 9. Karpathy 编码原则自审

| 原则              | 落实情况                                                                                                                            |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 1. 先想后写       | ✅ 发现"项目无 lance 依赖"的关键事实后**立即停下**，向用户暴露 3 种解读 + 2 个部署形态选项；不做推测                                 |
| 2. 简单优先       | ✅ 不引入 pylance；不新建 `finish_optimize_index` 函数；CLI 不做 metrics / heartbeat；INDEX_OPTIMIZE 内联 UPDATE 而非新 service 方法 |
| 3. 外科手术式修改 | ✅ **零修改既有代码**；新增 5 个文件全在 `lcp/workers/` 命名空间下                                                                   |
| 4. 目标驱动执行   | ✅ 6 步计划逐步验证；每个 executor 单元测试 + 整体单元测试 + 真实 MySQL E2E 三层验证                                                 |

## 10. 下一步建议

1. **真 lance 集成 — 独立子工程**：引入 `pylance`，把 TtlDeleteExecutor / CompactionExecutor 升级为真做事；这一步会涉及 S3/GCS 凭据、真 lance 数据集、新一组集成测试 — 是单独一轮（甚至多轮）的工作量
2. **`task_event` 表**：状态变更审计 trail；让 worker / scheduler / planner 协同写入
3. **Worker heartbeat 刷新**：当前注册时只盖一次 lease；接入 reaper 前，需要 worker 周期性刷新 `last_heartbeat_at`
4. **k8s Deployment YAML**：把 planner CronJob 和 worker Deployment 写成可部署清单
5. **专精化 worker pods**：每种 task type 一个 Deployment（用 `--task-type` 参数）防止慢 task 阻塞快 task

## 11. 提交信息

```
feat(workers): real lifecycle worker with executor abstraction
```
