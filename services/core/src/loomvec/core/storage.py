"""P0-CORE-02 存储适配器：RustFS / 任意 S3 兼容存储的客户端封装。

bucket 约定：
- `storage.bucket_raw`       原始文件（用户上传的源文件）
- `storage.bucket_derived`   派生物（缩略图 / 关键帧 / 解析产物等）

boto3 为同步 SDK；put/get 等阻塞操作统一经 `asyncio.to_thread` 暴露异步
接口，供 async 服务层调用。大文件走 multipart 系列方法。
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from loomvec.core.config import StorageSettings
from loomvec.core.errors import UpstreamUnavailableError


class ObjectStorage:
    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    # ---------- bucket 管理 ----------

    def ensure_buckets(self) -> None:
        """确保 bucket 存在，并应用浏览器直传所需的桶级 CORS（幂等，随启动覆盖）。

        预签名 URL 的鉴权在查询串签名中，直传请求不携带 cookie，
        因此 AllowedOrigins 允许通配（生产经 storage.cors_allowed_origins 收紧）。
        """

        for bucket in (self._settings.bucket_raw, self._settings.bucket_derived):
            try:
                self._client.head_bucket(Bucket=bucket)
            except ClientError:
                self._client.create_bucket(Bucket=bucket)
            self._client.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedOrigins": self._settings.cors_allowed_origins,
                            # PUT 直传 + GET 预签名下载分发
                            "AllowedMethods": ["GET", "PUT"],
                            "AllowedHeaders": ["*"],
                            "ExposeHeaders": ["ETag"],
                            "MaxAgeSeconds": 3600,
                        }
                    ]
                },
            )

    # ---------- 基础对象操作 ----------

    async def put_object(
        self, bucket: str, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        def _put() -> None:
            extra: dict[str, Any] = {"ContentType": content_type} if content_type else {}
            self._client.put_object(Bucket=bucket, Key=key, Body=data, **extra)

        await asyncio.to_thread(self._guarded, _put)

    async def get_object(self, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            resp = self._client.get_object(Bucket=bucket, Key=key)
            with resp["Body"] as body:
                return body.read()

        return await asyncio.to_thread(self._guarded, _get)

    async def delete_object(self, bucket: str, key: str) -> None:
        await asyncio.to_thread(
            self._guarded, lambda: self._client.delete_object(Bucket=bucket, Key=key)
        )

    async def head_object(self, bucket: str, key: str) -> dict[str, Any] | None:
        def _head() -> dict[str, Any] | None:
            try:
                return self._client.head_object(Bucket=bucket, Key=key)
            except ClientError as e:
                if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                    return None
                raise

        return await asyncio.to_thread(self._guarded, _head)

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        """按前缀批量删除（空间删除级联）；返回删除对象数。"""

        def _purge() -> int:
            deleted = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                keys = [c["Key"] for c in page.get("Contents", [])]
                if not keys:
                    continue
                resp = self._client.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True}
                )
                deleted += len(keys) - len(resp.get("Errors", []))
            return deleted

        return await asyncio.to_thread(self._guarded, _purge)

    # ---------- 预签名（上传直传 / 下载分发，流量不过 API 服务） ----------

    def presign_put(
        self, bucket: str, key: str, expires_in: int = 3600, content_type: str | None = None
    ) -> str:
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._guarded(
            lambda: self._client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=expires_in
            )
        )

    def presign_get(self, bucket: str, key: str, expires_in: int = 3600) -> str:
        return self._guarded(
            lambda: self._client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
            )
        )

    # ---------- multipart（大文件断点续传） ----------

    def create_multipart(self, bucket: str, key: str, content_type: str | None = None) -> str:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if content_type:
            kwargs["ContentType"] = content_type
        return self._guarded(lambda: self._client.create_multipart_upload(**kwargs)["UploadId"])

    def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        resp = self._guarded(
            lambda: self._client.upload_part(
                Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=data
            )
        )
        return resp["ETag"]

    def presign_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, expires_in: int = 86400
    ) -> str:
        return self._guarded(
            lambda: self._client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_in,
            )
        )

    def complete_multipart(
        self, bucket: str, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        self._guarded(
            lambda: self._client.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
            )
        )

    def abort_multipart(self, bucket: str, key: str, upload_id: str) -> None:
        self._guarded(
            lambda: self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        )

    # ---------- 内部 ----------

    def _guarded(self, fn):
        """把底层连接错误收敛为统一的 UpstreamUnavailableError。"""
        try:
            return fn()
        except (ClientError, ConnectionError, TimeoutError) as e:
            raise UpstreamUnavailableError(upstream="storage", reason=str(e)) from e
