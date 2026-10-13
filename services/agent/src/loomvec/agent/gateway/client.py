"""acp-gateway 薄 httpx 异步客户端：只覆盖 loomvec 用到的端点。

约定：
- 端点集中声明在 `ENDPOINTS`（方法 + OpenAPI 路径模板），客户端方法一律经它发请求；
  契约测试据此校验「用到的每个 (method, path) 都在 openapi 快照里」，请求体按快照 schema 校验；
- 普通请求带总超时（`request_timeout_s`）；SSE 流不设总超时，只设连接超时与读空闲超时
  （网关每 15s 发心跳注释，空闲超时远大于它）；
- 幂等 GET 在传输错误 / 502·503·504 时重试一次；POST / PUT / PATCH / DELETE 不重试；
- API Key 只放 `Authorization` 头，不进日志、不进异常消息。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from loomvec.agent.gateway.errors import GatewayError, GatewayPathError, GatewayTransportError
from loomvec.agent.gateway.sse import SseMessage, parse_sse
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.gateway")

CONNECT_TIMEOUT_S = 10.0
SSE_READ_TIMEOUT_S = 300.0  # 空闲读超时（不是总时长）：冷启动 agent 容器时响应头可能迟到
_RETRY_STATUS = frozenset({502, 503, 504})
_RETRY_DELAY_S = 0.3


@dataclass(frozen=True)
class Endpoint:
    """一个被用到的网关端点。"""

    name: str
    method: str
    path: str  # OpenAPI 路径模板；文件路径占位符写作 `{*}`
    in_openapi: bool = True  # /healthz 等运维端点不在 openapi 里

    def url(self, **params: str) -> str:
        out = self.path
        for key, value in params.items():
            if key == "file_path":
                rel = value.strip("/")
                if not rel or any(seg in (".", "..") for seg in rel.split("/")):
                    raise GatewayPathError(f"非法的文件路径：{value[:100]!r}")
                out = out.replace("{*}", quote(rel, safe="/"))
            else:
                out = out.replace("{" + key + "}", quote(value, safe=""))
        return out


def _ep(name: str, method: str, path: str, **kw: Any) -> Endpoint:
    return Endpoint(name, method, path, **kw)


ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        _ep("healthz", "GET", "/healthz", in_openapi=False),
        _ep("workspace_create", "POST", "/v1/workspaces"),
        _ep("workspace_list", "GET", "/v1/workspaces"),
        _ep("instructions_get", "GET", "/v1/workspaces/{id}/instructions"),
        _ep("instructions_put", "PUT", "/v1/workspaces/{id}/instructions"),
        _ep("session_create", "POST", "/v1/workspaces/{id}/sessions"),
        _ep("session_patch", "PATCH", "/v1/sessions/{id}"),
        _ep("session_archive", "DELETE", "/v1/sessions/{id}"),
        _ep("turn_start", "POST", "/v1/sessions/{id}/turns"),
        _ep("turn_get", "GET", "/v1/sessions/{id}/turns/{turn_id}"),
        _ep("turn_cancel", "POST", "/v1/sessions/{id}/cancel"),
        _ep("events", "GET", "/v1/sessions/{id}/events"),
        _ep("messages", "GET", "/v1/sessions/{id}/messages"),
        _ep("tree", "GET", "/v1/workspaces/{id}/tree"),
        _ep("file_get", "GET", "/v1/workspaces/{id}/files/{*}"),
        _ep("file_put", "PUT", "/v1/workspaces/{id}/files/{*}"),
        _ep("dir_create", "POST", "/v1/workspaces/{id}/dirs/{*}"),
    )
}


def make_http_client(
    base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """共享连接池（进程级一个）；API Key 逐请求带，故一个池可服务任意 Key。"""
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        transport=transport,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        # 默认超时兜底；各请求显式覆盖
        timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT_S),
    )


class GatewayClient:
    """绑定一个 API Key 的客户端视图；轻量对象，随用随建，连接池由调用方持有。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str,
        request_timeout_s: float = 30.0,
        sse_read_timeout_s: float = SSE_READ_TIMEOUT_S,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._timeout = httpx.Timeout(request_timeout_s, connect=CONNECT_TIMEOUT_S)
        self._sse_timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=sse_read_timeout_s,
            write=request_timeout_s,
            pool=request_timeout_s,
        )

    def __repr__(self) -> str:  # 防止 Key 经 repr 进日志
        return "GatewayClient(api_key=***)"

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", **(extra or {})}

    async def _request(
        self,
        ep: Endpoint,
        *,
        path_params: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: Any = None,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        authed: bool = True,
    ) -> httpx.Response:
        url = ep.url(**(path_params or {}))
        attempts = 2 if ep.method == "GET" else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = await self._http.request(
                    ep.method,
                    url,
                    params=params,
                    json=json_body,
                    content=content,
                    headers=self._headers(headers) if authed else headers,
                    timeout=timeout or self._timeout,
                )
            except httpx.HTTPError as e:
                last_exc = e
                if attempt + 1 < attempts:
                    await asyncio.sleep(_RETRY_DELAY_S)
                    continue
                raise GatewayTransportError(f"{type(e).__name__}: {str(e)[:200]}") from e
            if resp.status_code in _RETRY_STATUS and attempt + 1 < attempts:
                await asyncio.sleep(_RETRY_DELAY_S)
                continue
            if resp.status_code >= 400:
                raise GatewayError.from_response(resp)
            return resp
        raise GatewayTransportError(str(last_exc))  # pragma: no cover - 循环必然已返回/抛出

    async def _json(self, ep: Endpoint, **kw: Any) -> Any:
        resp = await self._request(ep, **kw)
        return resp.json() if resp.content else None

    @asynccontextmanager
    async def _stream(
        self,
        ep: Endpoint,
        *,
        path_params: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        url = ep.url(**path_params)
        try:
            async with self._http.stream(
                ep.method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(headers),
                timeout=self._sse_timeout,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise GatewayError.from_response(resp, body)
                yield resp
        except httpx.HTTPError as e:
            raise GatewayTransportError(f"{type(e).__name__}: {str(e)[:200]}") from e

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------

    async def healthz(self, *, timeout_s: float = 3.0) -> bool:
        """网关存活（数据库连通）。不抛：连不上 / 非 2xx 一律 False。"""
        try:
            resp = await self._http.get(
                ENDPOINTS["healthz"].url(), timeout=httpx.Timeout(timeout_s, connect=timeout_s)
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    # ------------------------------------------------------------------
    # workspace / instructions
    # ------------------------------------------------------------------

    async def create_workspace(
        self, external_ref: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"external_ref": external_ref}
        if metadata:
            body["metadata"] = metadata
        return await self._json(ENDPOINTS["workspace_create"], json_body=body)

    async def find_workspace_by_ref(self, external_ref: str) -> str | None:
        data = await self._json(ENDPOINTS["workspace_list"], params={"external_ref": external_ref})
        for item in (data or {}).get("items", []):
            if item.get("external_ref") == external_ref and item.get("status") == "active":
                return str(item["id"])
        return None

    async def get_or_create_workspace(
        self, external_ref: str, metadata: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        """幂等取得 workspace id：返回 (id, 本次是否新建)。

        409（external_ref 已占用，如并发首次使用）时读 `details.workspace_id` 回填；
        网关未给出时退回按 external_ref 列表查询。
        """
        try:
            ws = await self.create_workspace(external_ref, metadata)
            return str(ws["id"]), True
        except GatewayError as e:
            if e.status != 409:
                raise
            existing = e.detail_dict.get("workspace_id") or await self.find_workspace_by_ref(
                external_ref
            )
            if not existing:
                raise
            return str(existing), False

    async def get_instructions(self, workspace_id: str) -> str | None:
        data = await self._json(ENDPOINTS["instructions_get"], path_params={"id": workspace_id})
        content = (data or {}).get("content")
        return content if isinstance(content, str) else None

    async def put_instructions(self, workspace_id: str, content: str) -> None:
        await self._json(
            ENDPOINTS["instructions_put"],
            path_params={"id": workspace_id},
            json_body={"content": content},
        )

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    async def create_session(self, workspace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json(
            ENDPOINTS["session_create"], path_params={"id": workspace_id}, json_body=body
        )

    async def patch_session(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json(
            ENDPOINTS["session_patch"], path_params={"id": session_id}, json_body=body
        )

    async def archive_session(self, session_id: str) -> None:
        await self._json(ENDPOINTS["session_archive"], path_params={"id": session_id})

    # ------------------------------------------------------------------
    # 回合与事件
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def open_turn(
        self, session_id: str, content: list[dict[str, Any]]
    ) -> AsyncIterator[AsyncIterator[SseMessage]]:
        """发起回合并订阅其 SSE。受理阶段失败（HTTP 4xx）在进入上下文时抛 `GatewayError`。

        退出上下文只关闭本连接，不取消网关上的回合（回合与连接解耦）。
        """
        async with self._stream(
            ENDPOINTS["turn_start"],
            path_params={"id": session_id},
            json_body={"content": content, "stream": True},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            yield parse_sse(resp.aiter_lines())

    @asynccontextmanager
    async def open_events(
        self, session_id: str, *, after_seq: int = 0, follow: bool = True
    ) -> AsyncIterator[AsyncIterator[SseMessage]]:
        """订阅会话事件流（断线续传：`after_seq` 之后的事件）。"""
        async with self._stream(
            ENDPOINTS["events"],
            path_params={"id": session_id},
            params={"after_seq": after_seq, "follow": "1" if follow else "0"},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            yield parse_sse(resp.aiter_lines())

    async def cancel_turn(self, session_id: str) -> bool:
        data = await self._json(ENDPOINTS["turn_cancel"], path_params={"id": session_id})
        return bool((data or {}).get("cancelled"))

    async def get_turn(self, session_id: str, turn_id: str) -> dict[str, Any]:
        return await self._json(
            ENDPOINTS["turn_get"], path_params={"id": session_id, "turn_id": turn_id}
        )

    async def list_messages(
        self, session_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._json(
            ENDPOINTS["messages"], path_params={"id": session_id}, params=params
        )

    async def all_messages(self, session_id: str, *, page_limit: int = 100) -> list[dict[str, Any]]:
        """按回合翻页读完整个消息视图（旧 → 新）。"""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(1000):  # 防御：游标不前进时不死循环
            page = await self.list_messages(session_id, cursor=cursor, limit=page_limit)
            items.extend(page.get("items") or [])
            nxt = page.get("next_cursor")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return items

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    async def tree(self, workspace_id: str, path: str = "", depth: int = 1) -> dict[str, Any]:
        return await self._json(
            ENDPOINTS["tree"],
            path_params={"id": workspace_id},
            params={"path": path, "depth": depth},
        )

    async def mkdir(self, workspace_id: str, path: str) -> None:
        await self._json(
            ENDPOINTS["dir_create"], path_params={"id": workspace_id, "file_path": path}
        )

    async def stat_file(self, workspace_id: str, path: str) -> dict[str, Any]:
        return await self._json(
            ENDPOINTS["file_get"],
            path_params={"id": workspace_id, "file_path": path},
            params={"stat": "1"},
        )

    async def put_file(
        self,
        workspace_id: str,
        path: str,
        content: bytes | AsyncIterator[bytes],
        *,
        size: int | None = None,
    ) -> dict[str, Any]:
        """上传原始字节。给出 `size` 时带 Content-Length（网关据此整体预留磁盘额度）。"""
        headers = {"Content-Type": "application/octet-stream"}
        if size is not None and not isinstance(content, bytes):
            headers["Content-Length"] = str(size)
        return await self._json(
            ENDPOINTS["file_put"],
            path_params={"id": workspace_id, "file_path": path},
            content=content,
            headers=headers,
        )

    async def read_file(
        self, workspace_id: str, path: str, *, max_bytes: int
    ) -> tuple[bytes, int, bool]:
        """读文件前 `max_bytes` 字节：返回 (内容, 文件总大小, 是否被截断)。大文件不整读进内存。"""
        ep = ENDPOINTS["file_get"]
        async with self._stream(ep, path_params={"id": workspace_id, "file_path": path}) as resp:
            total = int(resp.headers.get("content-length") or 0)
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    break
            truncated = len(buf) > max_bytes or (total > max_bytes)
            data = bytes(buf[:max_bytes])
            return data, (total or len(data)), truncated
