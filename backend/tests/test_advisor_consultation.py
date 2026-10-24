from __future__ import annotations

import builtins
import hashlib
import io
import os
import socket
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from cti_app.application import advisor_consultation, publication_secret_guard
from cti_app.application.advisor_consultation import (
    PreparationRefusal,
    PreparedConsultation,
    SourceRef,
    SourceRefCatalog,
    prepare,
)


def _projection(*, question: str = "synthetic CTI consultation") -> dict:
    return {
        "consultation_type": "synthetic",
        "question": question,
        "evidence_refs": [],
        "evidence": [],
        "transmitted_refs": [],
    }


def _observation_projection(identifier: str, digest: str) -> dict:
    data = _projection()
    data["evidence_refs"] = [identifier]
    data["evidence"] = [
        {
            "evidence_id": identifier,
            "sha256": digest,
            "object_type": "observation",
            "content_excerpts": [
                {"kind": "text", "title": "Synthetic evidence", "text": "Synthetic body."}
            ],
        }
    ]
    return data


def _assert_conflict(data: dict) -> None:
    result = prepare(data)
    assert isinstance(result, PreparationRefusal)
    assert result.reason_code == "ref_reference_conflict"


def test_ref_sha_conflicts_fail_closed_across_projection_manifest_and_transmitted_refs() -> None:
    identifier = "observation:synthetic-1"
    first_sha = "a" * 64
    second_sha = "b" * 64

    projection_manifest = _observation_projection(identifier, first_sha)
    projection_manifest["transmitted_refs"] = [
        {"kind": "observation", "id": identifier, "sha256": first_sha}
    ]
    projection_manifest["dossier_manifest"] = [
        {"alias": "E1", "kind": "observation", "id": identifier, "sha256": second_sha}
    ]
    _assert_conflict(projection_manifest)

    projection_transmitted = _observation_projection(identifier, first_sha)
    projection_transmitted["transmitted_refs"] = [
        {"kind": "observation", "id": identifier, "sha256": second_sha}
    ]
    _assert_conflict(projection_transmitted)

    manifest_transmitted = _projection()
    manifest_transmitted["dossier_manifest"] = [
        {"alias": "E1", "kind": "observation", "id": identifier, "sha256": first_sha}
    ]
    manifest_transmitted["transmitted_refs"] = [
        {"kind": "observation", "id": identifier, "sha256": second_sha}
    ]
    _assert_conflict(manifest_transmitted)


@pytest.mark.parametrize(
    ("kind", "identifier", "source_identifier"),
    (
        ("measurement", "m001", "evidence-m001"),
        ("fact", "nf001", "evidence-f001"),
    ),
)
def test_manifest_owned_measurement_and_fact_identities_conflict_with_transmitted_refs(
    kind: str, identifier: str, source_identifier: str
) -> None:
    data = _projection()
    data["dossier_manifest"] = [
        {
            "alias": "E1",
            "kind": kind,
            "id": identifier,
            "sha256": "a" * 64,
            "evidence_id": source_identifier,
            "evidence_sha256": "c" * 64,
        }
    ]
    data["transmitted_refs"] = [{"kind": kind, "id": identifier, "sha256": "b" * 64}]

    _assert_conflict(data)


@pytest.mark.parametrize(
    ("kind", "identifier", "source_identifier"),
    (
        ("measurement", "m002", "evidence-m002"),
        ("fact", "nf002", "evidence-f002"),
    ),
)
def test_catalog_preserves_manifest_owned_and_source_measurement_fact_refs(
    kind: str, identifier: str, source_identifier: str
) -> None:
    data = _projection()
    data["dossier_manifest"] = [
        {
            "alias": "E1",
            "kind": kind,
            "id": identifier,
            "sha256": "b" * 64,
            "evidence_id": source_identifier,
            "evidence_sha256": "c" * 64,
        }
    ]
    data["transmitted_refs"] = [{"kind": kind, "id": identifier, "sha256": "b" * 64}]

    result = prepare(data)

    assert isinstance(result, PreparedConsultation)
    assert SourceRef(kind, identifier, "b" * 64) in result.source_catalog.refs
    assert SourceRef("observation", source_identifier, "c" * 64) in result.source_catalog.refs
    assert result.source_catalog.resolve_alias("E1") is None


def test_aliases_resolve_only_to_transmitted_refs_and_matching_kinds() -> None:
    identifier = "observation:manifest-only"
    data = _observation_projection(identifier, "c" * 64)
    prepared = prepare(data)

    assert isinstance(prepared, PreparedConsultation)
    assert prepared.source_catalog.resolve_alias("E1") is None
    assert prepared.source_catalog.resolve_alias(identifier) is None

    transmitted = _observation_projection(identifier, "c" * 64)
    transmitted["transmitted_refs"] = [
        {"kind": "observation", "id": identifier, "sha256": "c" * 64}
    ]
    resolved = prepare(transmitted)
    assert isinstance(resolved, PreparedConsultation)
    assert resolved.source_catalog.resolve_alias("E1") == SourceRef(
        kind="observation", id=identifier, sha256="c" * 64
    )
    assert resolved.source_catalog.resolve_alias("E1", expected_kind="finding") is None


def test_stale_and_restricted_manifest_aliases_do_not_resolve() -> None:
    identifier = "observation:old-or-restricted"
    data = _projection()
    data["dossier_manifest"] = [
        {
            "alias": "E1",
            "kind": "observation",
            "id": identifier,
            "sha256": "e" * 64,
            "origin": "stale_or_restricted_projection",
        }
    ]
    data["transmitted_refs"] = [
        {"kind": "observation", "id": "observation:other", "sha256": "f" * 64}
    ]
    data["disclosure_restricted_refs"] = [identifier]

    result = prepare(data)
    assert isinstance(result, PreparedConsultation)
    assert result.source_catalog.resolve_alias("E1") is None


def test_restricted_projection_ref_is_not_resolvable_even_if_marked_transmitted() -> None:
    identifier = "c7aaf75b-7fa8-44be-b4aa-6b5297cc77fa"
    data = _observation_projection(identifier, "d" * 64)
    data["disclosure_restricted_refs"] = [identifier]
    data["transmitted_refs"] = [{"kind": "observation", "id": identifier, "sha256": "d" * 64}]

    result = prepare(data)

    assert isinstance(result, PreparedConsultation)
    assert result.source_catalog.resolve_alias("E1") is None


def test_versioned_review_alias_resolves_to_exact_source_ref() -> None:
    opinion_id = str(uuid4())
    digest = "9" * 64
    source_ref = f"opinion:{opinion_id}:v3:{digest}:key_findings"
    review_text = "Synthetic versioned review target."
    data = _projection(question="Relire le texte cible.")
    data["consultation_type"] = "review_text"
    data["units"] = [
        {
            "unit_id": "U1",
            "opinion_id": opinion_id,
            "opinion_version": 3,
            "opinion_sha256": digest,
            "source_ref": source_ref,
            "block_type": "key_findings",
            "text": review_text,
            "reviewed_text_sha256": hashlib.sha256(review_text.encode("utf-8")).hexdigest(),
        }
    ]

    result = prepare(data)

    assert isinstance(result, PreparedConsultation)
    assert result.source_catalog.resolve_alias("T1", expected_kind="opinion") == SourceRef(
        kind="opinion", id=source_ref, sha256=digest
    )
    assert result.source_catalog.resolve_alias(opinion_id) is None
    assert result.source_catalog.resolve_alias("T1", expected_kind="finding") is None


def test_public_catalog_rejects_conflicting_digests_and_filters_non_transmitted_aliases() -> None:
    first = SourceRef("observation", "observation:collision", "1" * 64)
    second = SourceRef("observation", "observation:collision", "2" * 64)
    with pytest.raises(ValueError, match="conflicting SHA-256"):
        SourceRefCatalog(
            refs=(first, second),
            transmitted_refs=(first, second),
            aliases=(("E1", first),),
        )

    catalog = SourceRefCatalog(
        refs=(first,),
        transmitted_refs=(),
        aliases=(("E1", first),),
    )
    assert catalog.resolve_alias("E1") is None


def test_public_catalog_drops_ambiguous_alias_for_two_transmitted_identities() -> None:
    observation = SourceRef("observation", "observation:one", "1" * 64)
    finding = SourceRef("finding", "finding:two", "2" * 64)
    catalog = SourceRefCatalog(
        refs=(observation, finding),
        transmitted_refs=(observation, finding),
        aliases=(("E1", observation), ("E1", finding)),
    )

    assert catalog.resolve_alias("E1") is None


def test_opaque_configured_secret_is_redacted_without_loading_dotenv(monkeypatch) -> None:
    secret = "opaque-configured-secret-75b2ac4f"
    settings = SimpleNamespace(operator_api_token=SecretStr(secret))

    def settings_without_dotenv(**kwargs):
        assert kwargs == {"_env_file": None}
        return settings

    monkeypatch.setattr(advisor_consultation, "Settings", settings_without_dotenv, raising=False)
    result = prepare(_projection(question=f"Review this ordinary prose: {secret}"))

    assert isinstance(result, PreparedConsultation)
    assert secret.encode("utf-8") not in result.prompt_utf8


def test_prompt_utf8_and_sha256_are_deterministic_without_newline_normalization() -> None:
    question = "Vérifier l'artefact Ω.\r\nGarder cette ligne exacte."
    first = prepare(_projection(question=question))
    second = prepare(_projection(question=question))

    assert isinstance(first, PreparedConsultation)
    assert isinstance(second, PreparedConsultation)
    assert first.prompt_utf8 == second.prompt_utf8
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.prompt_sha256 == hashlib.sha256(first.prompt_utf8).hexdigest()
    assert question.encode("utf-8") in first.prompt_utf8
    assert b"\r\n" in first.prompt_utf8

    with pytest.raises(FrozenInstanceError):
        attribute_name = "refs"
        setattr(first.source_catalog, attribute_name, ())


def test_prepare_accepts_pure_mapping_and_does_not_open_files_start_processes_or_connect(
    monkeypatch,
) -> None:
    def unexpected_io(*args, **kwargs):
        raise AssertionError("prepare attempted I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(io, "open", unexpected_io)
    monkeypatch.setattr(os, "open", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    monkeypatch.setattr(Path, "resolve", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)
    monkeypatch.setattr(socket.socket, "connect", unexpected_io)
    monkeypatch.setattr(subprocess, "run", unexpected_io)
    monkeypatch.setattr(subprocess, "Popen", unexpected_io)
    monkeypatch.setattr(publication_secret_guard, "get_settings", unexpected_io)

    assert isinstance(prepare(MappingProxyType(_projection())), PreparedConsultation)


def test_secret_guard_redactions_and_preflight_omissions_are_observable() -> None:
    credential = "postgresql://service:synthetic-credential@db.internal:5432/cti"
    secret_projection = _projection(question=f"Review this setting: {credential}")
    prepared = prepare(secret_projection)
    assert isinstance(prepared, PreparedConsultation)
    assert prepared.redacted_secret_count > 0
    assert credential.encode("utf-8") not in prepared.prompt_utf8

    incomplete = _projection(question="Vérifier les preuves disponibles.")
    incomplete["consultation_type"] = "plan_investigation"
    refusal = prepare(incomplete)
    assert isinstance(refusal, PreparationRefusal)
    assert refusal.reason_code.startswith("evidence_preflight_")
    assert refusal.completeness is not None
    assert refusal.completeness["counts"]["missing"] > 0


def test_malformed_projection_returns_a_fail_closed_refusal() -> None:
    result = prepare({"consultation_type": "review_text", "units": None})

    assert isinstance(result, PreparationRefusal)
    assert result.reason_code == "invalid_projection"
