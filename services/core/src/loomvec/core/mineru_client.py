"""P1-WRK-02 MinerU HTTP 客户端（mineru-api `/file_parse`，同步 FastAPI 路径）。

输出契约（03 文档 §一）：Markdown（标题层级）+ content_list（版面块：
type/text/table_body/page_idx/bbox），用于行号→页码定位与表格独立成片。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from loomvec.core.config import MineruSettings
from loomvec.core.errors import UpstreamUnavailableError


@dataclass
class MineruResult:
    markdown: str
    content_list: list[dict[str, Any]] = field(default_factory=list)


class MineruClient:
    def __init__(self, settings: MineruSettings) -> None:
        self._settings = settings

    async def parse(self, filename: str, data: bytes, mime: str) -> MineruResult:
        timeout = httpx.Timeout(self._settings.timeout_seconds, connect=30.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.post(
                    f"{self._settings.base_url.rstrip('/')}/file_parse",
                    files={"files": (filename, data, mime)},
                    data={
                        "backend": self._settings.backend,
                        "return_md": "true",
                        "return_content_list": "true",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

        results = payload.get("results") or {}
        # results 以（规范化后的）文件名为键；单文件解析取第一个非空结果
        for item in results.values():
            md = item.get("md_content")
            if md is not None:
                content_list = item.get("content_list")
                if isinstance(content_list, str):  # MinerU 以 JSON 字符串返回 content_list
                    try:
                        content_list = json.loads(content_list)
                    except json.JSONDecodeError:
                        content_list = []
                if not isinstance(content_list, list):
                    content_list = []
                return MineruResult(markdown=str(md), content_list=content_list)
        raise UpstreamUnavailableError(
            upstream="mineru", reason=f"响应缺少 md_content：{str(payload)[:300]}"
        )

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.get(f"{self._settings.base_url.rstrip('/')}/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def parse_sync(self, filename: str, data: bytes, mime: str) -> MineruResult:
        """同步门面（Celery 任务内经 asyncio.run 调用异步版本，此方法供测试）。"""
        return asyncio.run(self.parse(filename, data, mime))

    # ------------------------------------------------------------------
    # 异步任务接口（mineru-api `POST /tasks` 系列；供 MinerU 官方 API 兼容层使用）
    # 以 response_format_zip 提交，完成后经 fetch_result_zip 取完整产物包
    # （markdown / content_list / images），与官方 full_zip_url 语义对齐。
    # ------------------------------------------------------------------

    async def submit_task(
        self,
        filename: str,
        data: bytes,
        mime: str,
        *,
        backend: str | None = None,
        parse_method: str | None = None,
        language: str | None = None,
        formula_enable: bool = True,
        table_enable: bool = True,
        start_page_id: int | None = None,
        end_page_id: int | None = None,
    ) -> str:
        form: dict[str, Any] = {
            "backend": backend or self._settings.backend,
            "formula_enable": "true" if formula_enable else "false",
            "table_enable": "true" if table_enable else "false",
            "return_md": "true",
            "return_content_list": "true",
            "return_images": "true",
            "response_format_zip": "true",
        }
        if parse_method:
            form["parse_method"] = parse_method
        if language:
            form["lang_list"] = [language]
        if start_page_id is not None:
            form["start_page_id"] = str(start_page_id)
        if end_page_id is not None:
            form["end_page_id"] = str(end_page_id)

        timeout = httpx.Timeout(self._settings.timeout_seconds, connect=30.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.post(
                    f"{self._settings.base_url.rstrip('/')}/tasks",
                    files={"files": (filename, data, mime)},
                    data=form,
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

        task_id = payload.get("task_id")
        if not task_id:
            raise UpstreamUnavailableError(
                upstream="mineru", reason=f"任务提交响应缺少 task_id：{str(payload)[:300]}"
            )
        return str(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """查询内部异步任务状态（payload 含 status: pending/processing/completed/failed）。"""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0), trust_env=False
            ) as client:
                resp = await client.get(f"{self._settings.base_url.rstrip('/')}/tasks/{task_id}")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    async def fetch_result_zip(self, task_id: str) -> bytes:
        """拉取已完成任务的完整产物 zip（mineru-api `GET /tasks/{task_id}/result`）。"""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.timeout_seconds, connect=30.0),
                trust_env=False,
            ) as client:
                resp = await client.get(
                    f"{self._settings.base_url.rstrip('/')}/tasks/{task_id}/result"
                )
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(upstream="mineru", reason=self._describe(e)) from e

    @staticmethod
    def _describe(e: httpx.HTTPError) -> str:
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            detail = f" {resp.text[:500]}"
        return f"{type(e).__name__}: {e}{detail}"
