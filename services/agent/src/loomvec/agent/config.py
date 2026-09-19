"""agent 服务运行配置装配（14 文档 §10）。

来源分层：
- AppConfig.agent（config/loomvec.json `agent` 段）：应用参数；
- Settings（env/.env，基础设施）：PG/Redis/auth 密钥、agent_service 段；
- `LOOMVEC_AGENT_STORAGE_ROOT`（env）优先于 json 的 storage_root（K8s PVC 场景）。

dsh 协议事实（third_party 源码核实，影响实现）：
- `--profile sdk` 仅 initialize/session/prompt/shutdown 四方法；无 cancel/resume；
- 跨进程同 session_id 再 prompt 报 SessionAlreadyExistsError → 逻辑会话与
  dsh 物理会话解耦（物理 id = `{logical}-{gen}`，见 orchestrator）；
- 停止生成 = 终止 runtime 进程（JSONL append-only，崩溃一致）；
- 审批 fail-closed（sdk 组合无 answerer，沙箱升级自动拒绝）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import AgentSettings, Settings


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """网关运行所需的全部配置切片（ lifespan 装配一次）。"""

    settings: Settings
    agent: AgentSettings
    # 运维端「AI 供方配置」写入 system_config（DB），优先于 config/loomvec.json；
    # 由 routes 在提问时读出，manager spawn 时经 dataclasses.replace 注入
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


async def load_llm_overrides(session: AsyncSession) -> tuple[str | None, str | None]:
    """读运维端写入的 ai.llm.base_url / ai.llm.api_key（DB 有非空值即覆盖文件配置）。"""
    from loomvec.core.db.models import SystemConfig

    rows = (
        await session.execute(
            select(SystemConfig).where(
                SystemConfig.key.in_(("ai.llm.base_url", "ai.llm.api_key"))
            )
        )
    ).scalars().all()
    vals = {r.key: (r.value.strip() if isinstance(r.value, str) else "") for r in rows}
    return vals.get("ai.llm.base_url") or None, vals.get("ai.llm.api_key") or None
