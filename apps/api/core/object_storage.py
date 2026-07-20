"""AWS S3 object storage helpers."""
from __future__ import annotations

import asyncio
from typing import Any

from core.config import settings


def _endpoint_url() -> str | None:
    endpoint = settings.aws_s3_endpoint.strip()
    if not endpoint:
        return None
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return f"https://{endpoint}"


def _client_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"region_name": settings.aws_s3_region}
    endpoint_url = _endpoint_url()
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        if settings.aws_session_token:
            kwargs["aws_session_token"] = settings.aws_session_token
    return kwargs


def s3_client():
    import boto3

    return boto3.client("s3", **_client_kwargs())


def _create_bucket_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"Bucket": settings.object_storage_bucket}
    if settings.object_storage_bucket_location:
        kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": settings.object_storage_bucket_location
        }
    return kwargs


def ensure_bucket_sync(client=None) -> None:
    client = client or s3_client()
    try:
        client.head_bucket(Bucket=settings.object_storage_bucket)
    except Exception:
        client.create_bucket(**_create_bucket_kwargs())


def check_bucket_sync(client=None) -> None:
    """Verify bucket access without mutating infrastructure.

    Operator health checks must not turn a missing bucket, denied task role, or
    transient provider error into an attempted CreateBucket request. Production
    buckets are Terraform-owned; creation remains confined to explicit setup and
    write paths through :func:`ensure_bucket_sync`.
    """

    client = client or s3_client()
    client.head_bucket(Bucket=settings.object_storage_bucket)


def put_object_sync(key: str, body: bytes, content_type: str, client=None) -> None:
    client = client or s3_client()
    ensure_bucket_sync(client)
    client.put_object(
        Bucket=settings.object_storage_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


def get_object_sync(key: str, client=None) -> bytes:
    client = client or s3_client()
    response = client.get_object(Bucket=settings.object_storage_bucket, Key=key)
    return response["Body"].read()


def delete_object_sync(key: str, client=None) -> None:
    """Delete one object idempotently.

    S3's DeleteObject operation succeeds when a key is already absent, which is
    important for retrying a retention run after a partial failure.
    """

    client = client or s3_client()
    client.delete_object(Bucket=settings.object_storage_bucket, Key=key)


async def ensure_bucket() -> None:
    await asyncio.to_thread(ensure_bucket_sync)


async def check_bucket() -> None:
    await asyncio.to_thread(check_bucket_sync)


async def put_object(key: str, body: bytes, content_type: str) -> None:
    await asyncio.to_thread(put_object_sync, key, body, content_type)


async def get_object(key: str) -> bytes:
    return await asyncio.to_thread(get_object_sync, key)


async def delete_object(key: str) -> None:
    await asyncio.to_thread(delete_object_sync, key)
