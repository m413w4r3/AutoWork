#!/usr/bin/env python3
"""Final validation: exactly mirror test_patch1_validation logic."""

import sys
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch, DiscoverySourceMode

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "discovery_golden"
EDITION_ID = UUID("12345678-1234-5678-1234-567812345678")
REQUEST_HASH = "a" * 64


def load_and_consolidate():
    """Load 4 runs and consolidate."""
    run_files = ["run_1.md", "run_2.md", "run_3.md", "run_4.md"]
    batches = []

    for run_file in run_files:
        path = FIXTURES_DIR / run_file
        report_text = path.read_text(encoding="utf-8")

        parsed = parse_discovery_report(
            report_text,
            visible_citations=(),
            period_start=date(2026, 7, 1),
            period_end=date(2026, 8, 17),
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )

        batch = DiscoveryBatch(
            edition_id=EDITION_ID,
            request_hash=REQUEST_HASH,
            complementary_axis="initial",
            queries=(),
            citations=parsed.citations,
            candidates=parsed.candidates,
            discovery_model_run_id=uuid4(),
            structuring_model_run_id=uuid4(),
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
            report_sha256=parsed.report_sha256,
            parser_version="chatgpt-markdown-v2",
            parsing_status=parsed.status,
            parsing_warnings=parsed.warnings,
            unattached_visible_citations=parsed.unattached_visible_citations,
            source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
            source_coverage_complete=True,
        )

        batches.append(batch)

    return consolidate_discovery_batches(batches)


def categorize_results(consolidated):
    """Use exact same categorization as test."""
    results = {
        "tag182": [],
        "cavern": [],
        "aa26_097a": [],
        "dindoor": [],
        "olalampo": [],
        "chainshell": [],
        "iconcat": [],
        "gigawiper": [],
        "redkitten": [],
        "dust_specter": [],
        "other": [],
    }

    for c in consolidated:
        title = c.representative.title.lower()
        found = False

        for key, pattern_list in [
            ("tag182", ["tag", "182"]),
            ("cavern", ["cavern", "manticore"]),
            ("aa26_097a", ["aa26", "plc", "programmable logic"]),
            ("dindoor", ["dindoor", "seedworm"]),
            ("olalampo", ["olalampo"]),
            ("chainshell", ["chainshell"]),
            ("iconcat", ["iconcat"]),
            ("gigawiper", ["gigawiper", "bluerabbit"]),
            ("redkitten", ["redkitten"]),
            ("dust_specter", ["dust", "specter"]),
        ]:
            if all(p in title for p in pattern_list):
                results[key].append(c)
                found = True
                break

        if not found:
            results["other"].append(c)

    return results


def main():
    consolidated = load_and_consolidate()
    results = categorize_results(consolidated)

    print("=" * 70)
    print("FINAL VALIDATION RESULTS")
    print("=" * 70 + "\n")

    expectations = [
        ("tag182", 1),
        ("cavern", 1),
        ("dindoor", 1),
        ("olalampo", 1),
        ("chainshell", 1),
        ("iconcat", 1),
    ]

    for key, expected in expectations:
        actual = len(results[key])
        status = "✅" if actual == expected else "❌"
        print(f"{status} {key.upper():15} expected={expected:2}, got={actual:2}")

    print(f"\n\nDETAIL FOR OLALAMPO:")
    for c in results["olalampo"]:
        print(f"  - {c.representative.title}")
        print(f"    ID: {c.representative.id}")
        print(f"    Members: {len(c.member_references)}")

    print(f"\n\nDETAIL FOR CHAINSHELL:")
    for c in results["chainshell"]:
        print(f"  - {c.representative.title}")
        print(f"    ID: {c.representative.id}")
        print(f"    Members: {len(c.member_references)}")

    print(f"\n\nDETAIL FOR ICONCAT:")
    for c in results["iconcat"]:
        print(f"  - {c.representative.title}")
        print(f"    ID: {c.representative.id}")
        print(f"    Members: {len(c.member_references)}")


if __name__ == "__main__":
    main()
