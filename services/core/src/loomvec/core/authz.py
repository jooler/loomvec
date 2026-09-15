"""P2-CORE-02 资源级授权服务：成员关系 × 审核状态 → 空间/资产可见性与操作权。

分层约定：
- 纯判定函数（`at_least` / `decide_asset_visibility`）无 IO，供单测覆盖权限矩阵；
- DB 判定（`require_space_role` / `visible_space_ids`）供 API 依赖与仓储层调用，
  是"租户 × 空间 × 角色"三重过滤的单点实现——路由层不自行拼成员查询。

语义（09 文档 · 能力矩阵）：
- viewer：只读，且仅可见"通过审核"的资产（review_required 空间）；
- editor：上传/编辑/删除自己可管理范围内的资产，可见全部审核状态；
- owner：成员管理、空间设置、删除空间；
- 被移除成员即失权：一切判定即时查 space_member，不缓存。

公共空间（P5 运营端）：`user_group_ids` / `visible_public_spaces` /
`linked_public_space_ids` —— 可见性 = 空间分组勾选 × 用户所属分组，
检索生效再叠加用户链接（space_link）；与成员关系（space_member）完全解耦，
公共空间不向普通用户开放内容浏览，仅可作问答检索源。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import (
    ReviewStatus,
    Space,
    SpaceGroupVisibility,
    SpaceLink,
    SpaceMember,
    SpaceRole,
    SpaceType,
    UserGroup,
    UserGroupMember,
)
from loomvec.core.errors import NotFoundError, PermissionDeniedError

ROLE_RANK: dict[SpaceRole, int] = {
    SpaceRole.VIEWER: 0,
    SpaceRole.EDITOR: 1,
    SpaceRole.OWNER: 2,
}


def at_least(role: SpaceRole | str, min_role: SpaceRole | str) -> bool:
    """角色序数比较：owner > editor > viewer。"""
    r = ROLE_RANK[SpaceRole(role)]
    m = ROLE_RANK[SpaceRole(min_role)]
    return r >= m


def can_manage_asset(
    *,
    role: SpaceRole | str,
    asset_created_by: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> bool:
    """资产编辑/删除边界（doc 05 能力矩阵）：

    - owner：空间内全部资产；
    - editor：仅自己上传的（created_by == user_id）；API Key 身份（user_id 为空）
      仅可管理同为 API Key 登记的资产（created_by 为空）；
    - viewer：无权。
    """
    if at_least(role, SpaceRole.OWNER):
        return True
    if not at_least(role, SpaceRole.EDITOR):
        return False
    if user_id is None:  # API Key 身份：仅限 API Key 登记的资产
        return asset_created_by is None
    return asset_created_by == user_id


def decide_asset_visibility(
    *, review_required: bool, review_status: ReviewStatus | None, role: SpaceRole | str
) -> bool:
    """审核状态 × 角色判可见：viewer 仅见通过审核资产；editor/owner 全见。"""
    if at_least(role, SpaceRole.EDITOR):
        return True
    if not review_required:
        return True
    return review_status == ReviewStatus.APPROVED


@dataclass(frozen=True)
class SpaceAccess:
    """通过校验后的空间访问上下文（space + 成员行）。"""

    space: Space
    member: SpaceMember

    @property
    def role(self) -> SpaceRole:
        return self.member.role


async def get_membership(
    session: AsyncSession, *, space_id: uuid.UUID, user_id: uuid.UUID
) -> SpaceMember | None:
    stmt = select(SpaceMember).where(
        SpaceMember.space_id == space_id,
        SpaceMember.user_id == user_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def require_space_role(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    user_id: uuid.UUID,
    min_role: SpaceRole | str = SpaceRole.VIEWER,
) -> SpaceAccess:
    """空间访问闸门：空间必须存在，且请求者是具备最低角色的成员。

    非成员与无权限统一 403（不泄露空间存在性）；空间不存在 404。
    """
    space = (
        await session.execute(select(Space).where(Space.id == space_id, Space.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if space is None:
        raise NotFoundError(resource="space", id=str(space_id))
    member = await get_membership(session, space_id=space_id, user_id=user_id)
    if member is None or not at_least(member.role, min_role):
        raise PermissionDeniedError(reason="无该空间访问权限", space_id=str(space_id))
    return SpaceAccess(space=space, member=member)


async def visible_space_ids(session: AsyncSession, *, user_id: uuid.UUID) -> list[uuid.UUID]:
    """用户可见（成员中）的空间集合：检索/列表聚合过滤的根。"""
    stmt = select(SpaceMember.space_id).where(
        SpaceMember.user_id == user_id,
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 公共空间（P5 运营端）：分组可见性 × 用户链接，与成员关系解耦
# ---------------------------------------------------------------------------


async def user_group_ids(session: AsyncSession, *, user_id: uuid.UUID) -> list[uuid.UUID]:
    """用户所属**未删除**分组集合（公共空间可见性判定的输入）。

    join 分组行过滤软删：分组删除后其成员立即失去经分组获得的可见性，
    不依赖成员行/可见性行的清理进度。
    """
    stmt = (
        select(UserGroupMember.group_id)
        .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
        .where(UserGroupMember.user_id == user_id, UserGroup.deleted_at.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


def _public_space_filter():
    """公共空间有效行过滤：类型 public × 未删除 × 未封禁。"""
    return (
        Space.space_type == SpaceType.PUBLIC,
        Space.deleted_at.is_(None),
        Space.banned_at.is_(None),
    )


async def visible_public_spaces(session: AsyncSession, *, user_id: uuid.UUID) -> list[Space]:
    """当前用户经分组可见的公共空间（用户端"可链接"列表的唯一取数）。"""
    group_ids = await user_group_ids(session, user_id=user_id)
    if not group_ids:
        return []
    stmt = (
        select(Space)
        .join(SpaceGroupVisibility, SpaceGroupVisibility.space_id == Space.id)
        .where(*_public_space_filter(), SpaceGroupVisibility.group_id.in_(group_ids))
        .distinct()
        .order_by(Space.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def is_public_space_visible(
    session: AsyncSession, *, space: Space, user_id: uuid.UUID
) -> bool:
    """单个公共空间对用户是否可见（链接资格判定，与列表同口径）。

    有效行三条件与 _public_space_filter() 一致（已加载行无法复用列表达式，
    字段变更时两处需同步修改）。
    """
    if (
        space.space_type != SpaceType.PUBLIC
        or space.deleted_at is not None
        or space.banned_at is not None
    ):
        return False
    group_ids = await user_group_ids(session, user_id=user_id)
    if not group_ids:
        return False
    stmt = (
        select(SpaceGroupVisibility.group_id)
        .where(
            SpaceGroupVisibility.space_id == space.id,
            SpaceGroupVisibility.group_id.in_(group_ids),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def linked_public_space_ids(session: AsyncSession, *, user_id: uuid.UUID) -> list[uuid.UUID]:
    """用户已链接且当前仍对其可见的公共空间（问答检索源扩展集）。

    链接行 × 分组可见性即时求交：分组被移出/空间封禁后即时失效，
    不依赖后台清理链接行（与"被移出成员即失权"同语义）。
    """
    stmt = (
        select(SpaceLink.space_id)
        .join(Space, Space.id == SpaceLink.space_id)
        .join(SpaceGroupVisibility, SpaceGroupVisibility.space_id == SpaceLink.space_id)
        .join(UserGroupMember, UserGroupMember.group_id == SpaceGroupVisibility.group_id)
        .where(
            SpaceLink.user_id == user_id,
            UserGroupMember.user_id == user_id,
            *_public_space_filter(),
        )
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())
