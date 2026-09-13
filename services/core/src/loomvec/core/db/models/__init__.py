"""ORM 模型包（按域拆分，本文件聚合导出保持兼容）。

历史导入路径 `loomvec.core.db.models.X` 不变；表结构演进在对应域文件内修改，
迁移脚本见 services/api/alembic/versions（只增不删，Alembic 串行演进）。
"""

from __future__ import annotations

from loomvec.core.db.models.asset import (
    Asset,
    AssetRendition,
    AssetStatus,
    AssetTag,
    AssetVersion,
    Category,
    ChunkMethod,
    GraphStatus,
    MetadataField,
    MetadataFieldType,
    RenditionKind,
    ReviewStatus,
    SemanticUnit,
    Tag,
    UnitType,
)
from loomvec.core.db.models.chat import (
    ChatMessage,
    ChatRole,
    ChatSession,
)
from loomvec.core.db.models.enterprise import (
    OauthAccessToken,
    OauthAuthorizationCode,
    OauthClient,
    OauthClientStatus,
    SystemConfig,
    TenantOidcBinding,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from loomvec.core.db.models.graph import (
    Community,
    Entity,
    EntityMergeLog,
    MergeLogStatus,
)
from loomvec.core.db.models.identity import (
    ApiKey,
    AuditLog,
    AuthSource,
    Role,
    Tenant,
    TenantStatus,
    User,
    UserRole,
    UserStatus,
)
from loomvec.core.db.models.pipeline import (
    JobStatus,
    JobType,
    ProcessingJob,
    ReembedTask,
    ReembedTaskStatus,
)
from loomvec.core.db.models.space import (
    Notification,
    Space,
    SpaceMember,
    SpaceRole,
    SpaceType,
    SpaceUsage,
)

__all__ = [
    "ApiKey",
    "Asset",
    "AssetRendition",
    "AssetStatus",
    "AssetTag",
    "AssetVersion",
    "AuditLog",
    "AuthSource",
    "Category",
    "ChatMessage",
    "ChatRole",
    "ChatSession",
    "ChunkMethod",
    "Community",
    "Entity",
    "EntityMergeLog",
    "GraphStatus",
    "JobStatus",
    "JobType",
    "MergeLogStatus",
    "MetadataField",
    "MetadataFieldType",
    "Notification",
    "OauthAccessToken",
    "OauthAuthorizationCode",
    "OauthClient",
    "OauthClientStatus",
    "ProcessingJob",
    "ReembedTask",
    "ReembedTaskStatus",
    "RenditionKind",
    "ReviewStatus",
    "Role",
    "SemanticUnit",
    "Space",
    "SpaceMember",
    "SpaceRole",
    "SpaceType",
    "SpaceUsage",
    "SystemConfig",
    "Tag",
    "Tenant",
    "TenantOidcBinding",
    "TenantStatus",
    "UnitType",
    "User",
    "UserRole",
    "UserStatus",
    "WebhookDelivery",
    "WebhookDeliveryStatus",
    "WebhookSubscription",
]
