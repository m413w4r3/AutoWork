import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParseResult,
    TechnicalExtraction,
    parse_reference_report,
    validate_synthesis,
)
from cti_app.application.production_prompts import (
    REFERENCES_PROMPT_VERSION,
    SYNTHESIS_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_references import (
    load_legacy_reference_report,
    parse_production_reference_proposals,
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _repair_problem_descriptions,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import DetectionRule, DetectionRuleType
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.publication import ArtifactType


def test_references_prompt_keeps_core_and_supporting_separate() -> None:
    prompt = ProductionPromptTemplates.get_references_prompt(
        subject_title="Subject",
        subject_description="Description",
        actor_info="Actor",
        technical_summary="Summary",
        research_date="2026-08-01",
        period_start="2026-07-01",
        period_end="2026-07-31",
        core_sources_text="- https://core.example/report",
        supporting_sources_text="- https://supporting.example/report",
    )

    assert "**Core Publications**" in prompt
    assert "**Previously Known Supporting References**" in prompt
    assert "they do not replace them" in prompt


def test_references_prompt_separates_linked_technical_sources_without_following_all_links() -> None:
    prompt = ProductionPromptTemplates.get_references_prompt(
        subject_title="Subject",
        subject_description="Description",
        actor_info="Actor",
        technical_summary="Summary",
        research_date="2026-08-01",
        period_start="2026-07-01",
        period_end="2026-07-31",
        core_sources_text="- https://core.example/report",
        supporting_sources_text="",
    )
    one_line = " ".join(prompt.split())

    for linked_resource in (
        "malware sandbox/report page",
        "downloadable IOC TXT/CSV",
        "vendor IOC page",
        "GitHub repository/file",
    ):
        assert linked_resource in one_line
    assert "same subject" in one_line
    assert "Do not turn every hyperlink into a SOURCE" in one_line
    assert REFERENCES_PROMPT_VERSION == "7"
    assert "kind: publication|technical_resource" in one_line
    assert "reason: <short explanation of relevance to the Subject>" in one_line
    assert "editorial-title:" in one_line
    assert "## EVENT R1" in one_line
    assert "# UNCERTAINTIES" in one_line


def _reference_source(
    url: str,
    tier: ProductionReferenceTier,
    *,
    kind: ProductionReferenceKind = ProductionReferenceKind.PUBLICATION,
    state: CollectionState = CollectionState.ARCHIVED,
    document_id=None,
    content_sha256: str | None = "a" * 64,
    proposed_by_model: bool = False,
    role: SourceRole = SourceRole.PRIMARY,
) -> ProductionReferenceSourceV1:
    if document_id is None and state is not CollectionState.UNAVAILABLE:
        document_id = uuid4()
    if state is CollectionState.UNAVAILABLE:
        content_sha256 = None
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=kind,
        role=role,
        title=None,
        publisher=None,
        published_at=None,
        source_collection_id=None,
        source_document_id=document_id,
        discovery_candidate_ids=(),
        collection_state=state,
        content_sha256=content_sha256,
        relevance_reason="Relevant to the subject" if proposed_by_model else None,
        proposed_by_model=proposed_by_model,
        eligible_for_extraction=(
            state
            in {
                CollectionState.ARCHIVED,
                CollectionState.EXTRACTED,
                CollectionState.COMPLETED,
            }
            and document_id is not None
            and content_sha256 is not None
        ),
    )


def _reference_corpus(
    sources: tuple[ProductionReferenceSourceV1, ...],
) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=uuid4(),
        research_date=date(2026, 8, 1),
        production_input_hash="b" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


def test_production_reference_corpus_strictly_validates_schema_hash_url_and_collection_state() -> (
    None
):
    with pytest.raises(ValueError, match="schema version"):
        ProductionReferenceCorpusV1(
            schema_version=2,
            subject_id=uuid4(),
            research_date=date(2026, 8, 1),
            production_input_hash="b" * 64,
            research_status=ProductionReferenceResearchStatus.COMPLETED,
            sources=(),
            warnings=(),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        ProductionReferenceCorpusV1(
            schema_version=1,
            subject_id=uuid4(),
            research_date=date(2026, 8, 1),
            production_input_hash="B" * 64,
            research_status=ProductionReferenceResearchStatus.COMPLETED,
            sources=(),
            warnings=(),
        )
    with pytest.raises(ValueError, match="canonical"):
        _reference_source("https://example.test/report/", ProductionReferenceTier.CORE)
    with pytest.raises(ValueError, match="content hash"):
        _reference_source(
            "https://example.test/report",
            ProductionReferenceTier.CORE,
            content_sha256="A" * 64,
        )
    with pytest.raises(ValueError, match="collection state"):
        ProductionReferenceSourceV1(
            canonical_url="https://example.test/report",
            tier=ProductionReferenceTier.CORE,
            kind=ProductionReferenceKind.PUBLICATION,
            role=SourceRole.PRIMARY,
            title=None,
            publisher=None,
            published_at=None,
            source_collection_id=None,
            source_document_id=None,
            discovery_candidate_ids=(),
            collection_state="archived",
            content_sha256="a" * 64,
            relevance_reason=None,
            proposed_by_model=False,
            eligible_for_extraction=True,
        )


def test_production_reference_corpus_round_trip_orders_sources_and_excludes_legacy_fields() -> None:
    corpus = _reference_corpus(
        (
            _reference_source("https://z.example/report", ProductionReferenceTier.TECHNICAL),
            _reference_source("https://support.example/report", ProductionReferenceTier.SUPPORTING),
            _reference_source("https://z-core.example/report", ProductionReferenceTier.CORE),
            _reference_source("https://a-core.example/report", ProductionReferenceTier.CORE),
        )
    )
    payload = production_reference_corpus_to_json(corpus)

    assert [source["tier"] for source in payload["sources"]] == [
        "core",
        "core",
        "supporting",
        "technical",
    ]
    assert [source["canonical_url"] for source in payload["sources"]] == [
        "https://a-core.example/report",
        "https://z-core.example/report",
        "https://support.example/report",
        "https://z.example/report",
    ]
    round_trip = production_reference_corpus_to_json(production_reference_corpus_from_json(payload))
    assert round_trip == payload
    assert "production_run_id" not in payload
    assert "events" not in payload
    assert "editorial_title" not in payload
    with pytest.raises(ValueError, match="invalid shape"):
        production_reference_corpus_from_json({**payload, "production_run_id": str(uuid4())})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("relevance_reason", None, "relevance reason"),
        ("relevance_reason", "", "relevance reason"),
        ("relevance_reason", "   ", "relevance reason"),
        ("tier", "technical", "SUPPORTING"),
        ("kind", "technical_resource", "TECHNICAL"),
        ("tier", "core", "CORE"),
    ),
)
def test_production_reference_corpus_from_json_rejects_invalid_model_sources(
    field: str,
    value: str | None,
    message: str,
) -> None:
    payload = production_reference_corpus_to_json(
        _reference_corpus(
            (
                _reference_source(
                    "https://model.example/report",
                    ProductionReferenceTier.SUPPORTING,
                    proposed_by_model=True,
                ),
            )
        )
    )
    payload["sources"][0][field] = value

    with pytest.raises(ValueError, match=message):
        production_reference_corpus_from_json(payload)


def test_production_reference_corpus_from_json_accepts_valid_model_source_kinds() -> None:
    corpus = _reference_corpus(
        (
            _reference_source(
                "https://publication.example/report",
                ProductionReferenceTier.SUPPORTING,
                proposed_by_model=True,
            ),
            _reference_source(
                "https://technical.example/resource",
                ProductionReferenceTier.TECHNICAL,
                kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
                proposed_by_model=True,
            ),
        )
    )

    payload = production_reference_corpus_to_json(corpus)
    round_trip = production_reference_corpus_from_json(payload)

    assert production_reference_corpus_to_json(round_trip) == payload


def test_production_reference_corpus_core_wins_duplicate_canonical_url() -> None:
    core = _reference_source(
        "https://same.example/report",
        ProductionReferenceTier.CORE,
        proposed_by_model=False,
        role=SourceRole.PRIMARY,
    )
    proposal = _reference_source(
        "https://same.example/report",
        ProductionReferenceTier.SUPPORTING,
        proposed_by_model=True,
        role=SourceRole.RELAY,
    )

    corpus = _reference_corpus((proposal, core))

    assert len(corpus.sources) == 1
    assert corpus.sources[0].tier is ProductionReferenceTier.CORE
    assert corpus.sources[0].role is SourceRole.PRIMARY


def test_production_reference_proposal_parser_is_tolerant_and_reads_source_blocks_only() -> None:
    parsed = parse_production_reference_proposals(
        """# REFERENCES
editorial-title: Legacy title
## SOURCE S1
title: First
url: https://example.test/report?utm_source=mail
role: primary
kind: publication
reason: Primary coverage of the subject
## SOURCE S2
url: file:///tmp/local.txt
reason: Invalid URL
## SOURCE S3
title: Future
url: https://future.example/report
published-at: 2026-08-02
kind: technical_resource
reason: Future source
## SOURCE S4
title: Missing kind
url: https://missing-kind.example/report
reason: Supporting coverage
## SOURCE S5
url: https://invalid-kind.example/report
kind: dataset
reason: Invalid kind
## SOURCE S6
title: Duplicate
url: https://example.test/report
kind: technical_resource
reason: Duplicates the first canonical URL
## EVENT R1
date: 2026-07-01
sources: S1
text: Ignored legacy event
# UNCERTAINTIES
- ignored by the canonical proposal parser
""",
        date(2026, 8, 1),
    )

    assert parsed.usable
    assert parsed.value is not None
    assert [proposal.canonical_url for proposal in parsed.value] == [
        "https://example.test/report",
        "https://missing-kind.example/report",
    ]
    assert parsed.value[0].kind is ProductionReferenceKind.PUBLICATION
    assert parsed.value[0].tier is ProductionReferenceTier.SUPPORTING
    assert parsed.value[1].kind is ProductionReferenceKind.PUBLICATION
    assert "reference_invalid_url" in parsed.warnings
    assert "reference_future_date" in parsed.warnings
    assert "reference_kind_missing_defaulted_to_publication" in parsed.warnings
    assert "reference_invalid_kind" in parsed.warnings
    assert "reference_duplicate_url_ignored" in parsed.warnings


def test_production_reference_proposal_requires_reason_and_accepts_no_new_sources() -> None:
    missing_reason = parse_production_reference_proposals(
        "## SOURCE S1\nurl: https://example.test/report\nkind: publication",
        date(2026, 8, 1),
    )
    empty = parse_production_reference_proposals(
        "# REFERENCES\neditorial-title: legacy\n## EVENT R1\ntext: ignored",
        date(2026, 8, 1),
    )

    assert missing_reason.usable and missing_reason.value == ()
    assert "reference_missing_reason" in missing_reason.warnings
    assert empty.usable and empty.value == ()


def test_legacy_reference_projection_filters_to_eligible_corpus_sources_and_handles_v4() -> None:
    corpus = _reference_corpus(
        (
            _reference_source("https://eligible.example/report", ProductionReferenceTier.CORE),
            _reference_source(
                "https://unavailable.example/report",
                ProductionReferenceTier.SUPPORTING,
                state=CollectionState.UNAVAILABLE,
                proposed_by_model=True,
            ),
        )
    )
    raw = """# REFERENCES
editorial-title: Legacy title
## SOURCE S1
title: Eligible
url: https://eligible.example/report
## SOURCE S2
title: Unavailable
url: https://unavailable.example/report
## EVENT R1
date: 2026-07-01
sources: S1, S2
text: Backed by eligible and unavailable sources
## EVENT R2
date: 2026-07-02
sources: S2
text: Unbacked after filtering
"""

    projected = load_legacy_reference_report(raw, date(2026, 8, 1), corpus=corpus)
    imported = load_legacy_reference_report(raw, date(2026, 8, 1), legacy_imported=True)

    assert [source.canonical_url for source in projected.sources] == [
        "https://eligible.example/report"
    ]
    assert [event.source_ids for event in projected.events] == [("S1",)]
    assert projected.editorial_title == "Legacy title"
    # Historical V4 data stays legacy; no V1 facts are fabricated.
    assert len(imported.sources) == 2


def test_synthesis_pack_assigns_tiers_without_defaulting_unknown_to_core() -> None:
    report = parse_reference_report(
        """# REFERENCES
editorial-title: Test
## SOURCE S1
title: Core
url: https://core.example/report
publisher: Core
published-at: 2026-07-01
role: primary
## SOURCE S2
title: Supporting
url: https://supporting.example/report
publisher: Supporting
published-at: 2026-07-02
role: independent
## SOURCE S3
title: Unknown
url: https://unknown.example/report
publisher: Unknown
published-at: 2026-07-03
role: unknown
## EVENT R1
date: 2026-07-03
sources: S1, S2, S3
text: Event
""",
        date(2026, 8, 1),
    ).value
    assert report is not None
    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        report,
        SimpleNamespace(items=(), uncertainties=()),
        {
            "https://core.example/report": "core",
            "https://supporting.example/report": "supporting",
        },
    )

    assert pack["version"] == "7"
    assert [source["tier"] for source in pack["reference_report"]["sources"]] == [
        "core",
        "supporting",
        "unknown",
    ]


def test_synthesis_prompt_makes_core_backbone_without_quota() -> None:
    prompt = ProductionPromptTemplates.get_synthesis_prompt("Subject")

    assert "CORE sources are the editorial backbone" in prompt
    assert "SUPPORTING sources are secondary evidence" in prompt
    assert "percentage" not in prompt.lower()
    assert "quota" not in prompt.lower()


def test_synthesis_prompt_version_matches_v8_template() -> None:
    assert SYNTHESIS_PROMPT_VERSION == "8"
    assert hasattr(ProductionPromptTemplates, "TECHNICAL_SYNTHESIS_V8")


def test_synthesis_prompt_requires_technical_cti_depth() -> None:
    """The V8 contract, not any particular model wording."""
    prompt = ProductionPromptTemplates.get_synthesis_prompt("Subject")
    lowered = prompt.lower()

    # Editorial priority: the discriminating CTI axes must be requested.
    for requirement in (
        "infection and execution chain",
        "processes, tools and commands",
        "persistence, privilege, evasion or anti-analysis mechanisms",
        "c2 protocols, communication structure and infrastructure role",
        "behavioral hunting or detection pivots",
        "differences between variants, campaigns or operators",
        "attribution",
    ):
        assert requirement in lowered

    # Concrete technical values must survive the IOC-inventory prohibition.
    for retained in (
        "parent/child execution relationships",
        "command-line patterns",
        "registry or scheduled-task persistence",
        "distinctive file paths",
        "local ports",
    ):
        assert retained in lowered
    assert "not an ioc inventory" in lowered or "not an IOC inventory" in prompt

    # Shape without a rigid word count.
    assert "3 to 6 dense paragraphs" in prompt

    # Attribution caution.
    assert "stronger attribution than the evidence supports" in prompt

    # Supporting sources keep their technical value.
    assert "solely\nbecause it comes from a supporting source" in prompt

    # No generic SOC playbook.
    assert "Il est recommandé de\nbloquer" in prompt
    assert "Les organisations devraient" in prompt
    assert "Never invent a SOC playbook." in prompt

    # Literal detection rules are reserved for future annexes; behavioral
    # detection and hunting pivots remain part of the main synthesis.
    for rule_format in ("yara", "sigma", "suricata", "snort"):
        assert rule_format not in lowered

    # Timeline restatement is discouraged.
    assert "Do not repeat chronology merely to restate the reference timeline." in prompt

    # Preserved invariants.
    assert "Produce no Markdown title or heading." in prompt
    assert "Produce no raw URL." in prompt
    assert "Do not reproduce the IOC section as a" in prompt
    assert "no final bibliography" in prompt

    # V8 lets Q4 select exact technical values for analytical reasons.
    assert "any exact technical indicator or artifact supplied" in prompt
    assert "materially improves the analysis" in prompt
    assert "presence of a value in the final IOC section does not prevent" in prompt
    assert "raw inventory" in prompt
    assert "mechanically enumerate indicators" in prompt


def test_synthesis_repair_prompt_stays_structural() -> None:
    repaired = ProductionPromptTemplates.get_format_repair_prompt(
        stage="synthesis", problems=["heading"]
    )

    assert "Do not research, add, remove, or alter any fact." in repaired
    assert "deepen" not in repaired.lower()
    assert "improve" not in repaired.lower()


def _item(category: str, value: str, context: str) -> ExtractionItem:
    return ExtractionItem(
        local_id=f"E{abs(hash(value)) % 1000}",
        category=category,
        value=value,
        context=context,
        artifact_type=None,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        indicator_status=IndicatorStatus.CONTEXTUAL,
        display_policy=DisplayPolicy.BODY_ONLY,
    )


def _minimal_report():
    report = parse_reference_report(
        """# REFERENCES
editorial-title: Test
## SOURCE S1
title: Core
url: https://core.example/report
publisher: Core
published-at: 2026-07-01
role: primary
## EVENT R1
date: 2026-07-01
sources: S1
text: Event
""",
        date(2026, 8, 1),
    ).value
    assert report is not None
    return report


def test_synthesis_pack_keeps_behavioral_categories() -> None:
    extraction = TechnicalExtraction(
        items=(
            _item("infection_chain", "wscript.exe lance deno.exe", "chaîne"),
            _item("commands", "curl -o deno.exe https://…", "téléchargement runtime"),
            _item("persistence", "clé Run HKCU\\...\\Run", "persistance"),
            _item("protocols", "HTTP POST /api/v1/ping", "C2"),
            _item("other_technical", "port local 51337", "loopback"),
        ),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    categories = {item["category"] for item in pack["technical_extraction"]["items"]}
    assert categories == {
        "infection_chain",
        "commands",
        "persistence",
        "protocols",
        "other_technical",
    }
    values = {item["value"] for item in pack["technical_extraction"]["items"]}
    assert "clé Run HKCU\\...\\Run" in values
    assert "port local 51337" in values


def test_synthesis_pack_excludes_detection_rules_and_preserves_extraction() -> None:
    rule = DetectionRule(
        rule_type=DetectionRuleType.YARA,
        name="APT_ExampleRAT_loader",
        body='rule APT_ExampleRAT_loader { strings: $a = "SECRET_BODY" condition: $a }',
        source_ids=("S1",),
        context="règle publiée par la source",
        evidence_quote="citation interne",
        supported=True,
        model_run_ids=("run-1",),
        sha256="a" * 64,
    )
    unsupported = DetectionRule(
        rule_type=DetectionRuleType.SIGMA,
        name="unsupported_rule",
        body="title: unsupported",
        source_ids=("S1",),
        context="",
        evidence_quote="",
        supported=False,
        model_run_ids=(),
        sha256="b" * 64,
    )
    extraction = TechnicalExtraction(items=(), uncertainties=(), rules=(rule, unsupported))

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    assert pack["version"] == "7"
    assert "detection_rules" not in pack["technical_extraction"]
    assert extraction.rules == (rule, unsupported)
    serialized = json.dumps(pack, ensure_ascii=False)
    assert "SECRET_BODY" not in serialized
    assert "APT_ExampleRAT_loader" not in serialized
    assert "citation interne" not in serialized
    assert "run-1" not in serialized
    assert "a" * 64 not in serialized
    assert "unsupported_rule" not in serialized


def test_synthesis_pack_omits_detection_rules_when_none() -> None:
    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(),
        TechnicalExtraction(items=(), uncertainties=(), rules=()),
        {"https://core.example/report": "core"},
    )

    assert "detection_rules" not in pack["technical_extraction"]


# --- Q4 pack: body detail keeps its value, IOC-section noise is dropped -----


def _artifact_item(
    local_id: str,
    value: str,
    artifact_type: ArtifactType,
    *,
    status: IndicatorStatus = IndicatorStatus.CONFIRMED_IOC,
    policy: DisplayPolicy = DisplayPolicy.IOC_SECTION,
    category: str = "network_artifacts",
    context: str = "",
    supported: bool = True,
) -> ExtractionItem:
    return ExtractionItem(
        local_id=local_id,
        category=category,
        value=value,
        context=context,
        artifact_type=artifact_type,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=supported,
        indicator_status=status,
        display_policy=policy,
    )


def _dust_specter_pack_extraction() -> TechnicalExtraction:
    hashes = tuple(
        _artifact_item(f"H{index}", f"{index:064x}", ArtifactType.HASH) for index in range(40)
    )
    domains = tuple(
        _artifact_item(f"D{index}", f"c2-{index}.example", ArtifactType.DOMAIN)
        for index in range(10)
    )
    files = tuple(
        _artifact_item(
            f"F{index}",
            name,
            ArtifactType.FILENAME,
            policy=DisplayPolicy.BODY_ONLY,
            category="files",
            context="chaîne d'exécution",
        )
        for index, name in enumerate(("libvlc.dll", "in.txt", "hostfxr.dll"))
    )
    filepath = _artifact_item(
        "P1",
        "C:\\Users\\Public\\twintask\\in.txt",
        ArtifactType.FILEPATH,
        policy=DisplayPolicy.BODY_ONLY,
        category="files",
        context="chemin de travail",
    )
    cve = _artifact_item(
        "C1",
        "CVE-2026-1234",
        ArtifactType.CVE,
        policy=DisplayPolicy.BODY_ONLY,
        category="cves",
        context="vulnérabilité exploitée",
    )
    behavioral = (
        _item("infection_chain", "VLC.exe charge libvlc.dll", "side-loading"),
        _item("persistence", "tâche planifiée TWINTASK", "persistance"),
    )
    return TechnicalExtraction(
        items=(*hashes, *domains, *files, filepath, cve, *behavioral),
        uncertainties=(),
    )


def test_synthesis_pack_keeps_all_visible_supported_items() -> None:
    extraction = _dust_specter_pack_extraction()

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    items = pack["technical_extraction"]["items"]
    assert pack["version"] == "7"

    assert len(items) == len(extraction.items)

    values = {item.get("value") for item in items}
    for expected in (
        "libvlc.dll",
        "in.txt",
        "hostfxr.dll",
        "C:\\Users\\Public\\twintask\\in.txt",
        "CVE-2026-1234",
    ):
        assert expected in values

    assert "c2-0.example" in values
    assert f"{0:064x}" in values

    # The canonical extraction is untouched.
    assert len(extraction.items) == 57
    assert sum(item.artifact_type is ArtifactType.HASH for item in extraction.items) == 40


def test_synthesis_pack_keeps_ioc_section_rows_that_still_carry_context() -> None:
    extraction = TechnicalExtraction(
        items=(_artifact_item("D1", "c2.example", ArtifactType.DOMAIN, context="serveur de C2"),),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    items = pack["technical_extraction"]["items"]
    assert len(items) == 1
    assert items[0]["value"] == "c2.example"
    assert items[0]["context"] == "serveur de C2"


def test_synthesis_pack_merges_typed_and_contextual_duplicate_values() -> None:
    value = r"%LOCALAPPDATA%\...\burn.exe"
    extraction = TechnicalExtraction(
        items=(
            _artifact_item("F1", value, ArtifactType.FILEPATH, category="files"),
            _item("files", value, "GhostFetch persistence copy"),
        ),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    items = pack["technical_extraction"]["items"]
    assert len(items) == 1
    assert items[0]["artifact_type"] == "filepath"
    assert items[0]["context"] == "GhostFetch persistence copy"


# --- Q4 pack: visible canonical values reach the model ----------------------


def test_synthesis_pack_exposes_canonical_indicator_values() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact_item("D1", "meetingapp.site", ArtifactType.DOMAIN),
            _artifact_item("I1", "203.0.113.9", ArtifactType.IP),
            _artifact_item("H1", "a" * 64, ArtifactType.HASH),
            _artifact_item("E1", "operator@example.com", ArtifactType.EMAIL),
            _artifact_item(
                "F1", "libvlc.dll", ArtifactType.FILENAME, policy=DisplayPolicy.BODY_ONLY
            ),
            _artifact_item(
                "P1",
                "C:\\Users\\Public\\payload.dll",
                ArtifactType.FILEPATH,
                policy=DisplayPolicy.BODY_ONLY,
            ),
        ),
        uncertainties=(),
    )

    report = _minimal_report()
    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        report, extraction, {"https://core.example/report": "core"}
    )
    values = {item["value"] for item in pack["technical_extraction"]["items"]}
    assert values == {
        "meetingapp.site",
        "203.0.113.9",
        "a" * 64,
        "operator@example.com",
        "libvlc.dll",
        "C:\\Users\\Public\\payload.dll",
    }

    synthesis = (
        "Le second étage est récupéré depuis meetingapp.site ; l'implant communique "
        "avec 203.0.113.9 et l'échantillon a pour SHA-256 "
        + "a"
        * 64
        + ". L'opérateur utilise operator@example.com ; VLC.exe charge libvlc.dll "
        "depuis C:\\Users\\Public\\payload.dll [S1]."
    )
    result = validate_synthesis(synthesis, report, extraction)
    assert result.usable, result.errors


def test_synthesis_pack_filters_unsupported_excluded_and_hidden_items() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact_item("V1", "visible.example", ArtifactType.DOMAIN),
            _artifact_item("U1", "unsupported.example", ArtifactType.DOMAIN, supported=False),
            _artifact_item(
                "E1",
                "excluded.example",
                ArtifactType.DOMAIN,
                status=IndicatorStatus.EXCLUDED,
            ),
            _artifact_item(
                "H1", "hidden.example", ArtifactType.DOMAIN, policy=DisplayPolicy.HIDDEN
            ),
        ),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    assert [item["value"] for item in pack["technical_extraction"]["items"]] == ["visible.example"]


def test_dust_specter_exact_values_are_not_rejected_or_enumerated() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact_item("D1", "meetingapp.site", ArtifactType.DOMAIN),
            _artifact_item(
                "F1", "libvlc.dll", ArtifactType.FILENAME, policy=DisplayPolicy.BODY_ONLY
            ),
            _artifact_item(
                "F2", "hostfxr.dll", ArtifactType.FILENAME, policy=DisplayPolicy.BODY_ONLY
            ),
        ),
        uncertainties=(),
    )
    text = (
        "La chaîne ClickFix récupère un second étage depuis meetingapp[.]site, "
        "puis VLC.exe charge libvlc.dll avant le chargement de hostfxr.dll [S1]."
    )

    result = validate_synthesis(text, _minimal_report(), extraction)

    assert result.usable, result.errors
    assert "mass_network_enumeration" not in result.errors
    assert "mass_hash_enumeration" not in result.errors


def test_repair_problem_descriptions_fall_back_to_codes() -> None:
    result: ParseResult[str] = ParseResult()
    result.errors.append("empty_response")

    assert _repair_problem_descriptions(result) == ["empty_response"]
