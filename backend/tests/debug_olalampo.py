#!/usr/bin/env python3
"""Debug Olalampo fragmentation."""

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


def debug_olalampo(batches):
    """Debug Olalampo consolidation."""
    print("\n" + "="*70)
    print("DEBUG: Olalampo CANDIDATES")
    print("="*70 + "\n")

    # Collect all Olalampo candidates
    olalampo_candidates = []
    for batch_idx, batch in enumerate(batches):
        for cand in batch.candidates:
            if "olalampo" in cand.title.lower():
                olalampo_candidates.append((batch_idx, cand))
                print(f"Run {batch_idx+1}: {cand.title[:70]}")
                print(f"  ID: {cand.id}")
                print(f"  Incident IDs: {_extract_incident_identifiers(cand.title)}")
                print(f"  Sources: {len(cand.sources)}")
                for src in cand.sources[:2]:
                    print(f"    - {src.canonical_url[:60]} (role={src.role})")
                print()

    print(f"Total Olalampo candidates: {len(olalampo_candidates)}\n")

    # Test pairwise matching
    if len(olalampo_candidates) > 1:
        print("="*70)
        print("PAIRWISE MATCHING")
        print("="*70 + "\n")

        identity_index = build_discovery_identity_index(batches)

        for i, (batch_i, cand_i) in enumerate(olalampo_candidates):
            for j, (batch_j, cand_j) in enumerate(olalampo_candidates):
                if i >= j:
                    continue

                print(f"Run{batch_i+1} vs Run{batch_j+1}:")
                print(f"  '{cand_i.title[:50]}...'")
                print(f"  '{cand_j.title[:50]}...'")

                # Incident IDs
                ids_i = _extract_incident_identifiers(cand_i.title)
                ids_j = _extract_incident_identifiers(cand_j.title)
                shared_ids = ids_i & ids_j
                print(f"  Incident IDs: {ids_i} ∩ {ids_j} = {shared_ids}")

                # Match result
                result = match_topics(cand_i, cand_j, identity_index)
                print(f"  → {result.decision}")
                print(f"     Reasons: {result.reasons}")
                print()


if __name__ == "__main__":
    batches = load_and_parse_runs()
    debug_olalampo(batches)
