"""P0-QA-01 core 单元测试：配置 / 错误契约 / AI 网关配置 / 存储适配器（stub）。"""

from __future__ import annotations

import pytest

from loomvec.core.config import Settings
from loomvec.core.errors import NotFoundError, UpstreamUnavailableError, ValidationError
from loomvec.core.storage import ObjectStorage


def test_settings_env_parsing(monkeypatch):
    monkeypatch.setenv("LOOMVEC_POSTGRES__URL", "postgresql+asyncpg://u:p@h:1/db")
    monkeypatch.setenv("LOOMVEC_AI__EMBEDDING__MODEL", "text-embedding-v4")
    monkeypatch.setenv("LOOMVEC_LOG_JSON", "false")
    s = Settings(_env_file=None)
    assert s.postgres.url.endswith("/db")
    assert s.ai.embedding.model == "text-embedding-v4"
    assert s.log_json is False


def test_error_payload_contract():
    err = NotFoundError("资产不存在", asset_id="42")
    payload = err.to_payload()
    # 全局错误结构（契约）：code/message/details
    assert payload == {
        "code": "not_found",
        "message": "资产不存在",
        "details": {"asset_id": "42"},
    }
    assert err.http_status == 404


def test_upstream_error_carries_component():
    err = UpstreamUnavailableError(upstream="milvus")
    assert err.details["upstream"] == "milvus"
    assert err.http_status == 502


def test_ai_gateway_requires_config():
    from loomvec.core.ai import AiGateway
    from loomvec.core.config import AiSettings

    gw = AiGateway(AiSettings())
    with pytest.raises(ValidationError):
        gw._provider("embedding")


class _BotoStub:
    def __init__(self):
        self.calls = []

    def head_bucket(self, Bucket):
        self.calls.append(("head_bucket", Bucket))
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")  # 模拟 bucket 尚不存在

    def create_bucket(self, Bucket):
        self.calls.append(("create_bucket", Bucket))

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.calls.append(("put_bucket_cors", Bucket))


def test_storage_ensure_buckets(monkeypatch):
    stub = _BotoStub()
    monkeypatch.setattr("loomvec.core.storage.boto3.client", lambda *a, **kw: stub)
    storage = ObjectStorage(Settings(_env_file=None).storage)
    storage.ensure_buckets()
    actions = [c[0] for c in stub.calls]
    # 每个 bucket：head 404 → create → 应用直传 CORS（逐桶处理）
    assert actions == [
        "head_bucket",
        "create_bucket",
        "put_bucket_cors",
        "head_bucket",
        "create_bucket",
        "put_bucket_cors",
    ]
