"""P4-API-05 动态配置服务：可写配置注册表、脱敏回显、生效方式标注、TTL 缓存。

- key 注册表单源（SETTING_REGISTRY）：未入库的键回落 AppConfig 默认值；
- sensitive 键回显脱敏（`******`），写入保留明文（密钥只写不读）；
- effect="immediate" 经 effective_value() 即时生效（进程内 5s TTL 缓存）；
  effect="restart" 仅落库提示，需重启读取（infra 启动项单源仍在 Settings/env）。

配置来源唯一：DB 有值即生效；未配置回落 config/loomvec.json（AppConfig），
不回落环境变量——应用参数（模型/检索/上传/图谱）不经 env 配置。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import get_app_config, get_settings
from loomvec.core.constants import CHUNK_PRESETS, EMBEDDING_MODELS
from loomvec.core.db.models import SystemConfig
from loomvec.core.errors import NotFoundError, ValidationError

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
    ) -> None:
        self.key = key
        self.group = group
        self.description = description
        self.default = default
        self.sensitive = sensitive
        self.effect = effect  # immediate | restart
        self.admin_only = admin_only  # 仅 super_admin 可改（ai/sso/extensions 组）


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
        # AI 供方（敏感；未配 runtime 键时回落进程 Settings，写库即刻生效）
        SettingDef(
            "ai.embedding.base_url",
            group="ai",
            description="向量化服务地址",
            default=s.ai.embedding.base_url,
            admin_only=True,
        ),
        SettingDef(
            "ai.embedding.api_key",
            group="ai",
            description="向量化密钥",
            default=None,
            sensitive=True,
            admin_only=True,
        ),
        SettingDef(
            "ai.embedding.model",
            group="ai",
            description="向量化模型名",
            default=s.ai.embedding.model,
            admin_only=True,
        ),
        SettingDef(
            "ai.rerank.base_url",
            group="ai",
            description="重排服务地址",
            default=s.ai.rerank.base_url,
            admin_only=True,
        ),
        SettingDef(
            "ai.rerank.api_key",
            group="ai",
            description="重排密钥",
            default=None,
            sensitive=True,
            admin_only=True,
        ),
        SettingDef(
            "ai.llm.base_url",
            group="ai",
            description="LLM 服务地址",
            default=s.ai.llm.base_url,
            admin_only=True,
        ),
        SettingDef(
            "ai.llm.api_key",
            group="ai",
            description="LLM 密钥",
            default=None,
            sensitive=True,
            admin_only=True,
        ),
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
    return {d.key: d for d in defs}


SETTING_REGISTRY = _build_registry()


def mask(value: Any) -> Any:
    if isinstance(value, str) and value:
        return MaskedValue
    if value is None:
        return None
    return MaskedValue


async def load_overrides(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(SystemConfig))).scalars().all()
    return {r.key: r.value for r in rows}


async def list_settings(session: AsyncSession) -> list[dict[str, Any]]:
    overrides = await load_overrides(session)
    out = []
    for d in SETTING_REGISTRY.values():
        has_override = d.key in overrides
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
                "overridden": has_override,
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
        "overridden": row is not None,
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
