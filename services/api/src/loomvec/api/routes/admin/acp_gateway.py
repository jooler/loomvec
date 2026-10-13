"""ACP 网关管理代理（/api/v1/admin/acp-gateway/*）：租户、API Key、配额、用量、运行时状态。

把 ACP 网关 `/admin/v1/*` 的运维能力接入运维端（P2 合同 §5），不是通用转发——
只放行 services/acp_gateway_admin.py 白名单里的 10 个操作：

| 路由 | 网关操作 | 角色 |
|---|---|---|
| GET  /config | —（本地配置 + 凭据是否已配） | 三角色只读 |
| GET  /status | healthz + runtimes | 三角色只读 |
| GET  /tenants、/tenants/{id}、…/api-keys、…/usage | 对应读接口 | 三角色只读 |
| POST /tenants、PATCH /tenants/{id}（含配额 / settings）| 创建 / 修改租户 | 仅 super_admin |
| POST /tenants/{id}/api-keys、POST /api-keys/{id}/revoke | 签发 / 吊销 Key | 仅 super_admin |
| POST /workspaces/{id}/stop | 强制停止 workspace | super_admin + operator |

凭据与审计：
- 网关管理 token 取 admin 动态配置 `agent.gateway.admin_token`，只出现在发往网关的请求头；
- 每个写操作必须带理由（reason），成功后显式写审计（action 前缀 `admin.acp_gateway.`）；
  审计详情只记非敏感字段（Key 的 id / 前缀 / scope / 过期时间），**从不含 Key 明文**；
- 签发响应是 API Key 明文唯一出现处（`Cache-Control: no-store`），其余接口只含前缀；
- 网关错误统一转平台错误结构（services/acp_gateway_admin._map_error）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.api.services import acp_gateway_admin as gw
from loomvec.core.constants import PLATFORM_ROLE_SUPER_ADMIN
from loomvec.core.errors import LoomvecError, PermissionDeniedError

router = APIRouter(prefix="/acp-gateway", tags=["admin-acp-gateway"])
logger = structlog.get_logger("loomvec.api.admin.acp_gateway")

AgentName = Literal["claude", "dsh"]
KeyScope = Literal[
    "sessions:write",
    "sessions:read",
    "files:read",
    "files:write",
    "chat:completions",
    "acp:connect",
]
Reason = Annotated[str, Field(min_length=1, max_length=500)]

AUDIT_OBJECT_TYPE = "acp_gateway"


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


async def get_gateway_admin(session: AsyncSession = Depends(get_session)) -> gw.GatewayAdminClient:
    """网关管理客户端（按当前 admin 配置构造；测试经 dependency_overrides 换成 MockTransport）。"""
    return await gw.build_client(session)


async def require_acp_manage(
    identity: Identity = Depends(require_admin("admin:write")),
) -> Identity:
    """租户 / Key / 配额写操作：仅 super_admin（operator 只读 + 强制停止 workspace）。"""
    if PLATFORM_ROLE_SUPER_ADMIN not in identity.roles:
        raise PermissionDeniedError(
            reason="ACP 服务的租户 / API Key / 配额写操作仅 super_admin 可执行",
            required_roles=[PLATFORM_ROLE_SUPER_ADMIN],
        )
    return identity


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class AcpTenantQuotas(BaseModel):
    """网关租户配额（缺省 = 不限制）。PATCH 时整体替换，未列出的项即取消限制。"""

    model_config = ConfigDict(extra="forbid")

    max_workspaces: int | None = Field(default=None, ge=0)
    max_concurrent_turns: int | None = Field(default=None, ge=0)
    daily_tokens: int | None = Field(default=None, ge=0)
    monthly_tokens: int | None = Field(default=None, ge=0)
    upstream_rpm: int | None = Field(default=None, ge=0)
    upstream_concurrency: int | None = Field(default=None, ge=0)
    max_turn_seconds: int | None = Field(default=None, ge=1)
    max_disk_bytes: int | None = Field(default=None, ge=0)


class AcpMcpPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        default_factory=list, max_length=64
    )
    allow_stdio: bool = False

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts_shape(cls, hosts: list[str]) -> list[str]:
        for h in hosts:
            if "://" in h or "/" in h or " " in h:
                raise PydanticCustomError(
                    "allowed_hosts_shape", "allowed_hosts 条目应为 host / host:port / *.suffix"
                )
        return hosts

    @field_validator("allow_stdio")
    @classmethod
    def _no_stdio(cls, v: bool) -> bool:
        # stdio MCP 会在 agent 容器里执行任意命令；loomvec 只用 http MCP，不经运维端放开
        if v:
            raise PydanticCustomError("stdio_forbidden", "运维端不允许开启 allow_stdio")
        return v


class AcpTenantSettings(BaseModel):
    """网关租户 settings：mcp 段按规则校验，其余键原样保留（PATCH 时整体替换）。"""

    model_config = ConfigDict(extra="allow")

    mcp: AcpMcpPolicy | None = None


def _dump(model: BaseModel | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_dump(exclude_none=True)


class AcpTenantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    default_agent: AgentName | None = None
    quotas: AcpTenantQuotas | None = None
    settings: AcpTenantSettings | None = None
    reason: Reason


class AcpTenantPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "suspended"] | None = None
    default_agent: AgentName | None = None
    # quotas / settings 是整体替换语义（网关 PATCH 即覆盖），调用方需带完整对象
    quotas: AcpTenantQuotas | None = None
    settings: AcpTenantSettings | None = None
    reason: Reason

    @model_validator(mode="after")
    def _has_change(self) -> AcpTenantPatchRequest:
        if not self.model_fields_set - {"reason"}:
            raise PydanticCustomError("no_change", "至少修改一项")
        return self


class AcpApiKeyIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=256)
    scopes: list[KeyScope] = Field(min_length=1)
    rate_limit_rps: float | None = Field(default=None, gt=0, le=10000)
    expires_at: datetime | None = None
    reason: Reason

    @field_validator("scopes")
    @classmethod
    def _unique(cls, scopes: list[str]) -> list[str]:
        return list(dict.fromkeys(scopes))

    @field_validator("expires_at")
    @classmethod
    def _future_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise PydanticCustomError("tz_required", "expires_at 需带时区")
        v = v.astimezone(UTC)
        if v <= datetime.now(UTC):
            raise PydanticCustomError("expires_in_past", "expires_at 必须晚于当前时间")
        return v


class AcpReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _paged(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    """网关列表不分页，代理层按平台管理 API 的分页约定切片。"""
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
    }


def _items(payload: Any) -> list[dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _tenant_snapshot(t: dict[str, Any]) -> dict[str, Any]:
    return {k: t.get(k) for k in ("name", "status", "default_agent", "quotas", "settings")}


async def _audit(
    session: AsyncSession,
    *,
    identity: Identity,
    request: Request,
    action: str,
    object_id: str | None,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """网关写操作已成功后记审计；审计落库失败只告警（网关侧另有 audit_log 兜底）。"""
    try:
        await record_audit(
            session,
            identity=identity,
            action=f"admin.acp_gateway.{action}",
            object_type=AUDIT_OBJECT_TYPE,
            object_id=object_id,
            reason=reason,
            before=before,
            after=after,
            request=request,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("acp_gateway_audit_failed", action=action, object_id=object_id)


# ---------------------------------------------------------------------------
# 只读：配置 / 状态 / 租户 / Key / 用量
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """接入配置概览（页面提示用）：网关地址只读展示，两个凭据只告知「是否已配置」。"""
    return {
        "url": gw.gateway_url(),
        "admin_token_configured": await gw.is_configured(session, gw.SETTING_ADMIN_TOKEN),
        "api_key_configured": await gw.is_configured(session, gw.SETTING_API_KEY),
        "required_key_scopes": list(gw.REQUIRED_KEY_SCOPES),
        "mcp_sandbox": gw.mcp_sandbox_target(),
    }


@router.get("/status")
async def get_status(
    identity: Identity = Depends(require_admin("admin:read")),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    """运行时状态卡：网关健康 + 活跃 runtime。网关故障降级为 200 + error，不抛错。"""
    out: dict[str, Any] = {
        "url": gw.gateway_url(),
        "configured": client.has_token,
        "reachable": False,
        "healthy": False,
        "agents": [],
        "provider": None,
        "running_turns": 0,
        "runtimes": [],
        "error": None,
    }
    try:
        out["healthy"] = await client.healthz()
        out["reachable"] = True
    except LoomvecError as e:
        out["error"] = e.message
        return out
    if not client.has_token:
        out["error"] = "未配置 agent.gateway.admin_token，无法读取 runtime 明细"
        return out
    try:
        rt = await client.call("runtimes.list")
    except LoomvecError as e:
        out["error"] = e.message
        return out
    if isinstance(rt, dict):
        out["agents"] = rt.get("agents") or []
        out["provider"] = rt.get("provider")
        out["running_turns"] = rt.get("running_turns") or 0
        out["runtimes"] = _items(rt)
    return out


@router.get("/tenants")
async def list_tenants(
    q: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    items = _items(await client.call("tenants.list"))
    if q:
        needle = q.lower()
        items = [t for t in items if needle in str(t.get("name", "")).lower()]
    return _paged(items, limit, offset)


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    identity: Identity = Depends(require_admin("admin:read")),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    return await client.call("tenants.get", path_params={"tenant_id": tenant_id})


@router.get("/tenants/{tenant_id}/api-keys")
async def list_api_keys(
    tenant_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    payload = await client.call("keys.list", path_params={"tenant_id": tenant_id})
    return _paged(_items(payload), limit, offset)


@router.get("/tenants/{tenant_id}/usage")
async def get_usage(
    tenant_id: str,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    identity: Identity = Depends(require_admin("admin:read")),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    """用量：配额口径当前占用 + 按天聚合（网关默认回看 30 天，`to` 为开区间上界）。"""
    params = {k: v.isoformat() for k, v in (("from", from_), ("to", to)) if v is not None}
    return await client.call("usage.get", path_params={"tenant_id": tenant_id}, params=params)


# ---------------------------------------------------------------------------
# 写：租户 / 配额 / Key（仅 super_admin）
# ---------------------------------------------------------------------------


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: AcpTenantCreateRequest,
    request: Request,
    identity: Identity = Depends(require_acp_manage),
    session: AsyncSession = Depends(get_session),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": body.name}
    if body.default_agent:
        payload["default_agent"] = body.default_agent
    if body.quotas is not None:
        payload["quotas"] = _dump(body.quotas)
    if body.settings is not None:
        payload["settings"] = body.settings.model_dump(exclude_none=True)
    tenant = await client.call("tenants.create", json=payload)
    await _audit(
        session,
        identity=identity,
        request=request,
        action="tenant_create",
        object_id=tenant.get("id"),
        reason=body.reason,
        after=_tenant_snapshot(tenant),
    )
    return tenant


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(
    tenant_id: str,
    body: AcpTenantPatchRequest,
    request: Request,
    identity: Identity = Depends(require_acp_manage),
    session: AsyncSession = Depends(get_session),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    """修改租户 / 暂停 / 配额 / settings。`quotas`、`settings` 整体替换，需带完整对象。"""
    path = {"tenant_id": tenant_id}
    before = await client.call("tenants.get", path_params=path)
    payload: dict[str, Any] = {}
    for field in ("name", "status", "default_agent"):
        if field in body.model_fields_set and getattr(body, field) is not None:
            payload[field] = getattr(body, field)
    if "quotas" in body.model_fields_set:
        payload["quotas"] = _dump(body.quotas) or {}
    if "settings" in body.model_fields_set:
        payload["settings"] = body.settings.model_dump(exclude_none=True) if body.settings else {}
    tenant = await client.call("tenants.update", path_params=path, json=payload)
    await _audit(
        session,
        identity=identity,
        request=request,
        action="tenant_update",
        object_id=tenant_id,
        reason=body.reason,
        before={k: v for k, v in _tenant_snapshot(before).items() if k in payload},
        after={k: v for k, v in _tenant_snapshot(tenant).items() if k in payload},
    )
    return tenant


@router.post("/tenants/{tenant_id}/api-keys", status_code=201)
async def issue_api_key(
    tenant_id: str,
    body: AcpApiKeyIssueRequest,
    request: Request,
    response: Response,
    identity: Identity = Depends(require_acp_manage),
    session: AsyncSession = Depends(get_session),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    """签发网关 API Key。响应里的 `api_key` 是明文唯一出现处，审计与日志不含它。"""
    payload: dict[str, Any] = {"scopes": body.scopes}
    if body.name:
        payload["name"] = body.name
    if body.rate_limit_rps is not None:
        payload["rate_limit_rps"] = body.rate_limit_rps
    if body.expires_at is not None:
        payload["expires_at"] = body.expires_at.isoformat().replace("+00:00", "Z")
    issued = await client.call("keys.issue", path_params={"tenant_id": tenant_id}, json=payload)
    await _audit(
        session,
        identity=identity,
        request=request,
        action="api_key_issue",
        object_id=issued.get("id"),
        reason=body.reason,
        # 只取白名单字段：明文 api_key 绝不进审计
        after={
            "tenant_id": issued.get("tenant_id") or tenant_id,
            "prefix": issued.get("prefix"),
            "name": issued.get("name"),
            "scopes": issued.get("scopes"),
            "rate_limit_rps": issued.get("rate_limit_rps"),
            "expires_at": issued.get("expires_at"),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return issued


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: str,
    body: AcpReasonRequest,
    request: Request,
    identity: Identity = Depends(require_acp_manage),
    session: AsyncSession = Depends(get_session),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    result = await client.call("keys.revoke", path_params={"key_id": key_id})
    await _audit(
        session,
        identity=identity,
        request=request,
        action="api_key_revoke",
        object_id=key_id,
        reason=body.reason,
        after={"revoked": True},
    )
    return result


# ---------------------------------------------------------------------------
# 写：强制停止 workspace（super_admin + operator）
# ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/stop")
async def stop_workspace(
    workspace_id: str,
    body: AcpReasonRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    client: gw.GatewayAdminClient = Depends(get_gateway_admin),
) -> dict[str, Any]:
    """取消进行中的回合并停止该 workspace 的 agent 进程（对话数据保留，下次提问自动重启）。"""
    result = await client.call("workspaces.stop", path_params={"workspace_id": workspace_id})
    await _audit(
        session,
        identity=identity,
        request=request,
        action="workspace_stop",
        object_id=workspace_id,
        reason=body.reason,
        after={"stopped": True},
    )
    return result
