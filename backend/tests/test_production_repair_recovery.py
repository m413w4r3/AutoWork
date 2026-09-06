"""Recovering historical Q2 values from archived evidence, with no model call.

Every scenario here works only from what was persisted at the time: the
LOT18+ evidence pack, a legacy inline value, or the raw Q2 answer already
archived for its ``ModelRun``.  Any recovered candidate must match the exact
SHA-256 the rejection recorded, and no provider is ever contacted.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.production_repair_payloads import (
    ProductionRepairPayloadResolver,
    RepairPayloadOrigin,
)
from tests.test_production_recovery_support import (
    DOMAIN,
    EMAIL,
    FILE_HASH,
    INDIVIDUAL_REFERENCE,
    IP,
    MODEL_RUN_ID,
    S1_DOMAIN,
    S2_DOMAIN,
    SOURCE_URL,
    URL,
    YARA_BODY,
    RecordingArchive,
    batch_archive,
    individual_archive,
    legacy_entry,
    sha256,
)

# ---------------------------------------------------------------------------
# 1. The LOT18+ evidence pack keeps its exact previous behaviour
# ---------------------------------------------------------------------------


async def test_complete_evidence_pack_is_used_unchanged() -> None:
    archive = individual_archive()
    entry = {
        "source_id": "S1",
        "source_url": SOURCE_URL,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "value": DOMAIN,
        "value_sha256": sha256(DOMAIN),
    }

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=True, value_sha256=sha256(DOMAIN)
    )

    assert payload.value == DOMAIN
    assert payload.origin is RepairPayloadOrigin.REPAIR_EVIDENCE_PACK
    assert payload.available is True
    # The pack answers on its own: no archive was opened.
    assert archive.reads == []


# ---------------------------------------------------------------------------
# 2 & 3. Legacy inline values, trusted only when the hash proves them complete
# ---------------------------------------------------------------------------


async def test_complete_legacy_inline_value_is_verified_by_its_hash() -> None:
    archive = individual_archive()
    entry = legacy_entry(
        value="evil.example", value_hash=sha256("evil.example"), artifact_type="domain"
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256("evil.example")
    )

    assert payload.value == "evil.example"
    assert payload.origin is RepairPayloadOrigin.LEGACY_INLINE_VERIFIED
    assert payload.available is True
    assert archive.reads == []


async def test_truncated_legacy_rule_is_never_declared_exact() -> None:
    """The old diagnostics stored 512 characters; the hash is of the whole."""
    full_body = 'rule Big { strings: $a = "' + ("A" * 900) + '" condition: $a }'
    truncated = full_body[:512]
    assert len(truncated) == 512
    entry = legacy_entry(
        value=truncated,
        value_hash=sha256(full_body),
        artifact_type="yara",
        proposal_kind="rule",
    )
    # The archived output no longer holds this rule either.
    archive = individual_archive()

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(full_body)
    )

    assert payload.value is None
    assert payload.available is False
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE


# ---------------------------------------------------------------------------
# 4 & 5. Recovery from the archived Q2 output, for every indicator type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "artifact_type"),
    [
        (DOMAIN, "domain"),
        (IP, "ip"),
        (URL, "url"),
        (EMAIL, "email"),
        (FILE_HASH, "hash"),
    ],
)
async def test_legacy_indicator_is_recovered_from_the_archived_q2_output(
    value: str, artifact_type: str
) -> None:
    archive = individual_archive()
    entry = legacy_entry(value=None, value_hash=sha256(value), artifact_type=artifact_type)

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(value)
    )

    assert payload.value == value
    assert payload.origin is RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED
    assert payload.available is True
    assert sha256(payload.value) == payload.value_sha256
    # Exactly one archived read; never a model execution.
    assert archive.reads == [INDIVIDUAL_REFERENCE]


# ---------------------------------------------------------------------------
# 6. A detection rule body recovered in full
# ---------------------------------------------------------------------------


async def test_legacy_rule_body_is_recovered_byte_for_byte() -> None:
    archive = individual_archive()
    entry = legacy_entry(
        value=YARA_BODY[:32],
        value_hash=sha256(YARA_BODY),
        artifact_type="yara",
        proposal_kind="rule",
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(YARA_BODY)
    )

    assert payload.value == YARA_BODY
    assert payload.origin is RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED


async def test_recovery_reads_each_model_output_once_for_a_group_of_issues() -> None:
    archive = individual_archive()
    entries = [
        legacy_entry(value=None, value_hash=sha256(value), artifact_type=kind)
        for value, kind in ((DOMAIN, "domain"), (IP, "ip"), (URL, "url"))
    ]

    payloads = await ProductionRepairPayloadResolver(archive).resolve_many(
        entries,
        payload_available=False,
        value_sha256_by_index={
            index: sha256(value) for index, value in enumerate((DOMAIN, IP, URL))
        },
    )

    assert [payload.value for payload in payloads] == [DOMAIN, IP, URL]
    assert archive.reads == [INDIVIDUAL_REFERENCE]


# ---------------------------------------------------------------------------
# 7. A batched Q2 answer resolves the right source, never a neighbour
# ---------------------------------------------------------------------------


async def test_batched_issue_recovers_only_its_own_source_block() -> None:
    archive = batch_archive()
    entry = legacy_entry(
        value=None, value_hash=sha256(S2_DOMAIN), artifact_type="domain", batch_id="B2"
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(S2_DOMAIN)
    )

    assert payload.value == S2_DOMAIN
    assert payload.origin is RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED


async def test_batched_issue_refuses_a_value_from_a_neighbouring_source() -> None:
    """S2's issue must not adopt S1's domain, even though it is in the file."""
    archive = batch_archive()
    entry = legacy_entry(
        value=None, value_hash=sha256(S1_DOMAIN), artifact_type="domain", batch_id="B2"
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(S1_DOMAIN)
    )

    assert payload.value is None
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE


async def test_batched_issue_without_a_batch_id_falls_back_to_its_source_url() -> None:
    archive = batch_archive()
    entry = legacy_entry(
        value=None, value_hash=sha256(S2_DOMAIN), artifact_type="domain", batch_id=None
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(S2_DOMAIN)
    )

    assert payload.value == S2_DOMAIN


# ---------------------------------------------------------------------------
# 8 & 9. Nothing is ever invented
# ---------------------------------------------------------------------------


async def test_a_lookalike_proposal_with_another_hash_is_refused() -> None:
    archive = individual_archive()
    entry = legacy_entry(
        value=None, value_hash=sha256("almost.example.com"), artifact_type="domain"
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256("almost.example.com")
    )

    assert payload.value is None
    assert payload.available is False
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE


async def test_an_inconsistent_proposal_index_refuses_the_candidate() -> None:
    archive = individual_archive()
    # The domain is the second proposal (one fact precedes it), not the tenth.
    entry = legacy_entry(
        value=None,
        value_hash=sha256(DOMAIN),
        artifact_type="domain",
        proposal_index=10,
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(DOMAIN)
    )

    assert payload.value is None
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE


async def test_a_consistent_proposal_index_is_accepted() -> None:
    archive = individual_archive()
    entry = legacy_entry(
        value=None,
        value_hash=sha256(DOMAIN),
        artifact_type="domain",
        proposal_index=2,
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(DOMAIN)
    )

    assert payload.value == DOMAIN


async def test_a_missing_output_archive_is_unavailable_not_an_error() -> None:
    archive = RecordingArchive(
        {
            MODEL_RUN_ID: SimpleNamespace(
                id=MODEL_RUN_ID,
                raw_output_reference="model://q2/gone",
                output_references=("model://q2/gone",),
                parameters={},
            )
        },
        {},
    )
    entry = legacy_entry(value=None, value_hash=sha256(DOMAIN), artifact_type="domain")

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(DOMAIN)
    )

    assert payload.value is None
    assert payload.available is False
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE


async def test_a_missing_model_run_is_unavailable() -> None:
    archive = RecordingArchive({}, {})
    entry = legacy_entry(
        value=None,
        value_hash=sha256(DOMAIN),
        artifact_type="domain",
        model_run_id=uuid4(),
    )

    payload = await ProductionRepairPayloadResolver(archive).resolve(
        entry, payload_available=False, value_sha256=sha256(DOMAIN)
    )

    assert payload.available is False


async def test_without_an_archive_reader_legacy_issues_stay_unavailable() -> None:
    entry = legacy_entry(value=None, value_hash=sha256(DOMAIN), artifact_type="domain")

    payload = await ProductionRepairPayloadResolver().resolve(
        entry, payload_available=False, value_sha256=sha256(DOMAIN)
    )

    assert payload.available is False
    assert payload.origin is RepairPayloadOrigin.UNAVAILABLE
