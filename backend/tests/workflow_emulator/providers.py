"""Small, versioned inputs for the deterministic workflow emulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from uuid import UUID, uuid4

from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    PlannedDiscoveryMerge,
    ResolvedMergeHandles,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import CandidateTopic, SourceCandidate, SourceRole
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeGroup,
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    MergeConfidence,
    MergeDisposition,
)

DISCOVERY_FIXTURE_VERSION = "workflow-discovery-v1"
FUSION_POLICY_VERSION = "workflow-fusion-apply-v1"
CAPTURE_FIXTURE_VERSION = "workflow-capture-v1"
REFERENCES_FIXTURE_VERSION = "workflow-references-v1"
EXTRACTION_FIXTURE_VERSION = "workflow-extraction-v1"
SYNTHESIS_FIXTURE_VERSION = "workflow-synthesis-v1"
BRIDGE_FIXTURE_VERSION = "workflow-bridge-v1"


@dataclass(frozen=True, slots=True)
class CaptureBundle:
    label: str
    sources: dict[str, dict[str, object]]
    references_response: str
    extraction_responses: dict[str, str]


def capture_bundle(label: str) -> CaptureBundle:
    """Create distinct, grounded source bytes for one production scenario."""
    capture_id = uuid4().hex
    malware_name = f"ExampleMalware-{label}"
    core_url = f"https://cti.example.test/{label.lower()}/{capture_id}/core"
    secondary_url = f"https://cti.example.test/{label.lower()}/{capture_id}/secondary"
    core_body = (
        "<html><body><h1>Campaign report</h1> "
        f"{malware_name} uses core-{capture_id}.security-lab.io. Capture {capture_id}."
        "</body></html>"
    )
    secondary_body = (
        "<html><body><h1>Independent analysis</h1> "
        f"The loader reaches secondary-{capture_id}.security-lab.io. Capture {capture_id}."
        "</body></html>"
    )
    sources = {
        core_url: {"status": 200, "mime": "text/html", "body": core_body},
        secondary_url: {"status": 200, "mime": "text/html", "body": secondary_body},
    }
    references = f"""# REFERENCES

## SOURCE S1

title: {malware_name} core report {label}
url: {core_url}
publisher: CTI Emulator
published-at: 2026-08-10
role: primary
editorial-role: primary
kind: publication
reason: Primary reporting on the campaign

## SOURCE S2

title: {malware_name} secondary analysis {label}
url: {secondary_url}
publisher: CTI Emulator
published-at: 2026-08-11
role: independent
editorial-role: corroboration
kind: publication
reason: Independent corroboration of the campaign

## EVENT R1

date: 2026-08-15
sources: S1, S2
text: The reports describe related {malware_name} infrastructure.
"""
    return CaptureBundle(
        label=label,
        sources=sources,
        references_response=references,
        extraction_responses={
            core_url: (
                "FACT malware\n"
                f"- {malware_name} :: The report identifies the malware family.\n\n"
                "IOC confirmed domain\n"
                f"- core-{capture_id}.security-lab.io :: Command-and-control domain in the report."
            ),
            secondary_url: (
                "IOC contextual domain\n"
                f"- secondary-{capture_id}.security-lab.io :: "
                "Related infrastructure in the analysis."
            ),
        },
    )


def discovery_candidate(label: str, bundle: CaptureBundle) -> CandidateTopic:
    sources = [
        SourceCandidate(
            url=url,
            title=f"ExampleMalware-{label} {label} source {position}",
            publisher="CTI Emulator",
            role=SourceRole.PRIMARY if position == 1 else SourceRole.INDEPENDENT,
            published_at=date(2026, 8, 9 + position),
            tlp=TLP.AMBER,
            sensitivity="public",
            external_llm_allowed=True,
        )
        for position, url in enumerate(bundle.sources, start=1)
    ]
    return CandidateTopic(
        title=f"ExampleMalware-{label} campaign {label}",
        summary=f"Synthetic discovery candidate {label} ({DISCOVERY_FIXTURE_VERSION}).",
        novelty=f"New reporting set {label}.",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("technical depth",),
        actors=(f"Example actor {label}",),
        campaigns=(f"Example campaign {label}",),
        malware=(f"ExampleMalware-{label}",),
        cves=(),
        victims=("industrial organizations",),
        sectors=("industry",),
        countries=("France",),
        likely_artifacts=("domain",),
        sources=sources,
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        actor_or_campaign=f"Example campaign {label}",
        event_date=date(2026, 8, 15),
    )


class DeterministicFusionPlanner:
    """Versioned local planner response; CumulativeDiscoveryService remains real."""

    kind = DiscoveryPlannerKind.HEURISTIC
    policy_version = FUSION_POLICY_VERSION

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge:
        del parent_snapshot, delta, edition_id, external_llm_allowed, sensitivity
        return PlannedDiscoveryMerge(
            DiscoveryMergePlanV1(
                groups=[
                    DiscoveryMergeGroup(
                        existing_subject_handles=[],
                        incoming_candidate_handles=[handle],
                        confidence=MergeConfidence.HIGH,
                        disposition=MergeDisposition.APPLY,
                        rationale=f"Deterministic fixture plan ({FUSION_POLICY_VERSION}).",
                    )
                    for handle in sorted(handles.incoming)
                ]
            )
        )


def fixture_hash(label: str) -> str:
    return sha256(f"{DISCOVERY_FIXTURE_VERSION}:{label}:{uuid4()}".encode()).hexdigest()
