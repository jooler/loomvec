# 19-ACP 网关接入

> 智能体对话经独立服务 acp-gateway（网关）运行 claude / dsh。本文记录 loomvec 侧的现状：映射、配置、事件翻译、admin 管理面、存量数据迁移与部署。网关自身的接口与行为见网关仓库 `docs/api.md`、`docs/architecture.md`；契约快照在 [openapi.snapshot.json](../services/agent/src/loomvec/agent/gateway/openapi.snapshot.json)。

## 1. 分工

- `services/agent` 是业务适配层，对外仍是 `/internal/agent/*` 与既有 SSE 帧，`apps/web` 与 `services/api/routes/agent.py` 不感知网关。
- 网关负责进程 / 容器生命周期、会话事件日志、workspace 文件、模型凭据（`ACPGW_ANTHROPIC_API_KEY` / `ACPGW_DEEPSEEK_API_KEY`）与配额。`admin 设置 → AI 供方配置` 的 `ai.llm.*` 对 agent 不再生效。
- 代码在 `services/agent/src/loomvec/agent/gateway/`：`client.py`（httpx 客户端，端点登记在 `ENDPOINTS`）、`service.py`（映射状态机）、`orchestrator.py`（提问）、`translate.py`（事件翻译）、`history.py`（历史折叠）、`files.py`（工作区树）、`instructions.py`（AGENTS.md 模板）、`usage.py`（用量折叠）。

## 2. 配置与凭据

`config/loomvec.json` 的 `agent`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `agent.default_agent` | `claude` | 新会话用的 agent（`claude` / `dsh`）；已有会话以 `agent_session.agent` 为准 |
| `agent.gateway.url` | `http://127.0.0.1:38095` | 网关地址（admin 只读展示） |
| `agent.gateway.request_timeout_s` | `30` | 普通请求超时；SSE 流不设总超时 |
| `agent.gateway.models.claude` / `.dsh` | `null` | 各 agent 的默认模型名；`null` = agent 默认，`dsh` 缺省回落 `agent.model.name` |

`system_config`（admin「设置 → agent」写入，立即生效）：`agent.default_agent`、`agent.gateway.models.claude`、`agent.gateway.models.dsh` 覆盖文件值；两个敏感项**没有**文件回落，空 = 未配置：

| key | 用途 |
| --- | --- |
| `agent.gateway.api_key` | 租户 API Key（scope：`sessions:read/write`、`files:read/write`），对话与文件用 |
| `agent.gateway.admin_token` | 网关 `ACPGW_ADMIN_TOKEN`，仅「ACP 服务」菜单与迁移脚本使用 |

网关租户的 `settings.mcp.allowed_hosts` 必须包含 `agent.mcp.url_sandbox` 的主机；不在白名单内时网关返回 403，提问会带着网关给出的具体原因报错（`MCP 服务器地址不在租户白名单内: …`）。适配层挂载 MCP 时始终用 `agent.mcp.url_sandbox`（空则回落 `agent.mcp.url`）；网关用 `local` provider（开发）时 agent 进程在宿主上，需把它设成本机可达的地址（如 `http://127.0.0.1:38080/api/v1/mcp`）。

模型凭据在网关：dsh 经网关的 `/upstream/deepseek` 代理访问 `upstream.deepseek.base_url`，代理只转发 `content-type`、`accept` 两个请求头，`ai.llm.headers` 不适用于 agent。要求调用方自报会话的供方（如 OpenCode Go 的 `x-opencode-session`）因此不能直接作为网关的 deepseek 上游，会以 `模型调用失败：… missing x-opencode-session …` 报错。

## 3. 数据映射

| 表.列 | 含义 |
| --- | --- |
| `agent_environment.gateway_workspace_id` | 网关 workspace；首次使用时 `POST /v1/workspaces {external_ref: str(env.id)}`，409 读 `details.workspace_id` 回填，创建后写 AGENTS.md |
| `agent_session.agent` | `claude` / `dsh`，默认 `dsh`（存量） |
| `agent_session.gateway_session_id` | 网关会话；首个提问时懒创建，`cwd` = `project_path` |
| `agent_session.mcp_token_exp` | 网关会话上当前 MCP token 的过期时间；空 = 需要签发 / 续签 |
| `agent_session.replay_pending` | 迁移导入的会话、尚无成功回合：下次提问补重放前缀；默认 `false` |

建表走 `init_db._ADDITIVE_COLUMNS`（幂等 `ADD COLUMN IF NOT EXISTS`），不用 Alembic。

- **MCP token**：会话级（`issue_agent_token(..., session_id=agent_session.id.hex)`），以 `mcp_servers: [{name:"loomvec", type:"http", url: agent.mcp.url_sandbox, headers}]` 挂在网关会话上。每次提问前，`mcp_token_exp` 为空或剩余有效期低于 `max(agent.mcp.token_ttl_s // 2, 600)` 秒就重签并 `PATCH /v1/sessions/{id}`。会话 `scope_space_ids` 被修改时 `mcp_token_exp` 置空。
- **@ 文献**：本轮 @ 的 asset_id 写 Redis `agent:mentions:{agent_session_id}`（TTL 24h），`routes/mcp.py` 检索时按会话 token 里的 `agent_session_id` 读取并强制收敛。
- **审批策略**：`approvals.build_permission_policy()` 把审批矩阵翻译成网关 `PermissionPolicy`（`default: reject`；放行 `mcp__loomvec__*`、读取 / 检索 / 编辑类 kind 与矩阵里的编辑器工具名）。dsh 容器以 `danger-full-access` 运行，策略对它不生效。对 claude 策略生效：每次被询问的工具调用在网关事件里是 `permission.resolved`（`decided_by: policy`），被拒的工具以失败结束、模型据此回复。Claude Code 自己判为安全的只读命令（如 `echo`）不会向网关发审批请求，直接执行；写文件、联网类 Bash（如 `touch`、`curl`）会被询问并按策略拒绝。
- **附件**：`PUT /v1/workspaces/{ws}/files/.loomvec/uploads/{session_id}/{name}`；类型 / 大小白名单沿用 `agent.session.attachments`；`attachment_id` = 清洗后带短随机前缀的文件名。
- **工作区浏览**：`/workspace/tree` 取网关 `tree` 后在适配层过滤（隐藏段不出树、最多 500 项、带 `prefix` 时含该目录自身）；`/workspace/file` 取前 256KB 预览并带 `truncated`。

## 4. 提问流与事件翻译

`POST /internal/agent/sessions/{id}/messages`（SSE 契约不变）：先 `prepare_session`（workspace → 会话 → token），再 `POST /v1/sessions/{id}/turns {prompt, stream:true}`。网关受理阶段 4xx 在发 SSE 之前以 `error` + `done` 两帧收尾（`finish_reason:"error"`）。浏览器断开不取消网关回合；取消走 `POST /v1/sessions/{id}/cancel`（只取消回合，不杀进程）。

| 网关事件 | loomvec SSE 帧 |
| --- | --- |
| `turn.started` | `meta {session_id, message_id, model, env_id}` |
| `message.delta` | `delta {text}` |
| `thought.delta` | `reasoning {text}` |
| `tool.started` | `tool_start {call_id, tool, args}` |
| `tool.completed` | `tool_end {call_id, tool, ok, result_summary}`；检索工具另发 `citations`（Redis 旁路 `agent:citations:{session_id}:{batch_id}`，inline `ref_items` 兜底） |
| `turn.completed` | `done {message_id, answer, citations, graph_evidence, usage, finish_reason}`；有用量时先发 `usage` 帧 |
| `turn.failed`、流内 `event: error` | `error {message, code}` + `done {finish_reason:"error"}` |
| 其余（`permission.*`、上下文占用 `usage`、`plan_*`、`raw` 等） | 忽略 |

dsh 不报用量（`usage` 为空对象）；`message.delta` 原样转发，不做打字机回放。

流被切断或网关发 `event: error` 时，已收到 `turn.started` 的回合带最后的 seq 用 `GET /v1/sessions/{id}/events?after_seq=` 重连续传，最多 2 次，仍不行才按错误收尾；一个事件都没收到就断开（无法确认回合是否受理）直接报错，不重发 POST。

其他会话生命周期：创建会话带 `project_path` 时先在 workspace 里 `mkdir` 该目录；删除会话（软删）时尽力归档网关会话（网关不可用不影响本地删除）。

迁移导入的会话：网关为它新建的 agent 会话看不到导入的历史，所以迁移脚本把 `agent_session.replay_pending` 置位，`prepare_session` 把它带进 `PreparedSession.imported`，`build_prompt_blocks` 在 prompt 的第一个文本块补「此前的对话记录」前缀——取 `fold_history` 最近 20 条有文字的消息，超过 8000 字符保尾部，以 `<user>` / `<assistant>` 标签排列。标记在一个回合成功收尾（`Outcome.ok`）后清除；回合失败则保留，重试仍补（不依赖网关的会话状态：网关在 agent 会话建好、发 prompt 之前就把 `imported` 转成 `active`）。前缀是独立的文本块，只给模型看，历史展示时按块剔除。

历史（`GET .../messages`）：有 `gateway_session_id` 的读网关 `GET /v1/sessions/{id}/messages`，每个回合折成一条 user + 一条 assistant，会话统计由各回合 usage 折叠；`gateway_session_id` 为空的存量会话回落读旧 dsh JSONL。

错误映射：401 / 403 → 「ACP 网关凭据无效或权限不足」（403 且网关给了具体原因时改为「ACP 网关拒绝了请求：…」），未配置 Key → 「未配置」，404 → NotFound，429 / `quota_exceeded` → 可读文案，5xx / 连接失败 → `UpstreamUnavailableError`；`/internal/agent/health` 探测网关 `GET /healthz`，返回的 `runtimes` 恒为 0。

## 5. admin 管理面

`services/api` 的 `/api/v1/admin/acp-gateway/*`（`routes/admin/acp_gateway.py`，`services/acp_gateway_admin.py` 只允许白名单操作）代理到网关 `/admin/v1/*`，用 `agent.gateway.admin_token`：

- 读（`admin:read`）：`config`、`status`、租户列表 / 详情、租户的 API Key 列表、租户用量；
- 写：创建 / 修改租户、签发 / 吊销 API Key 仅 `super_admin`；强制停止 workspace（`admin:write`）operator 也可执行；
- 写操作要求填写原因并写审计；API Key 明文只在签发响应里出现一次，不入审计详情。

`apps/admin` 的「设置 → agent」（网关地址只读、两个敏感项只写、`default_agent`、默认模型）与「ACP 服务」菜单（租户、API Key、用量、运行时状态）。

## 6. 存量数据迁移

脚本 `scripts/migrate_agent_to_gateway.py`（逻辑在 `gateway/legacy_import.py`）把旧环境迁入网关，可重入：

```
uv run python scripts/migrate_agent_to_gateway.py --dry-run          # 只统计，不写网关 / 库
uv run python scripts/migrate_agent_to_gateway.py                    # 全量
uv run python scripts/migrate_agent_to_gateway.py --env-id <uuid>    # 只迁指定 env（可重复）
uv run python scripts/migrate_agent_to_gateway.py --skip-files       # 只迁会话
uv run python scripts/migrate_agent_to_gateway.py --skip-sessions    # 只同步文件
uv run python scripts/migrate_agent_to_gateway.py --include-archived # 同时导入已归档会话
```

前置：`agent.gateway.api_key` 与（导入会话时）`agent.gateway.admin_token` 已配置，网关可达；在没有 `LOOMVEC_AGENT_STORAGE_ROOT` 的 shell 里运行时以 `agent.storage_root` 为准（脚本开头会打印实际使用的 `storage_root`，本地目录不存在时给出警告）。退出码 `0` 全部成功、`1` 有失败（详情打到 stderr，凭据已抹掉）、`2` 前置不满足。

**顺序**：迁移须在放开用户提问之前完成。`gateway_session_id` 为空的旧会话一旦被提问，适配层会为它新建空的网关会话，旧历史随之不再可见，脚本之后也把它当作已迁移跳过；导入期间恰好被提问的会话回填落空，脚本把它记为失败（`已导入为 …，但库里该会话已绑定别的网关会话`），需人工处理。

对每个 `agent_environment`：

1. `ensure_workspace` 建 workspace 并写 AGENTS.md。`workspace/AGENTS.md` 与旧渲染的正文不同（租户 / 用户改名不影响判断）时，写成「新模板 + `## 迁移自旧版工作区的自定义内容` 附段（用户原文）」，不带模板标记（模板升级不会覆盖它）；网关上的 instructions 已被改写（无模板标记）时不覆盖；
2. 把 `{storage_root}/envs/{env_id}/workspace/` 经 files API 同步进网关：保留相对路径，只迁文件（不迁空目录）；跳过符号链接、含隐藏段的目录（`.loomvec/uploads` 除外）、隐藏文件、`.loomvec/` 直属文件和根部 `AGENTS.md`；文件以不跟随符号链接的方式打开。网关上已有同名文件则不动，不比较内容；
3. 对 `agent='dsh'` 且 `gateway_session_id` 为空的会话：解析 `dsh-home/sessions` 下全部物理段 JSONL，`POST /admin/v1/workspaces/{ws}/sessions:import`（`import_key = agent_session.id.hex`，`cwd = project_path`，`model` 取 `dsh` 的默认模型），成功后回填 `gateway_session_id`，`agent` 保持 `dsh`，`mcp_token_exp` 置空，`replay_pending` 置位。单个文件 / 会话失败只计入失败汇总，不中断其余；库里 `message_count > 0` 却找不到可导入的对话时记为失败。

`--overwrite` 用旧文件与旧 AGENTS.md 覆盖网关上的同名内容。`--dry-run` 不写网关与数据库（不建 workspace），文件数为本地全量，不与网关比对。

导入映射：每条 user 消息开一个回合（回合时间取自 JSONL 的毫秒 epoch `time`）；旧版拼在用户消息前的两段内部文字——runtime 重启后的「此前对话记录」重放前缀和项目目录说明——不进 prompt（目录由会话 `cwd` 承担）；一步内按 thought → text → tool 排列；@ 文献还原成 prompt 里的提示块；检索工具的 `ref_items` / `graph_evidence` 写入 `raw_output` 的 JSON，历史回读据此还原引用；无任何 assistant 输出的回合记为 `cancelled`，`turn/end` 为 `error` 的记为 `failed`，其余记为 `completed`；回合数超过 5000 保留最近的，单回合 items 超过 2000 保留结尾。dsh 用量不迁移。没有任何对话的会话不导入，留给首次提问时懒创建。导入的会话状态为 `imported`，首次继续对话时网关新建 agent 会话（agent 看不到导入的历史，由 `replay_pending` 机制补前缀，见 §4）。

## 7. 部署

- `deploy/compose/compose.yaml` 的可选服务 `acp-gateway`（profile `gateway`，host 网络，监听 38095，配置 `deploy/compose/acp-gateway/config.json`）；密钥与路径变量见 `deploy/compose/.env.example`（`ACPGW_SECRET_KEY`、`ACPGW_ADMIN_TOKEN`、`ACPGW_ANTHROPIC_API_KEY`、`ACPGW_DEEPSEEK_API_KEY`、`ACPGW_SANDBOX_SUBNET`、`ACPGW_STORAGE_ROOT`、`DOCKER_GID`）。`config.json` 里 `server.admin_allowed_cidrs` 限定为回环（`127.0.0.1/32`、`::1/128`），管理接口只接受宿主本机来源（api 经 `127.0.0.1` 访问）；api 不在网关同机时需改成它的来源网段。网关也可独立部署，此时只需配置 `agent.gateway.url`。
- 沙箱网段 `acpgw-sandbox` 的防火墙只用 `deploy/compose/sandbox-firewall.sh`，端口参数传「api 端口,网关端口」。

## 8. 测试

- `services/agent/tests/test_gateway_*.py`、`test_legacy_import.py`：客户端（`httpx.MockTransport`）、翻译、状态机、契约（每个 `(method, path)` 与请求体按快照 schema 校验）、迁移映射；
- `test_e2e_gateway.py`：环境具备时对着真实网关（`/home/jyy/acp-gateway`，假 agent）跑完整提问流与迁移脚本，缺 Node ≥ 22.9 / 网关仓库 / 开发 PG / Redis 时整组 skip；
- `services/api/tests/test_admin_acp_gateway.py`：admin 代理。
