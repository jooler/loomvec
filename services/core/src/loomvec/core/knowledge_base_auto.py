"""管线自动归类的知识库系统路径（worker 调用，不经人工权限校验；见 docs/17）。

- 名称查重：只认「规范化后完全相同」——NFKC 全角/上标归一、casefold、英文词尾复数 s
  归一、去掉空白/标点/下划线（连接词间的连字符按分隔符去掉，如 "graph-neural-network"）。
  不做相似度模糊匹配：近似名称常是语义不同甚至相反的兄弟主题（1 型/2 型糖尿病、小细胞/
  非小细胞肺癌，以及 HER2+/HER2-、ER+/ER-、CD4+/CD4- 这类生物医学里常见的正负号后缀
  写法，含上标 ⁺/⁻、全角 ＋/－、括号 "HER2(-)"、LaTeX "CD4^{-}" 等变体），LLM 已判定
  现有节点不合适时不能强行并入。因此 "+"、"#"、未被当作连接符去掉的 "-" 都保留；如果
  规范化后一个字母/数字都不剩（纯符号，如 "!!! ---"），才归一为空；
- 合并重定向：人工合并掉的节点记 merged_into，同名建议与归类写入都沿链落到目标节点；
  删除（未合并）的同名节点视为人工否决，不再自动重建；
- 并发：同一空间的系统新建经事务级 advisory lock 串行化，查重、pending 上限与插入
  在锁内完成（覆盖 tenant 为空时唯一约束不生效、以及同名不同写法并发新建的情况）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import Space, Tag, TagStatus
from loomvec.core.errors import ValidationError
from loomvec.core.logging import get_logger
from loomvec.core.taxonomy import normalize_tag_name

logger = get_logger("loomvec.core.knowledge_base_auto")

NAME_MAX_LEN = 64  # 与 Tag.name 列宽一致
_PLURAL_S = re.compile(r"(?<=[a-z]{3})(?<!s)s(?![a-z])")  # 英文词尾单个 s（不动 -ss）
_SEP_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")  # 两侧都是字母/数字时才是连接用连字符，去掉
_NON_WORD = re.compile(r"[^\w+#-]|_")  # 其余保留 + # 和未被当连接符去掉的 "-"
_MERGE_CHAIN_MAX = 16
# NFKC 不会把这几个书写变体折叠成 ASCII "-"（减号、一般连字符、不断行连字符；
# 小连字符 FE63 其实 NFKC 已处理，这里显式列出做兜底），需要手动归一，否则
# "HER2⁻"（经 NFKC 变成 U+2212）会被当成普通符号而不是有意义的负号后缀。
_DASH_TRANSLATION = str.maketrans({"−": "-", "‐": "-", "‑": "-", "﹣": "-"})


def name_key(name: str) -> str:
    """查重键："Graph Neural Networks" / "graph-neural-network" 同键；"1 型" 与 "2 型"、
    "HER2+" 与 "HER2-"（含上标 ⁺/⁻、全角 ＋/－、"HER2(-)"、"CD4^{-}" 等写法变体）不同键。
    "+"/"#"、未被当作连接符去掉的 "-" 永不剥离；规范化后不含任何字母/数字（纯符号，如
    "!!! ---"、单独的 "-"/"+"/"#"）才归一为空。
    """
    text = unicodedata.normalize("NFKC", name).translate(_DASH_TRANSLATION)
    text = _PLURAL_S.sub("", text.casefold())
    text = _SEP_HYPHEN.sub("", text)
    text = _NON_WORD.sub("", text)
    return text if any(ch.isalnum() for ch in text) else ""


def find_same_name_tag(tags: list[Tag], name: str) -> Tag | None:
    """tags 中与 name 查重键相同的节点（active 优先）；没有返回 None。"""
    key = name_key(name)
    if not key:
        return None
    matches = sorted(
        (t for t in tags if name_key(t.name) == key), key=lambda t: t.status != TagStatus.ACTIVE
    )
    return matches[0] if matches else None


async def resolve_live_tag(
    session: AsyncSession, tag_id: uuid.UUID, *, lock: bool = False
) -> Tag | None:
    """沿 merged_into 链找到仍存在的节点：未删除返回自身；删除且未合并（或链断）返回 None。

    单 id 场景的薄封装，实际追链加锁逻辑见 resolve_live_tags（批量版）；单独保留是因为
    create_system_tag 等调用方一次只查一个 id，不需要关心锁顺序。
    """
    return (await resolve_live_tags(session, [tag_id], lock=lock))[tag_id]


async def resolve_live_tags(
    session: AsyncSession, tag_ids: list[uuid.UUID], *, lock: bool = False
) -> dict[uuid.UUID, Tag | None]:
    """批量沿 merged_into 链找到仍存在的节点：每一跳都把待解析节点按 id 升序合成一条
    语句一次性加锁。

    自动归类写关联前要对多个候选节点加锁复核，人工 merge 对源/目标节点按 id 升序加
    FOR UPDATE（见 services/api/.../knowledge_bases.py:merge）；如果各自逐个 id 加锁、
    顺序又与对方不一致，会出现 A 等 B、B 等 A 的死锁。按 id 升序批量加锁与 merge 用
    同一种全局顺序，消除两个候选节点之间这一类死锁。

    lock=True 时用 FOR KEY SHARE（不是 FOR SHARE）：merge 提交时会把源节点的直接子节点
    一并 UPDATE parent_id（FOR NO KEY UPDATE 隐式行锁），如果这里仍用 FOR SHARE，会和
    那次 UPDATE 互斥，又会在"自动归类命中子节点、merge 命中父节点"的组合下形成新的
    AB-BA 死锁；FOR KEY SHARE 只和真正改动被引用列（唯一约束/主键）的操作互斥，与
    UPDATE parent_id 这种只碰非 key 列的操作兼容，同时仍与 merge 本身的 FOR UPDATE
    互斥，足以保证写入的文献关联不会挂到并发合并掉的节点上。

    注：如果候选节点在追链过程中跨越了多跳（即解析期间又被合并了一次以上），这一跳
    新牵涉进来的节点 id 不在最初那条批量语句的排序范围内，理论上仍可能与一次跨越多个
    版本的 merge 构成环；这种多跳场景发生概率很低，出现时会被 PG 判定为死锁并中止其中
    一个事务（自动归类失败会被外层捕获记日志，不影响已完成的解析和向量化，可重试）。
    """
    result: dict[uuid.UUID, Tag | None] = {}
    visited: dict[uuid.UUID, set[uuid.UUID]] = {tid: {tid} for tid in tag_ids}
    current: dict[uuid.UUID, uuid.UUID] = {tid: tid for tid in tag_ids}
    hops = 0
    while current and hops < _MERGE_CHAIN_MAX:
        hops += 1
        stmt = (
            select(Tag)
            .where(Tag.id.in_(sorted(set(current.values()))))
            .order_by(Tag.id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update(read=True, key_share=True)
        rows = {t.id: t for t in (await session.execute(stmt)).scalars().all()}
        next_round: dict[uuid.UUID, uuid.UUID] = {}
        for orig, node_id in current.items():
            tag = rows.get(node_id)
            if tag is None or tag.deleted_at is None:
                result[orig] = tag
                continue
            nxt = tag.merged_into
            if nxt is None or nxt in visited[orig]:
                result[orig] = None
                continue
            visited[orig].add(nxt)
            next_round[orig] = nxt
        current = next_round
    for orig in current:
        result[orig] = None
    return result


async def count_pending_tags(session: AsyncSession, space_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Tag)
            .where(
                Tag.space_id == space_id,
                Tag.status == TagStatus.PENDING.value,
                Tag.deleted_at.is_(None),
            )
        )
    ).scalar_one()


async def _lock_space(session: AsyncSession, space_id: uuid.UUID) -> None:
    """事务级 advisory lock（提交/回滚自动释放）：同空间的系统新建串行执行。"""
    digest = hashlib.blake2b(f"kb-auto:{space_id}".encode(), digest_size=8).digest()
    key = int.from_bytes(digest, "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def create_system_tag(
    session: AsyncSession,
    *,
    space: Space,
    name: str,
    parent_id: uuid.UUID | None = None,
    reason: str | None = None,
    max_pending: int | None = None,
) -> Tag | None:
    """系统身份新建知识库节点（status=pending，无创建人与成员）。

    依次：空间内（含早期租户级标签）有同名活节点 → 复用；同名节点被合并过 → 返回合并
    目标；同名节点被删除 → 返回 None（人工已否决）；空间未处理的 pending 节点数已达
    max_pending → 返回 None；否则新建。只 flush 不 commit，由调用方与文献关联同事务提交
    （advisory lock 随之释放）。
    """
    name = normalize_tag_name(name)
    key = name_key(name)
    if not key:
        raise ValidationError("知识库名称不能为空")
    if len(name) > NAME_MAX_LEN:
        raise ValidationError("知识库名称过长", max_length=NAME_MAX_LEN)
    await _lock_space(session, space.id)
    rows = list(
        (
            await session.execute(
                select(Tag)
                .where(
                    Tag.tenant_id == space.tenant_id,
                    (Tag.space_id == space.id) | Tag.space_id.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    existing = find_same_name_tag([t for t in rows if t.deleted_at is None], name)
    if existing is not None:
        return existing
    removed = [t for t in rows if t.deleted_at is not None and name_key(t.name) == key]
    for tag in removed:
        target = await resolve_live_tag(session, tag.id)
        if target is not None:
            logger.info(
                "knowledge_base_suggestion_redirected",
                space_id=str(space.id),
                name=name,
                target_id=str(target.id),
            )
            return target
    if removed:
        logger.info(
            "knowledge_base_suggestion_previously_removed", space_id=str(space.id), name=name
        )
        return None
    if max_pending is not None and await count_pending_tags(session, space.id) >= max_pending:
        logger.info("knowledge_base_pending_limit_reached", space_id=str(space.id), name=name)
        return None
    if parent_id is not None:
        parent = next((t for t in rows if t.id == parent_id and t.deleted_at is None), None)
        if parent is None:
            raise ValidationError("上级知识库不存在或不属于该空间", parent_id=str(parent_id))
    tag = Tag(
        tenant_id=space.tenant_id,
        space_id=space.id,
        name=name,
        parent_id=parent_id,
        status=TagStatus.PENDING.value,
    )
    try:
        async with session.begin_nested():
            session.add(tag)
            await session.flush()
    except IntegrityError:
        # 人工同时建了同名节点（人工路径不取 advisory lock）：唯一约束兜底，复用那一行
        return (
            await session.execute(
                select(Tag).where(
                    Tag.tenant_id == space.tenant_id,
                    Tag.space_id == space.id,
                    Tag.name == name,
                    Tag.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    logger.info(
        "knowledge_base_auto_created",
        tag_id=str(tag.id),
        space_id=str(space.id),
        name=name,
        parent_id=str(parent_id) if parent_id else None,
        reason=reason,
    )
    return tag
