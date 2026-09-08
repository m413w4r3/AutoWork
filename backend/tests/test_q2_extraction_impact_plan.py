"""LOT 42 -- the Q2 extraction impact plan is the only source of truth.

A production repair must never cost a model call it cannot justify. These
tests pin the exact number of provider calls for the business events an
analyst can trigger, and pin the plan event that explains them before any
call is made.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_q2_batch
from cti_app.application.production_parsers import ParsedSource, ReferenceReport
from cti_app.application.production_workflow import plan_q2_extraction_profiles
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    ExtractionImpactPlan,
    ExtractionProfile,
    ProductionDerivedOutput,
    ProductionInputSource,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    Q2SourceDisposition,
    Q2SourceImpact,
    RepairImpactInvariantError,
    SubjectProductionRun,
    SubjectProductionStage,
)
from tests.test_production_extraction_profiles import (
    _archived_document,
    _ArchivedBlobs,
    _cached_orchestrator,
    _CacheState,
    _CacheStore,
    _collection_for,
    _ExtractionSink,
    _input_source,
    _snapshot,
)
from tests.test_production_q2_batch import _batch_response, _BatchGateway, _source

# A core source is the one URL the snapshot freezes as FULL; every other
# source planned here is IOC_RULES and therefore batchable.
_CORE_URL = "https://example.test/core"


def _ioc_block(index: int) -> str:
    return f"IOC confirmed domain\n- domain-{index}.security-lab.io"


class _CountingGateway(_BatchGateway):
    """A gateway that answers any batch or individual request it is given.

    Unlike the fixed-script `_BatchGateway`, it derives the answer from the
    request itself, so a test may assert the *number* of calls without having
    to predict their grouping in advance -- which is exactly what the planner
    is being tested on.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.batch_sizes: list[int] = []

    async def execute(self, request: Any, role: Any) -> Any:
        del role
        markers = [
            batch_id
            for batch_id in (f"B{index}" for index in range(1, 32))
            if f"{batch_id} https://" in request.text
        ]
        self.calls.append(request)
        self.batch_sizes.append(len(markers) or 1)
        if markers:
            response = _batch_response(*(_ioc_block(index) for index in range(1, len(markers) + 1)))
        else:
            response = _ioc_block(1)
        return SimpleNamespace(
            output_text=response,
            run=SimpleNamespace(
                id=request.run_id,
                status=ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            metadata={},
        )


def _content(url: str, revision: int = 1) -> bytes:
    return (f"ARCHIVED r{revision} {url} " * 60).encode()


class _Corpus:
    """A Q1 corpus of archived sources whose captures a test can rewrite."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, count: int) -> None:
        self.subject = uuid4()
        self.blobs = _ArchivedBlobs()
        self.sources = tuple(_source(index) for index in range(1, count + 1))
        self.documents: dict[UUID, SimpleNamespace] = {}
        self.collections: dict[UUID, SimpleNamespace] = {}
        self._collection_ids: dict[str, UUID] = {}
        for source in self.sources:
            self._install(source.canonical_url, revision=1)
        self.state = _CacheState(self.documents, self.collections)
        # One payload store for the whole corpus: a checkpoint is only
        # reusable if the bytes it points at outlive the run that wrote them.
        self.store = _CacheStore()
        # By default no corpus source is in the frozen snapshot, so each is a
        # supplemental IOC_RULES source. A test may freeze explicit roles.
        self.snapshot_sources: tuple[ProductionInputSource, ...] = (
            _input_source(_CORE_URL, date(2026, 7, 10)),
        )
        self._monkeypatch = monkeypatch

    def _install(self, url: str, *, revision: int) -> None:
        document = _archived_document(
            subject_id=self.subject,
            url=url,
            content=_content(url, revision),
            blobs=self.blobs,
        )
        self.documents[document.id] = document
        collection_id = self._collection_ids.setdefault(url, uuid4())
        self.collections[collection_id] = _collection_for(document, url)

    def recapture(self, source_id: str, *, revision: int) -> None:
        """Give one source a brand-new archived capture: a new SHA-256."""
        url = next(s.canonical_url for s in self.sources if s.local_id == source_id)
        stale = [key for key, doc in self.documents.items() if doc.final_url == url]
        for key in stale:
            del self.documents[key]
        self._install(url, revision=revision)

    def orchestrator(self, gateway: _CountingGateway) -> Any:
        sink = _ExtractionSink()
        orchestrator = _cached_orchestrator(
            self.state,
            gateway,  # type: ignore[arg-type]
            self.store,
            sink,
            self._monkeypatch,
            self.blobs,
        )
        report = ReferenceReport(sources=self.sources, events=())

        async def load_report(*args: object) -> ReferenceReport:
            del args
            return report

        orchestrator._load_reference_report = load_report
        return orchestrator

    def run(self) -> SubjectProductionRun:
        run = SubjectProductionRun(
            subject_id=self.subject,
            edition_id=uuid4(),
            current_stage=SubjectProductionStage.EXTRACTION,
        )
        self.state._runs[run.id] = run
        return run


async def _extract(corpus: _Corpus, gateway: _CountingGateway) -> dict[str, Any]:
    orchestrator = corpus.orchestrator(gateway)
    run = corpus.run()
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot(corpus.snapshot_sources),
    )
    outcome: dict[str, Any] = dict(result)
    outcome["_events"] = orchestrator._diagnostics.events
    return outcome


def _plan_event(result: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        item for item in result["_events"] if item.get("event") == "q2.extraction.plan"
    ]
    assert len(events) == 1, f"expected exactly one plan event, got {len(events)}"
    return events[0]


# --------------------------------------------------------------------------
# The plan object itself
# --------------------------------------------------------------------------


def _impact(
    source_id: str,
    disposition: Q2SourceDisposition,
    *,
    reason: str = "no_checkpoint",
    batch_index: int | None = None,
    primary: str | None = None,
) -> Q2SourceImpact:
    return Q2SourceImpact(
        source_id=source_id,
        canonical_url=f"https://example.test/{source_id}",
        disposition=disposition,
        profile=ExtractionProfile.IOC_RULES,
        reason=reason,
        primary_source_id=primary,
        batch_index=batch_index,
    )


def test_estimated_calls_counts_one_per_batch_and_one_per_individual_source() -> None:
    plan = ExtractionImpactPlan(
        sources=(
            _impact("S1", Q2SourceDisposition.EXTRACT_BATCHED, batch_index=0),
            _impact("S2", Q2SourceDisposition.EXTRACT_BATCHED, batch_index=0),
            _impact("S3", Q2SourceDisposition.EXTRACT_INDIVIDUAL),
            _impact("S4", Q2SourceDisposition.REUSED, reason="reusable_checkpoint"),
            _impact("S5", Q2SourceDisposition.CONTENT_DUPLICATE, primary="S4"),
        ),
        batches=(("S1", "S2"),),
    )

    # Three sources are extracted but only two calls are billed: the batch is
    # one request.
    assert len(plan.extracted) == 3
    assert plan.estimated_model_calls == 2
    assert plan.model_calls_avoided == 2
    assert plan.total_sources == 5


def test_plan_payload_justifies_every_source() -> None:
    plan = ExtractionImpactPlan(
        sources=(
            _impact("S1", Q2SourceDisposition.EXTRACT_INDIVIDUAL, reason="source_content_changed"),
            _impact("S2", Q2SourceDisposition.REUSED, reason="reusable_checkpoint"),
        )
    )
    payload = plan.as_event_payload()

    assert payload["total_sources"] == 2
    assert payload["estimated_calls"] == 1
    assert payload["extracted"] == [
        {
            "source": "S1",
            "url": "https://example.test/S1",
            "reason": "source_content_changed",
            "profile": "ioc_rules",
        }
    ]
    assert payload["reused"][0]["reason"] == "reusable_checkpoint"
    # Every source carries a reason: no call is ever unexplained.
    assert all(entry["reason"] for entry in payload["extracted"] + payload["reused"])


# --------------------------------------------------------------------------
# CAS 5 / CAS 8 -- identical content is never re-read
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_shas_reuse_every_checkpoint_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _Corpus(monkeypatch, 6)
    first = _CountingGateway()
    assert (await _extract(corpus, first))["status"] == "success"
    assert first.calls, "the first pass must actually read the corpus"

    second = _CountingGateway()
    result = await _extract(corpus, second)

    assert result["status"] == "success", result
    assert second.calls == [], "unchanged content must never reach the provider"
    assert result["model_calls"] == 0
    assert result["cache_hits"] == 6

    plan = _plan_event(result)
    assert plan["estimated_calls"] == 0
    assert len(plan["reused"]) == 6
    assert plan["extracted"] == []
    assert {entry["reason"] for entry in plan["reused"]} == {"reusable_checkpoint"}


@pytest.mark.asyncio
async def test_ten_sources_with_one_new_sha_replay_only_that_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS 6 + CAS 8: 10 sources, 9 unchanged -> 9 reuses and 1 extraction."""
    corpus = _Corpus(monkeypatch, 10)
    assert (await _extract(corpus, _CountingGateway()))["status"] == "success"

    corpus.recapture("S4", revision=2)
    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)

    assert result["status"] == "success", result
    assert result["model_calls"] == 1, "only the recaptured source may be re-read"
    assert result["cache_hits"] == 9

    plan = _plan_event(result)
    assert plan["estimated_calls"] == 1
    assert [entry["source"] for entry in plan["extracted"]] == ["S4"]
    assert plan["extracted"][0]["reason"] == "source_content_changed"
    assert len(plan["reused"]) == 9
    assert "S4" not in {entry["source"] for entry in plan["reused"]}


# --------------------------------------------------------------------------
# The partition/reuse ordering: residual sources must share one batch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_sources_from_distinct_partitions_share_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse is filtered before partitioning, so the residue stays contiguous.

    S1 and S5 fall in different `MAX_Q2_BATCH_SOURCES` windows of the full
    candidate list. Partitioning first would leave each window with a single
    unreusable source and bill two individual calls; filtering first leaves
    one contiguous residue of two and bills a single batch.
    """
    assert production_q2_batch.MAX_Q2_BATCH_SOURCES == 4
    corpus = _Corpus(monkeypatch, 8)
    assert (await _extract(corpus, _CountingGateway()))["status"] == "success"

    corpus.recapture("S1", revision=2)
    corpus.recapture("S5", revision=2)
    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)

    assert result["status"] == "success", result
    assert result["model_calls"] == 1, "two residual sources are one batch, not two calls"
    assert result["light_batches"] == 1
    assert result["light_sources_batched"] == 2
    assert gateway.batch_sizes == [2]

    plan = _plan_event(result)
    assert plan["estimated_calls"] == 1
    assert plan["batches"] == [["S1", "S5"]]
    assert {entry["source"] for entry in plan["extracted"]} == {"S1", "S5"}
    assert len(plan["reused"]) == 6


@pytest.mark.asyncio
async def test_a_lone_residual_source_still_uses_the_individual_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _Corpus(monkeypatch, 8)
    assert (await _extract(corpus, _CountingGateway()))["status"] == "success"

    corpus.recapture("S7", revision=2)
    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)

    assert result["model_calls"] == 1
    assert result["light_batches"] == 0
    plan = _plan_event(result)
    assert plan["batches"] == []
    assert [entry["source"] for entry in plan["extracted"]] == ["S7"]


# --------------------------------------------------------------------------
# CAS 1 / CAS 2 -- adding a source re-reads only that source
# --------------------------------------------------------------------------


def _extra_source(index: int) -> ParsedSource:
    url = f"https://example.test/added-{index}"
    return ParsedSource(
        local_id=f"S{index}",
        title=f"Added {index}",
        url=url,
        canonical_url=url,
        publisher="Publisher",
        published_at=date(2026, 7, 12),
        role=SourceRole.INDEPENDENT,
    )


@pytest.mark.asyncio
async def test_adding_a_source_extracts_only_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS 1/CAS 2: a new source costs one reading; the corpus keeps its own."""
    corpus = _Corpus(monkeypatch, 5)
    assert (await _extract(corpus, _CountingGateway()))["status"] == "success"

    added = _extra_source(6)
    corpus.sources = (*corpus.sources, added)
    corpus._install(added.canonical_url, revision=1)

    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)

    assert result["status"] == "success", result
    assert result["model_calls"] == 1
    assert result["cache_hits"] == 5

    plan = _plan_event(result)
    assert plan["total_sources"] == 6
    assert [entry["source"] for entry in plan["extracted"]] == ["S6"]
    assert plan["extracted"][0]["reason"] == "no_checkpoint"
    assert plan["estimated_calls"] == 1


@pytest.mark.asyncio
async def test_the_plan_is_recorded_before_the_first_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PHASE 5: the plan must be readable before any cost is incurred."""
    corpus = _Corpus(monkeypatch, 4)
    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)
    assert result["status"] == "success"

    events = [str(item.get("event")) for item in result["_events"]]
    plan_at = events.index("q2.extraction.plan")
    started = [index for index, name in enumerate(events) if name.endswith(".started")]
    assert started, "this run must contain at least one provider call"
    assert plan_at < min(started), events

    plan = _plan_event(result)
    assert plan["estimated_calls"] == len(gateway.calls) == 1
    assert plan["batches"] == [["S1", "S2", "S3", "S4"]]


# --------------------------------------------------------------------------
# CAS 3 / CAS 4 -- deterministic repairs may never buy a model call
# --------------------------------------------------------------------------


def test_a_publication_only_repair_can_never_declare_a_model_call() -> None:
    """CAS 3: correcting a hallucinated IOC rebuilds the projection only."""
    impact = ProductionRepairImpact(
        kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
        affected_outputs=frozenset({ProductionDerivedOutput.PUBLICATION}),
        model_call_required=False,
        reason="IOC value corrected",
    )
    assert impact.execution_plan.expected_q2_calls == 0
    assert impact.execution_plan.model_call_required is False
    assert impact.execution_plan.provider_steps == ()

    with pytest.raises(RepairImpactInvariantError):
        ProductionRepairImpact(
            kind=ProductionRepairImpactKind.PUBLICATION_ONLY,
            affected_outputs=frozenset({ProductionDerivedOutput.PUBLICATION}),
            model_call_required=True,
            reason="IOC value corrected",
        )


def test_a_rule_bundle_repair_can_never_stale_extraction_or_synthesis() -> None:
    """CAS 4: adding a detection rule touches the bundle and publication only."""
    impact = ProductionRepairImpact(
        kind=ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        affected_outputs=frozenset(
            {ProductionDerivedOutput.RULE_BUNDLE, ProductionDerivedOutput.PUBLICATION}
        ),
        model_call_required=False,
        reason="rule admitted",
    )
    assert ProductionDerivedOutput.EXTRACTION not in impact.affected_outputs
    assert impact.model_call_required is False

    for forbidden in (ProductionDerivedOutput.SYNTHESIS, ProductionDerivedOutput.REFERENCES):
        with pytest.raises(RepairImpactInvariantError):
            ProductionRepairImpact(
                kind=ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
                affected_outputs=frozenset({ProductionDerivedOutput.RULE_BUNDLE, forbidden}),
                model_call_required=False,
                reason="rule admitted",
            )


# --------------------------------------------------------------------------
# The scope of a reading is the discovery role, not snapshot membership
# --------------------------------------------------------------------------


def _snapshot_source(url: str, role: SourceRole) -> ProductionInputSource:
    return ProductionInputSource(
        batch_id=uuid4(),
        candidate_id=uuid4(),
        source_candidate_id=uuid4(),
        canonical_url=url,
        role=role,
        title="Source",
        publisher="Publisher",
        published_at=date(2026, 7, 10),
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )


def _reference_report(*urls: str) -> ReferenceReport:
    return ReferenceReport(
        sources=tuple(
            ParsedSource(
                local_id=f"S{index}",
                title=f"Source {index}",
                url=url,
                canonical_url=url,
                publisher="Publisher",
                published_at=date(2026, 7, 10),
                role=SourceRole.INDEPENDENT,
            )
            for index, url in enumerate(urls, start=1)
        ),
        events=(),
    )


def test_only_a_primary_discovery_source_earns_a_full_reading() -> None:
    """Every discovered source is in the snapshot, so membership means nothing.

    The snapshot captures the whole discovery output. Testing membership made
    every source FULL, which disabled batching entirely and threw away the
    role discovery had already decided.
    """
    urls = {
        SourceRole.PRIMARY: "https://example.test/primary",
        SourceRole.INDEPENDENT: "https://example.test/independent",
        SourceRole.RELAY: "https://example.test/relay",
        SourceRole.AGGREGATOR: "https://example.test/aggregator",
        SourceRole.SOCIAL: "https://example.test/social",
    }
    supplemental = "https://example.test/supplemental"
    snapshot = _snapshot(tuple(_snapshot_source(url, role) for role, url in urls.items()))

    plans = plan_q2_extraction_profiles(
        _reference_report(*urls.values(), supplemental),
        snapshot=snapshot,
    )
    by_url = {plan.canonical_url: plan for plan in plans}

    assert by_url[urls[SourceRole.PRIMARY]].profile is ExtractionProfile.FULL
    assert by_url[urls[SourceRole.PRIMARY]].reason == "primary_source"
    for role in (
        SourceRole.INDEPENDENT,
        SourceRole.RELAY,
        SourceRole.AGGREGATOR,
        SourceRole.SOCIAL,
    ):
        plan = by_url[urls[role]]
        assert plan.profile is ExtractionProfile.IOC_RULES, role
        assert plan.reason == f"non_primary_source:{role.value}"
    # A source the Q1 report introduced has no editorial role: it stays light.
    assert by_url[supplemental].profile is ExtractionProfile.IOC_RULES
    assert by_url[supplemental].reason == "supporting_source"
    assert sum(plan.profile is ExtractionProfile.FULL for plan in plans) == 1


@pytest.mark.asyncio
async def test_a_mostly_relay_corpus_batches_instead_of_reading_everything_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One primary and eight republishers cost 1 FULL + 2 batches, not 9 FULL."""
    corpus = _Corpus(monkeypatch, 9)
    primary_url = corpus.sources[0].canonical_url
    corpus.snapshot_sources = (
        _snapshot_source(primary_url, SourceRole.PRIMARY),
        *(
            _snapshot_source(source.canonical_url, SourceRole.RELAY)
            for source in corpus.sources[1:]
        ),
    )

    gateway = _CountingGateway()
    result = await _extract(corpus, gateway)

    assert result["status"] == "success", result
    assert result["full_calls"] == 1, "only the primary publication is read in full"
    assert result["light_batches"] == 2, "eight relays fit in two batches of four"
    assert result["model_calls"] == 3
    plan = _plan_event(result)
    assert plan["estimated_calls"] == 3
    assert [entry["batch"] for entry in plan["extracted"] if "batch" in entry] == [0] * 4 + [1] * 4
