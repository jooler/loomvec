# LoomVec

[English](./README.md) | **简体中文**

> **企业级**数字资产管理（DAM）+ 多模态语义检索平台。用 DAM 的严谨体系管理资产，用多模态向量让资产"可被语义找到"——命中后精确回溯到原文页码、图片区域、视频时间点。

LoomVec 是面向企业的多租户、可私有化部署的知识平台：文档、图片、音视频经解析-嵌入管线摄取，索引进混合检索引擎（dense + BM25 + CLIP + 知识图谱），通过三个 Web 控制台、AI 智能体对话与开放 API 对外提供服务。

## 核心亮点

### 生而企业级

- **多租户** — tenant → space → 成员角色；每次查询与向量检索都按租户 × 空间 × 角色三重过滤，杜绝越权。
- **身份与访问** — OIDC 对接企业 IdP（Keycloak 等）实现 SSO；OAuth 2.0 授权码流程支持第三方用户级授权；API Key + Webhook 面向应用级集成；平台角色（super_admin / operator / auditor）。
- **治理** — "先审后见"审核流、两级配额（租户/空间；存储量 + 文件数）、全局限流、审计日志（支持 JSONL 导出）。
- **运维就绪** — Prometheus / Grafana / Loki / Alertmanager 可观测性栈，Kubernetes Helm chart（api/worker 多副本 + HPA），备份/恢复/演练脚本。
- **合规** — CI 强制依赖 License 扫描（`scripts/check_licenses.py`）；MinerU 附加条款核对清单见 `docs/13`。

### 命中即可回溯的多模态检索

- 摄取管线：MinerU 解析 → LLM 语义分片 → 嵌入 → Milvus 入库；图片（缩略图/EXIF）、音视频（懒转码、关键帧）。
- 混合召回：dense + BM25 + 图谱三路，外加 CLIP 以文搜图；RRF 融合后强制 cross-encoder 重排。
- 命中携带 **locator**——页码 / 图片区域 / 视频时间戳，直达原文出处并高亮证据。
- 检索质量有评测护栏：66 条六类金标集，hit-rate@10 / MRR 回归（`make evals`）。

### 知识图谱

- 实体抽取与分片合并为单次 LLM 调用，两阶段实体合并（merge_log 可审计），Apache AGE 与 PostgreSQL 同库存图（资产删除同事务级联清图）。
- Leiden 社区检测 + 摘要；图谱召回路支撑多跳关系与全局总结类问题。

### dsh（deepseek-harness）驱动的智能体对话

- 智能体对话：工具调用、多步任务、崩溃可恢复的会话 resume——对话由 dsh 全量接管，前端只负责 UI。
- 经内置 MCP endpoint 接入知识库：智能体检索其有权访问的空间，回答携带 `[n]` 引用并可跳转到精确定位。
- 每用户**服务端工作环境**（持久化工作区 + 会话 + 知识范围），为离职交接而生：环境可重绑给新员工，文件、历史、知识范围即刻接手。

### 三个控制台 + 开放 API

| 控制台 | 面向 | 职责 |
|---|---|---|
| `apps/web` | 企业最终用户 | 知识空间、Finder 式资产管理、检索、智能体对话、图谱、媒体定位 |
| `apps/admin` | 平台管理员 | 租户、用户、空间治理、审核、管线、开放平台、系统配置、审计 |
| `apps/ops` | 内容运营 | 公共知识空间、用户分组、分组可见性 |

开放 API：契约先行，OpenAPI → 生成 TypeScript SDK（`packages/sdk-ts`，禁止手写接口类型）；第三方集成走 API Key、OAuth 2.0 与 Webhook 订阅。

## 文档

设计与任务文档见 [docs/](./docs)。`docs/00-项目核心文档.md` 是架构与决策的唯一事实源（SSOT）；开发推进以 `docs/06-开发计划总览.md` 为准。

## 快速开始（本地开发）

前置：Docker；Python 3.12（由 [uv](https://docs.astral.sh/uv/) 管理）；Node 20+ 与 [pnpm](https://pnpm.io)。

新环境首次部署：先运行 `./deploy.sh` 交互式录入真实 AI 供方（对话 LLM / 嵌入 / 重排为必配，VLM / CLIP 可选；全部回车选 mock 则生成离线配置），写入 `config/loomvec.json`；然后一键启动：

```bash
./deploy.sh      # 仅首次需要；已有配置时逐项回车即保留现值
./dev.sh start
```

若跳过 `./deploy.sh` 直接 `./dev.sh start`，脚本检测到尚未部署（无 tmp/loomvec-deployed.stamp 标记）时，会在交互终端下自动先拉起部署流程，完成后继续启动；部署被取消则本次启动终止（CI/脚本管道等非交互环境跳过自动部署，按离线 mock 兜底）。

`./dev.sh start` 一条命令拉起全部：镜像检查（缺失自动拉取/构建）→ 基础设施 + 监控栈 → 数据库初始化 → api/agent/worker + 三个前端（幂等，已在跑的自动跳过）。

| 服务 | 地址 | 说明 |
|---|---|---|
| 用户端 | http://localhost:35173 | dev 登录：任意用户名 |
| 运维端 | http://localhost:35174 | dev 登录默认 `super_admin` |
| 运营端 | http://localhost:35175 | dev 登录默认 `operator` |
| API 文档 | http://localhost:38080/docs | |
| Grafana | http://localhost:33002 | 密码见 `deploy/compose/.env` 的 `GRAFANA_ADMIN_PASSWORD`（默认 admin） |
| Prometheus | http://localhost:39090 | |

首次运行会自动生成 `deploy/compose/.env`、根 `.env` 与应用参数文件 `config/loomvec.json`（模板 `config/loomvec.example.json`，生成时已置 `ai.mock=true`，离线可跑通全链路；该文件含密钥、不入库）。MinerU 镜像首次构建较慢，首次启动需下载约 1~2GB 模型。`./dev.sh status` 查看各组件状态；`./dev.sh stop` 关闭应用进程与容器（数据卷保留）。

<details>
<summary>手动分步启动（等价于 dev.sh）</summary>

```bash
# 1) 基础设施栈 + 监控栈
cd deploy/compose && cp .env.example .env && docker compose --profile observability up -d && cd ../..

# 2) 后端（API 38080，agent 网关 38090 内网 only，MinerU 38000）+ 管线 worker
uv sync && cp .env.example .env
[ -f config/loomvec.json ] || cp config/loomvec.example.json config/loomvec.json  # 应用参数不入库；离线开发把 "mock": false 改为 true
make init-db                                        # 幂等初始化：建最新结构 + 种子（compose PG 在 35433）
uv run uvicorn loomvec.api.main:app --reload --port 38080
uv run python -m loomvec.agent --reload
uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low

# 3) 契约 → SDK（改 API 后执行）
uv run python scripts/export_openapi.py && pnpm install && pnpm sdk:generate

# 4) 前端
pnpm dev:web    # 用户端   http://localhost:35173
pnpm dev:admin  # 运维端   http://localhost:35174
pnpm dev:ops    # 运营端   http://localhost:35175

# 5) 冒烟 / 检索评测（需 API + worker 已启动）
uv run python scripts/smoke/run.py
make evals        # 检索评测回归（rerank A/B：--no-rerank）
```

</details>

### AI 供方

AI 供方参数**只**来自 `config/loomvec.json`（模板 `config/loomvec.example.json`；文件含密钥不入库，环境变量对其不生效）。推荐经 `./deploy.sh` 交互式写入（对话 LLM / 嵌入 / 重排必配，VLM / CLIP 可选；跳过则置 `ai.mock=true`，用确定性本地供方离线跑通全链路）。手工接入云端供方：在该文件中把 `ai.mock` 改为 `false`，填写 `ai.llm` / `ai.embedding` / `ai.rerank` 等段的 `base_url` / `api_key` / `model`（OpenAI 兼容端点；rerank / clip 另支持 `api_style: "dashscope"` 原生协议），然后重启 api/agent/worker 使其生效（`./dev.sh stop && ./dev.sh start`）。一切 AI 调用经 `loomvec.core.ai` 网关；供方地址、密钥、模型名均为部署配置——未来内网化时指向自托管端点即可，不改代码。

新环境部署注意：若走"手动分步启动"且漏建 `config/loomvec.json`，mock 停留在代码默认 `false`，上传资产会在 embed 步骤因拿不到 AI 供方的 key/model 而失败（资产卡在"失败"）。补救：补跑 `./deploy.sh`（离线开发可置 `ai.mock=true`），重启 api/agent/worker，再对失败资产在页面点"重试"即可重跑。

## 工程约定

- **契约先行**：改 API → 导出 OpenAPI → 重新生成 TS SDK → 快照与 CI 校验；禁止破坏性变更（当前仅 v1）。
- 所有业务表自带 `tenant_id` / `created_at` / `updated_at`。
- 测试：`make test`（后端 pytest——集成测试需 Docker，无 Docker 自动跳过；前端 vitest）。Lint：`make lint`。

## 依赖致谢

LoomVec 站在众多优秀开源项目的肩膀上，向它们的作者与社区致以诚挚感谢。

**摄取与智能体运行时**

- [MinerU](https://github.com/opendatalab/MinerU) — PDF/文档解析引擎，vendored 于 `third_party/mineru`（Apache-2.0 + 附加条款，见 `third_party/mineru/LICENSE.md` 与 `docs/13` 合规核对清单）。解析器链可插拔（默认 MinerU → 纯文本兜底），替换解析器无需改动管线步骤。
- [deepseek-harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) — 企业对话背后的智能体运行时（MIT）；以 git submodule 形式固定在 `third_party/deepseek-harness` 作协议参考，生产运行时经 `deepseek-harness-sdk` 包接入。

**数据与基础设施**

- [PostgreSQL](https://www.postgresql.org) 18 + [Apache AGE](https://age.apache.org/) — 事实源与属性图
- [Milvus](https://milvus.io) — 混合检索的向量数据库
- [RustFS](https://github.com/rustfs/rustfs) — S3 兼容对象存储
- [Redis](https://redis.io) — 任务队列、缓存与事件广播
- [Keycloak](https://www.keycloak.org) — SSO 用的 OIDC 身份源

**应用技术栈**

- 后端：[FastAPI](https://fastapi.tiangolo.com)、[Celery](https://docs.celeryq.dev)、[SQLAlchemy](https://www.sqlalchemy.org)、[Pydantic](https://docs.pydantic.dev)
- 前端：[React](https://react.dev)、[Vite](https://vite.dev)、[Tailwind CSS](https://tailwindcss.com)、[shadcn/ui](https://ui.shadcn.com)
- 可观测性：[Prometheus](https://prometheus.io)、[Grafana](https://grafana.com)、[Loki](https://grafana.com/oss/loki/)、[Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/)

依赖 License 由 CI 持续强制（`scripts/check_licenses.py`）。

## License

LoomVec 基于 [GNU AGPL-3.0](./LICENSE) 发布。第三方组件遵循其各自 License（见 `third_party/`）。
