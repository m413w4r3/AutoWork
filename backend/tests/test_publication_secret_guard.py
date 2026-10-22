from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from cti_app.application.publication_secret_guard import (
    PublicationSecretDetectedError,
    guard_publication_render_data,
)


def test_guard_allows_sample_crypto_material_and_github_c2_tokens() -> None:
    rc4_key = bytes(range(16)).hex()
    ghp_token = "ghp_" + "a" * 36
    fine_grained_token = "github_pat_" + "b" * 82

    guard_publication_render_data(
        {
            "content_sections": [
                {
                    "type": "analyst_opinion",
                    "blocks": [
                        {"text": f"L'implant conserve la clé RC4 {rc4_key}."},
                        {"text": f"Le canal GitHub utilise le jeton {ghp_token}."},
                        {"text": f"Autre jeton observé : {fine_grained_token}."},
                    ],
                },
                {
                    "type": "technical_materials",
                    "items": [{"type": "rc4_key", "value": rc4_key}],
                },
            ]
        }
    )


def test_guard_refuses_autowork_secrets_without_echoing_values() -> None:
    secret = "autowork_fixture_" + "Z" * 48
    settings = SimpleNamespace(operator_api_token=SecretStr(secret))
    payloads = (
        {"content_sections": [{"text": f"OPERATOR_API_TOKEN={secret}"}]},
        {"content_sections": [{"text": f"Credential value {secret}"}]},
        {"operator_api_token": secret},
    )

    for render_data in payloads:
        with pytest.raises(PublicationSecretDetectedError) as exc_info:
            guard_publication_render_data(render_data, settings=settings)
        assert exc_info.value.code == "publication_secret_detected"
        assert secret not in str(exc_info.value)


def test_guard_still_refuses_service_dsns_and_signed_urls() -> None:
    for value in (
        "postgresql://service:credential@db.internal:5432/cti",
        "https://storage.example/object?X-Amz-Signature=synthetic-signature",
    ):
        with pytest.raises(PublicationSecretDetectedError) as exc_info:
            guard_publication_render_data({"content_sections": [{"text": value}]})
        assert exc_info.value.code == "publication_secret_detected"
        assert value not in str(exc_info.value)


def test_guard_allows_typed_hash_ip_and_domain_iocs() -> None:
    digest = hashlib.sha256(b"fixture hash").hexdigest()
    guard_publication_render_data(
        {
            "title": f"Observed artifact {digest}",
            "content_sections": [
                {"artifact_type": "sha256", "value": digest},
                {"artifact_type": "ip", "value": "203[.]0[.]113[.]19"},
                {"artifact_type": "domain", "value": "fixture[.]example"},
            ],
        }
    )


def test_guard_allows_hash_iocs_in_labeled_narrative_tables() -> None:
    digest = hashlib.sha256(b"synthetic table IOC").hexdigest()
    guard_publication_render_data(
        {
            "content_sections": [
                {
                    "type": "table",
                    "columns": ["Observable", "Type"],
                    "rows": [[digest, "SHA-256"]],
                    "semantic_cells": [
                        [{"style": "technical", "text": digest}],
                        [{"text": "SHA-256"}],
                    ],
                }
            ]
        }
    )


def test_guard_allows_typed_provenance_digests_and_crypto_material_fields() -> None:
    identity_digest = hashlib.sha256(b"synthetic publication identity").hexdigest()
    guard_publication_render_data(
        {
            "document_label": f"Publication de base — SHA-256 {identity_digest}",
            "protocol_figure_provenance": [
                {"capture_sha256": hashlib.sha256(b"capture").hexdigest()}
            ],
        }
    )

    guard_publication_render_data({"crypto_key": bytes(range(32)).hex()})


def test_guard_allows_compact_uuid_ids_in_narrative_text() -> None:
    guard_publication_render_data(
        {"title": f"Publication fixture {uuid4().hex}", "text": f"Source record {uuid4().hex}"}
    )


def test_guard_allows_internal_uuid_hrefs() -> None:
    reference = uuid4()
    guard_publication_render_data({"href": f"autowork://finding/{reference}"})
