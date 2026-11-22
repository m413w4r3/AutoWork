"""Blob reads, uploads, verification, and orphan cleanup for proposal adoption.

Online cleanup removes only unreferenced catalog rows. Content-addressed bytes
remain available for a later idempotent put; physical purging belongs to an
offline retention sweep.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_enrichment_candidate import EditorialEnrichmentCandidate
from cti_app.application.editorial_synthesis.candidate import SynthesisCandidate
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import MAX_ARTIFACT_BYTES
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_enrichment_patch import VISUAL_REVISION_PATCH_SCHEMA_VERSION
from cti_app.domain.editorial_proposal import EditorialProposal

PATCH_BUCKET = "editorial-proposal-patches"
PATCH_MIME = "application/vnd.autowork.editorial-patch+json"
CANONICAL_BUCKET = "production-artifacts-canonical"
CANONICAL_MIME = "application/json"
MAX_PATCH_BYTES = 256 * 1024


class EditorialProposalBlobError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class EditorialProposalBlobManager:
    uow_factory: UnitOfWorkFactory
    blob_store: BlobStore

    async def read_verified_patch(
        self, proposal: EditorialProposal, blob: BlobRecord | None
    ) -> dict[str, Any]:
        if (
            blob is None
            or blob.descriptor.logical_bucket != PATCH_BUCKET
            or blob.descriptor.mime_type != PATCH_MIME
            or blob.descriptor.size > MAX_PATCH_BYTES
            or proposal.patch_sha256 is None
        ):
            raise EditorialProposalBlobError("proposal_patch_unavailable")
        try:
            raw = await self.blob_store.read(blob.descriptor, max_bytes=MAX_PATCH_BYTES)
        except Exception:
            raise EditorialProposalBlobError("proposal_patch_unavailable") from None
        digest = hashlib.sha256(raw).hexdigest()
        if (
            len(raw) != blob.descriptor.size
            or digest != blob.descriptor.sha256
            or digest != proposal.patch_sha256
        ):
            raise EditorialProposalBlobError("proposal_patch_hash_mismatch")
        try:
            patch = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EditorialProposalBlobError("proposal_patch_unavailable") from None
        if (
            not isinstance(patch, dict)
            or patch.get("schema_version")
            not in {"editorial-patch-v1", VISUAL_REVISION_PATCH_SCHEMA_VERSION}
            or patch.get("operation") != proposal.operation
            or patch.get("occurrence_id") != str(proposal.occurrence_id)
            or patch.get("target_id") != proposal.target_id
            or patch.get("before_hash") != proposal.before_hash
            or not isinstance(patch.get("rationale"), str)
            or not patch["rationale"].strip()
        ):
            raise EditorialProposalBlobError("proposal_patch_unavailable")
        return patch

    async def upload_and_verify_candidate(
        self, candidate: SynthesisCandidate | EditorialEnrichmentCandidate
    ) -> BlobDescriptor:
        if len(candidate.canonical_bytes) > MAX_ARTIFACT_BYTES:
            raise EditorialProposalBlobError("proposal_candidate_too_large")
        descriptor: BlobDescriptor | None = None
        try:
            descriptor = await self.blob_store.put(
                BytesIO(candidate.canonical_bytes),
                logical_bucket=CANONICAL_BUCKET,
                mime_type=CANONICAL_MIME,
            )
            expected_sha = hashlib.sha256(candidate.canonical_bytes).hexdigest()
            if (
                candidate.canonical_sha256 != expected_sha
                or descriptor.sha256 != expected_sha
                or descriptor.size != len(candidate.canonical_bytes)
            ):
                raise EditorialProposalBlobError("proposal_candidate_blob_hash_mismatch")
            observed = await self.blob_store.read(descriptor, max_bytes=MAX_ARTIFACT_BYTES)
            if (
                len(observed) != descriptor.size
                or hashlib.sha256(observed).hexdigest() != expected_sha
                or observed != candidate.canonical_bytes
            ):
                raise EditorialProposalBlobError("proposal_candidate_blob_hash_mismatch")
        except Exception as error:
            if descriptor is not None:
                try:
                    await self.cleanup_orphan_blob(descriptor)
                except Exception:
                    pass
            if isinstance(error, EditorialProposalBlobError):
                raise
            raise EditorialProposalBlobError("proposal_candidate_blob_unavailable") from None
        assert descriptor is not None
        return descriptor

    async def cleanup_orphan_blob(self, descriptor: BlobDescriptor) -> bool:
        """Remove an unreferenced catalog row while retaining its object bytes."""
        async with self.uow_factory() as uow:
            await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
            blob = await uow.blobs.get_by_address(descriptor.logical_bucket, descriptor.sha256)
            if blob is None or not _descriptor_matches(blob.descriptor, descriptor):
                return False
            if await uow.blobs.count_references(blob.id):
                return False
            await uow.blobs.delete(blob.id)
            await uow.commit()
        return True


def _descriptor_matches(left: BlobDescriptor, right: BlobDescriptor) -> bool:
    return (
        left.sha256 == right.sha256
        and left.size == right.size
        and left.mime_type == right.mime_type
        and left.logical_bucket == right.logical_bucket
    )
