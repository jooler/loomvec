"""身份与平台基础域：租户 / 用户 / 角色 / 审计 / API Key（迁移 0001）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status", values_callable=enum_values),
        nullable=False,
        default=TenantStatus.ACTIVE,
    )
    # 配额（P2 落地校验；P0 先建列）
    quota_storage_bytes: Mapped[int] = mapped_column(nullable=False, default=0)
    quota_file_count: Mapped[int] = mapped_column(nullable=False, default=0)


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AuthSource(enum.StrEnum):
    LOCAL = "local"
    OIDC = "oidc"


class User(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "user"

    # 租户归属：本地注册/OIDC 域归属在 P2/P4 落地，P0 允许为空（dev token 用户）
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", values_callable=enum_values),
        nullable=False,
        default=UserStatus.ACTIVE,
    )
    auth_source: Mapped[AuthSource] = mapped_column(
        Enum(AuthSource, name="auth_source", values_callable=enum_values),
        nullable=False,
        default=AuthSource.LOCAL,
    )
    # P4-API-06 OIDC 身份标识（issuer+sub 唯一定位；本地用户为空）
    oidc_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """平台角色（super_admin/operator/auditor）为全局行；租户内模板
    （owner/editor/viewer）由产品预置、可挂 tenant_id。"""

    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_platform_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserRole(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role_pair"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )


class UserGroup(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """用户分组（P5 运营端维护）：公共空间可见性的授权单元，与空间角色解耦。

    唯一性只约束活行（部分唯一索引）：软删行不占名，同名分组可重建。
    """

    __tablename__ = "user_group"
    __table_args__ = (
        Index(
            "uq_user_group_tenant_name_live",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserGroupMember(UuidPkMixin, TimestampMixin, Base):
    """分组-用户关联：一个用户可属多个分组（运营端按用户名添加）。"""

    __tablename__ = "user_group_member"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_user_group_member_pair"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AuditLog(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """审计日志：追加写，不提供软删除/更新接口。"""

    __tablename__ = "audit_log"

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApiKey(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """API Key（P4 实现签发/校验）。

    user_id 为空 = 租户级 Key（默认，应用级集成，空间权限上限 editor）；
    非空 = 绑定用户的 PAT：认证后走用户语义（identity.identity_from_api_key
    按该用户派生身份），用户空间列表/公共空间/聚合检索自然可用。
    """

    __tablename__ = "api_key"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
