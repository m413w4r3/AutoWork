"""
Patch 1 End-to-End Validation: Parse 4 real ChatGPT runs → Consolidate → Evaluate
"""

import sys
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch, DiscoverySourceMode

# Paths to golden fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "discovery_golden"
EDITION_ID = UUID("12345678-1234-5678-1234-567812345678")
REQUEST_HASH = "a" * 64


def load_golden_batches():
    """Load and parse all 4 golden runs."""
    run_files = [
        "run_1.md",
        "run_2.md",
        "run_3.md",
        "run_4.md",
    ]

    batches = []
    print("\n" + "="*70)
    print("PHASE 1: PARSING")
    print("="*70)

    for run_file in run_files:
        path = FIXTURES_DIR / run_file
        if not path.exists():
            raise FileNotFoundError(f"Golden fixture missing: {path}")

        report_text = path.read_text(encoding="utf-8")
        print(f"\n📖 Parsing {run_file}...")

        try:
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
            print(f"   ✅ {len(batch.candidates)} candidates parsed")
            print(f"   ✅ {sum(len(c.sources) for c in batch.candidates)} total sources")

            if parsed.warnings:
                print(f"   ⚠️  Warnings: {', '.join(parsed.warnings[:3])}")

        except Exception as e:
            print(f"   ❌ Parse error: {e}")
            raise

    return batches


def consolidate_batches(batches):
    """Apply consolidation logic."""
    print("\n" + "="*70)
    print("PHASE 2: CONSOLIDATION")
    print("="*70)

    raw_count = sum(len(b.candidates) for b in batches)
    raw_sources = sum(sum(len(c.sources) for c in b.candidates) for b in batches)

    print(f"\n📊 Input metrics:")
    print(f"   Batches: {len(batches)}")
    print(f"   Raw candidates: {raw_count}")
    print(f"   Raw sources: {raw_sources}")

    try:
        consolidated = consolidate_discovery_batches(batches)
        print(f"\n✅ Consolidation succeeded")
        print(f"   Consolidated candidates: {len(consolidated)}")
        merged_sources = sum(len(c.sources) for c in consolidated)
        print(f"   Merged sources: {merged_sources}")
        print(f"   Deduplication rate: {raw_sources - merged_sources}/{raw_sources} URLs merged")

        return consolidated
    except Exception as e:
        print(f"\n❌ Consolidation failed: {e}")
        import traceback
        traceback.print_exc()
        raise


def evaluate_results(consolidated):
    """Evaluate consolidation against golden expectations."""
    print("\n" + "="*70)
    print("PHASE 3: GOLDEN VALIDATION")
    print("="*70)

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

    # Categorize each consolidated candidate
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

    # Validate expectations
    passed = 0
    failed = 0

    print("\n🎯 Core Expectations (MUST PASS):\n")

    expectations = [
        ("tag182", 1, "Same anchor URL + title similarity across all runs"),
        ("cavern", 1, "Distinct anchor URL + consistent titles"),
        ("aa26_097a", 1, "Same advisory ID + anchor URL"),
        ("dindoor", 1, "Same anchor URL + title similarity"),
    ]

    for key, expected_count, reason in expectations:
        actual_count = len(results[key])
        status = "✅" if actual_count == expected_count else "❌"
        print(f"  {status} {key.upper():15} expected={expected_count}, got={actual_count}")
        print(f"      {reason}")
        if actual_count == expected_count:
            passed += 1
        else:
            failed += 1
            if results[key]:
                for c in results[key]:
                    print(f"         - {c.representative.title[:50]}")

    print("\n🔥 MuddyWater Sub-Operations (CRITICAL - must stay distinct):\n")

    muddy_expectations = [
        ("olalampo", 1),
        ("chainshell", 1),
        ("iconcat", 1),
    ]

    muddy_ids = {}
    for key, expected_count in muddy_expectations:
        actual_count = len(results[key])
        status = "✅" if actual_count == expected_count else "❌"
        print(f"  {status} {key.upper():15} expected={expected_count}, got={actual_count}")
        if actual_count == expected_count:
            passed += 1
            muddy_ids[key] = {c.representative.id for c in results[key]}
        else:
            failed += 1

    # Check MuddyWater distinctness
    print("\n  Distinctness checks:")
    muddy_pairs = [("olalampo", "chainshell"), ("olalampo", "iconcat"), ("chainshell", "iconcat")]
    for k1, k2 in muddy_pairs:
        if k1 in muddy_ids and k2 in muddy_ids:
            if muddy_ids[k1] & muddy_ids[k2]:
                print(f"  ❌ {k1} == {k2} (SHOULD BE DISTINCT!)")
                failed += 1
            else:
                print(f"  ✅ {k1} != {k2}")
                passed += 1
        else:
            print(f"  ⚠️  {k1} or {k2} missing (cannot validate)")

    # Report other findings
    print(f"\n📋 Other Subjects Found:")
    for key in ["gigawiper", "redkitten", "dust_specter", "other"]:
        count = len(results[key])
        if count > 0:
            print(f"   {key:15} {count:2} candidate(s)")
            for c in results[key][:2]:  # Show first 2
                print(f"      - {c.representative.title[:50]}")
            if count > 2:
                print(f"      ... and {count-2} more")

    # Summary
    print("\n" + "="*70)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("="*70)

    return failed == 0


class TestPatch1Validation:
    """Integration test: Parse 4 golden runs and validate consolidation."""

    def test_end_to_end_pipeline(self):
        """Execute full pipeline: parse → consolidate → evaluate."""
        batches = load_golden_batches()
        consolidated = consolidate_batches(batches)
        success = evaluate_results(consolidated)

        assert success, "Golden validation failed"
        print("\n✅ ALL GOLDEN ASSERTIONS PASSED\n")


if __name__ == "__main__":
    # Run directly without pytest if called as script
    test = TestPatch1Validation()
    test.test_end_to_end_pipeline()
