"""企业化域：OIDC 绑定 / 动态配置 / OAuth / Webhook（迁移 0004）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, SoftDeleteMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values

# ---------------------------------------------------------------------------
# P4-API-03/05/07/08 企业化表：OIDC 域绑定、动态配置、OAuth 应用、Webhook、
# 重嵌入任务。迁移 0004。
# ---------------------------------------------------------------------------


class TenantOidcBinding(UuidPkMixin, TimestampMixin, Base):
    """租户 OIDC 域绑定（P4-API-03/06）：该邮箱域的 IdP 登录用户自动归属本租户。

    issuer 为 IdP 签发者（如 https://keycloak:8443/realms/loomvec）；
    client_secret 明文落库仅限 dev/自托管场景，生产经 K8s Secret 注入（P4-INF-04）。
    """

    __tablename__ = "tenant_oidc_binding"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)  # 邮箱域
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SystemConfig(UuidPkMixin, TimestampMixin, Base):
    """动态配置（P4-API-05）：key 单源（见 api/routes/admin/settings.py 注册表），
    value JSONB；sensitive 键读取时脱敏回显。生效方式由注册表标注（即时/需重启）。"""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    value: Mapped[dict | list | str | int | float | bool] = mapped_column(JSONB, nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)


class OauthClientStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OauthClient(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """OAuth 应用（P4-API-07）：第三方应用接入。secret 仅存哈希；
    scopes 授予范围不超平台 scope 目录；用户授权后令牌权限不超用户本人。"""

    __tablename__ = "oauth_client"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OauthClientStatus] = mapped_column(
        Enum(OauthClientStatus, name="oauth_client_status", values_callable=enum_values),
        nullable=False,
        default=OauthClientStatus.ACTIVE,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(512), nullable=True)


class OauthAuthorizationCode(UuidPkMixin, TimestampMixin, Base):
    """OAuth 授权码：一次性、短时效（10 分钟）。"""

    __tablename__ = "oauth_authorization_code"

    code_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OauthAccessToken(UuidPkMixin, TimestampMixin, Base):
    """OAuth 用户级访问令牌：权限 = 客户端被授 scopes ∩ 用户自身权限。"""

    __tablename__ = "oauth_access_token"

    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookSubscription(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Webhook 订阅（P4-API-08）：event_types ∈ constants.WEBHOOK_EVENT_TYPES；
    secret 用于 HMAC 签名，创建后仅可重置不可回显。"""

    __tablename__ = "webhook_subscription"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[str] = mapped_column(String(255), nullable=False)
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )


class WebhookDeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"  # 待重试
    DEAD = "dead"  # 重试梯度走完，等待人工重放


class WebhookDelivery(UuidPkMixin, TimestampMixin, Base):
    """Webhook 投递记录：每次投递一行；重试复用同一行（attempt 累加）。"""

    __tablename__ = "webhook_delivery"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscription.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        Enum(WebhookDeliveryStatus, name="webhook_delivery_status", values_callable=enum_values),
        nullable=False,
        default=WebhookDeliveryStatus.PENDING,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
