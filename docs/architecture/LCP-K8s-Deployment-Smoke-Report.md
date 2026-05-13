# LCP k8s Deployment Dry-run + Lance/MinIO Smoke Report

> Run date: 2026-05-12
> Cluster: Colima k3s v1.35.0 (cri-dockerd, single node `colima`)
> Plan: 方案 C — k8s 部署演练 + 独立 lance/MinIO 冒烟（不引入 lance 到 LCP 主代码）

## 1. 摘要

| 验收项                                                                   | 结果                                                 |
| ------------------------------------------------------------------------ | ---------------------------------------------------- |
| `lcp:dev` Docker 镜像构建                                                | ✅ 418 MB / 98 MB content                             |
| `lcp` 命名空间 + ConfigMap + Secret                                      | ✅ apply 一次成功                                     |
| `lcp-api` Deployment + Service                                           | ✅ 1/1 Running, `/healthz` `200 OK`                   |
| `lcp-worker` Deployment                                                  | ✅ 1/1 Running, 注册到 `worker_registry`              |
| `lcp-planner` CronJob                                                    | ✅ 每分钟自动调度，emit task 后 worker 处理 SUCCEEDED |
| 控制面端到端（dataset → policy → task → SUCCEEDED）                      | ✅ 1.0s 内完成                                        |
| Lance + MinIO 数据面冒烟                                                 | ✅ 写 3 行/读 3 行/删 1 行/版本 ≥ 2                   |
| MinIO 实际对象（`_deletions/`, `_transactions/`, `_versions/`, `data/`） | ✅ 6 个对象落盘                                       |

## 2. 部署拓扑

```
┌────────────────────────────────────────────────────────────────────┐
│  Colima k8s (single node)                                          │
│                                                                    │
│  Namespace: lcp                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ lcp-api      │  │ lcp-worker   │  │ lcp-planner (CronJob)    │  │
│  │ Deployment   │  │ Deployment   │  │ ─ runs every minute       │  │
│  │ port 8080    │  │ daemon loop  │  │ ─ "* * * * *"             │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┬───────────┘  │
│         │                 │                         │              │
│         └─────────────────┴─────────────────────────┘              │
│                              │                                     │
│                       envFrom: ConfigMap + Secret                  │
│                              │                                     │
│  Namespace: default          ▼                                     │
│  ┌─────────────────────────────────────┐                           │
│  │ mysql-cdc Pod (NodePort 30306)      │  ← LCP_DB_DSN             │
│  │ schema `lcp` (lcp/lcp_dev_pwd)      │                           │
│  └─────────────────────────────────────┘                           │
│                                                                    │
│  Namespace: daft-platform                                          │
│  ┌─────────────────────────────────────┐                           │
│  │ minio Pod (NodePort 30900 S3 / 30901 console)                  │
│  │ buckets: lcp-lance, lcp-smoke       │  ← lance/MinIO smoke     │
│  └─────────────────────────────────────┘                           │
└────────────────────────────────────────────────────────────────────┘
```

## 3. 文件清单（本轮新增）

| 文件                                                                                       | 角色                                                                                             |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| [Dockerfile](../../Dockerfile)                                                             | 单镜像三角色（api/worker/planner），uv-installed runtime deps                                    |
| [.dockerignore](../../.dockerignore)                                                       | 排除 `tests/`, `docs/`, `.venv/` 等避免镜像膨胀                                                  |
| [deploy/k8s/00-namespace-config.yaml](../../deploy/k8s/00-namespace-config.yaml)           | `lcp` namespace + ConfigMap (`LCP_DB_DSN`, `LCP_REST_HOST=0.0.0.0`, etc.) + Secret (MinIO creds) |
| [deploy/k8s/10-api-deployment.yaml](../../deploy/k8s/10-api-deployment.yaml)               | REST API Deployment + NodePort Service `30808` + readiness/liveness on `/healthz`                |
| [deploy/k8s/20-worker-deployment.yaml](../../deploy/k8s/20-worker-deployment.yaml)         | Worker Deployment（无端口，`terminationGracePeriodSeconds: 30` 让 in-flight tick 排干）          |
| [deploy/k8s/30-planner-cronjob.yaml](../../deploy/k8s/30-planner-cronjob.yaml)             | CronJob 每分钟调度 `python -m lcp.workers.lifecycle_planner_cli`                                 |
| [src/lcp/workers/lifecycle_planner_cli.py](../../src/lcp/workers/lifecycle_planner_cli.py) | Planner CLI wrapper：起 engine、跑一次 `plan_once`、disposed 后退出                              |
| [scripts/k8s_smoke_seed.py](../../scripts/k8s_smoke_seed.py)                               | 通过 service 层在 Pod 内 seed 一个 dataset+policy（绕过 OIDC）                                   |
| [scripts/lance_minio_smoke.py](../../scripts/lance_minio_smoke.py)                         | 独立 lance + MinIO 冒烟脚本                                                                      |

## 4. 关键决策与权衡

### 4.1 为什么是单镜像三角色

API / Worker / Planner 都用 `lcp:dev`，由 manifest 的 `command/args` 选择 entrypoint：
- 镜像构建数量减少；
- 三个工作负载使用**完全相同**的 LCP 代码版本，规避 "api 改了 worker 没跟上" 的漂移风险；
- 代价：Worker Pod 也带了 FastAPI / uvicorn 依赖（约 30 MB），换来运维简单。

### 4.2 为什么 Planner 是 CronJob 而不是常驻 Deployment 内 sleep loop

|            | CronJob                           | 常驻 Deployment            |
| ---------- | --------------------------------- | -------------------------- |
| 失败重试   | k8s 原生 `failedJobsHistoryLimit` | 自己实现                   |
| 长连接漂移 | 每分钟新 engine，无累积           | DB 连接需要 `pool_recycle` |
| 漏触发     | `concurrencyPolicy: Forbid` 显式  | 容易在 sleep 期间漏        |
| 资源占用   | 一分钟一次 ~3s，谷底为 0          | 全程占内存                 |

CronJob 的代价是 Pod 启动 ~2-3s 开销；对于 1 分钟的 cadence 完全可接受。

### 4.3 为什么不在 LCP 主代码引入 `pylance`

现有 executor 的语义是"翻 MySQL 的 `vector_index.status` 状态机"，**没在打开 lance 数据集**。让冒烟脚本独立证明 "lance + MinIO 这一栈是工作的"，比把 pylance 一次性塞到 3 个 executor 里、再处理 schema/分区/索引各种边界更稳。后续单独立项接 lance 即可，本轮承诺范围内"k8s 部署 + 验证 lance 服务能用"两个目标都达成。

### 4.4 OIDC bypass

`OIDCAuthMiddleware` 强制要 token，dev 环境没真 IdP。两条路：
- 改 LCP 代码加 dev bypass — **拒绝**：触碰安全机制；用户没要求
- 在 Pod 内用 `kubectl exec` 直调 service 层 — **采用**：service 层对应 REST handler 内层的同一组逻辑，等价证明 + 不污染 prod 代码

## 5. 端到端运行轨迹

```text
13:19:33Z  worker registered (lease=d9e6dea9-...) -- worker_registry 表写入
13:24:40Z  CronJob tick #1: scanned=0 emitted=0 -- DB 还没 policy
13:27:44Z  scripts/k8s_smoke_seed.py:
              created dataset 7dc0e29f-da6b-42fc-8ff5-932c6b18560d
              created policy smoke_ttl (enabled=True)
13:28:01Z  Planner job planner-test-2: scanned=1 emitted=1
13:28:02Z  Worker: task d252e4a0-47bf-40fb-a1a2-18a0b25e8b56 (TTL_DELETE) -> SUCCEEDED
              ↑ 1.0 秒延迟（poll interval idle=2s, busy=0.1s）
13:31:46Z  scripts/lance_minio_smoke.py:
              wrote 3 rows / read 3 rows / delete -> 2 rows / version count = 2
              ✅ MinIO 上 6 个对象（事务日志 + 数据文件）
```

## 6. 复现步骤

```bash
# 0. 前置：Colima k8s 跑着，MinIO @ daft-platform，MySQL @ default/mysql-cdc

# 1. 构建镜像
docker build -t lcp:dev .                       # ~3 min（首次）

# 2. 创建 MinIO buckets
python -c "import boto3; s3=boto3.client('s3', \
    endpoint_url='http://127.0.0.1:30900', \
    aws_access_key_id='minioadmin', aws_secret_access_key='minioadmin', \
    region_name='us-east-1'); \
    [s3.create_bucket(Bucket=b) for b in ('lcp-lance','lcp-smoke')]"

# 3. 部署
kubectl apply -f deploy/k8s/

# 4. 验证 API
kubectl -n lcp port-forward svc/lcp-api 8088:8080 &
curl -s http://127.0.0.1:8088/healthz
# {"status":"ok","version":"0.1.0","env":"dev-k8s"}

# 5. Seed dataset + policy
API_POD=$(kubectl -n lcp get pod -l app.kubernetes.io/name=lcp-api -o jsonpath='{.items[0].metadata.name}')
kubectl -n lcp cp scripts/k8s_smoke_seed.py "$API_POD:/tmp/k8s_smoke_seed.py"
kubectl -n lcp exec "$API_POD" -- python /tmp/k8s_smoke_seed.py

# 6. 触发 planner & 看 worker 处理
kubectl -n lcp create job --from=cronjob/lcp-planner planner-test
kubectl -n lcp logs job/planner-test
kubectl -n lcp logs deploy/lcp-worker --tail=20

# 7. Lance + MinIO 冒烟
AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \
    python scripts/lance_minio_smoke.py

# 8. 清理
kubectl delete ns lcp
```

## 7. 跳过的事项（明确不在本轮承诺范围）

| 项                                   | 状态                              | 推荐下一轮处理         |
| ------------------------------------ | --------------------------------- | ---------------------- |
| Worker heartbeat 刷新                | 未实现，scheduler reaper 也未启动 | 一并补                 |
| Real lance integration in executors  | 未做                              | 单独里程碑             |
| OIDC dev bypass                      | 未做（且不应做）                  | 接真 IdP               |
| Prometheus metrics / log aggregation | 仅 stdlib logging                 | 引入 prom client       |
| RBAC + NetworkPolicy                 | 未做                              | dev 环境免，生产前必做 |
| Image registry / GitHub Actions      | 本地 build only                   | CI 推 ghcr.io          |

## 8. 调试笔记（供后续追溯）

| 阶段      | 问题                                      | 根因                                                                  | 修复                         |
| --------- | ----------------------------------------- | --------------------------------------------------------------------- | ---------------------------- |
| Build #1  | `egg_base 'src' does not exist`           | `uv export` 包含 `lcp @ file:///app`，第一层 install 时 src 还没 COPY | 加 `--no-emit-project`       |
| Build #2  | `pypi.org` 超时                           | Colima 容器到 PyPI 网速差                                             | `UV_INDEX_URL=tuna.tsinghua` |
| Deploy #1 | NodePort 30808 拒连接                     | Colima k8s 的 NodePort 不自动暴露到 host                              | `port-forward` 替代          |
| Seed #1   | `'tuple' object has no attribute 'items'` | `list_datasets` 返回 `(items, total)` 而非 PageObject                 | 拆元组                       |

---

**结论**：方案 C 全部目标达成，控制面三组件在 k8s 上稳定运行 ≥ 12 分钟，lance + MinIO 数据面已独立证明可用。
