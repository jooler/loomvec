"""预签名 URL 同源前缀：前端代理声明头 → presign 改写；无头 / 非法头保持原 endpoint。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from loomvec.api.middleware import StoragePrefixMiddleware
from loomvec.core.config import StorageSettings
from loomvec.core.storage import ObjectStorage

storage = ObjectStorage(StorageSettings(endpoint="http://localhost:39000"))


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(StoragePrefixMiddleware)

    @app.get("/url")
    def url() -> dict:
        return {"url": storage.presign_get("loomvec-raw", "raw/a.pdf")}

    return app


async def _get(headers: dict[str, str] | None = None) -> str:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return (await client.get("/url", headers=headers)).json()["url"]


async def test_prefix_header_rewrites_presigned_url():
    url = await _get({"X-Loomvec-Storage-Prefix": "/s3"})
    assert url.startswith("/s3/loomvec-raw/raw/a.pdf?") and "X-Amz-Signature" in url


async def test_no_header_keeps_endpoint():
    assert (await _get()).startswith("http://localhost:39000/loomvec-raw/raw/a.pdf?")


async def test_invalid_prefix_ignored():
    url = await _get({"X-Loomvec-Storage-Prefix": "//evil.example.com"})
    assert url.startswith("http://localhost:39000/")
