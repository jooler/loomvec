"""P2-QA-01/02 DB 级集成测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

覆盖：迁移 0003 后的成员域查询、authz 闸门（角色 × 审核可见性）、
配额闸门与计量、审核决策服务（通知落库）、成员移除即失权。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


@pytest.fixture()
async def db(pg_url):
    """建表（metadata.create_all，与迁移等价的 schema）+ 种子租户/用户/空间。"""
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Space, SpaceMember, SpaceRole, Tenant, User

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    t
                    for t in Base.metadata.sorted_tables
                    if t.key
                    in {
                        "tenant",
                        "user",
                        "space",
                        "space_member",
                        "asset",
                        "asset_folder",
                        "asset_version",
                        "semantic_unit",
                        "space_usage",
                        "notification",
                        "tag",
                        "asset_tag",
                        "category",
                        "metadata_field",
                    }
                ],
            )
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    async with maker() as session:
        tenant = Tenant(name="t")
        session.add(tenant)
        await session.flush()
        ids["tenant"] = tenant.id
        for name in ("alice", "bob", "carol", "dave"):
            u = User(tenant_id=tenant.id, username=name, display_name=name)
            session.add(u)
            await session.flush()
            ids[name] = u.id
        space = Space(
            tenant_id=tenant.id,
            slug="demo",
            name="Demo",
            review_required=True,
            owner_id=ids["alice"],
        )
        session.add(space)
        await session.flush()
        ids["space"] = space.id
        for name, role in (("alice", "owner"), ("bob", "editor"), ("carol", "viewer")):
            session.add(
                SpaceMember(
                    space_id=space.id,
                    user_id=ids[name],
                    role=SpaceRole(role),
                    tenant_id=tenant.id,
                )
            )
        await session.commit()
    yield maker, ids
    await engine.dispose()


async def test_authz_role_matrix(db):
    """P2-QA-01：owner/editor/viewer × 最低角色闸门。"""
    import uuid

    from loomvec.core.authz import require_space_role
    from loomvec.core.errors import PermissionDeniedError

    maker, ids = db
    async with maker() as session:
        for name, min_role, ok in (
            ("alice", "viewer", True),
            ("alice", "owner", True),
            ("bob", "editor", True),
            ("bob", "owner", False),
            ("carol", "viewer", True),
            ("carol", "editor", False),
        ):
            if ok:
                access = await require_space_role(
                    session, space_id=ids["space"], user_id=ids[name], min_role=min_role
                )
                assert access.space.id == ids["space"]
            else:
                with pytest.raises(PermissionDeniedError):
                    await require_space_role(
                        session, space_id=ids["space"], user_id=ids[name], min_role=min_role
                    )
        # 非成员（dave）与未知空间
        with pytest.raises(PermissionDeniedError):
            await require_space_role(
                session, space_id=ids["space"], user_id=ids["dave"], min_role="viewer"
            )
        from loomvec.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await require_space_role(
                session, space_id=uuid.uuid4(), user_id=ids["alice"], min_role="viewer"
            )


async def test_quota_gate_and_metering(db):
    """P2-QA-02：两级配额闸门 + 登记计量 + 删除扣减。"""
    from loomvec.core.db.repos import SpaceRepo
    from loomvec.core.errors import PermissionDeniedError
    from loomvec.core.quota import (
        apply_asset_added,
        apply_asset_removed,
        check_upload_quota,
        get_or_create_usage,
    )

    maker, _ids = db
    async with maker() as session:
        space = await SpaceRepo(session).get_by_slug("demo")
        # 租户不限 + 空间文件数限 2
        await SpaceRepo(session).update(space, quota_file_count=2)
        await apply_asset_added(
            session, space_id=space.id, tenant_id=space.tenant_id, size_bytes=100
        )
        await apply_asset_added(
            session, space_id=space.id, tenant_id=space.tenant_id, size_bytes=50
        )
        usage = await get_or_create_usage(session, space)
        assert usage.file_count == 2 and usage.storage_bytes == 150
        with pytest.raises(PermissionDeniedError) as ei:
            await check_upload_quota(session, space=space, incoming_bytes=10)
        assert ei.value.details.get("quota_reason") == "space_file_count_exceeded"
        # 删除扣减后闸门放行
        await apply_asset_removed(session, space_id=space.id, size_bytes=50)
        await check_upload_quota(session, space=space, incoming_bytes=10)
        # 存量资产不受影响：闸门只作用于写入路径（usage 与检索无耦合）
        assert (await SpaceRepo(session).get_by_slug("demo")).quota_file_count == 2


async def test_review_flow_and_notifications(db):
    """P2-QA-02：待审可见性、通过/驳回通知、驳回理由必填。"""
    from loomvec.api.services import review as review_service
    from loomvec.core.db.models import Asset, ReviewStatus
    from loomvec.core.db.repos import NotificationRepo, SpaceRepo
    from loomvec.core.errors import ValidationError

    maker, ids = db
    async with maker() as session:
        space = await SpaceRepo(session).get_by_slug("demo")
        asset = Asset(
            tenant_id=space.tenant_id,
            space_id=space.id,
            name="a.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=10,
            status="ready",
            created_by=ids["bob"],
        )
        session.add(asset)
        await session.flush()

        # 模拟管线完成：review_required 空间 → pending_review
        asset.review_status = ReviewStatus.PENDING_REVIEW
        await session.commit()

        queue = await review_service.review_queue(session, space=space)
        assert [a.id for a in queue] == [asset.id]

        # 驳回必须带理由
        with pytest.raises(ValidationError):
            await review_service.decide_review(
                session,
                reviewer_id=ids["alice"],
                redis=None,
                space=space,
                asset_id=asset.id,
                action="reject",
                reason=None,
            )
        asset = await review_service.decide_review(
            session,
            reviewer_id=ids["alice"],
            redis=None,
            space=space,
            asset_id=asset.id,
            action="reject",
            reason="不合格",
        )
        assert asset.review_status == ReviewStatus.REJECTED
        notes = await NotificationRepo(session).list_for_user(ids["bob"])
        assert any(n.type == "review.rejected" and "不合格" in n.title for n in notes)

        asset = await review_service.decide_review(
            session,
            reviewer_id=ids["alice"],
            redis=None,
            space=space,
            asset_id=asset.id,
            action="approve",
            reason=None,
        )
        assert asset.review_status == ReviewStatus.APPROVED
        notes = await NotificationRepo(session).list_for_user(ids["bob"])
        assert any(n.type == "review.approved" for n in notes)


async def test_member_removal_revokes_access(db):
    """P2-QA-01：被移除即失权（闸门即时拒绝）。"""
    from loomvec.api.services import spaces as space_service
    from loomvec.core.authz import require_space_role
    from loomvec.core.db.models import SpaceRole
    from loomvec.core.db.repos import SpaceMemberRepo, SpaceRepo
    from loomvec.core.errors import PermissionDeniedError, ValidationError

    maker, ids = db
    async with maker() as session:
        space = await SpaceRepo(session).get_by_slug("demo")
        member = await SpaceMemberRepo(session).get(space.id, ids["carol"])
        await space_service.remove_member(session, space=space, member=member)
        with pytest.raises(PermissionDeniedError):
            await require_space_role(
                session, space_id=space.id, user_id=ids["carol"], min_role=SpaceRole.VIEWER
            )
        # owner 不可移除
        owner_member = await SpaceMemberRepo(session).get(space.id, ids["alice"])
        with pytest.raises(ValidationError):
            await space_service.remove_member(session, space=space, member=owner_member)
