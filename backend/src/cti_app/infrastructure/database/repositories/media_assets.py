from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.media_assets import MediaAssetKind, MediaAssetManifest
from cti_app.infrastructure.database.models.media_assets import MediaAssetRow


class SqlAlchemyMediaAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, asset: MediaAssetManifest) -> MediaAssetManifest:
        await self._session.execute(
            insert(MediaAssetRow)
            .values(
                asset_id=asset.asset_id,
                kind=asset.kind.value,
                blob_id=asset.blob_id,
                sha256=asset.sha256,
                mime_type=asset.mime_type,
                byte_size=asset.byte_size,
                source=asset.source,
                compiler_name=asset.compiler_name,
                compiler_version=asset.compiler_version,
                policy_version=asset.policy_version,
                provenance=asset.provenance,
                locator=asset.locator,
                decision=asset.decision,
                created_at=asset.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_media_assets_sha256_mime_type")
        )
        stored = await self.get_by_identity(asset.sha256, asset.mime_type)
        if stored is None:
            raise RuntimeError("Media asset insert did not produce a manifest")
        if stored.asset_id != asset.asset_id:
            raise RuntimeError("Media asset identity conflicts with its content address")
        return stored

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        row = await self._session.get(MediaAssetRow, asset_id)
        return _media_asset_from_row(row) if row is not None else None

    async def get_by_identity(self, sha256: str, mime_type: str) -> MediaAssetManifest | None:
        row = await self._session.scalar(
            select(MediaAssetRow).where(
                MediaAssetRow.sha256 == sha256,
                MediaAssetRow.mime_type == mime_type,
            )
        )
        return _media_asset_from_row(row) if row is not None else None


def _media_asset_from_row(row: MediaAssetRow) -> MediaAssetManifest:
    return MediaAssetManifest(
        asset_id=row.asset_id,
        kind=MediaAssetKind(row.kind),
        blob_id=row.blob_id,
        sha256=row.sha256,
        mime_type=row.mime_type,
        byte_size=row.byte_size,
        source=row.source,
        compiler_name=row.compiler_name,
        compiler_version=row.compiler_version,
        policy_version=row.policy_version,
        provenance=row.provenance,
        locator=row.locator,
        decision=row.decision,
        created_at=row.created_at,
    )
