"""Build bounded, self-contained Bridge contexts from canonical investigation data."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_CONFIRMED_IOC_BYTES,
    MAX_CONTEXT_CONFIRMED_IOCS,
    ArticleClaimV1,
    ArticleContextV1,
    ConfirmedIOCContextV1,
    ContextInvestigationV1,
    EvidenceSummaryV1,
    FindingSummaryV1,
    IndicatorInventoryV1,
    InvestigationContextV1,
    InvestigationSourceV1,
    InvestigationUserRequestV1,
    OpinionNoteSummaryV1,
    PublicationAnchorV1,
    ToolCoverageEntryV1,
    ToolSummaryV1,
)
from cti_app.application.investigation_persistence import (
    AnalystOpinionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    SampleAssociationRepository,
    ToolInvocationRepository,
)
from cti_app.application.investigation_pivots import available_investigation_pivots
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1, investigation_opinion_id
from cti_app.domain.investigation import (
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    InvestigationFinding,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    thaw_json,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    InvestigationWorkingState,
)
from cti_app.domain.rule_workbench import RuleTestCorpusRole, RuleValidationStatus

MAX_ARTICLE_NARRATIVE_CHARS = 8_000
MAX_CONTEXT_HYPOTHESES = 16
MAX_CONTEXT_EVIDENCE = 16
MAX_CONTEXT_EVIDENCE_CHARS = 600
MAX_CONTEXT_EVIDENCE_INDEX = 200
MAX_CONTEXT_EVIDENCE_INDEX_BYTES = 24 * 1024
MAX_CONTEXT_TOOL_COVERAGE = 200
MAX_CONTEXT_TOOL_COVERAGE_BYTES = 48 * 1024
MAX_CONTEXT_OPINION_NOTES = 24
MAX_CONTEXT_OPINION_NOTE_BYTES = 12 * 1024
PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE = "provider_authorization_refusal"
PROVIDER_AUTHORIZATION_REFUSAL_THRESHOLD = 2
PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE = "provider_download_route_refused"
PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE = "provider_download_authorization_refused"
PROVIDER_DOWNLOAD_REFUSAL_THRESHOLD = 2
MAX_CONTEXT_FINDINGS = 200
MAX_CONTEXT_FINDING_BYTES = 48 * 1024
MAX_CONTEXT_FINDING_STATEMENT_CHARS = 420
MAX_CONTEXT_PUBLICATION_ANCHORS = 128
MAX_CONTEXT_PUBLICATION_ANCHOR_BYTES = 20 * 1024
MAX_CONTEXT_TOOLS = 64
MAX_CONTEXT_RULE_CORPUS_IMPORTS = 64
MAX_CONTEXT_RULE_CANDIDATES = 16
INVESTIGATION_MAX_CONTEXT_BYTES = 256 * 1024
USER_REQUEST_ACTION_PREFIX = "user-request:"
MAX_OPERATOR_REQUESTS = 8
MAX_OPERATOR_REQUEST_BYTES = 32_768
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|private[_ -]?key|access[_ -]?token|client[_ -]?secret|"
    r"api[_ -]?token|authorization|credential|password|secret|signature|sig)\s*[:=]\s*[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SIGNED_URL = re.compile(r"https?://[^\s\]\[()<>\"]+", re.I)
_SIGNED_QUERY = re.compile(r"[?&](?:x-amz-[^=]+|x-goog-[^=]+|signature|sig|access_token)=", re.I)


@dataclass(frozen=True, slots=True)
class ArticleSnapshot:
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    article: ArticleContextV1
    base_publication_artifact_id: UUID | None = None
    base_publication_sha256: str | None = None
    tlp: str | None = None
    do_not_submit: bool = True
    external_llm_allowed: bool = False
    narrative_hashes: tuple[str, ...] = ()
    confirmed_iocs: tuple[ConfirmedIOCContextV1, ...] = ()
    confirmed_iocs_truncated: bool = False

    def __post_init__(self) -> None:
        if (self.base_publication_artifact_id is None) != (self.base_publication_sha256 is None):
            raise ValueError("Base publication identity and SHA-256 must be supplied together")
        for value in (self.synthesis_sha256, self.base_publication_sha256):
            if value is not None and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError("Article snapshot hashes must be lowercase SHA-256")

    @property
    def source(self) -> InvestigationSourceV1:
        return InvestigationSourceV1(
            subject_id=self.subject_id,
            production_run_id=self.production_run_id,
            synthesis_artifact_id=self.synthesis_artifact_id,
            synthesis_sha256=self.synthesis_sha256,
        )


class ArticleSnapshotPort(Protocol):
    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot: ...

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[ArticleClaimV1, ...]: ...


class EvidenceSummaryPort(Protocol):
    async def summary(self, observation: EvidenceObservation) -> Mapping[str, Any] | str | None: ...


class RuleCorpusContextInventoryPort(Protocol):
    async def manual_rule_corpus_imports(self, investigation_id: UUID) -> Sequence[Any]: ...

    async def rule_candidate_versions(
        self, investigation_id: UUID
    ) -> Sequence[DetectionCandidateVersion]: ...


class CanonicalEvidenceSummaryPort:
    """Read bounded, structured facts from canonical evidence blobs."""

    def __init__(self, artifact_store: Any) -> None:
        self._artifact_store = artifact_store

    async def summary(self, observation: EvidenceObservation) -> Mapping[str, Any] | None:
        reader = getattr(self._artifact_store, "read_bytes", None)
        if not callable(reader):
            return None
        try:
            raw = await reader(observation.blob_id, max_bytes=128 * 1024)
            if hashlib.sha256(raw).hexdigest() != observation.sha256:
                return None
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, Mapping):
            return None
        schema = payload.get("schema")
        if schema == "investigation-tool-observation.v1":
            result = payload.get("result")
            return result if isinstance(result, Mapping) else None
        if schema == "virustotal-investigation-summary.v1":
            return payload
        if schema == "shodan-observation.v1":
            summary = payload.get("summary")
            return summary if isinstance(summary, Mapping) else None
        if schema == "investigation-yara-test.v1":
            return {
                key: payload[key]
                for key in (
                    "source_sha256",
                    "validation_status",
                    "execution_status",
                    "coverage_statement",
                    "provenance_counts_by_role",
                    "false_positive_count",
                    "false_negative_count",
                )
                if key in payload
            }
        return None


class InvestigationContextTooLarge(ValueError):
    code = "investigation_context_too_large"

    def __init__(self, actual_bytes: int, max_bytes: int = INVESTIGATION_MAX_CONTEXT_BYTES) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(f"{self.code}: {actual_bytes} bytes exceeds {max_bytes} bytes")


def ensure_investigation_context_size(context_bytes: bytes) -> None:
    if len(context_bytes) > INVESTIGATION_MAX_CONTEXT_BYTES:
        raise InvestigationContextTooLarge(len(context_bytes), INVESTIGATION_MAX_CONTEXT_BYTES)


class InvestigationReadPort(Protocol):
    async def get_evidence(self, observation_id: UUID) -> Mapping[str, Any] | None: ...

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...]
    ) -> Sequence[Mapping[str, Any]]: ...

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None: ...

    async def graph_neighbors(
        self, object_type: str, object_id: str
    ) -> Sequence[Mapping[str, Any]]: ...


class SampleFeaturePackPort(Protocol):
    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None: ...


class InvestigationGraphReadPort(Protocol):
    async def neighbors(self, object_type: str, object_id: str) -> Sequence[Mapping[str, Any]]: ...


class RepositoryInvestigationGraphReadPort:
    """Build read-only graph edges from persisted findings and sample associations."""

    def __init__(
        self,
        *,
        investigation_id: UUID,
        findings: FindingRepository,
        associations: SampleAssociationRepository,
    ) -> None:
        self._investigation_id = investigation_id
        self._findings = findings
        self._associations = associations

    async def neighbors(self, object_type: str, object_id: str) -> Sequence[Mapping[str, Any]]:
        edges: list[Mapping[str, Any]] = []
        if object_type in {"sample", "hash"}:
            for association in await self._associations.list_for_investigation(
                self._investigation_id
            ):
                if association.sample_sha256 == object_id:
                    edges.extend(
                        {
                            "source_type": "sample",
                            "source_id": association.sample_sha256,
                            "relationship": "related_sample",
                            "target_type": "sample",
                            "target_id": related,
                            "qualification": association.qualification.value,
                        }
                        for related in association.related_sample_sha256
                    )
                elif object_id in association.related_sample_sha256:
                    edges.append(
                        {
                            "source_type": "sample",
                            "source_id": association.sample_sha256,
                            "relationship": "related_sample",
                            "target_type": "sample",
                            "target_id": object_id,
                            "qualification": association.qualification.value,
                        }
                    )

        findings = await self._findings.list_for_investigation(self._investigation_id)
        for finding in findings:
            if object_type in {"finding", "investigation_finding"} and str(finding.id) == object_id:
                edges.extend(self._finding_edges(finding))
            elif object_type in {"evidence", "observation"} and any(
                str(ref) == object_id for ref in finding.observation_refs
            ):
                edges.append(self._finding_edge(finding, "evidence", object_id))
            elif object_type == "claim" and object_id in finding.claim_refs:
                edges.append(self._finding_edge(finding, "claim", object_id))
        return tuple(edges[:64])

    @staticmethod
    def _finding_edges(finding: InvestigationFinding) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            RepositoryInvestigationGraphReadPort._finding_edge(finding, node_type, node_id)
            for node_type, node_ids in (
                ("evidence", tuple(str(item) for item in finding.observation_refs)),
                ("claim", finding.claim_refs),
            )
            for node_id in node_ids
        )

    @staticmethod
    def _finding_edge(
        finding: InvestigationFinding, target_type: str, target_id: str
    ) -> Mapping[str, Any]:
        return {
            "source_type": "finding",
            "source_id": str(finding.id),
            "relationship": f"{finding.assessment.value}_references",
            "target_type": target_type,
            "target_id": target_id,
            "finding_kind": finding.kind.value,
            "summary": finding.statement,
        }


class RepositoryInvestigationReadPort:
    """Join persisted observations/claims with persisted feature and edge readers."""

    def __init__(
        self,
        *,
        investigation_id: UUID,
        observations: EvidenceObservationRepository,
        article: ArticleSnapshotPort,
        feature_packs: SampleFeaturePackPort,
        graph: InvestigationGraphReadPort,
        evidence_summary: EvidenceSummaryPort | None = None,
    ) -> None:
        self._investigation_id = investigation_id
        self._observations = observations
        self._article = article
        self._feature_packs = feature_packs
        self._graph = graph
        self._evidence_summary = evidence_summary

    async def get_evidence(self, observation_id: UUID) -> Mapping[str, Any] | None:
        observation = await self._observations.get(observation_id)
        if observation is None or observation.investigation_id != self._investigation_id:
            return None
        summary = (
            await self._evidence_summary.summary(observation) if self._evidence_summary else None
        )
        payload: dict[str, Any] = {
            "evidence_id": str(observation.id),
            "kind": observation.kind.value,
            "sha256": observation.sha256,
            "source": observation.source,
            "source_version": observation.source_version,
            "object_type": observation.object_type,
            "object_id": observation.object_id,
            "limitations": list(observation.limitations),
        }
        if summary is not None:
            payload["summary"] = summary
        return payload

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...]
    ) -> Sequence[Mapping[str, Any]]:
        if investigation_id != self._investigation_id:
            return ()
        return tuple(
            claim.model_dump(mode="json")
            for claim in await self._article.get_claims(self._investigation_id, claim_ids)
        )

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None:
        return await self._feature_packs.get_feature_pack(sample_sha256)

    async def graph_neighbors(
        self, object_type: str, object_id: str
    ) -> Sequence[Mapping[str, Any]]:
        return await self._graph.neighbors(object_type, object_id)


class ResearchContextBuilder:
    def __init__(
        self,
        *,
        article_snapshots: ArticleSnapshotPort,
        observations: EvidenceObservationRepository,
        findings: FindingRepository,
        registry: ToolRegistry | None = None,
        evidence_summaries: EvidenceSummaryPort | None = None,
        invocations: ToolInvocationRepository | None = None,
        opinions: AnalystOpinionRepository | None = None,
        rule_corpus_inventory: RuleCorpusContextInventoryPort | None = None,
    ) -> None:
        self.article_snapshots = article_snapshots
        self._observations = observations
        self._findings = findings
        self._registry = registry
        self._evidence_summaries = evidence_summaries
        self._invocations = invocations
        self._opinions = opinions
        self._rule_corpus_inventory = rule_corpus_inventory

    async def build(
        self,
        *,
        investigation_id: UUID,
        synthesis_artifact_id: UUID,
        state: InvestigationWorkingState,
        tool_context: ToolExecutionContext,
        budget_remaining: Mapping[str, int | None],
        registry: ToolRegistry | None = None,
        blocked_capabilities: tuple[str, ...] = (),
    ) -> InvestigationContextV1:
        if (
            state.investigation_id != investigation_id
            or tool_context.investigation_id != investigation_id
        ):
            raise ValueError("Research context identity does not match its investigation")
        snapshot = await self.article_snapshots.resolve(synthesis_artifact_id)
        observations = tuple(
            sorted(
                await self._observations.list_for_investigation(investigation_id),
                key=lambda item: (item.observed_at, str(item.id)),
            )
        )
        findings = tuple(
            sorted(
                await self._findings.list_for_investigation(investigation_id),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )
        all_observation_ids = {item.id for item in observations}
        all_finding_ids = {item.id for item in findings}
        # Every Bridge turn is an independent Temporary Chat. Include the newest
        # bounded evidence on every turn so the prompt remains self-contained.
        evidence_summary = await self._evidence_context(tuple(reversed(observations)))
        tool_invocations = (
            tuple(await self._invocations.list_for_investigation(investigation_id))
            if self._invocations is not None
            else ()
        )
        tool_coverage, tool_coverage_truncated = await self._tool_coverage(
            tool_invocations, observations
        )
        derived_blocked = await provider_authorization_blocked_capabilities(
            investigation_id=investigation_id,
            invocations=tool_invocations,
            observations=observations,
        )
        derived_blocked = tuple(
            sorted(
                set(derived_blocked).union(
                    provider_download_blocked_capabilities(
                        investigation_id=investigation_id,
                        invocations=tool_invocations,
                        observations=observations,
                    )
                )
            )
        )
        all_blocked = tuple(sorted(set(blocked_capabilities).union(derived_blocked)))
        blocked_providers = {
            item.split(":", 1)[0]
            for item in all_blocked
            if ": provider authorization refused" in item
        }
        blocked_capability_keys = {
            item.split(":", 1)[0] for item in all_blocked if item.startswith("vt.file_download:")
        }
        finding_summary, findings_truncated = self._finding_context(
            findings,
            observation_ids=all_observation_ids,
        )
        evidence_index, evidence_index_truncated = self._evidence_index(observations)
        current_opinion = (
            await self._opinions.get(investigation_opinion_id(investigation_id))
            if self._opinions is not None
            else None
        )
        opinion_notes, opinion_notes_truncated = self._opinion_note_index(
            current_opinion,
            finding_ids=all_finding_ids,
            observation_ids=all_observation_ids,
        )
        rule_corpus_deltas = await self._rule_corpus_inventory_context(
            investigation_id, observations
        )
        tools = tuple(
            ToolSummaryV1(
                name=item.name,
                schema_version=item.schema_version,
                effect=item.effect,
                arguments_schema=thaw_json(item.input_schema),
            )
            for item in sorted(
                (
                    definition
                    for definition in self._available_tools(tool_context, registry)
                    if definition.capability_key.split(".", 1)[0] not in blocked_providers
                    and definition.capability_key not in blocked_capability_keys
                ),
                key=lambda definition: (definition.name, definition.schema_version),
            )[:MAX_CONTEXT_TOOLS]
        )
        hypothesis_payload = tuple(
            self._hypothesis_payload(
                item,
                observation_ids=all_observation_ids,
                finding_ids=all_finding_ids,
            )
            for item in state.hypotheses[:MAX_CONTEXT_HYPOTHESES]
        )
        indicator_inventory = self._indicator_inventory(snapshot)
        available_pivots, available_pivots_truncated = available_investigation_pivots(
            inventory=indicator_inventory.items,
            tools=tools,
            tool_coverage=tool_coverage,
            invocations=tool_invocations,
            observations=observations,
        )
        user_requests = self._user_requests_from_actions(state.recent_actions)
        previous_actions = tuple(
            {"summary": _truncate(_safe_untrusted(action), 300)}
            for action in state.recent_actions
            if not action.startswith(USER_REQUEST_ACTION_PREFIX)
        )
        investigation_context = ContextInvestigationV1(
            agenda=tuple(
                _truncate(_safe_untrusted(item.question), 500) for item in state.agenda.items
            ),
            hypotheses=hypothesis_payload,
            new_evidence=evidence_summary,
            evidence_index=evidence_index,
            evidence_index_truncated=evidence_index_truncated,
            findings=finding_summary,
            findings_truncated=findings_truncated,
            opinion_notes=opinion_notes,
            opinion_notes_truncated=opinion_notes_truncated,
            sample_deltas=(),
            rule_deltas=rule_corpus_deltas,
            previous_actions=previous_actions[-8:],
            user_requests=(),
            tool_coverage=tool_coverage,
            tool_coverage_truncated=tool_coverage_truncated,
            blocked_capabilities=tuple(item[:128] for item in all_blocked[:64]),
            budget_remaining=_safe_budget_names(budget_remaining),
        )
        context = InvestigationContextV1(
            view="full",
            investigation_id=investigation_id,
            source=snapshot.source,
            article=self._full_article(snapshot),
            investigation=investigation_context,
            operator_requests=user_requests,
            indicator_inventory=indicator_inventory,
            available_pivots=available_pivots,
            available_pivots_truncated=available_pivots_truncated,
            available_tools=tools,
        )
        ensure_investigation_context_size(context.canonical_bytes())
        return context

    async def _rule_corpus_inventory_context(
        self,
        investigation_id: UUID,
        observations: Sequence[EvidenceObservation],
    ) -> tuple[dict[str, Any], ...]:
        provider = self._rule_corpus_inventory
        if provider is None:
            return ()
        rows: list[dict[str, Any]] = []
        try:
            items = tuple(await provider.manual_rule_corpus_imports(investigation_id))
        except Exception:
            rows.append(
                {
                    "kind": "operator_rule_corpus_inventory",
                    "status": "unavailable",
                    "summary": "Operator-imported rule corpus inventory is unavailable.",
                }
            )
            items = ()
        rows.extend(
            {
                "kind": "operator_rule_corpus_sample",
                "sha256": item.sample_sha256,
                "role": item.role.value,
                "provenance": item.provenance.value,
                "size_bytes": item.size_bytes,
                "source_description": _safe_untrusted(
                    _truncate(_sanitize(item.source_description), 512)
                ),
            }
            for item in sorted(items, key=lambda item: item.sample_sha256)[
                :MAX_CONTEXT_RULE_CORPUS_IMPORTS
            ]
        )
        if len(items) > MAX_CONTEXT_RULE_CORPUS_IMPORTS:
            rows.append(
                {
                    "kind": "operator_rule_corpus_inventory_truncated",
                    "omitted_count": len(items) - MAX_CONTEXT_RULE_CORPUS_IMPORTS,
                }
            )

        candidate_reader = getattr(provider, "rule_candidate_versions", None)
        if callable(candidate_reader):
            try:
                versions = tuple(await candidate_reader(investigation_id))
            except Exception:
                rows.append(
                    {
                        "kind": "rule_candidate_inventory",
                        "status": "unavailable",
                        "summary": "Rule candidate inventory is unavailable.",
                    }
                )
            else:
                latest_by_candidate: dict[UUID, DetectionCandidateVersion] = {}
                for version in versions:
                    if version.investigation_id != investigation_id:
                        continue
                    previous = latest_by_candidate.get(version.candidate_id)
                    if previous is None or (version.version, version.id.hex) > (
                        previous.version,
                        previous.id.hex,
                    ):
                        latest_by_candidate[version.candidate_id] = version
                ordered = sorted(
                    latest_by_candidate.values(),
                    key=lambda item: (item.created_at, item.candidate_id.hex),
                    reverse=True,
                )
                for version in ordered[:MAX_CONTEXT_RULE_CANDIDATES]:
                    latest_report = await self._latest_rule_test_report(version, observations)
                    rows.append(
                        {
                            "kind": "rule_candidate",
                            "candidate_id": str(version.candidate_id),
                            "version": version.version,
                            "version_id": str(version.id),
                            "status": version.status.value,
                            "validation_status": (
                                latest_report["validation_status"]
                                if latest_report is not None
                                else RuleValidationStatus.UNTESTED.value
                            ),
                            "latest_test_report": latest_report,
                            "source_sha256": version.source_sha256,
                            "source_excerpt": _safe_untrusted_excerpt(version.source, 2_000),
                        }
                    )
                if len(ordered) > MAX_CONTEXT_RULE_CANDIDATES:
                    rows.append(
                        {
                            "kind": "rule_candidate_inventory_truncated",
                            "omitted_count": len(ordered) - MAX_CONTEXT_RULE_CANDIDATES,
                        }
                    )
        return tuple(rows)

    async def _latest_rule_test_report(
        self,
        version: DetectionCandidateVersion,
        observations: Sequence[EvidenceObservation],
    ) -> dict[str, Any] | None:
        summaries = self._evidence_summaries
        if summaries is None:
            return None
        matching = sorted(
            (
                observation
                for observation in observations
                if observation.kind is EvidenceKind.YARA_MATCH
                and observation.object_type == "detection_candidate_version"
                and observation.object_id == str(version.id)
            ),
            key=lambda item: (item.observed_at, item.id.hex),
        )
        latest: dict[str, Any] | None = None
        for observation in matching:
            try:
                summary = await summaries.summary(observation)
            except Exception:
                continue
            if (
                not isinstance(summary, Mapping)
                or summary.get("source_sha256") != version.source_sha256
            ):
                continue
            try:
                validation_status = RuleValidationStatus(summary["validation_status"]).value
            except (KeyError, TypeError, ValueError):
                validation_status = RuleValidationStatus.UNTESTED.value
            provenance_counts = summary.get("provenance_counts_by_role")
            normalized_counts: dict[str, dict[str, int]] = {}
            if isinstance(provenance_counts, Mapping):
                for role in RuleTestCorpusRole:
                    counts = provenance_counts.get(role.value)
                    if not isinstance(counts, Mapping):
                        continue
                    normalized = {
                        provenance: value
                        for provenance in ("real", "synthetic")
                        if isinstance((value := counts.get(provenance)), int)
                        and not isinstance(value, bool)
                        and value >= 0
                    }
                    if normalized:
                        normalized_counts[role.value] = normalized
            execution_status = summary.get("execution_status")
            latest = {
                "observation_id": str(observation.id),
                "validation_status": validation_status,
                "execution_status": (
                    str(execution_status)[:40] if isinstance(execution_status, str) else None
                ),
                "provenance_counts_by_role": normalized_counts,
                "false_positive_count": _nonnegative_int(summary.get("false_positive_count")),
                "false_negative_count": _nonnegative_int(summary.get("false_negative_count")),
            }
        return latest

    @staticmethod
    def _user_requests_from_actions(
        actions: Sequence[str],
    ) -> tuple[InvestigationUserRequestV1, ...]:
        parsed: list[InvestigationUserRequestV1] = []
        for action in actions:
            if not action.startswith(USER_REQUEST_ACTION_PREFIX):
                continue
            try:
                payload = json.loads(action[len(USER_REQUEST_ACTION_PREFIX) :])
                if not isinstance(payload, dict):
                    continue
                payload["request_id"] = UUID(str(payload.get("request_id", "")))
                payload["text"] = _safe_operator_instruction(
                    str(payload.get("text", "")), maximum=2_000
                )
                target = payload.get("target")
                if isinstance(target, dict):
                    target["value"] = _safe_operator_instruction(
                        str(target.get("value", "")), maximum=256
                    )
                parsed.append(InvestigationUserRequestV1.model_validate(payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(parsed[-32:])

    @staticmethod
    def _indicator_inventory(snapshot: ArticleSnapshot) -> IndicatorInventoryV1:
        items: list[ConfirmedIOCContextV1] = []
        encoded_bytes = 0
        truncated = snapshot.confirmed_iocs_truncated
        for raw_item in snapshot.confirmed_iocs:
            item = ConfirmedIOCContextV1.model_validate(raw_item, strict=True)
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json")))
            if (
                len(items) >= MAX_CONTEXT_CONFIRMED_IOCS
                or encoded_bytes + item_bytes > MAX_CONTEXT_CONFIRMED_IOC_BYTES
            ):
                truncated = True
                continue
            items.append(item)
            encoded_bytes += item_bytes
        return IndicatorInventoryV1(items=tuple(items), truncated=truncated)

    async def _evidence_context(
        self, observations: tuple[EvidenceObservation, ...]
    ) -> tuple[EvidenceSummaryV1, ...]:
        result = []
        for observation in observations[:MAX_CONTEXT_EVIDENCE]:
            summary: Mapping[str, Any] | str | None = None
            if self._evidence_summaries is not None:
                summary = await self._evidence_summaries.summary(observation)
            if isinstance(summary, Mapping):
                raw_text = json.dumps(_sanitize(summary), ensure_ascii=False)
            elif isinstance(summary, str):
                raw_text = summary
            else:
                raw_text = (
                    f"{observation.source} observed {observation.object_type} "
                    f"{observation.object_id}"
                )
            text = _safe_untrusted(_truncate(raw_text, MAX_CONTEXT_EVIDENCE_CHARS))
            result.append(
                EvidenceSummaryV1(
                    evidence_id=observation.id,
                    kind=observation.kind,
                    summary=text,
                )
            )
        # Reserve room for contradictions before confirming findings are added.
        return tuple(result)

    @staticmethod
    def _evidence_index(
        observations: tuple[EvidenceObservation, ...],
    ) -> tuple[tuple[EvidenceSummaryV1, ...], bool]:
        result: list[EvidenceSummaryV1] = []
        encoded_bytes = 2
        truncated = len(observations) > MAX_CONTEXT_EVIDENCE_INDEX
        for observation in sorted(observations, key=lambda item: str(item.id)):
            summary = _safe_untrusted(
                _truncate(
                    f"{observation.source} recorded {observation.kind.value} for "
                    f"{observation.object_type} {observation.object_id}",
                    96,
                )
            )
            item = EvidenceSummaryV1(
                evidence_id=observation.id,
                kind=observation.kind,
                summary=summary,
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(result) >= MAX_CONTEXT_EVIDENCE_INDEX
                or encoded_bytes + item_bytes > MAX_CONTEXT_EVIDENCE_INDEX_BYTES
            ):
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    @staticmethod
    def _opinion_note_index(
        opinion: AnalystOpinionV1 | None,
        *,
        finding_ids: set[UUID],
        observation_ids: set[UUID],
    ) -> tuple[tuple[OpinionNoteSummaryV1, ...], bool]:
        if opinion is None:
            return (), False
        result: list[OpinionNoteSummaryV1] = []
        encoded_bytes = 2
        notes = sorted(opinion.observations, key=lambda item: (item.anchor, str(item.id)))
        truncated = len(notes) > MAX_CONTEXT_OPINION_NOTES
        for note in notes:
            item = OpinionNoteSummaryV1(
                note_id=note.id,
                anchor=note.anchor,
                state=note.state,
                relation=note.relation,
                text=_truncate(_safe_untrusted(note.text), 400),
                finding_refs=tuple(
                    sorted(set(note.finding_refs).intersection(finding_ids), key=str)[:16]
                ),
                evidence_refs=tuple(
                    sorted(set(note.evidence_refs).intersection(observation_ids), key=str)[:16]
                ),
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(result) >= MAX_CONTEXT_OPINION_NOTES
                or encoded_bytes + item_bytes > MAX_CONTEXT_OPINION_NOTE_BYTES
            ):
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    async def _tool_coverage(
        self,
        invocations: tuple[ToolInvocation, ...],
        observations: tuple[EvidenceObservation, ...],
    ) -> tuple[tuple[ToolCoverageEntryV1, ...], bool]:
        if not invocations:
            return (), False
        observation_by_id = {item.id: item for item in observations}
        candidates = invocations[-MAX_CONTEXT_TOOL_COVERAGE:]
        truncated = len(invocations) > len(candidates)
        entries: list[ToolCoverageEntryV1] = []
        encoded_bytes = 2
        for invocation in reversed(candidates):
            linked_observations = tuple(
                observation_by_id[ref]
                for ref in reversed(invocation.response_refs[-4:])
                if ref in observation_by_id
            )
            observation = linked_observations[0] if linked_observations else None
            facts: Mapping[str, Any] | str | None = None
            if observation is not None and self._evidence_summaries is not None:
                for candidate_observation in linked_observations:
                    candidate_facts = await self._evidence_summaries.summary(candidate_observation)
                    if candidate_facts is not None:
                        observation = candidate_observation
                        facts = candidate_facts
                        break
            summary = _tool_coverage_summary(invocation, observation, facts)
            entry = ToolCoverageEntryV1(
                invocation_id=invocation.id,
                tool_name=_truncate(_safe_untrusted(invocation.tool_name), 128),
                parameter_key=_coverage_parameter_key(invocation),
                status=invocation.status,
                observation_id=observation.id if observation is not None else None,
                summary=summary,
            )
            item_bytes = len(canonical_json_bytes(entry.model_dump(mode="json"))) + 1
            if encoded_bytes + item_bytes > MAX_CONTEXT_TOOL_COVERAGE_BYTES:
                truncated = True
                continue
            entries.append(entry)
            encoded_bytes += item_bytes
        return tuple(reversed(entries)), truncated

    def _finding_context(
        self,
        findings: tuple[InvestigationFinding, ...],
        *,
        observation_ids: set[UUID],
    ) -> tuple[tuple[FindingSummaryV1, ...], bool]:
        result: list[FindingSummaryV1] = []
        encoded_bytes = 2
        truncated = len(findings) > MAX_CONTEXT_FINDINGS
        for finding in self._order_findings(findings):
            valid_refs = tuple(ref for ref in finding.observation_refs if ref in observation_ids)
            item = FindingSummaryV1(
                finding_id=finding.id,
                kind=finding.kind,
                assessment=(
                    FindingAssessment.INCONCLUSIVE
                    if finding.assessment is FindingAssessment.SUPPORTED
                    and (not valid_refs or len(valid_refs) != len(finding.observation_refs))
                    else finding.assessment
                ),
                statement=_safe_untrusted(
                    _truncate(finding.statement, MAX_CONTEXT_FINDING_STATEMENT_CHARS)
                ),
                observation_refs=tuple(sorted(set(valid_refs), key=str)[:128]),
                claim_refs=tuple(sorted(set(finding.claim_refs))[:128]),
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(result) >= MAX_CONTEXT_FINDINGS
                or encoded_bytes + item_bytes > MAX_CONTEXT_FINDING_BYTES
            ):
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    @staticmethod
    def _order_findings(
        findings: Sequence[InvestigationFinding],
    ) -> tuple[InvestigationFinding, ...]:
        return tuple(sorted(findings, key=lambda item: str(item.id)))

    @staticmethod
    def _hypothesis_payload(
        hypothesis: Hypothesis,
        *,
        observation_ids: set[UUID],
        finding_ids: set[UUID],
    ) -> dict[str, Any]:
        evidence_refs = tuple(
            sorted(
                {item for item in hypothesis.evidence_refs if item in observation_ids},
                key=str,
            )[:8]
        )
        finding_refs = tuple(
            sorted({item for item in hypothesis.finding_refs if item in finding_ids}, key=str)[:8]
        )
        return {
            "id": hypothesis.id,
            "statement": _truncate(_safe_untrusted(hypothesis.statement), 500),
            "status": hypothesis.status.value,
            "evidence_refs": [str(item) for item in evidence_refs],
            "finding_refs": [str(item) for item in finding_refs],
        }

    @staticmethod
    def _full_article(snapshot: ArticleSnapshot) -> ArticleContextV1:
        article = snapshot.article
        publication_anchors: list[PublicationAnchorV1] = []
        anchor_bytes = 2
        anchors_truncated = article.publication_anchors_truncated
        for anchor in sorted(article.publication_anchors, key=lambda item: item.anchor):
            excerpt = _safe_untrusted(_truncate(anchor.excerpt, 96))
            item = PublicationAnchorV1(anchor=anchor.anchor, excerpt=excerpt)
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(publication_anchors) >= MAX_CONTEXT_PUBLICATION_ANCHORS
                or anchor_bytes + item_bytes > MAX_CONTEXT_PUBLICATION_ANCHOR_BYTES
            ):
                anchors_truncated = True
                continue
            publication_anchors.append(item)
            anchor_bytes += item_bytes
        return ArticleContextV1(
            short_summary=_truncate(
                _safe_untrusted(article.short_summary), MAX_ARTICLE_NARRATIVE_CHARS
            ),
            claims=tuple(
                ArticleClaimV1(
                    id=item.id,
                    statement=_truncate(_safe_untrusted(item.statement), 900),
                    source_refs=tuple(
                        ref
                        for ref in item.source_refs[:8]
                        if len(ref) <= 256
                        and not _SECRET_KEY.search(ref)
                        and not _SIGNED_QUERY.search(ref)
                    ),
                )
                for item in article.claims[:16]
            ),
            documented_samples=tuple(
                sample
                for sample in article.documented_samples[:128]
                if isinstance(sample, str)
                and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", sample)
            ),
            known_chain=tuple(
                {"summary": _truncate(json.dumps(_sanitize(item), ensure_ascii=False), 360)}
                for item in _prioritize_context_rows(article.known_chain)[:32]
            ),
            open_questions=tuple(
                _truncate(_safe_untrusted(item), 560) for item in article.open_questions[:16]
            ),
            publication_anchors=tuple(publication_anchors),
            publication_anchors_truncated=anchors_truncated,
        )

    def _available_tools(
        self, context: ToolExecutionContext, registry: ToolRegistry | None
    ) -> tuple[ToolDefinition, ...]:
        selected = registry or self._registry
        if selected is not None:
            return selected.list_available(context)
        return ()

    def register_local_tools(
        self, registry: InMemoryToolRegistry, read_port: InvestigationReadPort
    ) -> None:
        self._registry = registry
        registry.register(
            _definition(
                "evidence.get",
                {"observation_id": {"type": "string", "format": "uuid"}},
                ("observation_id",),
            ),
            _ReadAdapter(
                lambda args: read_port.get_evidence(UUID(str(args["observation_id"]))), "evidence"
            ),
        )
        registry.register(
            _definition(
                "article.get_claims",
                {
                    "claim_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 128,
                    }
                },
                (),
            ),
            _ClaimsAdapter(read_port),
        )
        registry.register(
            _definition(
                "sample.get_feature_pack",
                {"sample_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
                ("sample_sha256",),
            ),
            _ReadAdapter(
                lambda args: read_port.get_feature_pack(str(args["sample_sha256"])), "feature pack"
            ),
        )
        registry.register(
            _definition(
                "graph.neighbors",
                {
                    "object_type": {"type": "string", "maxLength": 64},
                    "object_id": {"type": "string", "maxLength": 256},
                },
                ("object_type", "object_id"),
            ),
            _NeighborsAdapter(read_port),
        )


def _definition(
    name: str,
    properties: Mapping[str, Any],
    required: tuple[str, ...],
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        schema_version="1",
        input_schema={
            "type": "object",
            "properties": dict(properties),
            "required": list(required),
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_READ,
        cost_model={},
        confidentiality=ToolConfidentiality.INTERNAL,
        limitations=("Read-only persisted context; returned data is untrusted.",),
        capability_key="investigation.local_read",
    )


class _ReadAdapter:
    def __init__(self, read: Any, label: str) -> None:
        self._read = read
        self._label = label

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        result = await self._read(arguments)
        if result is None:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY,
                message=f"No persisted {self._label} was found.",
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(json.dumps(_sanitize(result), ensure_ascii=False))
            },
        )


class _ClaimsAdapter:
    def __init__(self, read_port: InvestigationReadPort) -> None:
        self._read_port = read_port

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        claim_ids = tuple(arguments.get("claim_ids", ()))
        claims = await self._read_port.get_claims(context.investigation_id, claim_ids)
        if not claims:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY, message="No matching article claims."
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(json.dumps(_sanitize(claims), ensure_ascii=False))
            },
        )


class _NeighborsAdapter:
    def __init__(self, read_port: InvestigationReadPort) -> None:
        self._read_port = read_port

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        neighbors = await self._read_port.graph_neighbors(
            str(arguments["object_type"]), str(arguments["object_id"])
        )
        if not neighbors:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY, message="No persisted graph neighbors."
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(
                    json.dumps(_sanitize(neighbors), ensure_ascii=False)
                )
            },
        )


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key)) and "raw_bytes" not in str(key).lower()
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value[:128]]
    if isinstance(value, bytes | bytearray | memoryview):
        return "[binary omitted]"
    if isinstance(value, str):
        text = _SIGNED_URL.sub("[URL omitted]", value)
        text = _SECRET_VALUE.sub(r"\1=[redacted]", text)
        text = _BEARER_VALUE.sub("Bearer [redacted]", text)
        if _SIGNED_QUERY.search(text):
            return "[signed URL omitted]"
        return _truncate(text, 4_000)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _truncate(str(value), 1_000)


def _safe_untrusted(value: str) -> str:
    sanitized = str(_sanitize(value))
    return f"[UNTRUSTED ARTICLE OR TOOL DATA]\n{sanitized}\n[END UNTRUSTED DATA]"


def _safe_untrusted_excerpt(value: str, maximum: int) -> str:
    prefix = "[UNTRUSTED ARTICLE OR TOOL DATA]\n"
    suffix = "\n[END UNTRUSTED DATA]"
    body = _truncate(_sanitize(value), max(0, maximum - len(prefix) - len(suffix)))
    return f"{prefix}{body}{suffix}"


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_operator_instruction(value: str, *, maximum: int) -> str:
    def redact_signed_url(match: re.Match[str]) -> str:
        return "[signed URL omitted]" if _SIGNED_QUERY.search(match.group(0)) else match.group(0)

    sanitized = _SIGNED_URL.sub(redact_signed_url, str(value))
    sanitized = _SECRET_VALUE.sub(r"\1=[redacted]", sanitized)
    sanitized = _BEARER_VALUE.sub("Bearer [redacted]", sanitized)
    return _truncate(sanitized, maximum)


def _safe_budget_names(values: Mapping[str, int | None]) -> dict[str, int | None]:
    """Keep budget dimensions visible without triggering secret-field filters."""
    result: dict[str, int | None] = {}
    forbidden = re.compile(r"token|secret|password|authorization|credential|cookie", re.I)
    for index, (name, amount) in enumerate(sorted(values.items())):
        if forbidden.search(name):
            safe_name = "context_units" if name == "context_tokens" else f"budget_{index}"
        else:
            safe_name = name
        result[safe_name] = amount
    return result


def _truncate(value: str, maximum: int) -> str:
    clean = " ".join(value.replace("\x00", "").split())
    if len(clean) <= maximum:
        return clean
    return clean[: max(0, maximum - 1)].rstrip() + "…"


def _evidence_summary_for(
    observation: EvidenceObservation, summaries: tuple[EvidenceSummaryV1, ...]
) -> str:
    for item in summaries:
        if item.evidence_id == observation.id:
            return item.summary
    return f"{observation.kind.value}: {observation.object_type} {observation.object_id}"


async def provider_authorization_blocked_capabilities(
    *,
    investigation_id: UUID,
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
) -> tuple[str, ...]:
    """Return provider blocks after two persisted authorization refusals."""
    observation_by_id = {item.id: item for item in observations}
    counts: dict[str, int] = {}
    for invocation in invocations:
        if (
            invocation.investigation_id != investigation_id
            or invocation.status is not ToolInvocationStatus.UNAVAILABLE
        ):
            continue
        provider = invocation.tool_name.split(".", 1)[0].lower()
        if provider not in {"shodan", "vt"}:
            continue
        if any(
            (observation := observation_by_id.get(ref)) is not None
            and observation.object_type == PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE
            for ref in invocation.response_refs
        ):
            counts[provider] = counts.get(provider, 0) + 1
    return tuple(
        f"{provider}: provider authorization refused"
        for provider, count in sorted(counts.items())
        if count >= PROVIDER_AUTHORIZATION_REFUSAL_THRESHOLD
    )


def provider_download_blocked_capabilities(
    *,
    investigation_id: UUID,
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
) -> tuple[str, ...]:
    """Block only VT sample downloads after two same-cause refusals on distinct hashes."""
    observation_by_id = {item.id: item for item in observations}
    hashes_by_reason: dict[str, set[str]] = {}
    reason_details = {
        PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE: (
            "vt.file_download: provider download route unavailable"
        ),
        PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE: (
            "vt.file_download: provider authorization refused"
        ),
    }
    for invocation in invocations:
        if (
            invocation.investigation_id != investigation_id
            or invocation.tool_name != "vt.download_sample"
            or invocation.status is not ToolInvocationStatus.UNAVAILABLE
        ):
            continue
        parameters = thaw_json(invocation.normalized_parameters)
        requested_hash = parameters.get("sha256") if isinstance(parameters, Mapping) else None
        identity = requested_hash if isinstance(requested_hash, str) else str(invocation.id)
        for ref in invocation.response_refs:
            observation = observation_by_id.get(ref)
            if observation is not None and observation.object_type in reason_details:
                hashes_by_reason.setdefault(observation.object_type, set()).add(identity)
    return tuple(
        reason_details[object_type]
        for object_type in sorted(reason_details)
        if len(hashes_by_reason.get(object_type, ())) >= PROVIDER_DOWNLOAD_REFUSAL_THRESHOLD
    )


def _coverage_parameter_key(invocation: ToolInvocation) -> str:
    parameters = thaw_json(invocation.normalized_parameters)
    if not isinstance(parameters, Mapping):
        return "parameters: unavailable"
    preferred_names = (
        "file_hash",
        "hash",
        "sha256",
        "sha1",
        "md5",
        "ip",
        "ip_address",
        "domain",
        "url",
        "host",
        "query",
    )
    for name in preferred_names:
        value = parameters.get(name)
        if isinstance(value, str) and value.strip():
            safe_value = _truncate(_safe_untrusted(value), 240)
            return f"{name}: {safe_value}"
    encoded = json.dumps(_sanitize(parameters), ensure_ascii=False, sort_keys=True)
    return f"parameters: {_truncate(_safe_untrusted(encoded), 240)}"


def _tool_coverage_summary(
    invocation: ToolInvocation,
    observation: EvidenceObservation | None,
    facts: Mapping[str, Any] | str | None,
) -> str:
    if (
        observation is not None
        and observation.object_type == PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE
    ):
        return "Provider authorization was refused."
    if (
        observation is not None
        and observation.object_type == PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE
    ):
        return "VirusTotal download route unavailable; do not retry downloads."
    if (
        observation is not None
        and observation.object_type == PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE
    ):
        return "VirusTotal download authorization refused; do not retry downloads."
    if observation is not None and observation.kind is EvidenceKind.NEGATIVE_RESULT:
        return "The provider returned an explicit no-result observation."

    values: Mapping[str, Any] = facts if isinstance(facts, Mapping) else {}
    if invocation.tool_name.startswith("vt."):
        nested = values.get("summary")
        if isinstance(nested, Mapping):
            values = nested
        stats = values.get("last_analysis_stats")
        parts: list[str] = []
        if isinstance(stats, Mapping):
            counts = {
                key: value
                for key, value in stats.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            detections = counts.get("malicious", 0) + counts.get("suspicious", 0)
            total = counts.get(
                "total", sum(value for key, value in counts.items() if key != "total")
            )
            if total:
                parts.append(f"{detections}/{total} detections")
        name = _first_safe_fact(values, ("meaningful_name", "name"), 96)
        file_type = _first_safe_fact(values, ("type_description", "type"), 72)
        if name:
            parts.append(f"file {name}")
        if file_type:
            parts.append(f"type {file_type}")
        if parts:
            return _truncate(_safe_untrusted("VT: " + "; ".join(parts)), 400)
    if invocation.tool_name.startswith("shodan."):
        nested = values.get("summary")
        if isinstance(nested, Mapping):
            values = nested
        ports = values.get("ports")
        parts = []
        if isinstance(ports, (tuple, list)):
            safe_ports = [
                str(value)
                for value in ports[:16]
                if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 65_535
            ]
            if safe_ports:
                parts.append("ports " + ",".join(safe_ports))
        organization = _first_safe_fact(values, ("organization", "org"), 96)
        if organization:
            parts.append("organization " + organization)
        if parts:
            return _truncate(_safe_untrusted("Shodan: " + "; ".join(parts)), 400)
    if observation is not None:
        return _truncate(
            _safe_untrusted(
                f"{observation.source} recorded {observation.kind.value} "
                f"for {observation.object_type} {observation.object_id}."
            ),
            400,
        )
    return f"{invocation.tool_name} returned {invocation.status.value}."


def _first_safe_fact(values: Mapping[str, Any], names: tuple[str, ...], maximum: int) -> str | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            return _truncate(_safe_untrusted(value), maximum)
    return None


def _prioritize_context_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    indexed = tuple(enumerate(rows))
    return tuple(
        row
        for _, row in sorted(
            indexed,
            key=lambda pair: (
                0 if pair[1].get("kind") == "documented_ioc" else 1,
                pair[0],
            ),
        )
    )


__all__ = [
    "MAX_OPERATOR_REQUESTS",
    "MAX_OPERATOR_REQUEST_BYTES",
    "ArticleSnapshot",
    "ArticleSnapshotPort",
    "CanonicalEvidenceSummaryPort",
    "EvidenceSummaryPort",
    "InvestigationGraphReadPort",
    "InvestigationReadPort",
    "RepositoryInvestigationGraphReadPort",
    "RepositoryInvestigationReadPort",
    "ResearchContextBuilder",
    "SampleFeaturePackPort",
    "provider_authorization_blocked_capabilities",
    "provider_download_blocked_capabilities",
]
