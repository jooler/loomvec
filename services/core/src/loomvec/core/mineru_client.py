"""MinerU HTTP 客户端（mineru-api 4.x V1 接口，同步门面 + 异步任务）。

V1 工作流：创建上传 → PUT 字节 → complete 得 file_id → 提交解析 job →
轮询 → 经 Files API 取产物。输出契约（03 文档 §一）：
Markdown（标题层级）+ content_list（版面块：type/text/page_idx/bbox），
用于行号→页码定位；服务端 structured_content（页分组块）在此拉平为
扁平块列表，管线与存储格式保持不变。

内部任务词汇（pending/processing/completed/failed）由 `get_task` 把 V1
job 状态归一化而来，供 MinerU 官方 API 兼容层消费。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from loomvec.core.config import MineruSettings
from loomvec.core.errors import UpstreamUnavailableError

# job 轮询节奏；大文档解析以分钟计，2s 轮询开销可忽略
_POLL_INTERVAL_SECONDS = 2.0


@dataclass
class MineruResult:
    markdown: str
    content_list: list[dict[str, Any]] = field(default_factory=list)


class MineruClient:
    def __init__(
        self, settings: MineruSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport  # 测试注入（httpx.MockTransport 等），生产恒 None

    # ------------------------------------------------------------------
    # V1 底层操作：uploads → file_id；parse/jobs；files/{id}/content
    # ------------------------------------------------------------------

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, trust_env=False, transport=self._transport)

    @staticmethod
    def _describe(e: httpx.HTTPError) -> str:
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            # V1 错误信封 {"error": {type, code, message}}；尽力提取 message
            detail = f" {resp.text[:500]}"
        return f"{type(e).__name__}: {e}{detail}"

    async def _check(self, resp: httpx.Response, action: str) -> httpx.Response:
        if resp.status_code >= 400:
            raise UpstreamUnavailableError(
                upstream="mineru", reason=f"{action} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp

    async def _upload_file(self, filename: str, data: bytes, mime: str) -> str:
        """创建上传会话并写入字节，返回服务端 file_id。"""
        timeout = httpx.Timeout(min(self._settings.timeout_seconds, 300.0), connect=30.0)
        base = self._settings.base_url.rstrip("/")
        try:
            async with self._client(timeout) as client:
                resp = await client.post(
                    f"{base}/v1/uploads",
                    json={"filename": filename, "bytes": len(data), "mime_type": mime},
                )
                await self._check(resp, "创建上传")
                upload_id = resp.json()["id"]
                resp = await client.put(
                    f"{base}/v1/uploads/{upload_id}/content",
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                await self._check(resp, "上传文件")
                resp = await client.post(f"{base}/v1/uploads/{upload_id}/complete")
                await self._check(resp, "完成上传")
                file_id = (resp.json().get("file") or {}).get("id")
                if not file_id:
                    raise UpstreamUnavailableError(
                        upstream="mineru", reason=f"上传完成响应缺少 file.id：{resp.text[:300]}"
                    )
                return str(file_id)
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    async def _create_job(
        self,
        file_id: str,
        *,
        tier: str | None = None,
        ocr_mode: str = "auto",
        output_formats: list[str],
        page_range: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "files": [
                {
                    "source": {"type": "file_id", "file_id": file_id},
                    **({"page_range": page_range} if page_range else {}),
                }
            ],
            "ocr_mode": ocr_mode,
            "output_formats": output_formats,
        }
        if tier:
            body["tier"] = tier
        timeout = httpx.Timeout(min(self._settings.timeout_seconds, 120.0), connect=30.0)
        try:
            async with self._client(timeout) as client:
                resp = await client.post(
                    f"{self._settings.base_url.rstrip('/')}/v1/parse/jobs", json=body
                )
                await self._check(resp, "提交解析任务")
                job_id = resp.json().get("job_id")
                if not job_id:
                    raise UpstreamUnavailableError(
                        upstream="mineru", reason=f"任务提交响应缺少 job_id：{resp.text[:300]}"
                    )
                return str(job_id)
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    async def _get_job(self, job_id: str) -> dict[str, Any]:
        try:
            async with self._client(httpx.Timeout(30.0, connect=5.0)) as client:
                resp = await client.get(
                    f"{self._settings.base_url.rstrip('/')}/v1/parse/jobs/{job_id}"
                )
                await self._check(resp, "查询任务")
                return resp.json()
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    async def _download_file(self, file_id: str) -> bytes:
        try:
            async with self._client(
                httpx.Timeout(self._settings.timeout_seconds, connect=30.0)
            ) as client:
                resp = await client.get(
                    f"{self._settings.base_url.rstrip('/')}/v1/files/{file_id}/content"
                )
                await self._check(resp, "下载产物")
                return resp.content
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    async def _wait_job(self, job_id: str) -> dict[str, Any]:
        """轮询到终态；V1 无同步接口，同步解析也经此路径。"""
        deadline = asyncio.get_running_loop().time() + self._settings.timeout_seconds
        while True:
            job = await self._get_job(job_id)
            if job.get("status") not in ("queued", "running"):
                return job
            if asyncio.get_running_loop().time() + _POLL_INTERVAL_SECONDS > deadline:
                raise UpstreamUnavailableError(
                    upstream="mineru", reason=f"解析超时（>{self._settings.timeout_seconds:.0f}s）"
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _job_error(job: dict[str, Any]) -> str:
        """取首个失败文件的错误消息；服务端未给原因时返回空串。"""
        for f in job.get("files") or []:
            err = f.get("error") or {}
            if err.get("message"):
                return str(err["message"])
        return ""

    # ------------------------------------------------------------------
    # 同步解析门面（摄取管线）
    # ------------------------------------------------------------------

    async def parse(self, filename: str, data: bytes, mime: str) -> MineruResult:
        file_id = await self._upload_file(filename, data, mime)
        job_id = await self._create_job(
            file_id,
            tier=self._settings.tier,
            output_formats=["markdown", "structured_content"],
        )
        job = await self._wait_job(job_id)
        files = job.get("files") or []
        outputs = (files[0].get("output_files") or {}) if files else {}
        if str(job.get("status")) not in ("completed", "partial") or not outputs.get("markdown"):
            raise UpstreamUnavailableError(
                upstream="mineru",
                reason=f"解析失败：{self._job_error(job) or '服务端未提供原因'}",
            )
        markdown = (await self._download_file(outputs["markdown"]["file_id"])).decode("utf-8")
        content_list: list[dict[str, Any]] = []
        sc_ref = outputs.get("structured_content")
        if sc_ref:
            # 版面是辅助产物：下载/解码失败降级为空，不阻塞 Markdown 主产物
            try:
                content_list = _flatten_structured_content(
                    json.loads((await self._download_file(sc_ref["file_id"])).decode("utf-8"))
                )
            except (UpstreamUnavailableError, json.JSONDecodeError, UnicodeDecodeError):
                content_list = []
        return MineruResult(markdown=markdown, content_list=content_list)

    def parse_sync(self, filename: str, data: bytes, mime: str) -> MineruResult:
        """同步门面（Celery 任务内经 asyncio.run 调用异步版本，此方法供测试）。"""
        return asyncio.run(self.parse(filename, data, mime))

    async def health(self) -> bool:
        try:
            async with self._client(httpx.Timeout(5.0, connect=5.0)) as client:
                resp = await client.get(f"{self._settings.base_url.rstrip('/')}/v1/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # 异步任务接口（供 MinerU 官方 API 兼容层使用）
    # 以 zip 产物提交，完成后经 fetch_result_zip 取完整产物包
    # （markdown / structured_content / middle_json / images），与官方
    # full_zip_url 语义对齐。内部状态词汇 pending/processing/completed/failed。
    # ------------------------------------------------------------------

    async def submit_task(
        self,
        filename: str,
        data: bytes,
        mime: str,
        *,
        tier: str | None = None,
        ocr_mode: str = "auto",
        page_range: str | None = None,
    ) -> str:
        file_id = await self._upload_file(filename, data, mime)
        return await self._create_job(
            file_id,
            tier=tier or self._settings.tier,
            ocr_mode=ocr_mode,
            output_formats=["zip"],
            page_range=page_range,
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """查询内部任务状态（归一化为 pending/processing/completed/failed 词汇）。"""
        job = await self._get_job(task_id)
        status = str(job.get("status"))
        if status == "queued":
            internal = "pending"
        elif status == "running":
            internal = "processing"
        elif status in ("completed", "partial"):
            files = job.get("files") or []
            failed = any(f.get("status") == "failed" for f in files)
            completed = any(f.get("status") == "completed" for f in files)
            # partial（部分文件失败）：有成功产物按 completed 交付，否则按 failed
            internal = "failed" if failed and not completed else "completed"
        else:  # failed / canceled
            internal = "failed"
        error = "任务已取消" if status == "canceled" else self._job_error(job)
        return {"status": internal, "error": error if internal == "failed" else ""}

    async def fetch_result_zip(self, task_id: str) -> bytes:
        """拉取已完成任务的完整产物 zip（job 产物中的 zip file_id → Files API）。"""
        job = await self._get_job(task_id)
        files = job.get("files") or []
        outputs = (files[0].get("output_files") or {}) if files else {}
        zip_ref = outputs.get("zip")
        if not zip_ref:
            raise UpstreamUnavailableError(
                upstream="mineru", reason=f"任务产物缺少 zip：{str(job)[:300]}"
            )
        return await self._download_file(zip_ref["file_id"])


def _flatten_structured_content(sc: dict[str, Any]) -> list[dict[str, Any]]:
    """把页分组 structured_content 拉平为扁平版面块（旧 content_list 契约）。

    每块保留 type/text/page_idx/bbox；bbox 为 4.0 归一化坐标（0~1）。
    视觉块的 captions/footnotes 单列成块（Markdown 中独立成行，参与行号匹配）。
    """
    blocks: list[dict[str, Any]] = []

    def _entry(source: dict[str, Any], page_idx: int) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": source.get("type"),
            "text": str(source.get("content") or ""),
            "page_idx": page_idx,
        }
        bbox = source.get("bbox")
        if bbox:
            entry["bbox"] = list(bbox)
        return entry

    for page in sc.get("pages") or []:
        page_idx = int(page.get("page_idx") or 0)
        for block in page.get("blocks") or []:
            blocks.append(_entry(block, page_idx))
            for key in ("captions", "footnotes"):
                for note in block.get(key) or []:
                    if isinstance(note, dict) and note.get("content"):
                        blocks.append(_entry({**note, "type": key}, page_idx))
    return blocks
