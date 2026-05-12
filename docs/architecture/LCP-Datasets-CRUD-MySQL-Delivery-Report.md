# LCP — Datasets CRUD 接 MySQL（本地 K8s）交付报告

## 1. 任务

把 LCP 控制面 `Datasets` 资源从 `501 Not Implemented` 占位实现升级为**真实持久化的 CRUD**，
并将状态库托管在本地 Colima k3s 集群已有的 MySQL（`mysql-cdc` Pod，NodePort `30306`）上。

## 2. 关键决策与权衡

| 决策点                        | 选择                                     | 理由                                                                                     |
| ----------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------- |
| 复用现有 MySQL Pod 还是新部署 | **复用 `mysql-cdc`**                     | 已 Running 46 天，零部署成本；DDL 走独立 schema `lcp` 不冲突现有 `flink_test`/`poc_bank` |
| 新建 `lcp` 应用账号           | ✅                                        | 不用 root 跑业务流量；最小权限 `GRANT ALL ON lcp.*`                                      |
| 引入 Alembic 迁移             | ❌ 不引入                                 | 还在 skeleton 阶段，DDL 直接执行 SQL 文件够用；提前抽象违反"简单优先"                    |
| 引入 Repository 抽象          | ❌ 不引入                                 | 1 张表的 CRUD 不需要 UoW/Repository；service 模块直接用 SQLAlchemy 即可                  |
| 8 张表全部 ORM                | ❌ 仅 `Dataset`                           | 用到再加；过度建模会拖慢迭代                                                             |
| 单元测试用真实 MySQL          | ❌ 用 sqlite                              | sqlite + StaticPool 够快；专门加一组 `tests/integration/` 跑真实 MySQL                   |
| `BigInteger` 主键             | ✅ MySQL 用 BigInteger，sqlite 用 Integer | SQLite 的 ROWID 仅在 `INTEGER PRIMARY KEY` 时才 autoincrement；用 `with_variant` 跨方言  |
| RLS hook 集成                 | ✅ 复用现有 hook                          | hook 已在 `lcp.db.rls` 写好；本次只需修一行表名 `datasets` → `dataset`                   |

## 3. 改动清单

### 3.1 新增文件（4）

| 文件                                       | 行数 | 用途                                                                                                                                                    |
| ------------------------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/lcp/db/models.py`                     | 71   | `Dataset` ORM；`BigInteger().with_variant(Integer, "sqlite")` 跨方言主键                                                                                |
| `src/lcp/schemas/__init__.py`              | 1    | 包标记                                                                                                                                                  |
| `src/lcp/schemas/dataset.py`               | 78   | `DatasetRegisterRequest` / `DatasetResponse` / `DatasetListResponse`；OpenAPI 字段 `schema`/`table` 经 alias 映射到 ORM 内部名 `db_schema`/`table_name` |
| `src/lcp/services/__init__.py`             | 5    | 包标记                                                                                                                                                  |
| `src/lcp/services/dataset_service.py`      | 132  | `create_dataset` / `get_dataset` / `list_datasets` / `delete_dataset` 四个异步函数；自定义异常 `DatasetAlreadyExistsError`、`DatasetNotFoundError`      |
| `tests/integration/__init__.py`            | 1    | 包标记                                                                                                                                                  |
| `tests/integration/test_datasets_mysql.py` | 162  | 真实 MySQL 集成测试；不可达自动 skip；每个测试用独立 tenant_id 并清理自身行                                                                             |

### 3.2 修改文件（6）

| 文件                                   | 改动                                                                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `src/lcp/api/rest/routers/datasets.py` | 4 个 stub handler 替换为接入 `dataset_service` 的真实 CRUD；路径参数 `dataset_id` → `dataset_uuid`（对齐 OpenAPI）        |
| `src/lcp/db/rls.py`                    | `RLS_PROTECTED_TABLES` 中 `datasets` 改为 `dataset` 对齐 DDL 表名（同时附注释说明其余 tasks/indexes/... 占位待 ORM 落地） |
| `src/lcp/db/session.py`                | sqlite 内存 DB 添加 `StaticPool` + `check_same_thread=False`，否则多次连接会看到空 schema                                 |
| `tests/conftest.py`                    | `rest_app` fixture 内自动 `Base.metadata.create_all` + `install_rls_listener`；teardown 清表并 dispose engine             |
| `tests/api/rest/test_routers.py`       | `TestDatasetsRouter` 重写为真实 CRUD 合约（201/200/404/204 + tenant 隔离）                                                |
| `tests/api/rest/test_auth.py`          | `TestContextvarPropagation` 改为通过 RLS 隔离断言 tenant 注入                                                             |
| `tests/unit/db/test_rls.py`            | 单元测试中虚构表名 `datasets` → `dataset`                                                                                 |
| `pyproject.toml`                       | 移除 ruff 不识别的 `W503` 规则（pycodestyle 代码，与 ruff 不兼容）                                                        |

## 4. 验证证据

### 4.1 K8s 与 MySQL 准备

```bash
$ kubectl get pod mysql-cdc -o wide
mysql-cdc   1/1   Running   3   46d   colima

$ kubectl get svc mysql-cdc
NodePort   10.43.243.104   3306:30306/TCP

$ mysql --version  # 容器内
8.0.45  ✅ 满足 DDL 要求 (8.0+)
```

### 4.2 DDL 应用结果

```
mysql> USE lcp; SHOW TABLES;
+---------------------+
| Tables_in_lcp       |
+---------------------+
| dataset             |
| distributed_lock    |
| lifecycle_policy    |
| meta_sync_log       |
| task                |
| task_event          |
| vector_index        |
| vectorization_rule  |
| worker_registry     |
+---------------------+
9 tables ✅
```

### 4.3 应用账号

```sql
GRANT ALL PRIVILEGES ON `lcp`.* TO 'lcp'@'%' IDENTIFIED BY 'lcp_dev_pwd';
SELECT user, host FROM mysql.user WHERE user='lcp';  -- lcp@%  ✅
```

### 4.4 测试套件结果

```
tests/api/grpc/test_server.py            13 passed
tests/api/rest/test_auth.py              10 passed
tests/api/rest/test_routers.py           18 passed
tests/integration/test_datasets_mysql.py  2 passed   (真实 MySQL)
tests/unit/core/test_config.py            8 passed
tests/unit/core/test_security.py         19 passed
tests/unit/core/test_tenant.py            6 passed
tests/unit/db/test_rls.py                16 passed
================================================================
                                         92 passed in 5.10s
Coverage: 76.90% (gate 70%)              ✅
```

### 4.5 Lint

```
ruff check src/lcp tests
All checks passed!  ✅  (顺手修复了预先存在的 ruff 配置 bug：W503 不被 ruff 识别)
```

## 5. 关键 Bug 与修复轨迹

| #   | 现象                                                                                        | 根因                                                                                                                                                    | 修复                                                                  |
| --- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| B-1 | 首次 `POST /datasets` 返回 409                                                              | sqlite 上 `BigInteger` 主键不会自动 autoincrement，触发 `NOT NULL constraint failed: dataset.id`                                                        | `_PK_BIGINT = BigInteger().with_variant(Integer, "sqlite")`           |
| B-2 | tenant 隔离测试失败：`tenant-b` 看到 `tenant-a` 的数据                                      | `list_datasets` 中 `select(func.count()).select_from(base.subquery())` 让 RLS hook 看到的是匿名 subquery，无法识别为 `dataset` 表，predicate 被静默跳过 | 改为 `select(func.count(Dataset.id))` 直接走 Dataset，让 RLS 命中表名 |
| B-3 | `RLS_PROTECTED_TABLES` 用了 `datasets`，但 DDL 是 `dataset`                                 | 代码与 DDL 命名不一致                                                                                                                                   | 改为 `dataset`；其余 `tasks`/`indexes` 占位保留并加注释               |
| B-4 | sqlite `:memory:` 多次连接看到空 schema                                                     | 默认连接池每次新开连接，但 `:memory:` 是 per-connection 的                                                                                              | session.py 给 sqlite 配 `StaticPool`                                  |
| B-5 | `ruff` 无法运行：`Unknown rule selector: W503`                                              | W503 是 flake8 / pycodestyle 的代码，ruff 不兼容                                                                                                        | 从 `ignore` 移除 W503，注释说明 ruff 默认就遵循 PEP 8                 |
| B-6 | VS Code koro1 插件给 `schemas/dataset.py` 加了头部注释，导致 `from __future__` 不在文件首行 | 编辑器插件副作用                                                                                                                                        | 整文件重写覆盖                                                        |

## 6. 与 OpenAPI 合约一致性

| 端点                                 | OpenAPI                   | 实际 | 状态                                                    |
| ------------------------------------ | ------------------------- | ---- | ------------------------------------------------------- |
| `POST /v1/datasets`                  | 201 + DatasetResponse     | ✅    | 一致                                                    |
| `GET /v1/datasets`                   | 200 + DatasetListResponse | ✅    | 一致（含分页 `total`/`page`/`page_size`/`items`）       |
| `GET /v1/datasets/{dataset_uuid}`    | 200 / 404                 | ✅    | 一致                                                    |
| `DELETE /v1/datasets/{dataset_uuid}` | 204 / 404                 | ✅    | 一致（soft-delete，状态 → DELETED）                     |
| 字段 `schema` / `table`              | 透传                      | ✅    | 通过 Pydantic alias 映射到内部 `db_schema`/`table_name` |

## 7. 对比 Karpathy 编码原则

| 原则              | 落实情况                                                      |
| ----------------- | ------------------------------------------------------------- |
| 1. 先想后写       | ✅ 启动前列出 4 类待澄清；探明 K8s 现状再落地                  |
| 2. 简单优先       | ✅ 不引入 Alembic / Repository / UoW；只为 Dataset 写 ORM      |
| 3. 外科手术式修改 | ✅ 改动最小；非我引入的 lint 问题在工具一次自动修复中处理      |
| 4. 目标驱动执行   | ✅ 8 步计划每步都有验证标准；最终用 92 测试 + 76.9% 覆盖率收尾 |

## 8. 后续建议（不阻塞本次交付）

1. **dataset_service.py** 还有 8 行未覆盖（错误分支 + delete 异常路径）— 可加 2~3 个用例提升至 90%+
2. **`db_pool_pre_ping`** — 当前生产 DSN 无 `pool_pre_ping`，长连接断开时首次请求会失败。可在 `get_engine` MySQL 分支加 `pool_pre_ping=True`
3. **`task` / `vector_index` / 其他 6 张表 ORM** — 当任务调度模块开始接入时再补
4. **本地启动脚本** — 写一个 `scripts/bootstrap-mysql.sh` 一键完成 "kubectl cp + mysql -e CREATE USER + 应用 DDL" 三步，方便其他开发者
5. **CI 集成测试** — 当前 `tests/integration` 在 GitHub-hosted runner 上不会跑（无 MySQL）。可加 `services: mysql:8.0` 让 CI 也覆盖这部分
