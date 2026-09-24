"""MinerU 官方 API 兼容层（/api/v4，mineru.net「精准解析 API」同构）。

背景：解析能力由自建 MinerU（mineru-api）提供，此前仅服务摄取管线；
本模块把同一能力以 mineru.net 官方 API 兼容的 HTTP 面向第三方开放——
既有 MinerU 集成把 base_url 从 https://mineru.net 换成 loomvec、token 换成
loomvec API Key（或平台 Bearer JWT）即可无缝迁移，请求/响应信封
（{code, data, msg, trace_id}）与状态机与官方一致。

兼容范围（官方文档 https://mineru.net/apiManage/docs，2026-09 核对）：
- POST /api/v4/extract/task                单文件 URL 解析（异步）
- GET  /api/v4/extract/task/{task_id}      任务状态/结果轮询
- POST /api/v4/file-urls/batch             批量文件上传解析申请（返回直传 URL）
- PUT  /api/v4/file-urls/batch/{token}     文件直传（URL 即凭证，对应官方预签名 PUT）
- POST /api/v4/extract/task/batch          URL 批量解析
- GET  /api/v4/extract-results/batch/{id}  批量结果轮询
- GET  /api/v4/extract-results/file/{tok}  解析产物 zip 下载（full_zip_url 指向）

语义映射：
- 鉴权：官方 `Authorization: Bearer <token>` → Bearer 直带 loomvec API Key；
  X-API-Key（loomvec 惯例）与平台 JWT 同样接受。多租户隔离沿用 identity 体系。
- 状态机：waiting-file / pending / running / done / failed（与官方一致）；
  内部 mineru-api 任务状态 pending/processing/completed/failed 一一映射。
- 产物：内部任务以 response_format_zip=true 提交，结果端点流式转发完整 zip
  （markdown + content_list + images），对齐官方 full_zip_url。
- 状态存 Redis（mineru_compat: 前缀，批次 72h / 上传下载令牌 24h）；
  上传文件落对象存储 derived 桶，内部任务提交成功后即删除。
- 轮询为惰性拉取：客户端查询时才向内部 mineru-api 查一次实时状态，
  无后台轮询进程。

与官方的差异（文档「差异与局限」节）：
- model_version 默认 pipeline（官方默认 vlm）；vlm 在 pipeline-only 部署上
  降级为配置后端（服务端告警日志），MinerU-HTML（云端专属）不支持；
- page_range 仅支持 'N' / 'N-M'（官方逗号多段语法依赖云端分片）；
- Agent 轻量解析 API（免鉴权）不提供——企业平台解析入口必须鉴权。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.auth import bearer_scheme, get_identity_provider
from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_redis,
    get_session,
    get_storage,
    scopes_for_roles,
    service_settings,
)
from loomvec.api.identity import identity_from_api_key, resolve_user_row
from loomvec.core.config import Settings
from loomvec.core.errors import (
    PermissionDeniedError,
    RateLimitedError,
    UnauthenticatedError,
)
from loomvec.core.logging import get_logger
from loomvec.core.mineru_client import MineruClient
from loomvec.core.storage import ObjectStorage

logger = get_logger("loomvec.api.mineru_compat")

router = APIRouter(prefix="/api/v4", tags=["mineru-compat"], include_in_schema=False)

# ---- 官方限额 / 时效 ----
_MAX_BATCH_FILES = 50
_MAX_FILE_BYTES = 200 * 1024 * 1024
_RECORD_TTL_SECONDS = 72 * 3600  # 任务/批次记录保留 72h（官方解析结果保留 3 天）
_TOKEN_TTL_SECONDS = 24 * 3600  # 上传直传 URL / 下载链接 24h（同官方）

# ---- 与官方一致的状态机 ----
_STATE_WAITING_FILE = "waiting-file"
_STATE_PENDING = "pending"
_STATE_RUNNING = "running"
_STATE_DONE = "done"
_STATE_FAILED = "failed"

# 内部 mineru-api 任务状态 → 官方状态
_INTERNAL_STATE_MAP = {
    "pending": _STATE_PENDING,
    "processing": _STATE_RUNNING,
    "completed": _STATE_DONE,
    "failed": _STATE_FAILED,
}


class MineruCompatError(Exception):
    """兼容层错误：以官方信封 {code, msg, trace_id} 返回（code 非 0）。"""

    def __init__(self, status_code: int, msg: str, code: int = -1):
        super().__init__(msg)
        self.status_code = status_code
        self.msg = msg
        self.code = code


def register_mineru_compat_exception_handler(app: Any) -> None:
    @app.exception_handler(MineruCompatError)
    async def _handle_mineru_compat_error(request: Request, exc: MineruCompatError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "msg": exc.msg, "trace_id": uuid.uuid4().hex},
        )


# ---------------------------------------------------------------------------
# 依赖：鉴权（官方 Bearer 直带 loomvec API Key）与内部 MinerU 句柄
# ---------------------------------------------------------------------------


async def get_compat_identity(
    request: Request,
    credentials=Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
    provider=Depends(get_identity_provider),
) -> Identity:
    """官方客户端把 loomvec API Key 放在 `Authorization: Bearer` 中发送
    （对齐 mineru.net 用法）；loomvec 惯例的 X-API-Key 与平台 JWT 同样接受。
    """
    try:
        if x_api_key:
            identity = await identity_from_api_key(x_api_key, session, redis)
        elif credentials is not None:
            token = credentials.credentials
            if token.count(".") == 2:
                # 平台 JWT（dev/SSO 登录签发）：与 deps.get_identity 同构
                identity = await provider.exchange({"token": token})
                identity = await resolve_user_row(session, identity)
                identity = Identity(
                    user_id=identity.user_id,
                    username=identity.username,
                    tenant_id=identity.tenant_id,
                    roles=identity.roles,
                    scopes=scopes_for_roles(identity.roles),
                )
            else:
                identity = await identity_from_api_key(token, session, redis)
        else:
            raise UnauthenticatedError()
    except UnauthenticatedError as e:
        # 精确区分"未携带凭证"与"凭证无效"：客户端（如 InkCop）日志里直接可见
        # 该带什么凭证。注意 UnauthenticatedError 的 reason 落在 details 而非 message。
        if x_api_key is None and credentials is None:
            msg = (
                "未认证：请在 Authorization: Bearer 头中携带 loomvec API Key（或使用 X-API-Key 头）"
            )
        else:
            reason = str(e.details.get("reason") or e.message)
            msg = f"凭证无效或已过期：{reason}"
        raise MineruCompatError(401, msg) from e
    except RateLimitedError as e:
        raise MineruCompatError(429, str(e)) from e
    except PermissionDeniedError as e:
        raise MineruCompatError(403, str(e)) from e
    request.state.identity = identity
    if "read" not in identity.scopes:
        raise MineruCompatError(403, "当前身份缺少 read 权限，无法使用 MinerU 解析 API")
    return identity


def get_mineru(request: Request) -> MineruClient:
    return MineruClient(request.app.state.settings.mineru)


# ---------------------------------------------------------------------------
# 状态存取（Redis JSON）与通用小工具
# ---------------------------------------------------------------------------


def _task_key(task_id: str) -> str:
    return f"mineru_compat:task:{task_id}"


def _batch_key(batch_id: str) -> str:
    return f"mineru_compat:batch:{batch_id}"


def _upload_key(token: str) -> str:
    return f"mineru_compat:upload:{token}"


def _download_key(token: str) -> str:
    return f"mineru_compat:download:{token}"


async def _load(redis: Any, key: str) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _save(redis: Any, key: str, data: dict[str, Any], ttl: int = _RECORD_TTL_SECONDS) -> None:
    await redis.set(key, json.dumps(data, ensure_ascii=False), ex=ttl)


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": data, "msg": "ok", "trace_id": uuid.uuid4().hex}


def _tenant_ok(identity: Identity, record: dict[str, Any]) -> bool:
    """租户隔离：记录归属租户与当前身份不一致 → 视为不存在（不泄露存在性）。"""
    tid = record.get("tenant_id")
    return tid is None or identity.tenant_id is None or tid == identity.tenant_id


def _external_base(request: Request) -> str:
    """file_urls / full_zip_url 需为客户端可达的绝对地址（经反代时读转发头）。"""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return str(request.base_url).rstrip("/")
    return f"{proto}://{host}"


def _name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return name or "document"


def _resolve_backend(model_version: str | None, settings: Settings) -> str:
    mv = (model_version or "pipeline").strip() or "pipeline"
    if mv == "pipeline":
        return "pipeline"
    if mv == "vlm":
        configured = settings.mineru.backend
        if configured.startswith("vlm"):
            return configured
        # 官方云端 vlm 恒可用，第三方客户端普遍携带该默认值（如 InkCop 存量配置）；
        # 自建 pipeline-only 部署选择降级为配置后端而非 400，保证兼容面开箱可用。
        logger.warning("mineru_compat_vlm_degraded", model_version=mv, resolved_backend=configured)
        return configured
    raise MineruCompatError(
        400, f"不支持的 model_version: {mv}（MinerU-HTML 为云端专属，自建 MinerU 不支持）"
    )


def _parse_page_range(page_range: str | None) -> tuple[int, int] | None:
    if not page_range:
        return None
    s = page_range.strip()
    if re.fullmatch(r"\d+", s):
        n = int(s)
        return n, n
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", s)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            raise MineruCompatError(400, f"page_range 起止倒置：{page_range}")
        return start, end
    raise MineruCompatError(
        400, f"page_range 仅支持 'N' 或 'N-M'（官方逗号多段语法自建 MinerU 不支持）：{page_range}"
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# 后台任务强引用集（防 GC）；drain_background_tasks 供测试等待收尾。
_background_tasks: set[asyncio.Task[None]] = set()


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def drain_background_tasks() -> None:
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


# ---------------------------------------------------------------------------
# 后台协程：URL 下载 → 内部任务提交；上传文件 → 内部任务提交
# ---------------------------------------------------------------------------


async def _download_url(url: str) -> tuple[bytes, str, str]:
    """下载 URL 指向的文档，返回 (data, file_name, mime)；限额同官方 200MB。"""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True, trust_env=False
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            mime = resp.headers.get("content-type", "application/octet-stream")
    except httpx.HTTPError as e:
        raise MineruCompatError(400, f"下载解析源失败：{type(e).__name__}: {e}") from e
    if len(data) > _MAX_FILE_BYTES:
        raise MineruCompatError(400, f"文件超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB 上限")
    mime = mime.split(";")[0].strip() or "application/octet-stream"
    return data, _name_from_url(url), mime


async def _submit_to_mineru(
    mineru: MineruClient,
    *,
    file_name: str,
    data: bytes,
    mime: str,
    backend: str,
    is_ocr: bool,
    language: str | None,
    enable_formula: bool,
    enable_table: bool,
    page_range: tuple[int, int] | None,
) -> str:
    return await mineru.submit_task(
        file_name,
        data,
        mime,
        backend=backend,
        parse_method="ocr" if is_ocr else None,
        language=language,
        formula_enable=enable_formula,
        table_enable=enable_table,
        start_page_id=page_range[0] if page_range else None,
        end_page_id=page_range[1] if page_range else None,
    )


async def _bg_url_file(
    key: str,
    index: int | None,
    url: str,
    *,
    backend: str,
    is_ocr: bool,
    language: str | None,
    enable_formula: bool,
    enable_table: bool,
    page_range: tuple[int, int] | None,
    redis: Any,
    mineru: MineruClient,
) -> None:
    """下载单个 URL 并提交内部任务（单任务与 URL 批量的公共路径）。"""
    try:
        data, name, mime = await _download_url(url)
        internal_id = await _submit_to_mineru(
            mineru,
            file_name=name,
            data=data,
            mime=mime,
            backend=backend,
            is_ocr=is_ocr,
            language=language,
            enable_formula=enable_formula,
            enable_table=enable_table,
            page_range=page_range,
        )
        record = (await _load(redis, key)) or {}
        if index is None:
            record.update(
                {"internal_task_id": internal_id, "file_name": name, "state": _STATE_PENDING}
            )
        else:
            files = record.get("files") or []
            if index < len(files):
                files[index].update(
                    {
                        "internal_task_id": internal_id,
                        "file_name": name,
                        "state": _STATE_PENDING,
                    }
                )
        await _save(redis, key, record)
    except MineruCompatError as e:
        logger.warning("mineru_compat_bg_failed", key=key, index=index, error=e.msg)
        await _mark_failed(redis, key, index, e.msg)
    except Exception as e:  # 后台兜底：任何异常都必须落为可见的 failed 状态
        logger.exception("mineru_compat_bg_crashed", key=key, index=index)
        await _mark_failed(redis, key, index, f"内部错误：{type(e).__name__}: {e}")


async def _bg_upload_file(
    batch_id: str,
    index: int,
    redis: Any,
    storage: ObjectStorage,
    settings: Settings,
    mineru: MineruClient,
) -> None:
    key = _batch_key(batch_id)
    try:
        batch = await _load(redis, key)
        if batch is None or index >= len(batch.get("files", [])):
            return
        file = batch["files"][index]
        data = await storage.get_object(settings.storage.bucket_derived, file["storage_key"])
        mime = mimetypes.guess_type(file["file_name"])[0] or "application/octet-stream"
        internal_id = await _submit_to_mineru(
            mineru,
            file_name=file["file_name"],
            data=data,
            mime=mime,
            backend=batch["backend"],
            is_ocr=bool(file.get("is_ocr")),
            language=batch.get("language"),
            enable_formula=bool(batch.get("enable_formula", True)),
            enable_table=bool(batch.get("enable_table", True)),
            page_range=None,
        )
        file["internal_task_id"] = internal_id
        file["state"] = _STATE_PENDING
        await _save(redis, key, batch)
        # 内部任务已持有文件副本，存储中转件即时清理（失败时保留便于排查）
        try:
            await storage.delete_object(settings.storage.bucket_derived, file["storage_key"])
        except Exception:
            logger.warning("mineru_compat_cleanup_failed", batch_id=batch_id, index=index)
    except MineruCompatError as e:
        logger.warning("mineru_compat_bg_failed", batch_id=batch_id, index=index, error=e.msg)
        await _mark_failed(redis, key, index, e.msg)
    except Exception as e:
        logger.exception("mineru_compat_bg_crashed", batch_id=batch_id, index=index)
        await _mark_failed(redis, key, index, f"内部错误：{type(e).__name__}: {e}")


async def _mark_failed(redis: Any, key: str, index: int | None, msg: str) -> None:
    """把单任务（index=None）或批次内单文件标记为 failed（尽力而为）。"""
    record = await _load(redis, key)
    if record is None:
        return
    if index is None:
        record["state"] = _STATE_FAILED
        record["err_msg"] = msg
    else:
        files = record.get("files") or []
        if index < len(files):
            files[index]["state"] = _STATE_FAILED
            files[index]["err_msg"] = msg
    await _save(redis, key, record)


# ---------------------------------------------------------------------------
# 惰性轮询：客户端查询时向内部 mineru-api 拉一次实时状态
# ---------------------------------------------------------------------------


async def _refresh_entry(entry: dict[str, Any], mineru: MineruClient) -> None:
    """刷新单条任务记录/批次文件条目到最新状态（轮询失败保持现状态不误判）。"""
    internal_id = entry.get("internal_task_id")
    if not internal_id or entry.get("state") in (_STATE_DONE, _STATE_FAILED):
        return
    try:
        status = await mineru.get_task(internal_id)
    except Exception as e:
        logger.warning("mineru_compat_poll_failed", internal_task_id=internal_id, error=str(e))
        return
    mapped = _INTERNAL_STATE_MAP.get(str(status.get("status")))
    if mapped is None:
        return
    entry["state"] = mapped
    if mapped == _STATE_FAILED:
        entry["err_msg"] = str(status.get("error") or "解析失败")


async def _ensure_download_url(
    request: Request,
    entry: dict[str, Any],
    redis: Any,
) -> str:
    """done 条目一次性签发下载令牌（对齐官方 full_zip_url：链接本身即凭证）。"""
    token = entry.get("download_token")
    if not token:
        token = secrets.token_urlsafe(24)
        entry["download_token"] = token
        await _save(
            redis,
            _download_key(token),
            {
                "internal_task_id": entry.get("internal_task_id"),
                "file_name": entry.get("file_name") or "result",
            },
            ttl=_TOKEN_TTL_SECONDS,
        )
    return f"{_external_base(request)}/api/v4/extract-results/file/{token}"


# ---------------------------------------------------------------------------
# 请求模型（字段与官方文档对齐）
# ---------------------------------------------------------------------------


class ExtractTaskRequest(BaseModel):
    url: str
    model_version: str = "pipeline"
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    language: str | None = None
    page_range: str | None = None


class BatchUploadFile(BaseModel):
    name: str
    is_ocr: bool = False
    data_id: str | None = None


class BatchUploadRequest(BaseModel):
    model_version: str = "pipeline"
    language: str | None = None
    enable_formula: bool = True
    enable_table: bool = True
    files: list[BatchUploadFile]


class UrlBatchFile(BaseModel):
    url: str
    data_id: str | None = None
    is_ocr: bool = False


class UrlBatchRequest(BaseModel):
    model_version: str = "pipeline"
    language: str | None = None
    enable_formula: bool = True
    enable_table: bool = True
    files: list[UrlBatchFile]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post("/extract/task")
async def create_extract_task(
    body: ExtractTaskRequest,
    request: Request,
    identity: Identity = Depends(get_compat_identity),
    redis=Depends(get_redis),
    settings: Settings = Depends(service_settings),
    mineru: MineruClient = Depends(get_mineru),
):
    """单文件 URL 解析（官方 POST /api/v4/extract/task）。"""
    if not body.url.lower().startswith(("http://", "https://")):
        raise MineruCompatError(400, "url 必须为 http(s) 地址")
    backend = _resolve_backend(body.model_version, settings)
    page_range = _parse_page_range(body.page_range)

    task_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "kind": "url",
        "tenant_id": identity.tenant_id,
        "created_by": identity.user_id,
        "state": _STATE_PENDING,
        "err_msg": "",
        "internal_task_id": None,
        "download_token": None,
        "file_name": _name_from_url(body.url),
        "model_version": (body.model_version or "pipeline").strip() or "pipeline",
        "created_at": _now_iso(),
    }
    await _save(redis, _task_key(task_id), record)
    _spawn(
        _bg_url_file(
            _task_key(task_id),
            None,
            body.url,
            backend=backend,
            is_ocr=body.is_ocr,
            language=body.language,
            enable_formula=body.enable_formula,
            enable_table=body.enable_table,
            page_range=page_range,
            redis=redis,
            mineru=mineru,
        )
    )
    return _ok({"task_id": task_id})


@router.get("/extract/task/{task_id}")
async def get_extract_task(
    task_id: str,
    request: Request,
    identity: Identity = Depends(get_compat_identity),
    redis=Depends(get_redis),
    mineru: MineruClient = Depends(get_mineru),
):
    record = await _load(redis, _task_key(task_id))
    if record is None or not _tenant_ok(identity, record):
        raise MineruCompatError(404, "任务不存在或已过期")
    await _refresh_entry(record, mineru)
    data: dict[str, Any] = {
        "task_id": task_id,
        "state": record["state"],
        "err_msg": record.get("err_msg", ""),
        "model_version": record.get("model_version", "pipeline"),
    }
    if record["state"] == _STATE_DONE:
        data["full_zip_url"] = await _ensure_download_url(request, record, redis)
    await _save(redis, _task_key(task_id), record)
    return _ok(data)


@router.post("/file-urls/batch")
async def create_batch_upload(
    body: BatchUploadRequest,
    request: Request,
    identity: Identity = Depends(get_compat_identity),
    redis=Depends(get_redis),
    settings: Settings = Depends(service_settings),
):
    """批量文件上传解析申请（官方 POST /api/v4/file-urls/batch）。

    返回的 file_urls 为 loomvec 侧直传地址（URL 即凭证，24h 有效）；
    客户端逐个 PUT 文件字节后自动提交解析，无需额外触发调用。
    """
    if not body.files or len(body.files) > _MAX_BATCH_FILES:
        raise MineruCompatError(400, f"files 数量需为 1~{_MAX_BATCH_FILES}")
    backend = _resolve_backend(body.model_version, settings)

    batch_id = uuid.uuid4().hex
    file_urls: list[str] = []
    files: list[dict[str, Any]] = []
    for i, spec in enumerate(body.files):
        name = spec.name.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not name:
            raise MineruCompatError(400, f"files[{i}].name 不能为空")
        upload_token = secrets.token_urlsafe(24)
        await _save(
            redis,
            _upload_key(upload_token),
            {"batch_id": batch_id, "index": i},
            ttl=_TOKEN_TTL_SECONDS,
        )
        file_urls.append(f"{_external_base(request)}/api/v4/file-urls/batch/{upload_token}")
        files.append(
            {
                "file_name": name,
                "data_id": spec.data_id,
                "is_ocr": spec.is_ocr,
                "state": _STATE_WAITING_FILE,
                "err_msg": "",
                "internal_task_id": None,
                "download_token": None,
                "storage_key": f"mineru-compat/{batch_id}/{i}",
                "size": None,
            }
        )

    record: dict[str, Any] = {
        "kind": "upload",
        "tenant_id": identity.tenant_id,
        "created_by": identity.user_id,
        "backend": backend,
        "model_version": (body.model_version or "pipeline").strip() or "pipeline",
        "language": body.language,
        "enable_formula": body.enable_formula,
        "enable_table": body.enable_table,
        "files": files,
        "created_at": _now_iso(),
    }
    await _save(redis, _batch_key(batch_id), record)
    return _ok({"batch_id": batch_id, "file_urls": file_urls})


@router.put("/file-urls/batch/{upload_token}")
async def upload_batch_file(
    upload_token: str,
    request: Request,
    redis=Depends(get_redis),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(service_settings),
    mineru: MineruClient = Depends(get_mineru),
):
    """文件直传（官方语义：对返回的预签名 URL 做 PUT，携带原始文件字节，无请求头鉴权）。"""
    up = await _load(redis, _upload_key(upload_token))
    if up is None:
        raise MineruCompatError(404, "上传链接无效或已过期")
    batch = await _load(redis, _batch_key(up["batch_id"]))
    if batch is None:
        raise MineruCompatError(404, "批次不存在或已过期")
    index = int(up["index"])
    file = batch["files"][index]

    body = await request.body()
    if not body:
        raise MineruCompatError(400, "请求体为空，请携带文件字节")
    if len(body) > _MAX_FILE_BYTES:
        raise MineruCompatError(413, f"文件超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB 上限")

    # 并发/重复直传防御（官方预签名 URL 对同一对象同样只接受一次覆盖语义）；
    # 校验通过后再占用令牌，失败（4xx）不锁死，客户端可修正后重传。
    claimed = await redis.set(
        f"mineru_compat:uploading:{upload_token}", "1", nx=True, ex=_TOKEN_TTL_SECONDS
    )
    if not claimed or file["state"] != _STATE_WAITING_FILE:
        raise MineruCompatError(409, "该文件已上传，请勿重复上传")

    await storage.put_object(
        settings.storage.bucket_derived,
        file["storage_key"],
        body,
        content_type=request.headers.get("content-type"),
    )
    file["state"] = _STATE_PENDING
    file["size"] = len(body)
    await _save(redis, _batch_key(up["batch_id"]), batch)
    # 官方语义：上传完成后自动提交解析（按文件粒度，无需等待整批）
    _spawn(_bg_upload_file(up["batch_id"], index, redis, storage, settings, mineru))
    return Response(status_code=200)


@router.post("/extract/task/batch")
async def create_url_batch(
    body: UrlBatchRequest,
    request: Request,
    identity: Identity = Depends(get_compat_identity),
    redis=Depends(get_redis),
    settings: Settings = Depends(service_settings),
    mineru: MineruClient = Depends(get_mineru),
):
    """URL 批量解析（官方 POST /api/v4/extract/task/batch）。"""
    if not body.files or len(body.files) > _MAX_BATCH_FILES:
        raise MineruCompatError(400, f"files 数量需为 1~{_MAX_BATCH_FILES}")
    backend = _resolve_backend(body.model_version, settings)
    for i, spec in enumerate(body.files):
        if not spec.url.lower().startswith(("http://", "https://")):
            raise MineruCompatError(400, f"files[{i}].url 必须为 http(s) 地址")

    batch_id = uuid.uuid4().hex
    files = [
        {
            "file_name": _name_from_url(spec.url),
            "data_id": spec.data_id,
            "is_ocr": spec.is_ocr,
            "state": _STATE_PENDING,
            "err_msg": "",
            "internal_task_id": None,
            "download_token": None,
            "source_url": spec.url,
        }
        for spec in body.files
    ]
    record: dict[str, Any] = {
        "kind": "url",
        "tenant_id": identity.tenant_id,
        "created_by": identity.user_id,
        "backend": backend,
        "model_version": (body.model_version or "pipeline").strip() or "pipeline",
        "language": body.language,
        "enable_formula": body.enable_formula,
        "enable_table": body.enable_table,
        "files": files,
        "created_at": _now_iso(),
    }
    await _save(redis, _batch_key(batch_id), record)
    for i, spec in enumerate(body.files):
        _spawn(
            _bg_url_file(
                _batch_key(batch_id),
                i,
                spec.url,
                backend=backend,
                is_ocr=spec.is_ocr,
                language=body.language,
                enable_formula=body.enable_formula,
                enable_table=body.enable_table,
                page_range=None,
                redis=redis,
                mineru=mineru,
            )
        )
    return _ok({"batch_id": batch_id})


@router.get("/extract-results/batch/{batch_id}")
async def get_batch_results(
    batch_id: str,
    request: Request,
    identity: Identity = Depends(get_compat_identity),
    redis=Depends(get_redis),
    mineru: MineruClient = Depends(get_mineru),
):
    batch = await _load(redis, _batch_key(batch_id))
    if batch is None or not _tenant_ok(identity, batch):
        raise MineruCompatError(404, "批次不存在或已过期")

    await asyncio.gather(
        *(_refresh_entry(f, mineru) for f in batch.get("files", [])),
        return_exceptions=True,
    )
    extract_result: list[dict[str, Any]] = []
    for f in batch.get("files", []):
        entry: dict[str, Any] = {
            "file_name": f["file_name"],
            "state": f["state"],
            "err_msg": f.get("err_msg", ""),
        }
        if f.get("data_id") is not None:
            entry["data_id"] = f["data_id"]
        if f["state"] == _STATE_DONE:
            entry["full_zip_url"] = await _ensure_download_url(request, f, redis)
        extract_result.append(entry)
    await _save(redis, _batch_key(batch_id), batch)
    return _ok({"batch_id": batch_id, "extract_result": extract_result})


@router.get("/extract-results/file/{download_token}")
async def download_result_file(
    download_token: str,
    redis=Depends(get_redis),
    mineru: MineruClient = Depends(get_mineru),
):
    """解析产物 zip 下载（full_zip_url 指向；链接即凭证，对齐官方时效语义）。"""
    rec = await _load(redis, _download_key(download_token))
    if rec is None or not rec.get("internal_task_id"):
        raise MineruCompatError(404, "下载链接无效或已过期")
    try:
        data = await mineru.fetch_result_zip(rec["internal_task_id"])
    except Exception as e:
        raise MineruCompatError(502, f"解析结果暂不可用：{e}") from e
    base_name = (rec.get("file_name") or "result").rsplit(".", 1)[0]
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'},
    )
