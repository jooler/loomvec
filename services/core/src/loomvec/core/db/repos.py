"""P1/P2 领域仓储：在 P0 Repository 基类上落资产域查询。

约定（P0-CORE-03）：服务/路由层持有 Repository，不直接拼查询；软删过滤在
基类统一处理。P2 资源级授权语义（成员可见性、审核状态过滤）经
`authz.visible_space_ids` / `decide_asset_visibility` 的结果由服务层传入
本层查询参数（space_ids / include_unreviewed），路由层无需感知。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import Settings
from loomvec.core.db.models import (
    AgentEnvironment,
    AgentEnvironmentStatus,
    AgentSession,
    Asset,
    AssetFolder,
    AssetRendition,
    AssetTag,
    AssetVersion,
    Category,
    MetadataField,
    Notification,
    ProcessingJob,
    ReviewStatus,
    SemanticUnit,
    Space,
    SpaceMember,
    SpaceUsage,
    Tag,
    User,
)
from loomvec.core.db.repository import Repository


class UserRepo(Repository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        stmt = self._base_select(username=username)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class SpaceRepo(Repository[Space]):
    model = Space

    async def get_by_slug(self, slug: str) -> Space | None:
        stmt = self._base_select(slug=slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(Space.id).where(Space.slug == slug, Space.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


class SpaceMemberRepo(Repository[SpaceMember]):
    model = SpaceMember

    async def get(self, space_id: uuid.UUID, user_id: uuid.UUID) -> SpaceMember | None:
        stmt = self._base_select(space_id=space_id, user_id=user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_space(self, space_id: uuid.UUID) -> list[SpaceMember]:
        stmt = self._base_select(space_id=space_id).order_by(SpaceMember.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def space_ids_for_user(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(SpaceMember.space_id).where(SpaceMember.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(self, user_id: uuid.UUID) -> list[SpaceMember]:
        stmt = self._base_select(user_id=user_id)
        return list((await self.session.execute(stmt)).scalars().all())


class AssetFolderRepo(Repository[AssetFolder]):
    """文件夹仓储：树形结构遍历与重名/环校验的取数入口（结构不变量在服务层）。"""

    model = AssetFolder

    async def get_live(self, folder_id: uuid.UUID) -> AssetFolder | None:
        return await self.get(folder_id)

    async def list_for_space(self, space_id: uuid.UUID) -> list[AssetFolder]:
        stmt = self._base_select(space_id=space_id).order_by(AssetFolder.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def children_of(
        self, folder_id: uuid.UUID | None, space_id: uuid.UUID
    ) -> list[AssetFolder]:
        stmt = self._base_select(space_id=space_id).where(AssetFolder.parent_id == folder_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_sibling_name(
        self, space_id: uuid.UUID, parent_id: uuid.UUID | None, name: str
    ) -> AssetFolder | None:
        """同父下同名活文件夹（重名校验）。"""
        stmt = self._base_select(space_id=space_id, name=name).where(
            AssetFolder.parent_id == parent_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def descendant_ids(self, folder_id: uuid.UUID, *, space_id: uuid.UUID) -> list[uuid.UUID]:
        """递归收集后代文件夹 id（含自身；软删行也计入，防悬挂引用）。"""
        all_rows = await self.session.execute(
            # 含软删：环检测/删除级联需全集；文件夹树不跨空间，按空间收敛
            select(AssetFolder.id, AssetFolder.parent_id).where(AssetFolder.space_id == space_id)
        )
        children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
        for fid, parent in all_rows:
            children.setdefault(parent, []).append(fid)
        result: list[uuid.UUID] = [folder_id]
        queue = [folder_id]
        while queue:
            for child in children.get(queue.pop(), []):
                result.append(child)
                queue.append(child)
        return result


class AssetRepo(Repository[Asset]):
    model = Asset

    async def get_live(self, asset_id: uuid.UUID) -> Asset | None:
        """未删除的资产（含软删过滤）。"""
        return await self.get(asset_id)

    @staticmethod
    def _review_filter(include_unreviewed: bool, strict_review: bool = False):
        """审核可见性过滤：
        - editor/owner（include_unreviewed=True）：不过滤；
        - viewer + 非审核空间：不过滤（review_status 为 NULL）；
        - viewer + 审核空间（strict_review）：仅见 approved（处理中/待审/驳回全隐藏）。"""
        if include_unreviewed:
            return None
        if strict_review:
            return Asset.review_status == ReviewStatus.APPROVED
        return or_(Asset.review_status.is_(None), Asset.review_status == ReviewStatus.APPROVED)

    async def list_in_spaces(
        self,
        space_ids: list[uuid.UUID],
        *,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        status: str | None = None,
        ext: str | None = None,
        include_unreviewed: bool = False,
        strict_review: bool = False,
        tag_ids: list[uuid.UUID] | None = None,
        category_id: uuid.UUID | None = None,
        review_status: str | None = None,
        folder_id: str | None = None,
    ) -> list[Asset]:
        """跨空间游标列表：P2 资产列表/聚合检索的统一取数入口。

        folder_id（Finder 目录浏览）：'root' 仅根目录（folder_id IS NULL）、
        uuid 仅该文件夹内；缺省不过滤（全空间聚合视图）。
        """
        if not space_ids:
            return []
        stmt = (
            self._base_select()
            .where(Asset.space_id.in_(space_ids))
            .order_by(Asset.created_at.desc(), Asset.id.desc())
        )
        if folder_id == "root":
            stmt = stmt.where(Asset.folder_id.is_(None))
        elif folder_id:
            stmt = stmt.where(Asset.folder_id == uuid.UUID(folder_id))
        review = self._review_filter(include_unreviewed, strict_review)
        if review is not None:
            stmt = stmt.where(review)
        if status:
            stmt = stmt.where(Asset.status == status)
        if ext:
            stmt = stmt.where(Asset.ext == ext)
        if category_id:
            stmt = stmt.where(Asset.category_id == category_id)
        if review_status:
            stmt = stmt.where(Asset.review_status == review_status)
        if tag_ids:
            stmt = stmt.where(
                Asset.id.in_(select(AssetTag.asset_id).where(AssetTag.tag_id.in_(tag_ids)))
            )
        if cursor_created_at is not None and cursor_id is not None:
            from loomvec.core.db.pagination import Cursor, apply_cursor

            stmt = apply_cursor(stmt, Cursor(created_at=cursor_created_at, id=cursor_id))
        return list((await self.session.execute(stmt.limit(limit))).scalars().all())

    async def list_by_space(
        self,
        space_id: uuid.UUID,
        *,
        limit: int,
        cursor_created_at=None,
        cursor_id: uuid.UUID | None = None,
        status: str | None = None,
        ext: str | None = None,
        include_unreviewed: bool = False,
        strict_review: bool = False,
        tag_ids: list[uuid.UUID] | None = None,
        category_id: uuid.UUID | None = None,
        review_status: str | None = None,
        folder_id: str | None = None,
    ) -> list[Asset]:
        return await self.list_in_spaces(
            [space_id],
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            status=status,
            ext=ext,
            include_unreviewed=include_unreviewed,
            strict_review=strict_review,
            tag_ids=tag_ids,
            category_id=category_id,
            review_status=review_status,
            folder_id=folder_id,
        )

    async def find_by_checksum(self, space_id: uuid.UUID, checksum: str) -> list[Asset]:
        """去重提示：同空间同 checksum 的现存资产。"""
        stmt = self._base_select(space_id=space_id, checksum=checksum).order_by(
            Asset.created_at.desc()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_in_folders(self, folder_ids: list[uuid.UUID]) -> list[Asset]:
        """文件夹级联删除/复制：这些文件夹（含后代）下的活资产。"""
        if not folder_ids:
            return []
        stmt = self._base_select().where(Asset.folder_id.in_(folder_ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_space_cleanup(self, space_id: uuid.UUID) -> list[Asset]:
        """空间删除级联：含软删行（对象存储清理需要 storage_key 全集）。"""
        stmt = select(Asset).where(Asset.space_id == space_id)
        return list((await self.session.execute(stmt)).scalars().all())


class AssetVersionRepo(Repository[AssetVersion]):
    model = AssetVersion

    async def latest_for(self, asset_id: uuid.UUID) -> AssetVersion | None:
        stmt = (
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id)
            .order_by(AssetVersion.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class SemanticUnitRepo(Repository[SemanticUnit]):
    model = SemanticUnit

    async def ids_for_version(self, version_id: uuid.UUID) -> set[uuid.UUID]:
        stmt = select(SemanticUnit.id).where(SemanticUnit.version_id == version_id)
        return set((await self.session.execute(stmt)).scalars().all())

    async def for_version(self, version_id: uuid.UUID) -> list[SemanticUnit]:
        stmt = select(SemanticUnit).where(SemanticUnit.version_id == version_id)
        return list((await self.session.execute(stmt)).scalars().all())


class ProcessingJobRepo(Repository[ProcessingJob]):
    model = ProcessingJob

    async def recent_for_asset(self, asset_id: uuid.UUID, limit: int = 50) -> list[ProcessingJob]:
        stmt = (
            select(ProcessingJob)
            .where(ProcessingJob.asset_id == asset_id)
            .order_by(ProcessingJob.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# P2-CORE-04 协作与组织域仓储
# ---------------------------------------------------------------------------


class TagRepo(Repository[Tag]):
    model = Tag

    async def get_or_create(
        self, *, tenant_id: uuid.UUID | None, name: str, created_by: uuid.UUID | None = None
    ) -> Tag:
        stmt = self._base_select(name=name)
        if tenant_id is not None:
            stmt = stmt.where(Tag.tenant_id == tenant_id)
        tag = (await self.session.execute(stmt)).scalar_one_or_none()
        if tag is None:
            tag = await self.create(tenant_id=tenant_id, name=name, created_by=created_by)
        return tag

    async def list_by_ids(self, ids: list[uuid.UUID]) -> list[Tag]:
        if not ids:
            return []
        stmt = self._base_select().where(Tag.id.in_(ids)).order_by(Tag.name.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def tags_for_asset(self, asset_id: uuid.UUID) -> list[Tag]:
        stmt = (
            select(Tag)
            .join(AssetTag, AssetTag.tag_id == Tag.id)
            .where(AssetTag.asset_id == asset_id, Tag.deleted_at.is_(None))
            .order_by(Tag.name.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_asset_tags(self, asset: Asset, tags: list[Tag]) -> None:
        """整体替换资产的标签关联。"""
        existing = (
            (await self.session.execute(select(AssetTag).where(AssetTag.asset_id == asset.id)))
            .scalars()
            .all()
        )
        for row in existing:
            await self.session.delete(row)
        for tag in tags:
            self.session.add(AssetTag(asset_id=asset.id, tag_id=tag.id, tenant_id=asset.tenant_id))
        await self.session.flush()


class CategoryRepo(Repository[Category]):
    model = Category

    async def list_for_space(self, space_id: uuid.UUID) -> list[Category]:
        stmt = self._base_select(space_id=space_id).order_by(Category.name.asc())
        return list((await self.session.execute(stmt)).scalars().all())


class MetadataFieldRepo(Repository[MetadataField]):
    model = MetadataField

    async def list_for_space(self, space_id: uuid.UUID) -> list[MetadataField]:
        stmt = self._base_select(space_id=space_id).order_by(MetadataField.key.asc())
        return list((await self.session.execute(stmt)).scalars().all())


class AssetRenditionRepo(Repository[AssetRendition]):
    model = AssetRendition

    async def for_asset(self, asset_id: uuid.UUID, kind: str | None = None) -> list[AssetRendition]:
        stmt = self._base_select(asset_id=asset_id)
        if kind:
            stmt = stmt.where(AssetRendition.kind == kind)
        return list((await self.session.execute(stmt)).scalars().all())


class NotificationRepo(Repository[Notification]):
    model = Notification

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = 50, unread_only: bool = False
    ) -> list[Notification]:
        stmt = self._base_select(user_id=user_id).order_by(Notification.created_at.desc())
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return list((await self.session.execute(stmt.limit(limit))).scalars().all())

    async def unread_count(self, user_id: uuid.UUID) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(
            self._base_select(user_id=user_id).where(Notification.read_at.is_(None)).subquery()
        )
        return (await self.session.execute(stmt)).scalar_one()


class SpaceUsageRepo(Repository[SpaceUsage]):
    model = SpaceUsage

    async def get_for_space(self, space_id: uuid.UUID) -> SpaceUsage | None:
        stmt = select(SpaceUsage).where(SpaceUsage.space_id == space_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class AgentEnvironmentRepo(Repository[AgentEnvironment]):
    """工作环境仓储：env 与账号解耦，默认取用户最早创建的活跃 env。"""

    model = AgentEnvironment

    async def get_default_for_user(self, user_id: uuid.UUID) -> AgentEnvironment | None:
        stmt = (
            self._base_select(owner_user_id=user_id, status=AgentEnvironmentStatus.ACTIVE)
            .order_by(AgentEnvironment.created_at.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: uuid.UUID) -> list[AgentEnvironment]:
        stmt = self._base_select(owner_user_id=user_id).order_by(AgentEnvironment.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_or_create_default(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID | None, title: str
    ) -> AgentEnvironment:
        env = await self.get_default_for_user(user_id)
        if env is not None:
            return env
        return await self.create(
            owner_user_id=user_id,
            tenant_id=tenant_id,
            title=title,
            status=AgentEnvironmentStatus.ACTIVE,
        )


class AgentSessionRepo(Repository[AgentSession]):
    """会话元数据仓储：消息本体在 dsh JSONL，此处只管 UI 语义。"""

    model = AgentSession

    async def list_for_env(
        self, env_id: uuid.UUID, *, include_archived: bool = False
    ) -> list[AgentSession]:
        stmt = self._base_select(env_id=env_id)
        if not include_archived:
            stmt = stmt.where(AgentSession.archived_at.is_(None))
        stmt = stmt.order_by(
            AgentSession.last_message_at.desc().nullslast(), AgentSession.created_at.desc()
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_in_env(
        self, session_id: uuid.UUID, env_id: uuid.UUID, *, include_archived: bool = False
    ) -> AgentSession | None:
        stmt = self._base_select(id=session_id, env_id=env_id)
        if not include_archived:
            stmt = stmt.where(AgentSession.archived_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_for_env(self, env_id: uuid.UUID, *, include_archived: bool = False) -> int:
        stmt = self._base_select(env_id=env_id)
        if not include_archived:
            stmt = stmt.where(AgentSession.archived_at.is_(None))
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return (await self.session.execute(count_stmt)).scalar_one()


async def resolve_space_id(session: AsyncSession, settings: Settings) -> uuid.UUID:
    """按 slug 解析默认空间；P2 起由请求参数/成员关系取代。

    迁移 0002 seed 了 slug=default 的空间；查不到时回退到常量 UUID
    （constants.FALLBACK_DEFAULT_SPACE_ID），保证全新库未跑 seed 也可用。
    """
    from loomvec.core.constants import FALLBACK_DEFAULT_SPACE_ID

    space = await SpaceRepo(session).get_by_slug(settings.default_space_slug)
    if space is not None:
        return space.id
    return uuid.UUID(FALLBACK_DEFAULT_SPACE_ID)
