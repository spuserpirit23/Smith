"""Object storage: local disk (default, $0) or any S3-compatible service."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..base import ProviderError


class LocalStore:
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ProviderError("invalid key")
        return p

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise FileNotFoundError(key)
        return await asyncio.to_thread(p.read_bytes)

    async def delete_prefix(self, prefix: str) -> int:
        p = self._path(prefix)
        if p.is_dir():
            n = sum(1 for f in p.rglob("*") if f.is_file())
            await asyncio.to_thread(shutil.rmtree, p)
            return n
        if p.is_file():
            p.unlink()
            return 1
        return 0


class S3Store:
    """Works with AWS S3, Cloudflare R2, MinIO, Backblaze B2, GCS (interop) via endpoint_url."""

    name = "s3"

    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None):
        import boto3

        if not bucket:
            raise ProviderError("S3_BUCKET is empty")
        self.bucket = bucket
        self._client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url or None)

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key: str) -> bytes:
        def call() -> bytes:
            try:
                return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except self._client.exceptions.NoSuchKey as e:
                raise FileNotFoundError(key) from e

        return await asyncio.to_thread(call)

    async def delete_prefix(self, prefix: str) -> int:
        def call() -> int:
            n = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                    n += len(keys)
            return n

        return await asyncio.to_thread(call)
