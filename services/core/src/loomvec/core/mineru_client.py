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
            detail = ""
            if hasattr(e, "response") and e.response is not None:  # type: ignore[attr-defined]
                detail = e.response.text[:500]  # type: ignore[attr-defined]
            raise UpstreamUnavailableError(
                upstream="mineru", reason=f"{type(e).__name__}: {e} {detail}"
            ) from e

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
