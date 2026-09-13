"""P4-API 企业化表与平台角色种子：

- 列扩展：user.oidc_sub（OIDC 身份唯一标识）、space.banned_at（平台封禁）；
- 新表：tenant_oidc_binding / system_config / oauth_client /
  oauth_authorization_code / oauth_access_token / webhook_subscription /
  webhook_delivery / reembed_task；
- seed：平台角色 super_admin / operator / auditor（is_platform_role 全局行）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 平台角色固定 ID（全局行，重建库后 ID 稳定，便于引用与排障）
PLATFORM_ROLE_IDS = {
    "super_admin": "0198bec0-0000-7000-8000-000000000201",
    "operator": "0198bec0-0000-7000-8000-000000000202",
    "auditor": "0198bec0-0000-7000-8000-000000000203",
}


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, create_type=False)


def upgrade() -> None:
    # ---- 列扩展 ----
    op.add_column("user", sa.Column("oidc_sub", sa.String(255), nullable=True))
    op.create_index("ix_user_oidc_sub", "user", ["oidc_sub"], unique=True)
    op.add_column("space", sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True))

    # ---- tenant_oidc_binding ----
    op.create_table(
        "tenant_oidc_binding",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE", name="fk_oidc_binding_tenant_id_tenant"),
            nullable=False,
            index=True,
        ),
        sa.Column("domain", sa.String(255), nullable=False, unique=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("client_secret", sa.String(512), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- system_config ----
    op.create_table(
        "system_config",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_by", UUID(as_uuid=True), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- oauth_client ----
    op.create_table(
        "oauth_client",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_secret_hash", sa.String(255), nullable=False),
        sa.Column("redirect_uris", JSONB(), nullable=False, server_default="[]"),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum("oauth_client_status", "active", "suspended"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("homepage_url", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- oauth_authorization_code ----
    op.create_table(
        "oauth_authorization_code",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("code_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("client_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_oauth_code_user_id_user"),
            nullable=False,
            index=True,
        ),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- oauth_access_token ----
    op.create_table(
        "oauth_access_token",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("client_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_oauth_token_user_id_user"),
            nullable=False,
            index=True,
        ),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- webhook_subscription ----
    op.create_table(
        "webhook_subscription",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("secret", sa.String(255), nullable=False),
        sa.Column("event_types", JSONB(), nullable=False, server_default="[]"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- webhook_delivery ----
    op.create_table(
        "webhook_delivery",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "webhook_subscription.id",
                ondelete="CASCADE",
                name="fk_webhook_delivery_subscription_id_webhook_subscription",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "status",
            _enum("webhook_delivery_status", "pending", "success", "failed", "dead"),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- reembed_task ----
    op.create_table(
        "reembed_task",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("space_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("target_model", sa.String(128), nullable=False),
        sa.Column(
            "status",
            _enum("reembed_task_status", "pending", "running", "succeeded", "failed"),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("total_assets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done_assets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_assets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- seed：平台角色（全局行）----
    bind = op.get_bind()
    for code, role_id in PLATFORM_ROLE_IDS.items():
        bind.execute(
            sa.text(
                "INSERT INTO role (id, code, name, description, is_platform_role) "
                "VALUES (:id, :code, :name, :description, true) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(
                id=uuid.UUID(role_id),
                code=code,
                name={"super_admin": "超级管理员", "operator": "运营人员", "auditor": "审计员"}[
                    code
                ],
                description={
                    "super_admin": "全部权限，含系统配置与平台角色分配",
                    "operator": "日常运营（租户/空间/审核/管线），无系统配置",
                    "auditor": "全局只读 + 审计导出",
                }[code],
            )
        )


def downgrade() -> None:
    op.drop_table("reembed_task")
    op.drop_table("webhook_delivery")
    op.drop_table("webhook_subscription")
    op.drop_table("oauth_access_token")
    op.drop_table("oauth_authorization_code")
    op.drop_table("oauth_client")
    op.drop_table("system_config")
    op.drop_table("tenant_oidc_binding")
    op.drop_index("ix_user_oidc_sub", table_name="user")
    op.drop_column("user", "oidc_sub")
    op.drop_column("space", "banned_at")
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM role WHERE code IN ('super_admin', 'operator', 'auditor')"))
    sa.Enum(name="reembed_task_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="webhook_delivery_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="oauth_client_status").drop(op.get_bind(), checkfirst=True)
