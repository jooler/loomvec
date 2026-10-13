# ACP 网关（acp-gateway）方案：把 Claude Code 等 ACP agent 服务化并对外提供

> **状态**：**P0（网关核心）、P1（对外可用）已在独立仓库实现并验证**，见 §17 / §18；P2 / P3 待办。
> - 2026-10-12 初稿；同日按用户决策（§15）修订；同日 P0 实施完成，实施中的发现与偏差记在 §17；2026-10-13 P1 实施完成，记在 §18。
> - 网关在**独立仓库** `/home/jyy/acp-gateway` 实现（P0 + P1 均尚未提交，等待新会话审查），本文件留在 loomvec 仓，作为两边的共同方案。
> - 网关仓库的进度与待办见其 `planning/README.md`，现状见其 `docs/`。
> - 实施完成后：
>   - 网关现状写进网关仓库自己的文档；
>   - loomvec 侧现状迁入 `docs/`（按 AGENTS.md 规则）；
>   - 本文件只保留方案与决策记录。
>
> **读者**：在独立会话中推进实施的开发者 / agent。
> - 文中「已核实」的事实，均在 2026-10-12 对照源码或 npm / PyPI 包核对过。
> - 动手前请按 §14 末尾的命令再核一次版本。

---

## 1. 背景与目标

### 1.1 现状（已核实）

- **对话引擎**：loomvec 由 `services/agent` 网关驱动 dsh（deepseek-harness）。
  - 走的是 **dsh 私有 SDK 协议**，**不是 ACP**：`dsh --profile sdk`，stdio JSON-RPC，只有 `initialize` / `session/prompt` / `shutdown` 三个方法。
  - Python 包为 `deepseek-harness-sdk==0.1.5rc1`。
- **与 dsh 强耦合的位置**：
  - 事件翻译：`services/agent/src/loomvec/agent/sessions/orchestrator.py` 的 `_pump`，含「打字机回放」`_replay_plan`
  - 历史：`sessions/transcript.py` 读 dsh JSONL
  - MCP 挂载：`runtime/envs.py` 渲染 `cordis.patch.yml`
  - 进程启动：`runtime/docker.py` 用 SDK 私有参数 `_launch_args` 替换 argv
  - 凭据：`DEEPSEEK_*` 环境变量（真实 key 直接进容器）
- **抽象点**：只有 `runtime/manager.py` 的 `build_provider` / `runtime/provider.py` 的 `RuntimeProvider`。它只抽象「进程放在哪」（local / docker），不抽象协议。
- **services/agent 对前端提供的能力**（`routes.py`）：
  - 会话 CRUD、发消息（SSE）、取消、附件上传；
  - 项目目录（`/projects`）；
  - 工作区文件树与文件读取（`/workspace/tree`、`/workspace/file`）。
- **现有缺陷**（迁移到网关后全部解决，见 §11.5）：
  - MCP token 在运行时进程 spawn 时注入，是 per-runtime 而非 per-session：
    - 同一 env 后开的会话沿用首个会话的检索范围；
    - 进程存活超过 `agent.mcp.token_ttl_s`（3600s）后 MCP 调用 401。
  - `approvals.py` 的 `decide()` 运行时无人调用，前端也没有审批 UI。
  - 客户端断线不取消回合，也接不回来；取消只能杀整个 env 进程。
- **apps/admin**：平台运维端（super_admin / operator / auditor），目前不涉及 agent。

### 1.2 目标

1. 在独立仓库新建 **acp-gateway**，作为「agent 运行时中间层」：
   - **向下**：用 ACP 驱动 agent。P0 为 Claude Code（经 `@agentclientprotocol/claude-agent-acp`），P1 为 dsh（`dsh --profile acp`），以后可接任意 ACP agent。
   - **向上**：提供**对外服务**。多租户、API Key 鉴权、配额与用量计量，接口版本化。
2. 调用方能「像调用 LLM 一样」使用 agent，同时保留 agent 的本质能力：有状态会话、工作区文件、工具调用过程、审批、取消、断线续传。
3. 上游模型凭据**由平台统一提供**，调用方只管使用；真实 key 只在网关进程内，不进 agent 容器（§7.3 上游代理）。
4. loomvec 的对话**全部改由网关驱动**：
   - loomvec 作为网关的一个租户，和外部调用方用同一套接口；
   - `services/agent` 里 dsh 私有 SDK 路径与运行时管理代码最终删除（§11）。

### 1.3 非目标

- 不在网关里做任何 loomvec 业务语义：知识库、引用聚合、用户 / 空间权限仍归 loomvec。
- 不做计费系统（单价、账单、结算）。只做 token 计量与配额。
- 不做 OAuth2。对外鉴权只用 API Key。
- 不支持租户自带凭据（BYOK），不接 Bedrock / Vertex。
- 不做多节点。网关单节点部署，按需纵向扩容。
- 不提供 IDE 插件，不做 Web 终端。
- 不支持用 claude.ai 订阅账号登录（见 §12 合规）。

---

## 2. 总体架构

```
外部调用方（第三方应用 / 脚本 / ACP 客户端）              loomvec（租户之一）
        │                                              apps/web ─ services/api ─ services/agent（业务适配层）
        │ ① REST+SSE  ② OpenAI 兼容  ③ 原生 ACP                                        │ ① REST+SSE
        ▼                                                                               ▼
┌─────────────────────────────── acp-gateway（独立仓库，Node 22 / TypeScript，单节点）─────────────────────┐
│ 接入层   /v1/*（REST+SSE） /v1/chat/completions  /acp（ACP Streamable HTTP + WebSocket）  /admin/v1/*      │
│ 治理层   API Key 鉴权 · 租户 / 配额 / 限流 · 用量计量 · 审计 · Prometheus                                    │
│ 会话层   Session 编排 · 事件规范化与事件日志（断线续传）· 审批代理 · 取消 · MCP 注入                         │
│ 运行时层 WorkspaceRuntime：每 (workspace, agent) 一个 agent 进程（容器），惰性启动 / 复用 / 空闲回收          │
│          AgentAdapter：claude（claude-agent-acp）| dsh（dsh --profile acp）| generic                       │
│ 上游代理 /upstream/anthropic · /upstream/deepseek：校验 workspace 短时 token → 换平台 key 转发 → 计量 token  │
└───────────────┬──────────────────────────────────────────────────────────▲──────────────────────────────┘
                │ stdio ACP（JSON-RPC 2.0，换行分隔）                         │ 模型请求（只能到上游代理）
      ┌─────────▼──────────────────────────────────────┐                     │
      │ agent 容器 acpgw-{workspace_id}-{agent}          │─────────────────────┘
      │  claude-agent-acp → Claude Agent SDK  或  dsh   │
      │  cwd = {ws}/files（可指定子目录）                 │
      │  ANTHROPIC_BASE_URL / DEEPSEEK_BASE_URL → 上游代理│
      │  *_API_KEY = workspace 短时 token（非真实 key）   │
      └────────────────────────────────────────────────┘
                                                   上游代理 ──HTTPS──▶ Anthropic API / DeepSeek API（平台 key）
```

### 2.1 关键决策（ADR）

| # | 决策 | 理由 / 备选 |
|---|---|---|
| G1 | 网关在**独立仓库** `acp-gateway` 实现，**TypeScript（Node 22+）**，独立发布镜像与版本 | 对外交付边界清晰，版本节奏不受 loomvec 牵制。官方 TS SDK `@agentclientprotocol/sdk`（1.8.0）自带 ACP 服务端传输 `AcpServer`（Streamable HTTP + WebSocket）、版本路由 `AgentProtocolRouter` 和客户端 API，协议更新最先落地；`claude-agent-acp` 也要求 Node ≥22，运行时一致。Python SDK `agent-client-protocol`（0.12.1）只覆盖 stdio |
| G2 | 网关**不认识 loomvec 用户**，只认 `tenant` / `api_key` / `workspace` / `session` | 对外服务的前提。loomvec 通过「env_id ↔ workspace.external_ref」映射接入 |
| G3 | 主接口是**有会话的 REST + SSE**；OpenAI 兼容接口是简化入口；原生 ACP 端点给 ACP 客户端 | agent 有状态、工具由自己执行、有反向审批请求、回合长且可取消，这些在 OpenAI chat/completions 里都表达不了（降级说明见 §4.3） |
| G4 | 回合与 HTTP 连接**解耦**：prompt 在网关后台运行，事件写入事件日志，SSE 只是订阅 | 断线可凭 `Last-Event-ID` 续传；只有显式 `cancel` 才取消 |
| G5 | 历史以**网关事件日志**为对外事实源；agent 自身存储只用于恢复会话（claude `session/load`，dsh `session/resume`） | 事件日志与 agent 无关，换 agent 不改历史 API；不再解析 agent 私有文件格式 |
| G6 | 隔离单元是 **workspace**：文件目录归 workspace，每种 agent 在一个 workspace 内最多一个容器进程；同一 workspace 的回合默认串行 | 沿用 `services/agent` docker provider 的实践（路径恒等、只读根、cap-drop、专用网络）。Claude Code 带 Bash 等工具，**生产只允许容器隔离** |
| G7 | 审批由网关**代理**：按策略自动裁决，或转发给调用方，超时 fail-closed | 外部调用方多数不想实现审批 UI；需要时可开启转发 |
| G8 | 上游凭据**平台统一**，只存在网关进程（环境变量）。容器拿到的是 workspace 级短时 token，模型请求经网关**上游代理**换 key 转发 | 平台 key 被所有租户共用，容器内 Bash 能读环境变量，真实 key 绝不能进容器。代理同时是最准确的 token 计量点（含子 agent、压缩等所有模型调用） |
| G9 | loomvec 全部经网关驱动；`services/agent` 退化为业务适配层，迁移完成后建议并入 `services/api`（§11.4） | 进程生命周期（原 docs/14 设计决策 D1 让 agent 独立成服务的理由）已整体移交网关 |

---

## 3. 领域模型

```
tenant 1─* api_key
tenant 1─* workspace 1─* session 1─* session_event（追加写事件日志）
                                 1─* prompt_turn（回合记录：状态、用量、stop_reason）
tenant 1─* usage_ledger（token 计量）
```

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `tenant` | id, name, status(active/suspended), quotas(jsonb), default_agent | 外部客户或内部应用（loomvec 就是一个 tenant） |
| `api_key` | id, tenant_id, prefix, hash(argon2id), scopes[], permission_policy_default, expires_at, revoked_at | 明文只在创建时返回一次。scopes：`sessions:write`、`sessions:read`、`files:read`、`files:write`、`chat:completions`、`acp:connect` |
| `workspace` | id, tenant_id, external_ref, status, disk_quota_bytes, created_at, last_active_at, archived_at | `external_ref` 给调用方存自己的 id（loomvec 存 env_id），同租户内唯一 |
| `session` | id（网关生成）, workspace_id, agent(claude/dsh), agent_session_id（ACP 返回）, cwd（workspace 内相对路径）, title, model, effort, mode, permission_policy, mcp_servers(jsonb，header 值加密), system_prompt_append, status(active/archived/imported), created_at, last_active_at | 对外只暴露网关 id。`agent_session_id` 仅内部用于恢复会话 |
| `prompt_turn` | id, session_id, seq_start, seq_end, status(running/completed/cancelled/failed), stop_reason, usage(jsonb), error, started_at, ended_at | 一次 prompt 对应一行 |
| `session_event` | session_id, seq（会话内单调递增）, turn_id, type, data(jsonb), created_at | 规范化事件（§5）。大块工具输出截断后转存到 `{ws}/.gateway/outputs/` |
| `usage_ledger` | tenant_id, workspace_id, session_id, turn_id, agent, model, input / output / cache_read / cache_write tokens, source(upstream/acp), at | 配额依据与用量报表。`source=upstream` 为上游代理实测，作为配额口径 |

存储：
- **PostgreSQL**：网关自有 database。部署时可以和 loomvec 共用 PG 实例，但不共享 schema，也不互相引用。迁移工具在网关仓库内（建议 drizzle-kit 或 kysely + 迁移脚本）。
- **Redis**：限流与事件广播。

---

## 4. 对外接口

统一约定：
- 前缀 `/v1`；鉴权 `Authorization: Bearer acpgw_<key>`；所有响应带 `X-Request-Id`。
- 错误体为 `{error:{code, message, details?}}`。code 是稳定枚举，如 `quota_exceeded`、`workspace_busy`、`agent_unavailable`、`permission_timeout`、`path_forbidden`。
- OpenAPI 3.1 由代码生成（建议 Fastify + TypeBox / zod），产物 `openapi.json` 随每个版本发布。网关仓库同时发布 TS 客户端。loomvec 侧用薄 httpx 客户端，以 OpenAPI 快照做契约测试（§11.2）。

### 4.1 REST + SSE（主接口）

**Workspace 与文件**

```
POST   /v1/workspaces                      {external_ref?, metadata?}                → Workspace
GET    /v1/workspaces?external_ref=        → 列表
GET    /v1/workspaces/{ws}                 → 含各 agent 进程状态（idle/running/stopped）、磁盘用量
DELETE /v1/workspaces/{ws}                 → 归档（停进程，目录按保留策略清理）

GET    /v1/workspaces/{ws}/tree?path=&depth=     → 目录树（默认隐藏 .gateway/ 与 agent home）
GET    /v1/workspaces/{ws}/files/{path}          → 下载；?stat=1 只返回元信息
PUT    /v1/workspaces/{ws}/files/{path}          （二进制上传，自动建父目录，受磁盘配额与单文件大小限制）
POST   /v1/workspaces/{ws}/dirs/{path}           → mkdir -p
DELETE /v1/workspaces/{ws}/files/{path}?recursive=1
PUT    /v1/workspaces/{ws}/instructions          {content}   # 写 files/AGENTS.md（各 agent 如何读取见 §7.2）
```

所有路径相对于 `files/`，并做 realpath 校验：拒绝 `..`、拒绝越界的符号链接，回 `path_forbidden`。

**Session**

```
POST   /v1/workspaces/{ws}/sessions
       {
         agent?: "claude" | "dsh",             # 缺省取 tenant.default_agent
         cwd?: "projects/foo",                 # workspace files/ 下的相对目录，缺省为 files/ 根
         model?: string,                       # → ACP config option "model"
         effort?: string,                      # → claude "effort" / dsh "reasoning_effort"（agent 支持时）
         mode?: "default|acceptEdits|plan|auto|bypassPermissions",  # 仅 claude；dsh 无 modes
         permission_policy?: PermissionPolicy, # §6
         mcp_servers?: [{name, type:"http"|"sse"|"stdio", url?, headers?:[{name,value}], command?, args?, env?}],
                                               # dsh 不支持 sse；外部租户默认禁 stdio（§12）
         system_prompt_append?: string,        # 仅 claude：→ _meta.systemPrompt.append
         tools?: string[] | "preset",          # 仅 claude：→ _meta.claudeCode.options.tools
         title?, metadata?
       }                                       → Session
GET    /v1/workspaces/{ws}/sessions?status=&cursor=
GET    /v1/sessions/{id}                       → Session（含 config options 当前值、可用 modes / models）
PATCH  /v1/sessions/{id}                       {title?, model?, effort?, mode?, mcp_servers?}
                                               # model/effort/mode → set_config_option / set_mode；
                                               # 改 mcp_servers 时，下一回合前重建 agent 会话（§7.1 会话恢复）
DELETE /v1/sessions/{id}                       → 归档（ACP session/close，若 agent 支持）

POST   /v1/sessions/{id}/turns                 {content: ContentBlock[], stream?: true}
       stream=true（默认）→ text/event-stream，先发 turn.started，直到 turn.completed / turn.failed
       stream=false       → 202 {turn_id}，之后用 events 接口订阅，或轮询 turn
GET    /v1/sessions/{id}/turns/{turn_id}       → 回合状态、stop_reason、用量、最终文本
POST   /v1/sessions/{id}/cancel                → ACP session/cancel；回合以 stop_reason=cancelled 结束
GET    /v1/sessions/{id}/events?after_seq=N    → SSE；支持 Last-Event-ID 续传；follow=false 时回放完即断开
GET    /v1/sessions/{id}/messages?cursor=      → 由事件日志折叠出的消息视图（user / assistant / tool），分页
POST   /v1/sessions/{id}/permissions/{request_id}   {option_id}
```

- `ContentBlock` 与 ACP 一致：`text` / `image{data(base64), mimeType}` / `resource_link{uri,name}` / `resource{uri,text|blob}`。
  - 网关按 agent 的 `promptCapabilities` 校验。claude 为 `image: true`、`embeddedContext: true`；dsh 的图片只支持 PNG / JPEG / WebP / GIF，且要求所选模型支持图片。
  - 工作区内的文件用 `resource_link{uri:"file://<workspace 绝对路径>"}` 引用。网关接受 `workspace:///相对路径` 简写，并在转发前改写成绝对路径。
- 并发：同一 workspace 同时只跑 `max_concurrent_turns_per_workspace`（默认 1）个回合，超出返回 `409 workspace_busy`；带 `?queue=true` 时进入队列。

### 4.2 原生 ACP 端点（给 ACP 客户端）

```
POST/GET/DELETE /acp?workspace={ws}&agent={claude|dsh}   # ACP Streamable HTTP（POST 发客户端消息，GET 开 SSE 收服务端消息）
GET             /acp?workspace={ws}&agent=...            # Upgrade: websocket
```

用 TS SDK 的 `AcpServer`（`handleRequest` / `prepareWebSocketUpgrade`）实现。网关对外扮演 agent，对内扮演 client，是**有改写的代理**，不是透传：

| 方向 | 消息 | 网关处理 |
|---|---|---|
| C→G | `initialize` | 网关自己应答 `agentCapabilities`，取后端 agent 能力的子集 |
| C→G | `initialize.clientCapabilities.fs` / `terminal` | **剥离**，不转给后端。远端客户端的文件系统不是工作区，agent 必须用自己在容器内的工具 |
| C→G | `session/new` | 客户端给的 `cwd` 必须落在 workspace 内，否则改写为 workspace 根；`mcpServers` 先过租户白名单，再合并租户强制挂载的服务器 |
| C→G | `session/prompt` / `cancel` / `load` / `resume` / `set_mode` / `set_config_option` | 鉴权、配额检查后转发；`load` / `resume` 只允许本租户的会话 |
| G→C | `session/update` | 转发，同时写入事件日志（REST 侧也能看到同一会话） |
| A→G | `session/request_permission` | 先按策略裁决（§6）；策略为 `ask` 时转给 ACP 客户端，超时 fail-closed |
| A→G | `fs/*`、`terminal/*` | 不应出现（能力已剥离）；出现即返回 method_not_found |
| 任意 | `_meta` 厂商扩展 | 默认透传；租户策略可剥离 |

ACP 客户端连接 `/acp` 时，需要支持「Bearer header 认证的远程 agent」。截至核实时，TS SDK 的 `createHttpStream` 支持自定义 headers；各编辑器对远程 agent 的支持度不一，实施时逐个确认。

### 4.3 OpenAI 兼容接口（简化入口）

```
POST /v1/chat/completions
  model: "agent:claude" | "agent:claude:<上游模型名>" | "agent:dsh" | "agent:dsh:<上游模型名>"
  messages: [...]
  stream: bool
  user?: string                         # 映射为 workspace.external_ref（不存在则自动创建）
  头 X-ACPGW-Session-Id?: 续用已有会话；不给则每次新建临时会话（回合结束即归档）
```

model 名不用「claude-code」字样，原因见 §12 品牌条款。

语义与降级（必须写进对外文档）：
- **只取最后一条 user 消息**作为本回合 prompt，之前的 `messages` 忽略。没有 session 头时，把前文拼成一段上下文前缀（有损）。
- `tools` / `tool_choice` / `functions` **不支持**（agent 自己执行工具），传了返回 400。
- 输出：`delta.content` 只含 agent 文本，工具过程默认不输出。带 `X-ACPGW-Verbose: 1` 时，用 `delta.reasoning_content` 输出思考和工具摘要。
- 审批固定用 API Key 上的默认策略，且**不能**是 `ask`（这个接口没有反向通道）。
- `usage` 取回合用量。`finish_reason` 由 ACP `stopReason` 映射，原值放在 `X-ACPGW-Stop-Reason` 头：
  - `end_turn` → `stop`
  - `max_tokens` → `length`
  - `refusal` → `content_filter`
  - `cancelled` / `max_turn_requests` → `stop`
- 同时提供 `GET /v1/models`，列出可用的 `agent:*`。

### 4.4 管理接口 `/admin/v1/*`

供平台运维使用（loomvec `apps/admin` 经 services/api 代理调用，见 §11.6）：
- 租户 CRUD / 暂停；API Key 签发 / 吊销
- 配额设置；用量查询与导出（按租户 / workspace / 天聚合）
- workspace 强制停止 / 清理
- 历史导入：`POST /admin/v1/workspaces/{ws}/sessions:import`，loomvec 迁移旧会话用（§11.3）
- 运行时状态：活跃容器数、各 agent 适配器版本、上游连通性探测

鉴权：独立的管理 token（`ACPGW_ADMIN_TOKEN`）+ 来源 IP 限制，只在内网暴露。

---

## 5. 事件模型（REST SSE 与事件日志共用）

SSE 帧格式为 `id: <seq>`、`event: <type>`、`data: <json>`。每个 data 都带 `session_id`、`turn_id`、`seq`、`ts`。

| 网关事件 | 来源（ACP v1 `session/update`.`sessionUpdate`，已核实存在于 SDK 1.8.0 schema） | data 主要字段 |
|---|---|---|
| `turn.started` | 网关在发出 `session/prompt` 前产生 | prompt 摘要 |
| `message.delta` | `agent_message_chunk`（content.text） | `text` |
| `thought.delta` | `agent_thought_chunk` | `text` |
| `tool.started` | `tool_call` | `tool_call_id, title, kind(read/edit/delete/move/search/execute/think/fetch/switch_mode/other), status, raw_input, locations[]`。claude 的原始工具名在 `_meta.claudeCode.toolName`，规范化为 `tool_name` |
| `tool.updated` | `tool_call_update`（status 为 `pending` / `in_progress`） | 增量 `content[]`（text / diff / terminal） |
| `tool.completed` | `tool_call_update`（status 为 `completed` / `failed`） | `status, content[], raw_output`。超过 64KB 时截断，全文转存，给出 `output_ref` |
| `plan` | `plan`（仅 claude） | `entries[{content, priority, status}]` |
| `usage` | `usage_update` + `session/prompt` 响应中的 usage | token 分项 |
| `mode.changed` / `config.changed` | `current_mode_update` / `config_option_update` | 当前值 |
| `commands` | `available_commands_update`（仅 claude） | 斜杠命令列表 |
| `session.info` | `session_info_update` | 标题等 |
| `permission.requested` | `session/request_permission`（策略为 `ask` 时） | `request_id, tool_call, options[{option_id, name, kind}], expires_at` |
| `permission.resolved` | 网关裁决或调用方应答 | `request_id, option_id, decided_by(policy/caller/timeout)` |
| `turn.completed` | `session/prompt` 响应 | `stop_reason(end_turn/max_tokens/max_turn_requests/refusal/cancelled), usage, final_text` |
| `turn.failed` | 进程崩溃 / 超时 / 上游错误 | `code, message, retryable` |
| `session.context_reset` | 会话恢复失败、改用新 agent 会话时（§7.1） | `reason` |
| `raw` | 其他或未知的 `sessionUpdate` | 原样透传（订阅时带 `?include_raw=1` 才推送） |

规则：
- 未知的 update 类型（ACP v2 草案、厂商扩展）一律进 `raw`，不能让网关崩溃或丢帧。
- `message.delta` 落库时按 200ms 合并后写一行，避免事件日志爆量；SSE 实时推送不合并。
- **粒度差异**：dsh 的 ACP 模式只推送「已提交的完整消息与思考」，没有逐 token 增量（dsh-acp README 明确不上线原始 provider delta）。所以 dsh 会话的 `message.delta` 往往一条就是一整段。网关**不伪造**打字机效果，原样转发；调用方需能处理整段到达的文本（loomvec 直接整段显示，见 §11.2）。

---

## 6. 审批（permission）

`PermissionPolicy` 可以在 API Key、session 两级设置。session 优先，但不能比 API Key 更宽：

```jsonc
{
  "default": "reject" | "allow" | "ask",
  "rules": [
    {"match": {"kind": ["read","search","think","fetch"]}, "decision": "allow"},
    {"match": {"kind": ["edit","delete","move"]},          "decision": "allow"},    // 在工作区内
    {"match": {"tool_name": "Bash", "command_regex": "^(ls|cat|git status)\\b"}, "decision": "allow"},
    {"match": {"tool_name": "mcp__*"},                     "decision": "allow"}
  ],
  "ask_timeout_s": 120                                   // 超时按 reject_once 处理
}
```

- **裁决到 ACP 选项的映射**：在 `options` 中选 kind 为 `allow_once` / `reject_once` 的项。网关**永不自动选** `allow_always`，除非调用方显式应答。dsh 只提供一次性 allow / reject 选项（已核实 dsh-acp README）。
- **claude 的 session mode 配合**：
  - 默认用 `default` 模式：所有敏感操作都会发 `request_permission`，由网关策略统一裁决，便于审计。
  - `bypassPermissions` 只在容器隔离下允许。适配器在 **root 用户且未设 `IS_SANDBOX`** 时会禁用 bypass（已核实 `permissions/modes.js`），容器应以非 root 运行。
  - `AskUserQuestion`：客户端没有声明 form elicitation 能力时，适配器会禁用它（已核实）。网关 P0–P2 不声明 elicitation，P3 再考虑以 `question.requested` 事件转发。
- **dsh**：没有 modes，`mode` 参数忽略；审批完全由网关策略决定。

---

## 7. 运行时、工作区与上游代理

### 7.1 进程模型

- **WorkspaceRuntime**：
  - 键为 `(workspace_id, agent)`，每键最多一个 agent 进程。一条 ACP 连接上承载该 workspace 内该 agent 的多个 session（claude 适配器与 dsh-acp 都支持一连接多会话）。
  - 惰性启动；空闲 `idle_timeout_s`（默认 900）后 `shutdown`。
  - 上限 `max_active_runtimes`，超限按 LRU 回收空闲进程。
- **会话恢复**（进程被回收或 `mcp_servers` 变更后，下一次 prompt 时执行）：
  1. `initialize`，然后按能力恢复：
     - claude 有 `loadSession`，用 `session/load(agent_session_id, cwd, mcpServers)`；
     - dsh 不支持 load，用 `session/resume`（dsh-acp 已支持跨进程重启 resume）。
  2. 恢复失败时，新建 agent 会话，写一条 `session.context_reset` 事件，不再做文本回放拼接。
- **崩溃**：
  - 进程退出时，进行中的回合记 `turn.failed{code:"runtime_crashed", retryable:true}`；
  - 30s 内连续崩溃 3 次，该 `(workspace, agent)` 熔断 5 分钟。
- **RuntimeProvider**（参考 loomvec `services/agent/runtime/` 的实现思路，在网关仓库内重写，不跨仓引用代码）：
  - `docker`：生产默认。容器名 `acpgw-{workspace_id}-{agent}`；参数沿用 loomvec 现有实践：
    - `--rm -i`、`--user <非 root uid>`
    - `--cpus/--memory/--pids-limit`
    - `--cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp --tmpfs $HOME`
    - 只 bind mount 该 workspace 目录，路径恒等
    - 专用网络：默认只放行网关上游代理地址和租户白名单 MCP 地址，其余出网按配置开关（参考 loomvec `deploy/compose/sandbox-firewall.sh`）
  - `local`：仅开发与测试用，启动时打 WARN；生产配置下拒绝启动。

### 7.2 工作区目录

```
{storage_root}/workspaces/{workspace_id}/
├── files/                 # 调用方可见的工作区；agent cwd = files/ 或其子目录
│   ├── AGENTS.md          # instructions 接口写入
│   └── .claude/skills/    # 租户可下发的 claude skills
├── home/
│   ├── claude/            # CLAUDE_CONFIG_DIR：Claude 会话 JSONL、settings.json（网关渲染）
│   └── dsh/               # DSH_HOME：dsh 会话持久化、settings、cordis.patch.yml（网关渲染）
└── .gateway/              # 网关私有（工具输出转存等），不挂给 agent
```

**指令文件**：
- dsh 原生读取 `$DSH_HOME/AGENTS.md` 和项目根到 cwd 的 AGENTS.md / CLAUDE.md 链，直接用 `files/AGENTS.md`。
- Claude Code 不读 AGENTS.md。网关在 claude `session/new` 时，把 `files/AGENTS.md` 的内容拼到 `_meta.systemPrompt.append` 里（再接上调用方给的 `system_prompt_append`）。这样不往工作区写 CLAUDE.md，避免和 dsh 的指令链冲突。

**其他**：
- 磁盘配额：P0 定期 `du` 统计，超限拒绝新回合与上传；P3 用 XFS project quota 或独立卷。
- 保留策略：归档 workspace 在 `retention_days` 后删除目录；事件日志按租户配置保留。

### 7.3 上游代理

```
容器内:  ANTHROPIC_BASE_URL = http://<网关内网地址>/upstream/anthropic
         ANTHROPIC_API_KEY  = <workspace 短时 token>
         DEEPSEEK_BASE_URL  = http://<网关内网地址>/upstream/deepseek
         DEEPSEEK_API_KEY   = <workspace 短时 token>
网关:    校验 token（HMAC 签名，含 tenant / workspace / agent / exp，TTL 默认 24h，进程重启时换新）
         → 检查租户配额（超限直接 429）
         → 去掉调用方的认证头，换平台 key 转发（流式透传）
         → 从响应（含 SSE 流末尾的 usage）提取 token 写 usage_ledger(source=upstream)
```

- 只转发白名单路径：Anthropic `/v1/messages`、`/v1/messages/count_tokens`、`/v1/models`；DeepSeek `/chat/completions`、`/models`。其他路径返回 404。
- 每个租户可设上游 RPM / 并发上限，防止单租户占满平台 key 的额度。
- 平台 key 来自网关环境变量（§10），只在网关进程内使用。
- 待 P0 冒烟验证：
  - Claude Agent SDK 在自定义 `ANTHROPIC_BASE_URL` + 非官方格式 key 下能正常工作（适配器源码里有 `ANTHROPIC_BASE_URL` 与 gateway 认证方式，预期可行）；
  - dsh 的 `deepseek-official` provider 读取 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`（loomvec 现有 `runtime/local.py` 就是这样注入的）。

---

## 8. Agent 适配器

`AgentAdapter` 接口：

```ts
interface AgentAdapter {
  id: "claude" | "dsh" | "generic";
  image(cfg): string;                                  // 容器镜像
  command(cfg): string[];                              // 容器内启动命令
  env(ws, upstreamToken): Record<string,string>;       // 上游代理地址与短时 token、配置目录等
  prepareHome(ws): Promise<void>;                      // 渲染 home/<agent>/ 下的配置
  sessionNew(req, ws): acp.NewSessionRequest;          // 组装 session/new（含 _meta）
  resume: "load" | "resume" | "none";                  // 会话恢复方式
  normalizeToolName(update): string | undefined;
}
```

### 8.1 claude（P0）

已核实：`@agentclientprotocol/claude-agent-acp@0.89.1`，依赖 `@agentclientprotocol/sdk@1.8.0`、`@anthropic-ai/claude-agent-sdk@0.3.293`，`engines.node >=22`。旧包 `@zed-industries/claude-code-acp`、`@zed-industries/claude-agent-acp` 已废弃。

- **启动**：可执行文件 `claude-agent-acp`，走 stdio。
- **initialize 能力**：
  - `loadSession: true`
  - `promptCapabilities.image/embeddedContext: true`
  - `mcpCapabilities.http/sse: true`
  - 另有 `providers/*`、`auth.logout`、`_meta.steering` 等扩展
- **环境变量**：
  - `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`：指向上游代理，用短时 token
  - `ANTHROPIC_MODEL`：默认模型
  - `CLAUDE_CONFIG_DIR`：设为 `home/claude`
  - `MAX_THINKING_TOKENS`
- **session modes**：`default`(Manual) / `acceptEdits` / `plan` / `auto` / `bypassPermissions`（后者受 root 限制）。
- **config option id**：`mode`、`model`、`effort`、`fast`。
- **session/new 的 `_meta`**：
  - `systemPrompt`：字符串则整体替换；对象时以 preset `claude_code` 为基础，可带 `append`
  - `claudeCode.options`：透传给 Agent SDK 的选项，如 `tools`、`allowDangerouslySkipPermissions`、`resume`
  - `disableBuiltInTools`（旧写法）
  - `additionalRoots`
- **镜像** `acpgw/agent-claude`：
  - 从 `node:22-slim` 起步，加 `git`、`ripgrep`、`python3`、`jq` 等常用工具，`npm i -g @agentclientprotocol/claude-agent-acp@<锁定版本>`；
  - 非 root 用户；
  - 关闭自更新与遥测（实施时按所锁版本确认具体环境变量）。
- 网关**锁定适配器版本**；升级先过 CI 真实冒烟（§13），再改锁定值。

### 8.2 dsh（P1）

已核实（loomvec 仓 `third_party/deepseek-harness/packages/acp/acp/README.md`、`packages/bundle/acp-app/cordis.patch.yml`）：

- **启动**：`dsh --profile acp`，标准 ACP v1，「automation-only」。
- **支持的方法**：
  - `initialize`、`authenticate`（直接成功）
  - `session/new`、`session/list`、`session/resume`、`session/close`
  - `session/set_config_option`（`model`、`reasoning_effort`）
  - `session/prompt`（每会话同时一个）
  - `session/cancel` / `$/cancel_request`
- **服务端推送**：`session/update`（已提交的消息与思考、工具生命周期、配置变更、上下文用量）、`session/request_permission`（一次性 allow / reject）。
- **不支持**：`session/load`、modes、commands、plans、terminals、client fs、elicitation、SSE MCP（只支持 stdio 和 HTTP MCP）、额外目录。
- **模型**：provider / model 配在 `@deepseek-ai/dsh-acp` 插件的 config 里（acp-app 默认 `deepseek-official` / `deepseek-v4-flash`），会话内可用 `set_config_option` 切换。网关在 `home/dsh/cordis.patch.yml` 中渲染默认 provider / model。
- **镜像** `acpgw/agent-dsh`：沿用 loomvec `deploy/compose/sandbox/Dockerfile` 的做法。基于 `python:3.12-slim`，`pip install deepseek-harness-sdk==<锁定版本>`（wheel 自带 dsh 原生可执行文件，无需 Node），入口 `dsh`，命令 `--profile acp`。
- **实施时必须先验证的三点**（未核实）：
  1. 所锁 `deepseek-harness-sdk` wheel 内置的 dsh 是否已带 `--profile acp`。loomvec 当前锁定的是 0.1.5rc1，vendored 源码有 acp 包，但不代表该 wheel 版本有。
  2. acp profile 下会话持久化的 root 如何配置。sdk profile 要求在 cordis patch 里显式配置 `session-persistence-jsonl.root`，参考 loomvec `runtime/envs.py` 的 `CORDIS_PATCH`。
  3. 图片 prompt 依赖的「durable attachment store」在 acp profile 下是否默认挂载。

### 8.3 generic

配置任意 ACP 命令与镜像，供以后接 Codex / Gemini CLI 等。

---

## 9. 治理

- **鉴权**：API Key（argon2id 哈希，可按前缀检索）；scopes 校验到路由级。`/acp` 需要 `acp:connect`。
- **限流**：每个 Key 的 RPS（令牌桶，Redis）。
- **配额**（每租户，存 `tenant.quotas`）：
  - 并发 workspace 数、并发回合数、磁盘上限
  - 日 / 月 token 上限，以上游代理实测的 `usage_ledger(source=upstream)` 为口径
  - 上游 RPM / 并发上限
  - 每回合最长时长 `max_turn_seconds`（默认 1800）：到时先 `cancel`，再判失败
- **计量**：
  - 上游代理逐请求记账，这是配额口径；
  - 回合结束时按 ACP usage 汇总写 `prompt_turn.usage`，用于展示。
  - 不计算金额。
- **审计**：管理接口操作、API Key 签发与吊销、审批裁决（含自动裁决）都写审计日志。
- **可观测**：
  - Prometheus 指标：活跃 runtime、回合时长直方图、stop_reason 计数、审批结果计数、上游请求与错误、各租户 token、容器启动耗时。
  - 结构化日志，带 `tenant_id / workspace_id / session_id / turn_id / request_id`。
  - 网关仓库提供 `/metrics` 和 Grafana 面板 JSON。部署方（如 loomvec 的监控栈）自行抓取。

---

## 10. 配置

网关仓库根目录的 `config.example.json`（本机实值放 `config.json`，不入库），敏感项走环境变量：

```jsonc
{
  "server": {"host": "0.0.0.0", "port": 38095, "public_base_url": "", "internal_base_url": "http://acp-gateway:38095"},
  "storage_root": "/data/acp-gateway",
  "runtime": {
    "provider": "docker",                // docker | local（仅开发）
    "idle_timeout_s": 900,
    "max_active_runtimes": 32,
    "max_concurrent_turns_per_workspace": 1,
    "max_turn_seconds": 1800,
    "docker": {"network": "acpgw-sandbox", "cpus": 2, "memory": "4g", "pids_limit": 512, "uid": 10001, "egress": "upstream-only"}
  },
  "agents": {
    "claude": {"image": "acpgw/agent-claude:<ver>", "command": ["claude-agent-acp"], "default_model": ""},
    "dsh":    {"image": "acpgw/agent-dsh:<ver>",    "command": ["--profile", "acp"], "default_provider": "deepseek-official", "default_model": ""}
  },
  "upstream": {
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ACPGW_ANTHROPIC_API_KEY"},
    "deepseek":  {"base_url": "https://api.deepseek.com",  "api_key_env": "ACPGW_DEEPSEEK_API_KEY"},
    "token_ttl_s": 86400
  },
  "events": {"max_inline_tool_output_bytes": 65536, "delta_coalesce_ms": 200}
}
```

环境变量：
- `ACPGW_DATABASE_URL`、`ACPGW_REDIS_URL`
- `ACPGW_ANTHROPIC_API_KEY`、`ACPGW_DEEPSEEK_API_KEY`：平台统一上游 key
- `ACPGW_SECRET_KEY`：上游短时 token 的 HMAC 密钥，同时用于加密 session 中 MCP header 的值。生产必填，缺失则拒绝启动
- `ACPGW_ADMIN_TOKEN`

---

## 11. 与 loomvec 的集成与迁移

### 11.1 角色划分（迁移完成后）

| 职责 | 归属 |
|---|---|
| 用户与租户、空间权限、知识库与 MCP endpoint、引用聚合、env ↔ workspace 映射、会话 UI 元数据（标题、范围、预览）、`/api/v1/agent/*` 对前端的 SSE 契约 | loomvec |
| agent 进程、隔离、ACP、审批代理、事件日志与历史、上游凭据与计量、工作区文件 | acp-gateway |

### 11.2 L1：services/agent 改为网关客户端

1. **客户端**：新增 `services/agent/src/loomvec/agent/gateway/`。
   - 用薄 httpx 客户端，只覆盖用到的约 15 个端点；
   - 入库一份网关 `openapi.json` 快照，契约测试校验客户端请求与快照一致；
   - 网关升级时更新快照。
2. **配置**：loomvec `config/loomvec.json` 新增 `agent.gateway.{url}`，API Key 走 `system_config` 敏感项 `agent.gateway.api_key`（admin 可配置）。`agent.default_agent`（`claude` / `dsh`）决定新会话用哪个 agent。
3. **映射**：
   - `agent_environment.id` ↔ 网关 `workspace.external_ref`。首次使用时创建 workspace，回填新列 `agent_environment.gateway_workspace_id`。
   - `agent_session` 新增列 `agent`、`gateway_session_id`（Alembic 迁移）。
   - 会话的 `project_path` 映射为网关 session 的 `cwd`（`projects/<name>`），不再往 prompt 里拼项目目录前缀。
4. **AGENTS.md**：原来由 `runtime/envs.py` 渲染两份 AGENTS.md。改为 services/agent 在创建 workspace 和模板版本变更时，调用 `PUT instructions` 写入。
5. **事件翻译**（网关事件 → 现有前端 SSE 契约；前端 [apps/web/src/agent/agent.ts](../apps/web/src/agent/agent.ts) 不改）：
   - `turn.started` → `meta`
   - `message.delta` → `delta`
   - `thought.delta` → `reasoning`
   - `tool.started` → `tool_start`
   - `tool.completed` → `tool_end`（检索工具另发 `citations`）
   - `usage` → `usage`
   - `turn.completed` → `done`
   - `turn.failed` → `error` + `done`
   - dsh 会话的 `message.delta` 往往整段到达，翻译层原样转成一个 `delta` 帧，前端一次性显示，不做打字机回放（`_replay_plan` 随 L3 删除）。
6. **MCP**：建会话时由 services/agent 签发**会话级** token（`issue_agent_token`，绑定 `agent_session_id` 与 `scope_space_ids`），作为 `mcp_servers: [{name:"loomvec", type:"http", url, headers:[{name:"Authorization", value:"Bearer …"}]}]` 传给网关。
   - 每回合发送前，若 token 剩余有效期 < 回合上限时长，先 `PATCH mcp_servers` 换新。
   - MCP 地址用 `agent.mcp.url_sandbox` 语义：从网关容器网络可达的 loomvec api 地址，网关侧要把它加入出网白名单。
7. **引用**：`mcp__loomvec__search_knowledge` 的结果继续走 Redis 侧通道。token 已是会话级，key `agent:citations:{agent_session_id}:*` 能对上；inline `ref_items` 作兜底。
8. **附件**：上传改为调用网关 `PUT files/.loomvec/uploads/{session_id}/{name}`；prompt 中图片用 image block，其他文件用 `resource_link`。保留现有类型与大小白名单。
9. **项目与文件浏览**：`/projects`、`/workspace/tree`、`/workspace/file` 改为转调网关的 tree / files / dirs 接口。
10. **取消与审批**：取消改为调网关 `cancel`。审批策略在建会话时传入（由 `approvals.py` 的现有矩阵翻译成 `PermissionPolicy`：`mcp__loomvec__*` 和编辑类允许，其余拒绝）。前端审批交互另立需求。

### 11.3 L2：数据迁移

一次性迁移脚本放在 loomvec 仓 `scripts/`，可重入：
1. 对每个 `agent_environment`：创建网关 workspace（`external_ref = env_id`），把 `{agent.storage_root}/envs/{env_id}/workspace/` 的内容同步到网关 workspace 的 `files/`（同机部署时 rsync，否则走 files API）。
2. **旧会话历史**：
   - 用现有 `transcript.py` 把每个 dsh 会话解析成规范化消息；
   - 调用网关 `sessions:import` 导入事件日志，状态为 `imported`；
   - 导入的会话可以继续对话：首个新回合新建 agent 会话，并记 `session.context_reset`。
   - 旧会话不需要保留时，可跳过这一步，直接归档。
3. 回填 `agent_session.agent = "dsh"`、`gateway_session_id`。

### 11.4 L3 / L4：删除与收敛

- **L3 删除**（迁移验证通过后）：
  - `services/agent` 的 `runtime/`（provider、manager、local、docker、envs）、`sessions/orchestrator.py` 中的 dsh 部分、`sessions/transcript.py`、`sessions/usage.py`；
  - 依赖 `deepseek-harness-sdk`；
  - loomvec 的 `deploy/compose/sandbox/`、`agent-sandbox` 网络、`agent_envs` 卷；
  - 配置段 `agent.runtime.*` / `agent.model.*` / `agent.sandbox.*`；
  - 相关测试。
  - 同步改写 `docs/14-阶段P5-dsh智能体对话与企业工作环境.md` 和 `docs/00-项目核心文档.md` 中的架构描述。
- **L4 收敛**（建议）：`services/agent` 剩下的只是一层业务适配（映射、元数据、token 签发、事件翻译、引用）。它独立成服务的理由（docs/14 D1：进程生命周期、单写者）已不存在，建议并入 `services/api` 的 `routes/agent.py`，删除 agent 服务与内部 token。

### 11.5 迁移后解决的现有问题

- per-session MCP token：范围串用和 1 小时后 401 两个问题都解决；
- 审批真正生效（网关策略）；
- 取消只取消回合，不再杀进程；
- 断线续传；
- 平台 key 不再进入容器。

### 11.6 apps/admin

- **设置 → agent**：
  - 网关地址只读展示（来自配置）；
  - loomvec 自身的网关 API Key（只写）；
  - `agent.default_agent`；
  - 默认模型与审批策略。
- **新菜单组「ACP 服务」**：
  - services/api 新增 `/api/v1/admin/acp-gateway/*`，代理到网关 `/admin/v1/*`。admin 前端不直连网关，复用平台角色鉴权与审计。
  - 租户管理与 API Key 管理。签发 Key 时用现有 `SecretModal`，明文只显示一次。
  - 配额设置、用量报表（按租户 / 天）、运行时状态卡片（参考 `LocalModelsStatus`：活跃容器、适配器版本、上游连通性）。
- 写操作按 admin 惯例，走 `ReasonModal` 填原因并审计。租户、Key、配额的写操作仅 super_admin 可执行；operator 只读并可强制停止 workspace。

### 11.7 部署

- 网关仓库发布镜像：`acp-gateway`、`acpgw/agent-claude`、`acpgw/agent-dsh`。
- loomvec compose 增加可选的 `acp-gateway` 服务，引用已发布的镜像版本，并和 `api` 共享网络，便于 MCP 回调。生产可独立部署网关，loomvec 只配置 `agent.gateway.url`。
- 网关需要访问宿主 Docker（挂 docker.sock 或 `DOCKER_HOST`），这与 loomvec agent 服务现状一致。

---

## 12. 安全与合规

- **隔离**：
  - 生产只允许 docker；容器非 root、只读根、cap-drop ALL、no-new-privileges、资源上限、专用网络，出网默认只到上游代理。
  - 文件接口做 realpath 校验，拒绝越界的符号链接。
- **凭据**：
  - 平台 key 只在网关进程内，容器里是 workspace 级短时 token。token 泄露的影响限于该租户配额，且会过期。
  - session 中 MCP header 的值加密存储。
- **MCP 注入面**：
  - 外部租户的 `mcp_servers` 默认禁止 `stdio`；
  - http / sse 地址必须在租户白名单内，防止 SSRF 打内网。
- **数据**：事件日志含用户代码与输出，按租户隔离查询；提供租户级删除接口（P3）。
- **Anthropic 条款（实施前必须人工确认）**：
  - 对第三方提供服务，必须用 Anthropic API key（商业条款），不得以 claude.ai 订阅登录提供服务；
  - 产品命名与品牌须遵守 Claude Agent SDK 品牌指南（适配器 README 链接到 `platform.claude.com/docs/en/agent-sdk/overview#branding-guidelines`），对外文档、UI、model 名都不要把服务称为「Claude Code」本身；
  - DeepSeek API 的转售与对外服务条款同样需要确认。

---

## 13. 测试策略（网关仓库）

1. **单元测试**：
   - 事件规范化（每种 `sessionUpdate` 到网关事件）、审批策略匹配、OpenAI 映射；
   - 配额计数、路径校验、上游代理的 usage 提取（含流式响应）。
2. **协议契约测试**：用 TS SDK `agent()` 写一个**假 ACP agent**，可以脚本化地发 update、请求审批、模拟崩溃与超时，并可切换为「claude 能力集」或「dsh 能力集」。覆盖：
   - 回合全流程、取消、断线续传（Last-Event-ID）
   - 审批 allow / reject / ask / 超时
   - 进程回收后的 `session/load` 与 `session/resume` 恢复，以及恢复失败时的 `context_reset`
   - 未知 update 透传
3. **上游代理测试**：用假上游（Anthropic Messages / OpenAI 兼容两种格式，含 SSE）验证换 key、路径白名单、配额拒绝、计量。
4. **原生 ACP 端点测试**：用 SDK `createHttpStream` + `client()` 作为外部客户端连 `/acp`，验证能力剥离与 cwd 改写。
5. **真实 agent 冒烟**（CI 可选 job，需上游 key secret）：
   - claude 和 dsh 各跑一组：一问一答、一次文件编辑、一次 MCP 调用（接一个测试 MCP server）；
   - 全程经上游代理。
   - 升级任一适配器版本，必须先过这个 job。
6. **压测**：50 个 workspace 并发冷启动、长回合（30 分钟）稳定性、SSE 背压。

loomvec 仓（§11）：
- 网关客户端契约测试（对 OpenAPI 快照）；
- 事件翻译测试（断言前端 SSE 契约不变，沿用 `test_agent_core.py` 风格）；
- 迁移脚本测试（造 dsh 会话目录 → 导入 → 校验消息）。

---

## 14. 分期计划与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 网关核心**（网关仓库） | 仓库与 CI；Fastify + TS + PG + Redis 骨架；租户 / API Key / workspace / session / turn / event 表；docker + local provider；claude 适配器与镜像；**上游代理（anthropic）**；REST + SSE 的 session / turn / events / cancel / messages；事件规范化、事件日志与续传；审批策略（不含 `ask`）；`session/load` 恢复；并发与回合时长配额；Prometheus；假 agent 契约测试 | 用 curl 建 workspace / session，发 prompt 能看到流式 delta 与工具事件；断线重连不丢事件；cancel 生效；进程被回收后能继续对话；容器内读不到真实 key；真实 claude 冒烟通过 |
| **P1 对外可用**（网关仓库） | dsh 适配器与镜像（先完成 §8.2 的三项验证）+ 上游代理（deepseek）；files / tree / dirs / instructions；session `cwd`；token 配额与用量导出；管理接口（含 `sessions:import`）；OpenAI 兼容接口；OpenAPI 与 TS 客户端发布；对外接口文档 | 外部用 OpenAI SDK 改 base_url 即可调用；dsh 真实冒烟通过；管理接口能签发 Key、看用量、设配额 |
| **P2 loomvec 切换**（loomvec 仓） | L1 网关客户端与翻译层；Alembic 迁移；admin 设置页与「ACP 服务」菜单；L2 数据迁移脚本；切换验证后执行 L3 删除；同步改写 docs | web 对话前端无改动即可用 claude / dsh 两种 agent；旧会话历史可查；`services/agent` 中已无 dsh SDK 与运行时代码；admin 能管理租户、Key、配额、用量 |
| **P3 完整** | 原生 `/acp` 端点；审批 `ask` 转发与 web 审批 UI；elicitation / AskUserQuestion 转发；XFS 磁盘配额；租户数据删除接口；L4 并入 services/api；generic 适配器接更多 agent | Zed 等 ACP 客户端可经 `/acp` 连上并使用 |

实施前置核对（每次开工前）：

```bash
npm view @agentclientprotocol/claude-agent-acp version dependencies engines
npm view @agentclientprotocol/sdk version
pip index versions deepseek-harness-sdk
# 核对 §5 事件类型、§8.1 modes / config ids / _meta、§8.2 dsh-acp 方法面是否仍成立
```

---

## 15. 已定决策（2026-10-12 用户确认）

| # | 问题 | 决定 | 对方案的影响 |
|---|---|---|---|
| 1 | 部署形态 | 独立仓库 | G1；§10 配置、§11.7 部署、§13 测试都按独立仓库组织；loomvec 只通过 HTTP 和 OpenAPI 快照依赖网关 |
| 2 | 对外鉴权 | 只需要 API Key | §9；不做 OAuth2 |
| 3 | 计费 | 不需要 | 只做 token 计量与配额，不算金额（§3、§9） |
| 4 | dsh 去向 | 全部由网关驱动 | G9；dsh 适配器升为 P1 必做（§8.2）；loomvec 迁移与删除见 §11.2–§11.4 |
| 5 | 上游通道 | 平台统一 key，不需要 Bedrock / Vertex | G8；上游代理提前到 P0（§7.3），去掉 BYOK 与凭据表 |
| 6 | 多节点 | 不需要 | 单节点；去掉节点粘滞与跨节点迁移 |

---

## 16. 参考

- ACP 协议：<https://agentclientprotocol.com>（v1 稳定；v2 为草案，SDK 需显式 `experimental/v2` 导入，本方案不用 v2）
- TS SDK：`@agentclientprotocol/sdk@1.8.0`（`AcpServer`、`createHttpStream`、`AgentProtocolRouter`、`client()` / `agent()`）
- Claude 适配器：`@agentclientprotocol/claude-agent-acp@0.89.1`
- dsh ACP：loomvec 仓 `third_party/deepseek-harness/packages/acp/acp/`（README 的 Protocol contract 与 Known Limitations 两节）、`packages/bundle/acp-app/cordis.patch.yml`；事故复盘 `third_party/deepseek-harness/docs/postmortem/0001-acp-default-export-drops-inject.md`
- loomvec 现有实现（迁移的输入）：
  - `services/agent/src/loomvec/agent/`（`routes.py`、`runtime/`、`sessions/`、`approvals.py`、`attachments.py`）
  - `services/api/src/loomvec/api/routes/{agent,mcp}.py`
  - `services/core/src/loomvec/core/agent_tokens.py`
  - `deploy/compose/sandbox/Dockerfile`、`deploy/compose/sandbox-firewall.sh`
- 现有设计文档：`docs/14-阶段P5-dsh智能体对话与企业工作环境.md`（其中协议方法列表、续会话、审批的描述已与代码不符，以代码为准；L3 时改写）

---

## 17. 实施记录（P0，2026-10-12）

P0 已在 `/home/jyy/acp-gateway` 实现：REST + SSE、事件日志与断线续传、claude 适配器、审批策略（含 `ask`）、
上游代理（Anthropic + DeepSeek）、会话恢复、租户 / Key / 配额 / 限流 / 审计 / 指标、docker 与 local provider、两个镜像。
测试：单元 / 集成 / e2e 全部通过，e2e 含**真实的 claude-agent-acp + Claude Code 二进制**（对接脚本化的假 Anthropic）与**真实 docker + agent 镜像**。
**唯一缺口**：没有用真实 Anthropic key 冒烟（开发环境没有 key）。

### 对方案的修正（对着包与源码核对后发现）

1. **会话恢复顺序改为 `session/resume` → `session/load` → 新建 + `context_reset`**（修正 §7.1 / §8.1）。claude-agent-acp 0.89.1 同时声明
   `loadSession` 与 `sessionCapabilities.resume`；resume 不回放历史，会话已存在时比较参数指纹，参数变了（MCP 服务器、system prompt）自动重建，
   所以 `PATCH mcp_servers` 的重建只需 `session/close` + `session/resume`。
2. **SDK 用 `client()` 构建器**（`ClientSideConnection` 在 1.8.0 已 deprecated）；`AcpServer` / `createHttpStream` 在
   `@agentclientprotocol/sdk/experimental/*`，**不是稳定导出**——P3 的 `/acp` 端点要接受这一点（修正 §4.2 / G1 的说法）。
3. **SDK 的 `client()` 内置严格的 `SessionUpdateRouter`，会丢弃未知的 `session/update`**。对「未知 update 进 `raw`、不丢帧」（§5）这条要求，
   网关在 stream 层把未知类型改写成私有方法绕开。P3 的 `/acp` 代理端点同样会受此影响。
4. **事件类型比 §5 多**：`plan_update`、`plan_removed`、`notice`、`compaction_*`、`subagent_update`、`session_message*`（目前都进 `raw`，客户端未声明对应能力时 agent 也不会发）。
   `ToolCall` 有原生 `name` 字段，工具名优先取它，其次 `_meta.claudeCode.toolName`。`usage` 事件是**上下文窗口占用**（字段 `context_used` / `context_size`，可选 `cost`），回合 token 用量在 `turn.completed.usage` 里。
5. **agent 的 `command` 一律是容器内完整命令**（§10 里 dsh 只写了 `["--profile","acp"]`，实现统一为 `["dsh","--profile","acp"]`，镜像不设 ENTRYPOINT）。
6. **Redis 只做限流**，事件广播用进程内订阅（§3 / G 中写的是「限流与事件广播」；单节点不需要跨进程广播）。
7. **配额 token 口径**（§9 未定义）：`input + output + cache_write + ⌈cache_read/10⌉`。
8. **审批**：自动裁决也发 `permission.resolved` 并写审计；`ask` 的 REST 应答（§4.1 的 `POST /permissions/{request_id}`）提前到 P0 实现。
   session 策略与 Key 策略「取更严格者」。
9. **容器回收只认 label**（`acpgw.managed` + `acpgw.instance`），不按名字前缀（§7.1）。这是实施中踩到的真实事故：按前缀回收会删掉别的服务的容器（含开发用的 PG / Redis）。
10. **网络出口收紧不在网关进程内**（§7.1 的 `egress: upstream-only`）：由 docker 网络 + 宿主防火墙实现。开发机默认丢弃「容器 → 宿主」入站，
    部署必须显式放行沙箱网段到网关端口（网关仓库 `deploy/sandbox-firewall.sh`，改编自 loomvec 的沙箱防火墙，**未在有 root 的环境实测**）。
    该脚本的 INPUT 规则只管「目的地是宿主自己」的流量，所以**网关必须在宿主网络命名空间里**（宿主进程，或容器 `network_mode: host`）；网关挂在 bridge 网络的容器里时，沙箱 → 网关走 FORWARD（经 DNAT），脚本没有覆盖。
11. **宿主侧读取 workspace 内容的安全要求**（补充 §12）：`files/` 由容器内不可信的 agent 写入，符号链接可指向宿主任意文件。
    网关读取 `files/AGENTS.md` 等必须 realpath 校验 + `O_NOFOLLOW` + 大小上限；P1 的 files 接口同理。TOCTOU 窗口是已知局限。

### 对 loomvec 迁移（§11）的影响

- §11.2 事件翻译表可按 §5 + 本节第 4 点实施；`usage` 帧应取 `turn.completed.usage`，不要用 `usage` 事件。
- `agent.gateway.api_key` 的 Key 需要 scope `sessions:write` + `sessions:read`；loomvec 租户的 `settings.mcp.allowed_hosts` 必须包含 `agent.mcp.url_sandbox` 的主机。
- 网关的 `external_ref` 在同租户内唯一，重复创建返回 409 且 `details.workspace_id` 给出已有 id，适合做「首次使用时创建」。

---

## 18. 实施记录（P1，2026-10-13）

P1 已在 `/home/jyy/acp-gateway` 实现：dsh 适配器与镜像、files / tree / dirs / instructions、磁盘配额强制、归档保留清理、OpenAI 兼容接口、`sessions:import`、TS 客户端。
测试全部通过，包括**真实的 `dsh --profile acp`**（对接脚本化的假 DeepSeek）与**真实的 dsh docker 镜像**。逐条偏差见网关仓库 `planning/README.md` 第 11–19 条，这里只记对 loomvec 迁移（§11）有影响的：

### §8.2 要求先验证的三点

1. `deepseek-harness-sdk==0.1.5rc1` 的 wheel 里 `dsh` **带** `--profile acp`；
2. acp profile 自带 session-persistence-jsonl，root 缺省为 `$DSH_HOME/sessions`，不需要像 sdk profile 那样在 patch 里补；
3. attachment-local 在 acp profile 里已挂载，但图片 prompt 是否可用取决于所选模型（缺省 `deepseek-v4-flash` 下 `promptCapabilities.image=false`）。

### 对方案的修正

- **dsh 的审批在 docker 下不生效**（修正 §6 / §8.2 / G7）：dsh 自带的文件沙箱需要 bwrap 或 landlock-run，wheel 不带 landlock-run、bwrap 在 `cap-drop ALL` 容器里起不来（已验证），
  于是 docker 下网关让 dsh 以 `danger-full-access` 运行，隔离靠容器。dsh 只在沙箱升级时发 `request_permission`，所以网关的 `permission_policy` 对 dsh 不起作用。
  **loomvec 迁移后原来 `approvals.py` 的矩阵（`mcp__loomvec__*` 与编辑类允许、其余拒绝）不会约束 dsh 的 bash 等工具**，靠容器边界兜底——这与现状（loomvec 自己的 dsh 在 docker 里同样没有可用沙箱后端时）需要对照确认。
- **dsh 的 `model` option 取值是 JSON 编码的 `["provider","model"]`**：网关接受模型名（`deepseek-v4-pro`）或 `provider/模型名`，loomvec 传 `agent.model.name` 即可。
- **dsh 的 prompt 响应没有 usage**：loomvec 的 `usage` 帧要取网关事件 `turn.completed.usage`——对 dsh 它是 `null`。需要用量时查 `GET /admin/v1/tenants/{id}/usage`，或回合结束后读 `turn` 的用量（同样可能为空）；上游代理实测值只在 OpenAI 兼容接口里做了回退。
- **`sessions:import` 的请求体由网关定义**（§11.3）：按回合组织 `{prompt, items[text|thought|tool], status, stop_reason, usage, error}`，`import_key` 幂等。迁移脚本要把 `transcript.py` 的输出映射成它。
  导入的会话 `model` 会在继续对话时推送给新 agent 会话。
- **文件接口**（§11.2 第 8–9 条）：`PUT /v1/workspaces/{id}/files/{path}` 的请求体是原始字节；`GET tree` 默认 `depth=1`；符号链接不提供下载。loomvec 的附件目录 `.loomvec/uploads/{session_id}/` 可直接用。
  `GET/PUT /v1/workspaces/{id}/instructions` 读写 `files/AGENTS.md`。
- **租户 scope**：loomvec 的 Key 需要 `sessions:write` + `sessions:read` + `files:read` + `files:write`。
- **磁盘配额已强制**：默认 5GiB / workspace；超限上传 413、发起回合 429（`quota_exceeded`）。归档 workspace 默认 30 天后删目录。

### 审查修复轮对方案的影响（2026-10-13）

P0/P1 之后网关又过了一轮五路审查与修复（文件 / 磁盘安全、回合事件核心、OpenAI + 客户端、dsh + 部署、文档一致性），细节见网关仓库 `planning/README.md`「P0/P1 审查修复轮」。这里只记对 P2 迁移有影响的：

- **prompt 预检变成 HTTP 4xx**（影响 §11.2 的错误处理）：`POST /v1/sessions/{id}/turns` 的 prompt 校验在受理阶段完成，`stream=true` 与 `stream=false` 一样，
  不合格直接返回 400 `invalid_request` / `unsupported`（图片、内嵌资源 agent 不支持）/ `path_forbidden`（`workspace:///` 越界）或 413 `payload_too_large`，没有 `turn.started`，也不再是 200 / 202 之后的 `turn.failed`。
  客户端要在「发起回合」这一步处理 4xx；流内的 `turn.failed` 只剩运行期失败（超时、崩溃、agent 报错、runtime 拉不起来、`gateway_restarted` 等），`code` 不是封闭枚举。
  runtime 拉不起来的回合以 `turn.failed` 开头，没有 `turn.started`。
- **OpenAI 接口的 `external_ref` 前缀是 `openai-compat:<user>`**（不是 `openai:`）；原生 `POST /v1/workspaces` 拒绝该前缀。loomvec 自己建 workspace 的 `external_ref` 不要用它。
- **`import_key` 是独立列**（迁移 003，`(workspace_id, import_key)` 唯一）：并发导入同一个 key 幂等（后到的拿先到的，200），`metadata` 里的 `import_key` 被忽略。§11.3 的迁移脚本可以放心并发重跑；用 `import_key` 做断点续跑即可。导入文本里的 NUL 会被静默去除。
- **`Last-Event-ID` / `after_seq` 必须是 `[0, 2^53-1]` 的整数**，否则 400；SSE 流开始（200）之后的服务端错误以 `event: error` 帧（`data: {"error":{"code","message"}}`）通知并关闭连接，
  客户端应带最后收到的 `id` 重连。慢连接（排队 > 16MiB 或结束时 10 秒写不完）会被主动断开，同样凭 `Last-Event-ID` 续传。§11.2 的 SSE 消费循环要处理 `error` 帧。
- **审批策略解析失败 fail-closed**：Key 或 session 上存的 `permission_policy` 无法解析时按「全部拒绝」处理，不再当成无策略放行。迁移时写入的策略要通过网关 schema 校验。
- **`extra_env` 保留键被拒**：`PATH`、`HOME`、`DOCKER_*`、`*_PROXY`、`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`、`DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`、`DSH_HOME`、`CLAUDE_CONFIG_DIR`（启动即报错）。
  迁移 loomvec 的 agent 配置时不要把这些放进 `extra_env`（`DSH_PERMISSION_MODE` 仍可覆盖）。
- **`server.internal_base_url` 缺省自动推导**（local → `http://127.0.0.1:<port>`，docker → `http://host.docker.internal:<port>`），§10 / §11.7 部署时不必再手填。
- **沙箱防火墙**：网关仓库的 `deploy/sandbox-firewall.sh` 修了两个缺陷（首次运行 `pipefail` 静默退出；DOCKER-USER 用 `-A` 追加到 dockerd 预置的 `RETURN` 之后导致隔离整体失效），并加了次序自检、`open` 模式的 CGNAT / 链路本地 DROP。
  **loomvec 原脚本 `deploy/compose/sandbox-firewall.sh` 有同样的两个缺陷且没有次序自检，待同步修复**（在本仓库处理，建议 P2 部署前完成）；网关侧脚本仍未在真实内核上实测。
