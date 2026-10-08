from __future__ import annotations

import json
from pathlib import Path

import pytest

from cti_app.domain.legitimate_services import (
    LegitimateServiceCategory,
    LegitimateServicePolicyError,
    validate_legitimate_services_document,
)
from cti_app.domain.publication import ArtifactType
from cti_app.infrastructure.legitimate_services import load_legitimate_services


def test_policy_is_versioned_and_entries_have_categories_and_reasons() -> None:
    registry = load_legitimate_services()

    by_host = {entry.hostname: entry for entry in registry.entries}
    assert {
        "api.telegram.org",
        "telegram.org",
        "gofile.io",
        "outlook.office.com",
        "outlook.office365.com",
        "outlook.live.com",
        "login.microsoftonline.com",
        "graph.microsoft.com",
        "ams1.vultrobjects.com",
        "sgp1.vultrobjects.com",
        "discord.com",
        "api.github.com",
        "github.com",
        "raw.githubusercontent.com",
        "www.google.com",
        "drive.google.com",
        "docs.google.com",
        "dropbox.com",
        "onedrive.live.com",
        "1drv.ms",
        "pastebin.com",
        "transfer.sh",
    } <= by_host.keys()
    assert by_host["api.telegram.org"].category is LegitimateServiceCategory.MESSAGING_API
    assert by_host["api.telegram.org"].reason
    assert by_host["ams1.vultrobjects.com"].category is (
        LegitimateServiceCategory.GENERIC_OBJECT_HOSTING
    )
    assert by_host["sgp1.vultrobjects.com"].reason
    assert "micbucket.ams1.vultrobjects.com" not in by_host


@pytest.mark.parametrize(
    ("artifact_type", "value"),
    (
        (ArtifactType.DOMAIN, "Api.Telegram.Org."),
        (ArtifactType.DOMAIN, "gofile.io"),
        (ArtifactType.URL, "https://api.telegram.org"),
        (ArtifactType.URL, "https://API.TELEGRAM.ORG/"),
        (ArtifactType.URL, "https://api.telegram.org:443/"),
        (ArtifactType.URL, "https://api.telegram.org:8443"),
        (ArtifactType.URL, "https://ams1.vultrobjects.com"),
        (ArtifactType.URL, "https://sgp1.vultrobjects.com/"),
        (ArtifactType.DOMAIN, "s3.amazonaws.com"),
    ),
)
def test_policy_matches_only_generic_service_hosts(artifact_type: ArtifactType, value: str) -> None:
    registry = load_legitimate_services()

    assert registry.match_indicator(artifact_type, value) is not None


@pytest.mark.parametrize(
    ("artifact_type", "value"),
    (
        (ArtifactType.URL, "https://api.telegram.org/bot123:secret/sendMessage"),
        (ArtifactType.URL, "https://api.telegram.org/?chat_id=1"),
        (ArtifactType.URL, "https://api.telegram.org/#section"),
        (ArtifactType.URL, "https://api.telegram.org/?"),
        (ArtifactType.URL, "https://api.telegram.org/#"),
        (ArtifactType.URL, "https://github.com/org/repo/tree/main"),
        (ArtifactType.URL, "https://s3.amazonaws.com/oracle-challenge/payload.exe"),
        (ArtifactType.URL, "https://micbucket.ams1.vultrobjects.com/MSCache.exe"),
        (ArtifactType.DOMAIN, "micbucket.ams1.vultrobjects.com"),
        (ArtifactType.DOMAIN, "oracle-challenge.s3.amazonaws.com"),
        (ArtifactType.DOMAIN, "sample.azurewebsites.net"),
        (ArtifactType.DOMAIN, "attacker.api.telegram.org"),
        (ArtifactType.DOMAIN, "api.telegram.org.evil.test"),
    ),
)
def test_policy_preserves_specific_services_and_non_exact_hosts(
    artifact_type: ArtifactType, value: str
) -> None:
    registry = load_legitimate_services()

    assert registry.match_indicator(artifact_type, value) is None


def test_policy_validator_rejects_duplicate_or_non_host_entries(tmp_path: Path) -> None:
    duplicate = {
        "schema_version": "legitimate-services-v1",
        "entries": [
            {
                "hostname": "api.telegram.org",
                "category": "messaging_api",
                "reason": "Public messaging API.",
            },
            {
                "hostname": "api.telegram.org",
                "category": "messaging_api",
                "reason": "Duplicate host.",
            },
        ],
    }
    with pytest.raises(LegitimateServicePolicyError):
        validate_legitimate_services_document(duplicate)

    invalid = {
        "schema_version": "legitimate-services-v1",
        "entries": [
            {
                "hostname": "*.vultrobjects.com",
                "category": "generic_object_hosting",
                "reason": "A wildcard would also match named buckets.",
            }
        ],
    }
    path = tmp_path / "legitimate-services.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(LegitimateServicePolicyError):
        load_legitimate_services(path)
