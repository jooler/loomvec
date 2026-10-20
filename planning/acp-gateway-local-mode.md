# acp-gateway 本地模式改造方案

> 2026-10-14。起因：P2 切换后发现部署要 Anthropic key、docker 沙箱、宿主防火墙、独立 PG——这是「多租户服务」的形态，不是最初要的东西。
> 本方案取代 [acp-gateway-p2.md](./acp-gateway-p2.md) §9.7 的上线手册（docker 部署路线暂停，不删）。网关仓库：`/home/jyy/acp-gateway`。

## 1. 目标

**一个本机工具**：和 Zed 调用 Claude Code 一样——用户在 CLI 里 `claude` 登录好，网关在本机把 Claude Code（以及 dsh）以 ACP 方式拉起来，本地的 loomvec（智能体对话、知识库文档处理等）通过网关的既有 HTTP 接口调用它。

验收一句话：**一台装了 Node ≥ 22.9 和 `claude` 并已登录的机器上，`npm run local` 一条命令起网关，不填任何 Anthropic key，不装 docker，不跑 root 命令，loomvec 智能体用 claude 能完成一次带检索和引用的对话。**

非目标：多租户、配额、对外提供服务、网络隔离。本模式只服务本机单用户。

## 2. 审视结论：为什么现状偏了

| 现状（server 模式） | 对本机工具的实际作用 | 本地模式 |
| --- | --- | --- |
| claude 的认证：网关清掉 `ANTHROPIC*` / `CLAUDE*` 环境，给 agent 发 workspace 级短时 token，`ANTHROPIC_BASE_URL` 指向网关 `/upstream/anthropic` 代理，由代理换成 `ACPGW_ANTHROPIC_API_KEY`（网关仓库 `src/runtime/adapters/claude.ts` 的 `env()`（server 分支）） | 为了「平台 key 不进容器」和计量。本机只有一个人，没有这个威胁 | **不设任何 `ANTHROPIC*`，让 claude 自己读 CLI 登录态**（`~/.claude`）。`claude-agent-acp` 自带原生登录支持 |
| 每个 workspace 一个独立 `CLAUDE_CONFIG_DIR`（`home/claude`），里面没有登录态 | 只能靠 API key | 使用真实 `~/.claude`（可配 `config_dir`） |
| docker provider + `acpgw-sandbox` 网络 + 宿主 iptables + `host.docker.internal` + `/data/acp-gateway` | 隔离多租户不可信 agent；需要 root | `local` provider（已有），agent 就是本机子进程 |
| `NODE_ENV=production` 拒绝 local；`loadConfig` 强制 `ACPGW_DATABASE_URL`、生产强制 `SECRET_KEY` / `ADMIN_TOKEN` | 合理，但本机工具不该先配这些 | 本地模式自动生成并落盘 |
| PostgreSQL（Kysely，jsonb、`pg_advisory_lock`、部分唯一索引）+ 可选 Redis | 起一个 PG 只为存一个人的会话日志 | 内嵌 PGlite（同方言，几乎不改 SQL），Redis 本来就可选 |
| 租户、API Key、admin token 要手工建（admin 菜单） | 一次性配置负担 | 启动时自动建 `local` 租户和 key，写文件 |
| 租户 `mcp.allowed_hosts` 白名单 | 防 SSRF | 本地租户预置 `127.0.0.1` / `localhost` |

可直接复用、不改的部分（这是网关的主要价值）：ACP 连接与进程生命周期（`runtime/`）、事件规范化与断点续传（`events/`）、会话/回合编排与恢复（`sessions/`）、审批策略、文件接口、历史导入、OpenAI 兼容接口。**对 loomvec 的 HTTP 契约不变**，所以 `services/agent` 的适配层基本不动。

## 3. 本地模式定义

开关：配置 `mode: "local"`（默认 `"server"`，行为与现在完全一致）或环境变量 `ACPGW_MODE=local`。

`mode=local` 时：

1. **强制 `runtime.provider=local`**；监听地址强制回环（`127.0.0.1`），配置了非回环地址则启动报错。
2. **claude 用你在 CLI 里的订阅登录，不用 API key**，两档（见 §6 S0 结论，竞态是已知未修的 Claude Code 问题）：
   - **`token`（推荐）**：你在终端跑一次 `claude setup-token`，把得到的长期 token 放进 `{state_dir}/claude-oauth-token`（权限 600，或环境变量 `CLAUDE_CODE_OAUTH_TOKEN`），网关把它作为 `CLAUDE_CODE_OAUTH_TOKEN` 注入 agent 进程。不涉及 `~/.claude` 的 token 刷新，多个进程并发不会互相挤掉登录。
   - **`login`（缺省回退）**：没有 token 文件时，子进程直接用 `~/.claude` 的登录态（Zed 的做法）。可用，但多个 claude 进程（含你自己在用的 `claude`）在 token 刷新那一刻有竞态，可能被登出，启动时打印警告。
   两档下适配器都不设 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `CLAUDE_CONFIG_DIR`；子进程环境走白名单透传（`HOME`、`PATH`、`USER`、`LANG*`、`TMPDIR`、`XDG_*`、`HTTP(S)_PROXY` / `NO_PROXY`；`SSH_*` 不透传）。`agents.claude.config_dir` 可显式指定（默认不设 = `~/.claude`）。
3. **不加载用户的全局 Claude 设置**：session/new 的 `_meta.claudeCode.options.settingSources` 默认设为 `["project"]`（`claude-agent-acp` 默认是 `["user","project","local"]`，会把用户 `~/.claude` 里的 CLAUDE.md、hooks、插件、个人 MCP 都带进来；`_meta.claudeCode.options` 在其后展开，可覆盖，见 `acp-agent.js` 7320 附近）。登录态不属于 settings source，不受影响。可配 `agents.claude.setting_sources` 改回去。
4. **dsh 不变**：仍走上游代理 + `ACPGW_DEEPSEEK_API_KEY`（已决策换官方 DeepSeek key）；`local_command` 指向本仓库的 `.venv/bin/dsh`。上游代理路由保留，只是 claude 不再用它。
5. **自带身份**：启动时幂等创建租户 `local`（无配额、`settings.mcp.allowed_hosts=["127.0.0.1","localhost"]`、`default_agent` 可配），签发 API Key 与 admin token，明文写入 `{state_dir}/local-api-key`、`local-admin-token`（权限 600，已存在则复用）。`ACPGW_LOCAL_API_KEY` / `ACPGW_ADMIN_TOKEN` 环境变量可覆盖为固定值。启动日志只打印文件路径，不打印明文。
6. **默认目录**：`state_dir` 缺省 `~/.local/share/acp-gateway`（遵循 `XDG_DATA_HOME`），`storage_root` 缺省为其下 `workspaces/`——不再需要 `/data/...` 和 root。
7. **数据库**：`ACPGW_DATABASE_URL` 缺省时用内嵌 PGlite，数据文件 `{state_dir}/db`；给了 `postgres://` 仍可用外部 PG。迁移（`migrations/*.sql`）、Kysely 查询不变，只换 dialect；`migrate.ts` 的 `pg_advisory_lock` 在单进程内嵌库下无意义，需跳过或兼容。
8. **安全默认**：本机 Bash 没有沙箱，所以 `permissions.default_decision` 在本地模式缺省为 `reject`（调用方——loomvec 的 `build_permission_policy`——显式放行检索、读取、编辑类）；`bypassPermissions` 保持仅 docker 可用；API Key 仍然必须（防本机其他进程/用户借用你的登录态）。
9. **一条命令**：`npm run local`（= `tsx src/index.ts`，注入 `ACPGW_MODE=local`），并提供 systemd `--user` 单元示例。

## 4. 网关仓库改动清单

| # | 改动 | 位置 | 说明 |
| --- | --- | --- | --- |
| G1 | `mode` 配置与校验 | `src/config.ts` | 新增 `mode`；`local` 时强制 provider、回环监听；跳过 production 的 docker/SECRET/ADMIN 强制（本地模式下自动生成）；`state_dir`、默认 `storage_root`；`ACPGW_DATABASE_URL` 缺省不再报错 |
| G2 | 本地环境白名单 | `src/runtime/local.ts`、`src/runtime/manager.ts:172` | 现 `env: {PATH, ...spec.env}` 且 `HOME` 被改成 workspace home；本地模式改成白名单透传 + 真实 `HOME` |
| G3 | claude 适配器分流 | `src/runtime/adapters/claude.ts` | `mode=local`：`env()` 只给 `DISABLE_*` 开关、`ANTHROPIC_MODEL`，以及 `token` 档的 `CLAUDE_CODE_OAUTH_TOKEN`，不设 `ANTHROPIC_*` / `CLAUDE_CONFIG_DIR`；`prepareHome` 不建 home；`sessionMeta` 增加 `settingSources`；`extra_env` 保留键规则按模式放宽 `CLAUDE_CONFIG_DIR` |
| G4 | 本地身份引导 | 新增 `src/local/bootstrap.ts`，`src/gateway.ts` 在 migrate 后调用 | 幂等建租户 / key / admin token，落盘 600；key 已存在时校验 argon2 哈希一致，不一致则重发并覆盖文件 |
| G5 | 内嵌数据库（S0 已验证可行） | `src/db/client.ts`、`src/db/migrate.ts`，依赖 `@electric-sql/pglite` + Kysely 的 PGlite dialect | `createDb` 按 URL 方案选择；pg 的 int8 解析器、`sanitizeJson` 保持；单连接下的事务/并发语义需回归 |
| G6 | 启动入口 | `package.json`、`src/index.ts` | `npm run local`；启动日志打印：模式、监听地址、数据目录、key 文件路径、claude 登录态检测结果 |
| G7 | 登录态预检 | 新增 `src/local/preflight.ts` | 启动时检查 `~/.claude/.credentials.json`（或 `config_dir`）是否存在及 `expiresAt`；缺失时打印「请先运行 `claude` 登录」，**不阻止启动**（dsh 仍可用）；`/admin/v1/status` 返回 `mode` 与 claude 登录态（不改 OpenAPI 的字段需评估，倾向放进已有的自由 `status` 对象） |
| G8 | 测试 | `test/unit`、`test/integration`、`test/e2e/claude-local-login.test.ts` | 单测：config 校验、环境白名单、适配器 env 不含 `ANTHROPIC*`、bootstrap 幂等；集成：PGlite 下现有 integration 套件；e2e：真实 claude + 登录态（缺登录态自动 skip，沿用 `ACPGW_REQUIRE_E2E`） |
| G9 | 文档 | `docs/`（现状）、`planning/` | docs 新增「本地模式」，运维文档把 docker 路线标为服务器模式；遵循仓库铁律：写代码 → 同步 docs → 新会话审查 → 提交 |

不改：`openapi.json` 的 `/v1` 契约、客户端类型、事件模型、`migrations/*.sql`、docker provider 与上游代理（server 模式保留）。

## 5. loomvec 仓库改动清单

| # | 改动 | 说明 |
| --- | --- | --- |
| L1 | 配置 | `agent.gateway.url` 不变；`agent.mcp.url_sandbox=http://127.0.0.1:38080/api/v1/mcp`（本机直连 api）；`agent.default_agent` 可设 `claude` |
| L2 | 凭据 | 已决策允许：`agent.gateway.api_key` / `admin_token` 为空且 `agent.gateway.url` 是回环地址时，api / agent 直接读取网关写出的文件（同机同用户）；非回环地址仍必须在 admin 里填。读取路径约定 `{state_dir}/local-api-key`、`local-admin-token`，`state_dir` 与网关同一约定（`XDG_DATA_HOME` / `~/.local/share/acp-gateway`） |
| L3 | 文档 | `docs/19` 增加「本地模式」章节并把 docker 部署写成可选；`deploy/compose` 的网关段保留为服务器模式；`planning/acp-gateway-p2.md` §9.7 标记被本方案取代 |
| L4 | 开发脚本 | `dev.sh` 可选地拉起 `npm run local`（`LOOMVEC_START_GATEWAY=1`），不默认启用 |
| L5 | 文档处理等非对话调用 | 已决策：走智能体会话（带 MCP 检索），不接 OpenAI 兼容接口。无额外改动，S3 验收里加一次文档处理类任务 |

`services/agent/src/loomvec/agent/gateway/` 的适配层基本不动；迁移脚本接入凭据回落，重放前缀窗口另行调整（见 docs/19 §4）。

## 5.5 S0 验证结论（2026-10-14）

脚本在一次性目录，补丁存档不入库。环境：Node 24、`claude-agent-acp` 0.89.1、真实订阅登录（Max）。

| 项 | 结论 |
| --- | --- |
| 不设任何 `ANTHROPIC*` / `CLAUDE*`，只传 `HOME`/`PATH` 等白名单环境 | ✅ 直接用 CLI 登录态完成提问（`end_turn`，有 usage） |
| `settingSources=["project"]` 隔离用户设置 | ✅ 生效。**而且必须做**：用户 `~/.claude/settings.json` 里有 `permissions` 放行规则、`model: sonnet`、`hooks`、全局 CLAUDE.md；不隔离时探针回答 YES（读到了 CLAUDE.md）且模型被用户设置覆盖，隔离后回答 NO、模型回到默认。其中 `permissions` 放行规则会绕过 loomvec 的审批策略 |
| 3 个进程 / 3 个 workspace 并发 | ✅ 各自正确回答 |
| 进程退出后新进程 `session/resume`（同 meta） | ✅ 记得上下文（暗号），能力里 `resume` 与 `loadSession` 均有；`settingSources` 恒定不变，不触发重建 |
| 登录态文件 | 本次验证期间未被改写（token 还剩约 5 小时，没覆盖到刷新） |
| 会话落点 | 写在 `~/.claude/projects/<cwd 编码>/`，已清理验证产生的目录。用户交互式 `claude --resume` 会看到网关创建的会话 |
| **OAuth 刷新竞态** | ⚠️ 没法实测，但查到是 Claude Code 的已知问题（多个会话共用 `.credentials.json`，刷新轮换 refresh token 时失败的一方可能把凭据写成登出状态，仍有 issue 未关闭，如 [#54443](https://github.com/anthropics/claude-code/issues/54443)、[#56339](https://claudeissues.com/issue/56339-multiple-cli-sessions-race-on-claude-credentials-json-token-refresh)）。所以默认推荐 `token` 档（`claude setup-token`），`login` 档只作回退 |
| PGlite（内嵌 PG）0.5.9 + `kysely-pglite-dialect` 1.3.1 | ✅ 网关全部 unit + integration 在 PGlite 上 **465 / 468 通过**（真实 PG 基线 468 全过）；落盘后重启复用正常，首次建库约 3 秒，重开约 0.3 秒，数据目录约 39 MB。3 个失败见下 |

PGlite 的 3 个失败与应对：
1. `readTurns` 与 `readTurn` 的一致性用例：落库比真实 PG 慢，断言时落库还在途，拿到的是未合并形态（语义等价）。测试对时序的假设，用例要改成先 `drain`。
2. `sessions:import` 的 003 回填用例：测试自己用 `pg.Client` 直连库，PGlite 没有；测试辅助层适配。
3. OpenAI 兼容接口「两个请求同时首次使用同一个 user」：赢家先插入 workspace 行、再建目录，输家读到行就继续，目录还没建出来，`realpath` 报 ENOENT 返回 500。**这是 `WorkspaceService.create` 本来就有的竞态**（真实 PG 下窗口更小，不是不存在），PGlite 只是放大了它。S1 要修（输家等目录就绪，或幂等地确保目录存在），与本地模式无关但应顺带修掉。

接入 PGlite 需要的实现点（已在副本里证实可行）：`createDb` 识别 `pglite:` 前缀；int8 解析器设为字符串（`parsers: {20: v => v}`）；迁移文件是多语句 SQL，PGlite 的 `query` 只支持单语句，需走 `exec`；`kysely-pglite-dialect` 的 peer 范围写到 `@electric-sql/pglite ^0.4.0`，而最新是 0.5.9，需要 `--legacy-peer-deps` 或自己写几十行 dialect（推荐自己写，避免依赖一个只声明到 0.4 的小包）；单进程独占，数据目录要加进程锁（防止两个网关实例同时打开同一目录导致损坏）；PGlite 只有一条连接，所有查询排队，事务期间会阻塞其他请求，对单用户本机足够。

## 6. 风险与待验证（先做 spike，再动工）

1. **共享 `~/.claude` 的并发与刷新**：S0 已确认是 Claude Code 的已知竞态，默认改用 `claude setup-token` 的长期 token（§3 第 2 条）；`token` 档尚未用真实 token 实测，S1 第一步由你跑一次 `claude setup-token` 后验证。
2. **`settingSources` 覆盖**：S0 已验证生效且不触发会话重建。
3. **PGlite 兼容**：S0 已验证可行（§5.5），不再需要退回外部 PG；仍保留 `postgres://` URL 作为可选。
4. **会话落点**：claude 会话 JSONL 写在 `~/.claude/projects/<cwd 编码>/`，用户交互式 `claude --resume` 会看到网关创建的会话。可接受，但要写进文档。
5. **本机 Bash 无沙箱**：风险靠 loopback + key + 默认 reject 策略兜底；若要放行写/联网类工具，必须由 loomvec 的审批矩阵显式决定。
6. **使用范围**：本模式用的是个人 CLI 登录态，仅限本机单用户自用，不对其他人提供服务。需要服务多人时走 server 模式 + API key。

## 7. 分期与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| S0 spike ✅ | 已完成，结论见 §5.5 | 认证 / 设置隔离 / 并发 / 恢复 / PGlite 均通过；刷新竞态改用 setup-token 规避 |
| S1 网关本地模式 | G1–G4、G6、G7、G8（单测+集成）、G9 | `npm run local` 冷启动成功；`curl /healthz`；用 key 建 workspace → session → turn，claude 回答，期间环境里无 `ANTHROPIC*`；typecheck / lint / test 全过 |
| S2 内嵌库 | G5 | 删除 `ACPGW_DATABASE_URL` 后全套 integration 通过 |
| S3 loomvec 对接 | L1–L4 | 本地 loomvec 里 claude 完成一次「检索 + 引用 + 续问」；dsh 迁移会话继续对话；网关重启后会话可恢复 |
| S4 收口 | 文档、记忆、观察期 | docs/19 与网关 docs 同步；此前 §9.2 的 root / 防火墙冒烟、L3 删除移出本期（仅服务器模式需要） |

## 8. 需要用户拍板

1. **数据库**：先做 S0 验证 PGlite；通过则内嵌，不通过则沿用开发机已有的 PG。（倾向：按验证结果定，不预先拍板。）
2. **凭据免粘贴**：本地模式下，是否允许 loomvec api 在 `agent.gateway.api_key` 为空时直接读取网关写出的 key 文件（同机同用户）。倾向：允许，仅限回环网关。
3. **非对话调用点**：「知识库文档处理」具体是哪些调用点——走智能体会话，还是走 OpenAI 兼容接口。（影响 L5，不影响 S1。）
4. 开工顺序：建议先 S0 → S1，S0 结论回来再定 S2 的取舍。

## 9. 实施记录（2026-10-14，未提交，待新会话审查）

按用户选择走 **`login` 档**（`token` 档已实现并有单测，但没有用真实 `setup-token` 令牌实测）。S1 与 S2（内嵌库）一起做完。

**网关仓库（`/home/jyy/acp-gateway`）**：G1–G9 全部完成。新增 `src/local/{state,bootstrap,preflight}.ts`、`src/db/pglite.ts`（自写 Kysely 方言，不依赖 `kysely-pglite-dialect`）；`config.ts` 加 `mode` / `state_dir` / claude 的 `auth` `config_dir` `setting_sources`；`LocalProvider` 环境白名单；`ClaudeAdapter` 本地模式分流；`npm run local`；`config.local.example.json`；docs 同步。顺带修了 `openai.ts` 里「并发首次使用同一个 user」的 workspace 目录竞态（变异验证：撤掉修复后 PGlite 下 3/3 变红）和内存库 int8 解析器不生效的问题。
- 测试：真实 PG 上 540 通过（10 个 docker / dsh e2e 前置缺失自动跳过）；`ACPGW_TEST_PGLITE=1` 在内存内嵌库上 unit + integration 522 全过；新增真实 Claude Code 的 e2e `test/e2e/claude-local-login.test.ts`（含「网关进程环境里放假的 `ANTHROPIC_API_KEY` 不得传给 agent」，变异验证有效）。typecheck / biome 通过，`openapi.json` 无变化。
- 真实冒烟：零配置 `npm run local` 冷启动 → 建库 / 密钥 / 本机身份 → 用生成的 key 建 workspace / session，claude 用 CLI 登录答出 PONG；`Bash` 被默认策略拒绝（`decided_by: policy`）；网关重启后 key 不变、库不重新迁移、会话带上下文恢复；`SIGTERM` 优雅退出并释放实例锁。

**loomvec 仓库**：`core/local_gateway.py`（回环网关的凭据文件回落）、`agent/config.py` 与 `api/services/acp_gateway_admin.py` 与迁移脚本接入；`AgentGatewaySettings.state_dir`；docs/19 新增 §2.1；新增 `test_e2e_local_mode.py`（loomvec agent → 本地模式网关 → 真实 claude，`system_config` 不配 key）和 core / agent / api 的单测。agent + api 相关 323 个测试通过。

**没做 / 待办**
- `ACPGW_LOCAL_API_KEY` 覆盖未实现（网关仅支持 `ACPGW_ADMIN_TOKEN` 同步写文件）。
- 登录态检测放在 `GET /admin/v1/runtimes`（返回 `mode` / `claude_auth`），而非 `/admin/v1/status`。
- 检索 + 引用（MCP 回调到运行中的 api）没有在本地模式网关上重新实测；链路与此前 local provider 冒烟相同，只多了 `settingSources` 与 HOME 的变化，建议上线前用只读库副本跑一次。
- `token` 档未用真实令牌实测。
- `dev.sh` 的 `LOOMVEC_START_GATEWAY` 未做（systemd 用户服务已在 §9.1 装好）；`dsh` 需要 `ACPGW_DEEPSEEK_API_KEY` 与 `agents.dsh.local_command`（指向 `.venv/bin/dsh`），本机未配。
- 旧会话迁移、真实环境切换仍按 `acp-gateway-p2.md` §9.2，须在放开提问前完成。

### 9.1 本机上线记录（2026-10-14 17:54）

- 真实库 `loomvec`（:35433）先 `pg_dump` 备份到 `~/backups/loomvec-pre-local-gateway-*.dump`，再 `python -m loomvec.api.init_db` 补上网关版需要的 5 列（`agent_session.agent / gateway_session_id / mcp_token_exp / replay_pending`、`agent_environment.gateway_workspace_id`）。存量 7 个会话都标为 `dsh`、`gateway_session_id` 为空。
- 网关装成 systemd 用户服务 `acp-gateway.service`（`enable --now`，linger 已开），监听 `127.0.0.1:38095`，状态目录 `~/.local/share/acp-gateway`，认证档位 `login`。
- `config/loomvec.json`（gitignore，原文件备份在 `~/backups/loomvec.json.pre-local-gateway`）：`agent.default_agent=claude`、`agent.mcp.url_sandbox=http://127.0.0.1:38080/api/v1/mcp`（缺省的 `host.docker.internal` 在本机模式下不可用）；agent 服务已重启读到新配置，`agent.gateway.api_key` 由状态目录文件回落提供。
- 用 dev 用户走网页同款接口验证（验证会话已删除）：纯文本问答、带 `scope_space_ids` 的知识库检索（`search_knowledge` 经 MCP 命中，引用 21 条）均正常。
- 观察到：MCP 工具 `list_my_spaces` 调用报 `Error executing tool`（`search_knowledge` 正常），在 `routes/mcp.py`，与本次改动无关，未处理。
- 旧的 7 个 dsh 会话：历史可看；在其中继续提问会建 dsh 的网关会话，而 dsh 的 DeepSeek key 未配置，会失败。旧会话迁移（`scripts/migrate_agent_to_gateway.py`）与 dsh 配置仍待做。

### 9.2 分片 / 知识图谱改用 Claude Haiku 5.5（2026-10-14 18:10）

- `config/loomvec.json` 的 `ai.llm` 改为 `base_url=http://127.0.0.1:38095/v1`、`model=agent:claude:haiku`（网关里 `haiku` = Haiku 5.5）、`json_response_format=false`（新增配置项，网关的 OpenAI 兼容接口拒绝 `response_format`）、`timeout_seconds=180`；旧配置（OpenCode Go / deepseek-v4.1-flash）备份在 `~/backups/loomvec.json.pre-llm-haiku`。重启了 celery worker 以读到新配置。
- 小文档（3 块）：1 次调用即完成分片 + 图谱，3 块、15 实体 7 关系，约 25 秒。
- 真实论文（Nature Medicine，11.8 万字符）：21 次 LLM 调用全部成功，0 个批次回退；95 块，199 实体 116 关系；入库总耗时 519 秒；合计输入 435,874 / 输出 110,005 token（单次平均约 2.1 万 / 5.2 千）。按 Haiku 5.5 的 API 价（$0.10 / $0.50 每百万 token）折算约 $0.08～0.10；订阅登录下没有按量计费，消耗的是订阅额度。
- 单次输入远大于批次正文：每次调用是一个独立的 Claude Code 会话，带约 1.8 万 token 的系统提示（约一半命中缓存）。
- 重跑（retry）同一文档不会清掉上一次抽出的实体，试验空间里出现了重复实体，是 loomvec 既有行为，未处理。

