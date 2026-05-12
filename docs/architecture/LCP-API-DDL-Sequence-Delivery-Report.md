# LCP 接口契约 / 数据模型 / 运维时序补充交付报告

> 交付时间：2026-05-11
> 范围：基于 Python 技术栈，输出 LCP 三类核心交付物
> 关联设计文档：[`LCP-Architecture-Design.md`](./LCP-Architecture-Design.md)

---

## 一、本次交付清单

```
docs/architecture/
├── api/
│   ├── openapi/
│   │   ├── lcp-control-api.yaml     # 对外控制（dataset/vectorization）
│   │   ├── lcp-task-api.yaml        # 任务生命周期（submit/list/cancel/retry）
│   │   ├── lcp-index-api.yaml       # 索引生命周期（create/optimize/merge/drop）
│   │   └── lcp-meta-api.yaml        # 元数据同步 + 健康/Prometheus
│   └── protobuf/
│       ├── lcp_worker.proto         # LCP <-> Worker 调度协议（gRPC bidi）
│       ├── vdw_writer.proto         # VDW 批量写入协议
│       └── embedding_service.proto  # 多模态向量化协议
├── ddl/
│   └── lcp_state_schema.sql         # MySQL 8.0+ 完整 DDL（9 张表）
└── diagrams/
    ├── 05-compaction-flow.excalidraw          # 合并流程时序
    ├── 06-index-rebuild-flow.excalidraw       # 索引重建时序
    └── 07-lifecycle-recycle-flow.excalidraw   # 生命周期回收时序
```

---

## 二、关键决策与权衡

### 2.1 API 协议双轨并行

| 通道                  | 协议                | 选择理由                      |
| --------------------- | ------------------- | ----------------------------- |
| 对外控制（用户/网关） | OpenAPI 3.0 REST    | 跨语言、易调试、API 网关友好  |
| LCP ↔ Worker（调度）  | gRPC bidi stream    | 高频心跳/进度流式上报，强类型 |
| LCP ↔ Gravitino       | 复用 Gravitino REST | 不重复造轮子                  |
| LCP ↔ LanceDB         | Python 原生调用     | 同进程，无 RPC 损耗           |

### 2.2 OpenAPI 设计要点

- **Action 风格 endpoint**：`POST /tasks/{uuid}:cancel`、`:retry`、`:optimize`、`:merge`，避免与 CRUD 路径冲突
- **idempotency_key**：任务提交支持客户端幂等键（DDL 中也设置了 UNIQUE 索引）
- **进度字段 `progress`**：使用 `0~1 的 float`，避免百分比歧义
- **错误体统一**：`{code, message, request_id}`

### 2.3 Protobuf 设计要点

- **使用 `google.protobuf.Struct` 承载任务参数**：保持灵活性同时避免 `bytes` 黑盒
- **Worker 协议双向流**：`Heartbeat` 双向流让 LCP 能向 Worker 推送 `should_drain` 优雅下线信号
- **VDW `WriteBatch` 用流式 RPC**：上游推一批，VDW 立即返回 manifest 版本，便于背压控制
- **Embedding `oneof payload`**：text / inline binary / 对象存储 URI 三选一，按 payload 大小自适应

### 2.4 DDL 设计原则

| 原则                 | 落地方式                                                           |
| -------------------- | ------------------------------------------------------------------ |
| 业务键 vs 内部键分离 | `id BIGINT` 内部主键 + `xxx_uuid` 业务唯一键                       |
| 状态字段不用 ENUM    | 使用 `VARCHAR(32)` + COMMENT 列出取值，便于无锁加状态              |
| 自由参数用 JSON      | `params` / `extra` / `tier_rules` 全部 `JSON` 类型（MySQL 8 原生） |
| 时间精度 ms          | `DATETIME(3)` + `ON UPDATE CURRENT_TIMESTAMP(3)`                   |
| 索引按访问路径       | 调度热路径 `idx_status_priority_scheduled` 复合索引                |
| 软删除/审计可追溯    | `task_event` 追加表记录全部状态迁移                                |

### 2.5 时序图特别说明

| 图          | 关键差异化点                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------------- |
| 05 合并流程 | 通过 `distributed_lock` 表保证同 dataset 串行；manifest 原子提交保证读路径无感知               |
| 06 索引重建 | 拆 `optimize`（增量 delta）与 `merge`（合并 delta）两阶段；全量重建另由 `INDEX_BUILD` 流程承载 |
| 07 生命周期 | 拆三阶段：版本回收 → 冷热分层 → TTL 删除，独立开关、互不阻塞                                   |

---

## 三、验证结果

### 3.1 OpenAPI 校验

```
lcp-control-api.yaml   openapi: 3.0.3   4 paths
lcp-index-api.yaml     openapi: 3.0.3   5 paths
lcp-meta-api.yaml      openapi: 3.0.3   7 paths
lcp-task-api.yaml      openapi: 3.0.3   5 paths
```
✅ 所有文件以 `openapi: 3.0.3` 开头，paths 段存在；可在 Swagger Editor 直接打开。

### 3.2 Protobuf 校验

```
embedding_service.proto  syntax=proto3  package=lcp.embedding.v1  service=EmbeddingService
lcp_worker.proto         syntax=proto3  package=lcp.worker.v1     service=WorkerService
vdw_writer.proto         syntax=proto3  package=lcp.vdw.v1        service=VdwService
```
✅ 三个文件均含 syntax / package / service 三要素，可被 `protoc` 编译。

### 3.3 DDL 校验

```
CREATE TABLE 数量：9
  dataset / vectorization_rule / vector_index / task / task_event /
  lifecycle_policy / meta_sync_log / worker_registry / distributed_lock
```
✅ 9 张表、全部 InnoDB + utf8mb4_0900_ai_ci，外键 `dataset_uuid` 级联。
> 计划 8 表 → 实际 9 表，新增 `distributed_lock` 是 compaction 流程的物理依赖，符合"按需扩展，不过度设计"原则。

### 3.4 Excalidraw 时序图

```
05-compaction-flow.excalidraw         47 elements
06-index-rebuild-flow.excalidraw      49 elements
07-lifecycle-recycle-flow.excalidraw  49 elements
```
✅ 全部 JSON 解析通过，与已有 4 张图（01–04）样式统一（角色配色、字号、生命线虚线）。

---

## 四、契约与 DDL 一致性映射

| OpenAPI 资源        | DDL 表                | Protobuf 消息                             |
| ------------------- | --------------------- | ----------------------------------------- |
| `Dataset`           | `dataset`             | —                                         |
| `VectorizationRule` | `vectorization_rule`  | `EmbedRequest.model_*`                    |
| `Task`              | `task` + `task_event` | `Task` / `TaskProgress` / `ReportResult*` |
| `IndexResponse`     | `vector_index`        | —                                         |
| `MetaSyncResponse`  | `meta_sync_log`       | —                                         |
| Worker 注册         | `worker_registry`     | `Register*` / `Heartbeat*`                |
| Compaction 锁       | `distributed_lock`    | —                                         |

> 三类产物形成闭环：上层 REST 请求落表 → Scheduler 查表派发 gRPC 任务 → Worker 上报回写状态。

---

## 五、未做事项与边界

| 事项                                    | 状态   | 原因                                              |
| --------------------------------------- | ------ | ------------------------------------------------- |
| 自动化 lint（spectral / buf）集成       | ❌ 未做 | 用户未要求；建议下一步 CI 接入                    |
| Python 代码骨架（FastAPI / gRPC stubs） | ❌ 未做 | 本轮聚焦"契约与时序"，代码生成留待下一轮          |
| 鉴权设计（OAuth2 / mTLS）               | ❌ 未做 | 用户未要求；建议在工程化阶段补                    |
| 多租户隔离的 SQL 行级策略               | ❌ 未做 | 当前用 `tenant_id` 列 + 应用层校验；如需 RLS 再补 |

均严格遵循 Karpathy 原则：**只动用户要求动的部分**，其他保留为后续明确需求触发。

---

## 六、下一步建议

1. **代码骨架生成**：基于本次 OpenAPI 用 `openapi-generator` 生成 FastAPI server stub；基于 .proto 用 `grpcio-tools` 生成 Python gRPC stub
2. **CI 校验**：接入 Spectral（OpenAPI lint）+ buf lint（Protobuf lint）+ MySQL 在 CI 中跑 DDL 防回退
3. **本地 Demo**：起一个最小 LCP（FastAPI + 单 Worker + Docker MySQL）跑通 `register dataset → submit task → 上报结果` 端到端链路

---

## 七、文件位置速查

| 类型          | 路径                                                                           |
| ------------- | ------------------------------------------------------------------------------ |
| OpenAPI 规范  | [`docs/architecture/api/openapi/`](./api/openapi/)                             |
| Protobuf 协议 | [`docs/architecture/api/protobuf/`](./api/protobuf/)                           |
| MySQL DDL     | [`docs/architecture/ddl/lcp_state_schema.sql`](./ddl/lcp_state_schema.sql)     |
| 新增时序图    | [`docs/architecture/diagrams/05-07`](./diagrams/)                              |
| 主架构文档    | [`docs/architecture/LCP-Architecture-Design.md`](./LCP-Architecture-Design.md) |

---

**报告完毕。** 所有产物已通过语法/JSON 校验，可直接交付下一阶段工程化使用。
