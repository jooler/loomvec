"""目录监听自动入库：扫描 ingest_watch.dir（及有限层子目录），把写完整的新文件上传登记并入队管线。

beat 周期任务 `ingest.watch_scan` 调用 scan_watch_dir，每轮（docs/18）：
1. 按层列目录（os.scandir 广度优先，最多向下 max_depth 层，子目录数 / 文件数有上限：
   监听目录可能挂在 NFS 上，无界递归会打满带宽），只看 ingest_watch.extensions 白名单
   后缀的普通文件；跳过隐藏文件 / 目录、NAS 系统目录、软链接（文件和目录都不跟随，防止把
   目录外的文件带进来）和非 UTF-8 文件名；
2. 台账 ingest_file 比对 (size, mtime)：新文件登记 pending_stable；有变化即刷新稳定计时；
   每行在独立保存点中写入，单行异常不拖垮整轮；长期不在目录中的行按保留期清理（本轮列表
   不完整时不清理）；duplicate 行指向的资产已不可用时回到 pending_stable 重新判定；
3. 补偿入队：已登记资产但未入队（uploaded）的行重新入队；
4. 稳定的文件（每轮最多 max_ingest_per_scan 个，其余留到下轮）：目标空间或同租户已发布的
   公共空间（大家都看得到的）有同大小、尚未算出 sha256 的在途资产则本轮不读不传、等它
   算出再判；否则读入（O_NOFOLLOW，不持有数据库事务）、PDF
   头尾校验、算 sha256；上述空间已有可用的同内容资产则记 duplicate（不上传、不入队）；否则
   写对象存储 → register_file_asset（台账 uploaded、预分配的 asset_id 与资产同事务提交）→
   enqueue_pipeline → done；
5. 单个文件失败只把该行记 error，继续处理其它文件；error 行在文件 size/mtime 变化后重新处理。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import stat
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded
from redis.exceptions import LockError
from sqlalchemy import and_, exists, or_, select, text, update

from loomvec.api.context import Identity
from loomvec.api.services import assets as asset_service
from loomvec.core.constants import (
    ASSET_META_SOURCE_KEY,
    ASSET_SOURCE_INGEST_WATCH,
    INGEST_WATCH_LOCK_KEY,
)
from loomvec.core.db.models import (
    Asset,
    AssetStatus,
    IngestFile,
    IngestFileStatus,
    ReviewStatus,
    Space,
    SpaceGroupVisibility,
    SpaceType,
    UserGroup,
)
from loomvec.core.db.repos import AssetRepo
from loomvec.core.errors import ValidationError
from loomvec.core.logging import get_logger
from loomvec.core.pipeline import PipelineDeps
from loomvec.core.pipeline.mime import EXT_TO_MIME
from loomvec.core.storage_keys import raw_upload_key
from loomvec.worker.celery_app import celery_app

logger = get_logger("loomvec.worker.ingest_watch")

# 系统身份：无真实用户。沿用 api 层「user_id 以 apikey: 开头 = 非用户主体」的约定，
# register_file_asset 据此把资产 created_by 置空；资产另带 asset_meta 来源标记，
# 管理权限按 authz.can_manage_asset 的系统入库规则（人类 editor 可管，API Key 不可）
SYSTEM_USER_ID = "apikey:ingest-watch"

# 终态行「最近在目录中见到」时间（last_checked_at）的刷新间隔：控制每轮写入量
_SEEN_REFRESH = timedelta(days=1)
# 文件从目录消失超过该时长即删除台账行（uploaded 行等补偿入队完成后再清）
_MISSING_RETENTION = timedelta(days=7)
# 同大小在途资产（管线尚未写 checksum）的最长等待，自文件稳定起算；超时照常入库
_INFLIGHT_WAIT = timedelta(hours=1)
# 扫描锁 TTL 下限（秒）；每处理完一个文件续期
_LOCK_TTL_MIN_SECONDS = 300
_PDF_PROBE_BYTES = 1024
# 不读文件内容的处理结果：不占每轮 max_ingest_per_scan 配额
_NO_READ_OUTCOMES = frozenset({"awaiting_inflight", "skipped"})
# 不进入的子目录名：NAS（群晖等）的缩略图 / 回收站 / 快照系统目录
_SKIP_DIRS = frozenset({"@eaDir", "#recycle", "#snapshot", "@Recycle", "@Recently-Snapshot"})


@dataclass(frozen=True)
class ScannedFile:
    path: str
    name: str
    size: int
    mtime: float


def _utf8_name(name: str, path: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        logger.warning("ingest_watch_skip_undecodable_name", path=ascii(path))
        return False
    return True


def list_candidates(
    watch_dir: str,
    extensions: list[str],
    *,
    max_depth: int = 0,
    max_dirs: int = 500,
    max_files: int = 20000,
    max_entries: int = 50000,
) -> tuple[list[ScannedFile], bool]:
    """从 watch_dir 起按层（广度优先，同层按名字排序）列出白名单后缀的普通文件，最多向下
    max_depth 层子目录。

    返回 (文件列表, 是否完整)。不跟随软链接（文件和目录都不跟）；隐藏文件 / 目录多为同步
    客户端的临时文件，跳过；NAS 系统目录（_SKIP_DIRS）跳过；非 UTF-8 文件名无法入库（数据库
    与对象存储 key 都要求 UTF-8），跳过。watch_dir 本身不可读时抛 OSError；子目录或目录项
    不可读、子目录数超过 max_dirs、白名单文件数超过 max_files、或累计读到的目录项（含非白名单
    文件与子目录）达到 max_entries 时只列到此为止并告警，返回不完整（调用方据此本轮不清理
    「已消失」的台账行）。max_entries 防止大目录（如解压的数据集）被同步进监听树后每轮整读。
    """
    allowed = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    found: list[ScannedFile] = []
    complete = True
    dir_limit_hit = False
    queue: deque[tuple[str, int]] = deque([(watch_dir, 0)])
    subdirs = 0
    seen_entries = 0
    while queue:
        if seen_entries >= max_entries:  # 截断后不再打开任何目录（空目录也不开）
            logger.warning("ingest_watch_entry_limit", max_entries=max_entries)
            complete = False
            break
        path, depth = queue.popleft()
        entries: list[os.DirEntry] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if seen_entries >= max_entries:
                        complete = False
                        break
                    seen_entries += 1
                    entries.append(entry)
        except OSError as e:
            if depth == 0:
                raise
            logger.warning("ingest_watch_subdir_unavailable", path=ascii(path), error=str(e))
            complete = False
            continue
        entries.sort(key=lambda e: e.name)
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            is_allowed = os.path.splitext(name)[1].lower() in allowed
            try:
                if entry.is_symlink():
                    if is_allowed:
                        logger.warning("ingest_watch_skip_symlink", path=ascii(entry.path))
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as e:  # NFS 不给 d_type 时退化为 lstat，可能 ESTALE 等
                logger.warning(
                    "ingest_watch_entry_unavailable", path=ascii(entry.path), error=str(e)
                )
                complete = False
                continue
            if is_dir:
                if depth >= max_depth or name in _SKIP_DIRS or not _utf8_name(name, entry.path):
                    continue
                if subdirs >= max_dirs:
                    if not dir_limit_hit:
                        logger.warning("ingest_watch_dir_limit", max_dirs=max_dirs)
                    dir_limit_hit = True
                    complete = False
                    continue
                subdirs += 1
                queue.append((entry.path, depth + 1))
                continue
            if not is_allowed:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:  # 列目录与 stat 之间被删除/改名
                continue
            if not _utf8_name(name, entry.path):
                continue
            if len(found) >= max_files:
                logger.warning("ingest_watch_file_limit", max_files=max_files)
                return found, False
            found.append(
                ScannedFile(path=entry.path, name=name, size=st.st_size, mtime=st.st_mtime)
            )
    return found, complete


def _read_regular_file(path: str) -> tuple[bytes, os.stat_result]:
    """读整文件：O_NOFOLLOW 拒绝软链接（防列目录后被替换），非普通文件拒绝；
    返回内容与读后 fstat（供核对读取期间是否仍在写入）。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("不是普通文件")
        data = fh.read()
        return data, os.fstat(fd)


def _check_pdf(data: bytes) -> None:
    """便宜的 PDF 完整性检查：文件头 %PDF-，尾部 %%EOF（缺失多为同步未写完）。"""
    if b"%PDF-" not in data[:_PDF_PROBE_BYTES]:
        raise ValidationError("文件头不是 PDF（缺少 %PDF-）")
    if b"%%EOF" not in data[-_PDF_PROBE_BYTES:]:
        raise ValidationError("PDF 尾部缺少 %%EOF，可能尚未写完整（文件变化后自动重试）")


def _system_identity(space: Space) -> Identity:
    return Identity(
        user_id=SYSTEM_USER_ID,
        username="ingest-watch",
        tenant_id=str(space.tenant_id) if space.tenant_id else None,
        roles=("system",),
        scopes=("write",),
    )


async def scan_watch_dir(deps: PipelineDeps) -> dict:
    """一轮扫描（ingest_watch.dir 为空时 no-op）。

    Redis 锁（redis-py Lock：token 校验的续期/释放均为 Lua 原子操作）保证多 worker 进程
    不并发扫描；TTL = max(5×扫描周期, 300s)，每处理完一个文件续期，进程被杀时最多残留一个 TTL。
    """
    cfg = deps.settings.ingest_watch
    if not cfg.dir:
        return {"status": "disabled"}
    watch_dir = os.path.normpath(cfg.dir)
    ttl = max(5 * cfg.poll_interval_seconds, _LOCK_TTL_MIN_SECONDS)
    lock = deps.redis.lock(INGEST_WATCH_LOCK_KEY, timeout=ttl, thread_local=False)
    if not await lock.acquire(blocking=False):
        return {"status": "locked"}
    try:
        return await _scan(deps, watch_dir, lock)
    finally:
        try:
            await lock.release()
        except LockError:
            logger.warning("ingest_watch_lock_lost", detail="扫描锁已过期或被其他进程持有")


async def _scan(deps: PipelineDeps, watch_dir: str, lock) -> dict:
    cfg = deps.settings.ingest_watch
    async with deps.session_factory() as session:
        space = await _target_space(session, cfg.space_id)
    if space is None:
        logger.warning(
            "ingest_watch_space_missing", detail="ingest_watch.space_id 未配置或空间不存在"
        )
        return {"status": "misconfigured"}
    try:
        files, complete = await asyncio.to_thread(
            functools.partial(
                list_candidates,
                watch_dir,
                cfg.extensions,
                max_depth=cfg.max_depth,
                max_dirs=cfg.max_dirs,
                max_files=cfg.max_files,
                max_entries=cfg.max_entries,
            )
        )
    except OSError as e:
        logger.warning("ingest_watch_dir_unavailable", watch_dir=watch_dir, error=str(e))
        return {"status": "dir_unavailable"}

    reopened = await _reopen_orphaned_duplicates(deps, space, watch_dir)
    stable = await _observe(deps, space, watch_dir, files, prune_missing=complete)
    n_stable = len(stable)
    outcomes: Counter[str] = await _resume_uploaded(deps, watch_dir)
    if reopened:
        outcomes["reopened"] = reopened
    if not complete:
        outcomes["incomplete"] = 1
    # 每轮最多读 max_ingest_per_scan 个文件；不读文件的结果（等在途、已非待处理）不占配额。
    # 超出配额的稳定文件保持 pending_stable，下轮仍判定为稳定、按列目录顺序接着处理
    read_count = 0
    for i, scanned in enumerate(stable):
        if read_count >= cfg.max_ingest_per_scan:
            outcomes["deferred"] = len(stable) - i
            break
        outcome = await _process(deps, space, watch_dir, scanned)
        outcomes[outcome] += 1
        if outcome not in _NO_READ_OUTCOMES:
            read_count += 1
        try:
            await lock.reacquire()
        except LockError:
            logger.warning("ingest_watch_lock_lost", detail="续期失败，停止本轮剩余文件")
            break
    return {"status": "ok", "scanned": len(files), "stable": n_stable, **outcomes}


async def _target_space(session, space_id: str) -> Space | None:
    try:
        space_uuid = uuid.UUID(space_id)
    except ValueError:
        return None
    return (
        await session.execute(
            select(Space).where(Space.id == space_uuid, Space.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def _record(session, watch_dir: str, file_path: str) -> IngestFile | None:
    return (
        await session.execute(
            select(IngestFile).where(
                IngestFile.watch_dir == watch_dir, IngestFile.file_path == file_path
            )
        )
    ).scalar_one_or_none()


def _observe_one(
    session,
    record: IngestFile | None,
    scanned: ScannedFile,
    *,
    space: Space,
    watch_dir: str,
    now: datetime,
    stability: timedelta,
) -> bool:
    """比对单个文件，就地登记/更新台账行；返回本轮是否判定为稳定待处理。"""
    if record is None:
        session.add(
            IngestFile(
                tenant_id=space.tenant_id,
                watch_dir=watch_dir,
                file_path=scanned.path,
                file_size=scanned.size,
                file_mtime=scanned.mtime,
                status=IngestFileStatus.PENDING_STABLE.value,
                stable_since=now,
                last_checked_at=now,
            )
        )
        return False
    if record.status == IngestFileStatus.UPLOADED:
        return False  # 已登记待入队：交给 _resume_uploaded，完成后再按新的 size/mtime 判定
    if (record.file_size, record.file_mtime) != (scanned.size, scanned.mtime):
        # checksum/asset_id 保留：内容未变时据此判 duplicate，不重复入库
        record.file_size = scanned.size
        record.file_mtime = scanned.mtime
        record.status = IngestFileStatus.PENDING_STABLE.value
        record.error_message = None
        record.stable_since = now
        record.last_checked_at = now
        return False
    if record.status == IngestFileStatus.PENDING_STABLE:
        record.last_checked_at = now
        return now - record.stable_since >= stability
    if now - record.last_checked_at >= _SEEN_REFRESH:
        record.last_checked_at = now
    return False


async def _observe(
    deps: PipelineDeps,
    space: Space,
    watch_dir: str,
    files: list[ScannedFile],
    *,
    prune_missing: bool = True,
) -> list[ScannedFile]:
    """台账比对：登记新文件、刷新变化文件的稳定计时、清理长期消失的文件；返回稳定文件。

    每个文件在独立保存点中写入：单行写入失败（如异常文件名）只跳过该文件。
    prune_missing=False（本轮列表不完整：子目录不可读或触及上限）时不清理消失的行，
    免得把只是本轮没列到的文件当成已删除。
    """
    now = datetime.now(UTC)
    stability = timedelta(seconds=deps.settings.ingest_watch.stability_seconds)
    stable: list[ScannedFile] = []
    async with deps.session_factory() as session:
        records = {
            r.file_path: r
            for r in (
                await session.execute(select(IngestFile).where(IngestFile.watch_dir == watch_dir))
            )
            .scalars()
            .all()
        }
        for scanned in files:
            record = records.get(scanned.path)
            try:
                async with session.begin_nested():
                    is_stable = _observe_one(
                        session,
                        record,
                        scanned,
                        space=space,
                        watch_dir=watch_dir,
                        now=now,
                        stability=stability,
                    )
                    await session.flush()
            except Exception as e:
                logger.warning(
                    "ingest_watch_observe_failed", file_path=ascii(scanned.path), error=str(e)
                )
                continue
            if is_stable:
                stable.append(scanned)
        # 只看目录中已不存在的行（本轮未触碰，属性仍已加载）
        present = {f.path for f in files} if prune_missing else set(records)
        for path, record in records.items():
            if (
                path not in present
                and record.status != IngestFileStatus.UPLOADED
                and now - record.last_checked_at > _MISSING_RETENTION
            ):
                await session.delete(record)
        await session.commit()
    return stable


async def _resume_uploaded(deps: PipelineDeps, watch_dir: str) -> Counter[str]:
    """补偿：已登记资产但未入队（入队失败 / 进程在登记与入队之间退出）的行重新入队。"""
    counts: Counter[str] = Counter()
    now = datetime.now(UTC)
    async with deps.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(IngestFile).where(
                        IngestFile.watch_dir == watch_dir,
                        IngestFile.status == IngestFileStatus.UPLOADED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        for record in rows:
            asset = await AssetRepo(session).get_live(record.asset_id) if record.asset_id else None
            if asset is not None and asset.status == AssetStatus.PENDING:
                try:
                    asset_service.enqueue_pipeline(celery_app, str(asset.id))
                except Exception as e:
                    record.error_message = f"入队失败，下轮扫描重试：{type(e).__name__}: {e}"[:2000]
                    record.last_checked_at = now
                    counts["enqueue_failed"] += 1
                    continue
                counts["resumed"] += 1
            # 资产已在处理 / 已删除：无需再入队
            record.status = IngestFileStatus.DONE.value
            record.error_message = None
            record.last_checked_at = now
        await session.commit()
    return counts


async def _process(deps: PipelineDeps, space: Space, watch_dir: str, scanned: ScannedFile) -> str:
    """处理单个稳定文件；任何异常只记该行 error（已登记资产的 uploaded 行保持待补偿），
    不中断本轮其它文件。"""
    try:
        return await _ingest_one(deps, space, watch_dir, scanned)
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:
        message = f"{type(e).__name__}: {e}"[:2000]
        logger.warning("ingest_watch_file_failed", file_path=scanned.path, error=message)
        try:
            async with deps.session_factory() as session:
                record = await _record(session, watch_dir, scanned.path)
                if record is not None:
                    if record.status != IngestFileStatus.UPLOADED:
                        record.status = IngestFileStatus.ERROR.value
                    record.error_message = message
                    record.last_checked_at = datetime.now(UTC)
                    await session.commit()
        except Exception as write_err:
            logger.warning(
                "ingest_watch_error_record_failed", file_path=scanned.path, error=str(write_err)
            )
        return "error"


async def _ingest_one(
    deps: PipelineDeps, space: Space, watch_dir: str, scanned: ScannedFile
) -> str:
    settings = deps.settings
    # 1) 台账状态 + 与用户直传同一套上传策略（后缀/大小）：不合规的文件不读不传
    async with deps.session_factory() as session:
        record = await _record(session, watch_dir, scanned.path)
        if record is None or record.status != IngestFileStatus.PENDING_STABLE:
            return "skipped"
        policy = await asset_service.upload_policy(session, settings)
        ext = asset_service.validate_upload(policy, scanned.name, scanned.size)

        # 2) 同大小在途资产（如人工上传的同一文件还在排队 / MinerU 解析中，checksum 解析完
        #    才写入）：本轮不读不传，保持 pending_stable 下轮重判，避免同内容并行跑两遍管线；
        #    等待期间不读文件，不给 NFS 加负担
        if datetime.now(UTC) - record.stable_since < _INFLIGHT_WAIT and (
            await _has_inflight_same_size(session, space, watch_dir, scanned.size)
        ):
            logger.info("ingest_watch_wait_inflight", file_path=scanned.path, size=scanned.size)
            return "awaiting_inflight"

    # 3) 读文件与校验（线程内，不持有数据库事务）
    data, st = await asyncio.to_thread(_read_regular_file, scanned.path)
    now = datetime.now(UTC)
    if (len(data), st.st_mtime) != (scanned.size, scanned.mtime):
        # 判定稳定后又被写入（同步未完成）：重置稳定计时，下轮再看
        async with deps.session_factory() as session:
            record = await _record(session, watch_dir, scanned.path)
            if record is not None:
                record.file_size = len(data)
                record.file_mtime = st.st_mtime
                record.stable_since = now
                record.last_checked_at = now
                await session.commit()
        return "changed"
    if ext == ".pdf":
        _check_pdf(data)
    checksum = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())

    # 4) 去重
    async with deps.session_factory() as session:
        record = await _record(session, watch_dir, scanned.path)
        if record is None or record.status != IngestFileStatus.PENDING_STABLE:
            return "skipped"
        duplicate = await _find_duplicate(session, space, watch_dir, record, checksum)
        if duplicate is not None:
            duplicate_id, duplicate_space_id = duplicate
            record.checksum = checksum
            record.status = IngestFileStatus.DUPLICATE.value
            record.asset_id = duplicate_id
            record.last_checked_at = now
            await session.commit()
            logger.info(
                "ingest_watch_file_duplicate",
                file_path=scanned.path,
                asset_id=str(duplicate_id),
                space_id=str(duplicate_space_id),
                cross_space=duplicate_space_id != space.id,
            )
            return "duplicate"

    # 5) 上传对象存储（不持有数据库事务）
    key = raw_upload_key(space.id, uuid.uuid4().hex, scanned.name)
    await deps.storage.put_object(
        settings.storage.bucket_raw, key, data, content_type=EXT_TO_MIME.get(ext)
    )

    # 6) 登记：台账 uploaded + 预分配 asset_id 与资产同事务提交（register_file_asset 内 commit），
    #    进程在登记与入队之间退出时由下轮 _resume_uploaded 补偿，不会重复登记
    try:
        async with deps.session_factory() as session:
            record = await _record(session, watch_dir, scanned.path)
            if record is None or record.status != IngestFileStatus.PENDING_STABLE:
                await _discard_object(deps, key)
                return "skipped"
            asset_id = await session.scalar(text("SELECT uuidv7()"))
            record.status = IngestFileStatus.UPLOADED.value
            record.asset_id = asset_id
            record.checksum = checksum
            record.error_message = None
            record.last_checked_at = now
            await asset_service.register_file_asset(
                session,
                identity=_system_identity(space),
                settings=settings,
                storage=deps.storage,
                key=key,
                filename=scanned.name,
                size=len(data),
                content_type=None,
                space=space,
                folder_id=None,
                asset_id=asset_id,
                asset_meta={ASSET_META_SOURCE_KEY: ASSET_SOURCE_INGEST_WATCH},
            )
    except Exception:
        await _discard_object(deps, key)
        raise

    # 7) 入队；失败时保持 uploaded，下轮补偿重试
    try:
        asset_service.enqueue_pipeline(celery_app, str(asset_id))
    except Exception as e:
        async with deps.session_factory() as session:
            record = await _record(session, watch_dir, scanned.path)
            if record is not None:
                record.error_message = f"入队失败，下轮扫描重试：{type(e).__name__}: {e}"[:2000]
                await session.commit()
        logger.warning("ingest_watch_enqueue_failed", asset_id=str(asset_id), error=str(e))
        return "enqueue_failed"
    async with deps.session_factory() as session:
        record = await _record(session, watch_dir, scanned.path)
        if record is not None and record.status == IngestFileStatus.UPLOADED:
            record.status = IngestFileStatus.DONE.value
            await session.commit()
    logger.info("ingest_watch_file_ingested", file_path=scanned.path, asset_id=str(asset_id))
    return "ingested"


def _usable_asset_conditions(space: Space) -> tuple:
    """可作为去重依据的现存资产（需 join Space）。

    大家都看得到的才算已有：位于目标空间，或已发布的公共空间（至少对一个未删除的用户分组
    可见，与 authz 公共空间可见性同口径）；需审核的非目标空间只认审核通过的（分组用户只看得到
    这些）。他人协作 / 个人空间、未发布的公共空间里的副本不挡入库。另须与目标空间同租户
    （tenant_id 可空，空租户之间也按同一租户判）、资产未删除且未失败 / 未被驳回、所属空间
    未删除且未封禁（删空间不软删资产行，须连空间一起判）。
    """
    published = exists().where(
        SpaceGroupVisibility.space_id == Space.id,
        UserGroup.id == SpaceGroupVisibility.group_id,
        UserGroup.deleted_at.is_(None),
    )
    return (
        or_(Space.id == space.id, and_(Space.space_type == SpaceType.PUBLIC, published)),
        or_(
            Space.id == space.id,
            Space.review_required.is_(False),
            Asset.review_status == ReviewStatus.APPROVED,
        ),
        Space.tenant_id.is_not_distinct_from(space.tenant_id),
        Space.deleted_at.is_(None),
        Space.banned_at.is_(None),
        Asset.deleted_at.is_(None),
        Asset.status != AssetStatus.FAILED,
        Asset.review_status.is_distinct_from(ReviewStatus.REJECTED),
    )


async def _find_duplicate(
    session, space: Space, watch_dir: str, record: IngestFile, checksum: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """内容相同的可用资产 (asset_id, space_id)：目标空间或同租户已发布的公共空间，目标空间
    优先、其次最早入库的。

    判定来源：管线已算出相同 sha256 的资产（含人工上传）/ 本路径上次入库的资产 / 同目录
    其它路径入库的资产（刚登记、管线尚未写 checksum 的也能认出）。台账行的 checksum 与
    asset_id 总是成对写入，文件变化回到 pending_stable / 记 error 时两者保留，所以同目录候选
    不看行状态：同一轮里副本排在已变化的原文件之前处理时也能认出原资产。须在改写
    record.checksum 前调用。
    """
    candidates: list[uuid.UUID] = []
    if record.asset_id is not None and record.checksum == checksum:
        candidates.append(record.asset_id)
    candidates.extend(
        (
            await session.execute(
                select(IngestFile.asset_id).where(
                    IngestFile.watch_dir == watch_dir,
                    IngestFile.checksum == checksum,
                    IngestFile.asset_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    same_content = Asset.checksum == checksum
    if candidates:
        same_content = or_(same_content, Asset.id.in_(candidates))
    row = (
        await session.execute(
            select(Asset.id, Asset.space_id)
            .join(Space, Space.id == Asset.space_id)
            .where(same_content, *_usable_asset_conditions(space))
            .order_by((Asset.space_id == space.id).desc(), Asset.created_at.asc())
            .limit(1)
        )
    ).first()
    return (row.id, row.space_id) if row is not None else None


async def _has_inflight_same_size(session, space: Space, watch_dir: str, size: int) -> bool:
    """目标空间或同租户已发布的公共空间里有同大小、管线尚未写入 checksum 的在途资产
    （pending / processing；需审核的非目标空间里的不算，审核通过前也不算已有）。

    本目录登记的资产不算：其 sha256 已记在台账，同内容已由 _find_duplicate 认出。
    """
    ledger_assets = select(IngestFile.asset_id).where(
        IngestFile.watch_dir == watch_dir, IngestFile.asset_id.is_not(None)
    )
    return bool(
        await session.scalar(
            select(
                exists()
                .where(Space.id == Asset.space_id)
                .where(
                    Asset.size_bytes == size,
                    Asset.checksum.is_(None),
                    Asset.status.in_((AssetStatus.PENDING, AssetStatus.PROCESSING)),
                    Asset.id.not_in(ledger_assets),
                    *_usable_asset_conditions(space),
                )
            )
        )
    )


async def _reopen_orphaned_duplicates(deps: PipelineDeps, space: Space, watch_dir: str) -> int:
    """duplicate 行指向的资产已不可用（删除 / 所在空间删除或封禁 / 失败 / 驳回 / 公共空间撤销
    发布 / 既不在目标空间也不在已发布的公共空间，如改了 space_id）：回到 pending_stable
    重新判定——仍有别的可用同内容资产则再记 duplicate，否则正常入库。

    指向监听自己入库的资产时不重开（done 行同理）：它被人删掉视为有意删除，文件不变就不再
    入库。同目录副本、或 done 行因 mtime 变化重判后，duplicate 都可能指向监听自己的资产。
    """
    usable = (
        exists()
        .where(Asset.id == IngestFile.asset_id, Space.id == Asset.space_id)
        .where(*_usable_asset_conditions(space))
    )
    own = exists().where(
        Asset.id == IngestFile.asset_id,
        Asset.asset_meta[ASSET_META_SOURCE_KEY].astext == ASSET_SOURCE_INGEST_WATCH,
    )
    now = datetime.now(UTC)
    async with deps.session_factory() as session:
        result = await session.execute(
            update(IngestFile)
            .where(
                IngestFile.watch_dir == watch_dir,
                IngestFile.status == IngestFileStatus.DUPLICATE.value,
                ~usable,
                ~own,
            )
            .values(status=IngestFileStatus.PENDING_STABLE.value, stable_since=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return result.rowcount


async def _discard_object(deps: PipelineDeps, key: str) -> None:
    """登记失败时清理已上传的原始对象（尽力而为）。"""
    try:
        await deps.storage.delete_object(deps.settings.storage.bucket_raw, key)
    except Exception as e:
        logger.warning("ingest_watch_object_cleanup_failed", key=key, error=str(e))
