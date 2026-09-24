"""MinerU 官方 API 兼容层单测（/api/v4；内部 mineru-api、Redis、对象存储全部打桩）。

覆盖：单 URL 任务全流程、上传批量全流程（申请→直传→自动解析→轮询→zip 下载）、
model_version / page_range 校验、失败标记、租户隔离、鉴权映射（Bearer 直带
API Key / X-API-Key）与官方信封（{code, data, msg, trace_id}）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loomvec.api.context import Identity
from loomvec.api.routes import mineru_compat as mc
from loomvec.core.config import Settings
from loomvec.core.errors import UnauthenticatedError, UpstreamUnavailableError

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True


class _FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, bucket: str, key: str, data: bytes, content_type=None) -> None:
        self.objects[key] = data

    async def get_object(self, bucket: str, key: str) -> bytes:
        return self.objects[key]

    async def delete_object(self, bucket: str, key: str) -> None:
        self.objects.pop(key, None)


class _FakeMineru:
    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.status = "processing"
        self.error: str | None = None
        self.fail_submit = False

    async def submit_task(self, filename: str, data: bytes, mime: str, **kwargs) -> str:
        if self.fail_submit:
            raise UpstreamUnavailableError(upstream="mineru", reason="boom")
        task_id = f"internal-{len(self.submitted) + 1}"
        self.submitted.append({"task_id": task_id, "filename": filename, "mime": mime, **kwargs})
        return task_id

    async def get_task(self, task_id: str) -> dict:
        return {"status": self.status, "error": self.error}

    async def fetch_result_zip(self, task_id: str) -> bytes:
        return b"ZIPDATA"


def _identity(tenant: str = TENANT_A) -> Identity:
    return Identity(
        user_id="apikey:t",
        username="tester",
        tenant_id=tenant,
        roles=("api_key",),
        scopes=("read", "write"),
    )


class _Env:
    """兼容层路由 + 全打桩依赖（身份默认 API Key，可随时换租户）。"""

    def __init__(self, tenant: str = TENANT_A) -> None:
        self.app = FastAPI()
        self.app.include_router(mc.router)
        mc.register_mineru_compat_exception_handler(self.app)
        self.redis = _FakeRedis()
        self.storage = _FakeStorage()
        self.mineru = _FakeMineru()
        self.settings = Settings(
            auth={"jwt_secret": "test-secret"}, mineru={"compat_enabled": True}
        )
        self.identity = _identity(tenant)
        ov = self.app.dependency_overrides
        ov[mc.get_compat_identity] = lambda: self.identity
        ov[mc.get_redis] = lambda: self.redis
        ov[mc.get_storage] = lambda: self.storage
        ov[mc.get_mineru] = lambda: self.mineru
        ov[mc.service_settings] = lambda: self.settings


@pytest.fixture()
async def env():
    e = _Env()
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://testserver") as c:
        yield e, c


def _stub_download(monkeypatch, data=b"%PDF-fake", name=None, mime="application/pdf"):
    async def fake_download(url: str):
        # 真实实现按 URL 推导文件名；桩保持同一行为（name 可显式覆盖）
        return data, name or mc._name_from_url(url), mime

    monkeypatch.setattr(mc, "_download_url", fake_download)


# ---------------------------------------------------------------------------
# 单 URL 任务
# ---------------------------------------------------------------------------


async def test_extract_task_submit_then_poll_then_download(env, monkeypatch):
    e, client = env
    _stub_download(monkeypatch, name="doc.pdf")
    r = await client.post("/api/v4/extract/task", json={"url": "https://example.com/doc.pdf"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0 and body["msg"] == "ok" and body["trace_id"]
    task_id = body["data"]["task_id"]

    await mc.drain_background_tasks()
    assert len(e.mineru.submitted) == 1
    sub = e.mineru.submitted[0]
    assert sub["backend"] == "pipeline"
    assert sub["filename"] == "doc.pdf"
    assert sub["mime"] == "application/pdf"

    r = await client.get(f"/api/v4/extract/task/{task_id}")
    data = r.json()["data"]
    assert data["state"] == "running"  # 打桩内部状态 processing → running

    e.mineru.status = "completed"
    r = await client.get(f"/api/v4/extract/task/{task_id}")
    data = r.json()["data"]
    assert data["state"] == "done"
    assert data["full_zip_url"].startswith("http://testserver/api/v4/extract-results/file/")

    zip_path = data["full_zip_url"].removeprefix("http://testserver")
    r = await client.get(zip_path)
    assert r.status_code == 200
    assert r.content == b"ZIPDATA"
    assert r.headers["content-type"].startswith("application/zip")


async def test_extract_task_rejects_non_http_url(env):
    _, client = env
    r = await client.post("/api/v4/extract/task", json={"url": "ftp://example.com/a.pdf"})
    assert r.status_code == 400
    assert r.json()["code"] != 0


async def test_extract_task_rejects_mineru_html_model(env):
    _, client = env
    r = await client.post(
        "/api/v4/extract/task",
        json={"url": "https://example.com/a.pdf", "model_version": "MinerU-HTML"},
    )
    assert r.status_code == 400
    assert "MinerU-HTML" in r.json()["msg"]


async def test_extract_task_vlm_degrades_to_configured_backend(env, monkeypatch):
    """官方默认值 vlm 在 pipeline-only 部署上降级为配置后端（而非 400）。"""
    e, client = env
    _stub_download(monkeypatch)
    r = await client.post(
        "/api/v4/extract/task", json={"url": "https://example.com/a.pdf", "model_version": "vlm"}
    )
    assert r.status_code == 200
    await mc.drain_background_tasks()
    assert e.mineru.submitted[0]["backend"] == "pipeline"  # settings.mineru.backend 默认值


async def test_extract_task_page_range_mapping(env, monkeypatch):
    e, client = env
    _stub_download(monkeypatch)
    r = await client.post(
        "/api/v4/extract/task",
        json={"url": "https://example.com/a.pdf", "page_range": "2-5"},
    )
    await mc.drain_background_tasks()
    assert (e.mineru.submitted[0]["start_page_id"], e.mineru.submitted[0]["end_page_id"]) == (2, 5)

    r = await client.post(
        "/api/v4/extract/task",
        json={"url": "https://example.com/a.pdf", "page_range": "1-2,4"},
    )
    assert r.status_code == 400  # 官方逗号多段语法不支持，须显式报错


async def test_extract_task_download_failure_marks_failed(env, monkeypatch):
    _, client = env

    async def bad_download(url: str):
        raise mc.MineruCompatError(400, "下载解析源失败：连接超时")

    monkeypatch.setattr(mc, "_download_url", bad_download)
    r = await client.post("/api/v4/extract/task", json={"url": "https://example.com/a.pdf"})
    task_id = r.json()["data"]["task_id"]
    await mc.drain_background_tasks()

    r = await client.get(f"/api/v4/extract/task/{task_id}")
    data = r.json()["data"]
    assert data["state"] == "failed"
    assert "下载解析源失败" in data["err_msg"]
    assert "full_zip_url" not in data


# ---------------------------------------------------------------------------
# 批量上传解析
# ---------------------------------------------------------------------------


async def test_batch_upload_full_flow(env):
    e, client = env
    r = await client.post(
        "/api/v4/file-urls/batch",
        json={
            "model_version": "pipeline",
            "files": [{"name": "a.pdf", "data_id": "doc-1"}, {"name": "b.pdf"}],
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    batch_id = data["batch_id"]
    assert len(data["file_urls"]) == 2

    # 直传第一个文件（对齐官方：PUT 原始字节、无鉴权头、URL 即凭证）
    upload_path = data["file_urls"][0].removeprefix("http://testserver")
    r = await client.put(upload_path, content=b"%PDF-a")
    assert r.status_code == 200

    await mc.drain_background_tasks()
    assert len(e.mineru.submitted) == 1
    assert e.mineru.submitted[0]["filename"] == "a.pdf"
    # 仅一个对象：上传中转件提交成功后已清理
    assert list(e.storage.objects.keys()) == []

    e.mineru.status = "completed"
    r = await client.get(f"/api/v4/extract-results/batch/{batch_id}")
    extract = r.json()["data"]["extract_result"]
    assert extract[0]["file_name"] == "a.pdf"
    assert extract[0]["state"] == "done"
    assert extract[0]["data_id"] == "doc-1"
    assert extract[0]["full_zip_url"].startswith("http://testserver/api/v4/extract-results/file/")
    assert extract[1]["state"] == "waiting-file"  # 未上传的文件保持 waiting-file
    assert "full_zip_url" not in extract[1]


async def test_batch_upload_double_put_rejected(env):
    _, client = env
    r = await client.post("/api/v4/file-urls/batch", json={"files": [{"name": "a.pdf"}]})
    upload_path = r.json()["data"]["file_urls"][0].removeprefix("http://testserver")
    assert (await client.put(upload_path, content=b"xx")).status_code == 200
    assert (await client.put(upload_path, content=b"yy")).status_code == 409


async def test_batch_upload_empty_and_oversized_body(env, monkeypatch):
    _, client = env
    monkeypatch.setattr(mc, "_MAX_FILE_BYTES", 10)
    r = await client.post("/api/v4/file-urls/batch", json={"files": [{"name": "a.pdf"}]})
    upload_path = r.json()["data"]["file_urls"][0].removeprefix("http://testserver")
    assert (await client.put(upload_path, content=b"")).status_code == 400
    assert (await client.put(upload_path, content=b"0123456789abcdef")).status_code == 413


async def test_batch_upload_submit_failure_marks_file_failed(env):
    e, client = env
    e.mineru.fail_submit = True
    r = await client.post("/api/v4/file-urls/batch", json={"files": [{"name": "a.pdf"}]})
    upload_path = r.json()["data"]["file_urls"][0].removeprefix("http://testserver")
    await client.put(upload_path, content=b"%PDF-a")
    await mc.drain_background_tasks()

    batch_id = r.json()["data"]["batch_id"]
    extract = (await client.get(f"/api/v4/extract-results/batch/{batch_id}")).json()["data"][
        "extract_result"
    ]
    assert extract[0]["state"] == "failed"
    assert extract[0]["err_msg"]


async def test_batch_is_ocr_maps_parse_method(env):
    e, client = env
    r = await client.post(
        "/api/v4/file-urls/batch",
        json={"files": [{"name": "scan.pdf", "is_ocr": True}]},
    )
    upload_path = r.json()["data"]["file_urls"][0].removeprefix("http://testserver")
    await client.put(upload_path, content=b"%PDF-scan")
    await mc.drain_background_tasks()
    assert e.mineru.submitted[0]["parse_method"] == "ocr"


async def test_batch_rejects_empty_and_oversized_file_list(env):
    _, client = env
    assert (await client.post("/api/v4/file-urls/batch", json={"files": []})).status_code == 400
    assert (
        await client.post(
            "/api/v4/file-urls/batch", json={"files": [{"name": f"{i}.pdf"} for i in range(51)]}
        )
    ).status_code == 400


# ---------------------------------------------------------------------------
# URL 批量
# ---------------------------------------------------------------------------


async def test_url_batch_flow(env, monkeypatch):
    e, client = env
    _stub_download(monkeypatch)
    r = await client.post(
        "/api/v4/extract/task/batch",
        json={
            "files": [
                {"url": "https://example.com/x.pdf", "data_id": "u1"},
                {"url": "https://example.com/y.pdf"},
            ]
        },
    )
    assert r.status_code == 200
    batch_id = r.json()["data"]["batch_id"]
    await mc.drain_background_tasks()
    assert len(e.mineru.submitted) == 2

    e.mineru.status = "completed"
    extract = (await client.get(f"/api/v4/extract-results/batch/{batch_id}")).json()["data"][
        "extract_result"
    ]
    assert [item["state"] for item in extract] == ["done", "done"]
    assert [item["file_name"] for item in extract] == ["x.pdf", "y.pdf"]
    assert all("full_zip_url" in item for item in extract)


# ---------------------------------------------------------------------------
# 隔离 / 鉴权 / 契约
# ---------------------------------------------------------------------------


async def test_batch_tenant_isolation(env):
    e, client = env
    r = await client.post("/api/v4/file-urls/batch", json={"files": [{"name": "a.pdf"}]})
    batch_id = r.json()["data"]["batch_id"]
    e.identity = _identity(TENANT_B)  # dependency_overrides 读 self.identity，即时生效
    r = await client.get(f"/api/v4/extract-results/batch/{batch_id}")
    assert r.status_code == 404  # 跨租户不泄露存在性
    e.identity = _identity(TENANT_A)
    assert (await client.get(f"/api/v4/extract-results/batch/{batch_id}")).status_code == 200


async def test_bearer_carries_loomvec_api_key(monkeypatch):
    """官方客户端把 API Key 放在 Authorization: Bearer —— 应按 API Key 解析。"""
    e = _Env()
    ov = e.app.dependency_overrides
    del ov[mc.get_compat_identity]
    ov[mc.get_session] = lambda: None
    ov[mc.get_redis] = lambda: e.redis
    ov[mc.get_identity_provider] = lambda: None
    seen: dict[str, str] = {}

    async def fake_api_key(raw_key, session, redis):
        seen["key"] = raw_key
        return _identity()

    monkeypatch.setattr(mc, "identity_from_api_key", fake_api_key)
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://testserver") as c:
        r = await c.get(
            "/api/v4/extract/task/none", headers={"Authorization": "Bearer loomvec-key-123"}
        )
        assert r.status_code == 404  # 认证已通过（否则 401），任务不存在
        assert seen["key"] == "loomvec-key-123"
        seen.clear()
        r = await c.get("/api/v4/extract/task/none", headers={"X-API-Key": "loomvec-key-456"})
        assert r.status_code == 404
        assert seen["key"] == "loomvec-key-456"


async def test_missing_credentials_returns_official_envelope():
    e = _Env()
    ov = e.app.dependency_overrides
    del ov[mc.get_compat_identity]
    ov[mc.get_session] = lambda: None
    ov[mc.get_redis] = lambda: e.redis
    ov[mc.get_identity_provider] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://testserver") as c:
        r = await c.get("/api/v4/extract/task/none")
    assert r.status_code == 401
    body = r.json()
    # 未携带凭证 → 提示应携带什么（区别于"凭证无效"）
    assert body["code"] != 0 and "未认证" in body["msg"] and "API Key" in body["msg"]
    assert body["trace_id"]


async def test_invalid_bearer_key_message_carries_reason(monkeypatch):
    """带了凭证但无效 → 401 消息须包含底层原因（便于客户端日志定位）。"""
    e = _Env()
    ov = e.app.dependency_overrides
    del ov[mc.get_compat_identity]
    ov[mc.get_session] = lambda: None
    ov[mc.get_redis] = lambda: e.redis
    ov[mc.get_identity_provider] = lambda: None

    async def bad_api_key(raw_key, session, redis):
        raise UnauthenticatedError(reason="API Key 无效或已过期")

    monkeypatch.setattr(mc, "identity_from_api_key", bad_api_key)
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="http://testserver") as c:
        r = await c.get(
            "/api/v4/extract/task/none", headers={"Authorization": "Bearer enc:v1:ciphertext"}
        )
    assert r.status_code == 401
    msg = r.json()["msg"]
    assert "凭证无效或已过期" in msg and "API Key 无效或已过期" in msg


async def test_compat_routes_excluded_from_openapi_contract():
    app = FastAPI()
    app.include_router(mc.router)
    paths = app.openapi()["paths"]
    assert not any(p.startswith("/api/v4") for p in paths)
