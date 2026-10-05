"""目录监听入库台账流转的 DB 级集成测试（testcontainers 临时 PG；对象存储/Redis/Celery 用桩）。

覆盖：首轮登记 pending_stable → 稳定后上传登记并入队（done，资产带来源标记、无上传人）→
上传策略不合规与 PDF 未写完整记 error 且不影响其它文件 → 软链接不入库 → 其它路径同内容判
duplicate → 只改 mtime 判 duplicate → 内容变化作为新资产入库 → 入队失败保持 uploaded、
下轮补偿入队 → 长期消失文件的台账行清理 → 扫描互斥锁。无 Docker 或 testcontainers 时跳过。
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


async def test_ingest_watch_ledger_flow(session_factory, tmp_path, monkeypatch):
    from sqlalchemy import select, update

    from loomvec.api.init_db import DEFAULT_SPACE_ID
    from loomvec.core.authz import is_system_ingested
    from loomvec.core.config import IngestWatchSettings
    from loomvec.core.constants import INGEST_WATCH_LOCK_KEY
    from loomvec.core.db.models import Asset, IngestFile, IngestFileStatus, Space
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
                space_id=DEFAULT_SPACE_ID,
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
