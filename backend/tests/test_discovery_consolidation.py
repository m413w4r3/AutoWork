"""Tests for discovery consolidation (P2)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_identity import (
    canonical_source_key,
    explicit_entity_tokens,
    has_strong_signal,
    normalize_title,
    title_fingerprint,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
    SourceVerificationStatus,
)


class TestDiscoveryIdentity:
    """Tests for discovery_identity helpers."""

    def test_normalize_title_removes_accents(self) -> None:
        assert normalize_title("café") == "cafe"
        assert normalize_title("Élève") == "eleve"

    def test_normalize_title_lowercases(self) -> None:
        assert normalize_title("Hello World") == "hello world"

    def test_normalize_title_normalizes_whitespace(self) -> None:
        assert normalize_title("Hello  \n  World") == "hello world"

    def test_title_fingerprint_deterministic(self) -> None:
        fp1 = title_fingerprint("Cavern Malware Campaign")
        fp2 = title_fingerprint("cavern malware campaign")
        assert fp1 == fp2

    def test_explicit_entity_tokens(self) -> None:
        candidate = CandidateTopic(
            id=uuid4(),
            title="Test",
            summary="Test",
            novelty="high",
            technical_potential=3,
            event_date=None,
            uncertainties=(),
            relevance_reasons=(),
            actors=("WIZARD SPIDER", "Evil Corp"),
            campaigns=(),
            malware=(),
            cves=(),
            victims=(),
            sectors=(),
            countries=(),
            likely_artifacts=(),
            iocs=(),
            provisional_iocs=(),
            sources=(),
            incomplete_sources=(),
            local_ref=None,
            actor_or_campaign="WIZARD SPIDER",
            technical_potential_reason="",
            parsing_warnings=(),
            context_only=False,
            selectable=True,
        )
        tokens = explicit_entity_tokens(candidate)
        assert "wizard spider" in tokens
        assert "evil corp" in tokens

    def test_has_strong_signal_title_similarity(self) -> None:
        # 70%+ similarity should trigger
        assert has_strong_signal(
            "Cavern Malware Campaign",
            "Cavern Malware",
            set(),
            set(),
            set(),
            set(),
            set(),
            set(),
        )

    def test_has_strong_signal_shared_entity(self) -> None:
        assert has_strong_signal(
            "Topic A",
            "Topic B",
            {"wizard spider"},
            {"wizard spider", "other"},
            set(),
            set(),
            set(),
            set(),
        )

    def test_canonical_source_key(self) -> None:
        url1 = "https://example.com/report"
        url2 = "HTTPS://EXAMPLE.COM/report"
        assert canonical_source_key(url1) == canonical_source_key(url2)


class TestDiscoveryConsolidation:
    """Tests for discovery batch consolidation."""

    @staticmethod
    def _make_source(url: str, publisher: str = "Test Publisher") -> SourceCandidate:
        return SourceCandidate(
            id=uuid4(),
            url=url,
            canonical_url=url.lower(),
            raw_url=url,
            title="Test",
            publisher=publisher,
            role=SourceRole.PRIMARY,
            published_at=date(2026, 7, 16),
            event_date=None,
            citation=None,
            ioc_presence="none",
            ioc_declared_count=None,
            ioc_visible_count=0,
            parsing_warnings=(),
            verification_status=SourceVerificationStatus.UNVERIFIED,
            relationship_status="direct",
            verification_changed_at=None,
            verification_changed_by=None,
            local_ref=None,
            source_ref=None,
            period_relation=None,
        )

    @staticmethod
    def _make_candidate(
        title: str,
        sources: list[SourceCandidate] | None = None,
        actors: tuple[str, ...] = (),
        campaigns: tuple[str, ...] = (),
    ) -> CandidateTopic:
        return CandidateTopic(
            id=uuid4(),
            title=title,
            summary="Test summary",
            novelty="high",
            technical_potential=3,
            event_date=date(2026, 7, 16),
            uncertainties=(),
            relevance_reasons=(),
            actors=actors,
            campaigns=campaigns,
            malware=(),
            cves=(),
            victims=(),
            sectors=(),
            countries=(),
            likely_artifacts=(),
            iocs=(),
            provisional_iocs=(),
            sources=tuple(sources) if sources else (),
            incomplete_sources=(),
            local_ref=None,
            actor_or_campaign=actors[0] if actors else "",
            technical_potential_reason="",
            parsing_warnings=(),
            context_only=False,
            selectable=True,
        )

    @staticmethod
    def _make_batch(
        candidates: list[CandidateTopic],
    ) -> DiscoveryBatch:
        batch_id = uuid4()
        return DiscoveryBatch(
            id=batch_id,
            edition_id=uuid4(),
            request_hash="test-hash",
            complementary_axis="test",
            queries=(),
            citations=(),
            candidates=tuple(candidates),
            discovery_model_run_id=uuid4(),
            structuring_model_run_id=uuid4(),
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
            report_sha256="abc123",
            parser_version="1.0",
            parsing_status="completed",
            parsing_warnings=(),
            unattached_visible_citations=(),
            source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
            bridge_capabilities={},
            citation_count=0,
            source_coverage_complete=False,
            source_coverage_incomplete_reason=None,
            created_at=None,
            parsing_revision=1,
            supersedes_batch_id=None,
            replaced_by_batch_id=None,
            is_active_revision=True,
        )

    def test_consolidate_same_subject_same_url(self) -> None:
        """Case A: Same subject, same URL → 1 subject, 1 URL, duplicate count = 1."""
        source_a = self._make_source("https://example.com/report")
        source_b = self._make_source("https://example.com/report")

        cand1 = self._make_candidate("Cavern Campaign", sources=[source_a])
        cand2 = self._make_candidate("Cavern Campaign", sources=[source_b])

        batch1 = self._make_batch([cand1])
        batch2 = self._make_batch([cand2])

        consolidated = consolidate_discovery_batches([batch1, batch2])

        assert len(consolidated) == 1
        assert consolidated[0].contribution_count == 2
        assert len(consolidated[0].sources) == 1
        assert consolidated[0].duplicate_publication_count == 1

    def test_consolidate_update_subject_add_url(self) -> None:
        """Case B: Subject update (A, B) + (A, C) → A, B, C with 2 contributions."""
        source_a = self._make_source("https://example.com/a")
        source_b = self._make_source("https://example.com/b")
        source_c = self._make_source("https://example.com/c")

        cand1 = self._make_candidate("Cavern", sources=[source_a, source_b])
        cand2 = self._make_candidate("Cavern", sources=[source_a, source_c])

        batch1 = self._make_batch([cand1])
        batch2 = self._make_batch([cand2])

        consolidated = consolidate_discovery_batches([batch1, batch2])

        assert len(consolidated) == 1
        assert consolidated[0].contribution_count == 2
        assert len(consolidated[0].sources) == 3
        assert consolidated[0].duplicate_publication_count == 1  # source_a appears twice

    def test_consolidate_url_deduplication(self) -> None:
        """Case C: UTM params removed by parser → 1 URL."""
        # Assuming parser already canonicalizes URLs (UTM removal)
        source_base = self._make_source("https://example.com/article")
        source_utm = self._make_source("https://example.com/article")  # already canonicalized

        cand1 = self._make_candidate("Story", sources=[source_base])
        cand2 = self._make_candidate("Story", sources=[source_utm])

        batch1 = self._make_batch([cand1])
        batch2 = self._make_batch([cand2])

        consolidated = consolidate_discovery_batches([batch1, batch2])

        assert len(consolidated) == 1
        assert len(consolidated[0].sources) == 1
        assert consolidated[0].duplicate_publication_count == 1

    def test_consolidate_separate_subjects_same_url(self) -> None:
        """Case D: Synthesis covering 2 subjects → 2 consolidated, URL in both."""
        shared_url = self._make_source("https://example.com/synthesis")

        cand_a = self._make_candidate("Campaign A", sources=[shared_url], campaigns=("Campaign A",))
        cand_b = self._make_candidate("Campaign B", sources=[shared_url], campaigns=("Campaign B",))

        batch = self._make_batch([cand_a, cand_b])

        consolidated = consolidate_discovery_batches([batch])

        # Should NOT merge because campaigns differ
        assert len(consolidated) == 2

    def test_consolidate_metadata_enrichment(self) -> None:
        """Case E: Metadata enrichment (unknown → known value)."""
        source_unknown = SourceCandidate(
            id=uuid4(),
            url="https://example.com/report",
            canonical_url="https://example.com/report",
            raw_url="https://example.com/report",
            title="Test",
            publisher="unknown",
            role=SourceRole.PRIMARY,
            published_at=None,
            event_date=None,
            citation=None,
            ioc_presence="none",
            ioc_declared_count=None,
            ioc_visible_count=0,
            parsing_warnings=(),
            verification_status=SourceVerificationStatus.UNVERIFIED,
            relationship_status="direct",
            verification_changed_at=None,
            verification_changed_by=None,
            local_ref=None,
            source_ref=None,
            period_relation=None,
        )

        source_known = SourceCandidate(
            id=uuid4(),
            url="https://example.com/report",
            canonical_url="https://example.com/report",
            raw_url="https://example.com/report",
            title="Test",
            publisher="Recorded Future",
            role=SourceRole.PRIMARY,
            published_at=date(2026, 7, 16),
            event_date=None,
            citation=None,
            ioc_presence="none",
            ioc_declared_count=None,
            ioc_visible_count=0,
            parsing_warnings=(),
            verification_status=SourceVerificationStatus.UNVERIFIED,
            relationship_status="direct",
            verification_changed_at=None,
            verification_changed_by=None,
            local_ref=None,
            source_ref=None,
            period_relation=None,
        )

        cand1 = self._make_candidate("Report", sources=[source_unknown])
        cand2 = self._make_candidate("Report", sources=[source_known])

        batch1 = self._make_batch([cand1])
        batch2 = self._make_batch([cand2])

        consolidated = consolidate_discovery_batches([batch1, batch2])

        assert len(consolidated) == 1
        assert consolidated[0].sources[0].publisher == "Recorded Future"
        assert consolidated[0].sources[0].published_at == date(2026, 7, 16)

    def test_consolidate_metadata_conflict(self) -> None:
        """Case F: Metadata conflict → merge_warnings."""
        source_v1 = SourceCandidate(
            id=uuid4(),
            url="https://example.com/report",
            canonical_url="https://example.com/report",
            raw_url="https://example.com/report",
            title="Test",
            publisher="Publisher A",
            role=SourceRole.PRIMARY,
            published_at=date(2026, 7, 16),
            event_date=None,
            citation=None,
            ioc_presence="none",
            ioc_declared_count=None,
            ioc_visible_count=0,
            parsing_warnings=(),
            verification_status=SourceVerificationStatus.UNVERIFIED,
            relationship_status="direct",
            verification_changed_at=None,
            verification_changed_by=None,
            local_ref=None,
            source_ref=None,
            period_relation=None,
        )

        source_v2 = SourceCandidate(
            id=uuid4(),
            url="https://example.com/report",
            canonical_url="https://example.com/report",
            raw_url="https://example.com/report",
            title="Test",
            publisher="Publisher B",
            role=SourceRole.PRIMARY,
            published_at=date(2026, 7, 17),
            event_date=None,
            citation=None,
            ioc_presence="none",
            ioc_declared_count=None,
            ioc_visible_count=0,
            parsing_warnings=(),
            verification_status=SourceVerificationStatus.UNVERIFIED,
            relationship_status="direct",
            verification_changed_at=None,
            verification_changed_by=None,
            local_ref=None,
            source_ref=None,
            period_relation=None,
        )

        cand1 = self._make_candidate("Report", sources=[source_v1])
        cand2 = self._make_candidate("Report", sources=[source_v2])

        batch1 = self._make_batch([cand1])
        batch2 = self._make_batch([cand2])

        consolidated = consolidate_discovery_batches([batch1, batch2])

        assert len(consolidated) == 1
        assert len(consolidated[0].merge_warnings) > 0
        # Conflict warnings should be present
        conflict_warnings = [w for w in consolidated[0].merge_warnings if "conflict" in w.lower()]
        assert len(conflict_warnings) > 0
