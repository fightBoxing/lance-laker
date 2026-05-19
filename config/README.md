# 配置文件说明

LCP 使用 `LCP_` 前缀的环境变量进行配置。本目录按服务/职责拆分为独立的 `.env` 文件，方便运维按需加载。

## 文件清单

| 文件 | 作用域 | 说明 |
|------|--------|------|
| `server.env` | lcp-server | REST/gRPC 服务端口、OIDC 认证、mTLS、RLS |
| `worker.env` | lcp-worker | Worker 运行参数、心跳、Planner 周期 |
| `database.env` | 共享 | MySQL 连接串、连接池配置 |
| `storage.env` | 共享 | MinIO/S3 对象存储凭证 |
| `gravitino.env` | lcp-server | Gravitino 元数据集成 |
| `embedding.env` | lcp-worker | Embedding 模型端点、API Key |
| `dev.env` | 全部 | 本地开发一体化配置 (all-in-one) |
| `k8s.env` | 全部 | K8s 生产部署参考配置 |

## 使用方式

### 本地开发

```bash
# 方式 1: 拷贝为 .env (项目根目录, pydantic-settings 自动加载)
cp config/dev.env .env
lcp-server --insecure

# 方式 2: 指定 env 文件
env $(cat config/dev.env | grep -v '^#' | xargs) lcp-server --insecure
```

### K8s 部署

ConfigMap 已在 `deploy/k8s/00-namespace-config.yaml` 中定义。如需从文件生成：

```bash
kubectl create configmap lcp-config \
  --from-env-file=config/database.env \
  --from-env-file=config/storage.env \
  --from-env-file=config/server.env \
  -n lcp --dry-run=client -o yaml | kubectl apply -f -
```

### 敏感信息

**不要将以下字段写入 ConfigMap**，应使用 K8s Secret 或 Vault：
- `LCP_DB_DSN`（含密码）
- `LCP_LANCE_STORAGE_ACCESS_KEY` / `LCP_LANCE_STORAGE_SECRET_KEY`
- `LCP_GRAVITINO_AUTH_TOKEN`
- `LCP_EMBEDDING_API_KEY`

## 配置优先级

```
环境变量 > .env 文件 > 代码默认值
```

`pydantic-settings` 按此顺序合并。K8s 中环境变量通过 `envFrom: configMapRef + secretRef` 注入。

## 配置项总表

| 环境变量 | 服务 | 默认值 | 说明 |
|---------|------|--------|------|
| `LCP_APP_NAME` | all | `lcp` | 应用名 |
| `LCP_APP_ENV` | all | `dev` | 环境标识 |
| `LCP_REST_HOST` | server | `127.0.0.1` | REST 绑定地址 |
| `LCP_REST_PORT` | server | `8080` | REST 端口 |
| `LCP_GRPC_HOST` | server | `127.0.0.1` | gRPC 绑定地址 |
| `LCP_GRPC_PORT` | server | `50051` | gRPC 端口 |
| `LCP_OIDC_ISSUER` | server | `https://idp.example.com` | OIDC 发行方 |
| `LCP_OIDC_AUDIENCE` | server | `lcp-api` | JWT audience |
| `LCP_OIDC_JWKS_CACHE_TTL_SECONDS` | server | `3600` | JWKs 缓存 TTL |
| `LCP_OIDC_ALLOWED_ALGORITHMS` | server | `RS256,...` | 签名算法白名单 |
| `LCP_OIDC_CLOCK_SKEW_LEEWAY_SECONDS` | server | `30` | 时钟偏移容忍 |
| `LCP_MTLS_SERVER_CERT_PATH` | server | `/etc/lcp/tls/server.crt` | 服务端证书 |
| `LCP_MTLS_SERVER_KEY_PATH` | server | `/etc/lcp/tls/server.key` | 服务端私钥 |
| `LCP_MTLS_CA_CERT_PATH` | server | `/etc/lcp/tls/ca.crt` | CA 证书 |
| `LCP_MTLS_REQUIRE_CLIENT_CERT` | server | `true` | 是否要求客户端证书 |
| `LCP_ENFORCE_TENANT_RLS` | all | `true` | 是否启用 RLS |
| `LCP_DB_DSN` | all | `mysql+aiomysql://...` | 数据库连接 |
| `LCP_DB_POOL_SIZE` | all | `10` | 连接池大小 |
| `LCP_DB_MAX_OVERFLOW` | all | `20` | 溢出连接数 |
| `LCP_DB_POOL_RECYCLE_SECONDS` | all | `3600` | 连接回收 |
| `LCP_LANCE_STORAGE_ENDPOINT` | all | (空) | S3 端点 |
| `LCP_LANCE_STORAGE_ACCESS_KEY` | all | (空) | S3 Key |
| `LCP_LANCE_STORAGE_SECRET_KEY` | all | (空) | S3 Secret |
| `LCP_LANCE_STORAGE_REGION` | all | `us-east-1` | S3 区域 |
| `LCP_LANCE_STORAGE_ALLOW_HTTP` | all | `true` | 允许 HTTP |
| `LCP_LANCE_STORAGE_PATH_STYLE` | all | `true` | 路径风格 |
| `LCP_GRAVITINO_URL` | server | (空) | Gravitino 地址 |
| `LCP_GRAVITINO_METALAKE` | server | `default` | metalake |
| `LCP_GRAVITINO_CATALOG` | server | `lance` | catalog |
| `LCP_GRAVITINO_AUTH_TOKEN` | server | (空) | 认证 token |
| `LCP_GRAVITINO_TIMEOUT_SECONDS` | server | `10` | 超时 |
| `LCP_EMBEDDING_DEFAULT_ENDPOINT` | worker | (空) | Embedding 端点 |
| `LCP_EMBEDDING_API_KEY` | worker | (空) | API Key |
| `LCP_EMBEDDING_API_KEY_HEADER` | worker | `Authorization` | Key header |
| `LCP_EMBEDDING_REQUEST_TIMEOUT_SECONDS` | worker | `30` | 超时 |
