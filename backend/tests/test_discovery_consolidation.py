"""Tests de la projection consolidée des batches de découverte (§33)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_identity import (
    candidates_match_strongly,
    canonical_source_key,
    explicit_entity_tokens,
    has_other_strong_signal,
    normalize,
    shared_strong_urls,
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

REQUEST_HASH = "a" * 64


def _source(
    url: str,
    *,
    publisher: str = "Vendor Research",
    role: SourceRole = SourceRole.PRIMARY,
    published_at: date | None = date(2026, 7, 16),
    verification_status: SourceVerificationStatus = SourceVerificationStatus.UNVERIFIED,
    verification_changed_at: datetime | None = None,
    verification_changed_by: str | None = None,
) -> SourceCandidate:
    return SourceCandidate(
        url=url,
        title="Rapport technique",
        publisher=publisher,
        role=role,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        published_at=published_at,
        verification_status=verification_status,
        verification_changed_at=verification_changed_at,
        verification_changed_by=verification_changed_by,
    )


def _candidate(
    title: str,
    sources: list[SourceCandidate],
    *,
    actors: tuple[str, ...] = ("MuddyWater",),
    campaigns: tuple[str, ...] = ("Example Campaign",),
    malware: tuple[str, ...] = ("ExampleRAT",),
    technical_potential: int = 3,
    iocs: tuple[str, ...] = (),
    uncertainties: tuple[str, ...] = (),
) -> CandidateTopic:
    return CandidateTopic(
        title=title,
        summary="Publication technique décrivant une campagne et ses indicateurs.",
        novelty="Nouveau rapport technique",
        technical_potential=technical_potential,
        event_date=date(2026, 7, 10),
        uncertainties=uncertainties,
        relevance_reasons=("Artefacts techniques",),
        actors=actors,
        campaigns=campaigns,
        malware=malware,
        cves=(),
        victims=("administration",),
        sectors=("gouvernement",),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        iocs=iocs,
        sources=sources,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def _batch(candidates: list[CandidateTopic], *, edition_id=None) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=edition_id or uuid4(),
        request_hash=REQUEST_HASH,
        complementary_axis="initial",
        queries=(),
        citations=(),
        candidates=candidates,
        discovery_model_run_id=uuid4(),
        structuring_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
        source_coverage_complete=True,
    )


class TestDiscoveryIdentity:
    def test_normalize_strips_accents_case_and_whitespace(self) -> None:
        assert normalize("Café  \n  Élève") == "cafe eleve"

    def test_explicit_entity_tokens_splits_and_drops_unknown(self) -> None:
        assert explicit_entity_tokens("WIZARD SPIDER / Evil Corp") == {
            "wizard spider",
            "evil corp",
        }
        assert explicit_entity_tokens("unknown") == set()

    def test_has_other_strong_signal_on_close_titles(self) -> None:
        left = _candidate("Campagne Cavern contre l'énergie", [_source("https://a.example/1")])
        right = _candidate(
            "Campagne Cavern contre l'énergie iranienne", [_source("https://b.example/1")]
        )
        assert has_other_strong_signal(left, right)

    def test_has_other_strong_signal_ignores_sector_and_country(self) -> None:
        left = _candidate(
            "Sujet totalement distinct alpha",
            [_source("https://a.example/1")],
            actors=("Actor A",),
            campaigns=("Campaign A",),
            malware=("MalwareA",),
        )
        right = _candidate(
            "Autre histoire sans rapport beta",
            [_source("https://b.example/1")],
            actors=("Actor B",),
            campaigns=("Campaign B",),
            malware=("MalwareB",),
        )
        # Même secteur et même pays dans le helper `_candidate`, ce qui ne doit
        # jamais suffire à rapprocher deux sujets.
        assert not has_other_strong_signal(left, right)

    def test_shared_strong_urls_ignores_relay_sources(self) -> None:
        left = _candidate("Sujet", [_source("https://a.example/1", role=SourceRole.RELAY)])
        right = _candidate("Autre", [_source("https://a.example/1", role=SourceRole.RELAY)])
        assert shared_strong_urls(left, right) == set()

    def test_canonical_source_key_is_case_insensitive(self) -> None:
        assert canonical_source_key("https://example.com/report") == canonical_source_key(
            "HTTPS://EXAMPLE.COM/report"
        )

    def test_shared_url_alone_does_not_match(self) -> None:
        left = _candidate(
            "Campagne alpha contre le secteur bancaire",
            [_source("https://a.example/synthese")],
            actors=("Actor A",),
            campaigns=("Campaign A",),
            malware=("MalwareA",),
        )
        right = _candidate(
            "Opération beta visant des ONG",
            [_source("https://a.example/synthese")],
            actors=("Actor B",),
            campaigns=("Campaign B",),
            malware=("MalwareB",),
        )
        assert shared_strong_urls(left, right)
        assert not candidates_match_strongly(left, right)


class TestDiscoveryConsolidation:
    def test_no_batches_returns_empty(self) -> None:
        assert consolidate_discovery_batches([]) == []

    def test_case_a_same_subject_same_url(self) -> None:
        """Cas A : même sujet, même URL → 1 sujet, 1 URL, 1 doublon."""
        first = _batch([_candidate("Campagne Cavern", [_source("https://example.com/report")])])
        second = _batch([_candidate("Campagne Cavern", [_source("https://example.com/report")])])

        consolidated = consolidate_discovery_batches([first, second])

        assert len(consolidated) == 1
        assert consolidated[0].contribution_count == 2
        assert len(consolidated[0].sources) == 1
        assert consolidated[0].duplicate_publication_count == 1
        assert len(consolidated[0].member_references) == 2

    def test_case_b_subject_update_adds_new_url(self) -> None:
        """Cas B : (A, B) puis (A, C) → A, B, C."""
        first = _batch(
            [
                _candidate(
                    "Campagne Cavern",
                    [_source("https://example.com/a"), _source("https://example.com/b")],
                )
            ]
        )
        second = _batch(
            [
                _candidate(
                    "Campagne Cavern",
                    [_source("https://example.com/a"), _source("https://example.com/c")],
                )
            ]
        )

        consolidated = consolidate_discovery_batches([first, second])

        assert len(consolidated) == 1
        assert consolidated[0].contribution_count == 2
        assert {source.canonical_url for source in consolidated[0].sources} == {
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        }
        assert consolidated[0].duplicate_publication_count == 1

    def test_case_c_tracking_parameters_are_ignored(self) -> None:
        """Cas C : les paramètres utm_* sont retirés à la canonicalisation."""
        first = _batch(
            [_candidate("Story", [_source("https://example.com/a?utm_source=newsletter")])]
        )
        second = _batch([_candidate("Story", [_source("https://example.com/a")])])

        consolidated = consolidate_discovery_batches([first, second])

        assert len(consolidated) == 1
        assert len(consolidated[0].sources) == 1
        assert consolidated[0].duplicate_publication_count == 1

    def test_case_d_synthesis_shared_by_two_subjects(self) -> None:
        """Cas D : une synthèse commune reste rattachée aux deux sujets distincts."""
        synthesis = "https://example.com/synthese-trimestrielle"
        batch = _batch(
            [
                _candidate(
                    "Campagne alpha contre le secteur bancaire",
                    [_source(synthesis)],
                    actors=("Actor A",),
                    campaigns=("Campaign A",),
                    malware=("MalwareA",),
                ),
                _candidate(
                    "Opération beta visant des ONG",
                    [_source(synthesis)],
                    actors=("Actor B",),
                    campaigns=("Campaign B",),
                    malware=("MalwareB",),
                ),
            ]
        )

        consolidated = consolidate_discovery_batches([batch])

        assert len(consolidated) == 2
        for candidate in consolidated:
            assert [source.canonical_url for source in candidate.sources] == [synthesis]

    def test_case_e_metadata_enrichment(self) -> None:
        """Cas E : une valeur connue comble une valeur inconnue, sans avertissement."""
        first = _batch(
            [
                _candidate(
                    "Rapport",
                    [_source("https://example.com/r", publisher="unknown", published_at=None)],
                )
            ]
        )
        second = _batch(
            [
                _candidate(
                    "Rapport",
                    [
                        _source(
                            "https://example.com/r",
                            publisher="Recorded Future",
                            published_at=date(2026, 7, 16),
                        )
                    ],
                )
            ]
        )

        consolidated = consolidate_discovery_batches([first, second])

        assert len(consolidated) == 1
        merged = consolidated[0].sources[0]
        assert merged.publisher == "Recorded Future"
        assert merged.published_at == date(2026, 7, 16)
        assert consolidated[0].merge_warnings == ()

    def test_case_f_metadata_conflict_is_traced(self) -> None:
        """Cas F : deux valeurs connues contradictoires → choix déterministe + warning."""
        first = _batch(
            [
                _candidate(
                    "Rapport",
                    [
                        _source(
                            "https://example.com/r",
                            publisher="Publisher A",
                            published_at=date(2026, 7, 16),
                        )
                    ],
                )
            ]
        )
        second = _batch(
            [
                _candidate(
                    "Rapport",
                    [
                        _source(
                            "https://example.com/r",
                            publisher="Publisher B",
                            published_at=date(2026, 7, 17),
                        )
                    ],
                )
            ]
        )

        consolidated = consolidate_discovery_batches([first, second])

        assert len(consolidated) == 1
        warnings = consolidated[0].merge_warnings
        assert any("publisher divergent" in warning for warning in warnings)
        assert any("date de publication divergente" in warning for warning in warnings)
        # Choix déterministe, reproductible d'un appel à l'autre.
        assert consolidated[0].sources[0].published_at == date(2026, 7, 16)
        replayed = consolidate_discovery_batches([first, second])
        assert replayed[0].sources[0].publisher == "Publisher A"

    def test_richer_role_wins_over_later_vaguer_contribution(self) -> None:
        first = _batch(
            [_candidate("Rapport", [_source("https://example.com/r", role=SourceRole.PRIMARY)])]
        )
        second = _batch(
            [_candidate("Rapport", [_source("https://example.com/r", role=SourceRole.AGGREGATOR)])]
        )

        consolidated = consolidate_discovery_batches([first, second])

        assert consolidated[0].sources[0].role is SourceRole.PRIMARY

    def test_human_verification_is_never_lost(self) -> None:
        """§23 : un marquage humain n'est pas écrasé par un import plus récent."""
        verified = _source(
            "https://example.com/r",
            verification_status=SourceVerificationStatus.VERIFY_LATER,
            verification_changed_at=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            verification_changed_by="dev-analyst",
        )
        first = _batch([_candidate("Rapport", [verified])])
        second = _batch([_candidate("Rapport", [_source("https://example.com/r")])])

        consolidated = consolidate_discovery_batches([first, second])

        merged = consolidated[0].sources[0]
        assert merged.verification_status is SourceVerificationStatus.VERIFY_LATER
        assert merged.verification_changed_by == "dev-analyst"

    def test_candidate_metadata_is_unioned_and_potential_maximised(self) -> None:
        first = _batch(
            [
                _candidate(
                    "Rapport",
                    [_source("https://example.com/a")],
                    technical_potential=2,
                    iocs=("1.2.3.4",),
                    uncertainties=("Doute A",),
                )
            ]
        )
        second = _batch(
            [
                _candidate(
                    "Rapport",
                    [_source("https://example.com/a")],
                    technical_potential=4,
                    iocs=("5.6.7.8",),
                    uncertainties=("Doute B",),
                )
            ]
        )

        consolidated = consolidate_discovery_batches([first, second])

        representative = consolidated[0].representative
        assert representative.technical_potential == 4
        assert set(representative.iocs) == {"1.2.3.4", "5.6.7.8"}
        assert set(representative.uncertainties) == {"Doute A", "Doute B"}

    def test_original_batches_are_not_mutated(self) -> None:
        """La projection est en lecture seule : les contributions restent auditables."""
        first = _batch(
            [
                _candidate(
                    "Rapport",
                    [_source("https://example.com/r", publisher="unknown", published_at=None)],
                )
            ]
        )
        second = _batch(
            [
                _candidate(
                    "Rapport",
                    [
                        _source(
                            "https://example.com/r",
                            publisher="Recorded Future",
                            published_at=date(2026, 7, 16),
                        )
                    ],
                )
            ]
        )

        consolidate_discovery_batches([first, second])

        original = first.candidates[0].sources[0]
        assert original.publisher == "unknown"
        assert original.published_at is None
