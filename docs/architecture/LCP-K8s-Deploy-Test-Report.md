# LCP K8s 部署与功能测试报告

> 日期：2026-05-16  
> 分支：`fix/code-review-optimizations` → 合并至 `master`  
> 集群：Colima (单节点 k3s)  
> 镜像：`lcp:dev`

---

## 1. 架构总览

### 服务拓扑（2 个 Deployment）

```
Namespace: lcp
│
├── Deployment: lcp-server (replicas=2)
│   ├── REST API   :8080  (FastAPI/Uvicorn, north-bound)
│   ├── gRPC       :50051 (grpc.aio + mTLS, east-west)
│   └── 共享: 1 DB engine, 1 RLS listener, 1 连接池
│
├── Deployment: lcp-worker (replicas=2)
│   ├── Task executor  (claim → execute → complete)
│   ├── Heartbeat loop (10s)
│   └── Planner loop   (60s, 替代独立 CronJob)
│
├── Service: lcp-server
│   ├── http:  8080 → NodePort 30808
│   └── grpc: 50051 → NodePort 31508
│
├── PodDisruptionBudget: lcp-server-pdb  (minAvailable=1)
├── PodDisruptionBudget: lcp-worker-pdb  (minAvailable=1)
├── ConfigMap: lcp-config
└── Secret: lcp-secrets
```

### 技术栈

| 组件 | 技术 |
|------|------|
| REST API | FastAPI + Uvicorn (pure-ASGI middleware) |
| gRPC | grpc.aio + mTLS interceptor |
| 数据库 | MySQL 8.0 (aiomysql async driver) |
| 对象存储 | MinIO (S3 兼容) |
| 数据格式 | Lance (columnar, 支持 ANN 索引) |
| ORM | SQLAlchemy 2.0 async |
| 多租户 | App-layer RLS (before_execute hook) |
| 认证 | OIDC JWT (REST) + mTLS (gRPC) |

---

## 2. 部署步骤

### 2.1 前置条件

```bash
# Colima 运行中
colima status

# MinIO on NodePort 30900
curl -s http://127.0.0.1:30900/minio/health/live

# MySQL on NodePort 30306
mysql -h 127.0.0.1 -P 30306 -u lcp -plcp_dev_pwd -e "SELECT 1"
```

### 2.2 构建镜像

```bash
docker build -t lcp:dev .
```

Dockerfile 使用 `python:3.12-slim`，最终 CMD 为 `lcp-server --insecure`。

### 2.3 部署到 K8s

```bash
# 清理旧资源（首次部署可跳过）
kubectl -n lcp delete deployment lcp-api --ignore-not-found
kubectl -n lcp delete cronjob lcp-planner --ignore-not-found
kubectl -n lcp delete service lcp-api --ignore-not-found

# 部署新架构
kubectl apply -f deploy/k8s/00-namespace-config.yaml
kubectl apply -f deploy/k8s/10-api-deployment.yaml
kubectl apply -f deploy/k8s/20-worker-deployment.yaml

# 等待就绪
kubectl -n lcp rollout status deployment/lcp-server --timeout=120s
kubectl -n lcp rollout status deployment/lcp-worker --timeout=120s
```

### 2.4 验证部署

```bash
# Pod 状态
kubectl -n lcp get pods
# 期望: lcp-server × 2 + lcp-worker × 2, 全部 Running

# 健康检查 (需 port-forward 或集群内访问)
kubectl -n lcp port-forward svc/lcp-server 30808:8080 &
curl http://127.0.0.1:30808/healthz
# {"status":"ok","version":"0.1.0","env":"dev-k8s"}

curl http://127.0.0.1:30808/readyz
# {"status":"ready"}  (包含真实 DB ping)
```

---

## 3. K8s Manifest 说明

### 3.1 `00-namespace-config.yaml`

| 资源 | 说明 |
|------|------|
| Namespace `lcp` | 隔离 RBAC / 网络策略 |
| ConfigMap `lcp-config` | DB DSN, REST/gRPC host:port, 存储配置 |
| Secret `lcp-secrets` | MinIO 凭证 |
| PDB `lcp-server-pdb` | minAvailable=1, 保护滚动更新 |
| PDB `lcp-worker-pdb` | minAvailable=1 |

### 3.2 `10-api-deployment.yaml`

| 配置 | 值 | 说明 |
|------|---|------|
| replicas | 2 | 消除单点故障 |
| strategy | RollingUpdate, maxUnavailable=0 | 零停机部署 |
| podAntiAffinity | preferred, hostname | 跨节点分散 |
| startupProbe | /healthz, 12×5s | 60s 启动窗口 |
| readinessProbe | /readyz, 10s | 含 DB ping |
| livenessProbe | /healthz, 30s | 轻量存活检测 |
| ports | 8080 (http) + 50051 (grpc) | 双端口 |

### 3.3 `20-worker-deployment.yaml`

| 配置 | 值 | 说明 |
|------|---|------|
| replicas | 2 | 高可用 |
| terminationGracePeriodSeconds | 120 | 慢任务排水 |
| args | `--planner-seconds=60` | 内嵌 planner 每分钟一次 |
| 无 HTTP 探针 | — | 轮询 daemon, 依赖心跳 |

---

## 4. 功能测试报告

### 4.1 测试环境

```
集群:    Colima k3s (单节点)
MySQL:   127.0.0.1:30306  (lcp/lcp_dev_pwd)
MinIO:   127.0.0.1:30900  (minioadmin/minioadmin, bucket: lcp-lance)
测试脚本: scripts/e2e_full_test.py
```

### 4.2 测试结果：24 passed, 0 failed ✅

#### Test 1: LanceDB 表访问

| 步骤 | 结果 |
|------|------|
| 写入 4 行数据到 MinIO (`s3://lcp-lance/smoke/tbl_*.lance`) | ✅ |
| 通过 service 层注册 dataset 到 MySQL | ✅ |
| 通过 lance SDK 读回数据，验证 row_count=4 | ✅ |

**验证**: 本地脚本可以通过 LCP service 层注册 lance 表，并直接通过 lance SDK 访问 MinIO 上的数据。

#### Test 2: Gravitino Meta Sync 端点

| 步骤 | 结果 |
|------|------|
| POST /v1/meta/sync 端点存在 | ✅ |
| 无 Gravitino 服务时返回 503 (GRAVITINO_DISABLED) | ✅ (预期行为) |

**说明**: Gravitino 集成代码已就绪，配置 `LCP_GRAVITINO_URL` 即可激活。

#### Test 3: TTL 删除（生命周期管理）

| 步骤 | 结果 |
|------|------|
| 创建 TTL 策略 (ttl_days=1) | ✅ |
| Planner tick: 扫描策略 → 发射 TTL_DELETE 任务 | ✅ emitted=1 |
| Worker 执行: claim → lance delete_rows → SUCCEEDED | ✅ |
| 验证: old 行已删除, young 行存活 | ✅ `['young_c', 'young_d']` |

**数据流**:
```
lifecycle_policy (ttl_days=1)
  → planner tick → emit PENDING task
    → worker claim → TTL_DELETE executor
      → lance.delete_rows(predicate="created_at < cutoff")
        → 2 old rows deleted, 2 young rows survive
```

#### Test 4: 增量索引 — INDEX_BUILD

| 步骤 | 结果 |
|------|------|
| 写入 256 行 × 64 维向量到 MinIO | ✅ |
| create_index(IVF_PQ) → 状态 BUILDING → 自动发射 INDEX_BUILD 任务 | ✅ |
| Worker 执行: lance.create_index(IVF_PQ) → SUCCEEDED | ✅ |
| 验证: ds.list_indices() 返回 1 个索引 | ✅ |

**数据流**:
```
POST create_index → Index row (BUILDING) + Task row (PENDING)
  → worker claim → IndexBuildExecutor
    → lance.create_index(column="embedding", type="IVF_PQ")
      → Index row → READY
```

#### Test 5: 增量索引 — INDEX_OPTIMIZE

| 步骤 | 结果 |
|------|------|
| 追加 64 行新数据到已索引的 dataset | ✅ (创建 delta) |
| optimize_index → 状态 OPTIMIZING → 发射 INDEX_OPTIMIZE 任务 | ✅ |
| Worker 执行: lance.optimize_indices() → SUCCEEDED | ✅ |

**数据流**:
```
append 64 rows → lance delta created
  → optimize_index() → Index row (OPTIMIZING) + Task (PENDING)
    → worker → IndexOptimizeExecutor
      → lance.optimize_indices() (merge deltas)
        → Index row → READY
```

**自动触发**: Planner 还支持 delta 阈值自动检测（`delta_count >= 10` 时自动发射 INDEX_OPTIMIZE），无需手动触发。

#### Test 6: 增量 Embedding — VECTORIZE

| 步骤 | 结果 |
|------|------|
| 写入 3 行文本数据（无 vector 列） | ✅ |
| 创建 VectorizationRule (target=vector, source=text) | ✅ |
| Planner 发现 enabled rule → 发射 VECTORIZE 任务 | ✅ emitted=1 |
| Worker 执行 VectorizationExecutor → SUCCEEDED | ✅ |

**数据流**:
```
VectorizationRule (enabled=True, target_column="vector")
  → planner scan → emit VECTORIZE task
    → worker → VectorizationExecutor
      → scan rows where vector IS NULL
        → batch POST to embedding endpoint
          → write vectors back to lance
```

**注意**: 当前测试环境无真实 embedding endpoint，executor 检测到 endpoint 为空后返回 "no endpoint" 结果（SUCCEEDED 但 rows_processed=0）。配置 `LCP_EMBEDDING_DEFAULT_ENDPOINT` 后即可连接真实模型服务。

#### Test 7: Compaction（碎片合并）

| 步骤 | 结果 |
|------|------|
| 创建 compaction 策略 | ✅ |
| Planner 发射 COMPACTION 任务 | ✅ emitted=1 |
| Worker 执行: lance.compact_files() → SUCCEEDED | ✅ |

---

## 5. Worker 日志验证

```log
# Worker 启动
worker lcp-worker-xxx registered (lease=xxx, task_type=ANY)
planner loop started (interval=60s)

# 第一次 planner tick
planner: scanned=3 emitted=2 skipped=0

# 任务自动执行
task 7084c8f6-xxx (TTL_DELETE) -> SUCCEEDED
task b1e8b0ab-xxx (TTL_DELETE) -> SUCCEEDED

# 第二次 planner tick (幂等去重)
planner: scanned=3 emitted=0 skipped=2
```

**验证**:
- ✅ Worker 注册 + 心跳正常
- ✅ Planner 内嵌定时运行
- ✅ 任务自动发射 + 自动消费
- ✅ 幂等键防重复

---

## 6. 配置参考

### 6.1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LCP_DB_DSN` | `mysql+aiomysql://lcp:lcp@127.0.0.1:3306/lcp` | 数据库连接 |
| `LCP_REST_HOST` | `127.0.0.1` | REST 绑定地址 |
| `LCP_REST_PORT` | `8080` | REST 端口 |
| `LCP_GRPC_HOST` | `127.0.0.1` | gRPC 绑定地址 |
| `LCP_GRPC_PORT` | `50051` | gRPC 端口 |
| `LCP_LANCE_STORAGE_ENDPOINT` | (空) | MinIO/S3 地址 |
| `LCP_LANCE_STORAGE_ACCESS_KEY` | (空) | S3 Access Key |
| `LCP_LANCE_STORAGE_SECRET_KEY` | (空) | S3 Secret Key |
| `LCP_GRAVITINO_URL` | (空) | Gravitino 地址, 空=禁用 |
| `LCP_GRAVITINO_METALAKE` | `default` | Gravitino metalake |
| `LCP_GRAVITINO_CATALOG` | `lance` | Gravitino catalog |
| `LCP_EMBEDDING_DEFAULT_ENDPOINT` | (空) | 默认 embedding 模型地址 |
| `LCP_ENFORCE_TENANT_RLS` | `true` | 是否启用 RLS |

### 6.2 Worker CLI 参数

```bash
python -m lcp.workers.lifecycle_worker \
  --idle-seconds 2 \
  --busy-seconds 0.1 \
  --heartbeat-seconds 10 \
  --planner-seconds 60 \
  --task-type TTL_DELETE    # 可选: 专用 worker
  --disable-planner         # 可选: 禁用内嵌 planner
```

---

## 7. 支持的 Task 类型

| Task Type | Executor | 说明 |
|-----------|----------|------|
| `TTL_DELETE` | TtlDeleteExecutor | 按 TTL 删除过期行 |
| `COMPACTION` | CompactionExecutor | 合并 lance 碎片文件 |
| `INDEX_BUILD` | IndexBuildExecutor | 构建 ANN 索引 (IVF_PQ/HNSW) |
| `INDEX_OPTIMIZE` | IndexOptimizeExecutor | 增量合并 delta 索引 |
| `VECTORIZE` | VectorizationExecutor | 增量计算 embedding |

---

## 8. REST API 端点

| Method | Path | 说明 |
|--------|------|------|
| GET | /healthz | 存活探针 |
| GET | /readyz | 就绪探针 (含 DB ping) |
| GET/POST | /v1/datasets | 数据集 CRUD |
| GET/POST | /v1/tasks | 任务管理 |
| POST | /v1/tasks/{uuid}/cancel | 取消任务 |
| POST | /v1/tasks/{uuid}/retry | 重试任务 |
| GET/POST | /v1/datasets/{uuid}/indexes | 索引管理 |
| POST | /v1/datasets/{uuid}/indexes/{name}/optimize | 触发优化 |
| GET/POST | /v1/datasets/{uuid}/lifecycle-policies | 生命周期策略 |
| GET/POST | /v1/datasets/{uuid}/vectorization-rules | 向量化规则 |
| POST | /v1/meta/sync | Gravitino 元数据同步 |
| GET | /v1/meta/schemas | 列出 Gravitino schemas |
| POST | /v1/meta/datasets/{uuid}/write-back | 回写统计到 Gravitino |
