import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.classification import TLP, derived_policy
from cti_app.domain.entities import Sample, SampleHashSource, SampleOrigin, SampleState, Subject
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


def _sample(
    subject: Subject,
    blob_id: UUID,
    *,
    origin_kind: SampleOrigin = SampleOrigin.MANUAL,
    state: SampleState = SampleState.QUARANTINED,
    tlp: TLP = TLP.GREEN,
    do_not_submit: bool = False,
    external_llm_allowed: bool = True,
    name: str = "hostile/../name",
) -> Sample:
    return Sample(
        subject_id=subject.id,
        blob_id=blob_id,
        original_name=name,
        origin="textual provenance",
        origin_kind=origin_kind,
        state=state,
        acquired_at=datetime(2026, 8, 7, tzinfo=UTC),
        license_restriction=None,
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_llm_allowed,
        imphash="abc",
        imphash_source=SampleHashSource.LOCAL,
    )


def test_sample_control_enums_and_derived_mixed_policy() -> None:
    assert {item.value for item in SampleOrigin} == {
        "source_seed",
        "vt_seed",
        "vt_hunt_hit",
        "benign_reference",
        "manual",
    }
    assert {item.value for item in SampleState} == {
        "quarantined",
        "review_candidate",
        "validated",
        "rejected",
    }
    subject = Subject(external_id="SAMPLE-ENUM", slug="sample-enum", tlp=TLP.CLEAR)
    members = [
        _sample(subject, subject.id, tlp=TLP.GREEN),
        _sample(subject, subject.id, tlp=TLP.RED, do_not_submit=True),
    ]
    policy = derived_policy(members)
    assert policy.tlp is TLP.RED
    assert policy.do_not_submit is True
    assert policy.external_llm_allowed is True
    members[0].external_llm_allowed = False
    assert derived_policy(members).external_llm_allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_kind", "state", "directory"),
    [
        (SampleOrigin.SOURCE_SEED, SampleState.VALIDATED, "03_samples/original"),
        (SampleOrigin.VT_SEED, SampleState.REVIEW_CANDIDATE, "03_samples/quarantine"),
        (SampleOrigin.VT_HUNT_HIT, SampleState.VALIDATED, "04_hunt/validated/samples"),
        (SampleOrigin.BENIGN_REFERENCE, SampleState.REJECTED, "03_samples/benign"),
    ],
)
async def test_sample_routing_manifest_and_idempotent_rematerialization(
    tmp_path: Path, origin_kind: SampleOrigin, state: SampleState, directory: str
) -> None:
    store = FilesystemBlobStore(tmp_path / "blobs")
    descriptor = await store.put(
        BytesIO(b"sample"),
        logical_bucket="controlled-samples",
        mime_type="application/octet-stream",
    )
    blob = BlobRecord(descriptor=descriptor)
    subject = Subject(external_id="SAMPLE-ROUTE", slug="sample-route", tlp=TLP.RED)
    sample = _sample(subject, blob.id, origin_kind=origin_kind, state=state)
    materializer = SubjectWorkspaceMaterializer(store)
    first = await materializer.materialize(
        subject, [], [sample], {blob.id: blob}, tmp_path / "workspaces"
    )
    second = await materializer.materialize(
        subject, [], [sample], {blob.id: blob}, tmp_path / "workspaces"
    )
    expected = second.path / directory / descriptor.sha256
    assert expected.read_bytes() == b"sample"
    assert first.path == second.path
    manifest = json.loads((second.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["canonical"] is False
    assert manifest["samples"][0]["path"] == f"{directory}/{descriptor.sha256}"
    assert not (second.path / "03_samples/original" / sample.original_name).exists()


@pytest.mark.asyncio
async def test_sample_workspace_refuses_symlink(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path / "blobs")
    descriptor = await store.put(
        BytesIO(b"sample"),
        logical_bucket="controlled-samples",
        mime_type="application/octet-stream",
    )
    blob = BlobRecord(descriptor=descriptor)
    subject = Subject(external_id="SAMPLE-SYMLINK", slug="sample-symlink", tlp=TLP.RED)
    workspace_root = tmp_path / "workspaces"
    materializer = SubjectWorkspaceMaterializer(store)
    await materializer.materialize(subject, [], [], {}, workspace_root)
    target = tmp_path / "outside"
    target.mkdir()
    (workspace_root / subject.slug / "03_samples" / "quarantine").rmdir()
    (workspace_root / subject.slug / "03_samples" / "quarantine").symlink_to(
        target, target_is_directory=True
    )
    with pytest.raises(ValueError, match="symbolic link"):
        await materializer.materialize(
            subject, [], [_sample(subject, blob.id)], {blob.id: blob}, workspace_root
        )
