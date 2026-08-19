"""Tests for Patch 2: Structured entity extraction in discovery parser."""

from datetime import date
from uuid import UUID

import pytest

from cti_app.application.discovery_report_parser import (
    _detect_structured_format,
    _extract_structured_list,
    parse_discovery_report,
)
from cti_app.domain.classification import TLP


class TestDetectStructuredFormat:
    """Test detection of new structured format (actors:, campaigns:, malware:)."""

    def test_detects_actors_header(self):
        """Legacy format without actors: header should return False."""
        legacy = "## SUBJECT S1\ntitle: Test\nactor_or_campaign: APT10"
        assert not _detect_structured_format(legacy.splitlines())

    def test_detects_new_format(self):
        """New format with actors: header should return True."""
        new_format = "## SUBJECT S1\nactors:\n  - APT10\n  - Ferocious Kitten"
        assert _detect_structured_format(new_format.splitlines())

    def test_detects_campaigns_header(self):
        """Format with campaigns: header should return True."""
        new_format = "## SUBJECT S1\ncampaigns:\n  - Operation Olalampo"
        assert _detect_structured_format(new_format.splitlines())

    def test_detects_malware_header(self):
        """Format with malware: header should return True."""
        new_format = "## SUBJECT S1\nmalware:\n  - MarkiRAT v2.1"
        assert _detect_structured_format(new_format.splitlines())


class TestExtractStructuredList:
    """Test extraction of structured list items."""

    def test_extracts_bullet_list(self):
        """Extract items from bullet list format."""
        text = "- APT10\n- Ferocious Kitten\n- Another Group"
        result = _extract_structured_list(text)
        assert result == ("APT10", "Ferocious Kitten", "Another Group")

    def test_extracts_comma_separated(self):
        """Extract items from comma-separated format."""
        text = "APT10, Ferocious Kitten, Another Group"
        result = _extract_structured_list(text)
        assert result == ("APT10", "Ferocious Kitten", "Another Group")

    def test_extracts_semicolon_separated(self):
        """Extract items from semicolon-separated format."""
        text = "APT10; Ferocious Kitten; Another Group"
        result = _extract_structured_list(text)
        assert result == ("APT10", "Ferocious Kitten", "Another Group")

    def test_deduplicates_items(self):
        """Duplicate items should be removed."""
        text = "- APT10\n- APT10\n- Ferocious Kitten"
        result = _extract_structured_list(text)
        assert result == ("APT10", "Ferocious Kitten")

    def test_filters_unknown(self):
        """Items 'unknown' and 'none' should be filtered out."""
        text = "- APT10\n- unknown\n- Ferocious Kitten"
        result = _extract_structured_list(text)
        assert result == ("APT10", "Ferocious Kitten")

    def test_empty_input(self):
        """Empty input should return empty tuple."""
        assert _extract_structured_list("") == ()
        assert _extract_structured_list("  \n  \n  ") == ()


class TestLegacyFormatBackwardCompatibility:
    """Test that legacy format still parses correctly (backward compatibility)."""

    @pytest.fixture
    def legacy_report(self) -> str:
        """Sample legacy format discovery report."""
        return """## SUBJECT S1
title: TAG-182 Campaign Analysis
presentation: Analysis of TAG-182 threat group activities
actor_or_campaign: TAG-182
technical_potential: 3
technical_potential_reason: Demonstrated attack capability
artifacts: ioc, samples
uncertainties: Attribution confidence

### PUBLICATION P1
title: Threat Report - TAG-182
url: https://example.com/report1
publisher: Security Vendor
published_at: 2026-01-15
source_role: primary
period_relation: in_period
ioc_presence: visible
visible_ioc_types: sha256, ipv4
visible_iocs:
  - abc123def456
  - 192.0.2.1
"""

    def test_legacy_format_parses(self, legacy_report: str):
        """Legacy format without structured fields should parse without error."""
        result = parse_discovery_report(
            legacy_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        assert result.status == "completed"
        assert len(result.candidates) == 1

    def test_legacy_format_populates_actor_or_campaign(self, legacy_report: str):
        """Legacy format should populate actor_or_campaign field."""
        result = parse_discovery_report(
            legacy_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        candidate = result.candidates[0]
        assert candidate.actor_or_campaign == "TAG-182"
        # Fallback: should also populate actors tuple
        assert candidate.actors == ("TAG-182",)

    def test_legacy_format_structured_fields_empty(self, legacy_report: str):
        """Legacy format should have empty campaigns and malware."""
        result = parse_discovery_report(
            legacy_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        candidate = result.candidates[0]
        assert candidate.campaigns == ()
        assert candidate.malware == ()


class TestEnhancedStructuredFormat:
    """Test parsing of new structured format (Patch 2)."""

    @pytest.fixture
    def enhanced_report(self) -> str:
        """Sample enhanced format discovery report with structured entities."""
        return """## SUBJECT S1
title: TAG-182 MarkiRAT Distribution
presentation: Analysis of TAG-182's MarkiRAT malware campaign
actors:
  - TAG-182
  - Ferocious Kitten
campaigns:
  - MarkiRAT Distribution Q1 2026
  - Operation Olalampo Phase 2
malware:
  - MarkiRAT v2.1
  - MarkiRAT v2.0 (legacy)
technical_potential: 4
technical_potential_reason: Active exploitation observed
artifacts: ioc, samples

### PUBLICATION P1
title: TAG-182 Campaign Report
url: https://example.com/enhanced-report
publisher: Threat Intel Corp
published_at: 2026-02-01
source_role: primary
period_relation: in_period
ioc_presence: visible
visible_ioc_types: sha256
visible_iocs:
  - a1b2c3d4e5f6g7h8
"""

    def test_enhanced_format_parses(self, enhanced_report: str):
        """Enhanced format with structured fields should parse without error."""
        result = parse_discovery_report(
            enhanced_report,
            visible_citations=[],
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        assert result.status == "completed"
        assert len(result.candidates) == 1

    def test_enhanced_format_extracts_actors(self, enhanced_report: str):
        """Enhanced format should extract structured actors."""
        result = parse_discovery_report(
            enhanced_report,
            visible_citations=[],
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        candidate = result.candidates[0]
        assert candidate.actors == ("TAG-182", "Ferocious Kitten")

    def test_enhanced_format_extracts_campaigns(self, enhanced_report: str):
        """Enhanced format should extract structured campaigns."""
        result = parse_discovery_report(
            enhanced_report,
            visible_citations=[],
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        candidate = result.candidates[0]
        assert candidate.campaigns == ("MarkiRAT Distribution Q1 2026", "Operation Olalampo Phase 2")

    def test_enhanced_format_extracts_malware(self, enhanced_report: str):
        """Enhanced format should extract structured malware."""
        result = parse_discovery_report(
            enhanced_report,
            visible_citations=[],
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        candidate = result.candidates[0]
        assert candidate.malware == ("MarkiRAT v2.1", "MarkiRAT v2.0 (legacy)")


class TestMixedBatch:
    """Test batch containing both legacy and enhanced formats."""

    @pytest.fixture
    def mixed_report(self) -> str:
        """Report with both legacy and enhanced subjects."""
        return """## SUBJECT S1
title: Legacy Style Analysis
presentation: Old format analysis
actor_or_campaign: BadActor
technical_potential: 2
artifacts: unknown

### PUBLICATION P1
url: https://example.com/legacy
title: Legacy Source
publisher: OldVendor

## SUBJECT S2
title: New Style Analysis
presentation: New structured format
actors:
  - GoodActor
campaigns:
  - New Campaign
malware:
  - TrojanX
technical_potential: 3
artifacts: ioc

### PUBLICATION P2
url: https://example.com/new
title: New Source
publisher: NewVendor
"""

    def test_mixed_batch_parses(self, mixed_report: str):
        """Mixed batch should parse both subject styles."""
        result = parse_discovery_report(
            mixed_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        assert len(result.candidates) == 2

    def test_legacy_subject_in_mixed_batch(self, mixed_report: str):
        """Legacy subject in mixed batch should use fallback."""
        result = parse_discovery_report(
            mixed_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        legacy_candidate = result.candidates[0]
        assert legacy_candidate.title == "Legacy Style Analysis"
        assert legacy_candidate.actors == ("BadActor",)  # Fallback from actor_or_campaign
        assert legacy_candidate.campaigns == ()
        assert legacy_candidate.malware == ()

    def test_enhanced_subject_in_mixed_batch(self, mixed_report: str):
        """Enhanced subject in mixed batch should use structured fields."""
        result = parse_discovery_report(
            mixed_report,
            visible_citations=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            tlp=TLP.AMBER,
            sensitivity="int",
            external_llm_allowed=True,
        )
        enhanced_candidate = result.candidates[1]
        assert enhanced_candidate.title == "New Style Analysis"
        assert enhanced_candidate.actors == ("GoodActor",)
        assert enhanced_candidate.campaigns == ("New Campaign",)
        assert enhanced_candidate.malware == ("TrojanX",)
