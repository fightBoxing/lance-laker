  # LCP 架构设计方案（方案 A · 技术语义型）

> **方案版本**：v1.0  
> **命名方案**：方案 A — `LCP` (Lance Control Plane) + `VDW` (Vector Data Writer)  
> **设计目标**：基于 LanceDB OSS + Gravitino + 对象存储，自研对齐 LanceDB Enterprise 的控制面能力  
> **架构核心**：**控制面 / 计算面 / 数据面** 三面分离

---

## 目录

- [一、命名方案说明](#一命名方案说明)
- [二、整体分层架构](#二整体分层架构)
- [三、三面分离架构](#三三面分离架构)
- [四、LCP 内部模块详解](#四lcp-内部模块详解)
- [五、典型业务流程时序](#五典型业务流程时序)
- [六、关键设计决策](#六关键设计决策)
- [七、模块职责与官网依据](#七模块职责与官网依据)
- [八、状态存储设计](#八状态存储设计)
- [九、与 LanceDB Enterprise 的对齐](#九与-lancedb-enterprise-的对齐)
- [十、架构图清单](#十架构图清单)

---

## 一、命名方案说明

为保证命名的语义清晰、技术对齐、避免冲突，采用**技术语义型**命名方案：

| 原名称                | 新名称    | 全称                            | 命名依据                                                                     |
| --------------------- | --------- | ------------------------------- | ---------------------------------------------------------------------------- |
| `LMS`                 | **`LCP`** | **L**ance **C**ontrol **P**lane | 直接对齐 LanceDB Enterprise 官方文档「Control Plane」术语                    |
| `LakeKeeper-executor` | **`VDW`** | **V**ector **D**ata **W**riter  | 明确表达「向量数据写入器」职责，避免与开源 LakeKeeper（Iceberg Catalog）冲突 |

### 命名优势

1. ✅ **官方对齐**：术语标准化，与 LanceDB Enterprise 文档一致
2. ✅ **职责明确**：名称即职责，无歧义
3. ✅ **避免冲突**：彻底规避 LakeKeeper 等开源项目命名冲突
4. ✅ **可扩展**：未来可形成命名族系（如 `Vector Index Writer`、`Vector Delta Writer`）

---

## 二、整体分层架构

整个系统从上到下分为 **5 层**：应用网关层、统一元数据层、自研控制面（LCP）、数据面（LanceDB）、对象存储层。

📊 **对应架构图**：[`01-overall-layered-architecture.excalidraw`](./diagrams/01-overall-layered-architecture.excalidraw)

```
┌─────────────────────────────────────────────────────────────────┐
│                  多模态应用 / API 网关                            │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│           Gravitino（统一元数据 Catalog）                         │
│      库 / 表 / Schema / 分区路径 / 权限 / 版本 / 索引元信息         │
└────────────────────────────┬────────────────────────────────────┘
                             │ 元数据同步
┌────────────────────────────▼────────────────────────────────────┐
│         LCP - Lance Control Plane（自研控制面）                    │
│         调度编排层 / 业务能力层 / 治理层                            │
│         状态存储：MySQL / PgSQL                                   │
└──┬─────────────────────────────────────────┬────────────────────┘
   │ 任务提交（Ray）                            │ 索引直连（SDK）
┌──▼──────────────────────┐         ┌────────▼─────────────────────┐
│ Ray 分布式计算引擎       │         │      LanceDB SDK / Client     │
│ Embedding Worker · VDW  │         │ create_index / optimize_indices│
│ Compaction Worker       │         │       merge_indices           │
└──┬──────────────────────┘         └────────┬─────────────────────┘
   │                                          │
   └──────────────────────┬───────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│             LanceDB（Data Plane 数据面）                          │
│         向量存储 / 查询执行 / 碎片读写 / 基础索引                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│         对象存储 COS / MinIO / S3                                 │
│         Lance 文件 · 数据碎片 · 索引文件                           │
└─────────────────────────────────────────────────────────────────┘
```

### 各层职责

| 层级         | 组件                      | 核心职责                                  |
| ------------ | ------------------------- | ----------------------------------------- |
| **应用层**   | API 网关                  | 多模态应用接入、鉴权、限流                |
| **元数据层** | Gravitino                 | 业务元数据统一 Catalog（库/表/权限/版本） |
| **控制面**   | LCP                       | 调度、决策、治理、监控                    |
| **计算面**   | Ray + VDW + Model Server  | 向量化、批量写入、合并、索引构建          |
| **数据面**   | LanceDB Engine + 对象存储 | 向量存储、查询执行、读写                  |

---

## 三、三面分离架构

> 这是本方案的**核心设计原则**：将「决策」「执行」「存储」彻底分离，避免任何单一组件成为瓶颈。

📊 **对应架构图**：[`03-three-planes-separation.excalidraw`](./diagrams/03-three-planes-separation.excalidraw)

### 三面对比

| 维度         | 控制面 Control Plane    | 计算面 Compute Plane                                            | 数据面 Data Plane                        |
| ------------ | ----------------------- | --------------------------------------------------------------- | ---------------------------------------- |
| **核心组件** | LCP + MySQL + Gravitino | Ray + Embedding Worker + VDW + Compaction Worker + Model Server | LanceDB SDK + LanceDB Engine + COS/MinIO |
| **职责**     | 决策「做什么 / 何时做」 | 执行「怎么做 / 重计算」                                         | 存储「数据持久化与读取」                 |
| **资源特征** | 轻量、长驻、状态化      | 弹性扩缩容、计算密集                                            | 存储密集、读写优化                       |
| **部署形态** | 高可用集群（2-3 节点）  | Ray Cluster（按需扩展）                                         | LanceDB 多节点 + 对象存储                |
| **故障影响** | 调度暂停，存量数据可读  | 后台任务延迟，不影响在线                                        | 数据不可用（核心）                       |
| **可观测性** | 任务状态 / 指标 / 告警  | Ray Dashboard / Worker 日志                                     | 存储水位 / 查询延迟                      |

### 双写入路径（关键设计）

```
索引更新路径（轻量、强一致）
  LCP ──直连 LanceDB SDK──► LanceDB Engine
  • 适用：create_index / optimize_indices / merge_indices
  • 特点：控制面直接操作，元数据强一致

向量更新路径（重量、高吞吐）
  LCP ──提交 Ray Job──► VDW (Vector Data Writer) ──批量写入──► LanceDB Engine
  • 适用：批量数据写入 / Upsert
  • 特点：计算面批量写入，吞吐优先
```

**冲突协调机制**：通过 MySQL 的任务版本号 + 分布式锁，防止索引操作与数据写入并发冲突。

---

## 四、LCP 内部模块详解

LCP 内部按职责划分为**调度编排 / 业务能力 / 治理**三层，下沉 MySQL 作为状态存储。

📊 **对应架构图**：[`02-lcp-internal-modules.excalidraw`](./diagrams/02-lcp-internal-modules.excalidraw)

### 4.1 调度编排层

| 模块           | 英文名           | 核心职责                                          |
| -------------- | ---------------- | ------------------------------------------------- |
| **库表扫描器** | `Scanner`        | 周期扫描 Gravitino 元数据，发现新库/新表/增量数据 |
| **任务生成器** | `Task Generator` | 阈值触发、事件触发、定时触发，生成各类任务        |
| **任务调度器** | `Scheduler`      | 优先级队列、分布式锁、失败重试、资源预留          |

### 4.2 业务能力层

| 模块           | 英文名               | 核心职责                                                 |
| -------------- | -------------------- | -------------------------------------------------------- |
| **向量化管理** | `Embedding Manager`  | 模型路由、批处理、版本管理、结果缓存、多模态预处理       |
| **索引服务**   | `Index Service`      | HNSW/IVF-PQ 全量+增量构建、`merge_indices`、参数自动调优 |
| **合并服务**   | `Compaction Service` | 小文件阈值触发、分布式并行合并、版本一致性保证           |
| **生命周期**   | `Lifecycle Manager`  | 冷热分层、数据过期清理、索引生命周期治理                 |

### 4.3 治理层

| 模块           | 英文名             | 核心职责                                         |
| -------------- | ------------------ | ------------------------------------------------ |
| **元数据同步** | `Meta Sync`        | Gravitino ↔ MySQL ↔ LanceDB 三方元数据一致性     |
| **资源隔离**   | `Resource Manager` | 租户配额、Ray Placement Group 隔离、对象存储限流 |
| **监控告警**   | `Monitor & Alert`  | Prometheus 指标、链路追踪、业务指标、告警规则    |

---

## 五、典型业务流程时序

以「**新表向量化入库 + 索引构建**」为例，展示 6 个核心组件之间的完整交互流程。

📊 **对应架构图**：[`04-vector-ingestion-sequence.excalidraw`](./diagrams/04-vector-ingestion-sequence.excalidraw)

### 步骤说明

| 序号 | 发起方       | 接收方       | 动作                                            | 类型     |
| ---- | ------------ | ------------ | ----------------------------------------------- | -------- |
| 1    | 用户         | Gravitino    | 注册新表 / 数据源                               | 同步     |
| 2    | LCP          | Gravitino    | Scanner 扫描发现新表                            | 同步     |
| 3    | LCP          | MySQL        | 写入向量化任务                                  | 同步     |
| 4    | LCP          | Ray          | 提交 Ray Job（任务编排）                        | 同步     |
| 5    | Ray          | Model Server | 调用模型推理                                    | 同步     |
| 6    | Model Server | Ray          | 返回向量结果                                    | 异步回调 |
| 7    | VDW          | LanceDB      | 批量写入向量到 LanceDB                          | 同步     |
| 8    | Ray          | LCP          | 任务结果回调（成功/失败）                       | 异步回调 |
| 9    | LCP          | MySQL        | 更新任务状态                                    | 同步     |
| 10   | LCP          | LanceDB      | 增量阈值达到 → 直连 SDK 触发 `optimize_indices` | 同步     |
| 11   | LCP          | Gravitino    | 同步索引元信息回写                              | 同步     |

### 流程要点

- **步骤 4-7**：重计算路径走 Ray + VDW，弹性扩缩容
- **步骤 10**：轻量索引操作 LCP 直连 LanceDB SDK，不经过 Ray
- **步骤 8-9**：异步回调机制，不阻塞 LCP 调度循环
- **步骤 11**：闭环到 Gravitino，保证业务元数据可见性

---

## 六、关键设计决策

### 6.1 为什么引入 Ray 作为计算面？

**问题**：原架构把"调度"和"执行"耦合在管控服务里，导致：
- 重计算（向量化、合并、索引构建）会拖慢调度循环
- 无法弹性扩缩容
- 资源隔离困难

**方案**：引入 Ray 作为独立计算面，LCP 只做调度，Ray 做执行。

**收益**：
- LCP 轻量化部署（CPU/内存需求降低 80%）
- 计算资源按需弹性（Ray Worker 可伸缩到 0）
- Ray 原生 Placement Group 支持租户隔离

### 6.2 为什么 MySQL 与 Gravitino 双元数据库？

| 维度         | Gravitino               | MySQL/PgSQL               |
| ------------ | ----------------------- | ------------------------- |
| **定位**     | 业务元数据              | 运维元数据                |
| **存储内容** | 库 / 表 / Schema / 权限 | 任务 / 状态 / 版本 / 审计 |
| **使用方**   | 上层应用、SQL 引擎      | 仅 LCP 内部               |
| **变更频率** | 低频（DDL）             | 高频（每秒级任务状态）    |

**边界**：业务可见的元数据走 Gravitino，运维内部的状态走 MySQL，互不干扰。

### 6.3 为什么 Model Server 独立部署？

- **解耦升级**：模型升级不影响调度逻辑
- **资源特性不同**：Model Server 是 GPU 密集，与 CPU 密集的 Ray Worker 分离部署
- **复用性**：Model Server 可被多个上层服务复用，不仅服务 LCP

### 6.4 为什么双写入路径设计？

| 路径             | 适用场景             | 性能特征             |
| ---------------- | -------------------- | -------------------- |
| **LCP 直连 SDK** | 索引创建、优化、合并 | 元数据级操作，延迟低 |
| **Ray + VDW**    | 数据批量写入、Upsert | 高吞吐，可并行       |

**避免错误用法**：
- ❌ 不要让 LCP 直接做向量批量写入（会成为瓶颈）
- ❌ 不要让 Ray 做轻量索引操作（启动开销过大）

---

## 七、模块职责与官网依据

> 所有模块的设计都对应 LanceDB OSS **缺失**而 Enterprise 提供的能力，自研补齐这部分能力。

| 模块           | 核心职责             | 官网依据（OSS 缺失，Enterprise 提供）                                       |
| -------------- | -------------------- | --------------------------------------------------------------------------- |
| **任务调度**   | 全链路任务编排       | OSS 无调度能力；Enterprise 用独立 Indexer Fleet                             |
| **向量化管理** | 多模态向量生成       | LanceDB 只存向量不生成向量，必须外部服务                                    |
| **合并服务**   | 自动合并对象存储碎片 | OSS 只有手动 OPTIMIZE；Enterprise 后台异步 Compaction                       |
| **索引服务**   | 全生命周期索引管理   | OSS 需手动 `create_index/optimize_indices`；Enterprise 全自动 Auto Indexing |
| **元数据同步** | 三方元数据一致性     | Gravitino 统一 Catalog 需外部服务同步数据层状态                             |
| **资源隔离**   | 多租户资源隔离       | OSS 无资源隔离；Enterprise 用 Control Plane 管理集群资源                    |
| **监控告警**   | 全链路可观测         | OSS 无监控；Enterprise 提供集群监控面板                                     |
| **生命周期**   | 数据与索引治理       | OSS 无生命周期管理；Enterprise 提供数据治理能力                             |

---

## 八、状态存储设计

LCP 自身的状态存储下沉到 MySQL/PgSQL，承担以下表结构：

| 表名         | 用途             | 关键字段示例                                                              |
| ------------ | ---------------- | ------------------------------------------------------------------------- |
| `tasks`      | 调度任务状态机   | `task_id`, `task_type`, `status`, `priority`, `retry_count`, `created_at` |
| `indices`    | 索引元信息与版本 | `index_id`, `table_id`, `index_type`, `params`, `version`, `status`       |
| `versions`   | 数据版本快照     | `version_id`, `table_id`, `commit_time`, `manifest_path`                  |
| `audit_logs` | 操作审计追踪     | `log_id`, `actor`, `action`, `target`, `timestamp`                        |
| `tenants`    | 多租户配额       | `tenant_id`, `cpu_quota`, `memory_quota`, `storage_quota`                 |

### 与 Gravitino 的边界

- **Gravitino**：对外暴露的业务元数据（库/表/Schema/权限）
- **MySQL/PgSQL**：LCP 内部使用的运维元数据（任务/状态/审计）
- **同步策略**：通过 `Meta Sync` 模块在两者之间做必要的双向同步

---

## 九、与 LanceDB Enterprise 的对齐

| 维度         | LanceDB Enterprise | 本方案（LCP）                              |
| ------------ | ------------------ | ------------------------------------------ |
| **控制面**   | 官方 Control Plane | 自研 LCP（对齐 Enterprise 能力）           |
| **索引服务** | Indexer Fleet      | LCP 索引服务模块 + Ray Compute             |
| **向量化**   | 内置或集成         | 自研 Embedding Manager + 独立 Model Server |
| **元数据**   | 官方 Catalog       | Gravitino（统一元数据）                    |
| **存储**     | 托管对象存储       | COS / MinIO / S3                           |
| **数据面**   | 托管 LanceDB       | LanceDB OSS                                |
| **部署形态** | SaaS 托管          | 私有化自部署                               |

**核心逻辑完全一致**：数据面（LanceDB）只做读写，控制面（LCP）做调度与运维，**仅交付形态不同**。

---

## 十、架构图清单

本方案共配套 **4 张架构图**，存放于 `./diagrams/` 目录下，均为 Excalidraw 格式（`.excalidraw`），可在 https://excalidraw.com 中打开和编辑。

| 序号 | 文件名                                                                                                | 内容说明                                                        |
| ---- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 01   | [`01-overall-layered-architecture.excalidraw`](./diagrams/01-overall-layered-architecture.excalidraw) | 整体五层分层架构（应用 → Gravitino → LCP → LanceDB → 对象存储） |
| 02   | [`02-lcp-internal-modules.excalidraw`](./diagrams/02-lcp-internal-modules.excalidraw)                 | LCP 内部三层模块（调度编排 / 业务能力 / 治理）                  |
| 03   | [`03-three-planes-separation.excalidraw`](./diagrams/03-three-planes-separation.excalidraw)           | 三面分离架构（控制面 / 计算面 / 数据面）                        |
| 04   | [`04-vector-ingestion-sequence.excalidraw`](./diagrams/04-vector-ingestion-sequence.excalidraw)       | 典型业务时序（新表向量化入库 + 索引构建）                       |

### 如何查看架构图

**方式 1：在线打开（推荐）**
1. 访问 https://excalidraw.com
2. 点击左上角菜单 → Open
3. 选择对应的 `.excalidraw` 文件

**方式 2：VS Code 插件**
1. 安装 [Excalidraw VS Code 扩展](https://marketplace.visualstudio.com/items?itemName=pomdtr.excalidraw-editor)
2. 直接在 VS Code 中打开 `.excalidraw` 文件即可可视化编辑

**方式 3：拖拽打开**
- 直接将 `.excalidraw` 文件拖拽到 https://excalidraw.com 页面

---

## 总结

本方案的核心价值：

1. ✅ **命名标准化**：`LCP` + `VDW` 完全对齐 LanceDB 官方术语，避免与 LakeKeeper 等开源项目冲突
2. ✅ **三面分离**：控制面 / 计算面 / 数据面彻底解耦，每一面都可独立伸缩
3. ✅ **双写入路径**：索引（轻量直连 SDK）与数据（重量经 Ray）分离，兼顾一致性与吞吐量
4. ✅ **双元数据库**：Gravitino 管业务元数据，MySQL 管运维元数据，边界清晰
5. ✅ **官网对齐**：补齐 LanceDB OSS 缺失的所有 Enterprise 能力，可平滑替换为 Enterprise

**下一步建议**：
- 📐 输出 LCP 各模块的**详细 API 接口定义**（OpenAPI / Protobuf）
- 🗂️ 输出 MySQL **状态库的完整建表 DDL**
- 🔄 补充更多业务流程时序图（合并、索引重建、生命周期回收等）
- 📦 拆解 Ray Worker 的具体实现细节（VDW 内部状态机、批处理策略等）
