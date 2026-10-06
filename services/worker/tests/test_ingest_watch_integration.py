"""目录监听入库台账流转的 DB 级集成测试（testcontainers 临时 PG；对象存储/Redis/Celery 用桩）。

覆盖：首轮登记 pending_stable → 稳定后上传登记并入队（done，资产带来源标记、无上传人）→
上传策略不合规与 PDF 未写完整记 error 且不影响其它文件 → 软链接不入库 → 其它路径同内容判
duplicate → 只改 mtime 判 duplicate → 内容变化作为新资产入库 → 入队失败保持 uploaded、
下轮补偿入队 → 长期消失文件的台账行清理 → 扫描互斥锁；跨空间去重（目标空间或同租户已发布的
公共空间已有可用的即跳过、目标空间优先；他人协作 / 个人空间、未发布的公共空间、审核未通过、
已删 / 失败 / 驳回 / 封禁与其它租户不算；空租户）→ 等待同大小在途资产 → duplicate 指向的资产
不可用（含撤销发布、不在去重范围内）后重开 → 改 space_id 后按新空间去重。
无 Docker 或 testcontainers 时跳过。
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LOOMVEC_SKIP_INTEGRATION") == "1" or shutil.which("docker") is None,
        reason="需要 Docker",
    ),
]

PDF_A = b"%PDF-1.4\npaper-a\n%%EOF\n"
PDF_A2 = b"%PDF-1.4\npaper-a v2 (sync finished)\n%%EOF\n"


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, bucket, key, data, content_type=None) -> None:
        self.objects[key] = data

    async def head_object(self, bucket, key):
        return {"ContentLength": len(self.objects[key])} if key in self.objects else None

    async def delete_object(self, bucket, key) -> None:
        self.objects.pop(key, None)


class _Lock:
    def __init__(self, redis: _Redis, name: str) -> None:
        self.redis, self.name, self.token = redis, name, uuid.uuid4().hex

    async def acquire(self, blocking=None):
        if self.name in self.redis.held:
            return False
        self.redis.held[self.name] = self.token
        return True

    async def reacquire(self):
        from redis.exceptions import LockNotOwnedError

        if self.redis.held.get(self.name) != self.token:
            raise LockNotOwnedError("lost")

    async def release(self):
        from redis.exceptions import LockNotOwnedError

        if self.redis.held.get(self.name) != self.token:
            raise LockNotOwnedError("lost")
        del self.redis.held[self.name]


class _Redis:
    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def lock(self, name, timeout=None, thread_local=True):
        assert timeout >= 300
        return _Lock(self, name)


@pytest.fixture()
async def session_factory(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from testcontainers.postgres import PostgresContainer

    from loomvec.api.init_db import init_database
    from loomvec.core.constants import GRAPH_NAME

    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    with PostgresContainer(
        "loomvec/postgres-age:18", username="loomvec", password="loomvec", dbname="loomvec"
    ) as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        # 同 deploy/compose/postgres-age/init/01-age-init.sql 预建 AGE 图（独立连接），
        # 使 init_db 走与 compose 环境一致的路径，任意新连接都能看到表
        bootstrap = create_async_engine(url)
        async with bootstrap.begin() as conn:
            await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS age")
            await conn.exec_driver_sql("LOAD 'age'")
            await conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')
            await conn.exec_driver_sql(f"SELECT create_graph('{GRAPH_NAME}')")
        await bootstrap.dispose()
        engine = create_async_engine(url)
        await init_database(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()


def _harness(session_factory, tmp_path, monkeypatch, space_id: str | None = None):
    """扫描依赖桩：返回 (deps, 已入队任务列表, 对象存储桩, broker 故障开关)。"""
    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.config import IngestWatchSettings
    from loomvec.worker import ingest_watch

    sent: list[tuple] = []
    broker_down = {"value": False}

    def send_task(name, args, queue):
        if broker_down["value"]:
            raise ConnectionError("broker down")
        sent.append((name, args))

    monkeypatch.setattr(ingest_watch, "celery_app", SimpleNamespace(send_task=send_task))
    storage = _Storage()
    deps = SimpleNamespace(
        settings=SimpleNamespace(
            ingest_watch=IngestWatchSettings(
                dir=str(tmp_path),
                space_id=space_id or DEFAULT_SPACE_ID,
                stability_seconds=0,
                extensions=[".pdf", ".epub"],  # .epub 不在上传白名单内 → 登记前即被拒
            ),
            worker=SimpleNamespace(task_time_limit=60),
            storage=SimpleNamespace(bucket_raw="raw"),
        ),
        session_factory=session_factory,
        storage=storage,
        redis=_Redis(),
    )
    return deps, sent, storage, broker_down


async def test_ingest_watch_ledger_flow(session_factory, tmp_path, monkeypatch):
    from sqlalchemy import select, update

    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.authz import is_system_ingested
    from loomvec.core.constants import INGEST_WATCH_LOCK_KEY
    from loomvec.core.db.models import Asset, IngestFile, IngestFileStatus, Space
    from loomvec.worker import ingest_watch

    deps, sent, storage, broker_down = _harness(session_factory, tmp_path, monkeypatch)
    scan = ingest_watch.scan_watch_dir

    async def ledger() -> dict[str, IngestFile]:
        async with session_factory() as session:
            rows = (await session.execute(select(IngestFile))).scalars().all()
        return {os.path.basename(r.file_path): r for r in rows}

    paper = tmp_path / "a.pdf"
    paper.write_bytes(PDF_A)
    (tmp_path / "x.epub").write_bytes(b"epub")
    (tmp_path / "partial.pdf").write_bytes(b"%PDF-1.4\nstill syncing")  # 缺 %%EOF
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}.pdf"
    outside.write_bytes(PDF_A2)
    (tmp_path / "link.pdf").symlink_to(outside)

    # 首轮只登记，不处理；软链接不进台账
    assert (await scan(deps))["stable"] == 0
    rows = await ledger()
    assert set(rows) == {"a.pdf", "x.epub", "partial.pdf"}
    assert {r.status for r in rows.values()} == {IngestFileStatus.PENDING_STABLE}

    # 第二轮稳定：a.pdf 入库；x.epub、partial.pdf 记 error 但不影响 a.pdf
    result = await scan(deps)
    assert (result["ingested"], result["error"]) == (1, 2)
    rows = await ledger()
    assert rows["a.pdf"].status == IngestFileStatus.DONE
    assert "ValidationError" in rows["x.epub"].error_message
    assert "%%EOF" in rows["partial.pdf"].error_message
    first_asset = rows["a.pdf"].asset_id
    async with session_factory() as session:
        asset = await session.get(Asset, first_asset)
        space = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
    assert (asset.name, asset.created_by, asset.tenant_id) == ("a.pdf", None, space.tenant_id)
    assert is_system_ingested(asset.asset_meta)
    assert sent == [("pipeline.process_asset", [str(first_asset), "parse"])]
    assert len(storage.objects) == 1

    # 同步写完（文件变化）→ 自动重试入库
    (tmp_path / "partial.pdf").write_bytes(b"%PDF-1.4\nsynced\n%%EOF\n")
    await scan(deps)
    assert (await scan(deps))["ingested"] == 1
    assert (await ledger())["partial.pdf"].status == IngestFileStatus.DONE
    assert len(sent) == 2

    # 其它路径同内容 → duplicate，不重复上传、不重复入队；error 行文件未变化 → 不重试
    shutil.copyfile(paper, tmp_path / "copy.pdf")
    await scan(deps)
    await scan(deps)
    rows = await ledger()
    assert (rows["copy.pdf"].status, rows["copy.pdf"].asset_id) == (
        IngestFileStatus.DUPLICATE,
        first_asset,
    )
    assert rows["x.epub"].status == IngestFileStatus.ERROR
    assert len(storage.objects) == 2 and len(sent) == 2

    # 只改 mtime（内容不变）→ 回到 pending_stable 后判 duplicate（指向本路径上次的资产）
    st = paper.stat()
    os.utime(paper, (st.st_atime, st.st_mtime + 10))
    await scan(deps)
    assert (await ledger())["a.pdf"].status == IngestFileStatus.PENDING_STABLE
    await scan(deps)
    rows = await ledger()
    assert (rows["a.pdf"].status, rows["a.pdf"].asset_id) == (
        IngestFileStatus.DUPLICATE,
        first_asset,
    )
    assert len(storage.objects) == 2 and len(sent) == 2

    # 内容变化 + 入队失败：资产已登记，台账保持 uploaded；下轮补偿入队后 done，不重复登记
    paper.write_bytes(PDF_A2)
    await scan(deps)
    broker_down["value"] = True
    assert (await scan(deps))["enqueue_failed"] == 1
    rows = await ledger()
    assert rows["a.pdf"].status == IngestFileStatus.UPLOADED
    second_asset = rows["a.pdf"].asset_id
    assert second_asset not in (None, first_asset)
    os.utime(paper, (st.st_atime, st.st_mtime + 20))  # uploaded 期间文件变化也不重新登记
    broker_down["value"] = False
    assert (await scan(deps))["resumed"] == 1
    rows = await ledger()
    assert (rows["a.pdf"].status, rows["a.pdf"].asset_id) == (
        IngestFileStatus.DONE,
        second_asset,
    )
    assert sent[-1] == ("pipeline.process_asset", [str(second_asset), "parse"])
    assert len(storage.objects) == 3
    async with session_factory() as session:
        assert len((await session.execute(select(Asset).where(Asset.name == "a.pdf"))).all()) == 2

    # 长期从目录消失的文件：台账行按保留期清理
    (tmp_path / "copy.pdf").unlink()
    async with session_factory() as session:
        await session.execute(
            update(IngestFile)
            .where(IngestFile.file_path == str(tmp_path / "copy.pdf"))
            .values(last_checked_at=datetime.now(UTC) - timedelta(days=30))
        )
        await session.commit()
    await scan(deps)
    assert "copy.pdf" not in await ledger()

    # 扫描互斥：锁被占用时本轮跳过
    deps.redis.held[INGEST_WATCH_LOCK_KEY] = "other"
    assert await scan(deps) == {"status": "locked"}
    outside.unlink()


def _pdf(name: str) -> bytes:
    return f"%PDF-1.4\n{name}\n%%EOF\n".encode()


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _space(session, slug: str, tenant_id, *, kind: str = "shared", review_required=False):
    from loomvec.core.db.models import Space, SpaceType

    space = Space(
        slug=slug,
        name=slug,
        space_type=SpaceType(kind),
        tenant_id=tenant_id,
        review_required=review_required,
    )
    session.add(space)
    return space


async def _publish(session, space, *, group_deleted: bool = False):
    """把公共空间发布给一个新建分组（行存在即该分组可见）；返回可见性行。"""
    from loomvec.core.db.models import SpaceGroupVisibility, UserGroup

    group = UserGroup(
        tenant_id=space.tenant_id,
        name=f"group-{uuid.uuid4().hex[:8]}",
        deleted_at=datetime.now(UTC) if group_deleted else None,
    )
    session.add(group)
    await session.flush()
    visibility = SpaceGroupVisibility(space_id=space.id, group_id=group.id)
    session.add(visibility)
    await session.flush()
    return visibility


def _asset(session, space, *, checksum, created_at=None, size=0, **fields):
    from loomvec.core.db.models import Asset

    asset = Asset(
        space_id=space.id,
        tenant_id=space.tenant_id,
        name="existing.pdf",
        mime_type="application/pdf",
        checksum=checksum,
        size_bytes=size,
        # created_at 为不带时区的列（server_default now()）
        created_at=created_at or datetime.now(),
        **fields,
    )
    session.add(asset)
    return asset


async def _ledger(session_factory) -> dict:
    from sqlalchemy import select

    from loomvec.core.db.models import IngestFile

    async with session_factory() as session:
        rows = (await session.execute(select(IngestFile))).scalars().all()
    return {os.path.basename(r.file_path): r for r in rows}


async def test_ingest_watch_dedup_across_spaces(session_factory, tmp_path, monkeypatch):
    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import (
        AssetStatus,
        IngestFile,
        IngestFileStatus,
        ReviewStatus,
        Space,
        Tenant,
    )
    from loomvec.worker import ingest_watch

    deps, sent, storage, _ = _harness(session_factory, tmp_path, monkeypatch)
    names = (
        "public-space",  # 同租户公共空间已有 → duplicate
        "both",  # 目标空间与公共空间都有 → 指向目标空间的
        "earliest",  # 两个公共空间都有 → 指向更早的
        "ledger",  # 台账另一路径登记过、资产在公共空间且尚无 checksum → duplicate
        "review-approved",  # 需审核的公共空间里审核通过的 → duplicate
        "shared-space",  # 以下均不算已有 → 正常入库：他人协作空间里的
        "personal-space",
        "unpublished-public",  # 未发布给任何分组的公共空间
        "deleted-group",  # 只发布给已删除分组的公共空间
        "review-pending",  # 需审核的公共空间里尚未审核通过的
        "asset-deleted",
        "deleted-space",
        "banned-space",
        "failed",
        "rejected",
        "other-tenant",
    )
    contents = {name: _pdf(name) for name in names}
    earlier = datetime.now() - timedelta(days=1)

    async with session_factory() as session:
        target = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
        other_tenant = Tenant(name="other-tenant")
        session.add(other_tenant)
        await session.flush()
        sibling = _space(session, "sibling", target.tenant_id, kind="public")
        cousin = _space(session, "cousin", target.tenant_id, kind="public")
        reviewed = _space(
            session, "reviewed", target.tenant_id, kind="public", review_required=True
        )
        private = _space(session, "private", target.tenant_id)
        personal = _space(session, "personal", target.tenant_id, kind="personal")
        unpublished = _space(session, "unpublished", target.tenant_id, kind="public")
        group_gone = _space(session, "group-gone", target.tenant_id, kind="public")
        removed = _space(session, "removed", target.tenant_id, kind="public")
        removed.deleted_at = datetime.now(UTC)
        banned = _space(session, "banned", target.tenant_id, kind="public")
        banned.banned_at = datetime.now(UTC)
        foreign = _space(session, "foreign", other_tenant.id, kind="public")
        await session.flush()
        for published in (sibling, cousin, reviewed, removed, banned, foreign):
            await _publish(session, published)
        await _publish(session, group_gone, group_deleted=True)

        def sha(name):
            return _sha(contents[name])

        in_sibling = _asset(session, sibling, checksum=sha("public-space"), created_at=earlier)
        _asset(session, sibling, checksum=sha("both"), created_at=earlier)  # 更早，但不在目标空间
        approved = _asset(
            session, reviewed, checksum=sha("review-approved"), review_status=ReviewStatus.APPROVED
        )
        _asset(session, private, checksum=sha("shared-space"), created_at=earlier)
        _asset(session, personal, checksum=sha("personal-space"))
        _asset(session, unpublished, checksum=sha("unpublished-public"))
        _asset(session, group_gone, checksum=sha("deleted-group"))
        _asset(
            session,
            reviewed,
            checksum=sha("review-pending"),
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        in_target = _asset(session, target, checksum=sha("both"))
        oldest = _asset(
            session, cousin, checksum=sha("earliest"), created_at=earlier - timedelta(days=1)
        )
        _asset(session, sibling, checksum=sha("earliest"), created_at=earlier)
        unparsed = _asset(session, sibling, checksum=None, status=AssetStatus.READY)
        _asset(session, sibling, checksum=sha("asset-deleted"), deleted_at=datetime.now(UTC))
        _asset(session, removed, checksum=sha("deleted-space"))
        _asset(session, banned, checksum=sha("banned-space"))
        _asset(session, sibling, checksum=sha("failed"), status=AssetStatus.FAILED)
        _asset(session, sibling, checksum=sha("rejected"), review_status=ReviewStatus.REJECTED)
        _asset(session, foreign, checksum=sha("other-tenant"))
        await session.flush()
        session.add(
            IngestFile(
                tenant_id=target.tenant_id,
                watch_dir=os.path.normpath(str(tmp_path)),
                file_path=str(tmp_path / "gone" / "ledger-old.pdf"),
                file_size=len(contents["ledger"]),
                file_mtime=0.0,
                checksum=sha("ledger"),
                status=IngestFileStatus.DONE.value,
                asset_id=unparsed.id,
                stable_since=datetime.now(UTC),
                last_checked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    for name, content in contents.items():
        (tmp_path / f"{name}.pdf").write_bytes(content)
    await ingest_watch.scan_watch_dir(deps)
    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["duplicate"], result["ingested"]) == (5, 11)

    rows = await _ledger(session_factory)
    expected_duplicates = {
        "public-space.pdf": in_sibling.id,
        "both.pdf": in_target.id,
        "earliest.pdf": oldest.id,
        "ledger.pdf": unparsed.id,
        "review-approved.pdf": approved.id,
    }
    for name, asset_id in expected_duplicates.items():
        assert (rows[name].status, rows[name].asset_id) == (IngestFileStatus.DUPLICATE, asset_id)
    ingested = {f"{n}.pdf" for n in names} - set(expected_duplicates)
    assert {rows[n].status for n in ingested} == {IngestFileStatus.DONE}
    assert len(storage.objects) == 11 and len(sent) == 11


async def test_ingest_watch_dedup_null_tenant(session_factory, tmp_path, monkeypatch):
    """tenant_id 为空的空间之间按同一租户去重；不会命中有租户的空间。"""
    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import IngestFileStatus, Space
    from loomvec.worker import ingest_watch

    shared, tenanted = _pdf("shared"), _pdf("tenanted")
    async with session_factory() as session:
        target = _space(session, "null-target", None)
        sibling = _space(session, "null-sibling", None, kind="public")
        await session.flush()
        await _publish(session, sibling)
        in_sibling = _asset(session, sibling, checksum=_sha(shared))
        _asset(
            session, await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID)), checksum=_sha(tenanted)
        )
        await session.commit()
        target_id = str(target.id)

    deps, _, _, _ = _harness(session_factory, tmp_path, monkeypatch, space_id=target_id)
    (tmp_path / "shared.pdf").write_bytes(shared)
    (tmp_path / "tenanted.pdf").write_bytes(tenanted)
    await ingest_watch.scan_watch_dir(deps)
    result = await ingest_watch.scan_watch_dir(deps)

    assert (result["duplicate"], result["ingested"]) == (1, 1)
    rows = await _ledger(session_factory)
    assert (rows["shared.pdf"].status, rows["shared.pdf"].asset_id) == (
        IngestFileStatus.DUPLICATE,
        in_sibling.id,
    )
    assert rows["tenanted.pdf"].status == IngestFileStatus.DONE


async def test_ingest_watch_waits_for_inflight_same_size(session_factory, tmp_path, monkeypatch):
    """目标空间或公共空间里同大小、尚无 checksum 的在途资产：等它算出 checksum 再判；
    超过等待上限照常入库；他人协作空间、需审核的公共空间里的在途资产不等。"""
    from sqlalchemy import update

    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import Asset, AssetStatus, IngestFile, IngestFileStatus, Space
    from loomvec.worker import ingest_watch

    deps, sent, _, _ = _harness(session_factory, tmp_path, monkeypatch)
    paper, other, late = _pdf("paper"), _pdf("other-size!"), _pdf("late")
    private_copy, review_copy = _pdf("in-private-space"), _pdf("in-review-space-copy")
    async with session_factory() as session:
        target = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
        sibling = _space(session, "sibling", target.tenant_id, kind="public")
        reviewed = _space(
            session, "reviewed", target.tenant_id, kind="public", review_required=True
        )
        private = _space(session, "private", target.tenant_id)
        await session.flush()
        await _publish(session, sibling)
        await _publish(session, reviewed)
        uploading = _asset(
            session, sibling, checksum=None, size=len(paper), status=AssetStatus.PROCESSING
        )
        _asset(session, private, checksum=None, size=len(private_copy), status=AssetStatus.PENDING)
        _asset(session, reviewed, checksum=None, size=len(review_copy), status=AssetStatus.PENDING)
        await session.commit()

    (tmp_path / "paper.pdf").write_bytes(paper)
    (tmp_path / "other.pdf").write_bytes(other)  # 大小不同 → 不等待
    (tmp_path / "private.pdf").write_bytes(private_copy)  # 同大小在途资产在他人协作空间 → 不等待
    (tmp_path / "review.pdf").write_bytes(review_copy)  # 在需审核的公共空间 → 不等待
    await ingest_watch.scan_watch_dir(deps)
    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["awaiting_inflight"], result["ingested"]) == (1, 3)
    rows = await _ledger(session_factory)
    assert rows["paper.pdf"].status == IngestFileStatus.PENDING_STABLE

    # 在途资产解析完写入 checksum → 下轮判 duplicate，不重复入库
    async with session_factory() as session:
        await session.execute(
            update(Asset).where(Asset.id == uploading.id).values(checksum=_sha(paper))
        )
        await session.commit()
    assert (await ingest_watch.scan_watch_dir(deps))["duplicate"] == 1
    assert (await _ledger(session_factory))["paper.pdf"].asset_id == uploading.id
    assert len(sent) == 3

    # 一直等不到 checksum：自稳定起超过等待上限后照常入库
    async with session_factory() as session:
        _asset(session, sibling, checksum=None, size=len(late), status=AssetStatus.PENDING)
        await session.commit()
    (tmp_path / "late.pdf").write_bytes(late)
    await ingest_watch.scan_watch_dir(deps)
    assert (await ingest_watch.scan_watch_dir(deps))["awaiting_inflight"] == 1
    async with session_factory() as session:
        await session.execute(
            update(IngestFile)
            .where(IngestFile.file_path == str(tmp_path / "late.pdf"))
            .values(stable_since=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()
    assert (await ingest_watch.scan_watch_dir(deps))["ingested"] == 1


async def test_ingest_watch_reopens_orphaned_duplicate(session_factory, tmp_path, monkeypatch):
    """duplicate 指向的资产不可用后重开：仍有别的可用副本则再判 duplicate，否则正常入库。"""
    from sqlalchemy import update

    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import Asset, IngestFileStatus, Space
    from loomvec.worker import ingest_watch

    deps, sent, _, _ = _harness(session_factory, tmp_path, monkeypatch)
    paper = _pdf("paper")
    async with session_factory() as session:
        target = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
        sibling = _space(session, "sibling", target.tenant_id, kind="public")
        cousin = _space(session, "cousin", target.tenant_id, kind="public")
        await session.flush()
        await _publish(session, sibling)
        await _publish(session, cousin)
        first = _asset(
            session, sibling, checksum=_sha(paper), created_at=datetime.now() - timedelta(days=1)
        )
        second = _asset(session, cousin, checksum=_sha(paper))
        await session.commit()

    (tmp_path / "paper.pdf").write_bytes(paper)
    await ingest_watch.scan_watch_dir(deps)
    await ingest_watch.scan_watch_dir(deps)
    assert (await _ledger(session_factory))["paper.pdf"].asset_id == first.id

    async def soft_delete(asset_id):
        async with session_factory() as session:
            await session.execute(
                update(Asset).where(Asset.id == asset_id).values(deleted_at=datetime.now(UTC))
            )
            await session.commit()

    # 指向的副本被删 → 重开后改指向另一份可用副本
    await soft_delete(first.id)
    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["reopened"], result["duplicate"]) == (1, 1)
    row = (await _ledger(session_factory))["paper.pdf"]
    assert (row.status, row.asset_id) == (IngestFileStatus.DUPLICATE, second.id)

    # 最后一份也被删 → 重开后正常入库到目标空间
    await soft_delete(second.id)
    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["reopened"], result["ingested"]) == (1, 1)
    assert (await _ledger(session_factory))["paper.pdf"].status == IngestFileStatus.DONE
    assert len(sent) == 1

    # 监听自己入库的资产被人删除 = 有意删除：done 行不重开；同目录副本、mtime 变化后
    # 重判出的 duplicate 行指向它时也不重开
    own = (await _ledger(session_factory))["paper.pdf"].asset_id
    shutil.copyfile(tmp_path / "paper.pdf", tmp_path / "paper(1).pdf")
    st = (tmp_path / "paper.pdf").stat()
    os.utime(tmp_path / "paper.pdf", (st.st_atime, st.st_mtime + 10))
    await ingest_watch.scan_watch_dir(deps)
    await ingest_watch.scan_watch_dir(deps)
    rows = await _ledger(session_factory)
    for name in ("paper.pdf", "paper(1).pdf"):
        assert (rows[name].status, rows[name].asset_id) == (IngestFileStatus.DUPLICATE, own)
    await soft_delete(own)
    for _ in range(2):
        result = await ingest_watch.scan_watch_dir(deps)
        assert "reopened" not in result and "ingested" not in result
    assert len(sent) == 1


async def test_ingest_watch_reopens_duplicate_outside_scope(session_factory, tmp_path, monkeypatch):
    """duplicate 指向不在去重范围内的资产（他人协作空间，如按旧规则判的或改过 space_id）：
    重开后按当前范围重判，正常入库到目标空间。"""
    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import IngestFile, IngestFileStatus, Space
    from loomvec.worker import ingest_watch

    deps, sent, _, _ = _harness(session_factory, tmp_path, monkeypatch)
    paper = _pdf("paper")
    path = tmp_path / "paper.pdf"
    path.write_bytes(paper)
    st = path.stat()
    async with session_factory() as session:
        target = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
        private = _space(session, "private", target.tenant_id)
        await session.flush()
        elsewhere = _asset(session, private, checksum=_sha(paper))
        await session.flush()
        session.add(
            IngestFile(
                tenant_id=target.tenant_id,
                watch_dir=os.path.normpath(str(tmp_path)),
                file_path=str(path),
                file_size=st.st_size,
                file_mtime=st.st_mtime,
                checksum=_sha(paper),
                status=IngestFileStatus.DUPLICATE.value,
                asset_id=elsewhere.id,
                stable_since=datetime.now(UTC),
                last_checked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["reopened"], result["ingested"]) == (1, 1)
    row = (await _ledger(session_factory))["paper.pdf"]
    assert row.status == IngestFileStatus.DONE and row.asset_id != elsewhere.id
    assert len(sent) == 1


async def test_ingest_watch_reopens_when_public_space_unpublished(
    session_factory, tmp_path, monkeypatch
):
    """公共空间撤销发布后不再算大家看得到：指向它的 duplicate 行重开并入库到目标空间。"""
    from sqlalchemy import delete

    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.db.models import IngestFileStatus, Space, SpaceGroupVisibility
    from loomvec.worker import ingest_watch

    deps, sent, _, _ = _harness(session_factory, tmp_path, monkeypatch)
    paper = _pdf("paper")
    async with session_factory() as session:
        target = await session.get(Space, uuid.UUID(DEFAULT_SPACE_ID))
        public = _space(session, "public", target.tenant_id, kind="public")
        await session.flush()
        visibility = await _publish(session, public)
        published = _asset(session, public, checksum=_sha(paper))
        await session.commit()

    (tmp_path / "paper.pdf").write_bytes(paper)
    await ingest_watch.scan_watch_dir(deps)
    assert (await ingest_watch.scan_watch_dir(deps))["duplicate"] == 1
    assert (await _ledger(session_factory))["paper.pdf"].asset_id == published.id

    async with session_factory() as session:
        await session.execute(
            delete(SpaceGroupVisibility).where(SpaceGroupVisibility.id == visibility.id)
        )
        await session.commit()
    result = await ingest_watch.scan_watch_dir(deps)
    assert (result["reopened"], result["ingested"]) == (1, 1)
    assert (await _ledger(session_factory))["paper.pdf"].status == IngestFileStatus.DONE
    assert len(sent) == 1


async def test_ingest_watch_space_id_change(session_factory, tmp_path, monkeypatch):
    """改 space_id 后：已入库的文件不迁移；文件变化后按新空间去重，旧空间（非公共）里的不算。"""
    from loomvec.core.db.models import Asset, IngestFileStatus
    from loomvec.worker import ingest_watch

    deps, sent, _, _ = _harness(session_factory, tmp_path, monkeypatch)
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(_pdf("paper"))
    await ingest_watch.scan_watch_dir(deps)
    await ingest_watch.scan_watch_dir(deps)
    old_asset = (await _ledger(session_factory))["paper.pdf"].asset_id

    async with session_factory() as session:
        old = await session.get(Asset, old_asset)
        old_space_id = old.space_id
        new_space = _space(session, "new-target", old.tenant_id)  # 同租户协作空间
        await session.commit()
        new_space_id = new_space.id
    deps.settings.ingest_watch.space_id = str(new_space_id)

    # 文件不变：不迁移、不重新入库
    result = await ingest_watch.scan_watch_dir(deps)
    assert "ingested" not in result and len(sent) == 1

    # 文件变化（只改 mtime）：旧空间不在新空间的去重范围内 → 入库到新空间
    st = paper.stat()
    os.utime(paper, (st.st_atime, st.st_mtime + 10))
    await ingest_watch.scan_watch_dir(deps)
    assert (await ingest_watch.scan_watch_dir(deps))["ingested"] == 1
    row = (await _ledger(session_factory))["paper.pdf"]
    async with session_factory() as session:
        new_asset = await session.get(Asset, row.asset_id)
    assert row.status == IngestFileStatus.DONE
    assert new_asset.space_id == new_space_id != old_space_id
