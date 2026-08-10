from __future__ import annotations

import json
from typing import Any

from cti_app.application.discovery import _validate_partially
from cti_app.application.model_output_normalization import normalize_discovery_output
from tests.test_discovery import research_fixture


def _raw_payload() -> dict[str, Any]:
    return research_fixture().model_dump(mode="json")


def test_normalizes_fence_escaped_key_markdown_url_and_empty_query() -> None:
    payload = _raw_payload()
    payload["queries"] = [""]
    raw = json.dumps(payload, ensure_ascii=False)
    raw = raw.replace('"provisional_title"', '"provisional\\_title"')
    raw = raw.replace(
        "https://vendor.example/reports/muddywater?utm_source=feed",
        "[rapport](https://vendor.example/reports/muddywater?utm_source=feed)",
        1,
    )

    normalized = normalize_discovery_output(f"```json\n{raw}\n```")

    assert normalized.value["queries"] == []
    topic = normalized.value["topics"][0]
    assert topic["provisional_title"].startswith("MuddyWater")
    assert topic["sources"][0]["url"].startswith("https://vendor.example/")
    assert normalized.transformations == (
        "remove_markdown_fence",
        "unescape_underscore_in_keys",
        "unwrap_unambiguous_markdown_url",
        "remove_empty_queries",
    )


def test_extracts_one_json_object_surrounded_by_markdown() -> None:
    raw = "Préambule\n" + json.dumps(_raw_payload(), ensure_ascii=False) + "\nFin"
    normalized = normalize_discovery_output(raw)
    assert "extract_unique_json_object" in normalized.transformations
    assert len(normalized.value["topics"]) == 2


def test_invalid_secondary_source_does_not_remove_valid_topic() -> None:
    payload = _raw_payload()
    topic = payload["topics"][0]
    topic["sources"].append(
        {
            "url": "[ambigu](https://one.example) ou https://two.example",
            "title": "Source ambiguë",
            "publisher": "Unknown",
            "published_at": None,
            "event_date": None,
            "source_role": "independent",
            "citation": None,
        }
    )

    normalized = normalize_discovery_output(json.dumps(payload, ensure_ascii=False))
    result, rejected = _validate_partially(normalized.value)

    assert result.topics
    assert all(source.url.startswith("https://") for source in result.topics[0].sources)
    assert any(item["path"][-2:] == ["2", "url"] for item in rejected)


def test_canonical_url_deduplication_is_deterministic() -> None:
    payload = _raw_payload()
    payload["citations"].append(
        {
            "label": "Duplicate",
            "url": "https://vendor.example/reports/muddywater?utm_campaign=duplicate",
            "excerpt": None,
        }
    )
    normalized = normalize_discovery_output(json.dumps(payload, ensure_ascii=False))
    assert len(normalized.value["citations"]) == 1
    assert "deduplicate_canonical_urls" in normalized.transformations
