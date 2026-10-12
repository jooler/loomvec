"""P5 集成测试：对话 @ 文献的服务端校验（agent routes._resolve_mentions）。

覆盖：成员可 @ 范围内文献（去重保序）；范围外 / 已删除 / 审核空间待审（viewer）一律整体拒绝；
editor 可 @ 待审文献。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


async def test_resolve_mentions_validates_scope_and_visibility(pg_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.agent.routes import _resolve_mentions
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import (
        Asset,
        ReviewStatus,
        Space,
        SpaceMember,
        SpaceRole,
        Tenant,
        User,
    )
    from loomvec.core.errors import ValidationError

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        tenant = Tenant(name="mention-tenant")
        session.add(tenant)
        await session.flush()
        viewer = User(username="viewer", tenant_id=tenant.id)
        editor = User(username="editor", tenant_id=tenant.id)
        session.add_all([viewer, editor])
        await session.flush()

        def new_space(slug: str, review_required: bool) -> Space:
            return Space(
                tenant_id=tenant.id,
                slug=slug,
                name=slug,
                review_required=review_required,
                owner_id=editor.id,
            )

        open_space, review_space, other_space = (
            new_space("open", False),
            new_space("review", True),
            new_space("other", False),
        )
        session.add_all([open_space, review_space, other_space])
        await session.flush()
        for space in (open_space, review_space, other_space):
            session.add(
                SpaceMember(
                    space_id=space.id, user_id=editor.id, role=SpaceRole.EDITOR, tenant_id=tenant.id
                )
            )
        for space in (open_space, review_space):
            session.add(
                SpaceMember(
                    space_id=space.id, user_id=viewer.id, role=SpaceRole.VIEWER, tenant_id=tenant.id
                )
            )

        def new_asset(space: Space, name: str, **extra) -> Asset:
            return Asset(
                tenant_id=tenant.id,
                space_id=space.id,
                name=name,
                mime_type="application/pdf",
                ext=".pdf",
                size_bytes=1,
                status="ready",
                created_by=editor.id,
                **extra,
            )

        ok_a, ok_b, deleted, pending, approved, elsewhere = (
            new_asset(open_space, "a.pdf"),
            new_asset(open_space, "b.pdf"),
            new_asset(open_space, "gone.pdf", deleted_at=datetime.now(UTC)),
            new_asset(review_space, "pending.pdf", review_status=ReviewStatus.PENDING_REVIEW),
            new_asset(review_space, "approved.pdf", review_status=ReviewStatus.APPROVED),
            new_asset(other_space, "elsewhere.pdf"),
        )
        session.add_all([ok_a, ok_b, deleted, pending, approved, elsewhere])
        await session.flush()
        ids = {
            "viewer": viewer.id,
            "editor": editor.id,
            # 与 agent_session.scope_space_ids（JSON 列）读出来的形态一致：字符串
            "scope": [str(open_space.id), str(review_space.id)],
            "a": ok_a.id,
            "b": ok_b.id,
            "deleted": deleted.id,
            "pending": pending.id,
            "approved": approved.id,
            "elsewhere": elsewhere.id,
        }

    async def resolve(user: str, *assets: str) -> list[dict[str, str]]:
        async with factory() as session, session.begin():
            return await _resolve_mentions(
                session, ids[user], ids["scope"], [ids[a] for a in assets]
            )

    # 去重保序，名称随资产回填
    got = await resolve("viewer", "b", "a", "b", "approved")
    assert [m["name"] for m in got] == ["b.pdf", "a.pdf", "approved.pdf"]
    assert got[0]["asset_id"] == str(ids["b"])
    assert await resolve("viewer") == []

    # 任一不满足整体拒绝：已删除 / 不在会话范围 / 审核空间待审（viewer 不可见）
    for bad in ("deleted", "elsewhere", "pending"):
        with pytest.raises(ValidationError):
            await resolve("viewer", "a", bad)

    # editor 看得到待审文献
    assert [m["name"] for m in await resolve("editor", "pending")] == ["pending.pdf"]
    await engine.dispose()


def test_mention_ids_are_uuid_typed():
    """请求体里的 asset id 必须是合法 uuid（非法值在 422 阶段拦下，不进检索表达式）。"""
    from pydantic import ValidationError as PydanticValidationError

    from loomvec.api.routes.agent import AgentQuestion

    AgentQuestion(question="q", mention_asset_ids=[uuid.uuid4()])
    with pytest.raises(PydanticValidationError):
        AgentQuestion(question="q", mention_asset_ids=['x" or 1==1'])
    with pytest.raises(PydanticValidationError):
        AgentQuestion(question="q", mention_asset_ids=[uuid.uuid4() for _ in range(11)])
