# LCP — LanceDB Control Plane

> 管控服务 for the Lance Lakehouse platform

LCP 是 LanceDB 的管控面服务，负责元数据管理、任务调度、向量索引生命周期、增量 Embedding 以及多租户安全隔离。采用 FastAPI + gRPC 双协议架构，部署为 2 个 K8s 微服务。

---

## 架构

```
                         ┌─────────────────────────────────┐
                         │        lcp-server (×2)          │
                         │  REST :8080  │  gRPC :50051     │
                         │  (north)     │  (east-west)     │
                         └──────┬───────┴──────┬───────────┘
                                │              │
           ┌────────────────────┼──────────────┼────────────────┐
           │                    │              │                 │
    ┌──────┴──────┐      ┌─────┴─────┐  ┌────┴────┐    ┌──────┴──────┐
    │   MySQL 8   │      │   MinIO   │  │Gravitino│    │Embedding Svc│
    │  (metadata) │      │ (S3 data) │  │(catalog)│    │  (vectors)  │
    └─────────────┘      └───────────┘  └─────────┘    └─────────────┘
                                │
                         ┌──────┴──────────────────────────┐
                         │        lcp-worker (×2)          │
                         │  Executor │ Heartbeat │ Planner │
                         └─────────────────────────────────┘
```

### 两个部署单元

| 服务 | 职责 | K8s 类型 |
|------|------|---------|
| **lcp-server** | REST API + gRPC（同进程双端口） | Deployment + Service |
| **lcp-worker** | 任务执行 + 心跳 + 内嵌 Planner | Deployment |

---

## 功能模块

| 模块 | 说明 | 状态 |
|------|------|------|
| Dataset CRUD | 数据集注册/查询/软删除 | ✅ |
| Task 管理 | 提交/取消/重试/分页查询 | ✅ |
| Scheduler | Worker 注册/心跳/Drain/抢占式派发 | ✅ |
| Index 管理 | 创建/优化/合并/删除 ANN 索引 | ✅ |
| Lifecycle 策略 | TTL 删除 / Compaction / 索引优化 cron | ✅ |
| Vectorization 规则 | Embedding 规则 CRUD + 自动触发 | ✅ |
| Gravitino 集成 | 元数据同步 / schema 发现 / 统计回写 | ✅ |
| 增量索引 | INDEX_BUILD + INDEX_OPTIMIZE + delta 阈值自动触发 | ✅ |
| 增量 Embedding | VECTORIZE executor + planner 自动发射 | ✅ |
| 多租户 RLS | App-layer before_execute hook | ✅ |
| OIDC 认证 | JWT 验证 + JWKs 缓存 + JTI replay 防御 | ✅ |
| mTLS | gRPC 证书验证 + SPIFFE URI SAN | ✅ |

---

## 快速开始

### 前置条件

- Python 3.10+
- MySQL 8.0（或 SQLite 内存模式跑测试）
- MinIO（S3 兼容对象存储，lance 数据面）
- Docker + Kubernetes（部署用）

### 本地开发

```bash
# 安装依赖
pip install -e ".[dev]"

# 运行单元测试 (不需要外部服务)
PYTHONPATH=src pytest tests/ --ignore=tests/integration -q
# 211 passed

# 启动统一服务 (REST + gRPC)
export LCP_DB_DSN="mysql+aiomysql://lcp:lcp@127.0.0.1:3306/lcp"
lcp-server --insecure --log-level DEBUG

# 或分别启动
lcp-rest   # REST only :8080
lcp-grpc   # gRPC only :50051
```

### K8s 部署

```bash
# 1. 构建镜像
docker build -t lcp:dev .

# 2. 部署
kubectl apply -f deploy/k8s/00-namespace-config.yaml
kubectl apply -f deploy/k8s/10-api-deployment.yaml
kubectl apply -f deploy/k8s/20-worker-deployment.yaml

# 3. 验证
kubectl -n lcp get pods
kubectl -n lcp port-forward svc/lcp-server 8080:8080
curl http://127.0.0.1:8080/healthz
```

### E2E 测试

```bash
# 需要 MinIO (30900) + MySQL (30306) 已在 K8s 中运行
./scripts/deploy_and_test.sh

# 或单独运行测试脚本
PYTHONPATH=src python scripts/e2e_full_test.py
```

---

## 项目结构

```
src/lcp/
├── api/
│   ├── rest/                    # FastAPI REST API
│   │   ├── main.py              # App factory + lifespan
│   │   ├── auth.py              # OIDC pure-ASGI middleware
│   │   ├── deps.py              # FastAPI 依赖注入
│   │   └── routers/             # 路由: datasets, tasks, indexes, lifecycle, vectorization, meta
│   ├── grpc/                    # gRPC server + mTLS interceptor
│   │   ├── server.py            # MtlsTenantInterceptor + bootstrap
│   │   └── services/            # gRPC servicer stubs
│   └── unified_server.py       # REST + gRPC 合并进程入口
├── core/
│   ├── config.py                # Pydantic Settings (env-based)
│   ├── security.py              # JWT 验证 / JWKs / SPIFFE / JTI denylist
│   ├── tenant.py                # ContextVar tenant propagation
│   └── time.py                  # utcnow_naive() 统一时间
├── db/
│   ├── models.py                # SQLAlchemy ORM (7 表)
│   ├── rls.py                   # App-layer Row-Level Security
│   └── session.py               # Async engine + session factory
├── data_plane/
│   └── lance_io.py              # lance SDK 薄封装 (open/delete/compact/index)
├── integrations/
│   └── gravitino/
│       └── client.py            # Gravitino REST API 异步客户端
├── services/
│   ├── dataset_service.py       # Dataset CRUD
│   ├── task_service.py          # Task 状态机
│   ├── scheduler_service.py     # Worker 调度原语
│   ├── index_service.py         # Index 生命周期
│   ├── lifecycle_service.py     # Lifecycle policy CRUD
│   ├── lifecycle_planner_service.py  # Planner: 策略 → 任务
│   ├── vectorization_service.py # Vectorization rule CRUD
│   └── meta_sync_service.py     # Gravitino 元数据同步
├── schemas/                     # Pydantic request/response models
└── workers/
    ├── lifecycle_worker.py      # Worker daemon (executor + heartbeat + planner)
    ├── lifecycle_worker_service.py  # run_iteration() 单次循环
    └── executors/
        ├── base.py              # ABC + registry
        ├── ttl_delete.py        # TTL_DELETE
        ├── compaction.py        # COMPACTION
        ├── index_build.py       # INDEX_BUILD
        ├── index_optimize.py    # INDEX_OPTIMIZE
        └── vectorize.py         # VECTORIZE (增量 embedding)

deploy/k8s/
├── 00-namespace-config.yaml     # Namespace + ConfigMap + Secret + PDB
├── 10-api-deployment.yaml       # lcp-server Deployment + Service
└── 20-worker-deployment.yaml    # lcp-worker Deployment

scripts/
├── deploy_and_test.sh           # 一键部署+测试
├── e2e_full_test.py             # 7 项 E2E 测试 (24 assertions)
├── local_e2e_smoke.py           # TTL 端到端
├── local_e2e_smoke_index_build.py   # INDEX_BUILD 端到端
├── local_e2e_smoke_index_optimize.py # INDEX_OPTIMIZE 端到端
└── probe_local_env.py           # 基础设施探测

tests/
├── unit/                        # 211 单元测试 (SQLite in-memory)
├── api/                         # REST + gRPC 接口测试
└── integration/                 # 集成测试 (需 MySQL)
```

---

## REST API

| Method | Path | 说明 |
|--------|------|------|
| GET | `/healthz` | 存活探针 |
| GET | `/readyz` | 就绪探针 (DB ping) |
| GET/POST | `/v1/datasets` | 数据集 CRUD |
| GET | `/v1/datasets/{uuid}` | 数据集详情 |
| DELETE | `/v1/datasets/{uuid}` | 软删除 |
| GET/POST | `/v1/tasks` | 任务列表/提交 |
| POST | `/v1/tasks/{uuid}/cancel` | 取消 |
| POST | `/v1/tasks/{uuid}/retry` | 重试 |
| GET/POST | `/v1/datasets/{uuid}/indexes` | 索引管理 |
| POST | `/v1/datasets/{uuid}/indexes/{name}/optimize` | 触发优化 |
| GET/POST | `/v1/datasets/{uuid}/lifecycle-policies` | 生命周期策略 |
| GET/POST | `/v1/datasets/{uuid}/vectorization-rules` | 向量化规则 |
| POST | `/v1/meta/sync` | Gravitino 元数据同步 |
| GET | `/v1/meta/schemas` | Gravitino schema 列表 |
| POST | `/v1/meta/datasets/{uuid}/write-back` | 统计回写 |

---

## 任务类型 (Executors)

| Task Type | 说明 | 触发方式 |
|-----------|------|---------|
| `TTL_DELETE` | 按 TTL 删除过期行 | lifecycle policy (ttl_days) |
| `COMPACTION` | 合并 lance 碎片文件 | lifecycle policy (compaction_threshold) |
| `INDEX_BUILD` | 构建 ANN 索引 | create_index API |
| `INDEX_OPTIMIZE` | 增量合并 delta 索引 | optimize_index API / cron / delta 阈值 |
| `VECTORIZE` | 增量计算 embedding | vectorization rule (enabled) |

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LCP_DB_DSN` | `mysql+aiomysql://lcp:lcp@127.0.0.1:3306/lcp` | 数据库 |
| `LCP_REST_HOST` | `127.0.0.1` | REST 绑定 |
| `LCP_REST_PORT` | `8080` | REST 端口 |
| `LCP_GRPC_HOST` | `127.0.0.1` | gRPC 绑定 |
| `LCP_GRPC_PORT` | `50051` | gRPC 端口 |
| `LCP_LANCE_STORAGE_ENDPOINT` | (空) | MinIO/S3 地址 |
| `LCP_LANCE_STORAGE_ACCESS_KEY` | (空) | S3 Key |
| `LCP_LANCE_STORAGE_SECRET_KEY` | (空) | S3 Secret |
| `LCP_GRAVITINO_URL` | (空) | Gravitino 地址（空=禁用） |
| `LCP_GRAVITINO_METALAKE` | `default` | metalake |
| `LCP_GRAVITINO_CATALOG` | `lance` | catalog |
| `LCP_EMBEDDING_DEFAULT_ENDPOINT` | (空) | 默认 embedding 服务地址 |
| `LCP_ENFORCE_TENANT_RLS` | `true` | 是否启用 RLS |
| `LCP_OIDC_ISSUER` | `https://idp.example.com` | OIDC 发行方 |
| `LCP_OIDC_AUDIENCE` | `lcp-api` | JWT audience |

---

## 认证

| 方向 | 协议 | 认证方式 | 说明 |
|------|------|---------|------|
| 用户 → LCP | REST | OIDC JWT | `Authorization: Bearer <token>` |
| Worker ↔ LCP | gRPC | mTLS | x509 CN=worker_id, O=tenant_id |
| 内部调度 | in-process | system principal | `with_system_context()` bypass RLS |

---

## 多租户隔离

- **App-layer RLS**: SQLAlchemy `before_execute` hook 自动注入 `WHERE tenant_id = ?`
- 保护表: `dataset`, `task`
- 子表（`vector_index`, `lifecycle_policy`, `vectorization_rule`）通过父表 dataset lookup 间接隔离
- System principal (`is_system=True`) 可跨租户操作（scheduler/planner/reaper）

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev,lance]"

# 运行测试
PYTHONPATH=src pytest tests/ --ignore=tests/integration -q

# 类型检查
mypy src/lcp --ignore-missing-imports

# Lint
ruff check src/ tests/
```

---

## License

Apache-2.0
