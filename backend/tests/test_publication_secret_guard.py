from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from cti_app.application.publication_secret_guard import (
    PublicationSecretDetectedError,
    configured_operational_secret_values,
    contains_explicit_operational_secrets,
    contains_operational_secrets,
    guard_publication_render_data,
    redact_operational_secrets_with_findings,
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


def test_advisor_filter_allows_host_paths_and_generic_url_query_names() -> None:
    settings = SimpleNamespace(
        workspace_path=Path("/app"),
        data_root=Path("/root"),
        diagnostics_directory=Path("/var/diagnostics"),
        windows_root=Path("C:\\Program Files\\AutoWork"),
    )
    text = (
        "/application /rootkit /var/diagnosticsX "
        "C:\\Program Files\\AutoWork\\samples\\implant.exe "
        "https://evil.example/x?key=abc&token=def&policy=p&expires=123&auth=ghi"
    )

    cleaned, findings = redact_operational_secrets_with_findings(
        {"hypotheses": [{"statement": text}]},
        settings=settings,
        include_host_paths=False,
    )

    assert cleaned["hypotheses"][0]["statement"] == text
    assert findings == ()
    assert not contains_operational_secrets(
        {"hypotheses": [{"statement": text}]},
        settings=settings,
        include_host_paths=False,
    )


def test_secret_findings_include_nested_locations_and_types_without_values() -> None:
    operator_token = "synthetic-operator-token-" + "Q" * 32
    settings = SimpleNamespace(operator_api_token=SecretStr(operator_token))
    signed_url = "https://storage.invalid/object?X-Amz-Signature=synthetic-signature"
    dsn = "postgresql://fixture-user:fixture-password@db.invalid:5432/cti"
    configured_host_url = f"https://{operator_token}.evil.invalid/object?key=arbitrary"
    payload = {
        "hypotheses": [
            {"statement": f"Configured operator token: {operator_token}"},
            {"statement": f"Signed reference: {signed_url}"},
            {"statement": f"Connection reference: {dsn}"},
            {"statement": f"Configured host reference: {configured_host_url}"},
        ]
    }

    _cleaned, findings = redact_operational_secrets_with_findings(
        payload, settings=settings, include_host_paths=False
    )

    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.hypotheses[0].statement", "configured_credential"),
        ("$.hypotheses[1].statement", "signed_url"),
        ("$.hypotheses[2].statement", "dsn"),
        ("$.hypotheses[3].statement", "configured_credential"),
    ]
    safe_findings = repr(findings)
    assert operator_token not in safe_findings
    assert "synthetic-signature" not in safe_findings
    assert "fixture-password" not in safe_findings


@pytest.mark.parametrize(
    "query_name",
    ["sig", "signature", "access_token", "X-Amz-Credential", "X-Amz-Anything", "X-Goog-Whatever"],
)
def test_strong_signature_query_names_are_redacted(query_name: str) -> None:
    url = f"https://storage.invalid/object?{query_name}=synthetic-value"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [{"url": url}]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["links"][0]["url"] == "[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.links[0].url", "signed_url")
    ]


def test_user_password_url_is_redacted_but_username_only_url_is_not() -> None:
    user_password = "https://fixture-user:fixture-password@storage.invalid/object"
    username_only = "https://fixture-user@storage.invalid/object"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [user_password, username_only]},
        settings=SimpleNamespace(),
        include_host_paths=False,
    )

    assert cleaned["links"] == ["[clé masquée]", username_only]
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.links[0]", "signed_url")
    ]


def test_pure_secret_guard_uses_only_explicit_values_and_deterministic_patterns(
    monkeypatch,
) -> None:
    secret = "explicit-credential-only-fixture"
    settings = SimpleNamespace(
        openai_bridge_api_key=SecretStr(secret),
        workspace_path=Path("/synthetic/benign/workspace"),
    )
    monkeypatch.setenv("HOME", "/synthetic/ambient/home")
    monkeypatch.setattr(Path, "cwd", lambda: Path("/synthetic/ambient/cwd"))
    monkeypatch.setattr(Path, "home", lambda: Path("/synthetic/ambient/home"))

    values = configured_operational_secret_values(settings)

    assert values == (secret,)
    assert contains_explicit_operational_secrets(
        {"text": f"configured value: {secret}"}, operational_secret_values=values
    )
    assert contains_explicit_operational_secrets({"text": "OPENAI_BRIDGE_API_KEY=fixture"})
    assert not contains_explicit_operational_secrets(
        {"text": "/synthetic/ambient/cwd /synthetic/ambient/home"},
        operational_secret_values=values,
    )


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
