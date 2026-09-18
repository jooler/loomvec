"""P0-INF-06 冒烟：RustFS（S3 兼容）可写读预签名。

用法：uv run python scripts/smoke/smoke_storage.py [--endpoint http://localhost:39000]
"""

from __future__ import annotations

import argparse
import sys
import uuid

import boto3
from botocore.client import Config

ENDPOINT = "http://localhost:39000"
ACCESS_KEY = "loomvec"
SECRET_KEY = "loomvec-secret"
BUCKET = "loomvec-smoke"


def main(endpoint: str, access_key: str, secret_key: str) -> int:
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        s3.head_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=BUCKET)

    key = f"smoke/{uuid.uuid4().hex}.txt"
    payload = b"loomvec smoke test"
    s3.put_object(Bucket=BUCKET, Key=key, Body=payload, ContentType="text/plain")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert body == payload, "读回内容不一致"
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=300
    )
    assert url and "Signature" in url, "预签名 URL 生成失败"
    s3.delete_object(Bucket=BUCKET, Key=key)
    print(f"[smoke:storage] OK — put/get/presign 全通（{endpoint}）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--access-key", default=ACCESS_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.endpoint, args.access_key, args.secret_key))
    except Exception as e:
        print(f"[smoke:storage] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
