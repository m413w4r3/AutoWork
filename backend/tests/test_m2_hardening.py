from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.goodware import NON_DISCRIMINANT_CATEGORIES
from cti_app.domain.reference_corpus import ReferenceCorpusVerdict, assess_reference_feature
from cti_app.infrastructure.database.repositories.core import SqlAlchemyBlobRepository
from cti_app.infrastructure.non_discriminant_patterns import load_non_discriminant_patterns


class _BlobRepository:
    def __init__(self, blob: BlobRecord) -> None:
        self.blob = blob
        self.deleted: list = []

    async def get(self, blob_id):
        return self.blob if blob_id == self.blob.id else None

    async def count_references(self, blob_id):
        return 0

    async def delete(self, blob_id):
        self.deleted.append(blob_id)


class _Uow:
    def __init__(self, blobs: _BlobRepository) -> None:
        self.blobs = blobs
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def commit(self):
        self.committed = True


class _Store:
    def __init__(self) -> None:
        self.deleted: list[BlobDescriptor] = []

    async def delete(self, descriptor):
        self.deleted.append(descriptor)


@pytest.mark.asyncio
async def test_blob_repository_delete_is_concrete_and_catalog_can_call_it() -> None:
    assert "delete" in SqlAlchemyBlobRepository.__dict__
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256="a" * 64,
            size=3,
            mime_type="application/octet-stream",
            logical_bucket="test",
        )
    )
    repository = _BlobRepository(blob)
    store = _Store()
    uow = _Uow(repository)
    service = BlobCatalogService(store, lambda: uow)

    await service.delete_unreferenced(blob.id)

    assert repository.deleted == [blob.id]
    assert store.deleted == [blob.descriptor]
    assert uow.committed


def test_reference_family_maturity_uses_total_eligible_samples() -> None:
    result = assess_reference_feature(
        feature_kind="string",
        normalized_value="marker",
        malware_members=[(uuid4(), "luna")],
        benign_sample_occurrences=0,
        total_eligible_samples_by_family={"luna": 5},
        min_family_samples=5,
    )
    assert result.verdict is ReferenceCorpusVerdict.FAMILY_SPECIFIC
    assert result.malware_sample_count == 1


def test_non_discriminant_registry_has_exact_lookup_and_required_categories() -> None:
    registry = load_non_discriminant_patterns()
    assert registry.lookup("section", ".text") is not None
    assert registry.lookup("section", ".TEXT") is None
    assert set(NON_DISCRIMINANT_CATEGORIES) == {
        "standard_sections",
        "go_runtime",
        "msvc_crt",
        "dotnet_metadata",
        "upx",
        "delphi",
    }
