#!/usr/bin/env python3
"""Read-only extraction of canonical publication and render inputs for IOC CSV export."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.config import Settings
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be a 64-character SHA-256")
    return value.casefold()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opinion-id", required=True, type=UUID)
    parser.add_argument("--version", required=True, type=_positive_int)
    parser.add_argument("--opinion-sha256", required=True, type=_sha256)
    parser.add_argument("--base-publication-sha256", required=True, type=_sha256)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("refusing to replace a symbolic-link output path")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _json_bytes(payload)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_runtime(
    *, settings: Settings
) -> tuple[AsyncEngine, AnalystOpinionPreviewService]:
    """Wire the read-only opinion preview path from container environment settings."""
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(blob_store, uow_factory)
    )
    media_asset_store = MediaAssetStore(artifact_store, uow_factory)
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    renderer = PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media_asset_store,
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(),
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
    )
    return engine, AnalystOpinionPreviewService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        renderer=renderer,
    )


async def _run(args: argparse.Namespace) -> int:
    engine, preview_service = build_runtime(settings=Settings(_env_file=None))
    try:
        publication_document, render_data = await preview_service.preview_render_inputs(
            args.opinion_id,
            version=args.version,
            expected_opinion_sha256=args.opinion_sha256,
            expected_base_publication_sha256=args.base_publication_sha256,
        )
        _write_json(
            args.output,
            {
                "opinion_id": str(args.opinion_id),
                "opinion_version": args.version,
                "opinion_sha256": args.opinion_sha256,
                "base_publication_sha256": args.base_publication_sha256,
                "publication_document": serialize_publication_document(
                    publication_document
                ),
                "render_data": render_data,
            },
        )
    except Exception as exc:  # noqa: BLE001 - avoid exposing stored CTI material in diagnostics
        code = getattr(exc, "code", None)
        detail = code if isinstance(code, str) else type(exc).__name__
        print(f"ERROR: opinion render inputs unavailable ({detail})", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(f"JSON d'entrée des IOC écrit dans {args.output}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
