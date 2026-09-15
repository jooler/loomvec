"""P5-OPS 运营域（/api/v1/ops/*）：公共知识库空间的维护与分组可见性。

路由域级闸门：
- 每个子路由自行声明 `Depends(require_ops("admin:read"|"admin:write"))`
  （operator / super_admin，auditor 不进入运营域）；
- 域级 `ops_audit` 依赖对写方法成功响应做兜底审计留痕（object_type=ops_request）；
- 运营者对具体空间的操作会自动补齐 owner 成员行（见 ops/spaces.py 模块注释），
  内容管理（上传/审核/图谱）复用用户域既有接口链路。

管理端（apps/admin，系统运维）与运营端（apps/ops，内容运营）职责分离：
管理端维持现状，不在本域内改动。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from loomvec.api.deps import ops_audit
from loomvec.api.routes.ops import groups as groups_routes
from loomvec.api.routes.ops import spaces as spaces_routes

ops_api_router = APIRouter(
    prefix="/api/v1/ops",
    tags=["ops"],
    dependencies=[Depends(ops_audit)],
)

for _r in (
    spaces_routes.router,
    groups_routes.router,
):
    ops_api_router.include_router(_r)
