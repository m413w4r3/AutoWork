from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
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
    "query",
    [
        "X-Amz-Signature=synthetic-value",
        "X-Amz-Credential=synthetic-value",
        "X-Amz-Security-Token=synthetic-value",
        "X-Goog-Signature=synthetic-value",
        "X-Goog-Credential=synthetic-value",
        "GoogleAccessId=synthetic-value",
        "sig=synthetic-value&sv=2024-01-01&se=2027-01-01",
        "signature=synthetic-value&Expires=123&Key-Pair-Id=fixture",
    ],
)
def test_strong_signature_query_sets_are_redacted(query: str) -> None:
    url = f"https://storage.invalid/object?{query}"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [{"url": url}]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["links"][0]["url"] == "[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.links[0].url", "signed_url")
    ]


@pytest.mark.parametrize(
    "query",
    [
        "access_token=attacker-token",
        "sig=attacker-value",
        "signature=attacker-value",
        "X-Amz-Anything=attacker-value",
        "X-Goog-Whatever=attacker-value",
    ],
)
def test_generic_cti_query_parameters_are_not_redacted(query: str) -> None:
    url = f"https://evil.example/x?{query}"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [{"url": url}]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["links"][0]["url"] == url
    assert findings == ()


@pytest.mark.parametrize("passes", [1, 2, 3])
def test_configured_secret_is_blocked_when_percent_encoded_in_generic_query(passes: int) -> None:
    secret = "fixture/credential+value"
    settings = SimpleNamespace(operator_api_token=SecretStr(secret))
    encoded_secret = secret
    for _ in range(passes):
        encoded_secret = quote(encoded_secret, safe="")
    url = f"https://evil.example/x?token={encoded_secret}"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [{"url": url}]}, settings=settings, include_host_paths=False
    )

    assert cleaned["links"][0]["url"] == "[clé masquée]", "encoded configured value remained"
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.links[0].url", "configured_credential")
    ]


@pytest.mark.parametrize("encoding", ["hex", "base64"])
def test_configured_secret_is_redacted_when_hex_or_base64_encoded(encoding: str) -> None:
    import base64

    secret = "fixture-secret/value"
    encoded_secret = (
        secret.encode().hex()
        if encoding == "hex"
        else base64.b64encode(secret.encode()).decode("ascii")
    )
    settings = SimpleNamespace(operator_api_token=SecretStr(secret))

    cleaned, findings = redact_operational_secrets_with_findings(
        {"evidence": [f"sample={encoded_secret}"]},
        settings=settings,
        include_host_paths=False,
    )

    assert cleaned["evidence"][0] == "sample=[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.evidence[0]", "encoded_credential")
    ]


@pytest.mark.parametrize("passes", [1, 2])
def test_percent_encoded_signed_url_is_redacted(passes: int) -> None:
    signed_url = "https://storage.invalid/object?X-Amz-Signature=synthetic-value"
    encoded_url = signed_url
    for _ in range(passes):
        encoded_url = quote(encoded_url, safe="")

    cleaned, findings = redact_operational_secrets_with_findings(
        {"links": [{"url": encoded_url}]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["links"][0]["url"] == "[clé masquée]", "encoded signed URL remained"
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


def test_service_dsn_without_userinfo_is_allowed() -> None:
    dsn = "postgresql://db.invalid:5432/cti"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"references": [dsn]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["references"][0] == dsn
    assert findings == ()


def test_service_dsn_with_password_query_is_redacted() -> None:
    dsn = "postgresql://db.invalid:5432/cti?user=fixture-user&password=fixture-password"

    cleaned, findings = redact_operational_secrets_with_findings(
        {"references": [dsn]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["references"][0] == "[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [("$.references[0]", "dsn")]


def test_configured_service_dsn_without_userinfo_remains_blocked() -> None:
    dsn = "postgresql://db.invalid:5432/cti"
    settings = SimpleNamespace(postgres_dsn=dsn)

    cleaned, findings = redact_operational_secrets_with_findings(
        {"references": [dsn]}, settings=settings, include_host_paths=False
    )

    assert cleaned["references"][0] == "[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [("$.references[0]", "dsn")]


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://fixture-user@db.invalid:5432/cti",
        "postgresql://fixture-user:fixture-password@db.invalid:5432/cti",
    ],
)
def test_service_dsn_with_userinfo_is_redacted(dsn: str) -> None:
    cleaned, findings = redact_operational_secrets_with_findings(
        {"references": [dsn]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["references"][0] == "[clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [("$.references[0]", "dsn")]


@pytest.mark.parametrize("passes", [1, 2])
def test_percent_encoded_service_dsn_with_userinfo_is_redacted(passes: int) -> None:
    dsn = "postgresql://fixture-user:fixture-password@db.invalid:5432/cti"
    encoded_dsn = dsn
    for _ in range(passes):
        encoded_dsn = quote(encoded_dsn, safe="")

    cleaned, findings = redact_operational_secrets_with_findings(
        {"references": [encoded_dsn]}, settings=SimpleNamespace(), include_host_paths=False
    )

    assert cleaned["references"][0] == "[clé masquée]", "encoded DSN remained"
    assert [(item.location, item.secret_type) for item in findings] == [("$.references[0]", "dsn")]


def test_finding_paths_mask_configured_and_encoded_dictionary_keys() -> None:
    import base64

    secret = "fixture-key"
    encoded_secret = "fixture/key"
    settings = SimpleNamespace(
        operator_api_token=SecretStr(secret),
        operator_client_api_token=SecretStr(encoded_secret),
    )
    keys = (
        secret,
        quote(encoded_secret, safe=""),
        encoded_secret.encode().hex(),
        base64.b64encode(encoded_secret.encode()).decode(),
        f"prefix-{secret}-suffix",
        f"prefix-{quote(encoded_secret, safe='')}-suffix",
        f"prefix-{encoded_secret.encode().hex()}-suffix",
        f"prefix-{base64.b64encode(encoded_secret.encode()).decode()}-suffix",
    )
    payload = {"nested": {key: "ordinary fixture evidence" for key in keys}}

    _cleaned, findings = redact_operational_secrets_with_findings(
        payload, settings=settings, include_host_paths=False
    )

    locations = tuple(item.location for item in findings)
    assert locations == tuple(f"$.nested[<key#{index}>]" for index in range(len(keys))), (
        "diagnostic path was not masked"
    )
    assert all(
        secret not in location and encoded_secret not in location for location in locations
    ), "credential appeared in a path"


def test_finding_path_masks_secret_fragments_in_mapping_keys() -> None:
    secret = "fixture-credential-fragment"
    payload = {
        f"prefix-{secret[:8]}-suffix": {
            "references": ["postgresql://fixture-user@db.invalid:5432/cti"]
        }
    }

    _cleaned, findings = redact_operational_secrets_with_findings(
        payload,
        settings=SimpleNamespace(operator_api_token=SecretStr(secret)),
        include_host_paths=False,
    )

    assert [(item.location, item.secret_type) for item in findings] == [
        ("$[<key#0>].references[0]", "dsn")
    ]
    assert secret[:8] not in findings[0].location, "credential fragment appeared in path"


def test_publication_guard_refuses_configured_credential_in_dictionary_key() -> None:
    secret = "fixture-key"
    settings = SimpleNamespace(operator_api_token=SecretStr(secret))

    with pytest.raises(PublicationSecretDetectedError) as exc_info:
        guard_publication_render_data({secret: "ordinary fixture evidence"}, settings=settings)

    assert exc_info.value.location == "render-data[<key#0>]", "credential key leaked into path"
    assert secret not in str(exc_info.value), "credential appeared in refusal"


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
