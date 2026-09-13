"""P4-API 管理 API 域（/api/v1/admin/*）。

路由域级闸门（P4-API-01）：
- 每个子路由自行声明 `Depends(require_admin("admin:read"|"admin:write"))`；
- 域级 `admin_audit` 依赖对写方法成功响应做兜底审计留痕；
- 危险操作端点（停用租户/封禁空间/清空/下架/角色分配）显式记录理由与前后值。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from loomvec.api.deps import admin_audit
from loomvec.api.routes.admin import (
    audit as audit_routes,
)
from loomvec.api.routes.admin import (
    graph as graph_routes,
)
from loomvec.api.routes.admin import (
    oauth as oauth_routes,
)
from loomvec.api.routes.admin import (
    pipeline as pipeline_routes,
)
from loomvec.api.routes.admin import (
    reembedding as reembedding_routes,
)
from loomvec.api.routes.admin import (
    reviews as reviews_routes,
)
from loomvec.api.routes.admin import (
    settings as settings_routes,
)
from loomvec.api.routes.admin import (
    spaces as spaces_routes,
)
from loomvec.api.routes.admin import (
    system as system_routes,
)
from loomvec.api.routes.admin import (
    tenants as tenants_routes,
)
from loomvec.api.routes.admin import (
    users as users_routes,
)
from loomvec.api.routes.admin import (
    webhooks as webhooks_routes,
)

admin_api_router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(admin_audit)],
)

for _r in (
    system_routes.router,
    tenants_routes.router,
    users_routes.router,
    spaces_routes.router,
    reviews_routes.router,
    pipeline_routes.router,
    reembedding_routes.router,
    graph_routes.router,
    settings_routes.router,
    oauth_routes.router,
    webhooks_routes.router,
    audit_routes.router,
):
    admin_api_router.include_router(_r)
