"""管线自动新建知识库（pending）与人工确认/合并的 DB 级集成测试（testcontainers PG）。

覆盖：init_db 给存量库补 tag.status / tag.merged_into / asset.checksum 索引；
create_system_tag 同名复用、近似名不误并、合并重定向、删除即否决、pending 上限、
唯一约束兜底、tenant 为空时的并发串行化；confirm / merge 的状态与权限约束、
关联与子节点改挂、不能并入租户级标签；自动归类写关联与 merge 的行锁互斥；列表 status 过滤。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


async def _precreate_age_graph(pg_url: str) -> None:
    """同 deploy/compose/postgres-age/init/01-age-init.sql 预建 AGE 图（独立连接，用完即弃），
    使 init_db 走与 compose 环境一致的路径；本测试有并发会话，需多条连接都能看到表。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from loomvec.core.constants import GRAPH_NAME

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS age")
        await conn.exec_driver_sql("LOAD 'age'")
        await conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')
        await conn.exec_driver_sql(f"SELECT create_graph('{GRAPH_NAME}')")
    await engine.dispose()


@pytest.fixture()
async def db(pg_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.api.init_db import DEFAULT_SPACE_ID, SEED_USERS, init_database

    await _precreate_age_graph(pg_url)
    engine = create_async_engine(pg_url)
    await init_database(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    ids = {
        "space": uuid.UUID(DEFAULT_SPACE_ID),
        "alice": uuid.UUID(SEED_USERS["alice"]),  # 空间 owner
        "bob": uuid.UUID(SEED_USERS["bob"]),  # 空间 editor
    }
    yield engine, maker, ids
    await engine.dispose()


def _identity(user_id: uuid.UUID, space):
    from loomvec.api.context import Identity

    return Identity(
        user_id=str(user_id),
        username="u",
        tenant_id=str(space.tenant_id) if space.tenant_id else None,
        scopes=("read", "write"),
        roles=(),
    )


async def _space(session, ids):
    from loomvec.core.db.models import Space

    return await session.get(Space, ids["space"])


async def _tag(session, space, name, *, status="active", parent_id=None, space_id="same"):
    from loomvec.core.db.models import Tag

    tag = Tag(
        tenant_id=space.tenant_id,
        space_id=space.id if space_id == "same" else space_id,
        name=name,
        status=status,
        parent_id=parent_id,
    )
    session.add(tag)
    await session.flush()
    return tag


async def _asset(session, space, name):
    from loomvec.core.db.models import Asset

    asset = Asset(
        tenant_id=space.tenant_id,
        space_id=space.id,
        name=name,
        mime_type="application/pdf",
        ext=".pdf",
        size_bytes=1,
    )
    session.add(asset)
    await session.flush()
    return asset


async def _live_names(maker, space_id) -> list[str]:
    from sqlalchemy import select

    from loomvec.core.db.models import Tag

    async with maker() as session:
        return sorted(
            (
                await session.execute(
                    select(Tag.name).where(Tag.space_id == space_id, Tag.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )


async def test_init_db_additive_ddl_on_existing_db(db):
    import sqlalchemy as sa

    from loomvec.api.init_db import _ensure_additive_columns

    engine, _maker, ids = db
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE tag DROP COLUMN status")
        await conn.exec_driver_sql("ALTER TABLE tag DROP COLUMN merged_into")
        await conn.exec_driver_sql("DROP INDEX ix_asset_checksum")
        await conn.execute(
            sa.text("INSERT INTO tag (name, space_id) VALUES ('存量知识库', :sid)"),
            {"sid": ids["space"]},
        )
    await _ensure_additive_columns(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT status, merged_into FROM tag WHERE name = '存量知识库'")
            )
        ).one()
        has_index = await conn.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ix_asset_checksum')"
            )
        )
    assert tuple(row) == ("active", None)
    assert has_index


async def test_create_system_tag_dedup_rejection_and_cap(db, monkeypatch):
    from datetime import UTC, datetime

    from loomvec.core import knowledge_base_auto as kba
    from loomvec.core.db.models import TagStatus

    _engine, maker, ids = db
    async with maker() as session:
        space = await _space(session, ids)
        deep = await _tag(session, space, "Deep Learning")
        diabetes1 = await _tag(session, space, "1型糖尿病")
        removed = await _tag(session, space, "废弃主题")
        removed.deleted_at = datetime.now(UTC)
        await session.commit()

        # 同名不同写法 → 复用
        reused = await kba.create_system_tag(session, space=space, name="deep-learning")
        assert reused.id == deep.id
        # 近似但不同的主题 → 新建，不误并
        diabetes2 = await kba.create_system_tag(session, space=space, name="2型糖尿病")
        assert diabetes2.id != diabetes1.id
        assert diabetes2.status == TagStatus.PENDING

        # 新主题 → pending、无创建人、上级透传；再以不同写法建议 → 复用同一节点
        created = await kba.create_system_tag(
            session, space=space, name=" 单细胞  测序 ", parent_id=deep.id, reason="r"
        )
        assert (created.name, created.parent_id, created.created_by) == (
            "单细胞 测序",
            deep.id,
            None,
        )
        assert (await kba.create_system_tag(session, space=space, name="单细胞测序")).id == (
            created.id
        )

        # 被删除（未合并）的同名节点：规范化后同名也视为否决
        assert await kba.create_system_tag(session, space=space, name="废弃 主题") is None

        # pending 上限：已有 2 个 pending，上限 2 → 不再新建（但同名复用不受限）
        assert await kba.count_pending_tags(session, space.id) == 2
        capped = await kba.create_system_tag(session, space=space, name="新主题", max_pending=2)
        assert capped is None
        reused_cap = await kba.create_system_tag(
            session, space=space, name="单细胞测序", max_pending=2
        )
        assert reused_cap.id == created.id

        # 唯一约束兜底：查重被绕过时插入撞约束，回滚保存点并复用已有行，会话仍可用
        monkeypatch.setattr(kba, "find_same_name_tag", lambda *_a: None)
        assert (await kba.create_system_tag(session, space=space, name="Deep Learning")).id == (
            deep.id
        )
        await session.commit()

    assert await _live_names(maker, ids["space"]) == [
        "1型糖尿病",
        "2型糖尿病",
        "Deep Learning",
        "单细胞 测序",
    ]


async def test_concurrent_create_serialized_for_null_tenant(db):
    from loomvec.core import knowledge_base_auto as kba
    from loomvec.core.db.models import Space

    _engine, maker, _ids = db
    async with maker() as session:
        space = Space(tenant_id=None, slug=f"null-tenant-{uuid.uuid4().hex[:8]}", name="无租户")
        session.add(space)
        await session.commit()

    started = asyncio.Event()

    async def first():
        async with maker() as session:
            tag = await kba.create_system_tag(session, space=space, name="并发主题")
            started.set()
            await asyncio.sleep(0.5)  # 持有 advisory lock 期间第二个任务须等待
            await session.commit()
            return tag.id

    async def second():
        await started.wait()
        async with maker() as session:
            tag = await kba.create_system_tag(session, space=space, name="并发-主题")
            await session.commit()
            return tag.id

    a, b = await asyncio.gather(first(), second())
    assert a == b  # tenant 为空时唯一约束不生效，靠空间级 advisory lock 串行化
    assert await _live_names(maker, space.id) == ["并发主题"]


async def test_confirm_merge_redirect_and_status_filter(db):
    from sqlalchemy import select

    from loomvec.api.routes.knowledge_bases import _nodes_for_access
    from loomvec.api.services import knowledge_bases as kb
    from loomvec.core import knowledge_base_auto as kba
    from loomvec.core.authz import require_space_role
    from loomvec.core.db.models import AssetTag, Tag, TagStatus
    from loomvec.core.errors import ConflictError, PermissionDeniedError, ValidationError

    _engine, maker, ids = db
    async with maker() as session:
        space = await _space(session, ids)
        target = await _tag(session, space, "肿瘤")
        legacy = await _tag(session, space, "租户级旧标签", space_id=None)
        pending = await _tag(session, space, "肿瘤学", status=TagStatus.PENDING.value)
        child = await _tag(session, space, "子节点", parent_id=pending.id)
        a1 = await _asset(session, space, "a1.pdf")
        a2 = await _asset(session, space, "a2.pdf")
        for asset, tag in ((a1, pending), (a2, pending), (a2, target)):
            session.add(
                AssetTag(tenant_id=space.tenant_id, asset_id=asset.id, tag_id=tag.id, source="auto")
            )
        await session.commit()

        access = await require_space_role(session, space_id=space.id, user_id=ids["alice"])
        listed = await _nodes_for_access(session, access, TagStatus.PENDING)
        assert [n.name for n in listed] == ["肿瘤学"]
        assert {n.status for n in await _nodes_for_access(session, access)} == {
            TagStatus.ACTIVE,
            TagStatus.PENDING,
        }

        alice = _identity(ids["alice"], space)
        bob = _identity(ids["bob"], space)
        with pytest.raises(PermissionDeniedError):  # editor 非知识库 owner
            await kb.merge(session, identity=bob, space=space, tag=pending, target=target)
        with pytest.raises(ConflictError):  # 只能合并 pending
            await kb.merge(session, identity=alice, space=space, tag=target, target=pending)
        with pytest.raises(ValidationError):  # 不能并入自己的子节点
            await kb.merge(session, identity=alice, space=space, tag=pending, target=child)
        with pytest.raises(ValidationError):  # 不能并入租户级旧标签
            await kb.merge(session, identity=alice, space=space, tag=pending, target=legacy)

        merged = await kb.merge(session, identity=alice, space=space, tag=pending, target=target)
        assert merged.id == target.id

    async with maker() as session:
        links = (await session.execute(select(AssetTag.asset_id, AssetTag.tag_id))).all()
        assert sorted((str(a), t) for a, t in links) == sorted(
            [(str(a1.id), target.id), (str(a2.id), target.id)]
        )
        source = await session.get(Tag, pending.id)
        assert source.deleted_at is not None and source.merged_into == target.id
        assert (await session.get(Tag, child.id)).parent_id == target.id

        # 合并后：同名建议与归类写入都重定向到合并目标
        space = await _space(session, ids)
        assert (await kba.create_system_tag(session, space=space, name="肿瘤学")).id == target.id
        assert (await kba.resolve_live_tag(session, pending.id)).id == target.id
        await session.commit()

        fresh = await _tag(session, space, "新主题", status=TagStatus.PENDING.value)
        await session.commit()
        with pytest.raises(PermissionDeniedError):
            await kb.confirm(session, identity=_identity(ids["bob"], space), space=space, tag=fresh)
        confirmed = await kb.confirm(
            session, identity=_identity(ids["alice"], space), space=space, tag=fresh
        )
        assert confirmed.status == TagStatus.ACTIVE


async def test_auto_assign_link_and_merge_are_serialized(db):
    """自动归类写关联前对节点加 FOR KEY SHARE；并发的 merge（FOR UPDATE）须等其提交后才读
    关联，关联一并迁移到目标，不会挂在已删除的节点上。"""
    from sqlalchemy import select

    from loomvec.api.services import knowledge_bases as kb
    from loomvec.core import knowledge_base_auto as kba
    from loomvec.core.db.models import AssetTag, TagStatus

    _engine, maker, ids = db
    async with maker() as session:
        space = await _space(session, ids)
        target = await _tag(session, space, "目标")
        pending = await _tag(session, space, "待合并", status=TagStatus.PENDING.value)
        asset = await _asset(session, space, "race.pdf")
        await session.commit()

    locked = asyncio.Event()

    async def auto_assign():
        async with maker() as session:
            live = (await kba.resolve_live_tags(session, [pending.id], lock=True))[pending.id]
            locked.set()
            await asyncio.sleep(0.5)
            session.add(
                AssetTag(
                    tenant_id=space.tenant_id, asset_id=asset.id, tag_id=live.id, source="auto"
                )
            )
            await session.commit()

    async def merge():
        await locked.wait()
        async with maker() as session:
            s = await _space(session, ids)
            await kb.merge(
                session,
                identity=_identity(ids["alice"], s),
                space=s,
                tag=await kb.get_tag(session, pending.id),
                target=await kb.get_tag(session, target.id),
            )

    await asyncio.gather(auto_assign(), merge())
    async with maker() as session:
        tag_ids = (
            (await session.execute(select(AssetTag.tag_id).where(AssetTag.asset_id == asset.id)))
            .scalars()
            .all()
        )
    assert tag_ids == [target.id]


def _is_deadlock(exc: Exception | None) -> bool:
    return exc is not None and "deadlock" in str(exc).lower()


async def test_auto_assign_two_candidates_vs_merge_no_deadlock(db):
    """AB-BA 死锁回归（三轮审查新发现并修复）：自动归类一次给两个候选节点加锁时，若逐个
    单独加锁、顺序又与 merge（按 id 升序一次性 FOR UPDATE）不一致，会出现"自动归类等
    merge、merge 等自动归类"的死锁；resolve_live_tags 按 id 升序把两个候选合成一条语句
    一次性加锁，与 merge 用同一全局顺序，不会死锁。用第三方事务先占住较大 id 的节点，
    把 merge 卡在"已锁较小 id、正在等较大 id"的确定状态，排除时序偶然性（否则大概率
    两边各走各的、根本碰不上，不能验证加锁顺序）。"""
    from sqlalchemy import select

    from loomvec.api.services import knowledge_bases as kb
    from loomvec.core import knowledge_base_auto as kba
    from loomvec.core.db.models import Tag, TagStatus

    _engine, maker, ids = db
    async with maker() as session:
        space = await _space(session, ids)
        tag_a = await _tag(session, space, "源", status=TagStatus.PENDING.value)
        tag_b = await _tag(session, space, "目标")
        await session.commit()
    lo_id, hi_id = sorted((tag_a.id, tag_b.id))

    ready = asyncio.Event()
    errs: dict[str, object] = {}

    async def blocker():
        async with maker() as session:
            await session.execute(select(Tag).where(Tag.id == hi_id).with_for_update(read=True))
            ready.set()
            await asyncio.sleep(0.8)
            await session.commit()

    async def merge():
        await ready.wait()
        try:
            async with maker() as session:
                s = await _space(session, ids)
                await kb.merge(
                    session,
                    identity=_identity(ids["alice"], s),
                    space=s,
                    tag=await kb.get_tag(session, tag_a.id),
                    target=await kb.get_tag(session, tag_b.id),
                )
        except Exception as e:
            errs["merge"] = e

    async def auto_assign():
        await ready.wait()
        await asyncio.sleep(0.3)  # 等 merge 已锁住较小 id、正卡在等较大 id 上
        try:
            async with maker() as session:
                live = await kba.resolve_live_tags(session, [hi_id, lo_id], lock=True)
                errs["live"] = {k: (v.id if v else None) for k, v in live.items()}
                await session.commit()
        except Exception as e:
            errs["auto"] = e

    await asyncio.gather(blocker(), merge(), auto_assign())
    assert not _is_deadlock(errs.get("merge")), errs.get("merge")
    assert not _is_deadlock(errs.get("auto")), errs.get("auto")
    assert "merge" not in errs, errs.get("merge")
    assert "auto" not in errs, errs.get("auto")
    # 源节点被合并，两个候选最终都解析到目标节点
    assert errs["live"] == {hi_id: tag_b.id, lo_id: tag_b.id}
