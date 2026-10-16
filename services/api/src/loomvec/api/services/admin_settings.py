"""P4-API-05 动态配置服务：可写配置注册表、脱敏回显、生效方式标注、TTL 缓存。

- key 注册表单源（SETTING_REGISTRY）：未入库的键回落 AppConfig 默认值；
- sensitive 键回显脱敏（`******`），写入保留明文（密钥只写不读）；
- effect="immediate" 经 effective_value() 即时生效（进程内 5s TTL 缓存）；
  effect="restart" 仅落库提示，需重启读取（infra 启动项单源仍在 Settings/env；
  ai 组经 core.config.apply_ai_overrides 在 api/worker 启动时合并，agent 的
  llm 地址/密钥另做会话级即时读取）；
- agent 组（ACP 网关接入）：DB 有非空值即覆盖 config/loomvec.json 的 agent.* 文件值
  （default_agent / gateway.models.*，注册默认值取自文件配置），业务侧按会话即时读取
  （effect=immediate）；agent.gateway.api_key / admin_token 两个敏感项无文件回落
  （空 = 未配置），admin_token 仅 /admin/acp-gateway/* 代理使用。

配置来源唯一：DB 有值即生效；未配置回落 config/loomvec.json（AppConfig），
不回落环境变量——应用参数（模型/检索/上传/图谱）不经 env 配置。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import get_app_config, get_settings
from loomvec.core.constants import CHUNK_PRESETS, EMBEDDING_MODELS
from loomvec.core.db.models import Space, SystemConfig
from loomvec.core.db.system_config import load_extraction_profiles
from loomvec.core.db.system_config import load_overrides as _load_overrides
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.graph.profiles import (
    CUSTOM_PROFILES_SETTING_KEY,
    available_profiles,
    parse_custom_profiles,
)

CACHE_TTL_SECONDS = 5.0
_CACHE: dict[str, tuple[float, Any]] = {}

MaskedValue = "******"


class SettingDef:
    def __init__(
        self,
        key: str,
        *,
        group: str,
        description: str,
        default: Any,
        sensitive: bool = False,
        effect: str = "immediate",
        admin_only: bool = False,
        validate: Callable[[Any], None] | None = None,
        validate_db: Callable[[AsyncSession, Any], Awaitable[None]] | None = None,
    ) -> None:
        self.key = key
        self.group = group
        self.description = description
        self.default = default
        self.sensitive = sensitive
        self.effect = effect  # immediate | restart
        # 仅 super_admin 可改（ai/sso/extensions 组及 pipeline.extraction_profiles）
        self.admin_only = admin_only
        self.validate = validate  # 写入前的值校验（None = 只做 JSON 兼容检查）
        self.validate_db = validate_db  # 写入前需查库的校验（如检查被引用情况），在 validate 之后


def _validate_base_url(value: Any) -> None:
    """base_url 可留空（回落 config/loomvec.json），非空必须为 http(s) URL。"""
    if value in (None, ""):
        return
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return
    raise ValidationError(reason="base_url 需以 http:// 或 https:// 开头（留空回落文件配置）")


def _make_api_style_validator(allowed: tuple[str, ...]) -> Callable[[Any], None]:
    def _validate(value: Any) -> None:
        if value in allowed:
            return
        raise ValidationError(reason=f"api_style 只能是 {' / '.join(allowed)}（留空回落文件配置）")

    return _validate


_GATEWAY_AGENTS = ("claude", "dsh")


def _validate_default_agent(value: Any) -> None:
    if value in _GATEWAY_AGENTS:
        return
    raise ValidationError(reason=f"default_agent 只能是 {' / '.join(_GATEWAY_AGENTS)}")


def _validate_model_name(value: Any) -> None:
    """默认模型名：留空（null / ""）= 用 agent 自身默认，非空需为短字符串。"""
    if value in (None, ""):
        return
    if isinstance(value, str) and len(value.strip()) <= 128 and "\n" not in value:
        return
    raise ValidationError(reason="模型名需为不超过 128 字符的单行字符串（留空用 agent 默认）")


def _validate_extraction_profiles(value: Any) -> None:
    """自定义抽取领域配置：{key: 配置 JSON}，结构与限制见 core.graph.profiles._ProfileSpec。"""
    try:
        parse_custom_profiles(value)
    except ValueError as e:
        raise ValidationError(f"自定义抽取配置不合法：{e}") from e


_MAX_LISTED_REFERENCES = 10


async def _validate_extraction_profiles_unreferenced(session: AsyncSession, value: Any) -> None:
    """删除自定义抽取配置前，拦截仍被引用的配置（空间选择、全局默认）。

    只拦本次写入「删掉」的 key：被删 key 若仍被未删除空间的 extraction_profile 或全局
    pipeline.extraction_profile 引用，管线会悄悄回落全局/general，用户的领域选择静默失效。
    本次写入前就已失效的引用不拦（否则任何一次写入都被历史遗留卡住）。
    """
    removed = set(await load_extraction_profiles(session)) - set(available_profiles(value))
    if not removed:
        return
    rows = (
        await session.execute(
            select(Space.id, Space.name, Space.extraction_profile)
            .where(Space.deleted_at.is_(None), Space.extraction_profile.in_(removed))
            .order_by(Space.created_at.asc())
        )
    ).all()
    global_key = get_settings().pipeline.extraction_profile
    if not rows and global_key not in removed:
        return
    refs = [f"空间「{name}」({sid}) 使用 {key}" for sid, name, key in rows[:_MAX_LISTED_REFERENCES]]
    if len(rows) > _MAX_LISTED_REFERENCES:
        refs.append(f"……共 {len(rows)} 个空间")
    if global_key in removed:
        refs.append(f"全局默认 pipeline.extraction_profile 使用 {global_key}")
    raise ValidationError(
        "自定义抽取配置仍被引用，无法删除："
        + "；".join(refs)
        + "。请先把这些空间改用其他配置（或清空），再删除配置",
        spaces=[
            {"id": str(sid), "name": name, "extraction_profile": key}
            for sid, name, key in rows[:_MAX_LISTED_REFERENCES]
        ],
        space_count=len(rows),
        global_profile=global_key if global_key in removed else None,
    )


def _build_registry() -> dict[str, SettingDef]:
    s = get_app_config()  # 应用参数默认值单源：config/loomvec.json（非 env）

    defs: list[SettingDef] = [
        # 模型与检索（P4-ADM-05 白名单）
        SettingDef(
            "models.embedding_whitelist",
            group="models",
            description="空间可选嵌入模型白名单（运营配置，用户只能从中选）",
            default=list(EMBEDDING_MODELS),
        ),
        SettingDef(
            "models.chunk_presets",
            group="models",
            description="空间可选分片预设",
            default=list(CHUNK_PRESETS.keys()),
        ),
        # 检索参数（即时生效）
        SettingDef(
            "search.default_top_k",
            group="retrieval",
            description="默认召回条数",
            default=s.search.default_top_k,
        ),
        SettingDef(
            "search.max_top_k",
            group="retrieval",
            description="召回条数上限",
            default=s.search.max_top_k,
        ),
        SettingDef(
            "search.rerank_enabled",
            group="retrieval",
            description="重排开关",
            default=s.search.rerank_enabled,
        ),
        SettingDef(
            "search.rrf_k",
            group="retrieval",
            description="RRF 平滑常数",
            default=s.search.rrf_k,
        ),
        SettingDef(
            "search.image_search_enabled",
            group="retrieval",
            description="以文搜图召回",
            default=s.search.image_search_enabled,
        ),
        # P3-API-02 图谱开关（运维可全局关；关闭后检索退化为 L1+L2，写入降级）
        SettingDef(
            "graph.enabled",
            group="retrieval",
            description="图谱总开关（L3/L4 召回与抽取写入）",
            default=s.graph.enabled,
        ),
        # 自定义抽取领域配置（与内置 general/academic/biomedical/legal 并列供空间选择；
        # worker 每次 chunk 步读取，即时生效，只影响之后新入库或重新处理的文件；
        # 删除仍被空间/全局默认引用的配置会被拒，见 _validate_extraction_profiles_unreferenced）。
        # admin_only：配置文本会原样拼进所有空间抽取 LLM 的 system prompt（提示词注入面），
        # operator 的 admin:write 不够，仅 super_admin 可改（前端 /settings/pipeline 同口径）
        SettingDef(
            CUSTOM_PROFILES_SETTING_KEY,
            group="pipeline",
            description="自定义抽取领域配置（JSON：{key: 配置}，字段同内置配置，不得覆盖内置 key）",
            default={},
            admin_only=True,
            validate=_validate_extraction_profiles,
            validate_db=_validate_extraction_profiles_unreferenced,
        ),
        # 上传策略（即时生效）
        SettingDef(
            "upload.max_size_bytes",
            group="upload",
            description="单文件大小上限（字节）",
            default=s.upload.max_size_bytes,
        ),
        SettingDef(
            "upload.allowed_extensions",
            group="upload",
            description="允许的扩展名",
            default=list(s.upload.allowed_extensions),
        ),
        SettingDef(
            "upload.presign_expires_seconds",
            group="upload",
            description="预签名有效期（秒）",
            default=s.upload.presign_expires_seconds,
        ),
    ]

    # AI 供方：DB 有非空值即覆盖 config/loomvec.json——api/worker 网关启动时
    # 经 core.config.apply_ai_overrides 合并（effect=restart），agent 的
    # llm 地址/密钥另做会话级即时读取。本地模型推理（vLLM/Infinity）的
    # 端点与协议见 docs/16-本地模型推理.md。
    _AI_CHANNELS: dict[str, str] = {
        "llm": "LLM",
        "embedding": "向量化",
        "rerank": "重排",
        "vlm": "VLM（图片描述）",
        "clip": "CLIP（图文向量）",
    }
    # api_style 各通道支持面（网关实现为准）：rerank 无 infinity 通路
    _API_STYLES: dict[str, tuple[str, ...]] = {
        "rerank": ("openai", "dashscope"),
        "clip": ("openai", "dashscope", "infinity"),
    }
    for channel, title in _AI_CHANNELS.items():
        provider_cfg = getattr(s.ai, channel)
        defs.extend(
            [
                SettingDef(
                    f"ai.{channel}.base_url",
                    group="ai",
                    description=f"{title}服务地址（本地推理见 docs/16-本地模型推理.md）",
                    default=provider_cfg.base_url,
                    admin_only=True,
                    effect="restart",
                    validate=_validate_base_url,
                ),
                SettingDef(
                    f"ai.{channel}.api_key",
                    group="ai",
                    description=f"{title}密钥",
                    default=None,
                    sensitive=True,
                    admin_only=True,
                    effect="restart",
                ),
                SettingDef(
                    f"ai.{channel}.model",
                    group="ai",
                    description=f"{title}模型名",
                    default=provider_cfg.model,
                    admin_only=True,
                    effect="restart",
                ),
            ]
        )
        if channel in _API_STYLES:
            styles = _API_STYLES[channel]
            defs.append(
                SettingDef(
                    f"ai.{channel}.api_style",
                    group="ai",
                    description=f"{title}协议（{' / '.join(styles)}）",
                    default=provider_cfg.api_style,
                    admin_only=True,
                    effect="restart",
                    validate=_make_api_style_validator(styles),
                )
            )

    # ACP 网关接入（P2）：agent.default_agent / gateway.models 的文件默认值取自 AgentSettings
    agent_cfg = s.agent
    gateway_models = agent_cfg.gateway.models
    defs.extend(
        [
            SettingDef(
                "agent.default_agent",
                group="agent",
                description="新建智能体会话使用的 agent（claude / dsh）",
                default=agent_cfg.default_agent,
                admin_only=True,
                validate=_validate_default_agent,
            ),
            SettingDef(
                "agent.gateway.models.claude",
                group="agent",
                description="claude agent 默认模型名（留空 = 用 agent 默认）",
                default=gateway_models.claude,
                admin_only=True,
                validate=_validate_model_name,
            ),
            SettingDef(
                "agent.gateway.models.dsh",
                group="agent",
                description="dsh agent 默认模型名（留空回落 agent.model.name）",
                default=gateway_models.dsh,
                admin_only=True,
                validate=_validate_model_name,
            ),
            SettingDef(
                "agent.gateway.api_key",
                group="agent",
                description="loomvec 租户的 ACP 网关 API Key（智能体对话用；只写）",
                default=None,
                sensitive=True,
                admin_only=True,
            ),
            SettingDef(
                "agent.gateway.admin_token",
                group="agent",
                description="ACP 网关管理 token（ACPGW_ADMIN_TOKEN，仅「ACP 服务」代理用；只写）",
                default=None,
                sensitive=True,
                admin_only=True,
            ),
        ]
    )

    defs.extend(
        [
            # SSO（敏感）
            SettingDef(
                "sso.default_issuer",
                group="sso",
                description="平台级默认 IdP issuer（租户未绑定域时兜底）",
                default=None,
                admin_only=True,
            ),
            SettingDef(
                "sso.default_client_secret",
                group="sso",
                description="平台级默认 OIDC client secret",
                default=None,
                sensitive=True,
                admin_only=True,
            ),
            # 扩展
            SettingDef(
                "extensions.manifests",
                group="extensions",
                description="extensions/ manifest 列表与启停",
                default=[],
                admin_only=True,
            ),
            # 启动项（需重启）
            SettingDef(
                "infra.postgres_url",
                group="infra",
                description="PG 连接（需重启；单源为环境变量，此处仅展示）",
                default=get_settings().postgres.url,
                effect="restart",
                sensitive=True,
                admin_only=True,
            ),
            SettingDef(
                "infra.milvus_uri",
                group="infra",
                description="Milvus URI（需重启；单源为环境变量，此处仅展示）",
                default=get_settings().milvus.uri,
                effect="restart",
                admin_only=True,
            ),
        ]
    )
    return {d.key: d for d in defs}


SETTING_REGISTRY = _build_registry()


def mask(value: Any) -> Any:
    """敏感值回显：未设置（None / 空串，写入 "" 即清除）回显 None，已设置回显 ******。"""
    if value is None or value == "":
        return None
    return MaskedValue


async def load_overrides(session: AsyncSession) -> dict[str, Any]:
    return await _load_overrides(session)


async def list_settings(session: AsyncSession) -> list[dict[str, Any]]:
    overrides = await load_overrides(session)
    out = []
    for d in SETTING_REGISTRY.values():
        value = overrides.get(d.key, d.default)
        out.append(
            {
                "key": d.key,
                "group": d.group,
                "description": d.description,
                "value": mask(value) if d.sensitive else value,
                "sensitive": d.sensitive,
                "effect": d.effect,
                "admin_only": d.admin_only,
            }
        )
    return out


async def get_setting(session: AsyncSession, key: str) -> dict[str, Any]:
    d = SETTING_REGISTRY.get(key)
    if d is None:
        raise NotFoundError(resource="setting", id=key)
    row = (
        await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    ).scalar_one_or_none()
    value = row.value if row else d.default
    return {
        "key": d.key,
        "group": d.group,
        "description": d.description,
        "value": mask(value) if d.sensitive else value,
        "sensitive": d.sensitive,
        "effect": d.effect,
        "admin_only": d.admin_only,
    }


def validate_value(value: Any) -> None:
    """值类型白名单：JSONB 兼容标量/列表/字典；拒绝过深嵌套与超大值。"""
    import json

    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise ValidationError(reason=f"配置值不可序列化: {e}") from e
    if len(encoded) > 64 * 1024:
        raise ValidationError(reason="配置值过大（>64KB）")


async def put_setting(
    session: AsyncSession, key: str, value: Any, updated_by: Any
) -> dict[str, Any]:
    d = SETTING_REGISTRY.get(key)
    if d is None:
        raise NotFoundError(resource="setting", id=key)
    validate_value(value)
    if d.validate is not None:
        d.validate(value)
    if d.validate_db is not None:
        await d.validate_db(session, value)
    row = (
        await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    ).scalar_one_or_none()
    if row is None:
        row = SystemConfig(key=key, value=value, sensitive=d.sensitive, updated_by=updated_by)
        session.add(row)
    else:
        row.value = value
        row.sensitive = d.sensitive
        row.updated_by = updated_by
    _CACHE.pop(key, None)  # 写后立即使缓存失效（即时生效语义）
    return {
        "key": d.key,
        "value": mask(value) if d.sensitive else value,
        "effect": d.effect,
    }


async def effective_value(session: AsyncSession, key: str) -> Any:
    """业务侧动态读取（即时生效组）：DB 覆盖 → 注册表默认 → 进程 Settings。"""
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    d = SETTING_REGISTRY.get(key)
    value = None
    if d is not None:
        row = (
            await session.execute(select(SystemConfig).where(SystemConfig.key == key))
        ).scalar_one_or_none()
        value = row.value if row else d.default
    _CACHE[key] = (now, value)
    return value
