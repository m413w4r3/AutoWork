from __future__ import annotations

import json
from datetime import date
from typing import Any

from cti_app.application.model_output_normalization import normalize_discovery_output


def _raw_payload() -> dict[str, Any]:
    return {
        "queries": ["Iran APT July 2026 technical report"],
        "citations": [
            {
                "label": "Vendor report",
                "url": "https://vendor.example/reports/muddywater",
                "excerpt": "Technical report with indicators.",
            }
        ],
        "topics": [
            {
                "provisional_title": "MuddyWater déploie une nouvelle chaîne d'infection",
                "summary": "Une campagne visant plusieurs secteurs iraniens expose une chaîne technique.",
                "novelty": "Nouvelle configuration et nouvelles TTP documentées.",
                "technical_potential": 4,
                "event_date": "2026-07-02",
                "actors": ["MuddyWater"],
                "campaigns": ["Example campaign"],
                "malware": ["ExampleRAT"],
                "cves": ["CVE-2026-0001"],
                "victims": ["organisations publiques"],
                "sectors": ["gouvernement"],
                "countries": ["Iran"],
                "iocs": [],
                "artifact_availability": {
                    "ioc": "yes",
                    "samples": "probable",
                    "configurations": "yes",
                    "pcap": "unknown",
                    "rules": "yes",
                },
                "uncertainties": ["Attribution reprise de la source, non vérifiée."],
                "reasons_for_relevance": ["Rapport technique original"],
                "sources": [
                    {
                        "url": "https://vendor.example/reports/muddywater?utm_source=feed",
                        "title": "MuddyWater technical report",
                        "publisher": "Vendor Research",
                        "published_at": "2026-07-10",
                        "event_date": "2026-07-02",
                        "source_role": "primary",
                        "citation": "Rapport original cité par la recherche.",
                    },
                    {
                        "url": "https://relay.example/news/muddywater",
                        "title": "A new MuddyWater campaign",
                        "publisher": "Security News",
                        "published_at": "2026-07-11",
                        "event_date": "2026-07-02",
                        "source_role": "relay",
                        "citation": "Reprise du rapport original.",
                    },
                ],
            },
            {
                "provisional_title": "MuddyWater déploie une nouvelle chaîne d'infection",
                "summary": "Une campagne visant plusieurs secteurs iraniens expose une chaîne technique.",
                "novelty": "Nouvelle configuration et nouvelles TTP documentées.",
                "technical_potential": 4,
                "event_date": "2026-07-02",
                "actors": ["MuddyWater"],
                "campaigns": ["Example campaign"],
                "malware": ["ExampleRAT"],
                "cves": ["CVE-2026-0001"],
                "victims": ["organisations publiques"],
                "sectors": ["gouvernement"],
                "countries": ["Iran"],
                "iocs": [],
                "artifact_availability": {
                    "ioc": "yes",
                    "samples": "probable",
                    "configurations": "yes",
                    "pcap": "unknown",
                    "rules": "yes",
                },
                "uncertainties": ["Victimologie encore incomplète."],
                "reasons_for_relevance": ["Rapport technique original"],
                "sources": [
                    {
                        "url": "https://vendor.example/reports/muddywater",
                        "title": "MuddyWater technical report (mirror title)",
                        "publisher": "Vendor Research",
                        "published_at": "2026-07-10",
                        "event_date": "2026-07-02",
                        "source_role": "primary",
                        "citation": "Même URL sans paramètre de suivi.",
                    },
                    {
                        "url": "https://cert.example/advisories/42",
                        "title": "CERT advisory on the campaign",
                        "publisher": "National CERT",
                        "published_at": "2026-07-12",
                        "event_date": "2026-07-03",
                        "source_role": "independent",
                        "citation": "Observation indépendante.",
                    },
                ],
            },
        ],
    }


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
