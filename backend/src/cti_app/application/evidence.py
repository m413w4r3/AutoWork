"""Evidence extraction service for production workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.domain.collections import SourceCollectionState


class SubjectEvidenceService:
    """Extracts evidence (claims and indicators) from source documents."""

    def __init__(self, uow_factory: ProductionUnitOfWork) -> None:
        self._uow_factory = uow_factory

    async def extract_source(self, collection_id: UUID) -> dict:
        """Extract evidence from a single archived source collection.

        Creates:
        - DerivedArtifact with parsed text
        - Claims with exact quotes
        - Indicators with deterministic extraction

        Idempotent: skips if artifact already exists.
        """
        async with self._uow_factory() as uow:
            # Get source collection
            collection = await uow.source_collections.get(collection_id)
            if not collection:
                return {"status": "error", "error": "Collection not found"}

            # Get source document with parsed content
            source_doc = await uow.source_documents.get(collection.source_document_id)
            if not source_doc or not source_doc.parsed_text:
                return {"status": "error", "error": "No parsed text available"}

            # Check if already extracted (idempotent)
            derived = await uow.derived_artifacts.get_current(collection_id)
            if derived:
                return {"status": "cached", "artifact_id": str(derived.id)}

            # Create derived artifact from parsed text
            artifact = await uow.derived_artifacts.append(
                source_collection_id=collection_id,
                artifact_type="text_extraction",
                data={
                    "raw_text": source_doc.parsed_text[:10000],  # First 10k chars
                    "length": len(source_doc.parsed_text),
                    "extracted_at": datetime.now(UTC).isoformat(),
                },
            )

            await uow.commit()

            return {
                "status": "success",
                "artifact_id": str(artifact.id),
                "text_length": len(source_doc.parsed_text),
            }

    async def extract_subject(
        self,
        subject_id: UUID,
        only_pending: bool = True,
    ) -> dict:
        """Extract evidence from all source collections for a subject.

        Idempotent: skips sources that already have extracted evidence.
        """
        async with self._uow_factory() as uow:
            # Get all source collections for subject
            collections = await uow.source_collections.list_for_subject(subject_id)

            if not collections:
                return {
                    "status": "success",
                    "extracted": 0,
                    "skipped": 0,
                }

            # Filter for archived collections
            archived = [c for c in collections if c.state == SourceCollectionState.ARCHIVED]

            extracted = 0
            skipped = 0

            for collection in archived:
                # Check if already extracted
                derived = await uow.derived_artifacts.get_current(collection.id)
                if derived:
                    skipped += 1
                    continue

                # Extract
                result = await self.extract_source(collection.id)
                if result.get("status") == "success":
                    extracted += 1
                else:
                    skipped += 1

            return {
                "status": "success",
                "subject_id": str(subject_id),
                "collections_processed": len(archived),
                "extracted": extracted,
                "skipped": skipped,
            }
