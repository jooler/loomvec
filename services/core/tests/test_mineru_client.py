"""MineruClient V1 线路单测（httpx.MockTransport 模拟 mineru-api 4.x）。

覆盖：uploads→job→产物下载全流程、structured_content 拉平、job 状态归一化、
zip 产物拉取、健康探测与错误信封传递。
"""

from __future__ import annotations

import json

import httpx
import pytest

from loomvec.core.config import MineruSettings
from loomvec.core.errors import UpstreamUnavailableError
from loomvec.core.mineru_client import MineruClient, _flatten_structured_content


def _settings(**kw) -> MineruSettings:
    return MineruSettings(base_url="http://mineru.test", timeout_seconds=30.0, **kw)


class _V1Server:
    """按 V1 契约模拟 uploads / parse/jobs / files 的内存服务。"""

    def __init__(self) -> None:
        self.job_status = "completed"
        self.job_error = None
        self.fail_stage: str | None = None
        self.drop_structured_content = False
        self.created_job_bodies: list[dict] = []
        self.seen_uploads: list[bytes] = []

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
            if state.drop_structured_content and path == "/v1/files/file_sc/content":
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "code": "not_found",
                            "message": "gone",
                        }
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
            if method == "GET" and path == "/v1/parse/jobs/job_1":
                status = state.job_status
                err = (
                    {"type": "engine_error", "code": "parse_failed", "message": state.job_error}
                    if state.job_error
                    else None
                )
                file_status = "failed" if status == "failed" else "completed"
                outputs = (
                    {
                        "markdown": {"file_id": "file_md", "bytes": 10},
                        "structured_content": {"file_id": "file_sc", "bytes": 10},
                        "zip": {"file_id": "file_zip", "bytes": 10},
                    }
                    if file_status == "completed"
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
            if method == "GET" and path == "/v1/files/file_md/content":
                return httpx.Response(200, text="# 标题\n\n正文段落")
            if method == "GET" and path == "/v1/files/file_sc/content":
                return httpx.Response(
                    200,
                    json={
                        "pages": [
                            {
                                "page_idx": 0,
                                "blocks": [
                                    {
                                        "type": "title",
                                        "content": "# 标题",
                                        "bbox": [0.1, 0.1, 0.9, 0.2],
                                    },
                                    {
                                        "type": "text",
                                        "content": "正文段落",
                                        "bbox": [0.1, 0.3, 0.9, 0.4],
                                    },
                                    {
                                        "type": "table",
                                        "content": "| a | b |",
                                        "bbox": [0.2, 0.5, 0.8, 0.7],
                                        "captions": [
                                            {"content": "表 1", "bbox": [0.2, 0.45, 0.8, 0.5]}
                                        ],
                                    },
                                ],
                            },
                            {"page_idx": 1, "blocks": []},
                        ]
                    },
                )
            if method == "GET" and path == "/v1/files/file_zip/content":
                return httpx.Response(200, content=b"ZIPDATA")
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

    assert result.markdown == "# 标题\n\n正文段落"
    assert server.seen_uploads == [b"%PDF-1.4"]
    job_body = server.created_job_bodies[0]
    assert job_body["tier"] == "basic"
    assert job_body["output_formats"] == ["markdown", "structured_content"]
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
    assert await client.fetch_result_zip("job_1") == b"ZIPDATA"

    server.job_status = "failed"
    with pytest.raises(UpstreamUnavailableError):
        await client.fetch_result_zip("job_1")


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
