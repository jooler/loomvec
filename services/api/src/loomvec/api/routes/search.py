"""P1/P2/P3 检索接口：`POST /search` → SemanticHit 列表。

P3 扩展（P3-API-02）：
- graph_evidence：命中的图谱证据链（实体→关系→实体路径）随结果下发；
- use_graph 参数：按请求开关图谱召回（L3/L4）；全局开关在 settings.search.graph_enabled，
  运维可经 SystemConfig(graph.enabled) 运行时关停（admin settings 注册表）；
- scope/过滤器/审核可见性语义不变（P2-API-05）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_milvus,
    get_retriever,
    get_session,
    require_scope,
    resolve_space_access,
)
from loomvec.api.identity import user_uuid
from loomvec.api.services.search import enrich_hits
from loomvec.core.authz import SpaceRole, visible_space_ids
from loomvec.core.config import SearchSettings
from loomvec.core.db.models import Space
from loomvec.core.db.repos import AssetRepo
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.retrieval import Retriever, SemanticHit

router = APIRouter(prefix="/api/v1", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    space_id: uuid.UUID | None = None  # 缺省 = 聚合模式（我的全部空间）
    top_k: int | None = Field(default=None, ge=1, le=50)
    asset_ids: list[uuid.UUID] | None = None
    unit_types: list[str] | None = Field(default=None, description="text / table / image")
    tag_ids: list[uuid.UUID] | None = None
    category_id: uuid.UUID | None = None
    rerank: bool | None = Field(default=None, description="默认取全局配置；可按请求开关（A/B 用）")
    image_search: bool | None = Field(default=None, description="以文搜图开关（默认取全局配置）")
    use_graph: bool | None = Field(default=None, description="图谱召回开关（默认取全局配置）")


class SearchHitOut(BaseModel):
    unit_id: uuid.UUID
    asset_id: uuid.UUID
    asset_name: str
    space_id: uuid.UUID
    space_name: str | None = None
    space_slug: str | None = None
    unit_type: str
    title: str | None
    text: str
    score: float
    scores: dict[str, float]
    locator: dict[str, Any]
    highlight: str | None
    parent: dict[str, Any] | None = None  # 父块上下文（若有）
    graph_evidence: list[dict[str, Any]] = Field(default_factory=list)  # 图谱证据链（P3-API-02）


class SearchResponse(BaseModel):
    items: list[SearchHitOut]
    total: int


async def _resolve_scope(
    session: AsyncSession, identity: Identity, space_id: uuid.UUID | None
) -> tuple[list[uuid.UUID], uuid.UUID | None, dict[uuid.UUID, SpaceRole]]:
    """检索范围收敛：单空间（成员校验）或聚合（我的空间集合）。"""
    if space_id is not None:
        access = await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
        roles = {space_id: access.role}
        return [space_id], access.space.tenant_id, roles
    if identity.user_id.startswith("apikey:"):
        raise ValidationError("API Key 聚合检索不开放；请指定 space_id")
    ids = await visible_space_ids(session, user_id=user_uuid(identity))
    if not ids:
        return [], None, {}
    from loomvec.core.db.repos import SpaceMemberRepo

    members = await SpaceMemberRepo(session).list_for_user(user_uuid(identity))
    roles = {m.space_id: m.role for m in members}
    tenant = (
        await session.execute(select(Space.tenant_id).where(Space.id == ids[0]))
    ).scalar_one_or_none()
    return ids, tenant, roles


def get_retriever_settings(request: Request) -> SearchSettings:
    return request.app.state.settings.search


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    retriever: Retriever = Depends(get_retriever),
    request: Request = None,
) -> SearchResponse:
    space_ids, tenant_id, roles = await _resolve_scope(session, identity, body.space_id)
    if not space_ids:
        return SearchResponse(items=[], total=0)

    # 标签/分类过滤：PG 侧先收敛 asset_ids；显式 asset_ids 与过滤条件取交集
    asset_ids = body.asset_ids
    if body.tag_ids or body.category_id:
        rows = await AssetRepo(session).list_in_spaces(
            space_ids,
            limit=get_retriever_settings(request).filter_scan_limit,
            include_unreviewed=True,
            tag_ids=body.tag_ids,
            category_id=body.category_id,
        )
        filtered = {a.id for a in rows}
        asset_ids = sorted(filtered & set(asset_ids)) if asset_ids else sorted(filtered)
        if not asset_ids:
            return SearchResponse(items=[], total=0)

    # 图谱总开关：SystemConfig 运行时覆盖 ∩ 请求参数（P3-API-02）
    from loomvec.api.services.search import graph_switch

    global_graph = await graph_switch(session, get_retriever_settings(request))
    use_graph = global_graph and (body.use_graph if body.use_graph is not None else True)

    hits: list[SemanticHit] = await retriever.search(
        space_ids=space_ids,
        tenant_id=tenant_id,
        query=body.query,
        top_k=body.top_k,
        asset_ids=asset_ids,
        unit_types=body.unit_types,
        rerank=body.rerank,
        image_search=body.image_search,
        use_graph=use_graph,
        session=session,
    )
    if not hits:
        return SearchResponse(items=[], total=0)

    items = await enrich_hits(session, hits, roles)
    return SearchResponse(items=[SearchHitOut(**item) for item in items], total=len(items))


@router.get("/search/health", include_in_schema=False)
async def search_ready(milvus=Depends(get_milvus)) -> dict:
    """检索链路可达性（Milvus 集合存在性）。"""
    try:
        exists = await milvus.async_collection_exists()
        return {"milvus": True, "collection": exists}
    except Exception as e:
        raise NotFoundError(resource="milvus", id=str(e)) from e
