"""MineruClient V1 线路单测（httpx.MockTransport 模拟 mineru-api 4.x）。

覆盖：uploads→job→zip 产物全流程、structured_content 拉平、job 状态归一化、
放弃等待即取消、解析名额、健康探测与错误信封传递。
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from loomvec.core.config import MineruSettings
from loomvec.core.errors import UpstreamUnavailableError
from loomvec.core.mineru_client import MineruClient, _flatten_structured_content
from loomvec.core.mineru_slots import MineruBusyError, MineruSlots

_STRUCTURED_CONTENT = {
    "pages": [
        {
            "page_idx": 0,
            "blocks": [
                {"type": "title", "content": "# 标题", "bbox": [0.1, 0.1, 0.9, 0.2]},
                {"type": "text", "content": "正文段落", "bbox": [0.1, 0.3, 0.9, 0.4]},
                {
                    "type": "table",
                    "content": "| a | b |",
                    "bbox": [0.2, 0.5, 0.8, 0.7],
                    "captions": [{"content": "表 1", "bbox": [0.2, 0.45, 0.8, 0.5]}],
                },
            ],
        },
        {"page_idx": 1, "blocks": []},
    ]
}
# zip 产物中的 markdown 以相对路径引用图片（单独 markdown 产物则内嵌 base64）
_ZIP_MARKDOWN = "# 标题\n\n正文段落\n\n![](images/page_0_image_3.jpg)"


def _result_zip(*, with_structured_content: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("markdown.md", _ZIP_MARKDOWN)
        zf.writestr("middle_json.json", "{}")
        if with_structured_content:
            zf.writestr("structured_content.json", json.dumps(_STRUCTURED_CONTENT))
        zf.writestr("images/page_0_image_3.jpg", b"\xff\xd8jpeg")
    return buf.getvalue()


class _FakeRedis:
    """MineruSlots 用到的 SET NX EX 与释放脚本（仅比对 token 后删除）的内存实现。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def eval(self, _script, _numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def _settings(**kw) -> MineruSettings:
    return MineruSettings(base_url="http://mineru.test", timeout_seconds=30.0, **kw)


class _V1Server:
    """按 V1 契约模拟 uploads / parse/jobs / files 的内存服务。"""

    def __init__(self) -> None:
        self.job_status = "completed"
        self.job_error = None
        self.fail_stage: str | None = None
        self.drop_structured_content = False
        self.zip_bytes: bytes | None = None  # None = 按 drop_structured_content 生成
        self.created_job_bodies: list[dict] = []
        self.seen_uploads: list[bytes] = []
        self.deleted_jobs: list[str] = []

    def handler(self) -> httpx.MockTransport:
        state = self

        def handle(request: httpx.Request) -> httpx.Response:
            path, method = request.url.path, request.method
            if state.fail_stage and path.endswith(state.fail_stage):
                return httpx.Response(
                    500,
                    json={
                        "error": {"type": "api_error", "code": "internal_error", "message": "boom"}
                    },
                )
            if method == "POST" and path == "/v1/uploads":
                body = json.loads(request.content)
                assert body["filename"] and body["bytes"] >= 0 and body["mime_type"]
                return httpx.Response(200, json={"id": "upload_1", "status": "pending"})
            if method == "PUT" and path == "/v1/uploads/upload_1/content":
                state.seen_uploads.append(request.content)
                return httpx.Response(200)
            if method == "POST" and path == "/v1/uploads/upload_1/complete":
                return httpx.Response(
                    200, json={"id": "upload_1", "status": "completed", "file": {"id": "file_src"}}
                )
            if method == "POST" and path == "/v1/parse/jobs":
                state.created_job_bodies.append(json.loads(request.content))
                return httpx.Response(202, json={"job_id": "job_1", "status": "queued"})
            if method == "DELETE" and path == "/v1/parse/jobs/job_1":
                state.deleted_jobs.append("job_1")
                return httpx.Response(200, json={"job_id": "job_1", "status": "canceled"})
            if method == "GET" and path == "/v1/parse/jobs/job_1":
                status = state.job_status
                err = (
                    {"type": "engine_error", "code": "parse_failed", "message": state.job_error}
                    if state.job_error
                    else None
                )
                file_status = {"failed": "failed", "running": "running", "queued": "queued"}.get(
                    status, "completed"
                )
                outputs = (
                    {"zip": {"file_id": "file_zip", "bytes": 10}}
                    if status in ("completed", "partial")
                    else None
                )
                return httpx.Response(
                    200,
                    json={
                        "job_id": "job_1",
                        "status": status,
                        "files": [
                            {
                                "name": "a.pdf",
                                "status": file_status,
                                "output_files": outputs,
                                "error": err,
                            }
                        ],
                    },
                )
            if method == "GET" and path == "/v1/files/file_zip/content":
                blob = state.zip_bytes
                if blob is None:
                    blob = _result_zip(with_structured_content=not state.drop_structured_content)
                return httpx.Response(200, content=blob)
            if method == "GET" and path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "version": "4.0.10"})
            return httpx.Response(
                404,
                json={
                    "error": {"type": "invalid_request_error", "code": "not_found", "message": path}
                },
            )

        return httpx.MockTransport(handle)


async def test_parse_full_flow():
    server = _V1Server()
    client = MineruClient(_settings(), transport=server.handler())
    result = await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")

    # 只取 zip：markdown 以 images/… 路径引用图片，不含 base64
    assert result.markdown == _ZIP_MARKDOWN
    assert "data:image" not in result.markdown
    assert server.seen_uploads == [b"%PDF-1.4"]
    job_body = server.created_job_bodies[0]
    assert job_body["tier"] == "basic"
    assert job_body["output_formats"] == ["zip"]
    assert job_body["files"][0]["source"] == {"type": "file_id", "file_id": "file_src"}
    # 页分组 structured_content 拉平为扁平版面块；无块页不产出条目
    assert [b["page_idx"] for b in result.content_list] == [0, 0, 0, 0]
    table = result.content_list[2]
    assert table["type"] == "table" and table["text"] == "| a | b |"
    assert table["bbox"] == [0.2, 0.5, 0.8, 0.7]
    assert result.content_list[3] == {
        "type": "captions",
        "text": "表 1",
        "page_idx": 0,
        "bbox": [0.2, 0.45, 0.8, 0.5],
    }


async def test_parse_job_failed_raises_with_message():
    server = _V1Server()
    server.job_status = "failed"
    server.job_error = "密码页无法解析"
    client = MineruClient(_settings(), transport=server.handler())
    with pytest.raises(UpstreamUnavailableError) as e:
        await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert "密码页无法解析" in e.value.details["reason"]


async def test_parse_survives_structured_content_unavailable():
    server = _V1Server()
    server.drop_structured_content = True
    client = MineruClient(_settings(), transport=server.handler())
    result = await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert result.markdown
    assert result.content_list == []  # 版面缺失降级，不阻塞 Markdown 主产物


async def test_submit_task_and_status_normalization():
    server = _V1Server()
    client = MineruClient(_settings(), transport=server.handler())
    job_id = await client.submit_task(
        "scan.pdf",
        b"%PDF-scan",
        "application/pdf",
        tier="flash",
        ocr_mode="ocr",
        page_range="1-3,r2",
    )
    assert job_id == "job_1"
    body = server.created_job_bodies[0]
    assert body["tier"] == "flash" and body["ocr_mode"] == "ocr"
    assert body["files"][0]["page_range"] == "1-3,r2"
    assert body["output_formats"] == ["zip"]

    for v1_status, expected in [
        ("queued", "pending"),
        ("running", "processing"),
        ("completed", "completed"),
        ("partial", "completed"),
    ]:
        server.job_status = v1_status
        server.job_error = None
        status = await client.get_task(job_id)
        assert status["status"] == expected and status["error"] == ""

    # 取消无服务端错误详情 → 归一化 failed 且 err_msg 明确为已取消
    server.job_status = "canceled"
    status = await client.get_task(job_id)
    assert status == {"status": "failed", "error": "任务已取消"}

    server.job_status = "failed"
    server.job_error = "boom"
    status = await client.get_task(job_id)
    assert status == {"status": "failed", "error": "boom"}


async def test_fetch_result_zip():
    server = _V1Server()
    client = MineruClient(_settings(), transport=server.handler())
    assert await client.fetch_result_zip("job_1") == _result_zip()

    server.job_status = "failed"
    with pytest.raises(UpstreamUnavailableError):
        await client.fetch_result_zip("job_1")


async def test_parse_invalid_zip_raises():
    server = _V1Server()
    server.zip_bytes = b"not a zip"
    client = MineruClient(_settings(), transport=server.handler())
    with pytest.raises(UpstreamUnavailableError) as e:
        await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert "zip" in e.value.details["reason"]


async def test_parse_timeout_cancels_job():
    # 放弃等待（超时）即取消 MinerU 任务，不留孤儿任务占着解析服务
    server = _V1Server()
    server.job_status = "running"
    client = MineruClient(
        MineruSettings(base_url="http://mineru.test", timeout_seconds=0.1),
        transport=server.handler(),
    )
    with pytest.raises(UpstreamUnavailableError) as e:
        await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert "超时" in e.value.details["reason"]
    assert server.deleted_jobs == ["job_1"]


async def test_slots_capacity_and_token_scoped_release():
    redis = _FakeRedis()
    slots = MineruSlots(redis, capacity=1, ttl_seconds=60)
    lease = await slots.acquire()
    assert lease is not None
    assert await slots.acquire() is None  # 满
    await slots.release(lease)
    second = await slots.acquire()
    assert second is not None
    await slots.release(lease)  # 过期租约的 token 不得删掉他人名额
    assert await slots.acquire() is None
    await slots.release(second)
    assert redis.store == {}


async def test_likely_busy_peeks_without_reserving():
    redis = _FakeRedis()
    slots = MineruSlots(redis, capacity=2, ttl_seconds=60)
    client = MineruClient(_settings(), slots=slots)

    assert await client.likely_busy() is False  # 空名额
    first = await slots.acquire()
    assert await client.likely_busy() is False  # 还有一个空位
    second = await slots.acquire()
    assert await client.likely_busy() is True  # 满
    await slots.release(first)
    assert await client.likely_busy() is False
    await slots.release(second)


async def test_likely_busy_false_when_unlimited():
    client = MineruClient(_settings())  # 未注入 slots：不限流
    assert await client.likely_busy() is False


async def test_parse_busy_when_slots_full_and_releases_after_failure():
    server = _V1Server()
    redis = _FakeRedis()
    slots = MineruSlots(redis, capacity=1, ttl_seconds=60)
    client = MineruClient(_settings(), transport=server.handler(), slots=slots)

    held = await slots.acquire()
    with pytest.raises(MineruBusyError):
        await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert server.seen_uploads == []  # 名额满不触达 MinerU
    await slots.release(held)

    server.job_status = "failed"
    server.job_error = "boom"
    with pytest.raises(UpstreamUnavailableError):
        await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert redis.store == {}  # 失败也归还名额

    server.job_status = "completed"
    server.job_error = None
    result = await client.parse("a.pdf", b"%PDF-1.4", "application/pdf")
    assert result.markdown == _ZIP_MARKDOWN and redis.store == {}


async def test_health():
    server = _V1Server()
    client = MineruClient(_settings(), transport=server.handler())
    assert await client.health() is True
    server.fail_stage = "/v1/health"
    assert await client.health() is False


def test_flatten_structured_content_without_optional_fields():
    blocks = _flatten_structured_content(
        {"pages": [{"page_idx": 2, "blocks": [{"type": "text", "content": "无坐标块"}]}]}
    )
    assert blocks == [{"type": "text", "text": "无坐标块", "page_idx": 2}]
