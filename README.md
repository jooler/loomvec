# LoomVec

> 企业级数字资产管理（DAM）+ 多模态语义检索平台。用 DAM 的严谨管理资产，用多模态向量让资产"可被语义找到"，命中后精确回溯到页码 / 图片区域 / 视频时间点。
>
> 设计与任务文档见 [docs/](./docs)（开发推进以 `docs/06-开发计划总览.md` 为准）。

## 仓库结构

```
services/
  core/     loomvec-core   领域库：config/constants（标识单源）
                           ai/（网关+mock 供方） pipeline/（parsers 解析器链/mime/chunking/extraction
                           /steps/image_flow/media_flow/graph_flow/runner） graph/（AGE/merge/communities）
                           retrieval/（Milvus store/retriever/graph_retrieval/highlight）
                           db/（models 按域拆包/repos/pagination） authz/quota/taxonomy/imaging/transcode
                           mineru_client / storage / errors / 可观测性
  api/      loomvec-api    FastAPI：routes/（薄路由，含 admin/ 管理域、chat/graph/playback、oauth/oidc）
                           schemas/（契约） services/（业务） deps/identity/auth（认证/限流/空间）
                           probes/metrics_runtime（健康与指标） Alembic 迁移（0001~0005）
  worker/   loomvec-worker Celery 管线执行器（队列 pipeline/pipeline_high/pipeline_low，心跳+死信，
                           Prometheus :9808，Webhook 投递，合并/社区周期任务）
apps/
  web/      用户端 SPA（空间/上传/审核/检索/问答/图谱/媒体定位；Vite + React 18 + Tailwind v4）
  admin/    运维端 SPA（同栈 + shadcn Sidebar 布局）
packages/
  sdk-ts/   OpenAPI 契约生成的 TypeScript SDK（禁止手写接口）
  ui/       web/admin 同构共享组件包（shadcn/ui + 业务组件 + 共享工具）
evals/                    检索评测：docs/ 语料 + images/ 图片语料 + golden.jsonl（66 条六类）+ run_evals.py
deploy/
  compose/                 基础设施栈（PG18+AGE / Milvus / RustFS / Redis / MinerU / Keycloak / 监控）
  k8s/loomvec/             Helm chart（api/worker 多副本 + HPA + migrate Job）
  monitoring/              Prometheus / Alertmanager / Grafana / Loki 配置
scripts/
  export_openapi.py        契约导出（services/api 代码内注解即源）
  check_licenses.py        License 合规扫描（CI audit job 共用）
  smoke/                   冒烟脚本（AGE/Milvus/MinerU/存储/Redis/API）
  backup/                  备份 / 恢复 / 演练脚本
third_party/mineru/        MinerU 3.4.5 vendored 源码（compose 镜像构建上下文）
```

## 快速开始（本地开发）

```bash
./dev.sh start      # 一键启动全部：镜像检查（缺失自动拉取/构建）→ 基础设施+监控栈 → 迁移 → api/worker + 三个前端 web/admin/ops（幂等，已在跑的自动跳过）
./dev.sh status     # 查看各组件状态
./dev.sh stop       # 关闭三个前端、api/worker 应用进程，并停止基础设施与监控容器（数据卷保留；彻底清理用 docker compose -f deploy/compose/compose.yaml --profile observability down）
```

前置：Python 3.12（uv 自动管理）、Node 20+、pnpm、Docker。首次运行会自动生成
`deploy/compose/.env` 与根 `.env`；MinerU 镜像首次构建较慢、首次启动需下载模型约 1~2GB。
监控栈随一键启动一起拉起：Grafana http://localhost:3002 （admin 首页「打开 Grafana」按钮依赖，
密码见 `deploy/compose/.env` 的 `GRAFANA_ADMIN_PASSWORD`，默认 admin）、Prometheus http://localhost:9090。

<details>
<summary>手动分步启动（等价于 dev.sh）</summary>

```bash
# 1) 基础设施栈 + 监控栈
cd deploy/compose && cp .env.example .env && docker compose --profile observability up -d && cd ../..

# 2) 后端（API 8080；MinerU 占 8000）+ 管线 worker
uv sync && cp .env.example .env
make migrate                                        # 迁移（compose PG 在 5433，见 deploy/compose/.env）
uv run uvicorn loomvec.api.main:app --reload --port 8080   # http://localhost:8080/docs
uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low

# 3) 契约 → SDK（改 API 后执行）
uv run python scripts/export_openapi.py && pnpm install && pnpm sdk:generate

# 4) 前端
pnpm dev:web    # 用户端 http://localhost:5173
pnpm dev:admin  # 运维端 http://localhost:5174
pnpm dev:ops    # 运营端 http://localhost:5175（公共知识库维护，docs/12）

# 5) 冒烟 / 评测（API + worker 需已启动）
uv run python scripts/smoke/run.py
make evals        # 检索评测回归（金标 66 条；rerank A/B：--no-rerank）
```

</details>

dev 登录：`POST /api/v1/auth/dev/token`（用户名任意，免密签发测试 JWT；运维端 dev 登录默认 super_admin，运营端默认 operator）。

AI 网关：默认 `LOOMVEC_AI__MOCK=true`（确定性本地实现，离线可跑通全链路）。
接入云端供方时在 `.env` 配置 `LOOMVEC_AI__EMBEDDING__*` / `LLM__*` / `RERANK__*`
（OpenAI 兼容 + `/rerank`），并把 `LOOMVEC_AI__MOCK` 置 false。

## 工程约定速查

- 分支 `feat/P{n}-{slug}`；提交信息 `[{P{n}-{task}}] 描述`（docs/06 §三）。
- 契约先行：改 API → `export_openapi.py` + `pnpm sdk:generate` → 快照与 CI 校验，禁止破坏性变更。
- 一切 AI 调用经 `loomvec.core.ai` 网关（OpenAI 兼容），密钥只经环境变量注入。
- 所有业务表自带 `tenant_id` / `created_at` / `updated_at`；迁移只增不删。
- 测试：`uv run pytest`（集成测试需 Docker，无 Docker 自动跳过）；前端 `pnpm test`（vitest）。

## P0 阶段退出标准核对

- [x] `docker compose up` 一键起全栈，`scripts/smoke/run.py` 全绿（6/6，含 MinerU 解析样例 PDF）
- [x] 四个服务镜像可构建（loomvec-api/worker/web/admin，nginx 托管 SPA 已实测）
- [x] 双前端壳子可登录（dev token）并渲染布局
- [x] AGE/Milvus/MinerU 可用性均有脚本化验证（scripts/smoke/）
- [ ] CI 全绿（GitHub Actions 推送后确认）

## P1 阶段退出标准核对

- [x] 演示：上传 → 数分钟内 ready → 检索命中并跳转正确页码（web 空间资产页与 /a/{id} 预览页码跳转）
- [x] 混合召回可用，rerank 生效（`make evals` vs `uv run python evals/run_evals.py --no-rerank` A/B）
- [x] evals 金标集 60 条，hit-rate@10 基线阈值 0.6（见 evals/README.md）
- [x] API Key 可调用 search（`POST /api/v1/api-keys` 签发 → `X-API-Key` 头调用）
- [x] 管线失败可见原因并可单步重试（`GET /assets/{id}/jobs` + `POST /assets/{id}/retry`）
- [ ] CI 全绿（同上）
