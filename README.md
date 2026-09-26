# LoomVec

**English** | [简体中文](./README.zh-CN.md)

> An **enterprise-grade** digital asset management (DAM) + multimodal semantic retrieval platform. Manage assets with DAM-grade rigor and make them findable by meaning with multimodal vectors — every hit traces back to an exact page, image region, or video timestamp.

LoomVec is a multi-tenant, self-hostable knowledge platform for enterprises: documents, images, audio and video are ingested through a parsing-and-embedding pipeline, indexed into a hybrid retrieval engine (dense + BM25 + CLIP + knowledge graph), and consumed through three web consoles, an AI agent chat, and an open API.

## Highlights

### Enterprise-grade by design

- **Multi-tenancy** — tenant → space → member role; every query and vector search is filtered by tenant × space × role to prevent cross-tenant leaks.
- **Identity & access** — SSO via OIDC (Keycloak or any enterprise IdP), OAuth 2.0 authorization-code flow for third-party user-level access, API keys + webhooks for app-level integration, platform roles (super_admin / operator / auditor).
- **Governance** — review-before-publish workflows, two-level quotas (tenant/space; storage + file count), global rate limiting, full audit logs with JSONL export.
- **Operations-ready** — Prometheus / Grafana / Loki / Alertmanager observability stack, Helm chart for Kubernetes (multi-replica api/worker + HPA), backup/restore/drill scripts.
- **Compliance** — dependency license scanning enforced in CI (`scripts/check_licenses.py`); MinerU additional-terms checklist maintained in `docs/13`.

### Multimodal retrieval that traces back

- Ingestion pipeline: MinerU parsing → LLM semantic chunking → embedding → Milvus indexing; images (thumbnails/EXIF), audio & video (lazy transcoding, keyframes).
- Hybrid recall: dense + BM25 + knowledge graph, plus CLIP text-to-image; RRF fusion with mandatory cross-encoder reranking.
- Hits carry **locators** — page number / image region / video timestamp — so users jump straight to the source, with highlighted evidence.
- Retrieval quality is guarded by an evaluation suite: a 66-item golden set with hit-rate@10 / MRR regression (`make evals`).

### Knowledge graph

- Entity extraction fused into the chunking call, two-phase entity merge with an auditable merge log, Apache AGE graph storage in the same PostgreSQL instance (asset deletion cascades in one transaction).
- Leiden community detection with summaries; graph recall path serves multi-hop and summary questions.

### Agentic chat powered by deepseek-harness (dsh)

- Agent conversations with tool calling, multi-step tasks, and crash-resilient session resume — dsh fully drives the conversation; the web console is UI only.
- Knowledge-base access via a built-in MCP endpoint: the agent searches spaces it is authorized for, and answers carry `[n]` citations that jump to exact locators.
- Per-user **server-side work environments** (persistent workspace + sessions + knowledge scope), designed for enterprise offboarding: an environment can be rebound to a new employee who instantly takes over files, history, and knowledge scope.

### Three consoles + open API

| Console | Audience | Scope |
|---|---|---|
| `apps/web` | End users | Spaces, Finder-style asset management, search, agent chat, graph, media locator |
| `apps/admin` | Platform admins | Tenants, users, spaces governance, review, pipelines, open platform, config, audit |
| `apps/ops` | Content operations | Public knowledge spaces, user groups, group-based visibility |

Open API: contract-first OpenAPI → generated TypeScript SDK (`packages/sdk-ts`, hand-written client types are forbidden); API keys, OAuth 2.0, and webhook subscriptions for third-party integrations. Self-hosted MinerU parsing is additionally exposed via a **MinerU-official-compatible API** (`/api/v4`, mineru.net Precise Parsing API endpoints & envelope — point existing MinerU integrations at LoomVec and swap the token; see `docs/15`).

## Documentation

Current-system docs live in [docs/](./docs) (Chinese): `docs/00-项目核心文档.md` is the architecture SSOT. Research, plans, TODOs, and decision rationale live in [planning/](./planning). Agent delivery workflow: [AGENTS.md](./AGENTS.md).

## Quick start (local development)

Prerequisites: Docker; Python 3.12 (managed by [uv](https://docs.astral.sh/uv/)); Node 20+ and [pnpm](https://pnpm.io).

For a fresh environment, run `./deploy.sh` first — it interactively collects your real AI providers (chat LLM / embedding / rerank required; VLM / CLIP optional; answer "mock" to generate an offline config instead) and writes `config/loomvec.json`. Then bring everything up:

```bash
./deploy.sh      # first run only: interactive AI provider setup + port migration + installs all Python/Node deps
./dev.sh start
```

If you skip `./deploy.sh` and run `./dev.sh start` directly, the script detects an undeployed environment (no `tmp/loomvec-deployed.stamp` marker) and automatically runs the deploy flow first on an interactive terminal; cancelling the deploy aborts the startup. Non-interactive environments (CI / piped scripts) skip auto-deploy and fall back to the offline mock bootstrap.

`./dev.sh start` brings up everything in one command: image check (auto pull/build) → infrastructure + observability stack → database init → api/agent/worker + the three front-ends (idempotent; already-running components are skipped). On an interactive terminal it then tails the FastAPI log live (Ctrl-C stops following; services keep running; use `--no-follow` to opt out). `./dev.sh logs [api|agent|worker|web|admin|ops|all]` follows any service at any time.

### Agent container sandbox (optional, P5.5a)

Agent conversations default to `provider: local` (same-host subprocess, L1, no container dependency). For production / multi-tenant deployments, enable the per-user container sandbox (L2 hard isolation, [research doc](docs/Research/01-阶段P5.5-每用户容器化隔离运行环境.md)): opt in via the `./deploy.sh` prompt (or set `agent.runtime.provider` to `docker` in `config/loomvec.json` and rerun `./deploy.sh`). The script then:

1. **Builds the sandbox image** `loomvec/agent-sandbox:stable` (self-contained dsh runtime + common toolchain, separate from the gateway image) and verifies its presence — with the image missing, the first question fails fast with `sandbox_unavailable` instead of silently degrading to L1;
2. **Creates the dedicated `agent-sandbox` network** with a fixed subnet (`AGENT_SANDBOX_SUBNET`, default `172.31.77.0/24`); the api and other services never join it;
3. **Installs the two-chain firewall** `deploy/compose/sandbox-firewall.sh` (INPUT + DOCKER-USER): sandboxes can only reach the host api port and the internet; east-west traffic, RFC1918, and cloud metadata are dropped (root required; if passwordless sudo is unavailable the script prints the manual command);
4. **Runs preflight checks**: kernel ≥ 5.13 (landlock), api listening covers the bridge source (0.0.0.0), and `bridge-nf-call-iptables=1`.

Notes: all infrastructure ports are now bound to `127.0.0.1` (do not revert; these services are unreachable from other LAN hosts — use an SSH tunnel or reverse proxy for remote access); firewall rules are lost on reboot — rerun `./deploy.sh` or refresh them periodically; sandbox env mounts carry the `:z` SELinux relabel (a no-op on non-SELinux hosts); docker group membership ≈ root (a compromised gateway equals a compromised host; sandbox containers mount no socket and hold no docker group). In CI / non-interactive shells, `LOOMVEC_SANDBOX=1 ./deploy.sh` bypasses the sandbox prompt (`0` forces skip).

| Service | URL | Notes |
|---|---|---|
| Web (end users) | http://localhost:35173 | dev login: any username |
| Admin | http://localhost:35174 | dev login defaults to `super_admin` |
| Ops | http://localhost:35175 | dev login defaults to `operator` |
| API docs | http://localhost:38080/docs | |
| Grafana | http://localhost:33002 | password in `deploy/compose/.env` (`GRAFANA_ADMIN_PASSWORD`, default `admin`) |
| Prometheus | http://localhost:39090 | |

First run generates `deploy/compose/.env`, the root `.env`, and the app-params file `config/loomvec.json` (from template `config/loomvec.example.json`, with `ai.mock=true` so the full loop runs offline; the file holds secrets and is gitignored). The MinerU image builds slowly the first time and downloads ~1–2 GB of models on first start. `./dev.sh status` shows component status; `./dev.sh stop` stops app processes and containers (data volumes are kept).

<details>
<summary>Manual step-by-step startup (equivalent to dev.sh)</summary>

```bash
# 1) Infrastructure + observability stack
cd deploy/compose && cp .env.example .env && docker compose --profile observability up -d && cd ../..

# 2) Backend (API on 38080, agent gateway on 38090 internal-only, MinerU on 38000) + pipeline worker
uv sync && cp .env.example .env
[ -f config/loomvec.json ] || cp config/loomvec.example.json config/loomvec.json  # app params are gitignored; set "mock": true for offline dev
make init-db                                        # idempotent schema init + seeds (compose PG is on 35433)
uv run uvicorn loomvec.api.main:app --reload --port 38080
uv run python -m loomvec.agent --reload
uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low

# 3) Contract → SDK (run after any API change)
uv run python scripts/export_openapi.py && pnpm install && pnpm sdk:generate

# 4) Front-ends
pnpm dev:web    # end users        http://localhost:35173
pnpm dev:admin  # platform admins  http://localhost:35174
pnpm dev:ops    # content ops      http://localhost:35175

# 5) Smoke tests / retrieval evals (API + worker must be running)
uv run python scripts/smoke/run.py
make evals        # retrieval regression (rerank A/B: --no-rerank)
```

</details>

### AI providers

AI provider settings come **only** from `config/loomvec.json` (template `config/loomvec.example.json`; the file holds secrets and is gitignored — environment variables do not apply to it). The recommended way is `./deploy.sh`, which writes it interactively (chat LLM / embedding / rerank required, VLM / CLIP optional; skipping everything sets `ai.mock=true` — a deterministic local provider that runs the full loop offline). To configure cloud providers by hand, set `ai.mock=false` in that file and fill in `base_url` / `api_key` / `model` under `ai.llm` / `ai.embedding` / `ai.rerank` etc. (OpenAI-compatible endpoints; rerank / clip also support the native `api_style: "dashscope"` protocol), then restart api/agent/worker for it to take effect (`./dev.sh stop && ./dev.sh start`). All AI calls flow through the `loomvec.core.ai` gateway; provider endpoints, keys, and model names are deployment configuration only — pointing them at self-hosted endpoints requires no code changes.

Fresh-deployment pitfall: with the manual step-by-step startup, if `config/loomvec.json` is missing, mock stays at its code default `false` and asset uploads fail at the embed step because no AI provider key/model is available (assets stuck in "failed"). Fix: run `./deploy.sh` (or set `ai.mock=true` for offline development), restart api/agent/worker, then hit "retry" on the failed assets.

## Engineering conventions

- **Contract-first**: API changes → export OpenAPI → regenerate the TS SDK → snapshot/CI checks; breaking contract changes are forbidden (v1 only).
- Every business table carries `tenant_id` / `created_at` / `updated_at`.
- Tests: `make test` (backend: pytest — integration tests need Docker and auto-skip without it; frontend: vitest). Lint: `make lint`.

## Acknowledgements

LoomVec stands on the shoulders of many outstanding open-source projects — sincere thanks to their authors and communities.

**Ingestion & agent runtime**

- [MinerU](https://github.com/opendatalab/MinerU) — PDF/document parsing engine, vendored under `third_party/mineru` (Apache-2.0 with additional terms; see `third_party/mineru/LICENSE.md` and the compliance checklist in `docs/13`). The parser chain is pluggable (MinerU → plain-text fallback by default), so alternative parsers can be swapped in without touching pipeline steps.
- [deepseek-harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) — the agent runtime behind enterprise chat (MIT); pinned as a git submodule under `third_party/deepseek-harness` for protocol reference, and consumed in production via the `deepseek-harness-sdk` package.

**Data & infrastructure**

- [PostgreSQL](https://www.postgresql.org) 18 + [Apache AGE](https://age.apache.org/) — system of record and property graph
- [Milvus](https://milvus.io) — vector database powering hybrid retrieval
- [RustFS](https://github.com/rustfs/rustfs) — S3-compatible object storage
- [Redis](https://redis.io) — task queues, cache, and event bus
- [Keycloak](https://www.keycloak.org) — OIDC identity provider for SSO

**Application stack**

- Backend: [FastAPI](https://fastapi.tiangolo.com), [Celery](https://docs.celeryq.dev), [SQLAlchemy](https://www.sqlalchemy.org), [Pydantic](https://docs.pydantic.dev)
- Frontend: [React](https://react.dev), [Vite](https://vite.dev), [Tailwind CSS](https://tailwindcss.com), [shadcn/ui](https://ui.shadcn.com)
- Observability: [Prometheus](https://prometheus.io), [Grafana](https://grafana.com), [Loki](https://grafana.com/oss/loki/), [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/)

Dependency licenses are continuously enforced in CI (`scripts/check_licenses.py`).

## License

LoomVec is licensed under the [GNU AGPL-3.0](./LICENSE). Third-party components remain under their own licenses (see `third_party/`).
