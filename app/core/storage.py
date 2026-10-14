"""Tier 1 of memory: the permanent, cheap archive of every conversation turn.

Each turn is written once as a small JSON file and never changed. The database only
keeps a pointer (`Memory.sourceKey`) back to it. Local folder in development, S3 later.
"""

import asyncio
import json
from functools import lru_cache
from pathlib import Path

import boto3

from app.config import settings


class LocalArchive:
    def __init__(self, folder: str):
        self.folder = Path(folder)

    async def put_json(self, key: str, data: dict) -> None:
        path = self.folder / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def get_json(self, key: str) -> dict:
        return json.loads((self.folder / key).read_text(encoding="utf-8"))

    async def list_keys(self, prefix: str) -> list[str]:
        """Every stored key under `prefix`, oldest first (keys start with the date)."""
        root = self.folder / prefix
        if not root.exists():
            return []
        return sorted(p.relative_to(self.folder).as_posix() for p in root.rglob("*.json"))


class S3Archive:
    def __init__(self, bucket: str, region: str):
        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)

    async def put_json(self, key: str, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await asyncio.to_thread(
            self.client.put_object, Bucket=self.bucket, Key=key, Body=body,
            ContentType="application/json",
        )

    async def get_json(self, key: str) -> dict:
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read())

    async def list_keys(self, prefix: str) -> list[str]:
        def collect() -> list[str]:
            keys: list[str] = []
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            return sorted(keys)

        return await asyncio.to_thread(collect)


@lru_cache
def get_archive() -> LocalArchive | S3Archive:
    if settings.storage_backend == "s3":
        return S3Archive(settings.s3_bucket, settings.aws_region)
    return LocalArchive(settings.local_archive_dir)
