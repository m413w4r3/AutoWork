#!/usr/bin/env python3
"""Debug AA26-097A."""

import sys
from pathlib import Path
from datetime import date
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.application.discovery_identity import (
    build_discovery_identity_index,
    match_topics,
    _extract_incident_identifiers,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch, DiscoverySourceMode

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "discovery_golden"
EDITION_ID = UUID("12345678-1234-5678-1234-567812345678")
REQUEST_HASH = "a" * 64


def load_and_parse_runs():
    """Load and parse all 4 golden runs."""
    run_files = ["run_1.md", "run_2.md", "run_3.md", "run_4.md"]
    batches = []

    for run_file in run_files:
        path = FIXTURES_DIR / run_file
        if not path.exists():
            raise FileNotFoundError(f"Golden fixture missing: {path}")

        report_text = path.read_text(encoding="utf-8")

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
        except Exception as e:
            print(f"❌ Parse error: {e}")
            raise

    return batches


def debug_aa26(batches):
    """Debug AA26-097A matching."""
    print("\n" + "="*70)
    print("DEBUG: ALL CANDIDATES (looking for AA26 or PLC)")
    print("="*70 + "\n")

    # Collect all candidates
    all_candidates = []
    for batch_idx, batch in enumerate(batches):
        for cand in batch.candidates:
            all_candidates.append((batch_idx, cand))
            title_lower = cand.title.lower()
            if "aa26" in title_lower or ("plc" in title_lower and "programmable" in title_lower):
                print(f"Run {batch_idx+1}: {cand.title[:70]}")
                print(f"  ID: {cand.id}")
                print(f"  Actor(s): {cand.actors}")
                print(f"  Incident IDs: {_extract_incident_identifiers(cand.title)}")
                print(f"  Sources: {len(cand.sources)}")
                for src in cand.sources:
                    print(f"    - {src.canonical_url[:60]} (role={src.role})")
                print()

    print(f"Total candidates parsed: {len(all_candidates)}\n")

    # Search for any mention of "AA26" or similar patterns
    print("All titles containing 'PLC' or 'programmable':")
    for batch_idx, cand in all_candidates:
        title_lower = cand.title.lower()
        if "plc" in title_lower or "programmable" in title_lower:
            print(f"  {cand.title[:70]}")
            incident_ids = _extract_incident_identifiers(cand.title)
            if incident_ids:
                print(f"    Incident IDs: {incident_ids}")


if __name__ == "__main__":
    batches = load_and_parse_runs()
    debug_aa26(batches)
