# LoomVec Helm Chart（P4-INF-02）

LoomVec 知识库平台的 Kubernetes 部署：FastAPI `api`、Celery `worker`（队列
`pipeline` / `pipeline_high`）、React 静态站 `web` / `admin`（nginx）、
RustFS 对象存储（StatefulSet，可切外置 S3）、Milvus 分布式（bitnami 子 chart，
可切外部集群）。数据库迁移经 `pre-install` / `pre-upgrade` 钩子 Job 执行
`alembic upgrade head`。

## 目录结构

```
deploy/k8s/loomvec/
├── Chart.yaml            # name: loomvec, version/appVersion 0.1.0（对齐 services/api）
├── values.yaml           # 默认值（所有 LOOMVEC_ 变量均有对应配置项，键名语义对齐 .env.example）
├── templates/
│   ├── _helpers.tpl      # 命名/标签/镜像/环境变量公共片段
│   ├── serviceaccount.yaml
│   ├── configmap.yaml    # 非敏感配置速览
│   ├── secret.yaml       # JWT secret / 对象存储凭据 / AI 密钥（支持 existingSecret）
│   ├── migrate-job.yaml  # helm hook: pre-install,pre-upgrade → alembic upgrade head
│   ├── api.yaml          # Deployment(2) + Service + 短名 api Service + HPA(2~6, CPU 70%)
│   ├── worker.yaml       # Deployment(2) + HPA(2~8, 可关)  队列 pipeline,pipeline_high
│   ├── web.yaml / admin.yaml  # Deployment(1~2) + Service
│   ├── rustfs.yaml       # StatefulSet(3) + Service（storage.mode=internal 时）
│   ├── ingress.yaml      # nginx ingress（默认关）
│   ├── servicemonitor.yaml    # Prometheus Operator（默认关）
│   ├── pdb.yaml          # api/worker 默认开启
│   └── NOTES.txt
└── README.md
```

## 依赖前提

- Kubernetes 1.25+，Helm 3.8+；已配置 StorageClass（RustFS / Milvus 持久卷）。
- PostgreSQL 18 + Apache AGE：**不在 chart 内部署**（需 AGE 扩展的自定义镜像，
  参照 `deploy/compose/postgres-age`）。用 `postgres.url` 指向自建/云托管实例，
  或把完整 DSN 放进 Secret 后设 `postgres.existingSecret`。
- Redis：同上，`redis.url` 或 `redis.existingSecret` 指向自建/云托管实例。
- Milvus 分布式：默认经 bitnami 子 chart（`milvus.enabled=true`）；先拉取依赖：

  ```bash
  helm dependency update deploy/k8s/loomvec
  ```

  外部/云托管 Milvus 时设 `milvus.enabled=false` + `milvus.external.uri`。
- MinerU（可选）：大镜像不随 chart 部署，`appConfig.mineru.base_url` 指向
  已部署的解析服务（镜像构建见 `deploy/compose/mineru`）。
- 监控：Prometheus Operator 部署后开 `serviceMonitor.enabled=true`；
  无 Operator 时沿用 `deploy/monitoring/prometheus.yml`，把静态 target 改为
  `<release>-api.<namespace>.svc:<port>`（路径 `/metrics`）。

## 安装

```bash
# 1) 拉取 bitnami/milvus 子 chart（milvus.enabled=true 时必需）
helm dependency update deploy/k8s/loomvec

# 2) 生产 values（务必覆盖 secret、外部依赖地址），示例：
helm install loomvec deploy/k8s/loomvec \
  -n loomvec --create-namespace \
  --set postgres.url='postgresql+asyncpg://loomvec:<pw>@pg-host:5432/loomvec' \
  --set redis.url='redis://redis-host:6379/0' \
  --set secret.jwtSecret='<强随机串>' \
  --set appConfig.ai.mock=false \
  --set secret.stringData.LOOMVEC_AI__EMBEDDING__API_KEY=sk-xxx

# 3) 校验
helm lint deploy/k8s/loomvec
helm template loomvec deploy/k8s/loomvec -n loomvec | less
```

升级与卸载：

```bash
helm upgrade loomvec deploy/k8s/loomvec -n loomvec   # 自动触发 migrate Job（pre-upgrade）
helm uninstall loomvec -n loomvec                     # PVC 不会自动删除，注意留存
```

## 关键配置对照（.env.example → values.yaml）

| 环境变量 | values 路径 |
| --- | --- |
| `LOOMVEC_ENV` / `LOOMVEC_LOG_LEVEL` / `LOOMVEC_LOG_JSON` | `config.env` / `config.logLevel` / `config.logJson` |
| `LOOMVEC_POSTGRES__URL` | `postgres.url`（或 `postgres.existingSecret`） |
| `LOOMVEC_REDIS__URL` | `redis.url`（或 `redis.existingSecret`） |
| `LOOMVEC_MILVUS__URI` | `milvus.enabled` + `milvus.external.uri`（内部自动指向子 chart 服务） |
| `LOOMVEC_STORAGE__ENDPOINT` | `storage.mode=external` 时 `storage.external.endpoint`；internal 指向内置 RustFS |
| `LOOMVEC_STORAGE__ACCESS_KEY` / `__SECRET_KEY` | `secret.storageAccessKey` / `secret.storageSecretKey`（或 `storage.external.existingSecret`） |
| `LOOMVEC_STORAGE__BUCKET_RAW` / `__BUCKET_DERIVED` | `config.storage.bucketRaw` / `bucketDerived` |
| （应用参数不再经环境变量） | `appConfig`（渲染为 app-config.json 挂载） |
| `LOOMVEC_AUTH__DEV_MODE` | `config.auth.devMode`（生产必须 false） |
| `LOOMVEC_AUTH__JWT_SECRET` | `secret.jwtSecret`（或 `secret.existingSecret` 键 `auth-jwt-secret`） |
| （应用参数不再经环境变量） | `appConfig.ai.mock`（生产必须 false） |
| （应用参数不再经环境变量） | `appConfig`（含全部 AI 通道；密钥直接写在 appConfig 内，经 Secret 渲染挂载） |
| 检索/管线/上传/图片/Worker 等其余项 | `config.extraEnv`（键名为 `LOOMVEC_` 之后的部分，如 `SEARCH__RRF_K`） |

## 扩缩容与滚动更新

- **api**：默认 2 副本，HPA 2~6（CPU 70%）。调整：`--set api.hpa.maxReplicas=8`
  或 `kubectl -n loomvec patch hpa <release>-api ...`。
- **worker**：默认 2 副本，HPA 2~8（CPU 70%，`worker.hpa.enabled=false` 可关）。
  队列经 `worker.celeryArgs` 控制（默认 `-Q pipeline,pipeline_high`）。
- **web / admin**：`web.replicaCount` / `admin.replicaCount`（1~2）。
- **滚动策略**：api / web / admin 均为 `maxSurge: 1 / maxUnavailable: 0`
  （先补后缩，容量不缩水）；worker 默认小步逐批，长任务场景可设
  `--set worker.strategy.type=OnDelete` 手动逐批滚动，Pod 优雅退出等待
  `terminationGracePeriodSeconds: 3600`（对齐任务软中断 3500s）。
- 就绪探针 `/readyz` 含依赖探测，单实例不健康不会进入负载；PDB（api/worker
  默认开启）保证节点维护时最少可用副本。

## 备份与恢复

数据库 / 对象存储 / Milvus 的备份恢复脚本见 [`scripts/backup/`](../../../scripts/backup/)：
`backup.sh` / `restore.sh` / `drill.sh`（演练）。K8s 环境下可在能访问
Postgres、RustFS(S3) 与 Milvus 的 Pod/节点上以 `kubectl exec` 或 Job 方式执行；
PVC 层面建议配合集群快照（VolumeSnapshot）策略。
