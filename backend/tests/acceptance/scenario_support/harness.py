"""Offline harness for the M24 synthetic 24-step acceptance scenario.

Real layers: the host workspace sidecar on a tmpdir AF_UNIX socket (workspace
*and* H1 session supervisor), the ``HttpWorkspaceAgentClient``, the backend
FastAPI application built by ``create_app`` (ASGITransport), the PostgreSQL
unit of work, the advisor/tool dispatcher chain and the Typst compiler.
Simulated layers: VirusTotal (in-memory port plus a MockTransport download
adapter), Bridge (scripted turn transport), Codex/Claude CLIs (the H1 ``fake``
profile registered under the backend adapter kinds).  No live provider, CLI,
network call or real CTI data is involved.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.agent import AgentConfig, WorkspaceAgent  # noqa: E402
from sidecars.workspace_agent.auth import TOKEN_FILE_NAME  # noqa: E402
from sidecars.workspace_agent.sessions import supervisor as supervisor_module  # noqa: E402
from sidecars.workspace_agent.sessions.supervisor import (  # noqa: E402
    FakeProfile,
    ProfileRegistry,
)

from cti_app.api import main as api_main  # noqa: E402
from cti_app.api.main import create_app  # noqa: E402
from cti_app.application.agent_session_verifier import (  # noqa: E402
    HmacSessionSecretDeriver,
    build_agent_session_message,
)
from cti_app.application.blobs import BlobCatalogService  # noqa: E402
from cti_app.application.bridge_investigator import ScriptedTurnTransport  # noqa: E402
from cti_app.application.identity import StaticTokenIdentityProvider  # noqa: E402
from cti_app.application.investigation_jobs import make_investigation_runner  # noqa: E402
from cti_app.application.jobs import JobService, create_job_registry  # noqa: E402
from cti_app.application.media_assets import MediaAssetStore  # noqa: E402
from cti_app.application.production_artifact_store import ProductionArtifactStore  # noqa: E402
from cti_app.application.publication_rendering import PublicationRenderService  # noqa: E402
from cti_app.application.typst_paths import typst_bundle_paths  # noqa: E402
from cti_app.application.typst_rendering import TypstRenderer  # noqa: E402
from cti_app.config import Settings  # noqa: E402
from cti_app.domain.classification import TLP  # noqa: E402
from cti_app.domain.discovery import SourceRole  # noqa: E402
from cti_app.domain.discovery_cumulative import (  # noqa: E402
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition  # noqa: E402
from cti_app.domain.entities import SourceDocument, Subject  # noqa: E402
from cti_app.domain.production import (  # noqa: E402
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionRun,
)
from cti_app.domain.production_routing import (  # noqa: E402
    ProductionRoutingDecisionV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.production_synthesis import (  # noqa: E402
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
)
from cti_app.domain.publication import (  # noqa: E402
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import serialize_publication_document  # noqa: E402
from cti_app.domain.publication_kind import PublicationKind  # noqa: E402
from cti_app.domain.selection import (  # noqa: E402
    SelectionAction,
    SelectionDecision,
)
from cti_app.domain.semantic_annotation import (  # noqa: E402
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from cti_app.infrastructure.agentic.wiring import build_session_runtime  # noqa: E402
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore  # noqa: E402
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler  # noqa: E402
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient  # noqa: E402
from tests.integration.edition_codes import reserve_edition_code  # noqa: E402
from tests.test_investigation_bootstrap import _object_report  # noqa: E402
from tests.test_investigation_vt_tools import _file_report  # noqa: E402
from tests.test_publication_document_v6 import _base_v5  # noqa: E402
from tests.workflow_emulator.investigation_support import (  # noqa: E402
    HashCheckedVirusTotal,
    SyntheticSampleMaterial,
    composition_factory,
)

HUMAN_TOKEN = "m24-acceptance-human-token-0123456789abcdef"
SESSION_KEY = "0123456789abcdef0123456789abcdef"
TYPST_BINARY = os.environ.get("TYPST_BINARY", "typst")
FAKE_SCRIPT_FILE_NAME = "fake-script.json"
FIXED_NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
ANALYSIS_SECTION_PARAGRAPH = (
    "ANALYSIS-PARAGRAPH: le second etage synthetique est decrit dans ce paragraphe."
)
TAIL_SECTION_PARAGRAPH = "TAIL-PARAGRAPH: portee synthetique finale de l'article."
PLAN_TEXT_TEMPLATE = """## Lead 1 - Verifier le chargeur synthetique
Identifiant: lead-loader
Objectif: confirmer le second etage depuis les octets synthetiques.
Justification CTI: la synthese affirme une chaine en deux etapes.
Claims: S1P1
Preuves de depart: {snapshot_id}
Echantillons: {sample_hash}
Outils: vt.file_report
Preconditions: echantillon en quarantaine
Interet: eleve
Criteres de conclusion: deux gates reseau independants prouves.
Limites: aucun echantillon reel.

## Lead 2 - Verifier l'infrastructure synthetique
Identifiant: lead-infra
Objectif: correler les domaines et adresses synthetiques du fake VT.
Justification CTI: l'article cite deux domaines et deux adresses.
Claims: S1P1
Preuves de depart: {snapshot_id}
Echantillons: {other_hash}
Outils: vt.object_report
Preconditions: inventaire IOC confirme
Interet: normal
Criteres de conclusion: relations de rapport d'objet verifiees.
Limites: pas d'acquisition reseau reelle.
"""


@dataclass(frozen=True)
class ScenarioMaterial:
    """Fully synthetic identifiers for one acceptance run (unique per run)."""

    available_hashes: tuple[str, str, str]
    unavailable_hashes: tuple[str, str]
    domains: tuple[str, str]
    ips: tuple[str, str]
    samples: Mapping[str, bytes]

    @classmethod
    def synthetic(cls) -> ScenarioMaterial:
        token = uuid4().hex
        samples: dict[str, bytes] = {}
        available: list[str] = []
        for index in range(3):
            payload = (
                b"MZ\x00\x00AUTOWORK-SYNTHETIC-ACCEPTANCE-"
                + token.encode("ascii")
                + b"-"
                + bytes([index])
                + b"\x00NO-EXECUTION\x00"
            )
            digest = hashlib.sha256(payload).hexdigest()
            samples[digest] = payload
            available.append(digest)
        unavailable = tuple(
            hashlib.sha256(f"unavailable:{token}:{index}".encode()).hexdigest()
            for index in range(2)
        )
        return cls(
            available_hashes=(available[0], available[1], available[2]),
            unavailable_hashes=(unavailable[0], unavailable[1]),
            domains=(f"acc-{token[:10]}-one.invalid", f"acc-{token[:10]}-two.invalid"),
            ips=("203.0.113.77", "198.51.100.88"),
            samples=samples,
        )

    @property
    def all_hashes(self) -> tuple[str, ...]:
        return (*self.available_hashes, *self.unavailable_hashes)


class AcceptanceVirusTotal(HashCheckedVirusTotal):
    """Fake VT: every hash has a report, three samples are downloadable."""

    def __init__(self, material: ScenarioMaterial) -> None:
        first_hash = material.available_hashes[0]
        first_bytes = material.samples[first_hash]
        super().__init__(
            material=SyntheticSampleMaterial(
                seed_hash=first_hash,
                sample_bytes=first_bytes,
                sample_hash=first_hash,
                ip=material.ips[0],
            ),
            include_candidate=False,
            extra_samples={
                digest: payload
                for digest, payload in material.samples.items()
                if digest != first_hash
            },
        )
        self.object_reports = {
            ("ip_addresses", material.ips[0]): _object_report("ip_addresses", material.ips[0]),
            ("ip_addresses", material.ips[1]): _object_report("ip_addresses", material.ips[1]),
            ("domains", material.domains[0]): _object_report("domains", material.domains[0]),
            ("domains", material.domains[1]): _object_report("domains", material.domains[1]),
        }

    async def file_report(self, file_hash: str) -> Any:
        self.calls["file_report"] += 1
        self._raise("file_report")
        return _file_report(file_hash)


class QueuedBridgeTransport(ScriptedTurnTransport):
    """Scripted Bridge transport whose queue the scenario fills step by step."""

    def enqueue(self, *responses: Any) -> None:
        self._responses.extend(responses)


@dataclass(frozen=True)
class SeededArticle:
    edition: Edition
    subject: Subject
    run: ProductionRun
    synthesis: ProductionArtifact
    publication: ProductionArtifact
    extraction: ProductionArtifact
    relevance: ProductionArtifact
    synthesis_sha256: str
    publication_sha256: str


class _CachedFakeProfile(FakeProfile):  # type: ignore[misc]
    """A stable fake probe: the prepare and the earlier probe share one hash.

    Production profiles cache their probe facts (``CodexProfile._facts_cache``);
    the base ``FakeProfile`` recomputes ``probed_at`` on every call, which would
    make the host prepare reject a backend probe hash taken a second earlier.
    """

    def __init__(
        self,
        *,
        simulate_network_attestation: bool = True,
        script: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            supports_resume=True, simulate_network_attestation=simulate_network_attestation
        )
        self._cached_probe: Any = None
        self._script = dict(script) if script is not None else None

    def probe(self, *, now: str) -> Any:
        if self._cached_probe is None:
            self._cached_probe = super().probe(now=now)
        return self._cached_probe

    def build_launch_plan(self, context: Any) -> Any:
        if self._script is not None:
            # The supervisor prefers the per-session script over the built-in
            # scenario; writing it here keeps the first run deterministic too.
            path = context.session_directory / FAKE_SCRIPT_FILE_NAME
            path.write_text(json.dumps(self._script, separators=(",", ":")), encoding="utf-8")
            os.chmod(path, 0o600)
        return super().build_launch_plan(context)


class _FakeCodexProfile(_CachedFakeProfile):
    """The H1 fake CLI profile registered under the backend adapter kind."""

    adapter_kind = "codex_cli"


class _FakeClaudeProfile(_CachedFakeProfile):
    adapter_kind = "claude_code"


def install_fake_cli_profiles(
    monkeypatch: Any,
    *,
    scenario: str = "hang",
    adapter_kinds: tuple[str, ...] = ("codex_cli", "claude_code"),
    simulate_network_attestation: bool = True,
    script: Mapping[str, Any] | None = None,
) -> None:
    """Register the H1 fake profile for the requested backend adapter kinds.

    ``scenario`` selects the built-in fake CLI script (``hang`` by default);
    ``script`` supplies a deterministic per-session JSON script instead.
    ``simulate_network_attestation=False`` models a host whose probe cannot
    attest the loopback-isolated egress namespace, so the backend must refuse.
    """

    monkeypatch.setenv("AUTOWORK_FAKE_CLI_SCENARIO", scenario)
    profiles: list[type[_CachedFakeProfile]] = []
    if "codex_cli" in adapter_kinds:
        profiles.append(_FakeCodexProfile)
    if "claude_code" in adapter_kinds:
        profiles.append(_FakeClaudeProfile)
    monkeypatch.setattr(
        supervisor_module,
        "default_profiles",
        lambda: ProfileRegistry(
            tuple(
                profile(
                    simulate_network_attestation=simulate_network_attestation,
                    script=script,
                )
                for profile in profiles
            )
        ),
    )


@dataclass
class RunningScenarioAgent:
    """A real workspace agent bound to a short tmpdir runtime directory."""

    agent: WorkspaceAgent
    base_dir: Path
    root: Path
    runtime_dir: Path

    @property
    def socket_path(self) -> Path:
        path: Path = self.runtime_dir / "workspace-agent.sock"
        return path

    @property
    def token_file(self) -> Path:
        path: Path = self.runtime_dir / TOKEN_FILE_NAME
        return path


@contextmanager
def running_scenario_agent() -> Iterator[RunningScenarioAgent]:
    """Start the real sidecar; the AF_UNIX base stays short (<=108 bytes)."""
    # AF_UNIX paths (session IPC sockets) must stay below 108 bytes; an explicit
    # short /tmp base also keeps a long caller TMPDIR out of the socket path.
    base_dir = Path(tempfile.mkdtemp(prefix="m24acc-", dir="/tmp"))
    root = base_dir / "ws"
    root.mkdir()
    runtime_dir = base_dir / "run"
    agent = WorkspaceAgent(
        AgentConfig(
            root=root,
            socket_dir=runtime_dir,
            group=os.getgid(),
            poll_interval_ms=200,
            stability_window_ms=100,
            log_level="WARNING",
        )
    )
    stop = threading.Event()
    thread = threading.Thread(target=agent.serve, args=(stop,), name="m24-acc-agent", daemon=True)
    thread.start()
    try:
        if not agent.wait_ready(15):
            raise AssertionError("the workspace agent did not become ready")
        yield RunningScenarioAgent(
            agent=agent, base_dir=base_dir, root=root, runtime_dir=runtime_dir
        )
    finally:
        stop.set()
        thread.join(timeout=15)
        shutil.rmtree(base_dir, ignore_errors=True)


class DeferredDispatcher:
    """Record dispatched job ids; never executes a worker in-process."""

    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.job_ids.append(job_id)


def acceptance_settings(agent: RunningScenarioAgent) -> Settings:
    return Settings(
        _env_file=None,
        app_env="development",
        api_bind_host="127.0.0.1",
        workspace_agent_socket=str(agent.socket_path),
        workspace_agent_token_file=str(agent.token_file),
        m24_agent_session_key=SESSION_KEY,
        m24_task_lease_seconds=3600,
        virustotal_proxy_url="http://vt-proxy.synthetic.invalid:8080",
        virustotal_base_url="http://www.virustotal.com/api/v3",
        virustotal_file_report_enabled=True,
        virustotal_object_relationships_enabled=False,
        virustotal_intelligence_search_enabled=False,
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts="vtsamples.commondatastorage.googleapis.com",
        virustotal_download_allowed_ports="443",
        investigation_max_vt_read_units=64,
        investigation_max_hits_acquired=5,
        investigation_max_tool_seconds=120,
        openai_bridge_min_submission_interval_seconds=0,
        openai_bridge_submission_jitter_fraction=0,
        openai_bridge_pre_submission_retry_base_seconds=0,
        openai_bridge_pre_submission_retry_max_seconds=0,
    )


def build_artifact_store(uow_factory: Any, blob_root: Path) -> ProductionArtifactStore:
    catalog = BlobCatalogService(FilesystemBlobStore(blob_root), uow_factory)
    return ProductionArtifactStore(catalog)


async def seed_article(
    *,
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    material: ScenarioMaterial,
) -> SeededArticle:
    """Persist one verified synthetic ARTICLE, its extraction and routing."""
    token = uuid4().hex
    source_document_id = uuid4()
    edition = Edition(
        country=f"M24 acceptance {token[:8]}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="M24 synthetic acceptance article",
        slug=f"m24-acceptance-{token[:10]}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        research_date=date(2026, 11, 22),
    )
    base_v5 = _base_v5()
    lead = base_v5.document.lead[0]
    overview_section = PublicationSectionV1(
        kind=PublicationSectionKind.OVERVIEW,
        heading="Vue d'ensemble",
        paragraphs=(PublicationParagraphV1(text=lead.text, evidence_refs=lead.evidence_refs),),
    )
    analysis_section = PublicationSectionV1(
        kind=PublicationSectionKind.TECHNICAL,
        heading="Analyse synthetique",
        paragraphs=(
            PublicationParagraphV1(
                text=ANALYSIS_SECTION_PARAGRAPH,
                evidence_refs=lead.evidence_refs,
            ),
        ),
    )
    tail_section = PublicationSectionV1(
        kind=PublicationSectionKind.TECHNICAL,
        heading="Portee synthetique",
        paragraphs=(
            PublicationParagraphV1(
                text=TAIL_SECTION_PARAGRAPH,
                evidence_refs=lead.evidence_refs,
            ),
        ),
    )
    document = replace(
        base_v5.document,
        subject_id=subject.id,
        sections=(overview_section, analysis_section, tail_section),
    )
    preserved_semantics = tuple(
        paragraph
        for paragraph in base_v5.semantic_text.paragraphs
        if not paragraph.anchor.startswith("section:")
    )
    overview_semantics = SemanticParagraphV1(
        anchor="section:0:paragraph:0001",
        spans=(SemanticTextSpanV1(role=SemanticRole.TEXT, text=lead.text),),
    )
    analysis_semantics = SemanticParagraphV1(
        anchor="section:1:paragraph:0001",
        spans=(SemanticTextSpanV1(role=SemanticRole.TEXT, text=ANALYSIS_SECTION_PARAGRAPH),),
    )
    tail_semantics = SemanticParagraphV1(
        anchor="section:2:paragraph:0001",
        spans=(SemanticTextSpanV1(role=SemanticRole.TEXT, text=TAIL_SECTION_PARAGRAPH),),
    )
    base_v5 = replace(
        base_v5,
        document=document,
        semantic_text=SemanticTextV1(
            schema_version=base_v5.semantic_text.schema_version,
            policy_version=base_v5.semantic_text.policy_version,
            paragraphs=(
                *preserved_semantics,
                overview_semantics,
                analysis_semantics,
                tail_semantics,
            ),
        ),
    )
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_v5)
    )
    publication_blob_id = await artifact_store.put_bytes(
        publication_bytes,
        bucket="m24-acceptance-publication",
        mime_type="application/json",
    )
    publication_sha256 = hashlib.sha256(publication_bytes).hexdigest()
    publication = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=publication_sha256,
        canonical_blob_id=publication_blob_id,
    )
    indicators = [
        {
            "artifact_type": "hash",
            "value": value,
            "indicator_status": "confirmed_ioc",
            "context": "synthetic sample indicator",
            "evidence_quote": f"sha256 {value}",
            "source_document_ids": [str(source_document_id)],
        }
        for value in material.all_hashes
    ]
    indicators.extend(
        {
            "artifact_type": "domain",
            "value": value,
            "indicator_status": "confirmed_ioc",
            "context": "synthetic domain indicator",
            "evidence_quote": f"domain {value}",
            "source_document_ids": [str(source_document_id)],
        }
        for value in material.domains
    )
    indicators.extend(
        {
            "artifact_type": "ip",
            "value": value,
            "indicator_status": "confirmed_ioc",
            "context": "synthetic documentation address",
            "evidence_quote": f"ip {value}",
            "source_document_ids": [str(source_document_id)],
        }
        for value in material.ips
    )
    extraction_payload = {"sources": [{"indicators": indicators}]}
    extraction_blob_id, extraction_sha256 = await artifact_store.put_canonical_json(
        extraction_payload, bucket="m24-acceptance-extraction"
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=extraction_sha256,
        canonical_blob_id=extraction_blob_id,
    )
    relevance_blob_id, relevance_sha256 = await artifact_store.put_canonical_json(
        {"items": []}, bucket="m24-acceptance-relevance"
    )
    relevance = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=relevance_sha256,
        canonical_blob_id=relevance_blob_id,
    )
    discovery_candidate_id = uuid4()
    merge_run = DiscoveryMergeRun(
        edition_id=edition.id,
        parent_snapshot_id=None,
        intake_id=None,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        prompt_version="m24-acceptance-prompt-v1",
        policy_version="m24-acceptance-policy-v1",
        blocking_version="m24-acceptance-blocking-v1",
        merge_input_hash=hashlib.sha256(f"merge-{token}".encode()).hexdigest(),
        handle_map={},
        included_subject_ids=(subject.id,),
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.VALID,
    )
    subject_identity = DiscoverySubjectIdentity(
        edition_id=edition.id,
        origin_key=f"subject:{subject.id}",
        created_by_merge_run_id=merge_run.id,
        id=subject.id,
    )
    discovery_snapshot = DiscoverySnapshot(
        edition_id=edition.id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=merge_run.id,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        subjects=(),
        snapshot_hash=hashlib.sha256(f"snapshot-{token}".encode()).hexdigest(),
        is_active=True,
    )
    selection_decision = SelectionDecision(
        edition_id=edition.id,
        discovery_subject_id=subject.id,
        snapshot_id=discovery_snapshot.id,
        snapshot_version=1,
        action=SelectionAction.SELECT,
        subject_id=subject.id,
        actor_id="operator:static-token",
        correlation_id=f"m24-acceptance-{token[:12]}",
        idempotency_key=f"m24-acceptance-selection-{token}",
    )
    core_source = ProductionInputSource(
        discovery_candidate_id=discovery_candidate_id,
        source_candidate_id=uuid4(),
        canonical_url=f"https://source.invalid/m24-acceptance/{token}",
        role=SourceRole.PRIMARY,
        title="Synthetic acceptance source",
        publisher="AutoWork test fixture",
        published_at=date(2026, 11, 20),
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )
    source_blob_id = await artifact_store.put_bytes(
        b"Synthetic source document for the M24 acceptance scenario.",
        bucket="m24-acceptance-source",
        mime_type="text/html",
    )
    source_document = SourceDocument(
        id=source_document_id,
        subject_id=subject.id,
        blob_id=source_blob_id,
        original_name="synthetic-source.html",
        origin="m24 acceptance fixture",
        acquired_at=FIXED_NOW,
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        final_url=core_source.canonical_url,
        detected_mime_type="text/html",
    )
    input_snapshot = ProductionInputSnapshot(
        production_run_id=run.id,
        edition_id=edition.id,
        subject_id=subject.id,
        subject_version=1,
        subject_title=subject.title,
        subject_tlp=TLP.CLEAR,
        selection_decision_id=selection_decision.id,
        origin_discovery_subject_id=subject.id,
        canonical_discovery_subject_id=subject.id,
        discovery_snapshot_id=discovery_snapshot.id,
        discovery_snapshot_version=1,
        member_candidate_ids=(discovery_candidate_id,),
        discovery_summary="Synthetic acceptance discovery snapshot",
        actor_or_campaign="Synthetic acceptance actor",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        publication_language="en",
        research_date=date(2026, 11, 22),
        core_sources=(core_source,),
    )
    synthesis_payload = {
        "schema_version": PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        "subject_id": str(subject.id),
        "production_input_hash": input_snapshot.input_hash,
        "extraction_hash": extraction_sha256,
        "publication_language": document.publication_language,
        "synthesis_policy_version": SYNTHESIS_POLICY_VERSION,
        "title": document.title,
        "lead": [
            {
                "text": (
                    "Synthetic synthesis for the M24 acceptance scenario: five hashes, "
                    "two domains and two documentation IP addresses are cited."
                ),
                "evidence_refs": [],
            }
        ],
        "sections": [],
        "timeline": [],
        "source_notes": [],
        "uncertainties": [
            {
                "text": "Synthetic fixture only; no live provider was consulted.",
                "source_document_ids": [str(source_document_id)],
            }
        ],
        "warnings": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="m24-acceptance-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    routing_decision = ProductionRoutingDecisionV1(
        decision_id=uuid4(),
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        extraction_artifact_id=extraction.id,
        extraction_artifact_hash=extraction_sha256,
        relevance_artifact_id=relevance.id,
        relevance_artifact_hash=relevance_sha256,
        input_hash=hashlib.sha256(f"routing-{token}".encode()).hexdigest(),
        policy_version="routing-policy-v1",
        status=RoutingStatus.DECIDED,
        publication_kind=PublicationKind.ARTICLE,
        reason_code=RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        confirmed_ioc_count=len(indicators),
        contextual_ioc_count=0,
        ambiguous_ioc_count=0,
        critical_source_diagnostics=(),
        decided_at=FIXED_NOW,
        decision_hash=hashlib.sha256(f"decision-{token}".encode()).hexdigest(),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        for artifact in (synthesis, extraction, relevance, publication):
            await uow.production_artifacts.append(artifact)
        await uow.discovery_merge_runs.add_if_absent(merge_run)
        await uow.discovery_snapshots.append(discovery_snapshot)
        await uow.discovery_subject_identities.add_many_if_absent([subject_identity])
        await uow.selection_decisions.append(selection_decision)
        await uow.source_documents.add(source_document)
        await uow.production_input_snapshots.add(input_snapshot)
        await uow.production_routing_decisions.append_and_adopt(
            routing_decision, expected_version=run.version
        )
        await uow.commit()
    return SeededArticle(
        edition=edition,
        subject=subject,
        run=run,
        synthesis=synthesis,
        publication=publication,
        extraction=extraction,
        relevance=relevance,
        synthesis_sha256=synthesis_sha256,
        publication_sha256=publication_sha256,
    )


@dataclass
class ScenarioApplication:
    """The real application plus the injected test ports it is bound to."""

    app: FastAPI
    settings: Settings
    human: StaticTokenIdentityProvider
    material: ScenarioMaterial
    artifact_store: ProductionArtifactStore
    workspace_port: HttpWorkspaceAgentClient
    session_runtime: Any
    composition: Any
    bridge: QueuedBridgeTransport
    virus_total: AcceptanceVirusTotal
    dispatcher: DeferredDispatcher
    render_service: PublicationRenderService
    blob_root: Path

    @property
    def human_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {HUMAN_TOKEN}"}

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            yield client

    async def aclose(self) -> None:
        await self.workspace_port.aclose()
        await self.session_runtime.aclose()


async def build_scenario_application(
    *,
    monkeypatch: Any,
    uow_factory: Any,
    agent: RunningScenarioAgent,
    material: ScenarioMaterial,
    blob_root: Path,
) -> ScenarioApplication:
    settings = acceptance_settings(agent)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    artifact_store = build_artifact_store(uow_factory, blob_root)
    bridge = QueuedBridgeTransport([])
    virus_total = AcceptanceVirusTotal(material)
    composition = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=bridge,
        virustotal=virus_total,
        external_effect_session_factory=uow_factory.session_factory,
    )
    app = create_app()
    app.state.identity_provider = StaticTokenIdentityProvider(HUMAN_TOKEN)
    app.state.settings = settings
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = artifact_store
    app.state.investigation_composition_factory = composition
    workspace_port = HttpWorkspaceAgentClient(
        socket_path=str(agent.socket_path),
        token_file=str(agent.token_file),
        timeout_seconds=60.0,
    )
    app.state.workspace_agent_port = workspace_port
    session_runtime = build_session_runtime(app.state, settings)
    if not session_runtime.available:
        raise AssertionError(
            f"the real host session runtime is unavailable: {session_runtime.unavailable_reason}"
        )
    app.state.agentic_operator_port = session_runtime.operator
    app.state.agent_session_secret_delivery = session_runtime.secret_delivery
    app.state.agent_session_runtime = session_runtime
    runner = make_investigation_runner(uow_factory, composition)
    registry = create_job_registry(investigation_runner=runner)
    dispatcher = DeferredDispatcher()
    app.state.job_service = JobService(uow_factory, registry)
    app.state.job_dispatcher = dispatcher
    render_service = build_publication_render_service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    app.state.publication_render_service = render_service
    return ScenarioApplication(
        app=app,
        settings=settings,
        human=app.state.identity_provider,
        material=material,
        artifact_store=artifact_store,
        workspace_port=workspace_port,
        session_runtime=session_runtime,
        composition=composition,
        bridge=bridge,
        virus_total=virus_total,
        dispatcher=dispatcher,
        render_service=render_service,
        blob_root=blob_root,
    )


def build_publication_render_service(
    *, uow_factory: Any, artifact_store: ProductionArtifactStore
) -> PublicationRenderService:
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    return PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(binary=TYPST_BINARY),
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
    )


def plan_text(material: ScenarioMaterial, *, snapshot_id: UUID) -> str:
    return PLAN_TEXT_TEMPLATE.format(
        snapshot_id=snapshot_id,
        sample_hash=material.available_hashes[0],
        other_hash=material.available_hashes[1],
    )


def note_draft_response(
    *,
    block_type: str,
    anchor: str,
    text: str,
    bridge_invocation_id: UUID,
) -> dict[str, Any]:
    """A strict ``advisor-response.v3`` note draft accepted by the real parser."""
    payload = {
        "schema": "advisor-response.v3",
        "consultation_type": "draft_text",
        "status": "succeeded",
        "language": "fr",
        "blocks": [
            {
                "unit_id": "U1",
                "block_type": block_type,
                "form": "analyst_note",
                "anchor": anchor,
                "sentences": [{"text": text}],
            }
        ],
        "untrusted": True,
        "limitations": ["Reponse Bridge synthetique pour le scenario d'acceptation."],
    }
    return {
        "output_text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "bridge_invocation_id": str(bridge_invocation_id),
    }


def advisory_consult_text() -> str:
    return (
        "advisor-wire.v1\n"
        "TYPE: pivot_strategy\n"
        "STATUS: succeeded\n"
        "SUMMARY: Synthetic advisory review of the persisted observation.\n"
        "LEAD: Compare the synthetic file report with a second independent observation. "
        "| reason=synthetic | refs=E1\n"
        "LIMITATION: Synthetic advisory only; no live provider was contacted.\n"
    )


def bridge_submission(output_text: str, *, bridge_invocation_id: UUID | None = None) -> Any:
    from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus

    return TurnSubmission(
        TurnSubmissionStatus.SUCCEEDED,
        output_text=output_text,
        bridge_invocation_id=str(bridge_invocation_id) if bridge_invocation_id else None,
    )


async def session_secret(uow_factory: Any, session_id: UUID) -> bytes:
    async with uow_factory() as uow:
        row = await uow.agent_sessions.get(session_id)
    if row is None:
        raise AssertionError(f"agent session {session_id} does not exist")
    return HmacSessionSecretDeriver(SESSION_KEY).derive(
        session_id=session_id, nonce=row.auth_secret_nonce
    )


async def signed_session_request(
    client: httpx.AsyncClient,
    *,
    path: str,
    session_id: UUID,
    secret: bytes,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """Play the host shim: one D7 signed session call, no bearer token."""
    body = json.dumps(
        dict(payload) if payload is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(datetime.now(UTC).timestamp()))
    message = build_agent_session_message(
        method="POST",
        path_and_query=path,
        session_id=session_id,
        body=body,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
    )
    signature = "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-AutoWork-Agent-Session": str(session_id),
        "X-AutoWork-Agent-Timestamp": timestamp,
        "X-AutoWork-Agent-Signature": signature,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return await client.post(path, content=body, headers=headers)


async def signed_tool_call(
    client: httpx.AsyncClient,
    *,
    investigation_id: UUID,
    session_id: UUID,
    secret: bytes,
    tool_name: str,
    schema_version: str,
    arguments: Mapping[str, Any],
    idempotency_key: str,
) -> httpx.Response:
    """Play the host shim: the D7 signed broker call, no bearer token."""
    path = f"/api/investigations/{investigation_id}/agent-sessions/{session_id}/tool-calls"
    return await signed_session_request(
        client,
        path=path,
        session_id=session_id,
        secret=secret,
        payload={
            "tool_name": tool_name,
            "schema_version": schema_version,
            "arguments": dict(arguments),
        },
        idempotency_key=idempotency_key,
    )


async def settle_and_sync(client: httpx.AsyncClient, investigation_id: UUID) -> dict[str, Any]:
    """Give the real polling watcher its stability window, then ingest twice.

    The three reports are merged: an earlier round may ingest the event while
    the last round only confirms the revision.
    """
    import asyncio

    reports: list[dict[str, Any]] = []
    for _ in range(3):
        await asyncio.sleep(0.2)
        response = await client.post(
            f"/api/investigations/{investigation_id}/workspace/sync",
            headers={"Authorization": f"Bearer {HUMAN_TOKEN}"},
        )
        if response.status_code != 200:
            raise AssertionError(f"workspace sync failed: {response.status_code} {response.text}")
        reports.append(response.json())
    merged = dict(reports[-1])
    for key in ("events_received", "events_inserted", "revisions_inserted"):
        merged[key] = sum(int(report.get(key, 0)) for report in reports)
    for key in ("new_human_notes", "changed_human_notes"):
        merged[key] = sorted({note for report in reports for note in report.get(key, ())})
    merged["rounds"] = len(reports)
    return merged


__all__ = [
    "HUMAN_TOKEN",
    "SESSION_KEY",
    "AcceptanceVirusTotal",
    "DeferredDispatcher",
    "QueuedBridgeTransport",
    "RunningScenarioAgent",
    "ScenarioApplication",
    "ScenarioMaterial",
    "SeededArticle",
    "acceptance_settings",
    "advisory_consult_text",
    "bridge_submission",
    "build_artifact_store",
    "build_publication_render_service",
    "build_scenario_application",
    "install_fake_cli_profiles",
    "note_draft_response",
    "plan_text",
    "running_scenario_agent",
    "seed_article",
    "session_secret",
    "settle_and_sync",
    "signed_session_request",
    "signed_tool_call",
]
