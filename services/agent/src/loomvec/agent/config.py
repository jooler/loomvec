"""agent 服务运行配置装配（14 文档 §10）。

来源分层：
- AppConfig.agent（config/loomvec.json `agent` 段）：应用参数；
- Settings（env/.env，基础设施）：PG/Redis/auth 密钥、agent_service 段；
- `LOOMVEC_AGENT_STORAGE_ROOT`（env）优先于 json 的 storage_root（K8s PVC 场景）。

P2 起对话 / 文件 / 历史经 acp-gateway：网关地址等在 `agent.gateway`，API Key 与
default_agent / 默认模型的动态覆盖在 system_config（`load_gateway_connection`）。
以下 dsh 协议事实仅供旧 runtime 路径（L3 删除）参考。

dsh 协议事实（third_party 源码核实，影响实现）：
- `--profile sdk` 仅 initialize/session/prompt/shutdown 四方法；无 cancel/resume；
- 跨进程同 session_id 再 prompt 报 SessionAlreadyExistsError → 逻辑会话与
  dsh 物理会话解耦（物理 id = `{logical}-{gen}`，见 orchestrator）；
- 停止生成 = 终止 runtime 进程（JSONL append-only，崩溃一致）；
- 审批 fail-closed（sdk 组合无 answerer，沙箱升级自动拒绝）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import AgentSettings, Settings
from loomvec.core.local_gateway import API_KEY_FILE, local_gateway_secret


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """网关运行所需的全部配置切片（ lifespan 装配一次）。"""

    settings: Settings
    agent: AgentSettings
    # 旧 runtime 路径（L3 删除）的 LLM 覆盖；经网关后 ai.llm.* 对 agent 不再生效
    llm_base_url_override: str | None = None
    llm_api_key_override: str | None = None

    @property
    def storage_root(self) -> Path:
        override = self.settings.agent_service.storage_root_override or os.environ.get(
            "LOOMVEC_AGENT_STORAGE_ROOT", ""
        )
        return Path(override or self.agent.storage_root).resolve()

    @property
    def envs_root(self) -> Path:
        return self.storage_root / "envs"

    def env_dir(self, env_id: str) -> Path:
        return self.envs_root / env_id

    def dsh_home(self, env_id: str) -> Path:
        return self.env_dir(env_id) / "dsh-home"

    def workspace(self, env_id: str) -> Path:
        return self.env_dir(env_id) / "workspace"

    def uploads_dir(self, env_id: str, session_id: str) -> Path:
        return self.workspace(env_id) / ".loomvec" / "uploads" / session_id

    def sessions_root(self, env_id: str) -> Path:
        """dsh 会话 JSONL 根（<DSH_HOME>/sessions）。"""
        return self.dsh_home(env_id) / "sessions"

    # ---- 模型与凭据（14 文档 §5.1 三层次注入） ----

    @property
    def model_name(self) -> str:
        return self.agent.model.name

    @property
    def llm_base_url(self) -> str | None:
        return self.llm_base_url_override or self.settings.ai.llm.base_url

    @property
    def llm_api_key(self) -> str | None:
        return self.llm_api_key_override or self.settings.ai.llm.api_key


def load_agent_config() -> AgentRuntimeConfig:
    from loomvec.core.config import get_app_config, get_settings

    return AgentRuntimeConfig(settings=get_settings(), agent=get_app_config().agent)


# system_config 里与网关相关的键（admin「设置 → agent」写入；单源见
# services/api/.../admin_settings.py 注册表）。敏感项在库里是明文存储（回显才脱敏），直接读即可
KEY_GATEWAY_API_KEY = "agent.gateway.api_key"
KEY_GATEWAY_ADMIN_TOKEN = "agent.gateway.admin_token"  # 仅 services/api 的 ACP 服务代理使用
KEY_DEFAULT_AGENT = "agent.default_agent"
KEY_MODEL_CLAUDE = "agent.gateway.models.claude"
KEY_MODEL_DSH = "agent.gateway.models.dsh"
_GATEWAY_KEYS = (
    KEY_GATEWAY_API_KEY,
    KEY_DEFAULT_AGENT,
    KEY_MODEL_CLAUDE,
    KEY_MODEL_DSH,
)
AGENTS = ("claude", "dsh")


@dataclass(frozen=True)
class GatewayConnection:
    """一次请求内生效的网关配置：文件默认值 + system_config（DB 非空值优先，即时生效）。"""

    url: str
    # 不进 repr / 日志。admin 配置优先；否则仅回环网关读本机状态目录；都没有 = 未配置
    api_key: str | None = field(repr=False)
    request_timeout_s: float
    default_agent: str
    models: dict[str, str | None]  # agent -> 默认模型名（None = 用 agent 默认）

    def model_for(self, agent: str) -> str | None:
        return self.models.get(agent)


def _clean(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


async def load_gateway_connection(session: AsyncSession, agent: AgentSettings) -> GatewayConnection:
    """读 system_config 的网关键并与 `agent.*` 文件配置合并。"""
    from loomvec.core.db.models import SystemConfig

    rows = (
        (await session.execute(select(SystemConfig).where(SystemConfig.key.in_(_GATEWAY_KEYS))))
        .scalars()
        .all()
    )
    vals = {r.key: _clean(r.value) for r in rows}
    default_agent = vals.get(KEY_DEFAULT_AGENT)
    if default_agent not in AGENTS:
        default_agent = agent.default_agent
    models = {
        "claude": vals.get(KEY_MODEL_CLAUDE) or agent.model_for("claude"),
        "dsh": vals.get(KEY_MODEL_DSH) or agent.model_for("dsh"),
    }
    # admin 里没配 Key 时，回环地址的本机网关（mode=local）从它的状态目录读（同机同用户，免粘贴）
    api_key = vals.get(KEY_GATEWAY_API_KEY) or local_gateway_secret(
        agent.gateway.url, API_KEY_FILE, agent.gateway.state_dir
    )
    return GatewayConnection(
        url=agent.gateway.url,
        api_key=api_key,
        request_timeout_s=agent.gateway.request_timeout_s,
        default_agent=default_agent,
        models=models,
    )
