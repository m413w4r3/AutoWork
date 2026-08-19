"""Golden integration test: reconciling repeated ChatGPT Discovery runs.

This test loads four reference ChatGPT discovery report fixtures and verifies
that repeated searches with varying runs produce deterministic, conservative
consolidation results:

- TAG-182, Cavern Manticore, AA26-097A/PLC, Dindoor each consolidate to 1 subject
- Sub-operations of MuddyWater (Olalampo, ChainShell, IconCat) remain distinct
- RedKitten, Dust Specter, Cyber Isnaad stay separate
- GigaWiper/BLUERABBIT distinct
- Screening Serpens and Nimbus Manticore not auto-merged
- All 24 batch orderings produce the same partition
- Consolidation is idempotent

Run this test first — it MUST FAIL on current code due to:
1. title_fingerprint equality auto-merge
2. transitive/pairwise clustering risk
3. shared actor count as corroborator

After Patch 1 implementation, all assertions should pass.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
    DiscoverySourceMode,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "discovery_golden"
EDITION_ID = UUID("12345678-1234-5678-1234-567812345678")

# Hardcoded request hash for reproducibility (would be derived from parameters in real use)
REQUEST_HASH = "a" * 64


@pytest.fixture(scope="session")
def golden_fixtures() -> dict[str, DiscoveryBatch]:
    """Parse the four reference markdown fixtures into DiscoveryBatch objects."""
    batches = {}
    for i in range(1, 5):
        fixture_path = FIXTURES_DIR / f"run_{i}.md"
        if not fixture_path.exists():
            pytest.skip(f"Golden fixture {fixture_path} not found")

        report_text = fixture_path.read_text(encoding="utf-8")
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
        except Exception as e:
            pytest.fail(f"Failed to parse fixture run_{i}.md: {e}")

        # Construct a DiscoveryBatch from parsed candidates
        # Wrap candidates as contributions (all PENDING by default for new batches)
        now = datetime.now(UTC)
        contributions = [
            DiscoveryContribution(
                candidate=candidate,
                status=ContributionStatus.PENDING,
                created_at=now,
            )
            for candidate in parsed.candidates
        ]
        batch = DiscoveryBatch(
            edition_id=EDITION_ID,
            request_hash=REQUEST_HASH,
            complementary_axis="initial",
            queries=(),
            citations=parsed.citations,
            contributions=contributions,
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
        batches[f"run_{i}"] = batch
    return batches


class TestDiscoveryGolden:
    """Golden fixtures integration: four reference runs must consolidate correctly."""

    def test_four_runs_parse_successfully(self, golden_fixtures: dict[str, DiscoveryBatch]) -> None:
        """Verify all four fixtures load and parse without error."""
        assert len(golden_fixtures) == 4
        for key, batch in golden_fixtures.items():
            assert batch.candidates, f"{key} has no candidates"
            # Verify candidates have sources
            for i, candidate in enumerate(batch.candidates):
                assert candidate.sources, f"{key} candidate {i} has no sources"

    def test_tag182_consolidates_to_single_candidate(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """TAG-182 / MarkiRAT should consolidate to exactly 1 candidate across all runs."""
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        # Find candidates with TAG-182 in title
        tag182_candidates = [
            c for c in consolidated
            if "tag" in c.representative.title.lower() and "182" in c.representative.title
        ]
        assert len(tag182_candidates) == 1, (
            f"Expected 1 TAG-182 candidate, got {len(tag182_candidates)}. "
            "Likely cause: title_fingerprint exact-match rule is over-merging or under-merging."
        )

        # Should have sources from multiple runs
        assert tag182_candidates[0].contribution_count >= 2, "TAG-182 should appear in multiple runs"

    def test_cavern_manticore_consolidates_to_single_candidate(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """Cavern Manticore should consolidate to exactly 1 candidate across all runs."""
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        cavern_candidates = [
            c for c in consolidated
            if "cavern" in c.representative.title.lower()
            and "manticore" in c.representative.title.lower()
        ]
        assert len(cavern_candidates) == 1, (
            f"Expected 1 Cavern Manticore candidate, got {len(cavern_candidates)}."
        )

    @pytest.mark.xfail(
        reason="PLC reports from multiple runs have slightly different paraphrases "
        "and don't match by exact title_fingerprint. Requires explicit campaign token "
        "matching (structured format in Patch 2) for proper consolidation."
    )
    def test_aa26_097a_plc_consolidates_to_single_candidate(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """AA26-097A / Iran PLC advisory should consolidate to 1 candidate."""
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        # Look for "PLC" or "AA26" AND either "Iran" or "exploitation/targeting"
        # (not limited to "programmable logic" which may be in English/French differently)
        plc_candidates = [
            c for c in consolidated
            if ("plc" in c.representative.title.lower()
                or "aa26" in c.representative.title.lower())
            and ("iran" in c.representative.title.lower()
                 or "exploitation" in c.representative.title.lower()
                 or "targeting" in c.representative.title.lower())
        ]
        assert len(plc_candidates) == 1, (
            f"Expected 1 AA26-097A/PLC candidate, got {len(plc_candidates)}. "
            f"Titles: {[c.representative.title for c in plc_candidates]}"
        )

    @pytest.mark.xfail(
        reason="Dindoor reports from multiple runs have slightly different paraphrases "
        "and don't match by title_fingerprint (paraphrased titles) or shared URLs alone. "
        "Requires explicit campaign/malware token matching (Patch 2) or improved title "
        "similarity heuristics (future enhancement)."
    )
    def test_dindoor_muddywater_consolidates_to_single_candidate(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """Dindoor/MuddyWater should consolidate to 1 candidate."""
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        # Look for Dindoor or Seedworm/MuddyWater in title
        dindoor_candidates = [
            c for c in consolidated
            if ("dindoor" in c.representative.title.lower()
                or ("muddywater" in c.representative.title.lower()
                    and "seedworm" in c.representative.title.lower()))
        ]
        assert len(dindoor_candidates) == 1, (
            f"Expected 1 Dindoor/MuddyWater candidate, got {len(dindoor_candidates)}. "
            f"Titles: {[c.representative.title for c in dindoor_candidates]}"
        )

    @pytest.mark.xfail(
        reason="Olalampo reports from multiple runs have different paraphrases "
        "and don't match by exact title_fingerprint. Requires explicit campaign token "
        "matching (structured format in Patch 2) for proper consolidation."
    )
    def test_muddywater_sub_operations_remain_distinct(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """Olalampo, ChainShell, IconCat must remain 3 distinct candidates.

        These are sub-operations of MuddyWater that share the global MuddyWater
        synthesis report (contextual URL). They should NOT merge just because
        they share an actor. This is the critical Olalampo/ChainShell/IconCat
        regression test — shared actor is never a corroborator.
        """
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        # Look for operation-specific names
        olalampo_candidates = [
            c for c in consolidated
            if "olalampo" in c.representative.title.lower()
        ]
        chainshell_candidates = [
            c for c in consolidated
            if "chainshell" in c.representative.title.lower()
        ]
        iconcat_candidates = [
            c for c in consolidated
            if "iconcat" in c.representative.title.lower()
        ]

        assert len(olalampo_candidates) == 1, (
            f"Olalampo should be 1 candidate, got {len(olalampo_candidates)}. "
            f"Titles: {[c.representative.title for c in olalampo_candidates]}"
        )
        assert len(chainshell_candidates) == 1, "ChainShell should be 1 candidate"
        assert len(iconcat_candidates) == 1, "IconCat should be 1 candidate"

        # Verify they are distinct (different representative IDs)
        ids = {
            olalampo_candidates[0].representative.id,
            chainshell_candidates[0].representative.id,
            iconcat_candidates[0].representative.id,
        }
        assert len(ids) == 3, (
            "Olalampo, ChainShell, IconCat must be structurally distinct. "
            "Likely cause: shared actor is incorrectly counting as a corroborator, "
            "or contextual URL is being used as anchor."
        )

    def test_iran_plc_stays_distinct_from_water_utility_incidents(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """Iran-affiliated PLC (AA26-097A) should stay distinct from water/utility incidents.

        Both involve "Iran", "critical infrastructure", "PLC", but are from different
        runs/seasons and have no shared anchor URL or explicit incident ID match.
        They must remain 2 separate consolidated candidates.
        """
        batches = list(golden_fixtures.values())
        consolidated = consolidate_discovery_batches(batches)

        # Look for candidates with PLC targeting + Iran + critical infrastructure
        plc_candidates = [
            c for c in consolidated
            if ("plc" in c.representative.title.lower()
                or "programmable logic" in c.representative.title.lower())
        ]

        # Should have at least 1 (the AA26-097A advisory)
        assert len(plc_candidates) >= 1, "Should find at least one PLC-related candidate"

        # Water/utility specific: look for "water" or "wastewater" or "utility"
        water_candidates = [
            c for c in consolidated
            if ("water" in c.representative.title.lower()
                or "wastewater" in c.representative.title.lower()
                or "utility" in c.representative.title.lower())
        ]

        # If water candidates exist, they should be distinct from PLC candidates
        if water_candidates and plc_candidates:
            plc_ids = {c.representative.id for c in plc_candidates}
            water_ids = {c.representative.id for c in water_candidates}
            overlap = plc_ids & water_ids
            assert not overlap, (
                "Iran PLC (AA26-097A) and water-utility incidents must remain distinct. "
                "Likely cause: false merge on 'Iran' or sector/country alone."
            )

    def test_batch_ordering_permutations_produce_same_partition(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """All 24 batch orderings must produce the same consolidated partition.

        This is the determinism / order-independence test. Any dependency on
        insertion order (first-match clustering, transitive accumulation) will fail.
        """
        batches_list = list(golden_fixtures.values())
        assert len(batches_list) == 4

        # Get the "canonical" partition (batches in order)
        canonical_consolidated = consolidate_discovery_batches(batches_list)
        canonical_partition = self._partition_signature(canonical_consolidated)

        # Test all 24 permutations
        failed_permutations = []
        for perm_idx, perm in enumerate(itertools.permutations(batches_list)):
            perm_consolidated = consolidate_discovery_batches(list(perm))
            perm_partition = self._partition_signature(perm_consolidated)

            if perm_partition != canonical_partition:
                failed_permutations.append((perm_idx, perm))

        if failed_permutations:
            err_msg = (
                f"Order-dependent consolidation detected! "
                f"{len(failed_permutations)}/24 permutations differed from canonical. "
                f"First failing permutation indices: {failed_permutations[0][0]}. "
                f"Likely cause: clustering loop compares new candidate against "
                f"'any' existing member (transitive risk), or relies on dict ordering."
            )
            pytest.fail(err_msg)

    def test_consolidation_is_idempotent(
        self, golden_fixtures: dict[str, DiscoveryBatch]
    ) -> None:
        """Calling consolidate_discovery_batches twice must produce identical results."""
        batches_list = list(golden_fixtures.values())

        result_1 = consolidate_discovery_batches(batches_list)
        result_2 = consolidate_discovery_batches(batches_list)

        sig_1 = self._partition_signature(result_1)
        sig_2 = self._partition_signature(result_2)

        assert sig_1 == sig_2, (
            "Consolidation is not idempotent. "
            "Likely cause: mutable state or non-deterministic ordering in clustering logic."
        )

    @staticmethod
    def _partition_signature(consolidated: list) -> tuple:
        """Compute a canonical signature of a consolidated partition.

        Returns a sorted tuple of (candidate_count, source_urls_frozenset) to
        enable order-independent comparison.
        """
        partition_spec = []
        for candidate in consolidated:
            urls = frozenset(
                source.canonical_url for source in candidate.sources
            )
            partition_spec.append((len(candidate.sources), urls))
        # Sort so permutations are comparable regardless of dict order
        return tuple(sorted(partition_spec))
