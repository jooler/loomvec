# P2 实施简报：loomvec 切换到 acp-gateway

> 上位方案：[acp-gateway.md](./acp-gateway.md)（§11、§14、§17、§18）。本文件是 P2 的**实施合同**：把方案落成可并行施工的具体决定。
> 网关现状文档：`/home/jyy/acp-gateway/docs/`（`api.md` 接口、`architecture.md` 行为），契约快照 `/home/jyy/acp-gateway/openapi.json`。
> 范围：L1（网关客户端 + 翻译层）、DB 补列、admin 设置与「ACP 服务」菜单、L2 迁移脚本、部署。**L3 删除不在本期**：方案要求切换验证通过后才删，而真实模型冒烟还没做（见末尾）。

## 1. 与方案正文的偏差（实施前核对代码得出）

1. **没有 Alembic**：loomvec 已改用 `services/api/src/loomvec/api/init_db.py` 的幂等初始化（`create_all` + `_ADDITIVE_COLUMNS` / `_ADDITIVE_INDEXES`）。方案里的「Alembic 迁移」实际是：改 ORM 模型 + 往 `_ADDITIVE_COLUMNS` 追加 `ADD COLUMN IF NOT EXISTS`。
2. **`services/agent` 保留为业务适配层，对外契约不变**：`apps/web` 和 `services/api/routes/agent.py` 都按 `/internal/agent/*` 与现有 SSE 帧工作。P2 只把 `services/agent` 的**内部实现**从「自管 dsh 进程」换成「调网关」，所以前端零改动、api 门面零改动（L4 并入 api 是以后的事）。
3. **MCP 检索的 @ 文献键从 `env_id` 改为 `agent_session_id`**（`agent_tokens.MENTIONS_PREFIX` 的 key 后缀、`routes/mcp.py:_mentioned_asset_ids`）。原因：网关里同一 workspace 的多个会话可以并发回合，env 级的键会串；会话级 token 本来就带 `agent_session_id`。
4. **管理 token 是第二个密钥**：网关 `/admin/v1/*` 用 `ACPGW_ADMIN_TOKEN`，与租户 API Key 不同。loomvec 需要两个敏感项：`agent.gateway.api_key`（对话用，租户 Key）和 `agent.gateway.admin_token`（仅「ACP 服务」菜单用）。
5. **ai.llm.* 的 admin 配置对 agent 不再生效**：dsh 的 DeepSeek 凭据现在在网关（`ACPGW_DEEPSEEK_API_KEY`）。`load_llm_overrides` 的 agent 用途删除；admin「AI 供方配置」页对 agent 的提示文案要同步。

## 2. 配置与存储合同

### 2.1 `config/loomvec.json` → `AgentSettings`（`services/core/.../config.py`）

新增（其余 `agent.*` 本期原样保留，L3 再删）：

```jsonc
"agent": {
  "default_agent": "claude",            // claude | dsh：新会话用哪个 agent
  "gateway": {
    "url": "http://127.0.0.1:38095",    // 网关地址（只读展示于 admin）
    "request_timeout_s": 30,            // 普通请求超时；SSE 流不设总超时
    "models": { "claude": null, "dsh": null }  // 各 agent 的默认模型名；null = 用 agent 默认。dsh 缺省回落 agent.model.name
  }
}
```

### 2.2 `system_config` 敏感项（admin 可配置，复用 `admin_settings.py` 的敏感项机制）

| key | 用途 |
|---|---|
| `agent.gateway.api_key` | loomvec 租户的网关 API Key（scope：`sessions:read/write` + `files:read/write`） |
| `agent.gateway.admin_token` | 网关 `ACPGW_ADMIN_TOKEN`，仅 services/api 的 `/api/v1/admin/acp-gateway/*` 代理使用 |

读取方式与现有 `ai.llm.api_key` 一致（DB 有非空值即生效；这两项**没有**配置文件回落，空 = 未配置 → agent 功能报明确错误）。

### 2.3 DB 补列（ORM + `init_db._ADDITIVE_COLUMNS`）

| 表 | 列 | 说明 |
|---|---|---|
| `agent_environment` | `gateway_workspace_id VARCHAR(64) NULL` | 网关 workspace id；首次使用时创建（`external_ref = agent_environment.id`） |
| `agent_session` | `agent VARCHAR(16) NOT NULL DEFAULT 'dsh'` | 会话所用 agent。存量行 = `dsh`（历史都是 dsh）；新建会话写 `agent.default_agent` |
| `agent_session` | `gateway_session_id VARCHAR(64) NULL` | 网关 session id；首个提问时懒创建 |
| `agent_session` | `mcp_token_exp TIMESTAMPTZ NULL` | 网关 session 上当前 MCP token 的过期时间；NULL = 需要（重新）签发并 `PATCH mcp_servers` |

## 3. 映射与行为合同（`services/agent`）

目录：`services/agent/src/loomvec/agent/gateway/`（`client.py` 薄 httpx 异步客户端、`translate.py` 事件翻译、`models.py` 必要的类型、`openapi.snapshot.json` 网关契约快照）。

1. **workspace**：`_own_env` 之后确保 `gateway_workspace_id`：空则 `POST /v1/workspaces {external_ref: str(env.id)}`；409 时读 `details.workspace_id` 回填（幂等）。创建后 `PUT instructions` 写入 AGENTS.md（内容沿用 `runtime/envs.py` 渲染的那份，含租户名 / 用户名等）；模板变更时重写（见 `envs.py` 现有触发点）。
2. **session**：首个提问时懒创建网关 session：`POST /v1/workspaces/{ws}/sessions {agent, model, cwd: "projects/<project_path>"|"", mcp_servers:[loomvec], permission_policy, system_prompt_append?}`，回填 `gateway_session_id`、`mcp_token_exp`。`project_path` 不再往 prompt 拼目录前缀，由 `cwd` 承担（旧 `_build_blocks` 的目录说明删除）。
3. **MCP token**：会话级，`issue_agent_token(..., env_id, session_id=agent_session.id.hex, scope_space_ids=会话 scope)`，作为 `mcp_servers: [{name:"loomvec", type:"http", url: <agent.mcp.url_sandbox>, headers:[{name:"Authorization", value:"Bearer …"}]}]`。每次提问前：`mcp_token_exp` 为空或剩余有效期 < `runtime 回合上限`（取 `agent.mcp.token_ttl_s` 的一半且不小于 600s）→ 重签并 `PATCH /v1/sessions/{id} {mcp_servers}`，更新 `mcp_token_exp`。会话的 `scope_space_ids` 被 PATCH 时置 `mcp_token_exp = NULL`。
4. **审批策略**：由 `approvals.py` 现有矩阵翻译成网关 `PermissionPolicy`：`default: reject`，规则允许 `tool_name: mcp__loomvec__*` 与矩阵里列出的编辑 / 读取类工具（按 `kind` 或 `tool_name`，以网关 `docs/api.md` 的规则语法为准）。**对 dsh 不生效**（docker 下 dsh 以 `danger-full-access` 运行，隔离靠容器），见方案 §18。
5. **提问**（`POST /internal/agent/sessions/{id}/messages`，SSE 契约不变）：
   - `prompt` 块：@ 文献说明块（沿用 `transcript.build_mention_block`）、附件（图片 → image block，其他 → `resource_link` / 文字说明指向 `workspace:///.loomvec/uploads/...`）、问题。
   - `POST /v1/sessions/{id}/turns {prompt, stream:true}`；网关返回 4xx（prompt 预检）时在发 SSE 之前以 `error` + `done` 两帧收尾（与现有「沙箱不可用」一致的帧形状），`finish_reason: "error"`。
   - `_publish_mentions` 改为按 `agent_session_id` 写 Redis。
   - 客户端（浏览器）断开：**不**取消网关回合（保持现行「后台收尾，刷新可见」语义）；SSE 迭代器收到断开就停止转发即可，必要时用 `GET /v1/sessions/{id}/events?follow=1&after_seq=` 让「刷新后接回」成为可能（不要求本期做前端接回）。
   - 事件翻译表：

     | 网关事件 | loomvec SSE 帧 |
     |---|---|
     | `turn.started` | `meta {session_id(逻辑), message_id, model, env_id}` |
     | `message.delta` | `delta {text}` |
     | `thought.delta` | `reasoning {text}` |
     | `tool.started` | `tool_start {call_id, tool, args}` |
     | `tool.completed` | `tool_end {call_id, tool, ok, result_summary}`；检索工具（`mcp__loomvec__search_knowledge`）另发 `citations`（Redis 旁路 `agent:citations:{session_id}:{batch_id}`，inline `ref_items` 兜底，沿用 `citations.py`） |
     | `turn.completed` | `done {message_id, answer, citations, graph_evidence, usage, finish_reason}`；`usage` 取 `turn.completed.usage`（dsh 为 `null`，此时输出空对象并补 `usage` 帧不发） |
     | `turn.failed` | `error {message, code}` + `done {finish_reason:"error"}` |
     | 其余（`permission.*`、`usage` 上下文占用、`plan_*`、`raw` 等） | 忽略（`usage` 事件是上下文窗口占用，不是回合用量，不要映射成 `usage` 帧） |

     `message.delta` 的粒度由网关决定：claude 是流式小块，dsh 往往整段到达——一律原样转 `delta`，**不做打字机回放**（`_replay_plan` 随 L3 删除）。网关 SSE 流中的 `event: error` 帧（hijack 之后的服务端错误）按 `error` + `done` 收尾。
   - 回答末尾的元数据收尾（`message_count`、`preview`）沿用现有逻辑。
6. **取消**：`POST /v1/sessions/{id}/cancel`（只取消回合，不杀进程）。
7. **历史**：`GET /internal/agent/sessions/{id}/messages` 改读网关 `GET /v1/sessions/{id}/messages`（回合结构 → 现有 `AgentMessageOut`：user 消息的 mentions 从 prompt 里的 @ 文献块解析，assistant 的 citations / tool_calls 从工具事件解析；解析逻辑从 `sessions/transcript.py` 抽出可复用部分）。`stats` 由网关各回合 usage 聚合（dsh 无用量时为空统计，不报错）。
8. **附件**：`PUT /v1/workspaces/{ws}/files/.loomvec/uploads/{session_id}/{name}`（原始字节；保留现有类型 / 大小白名单与文件名清洗，`attachments.py` 里的校验逻辑复用，落盘部分换成网关上传）。`attachment_id` 约定为清洗后的文件名（含短随机前缀，防重名），`resolve_attachment` 改为向网关 `GET …?stat=1` 取元信息。
9. **项目与文件浏览**：`/projects` 的目录创建 → `POST dirs/{name}`；`/workspace/tree` → `GET tree`（保留「隐藏段不出树」「最多 500 项」「prefix 子树」语义，在适配层过滤）；`/workspace/file` → `GET files/{path}`（取前 256KB 预览，保留 `truncated`）。
10. **status / health**：`/internal/agent/health` 探测网关 `GET /healthz`（以网关文档为准），`runtimes` 字段用网关 workspace 的运行时数不可得时置 0。
11. **错误映射**：网关 4xx/5xx → 现有 `LoomvecError` 家族（401/403 → 配置错误类提示「ACP 网关凭据无效」，404 → NotFound，429/`quota_exceeded` → 可读文案，5xx / 连接失败 → `UpstreamUnavailableError`）。
12. **dsh 与 claude 的差异要点**（实现时留意）：dsh 无 token 用量；dsh 不支持 `sse` 类型 MCP（loomvec 用 `http`，无影响）；dsh 的 `model` 取 `agent.model.name`，claude 取 `agent.gateway.models.claude`（null = agent 默认）。

## 4. 契约测试

- `gateway/openapi.snapshot.json` 入库自网关 `openapi.json`；契约测试：客户端用到的每个 `(method, path)` 都存在于快照，请求体字段名 / 必填项与快照 schema 一致（不是只比文件相等）。
- 单元测试：翻译层（用网关真实事件样例，覆盖每种事件与 `event: error`）、客户端（`httpx.MockTransport`）、审批策略翻译、懒创建 / 409 回填 / token 续签的状态机。
- **可选 e2e**（环境具备才跑，否则 skip）：起真实网关（`/home/jyy/acp-gateway`，`npm run dev:up` 的 PG/Redis，`provider=local`，`agents.claude.local_command=["node","test/fake-agent/agent.mjs"]`，网关测试里的假 agent 按脚本回放事件），loomvec `services/agent` 对着它跑完整提问流，断言 SSE 帧序列与现有契约一致。

## 5. admin（`services/api` + `apps/admin`）

1. **services/api**：`/api/v1/admin/acp-gateway/*` 代理到网关 `/admin/v1/*`（租户、Key、配额、用量、workspace 列表与强制停止、运行时状态），用 `agent.gateway.admin_token`；复用平台角色鉴权与审计（写操作仅 super_admin，operator 只读 + 强制停止 workspace，auditor 只读）。Key 明文只在签发响应里出现一次，**不入审计详情**。
2. **apps/admin**：
   - 「设置 → agent」：网关地址只读展示、`agent.gateway.api_key`（只写）、`agent.gateway.admin_token`（只写）、`agent.default_agent`、默认模型；
   - 新菜单组「ACP 服务」：租户管理、API Key 管理（签发用 `SecretModal`）、配额设置、用量报表（按租户 / 天）、运行时状态卡片（参考 `LocalModelsStatus`）；写操作走 `ReasonModal`。
   - 遵循 `apps/FRONTEND_CONVENTIONS.md`。
3. 网关 API Key 的 scope 清单与租户 `settings.mcp.allowed_hosts`（必须含 `agent.mcp.url_sandbox` 的主机）在页面上给出提示。

## 6. L2 数据迁移脚本（`scripts/migrate_agent_to_gateway.py`）

> 进度：已实现并通过真实网关 e2e（`test_e2e_gateway.py::test_legacy_migration_script_imports_history_and_files`）；用法与映射见 [docs/19-ACP网关接入.md](../docs/19-ACP网关接入.md) §6。2026-10-14 在开发机用真实旧数据（1 个 env、7 个会话、5 个文件）对着临时网关演练通过：导入后助手文本逐会话一致、工具调用数一致、重跑幂等；演练暴露的问题（JSONL 时间戳是毫秒 epoch、旧重放前缀与目录说明混在用户消息里）已修。尚未对生产环境演练。

可重入、可 `--dry-run`、可按 env 过滤：
1. 每个 `agent_environment`：确保网关 workspace（`external_ref=env_id`），把 `{agent.storage_root}/envs/{env_id}/workspace/` 同步进网关 `files/`（走 files API，保留相对路径，跳过 `.loomvec/uploads` 以外的隐藏目录以及符号链接）。
2. 每个旧会话：`transcript.aggregate_transcript` → 映射成网关 `sessions:import` 请求体（按回合 `{prompt, items[text|thought|tool], status, stop_reason, usage, error}`，`import_key = agent_session.id`），导入后回填 `agent_session.agent='dsh'`、`gateway_session_id`、`mcp_token_exp=NULL`。
3. 幂等：已有 `gateway_session_id` 的跳过；网关 `import_key` 也幂等。

## 7. 部署与杂项

- `deploy/compose`：可选 `acp-gateway` 服务（引用已发布镜像版本，和 `api` 共享网络，便于 MCP 回调；挂 docker.sock / `DOCKER_HOST`）；`config/loomvec.json` 示例补 `agent.gateway`。
- `deploy/compose/sandbox-firewall.sh`：同步修复网关侧已修的两个缺陷（`pipefail` 首次运行静默退出；`-A DOCKER-USER` 排在 `RETURN` 之后）并加次序自检、`open` 模式补 `100.64.0.0/10` 与 `169.254.0.0/16` 的 DROP。**无 root，不执行**，用桩 iptables 验证逻辑。
- 网关 API Key 的签发：运维在网关侧 `POST /admin/v1/tenants`、`.../api-keys` 一次性完成（或用 admin「ACP 服务」菜单），填进 `agent.gateway.api_key`。

## 8. 验收与本期不做的事

- 验收：`apps/web` 零改动，对话（SSE 帧序列）、引用、@ 文献、附件、项目目录、工作区浏览、取消、历史全部走网关；claude / dsh 可由 `agent.default_agent` 切换；单元 / 契约测试全过；e2e（环境具备时）全过；admin 能管理租户 / Key / 配额 / 用量。
- **不做**：L3 删除（`runtime/*`、`orchestrator.py` 的 dsh 部分、`transcript.py` 的 JSONL 解析、`usage.py`、`deepseek-harness-sdk` 依赖、`agent-sandbox` 网络等）。前置条件：真实 Anthropic / DeepSeek key 冒烟通过；旧会话迁移演练通过；一个观察期。满足后单独开 L3。
- **不做**：前端审批 UI、`/acp` 原生端点、L4 并入 `services/api`。

### L3 前置条件进度（2026-10-14）

| 条件 | 状态 |
|---|---|
| dsh 真实模型冒烟 | 已通过（开发机，网关 local provider + 真实 dsh 0.1.5rc1 + 用户配置的 OpenCode Go 上游，经一层只补 `x-opencode-session` 头的本机转发）：检索 + 引用（8 条）、续问上下文、写文件、取消后继续、迁移会话继续对话 |
| claude 真实模型冒烟 | 已通过（开发机，网关 local provider + `claude-agent-acp` 0.89.1，用的是用户订阅登录：包装脚本去掉网关塞入的 `ANTHROPIC_*` 变量并指向复制了凭据的临时配置目录，冒烟后删除；仅限开发冒烟，生产仍需网关持有 Anthropic key）：检索 + 引用（8 条）、流式（65 个 delta）、token 用量与会话统计、续问上下文、写文件放行、`touch` / `curl` 被审批策略拒绝、取消后继续、AGENTS.md 规范注入 |
| 旧会话迁移演练 | 开发机真实数据已演练；生产环境未演练 |
| docker provider + 防火墙冒烟 | **未做**：无 root，无法下发沙箱防火墙规则；网关 `local` provider 路径已验证 |
| 观察期 | 未开始 |

冒烟暴露的两项（已修）：迁移会话继续对话时模型不记得历史（补了重放前缀）；403 的具体原因被说成「凭据无效」。
遗留决策：OpenCode Go 这类要求自报会话头的供方不能直接作为网关 deepseek 上游（网关代理只转发 `content-type`/`accept`），生产要么换官方 DeepSeek key，要么在网关侧支持上游附加头。
