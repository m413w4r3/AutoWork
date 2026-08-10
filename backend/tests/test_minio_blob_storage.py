import asyncio
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from minio import Minio
from minio.error import S3Error

from cti_app.infrastructure.blob_storage.minio import MinioBlobStore


class FakeMinioClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.upload_count = 0

    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        try:
            content, metadata = self.objects[(bucket, key)]
        except KeyError as exc:
            raise S3Error(
                cast(Any, None), "NoSuchKey", "missing", key, None, None, bucket, key
            ) from exc
        return SimpleNamespace(
            size=len(content),
            metadata={f"x-amz-meta-{name}": value for name, value in metadata.items()},
        )

    def fput_object(
        self,
        bucket: str,
        key: str,
        path: str,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> SimpleNamespace:
        del content_type
        self.upload_count += 1
        self.objects[(bucket, key)] = (Path(path).read_bytes(), metadata)
        return SimpleNamespace()

    def fget_object(self, bucket: str, key: str, path: str) -> SimpleNamespace:
        Path(path).write_bytes(self.objects[(bucket, key)][0])
        return SimpleNamespace()

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


async def _run_inline(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


@pytest.mark.asyncio
async def test_minio_adapter_is_idempotent_with_simulated_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)
    client = FakeMinioClient()
    store = MinioBlobStore(
        cast(Minio, client), physical_bucket="cti-local", temp_directory=tmp_path / "temp"
    )

    first = await store.put(
        BytesIO(b"simulated minio content"),
        logical_bucket="source-documents",
        mime_type="text/plain",
    )
    second = await store.put(
        BytesIO(b"simulated minio content"),
        logical_bucket="source-documents",
        mime_type="text/plain",
    )

    assert first == second
    assert client.upload_count == 1
    assert await store.exists(first)
    destination = tmp_path / "workspace" / first.sha256
    assert await store.materialize(first, destination) == "copy"
    assert destination.read_bytes() == b"simulated minio content"
