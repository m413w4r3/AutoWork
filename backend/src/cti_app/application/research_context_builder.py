"""Build bounded full/delta Bridge contexts from canonical investigation data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.investigation_contracts import (
    ArticleClaimV1,
    ArticleContextV1,
    ContextChangeV1,
    ContextInvestigationV1,
    EvidenceSummaryV1,
    FindingSummaryV1,
    InvestigationContextV1,
    InvestigationDeltaV1,
    InvestigationSourceV1,
    InvestigationUserRequestV1,
    ToolSummaryV1,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    SampleAssociationRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    InvestigationFinding,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
)

MAX_ARTICLE_NARRATIVE_CHARS = 12_000
MAX_CONTEXT_HYPOTHESES = 16
MAX_CONTEXT_EVIDENCE = 16
MAX_CONTEXT_FINDINGS = 24
MAX_CONTEXT_TOOLS = 64
USER_REQUEST_ACTION_PREFIX = "user-request:"
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
    ) -> None:
        self.article_snapshots = article_snapshots
        self._observations = observations
        self._findings = findings
        self._registry = registry
        self._evidence_summaries = evidence_summaries

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
        new_observations = tuple(
            item for item in observations if item.id not in state.last_seen_evidence_ids
        )
        if state.turn_index:
            # The delta window is bounded: present the freshest evidence first so a
            # tool result produced by the previous turn is never queued behind an
            # older backlog. Older unseen evidence follows on later turns.
            new_observations = tuple(reversed(new_observations))
        new_findings = tuple(
            item for item in findings if item.id not in state.last_seen_finding_ids
        )
        evidence_summary = await self._evidence_context(
            new_observations if state.turn_index else observations
        )
        finding_summary = self._finding_context(
            new_findings if state.turn_index else findings,
            observation_ids=all_observation_ids,
        )
        tools = tuple(
            ToolSummaryV1(
                name=item.name,
                schema_version=item.schema_version,
                effect=item.effect,
            )
            for item in sorted(
                self._available_tools(tool_context, registry),
                key=lambda definition: (definition.name, definition.schema_version),
            )[:MAX_CONTEXT_TOOLS]
        )
        hypotheses_for_context = state.hypotheses
        if state.turn_index:
            hypotheses_for_context = tuple(
                sorted(
                    (
                        item
                        for item in state.hypotheses
                        if item.status in {HypothesisStatus.OPEN, HypothesisStatus.REFUTED}
                    ),
                    key=lambda item: item.id,
                )
            )
        hypothesis_payload = tuple(
            self._hypothesis_payload(
                item,
                observation_ids=all_observation_ids,
                finding_ids=all_finding_ids,
            )
            for item in hypotheses_for_context[:MAX_CONTEXT_HYPOTHESES]
        )
        user_requests = self._user_requests_from_actions(state.recent_actions)
        previous_actions = tuple(
            {"summary": _truncate(_safe_untrusted(action), 300)}
            for action in state.recent_actions
            if not action.startswith(USER_REQUEST_ACTION_PREFIX)
        )
        if state.turn_index == 0:
            previous_actions = (
                *previous_actions,
                *(
                    {"summary": _truncate(_safe_untrusted(f"User requested: {item.text}"), 1_000)}
                    for item in user_requests
                ),
            )
        investigation_context = ContextInvestigationV1(
            agenda=tuple(
                _truncate(_safe_untrusted(item.question), 500) for item in state.agenda.items
            ),
            hypotheses=hypothesis_payload,
            new_evidence=evidence_summary,
            findings=finding_summary,
            sample_deltas=(),
            rule_deltas=(),
            previous_actions=previous_actions[-8:],
            user_requests=user_requests,
            blocked_capabilities=tuple(
                item[:128] for item in sorted(set(blocked_capabilities))[:64]
            ),
            budget_remaining=_safe_budget_names(budget_remaining),
        )
        if state.turn_index == 0:
            return InvestigationContextV1(
                view="full",
                investigation_id=investigation_id,
                source=snapshot.source,
                article=self._full_article(snapshot),
                investigation=investigation_context,
                available_tools=tools,
            )

        changes = [
            ContextChangeV1(
                kind=f"finding:{finding.assessment.value}",
                identity=str(finding.id),
                summary=_truncate(_safe_untrusted(finding.statement), 1_200),
            )
            for finding in self._order_findings(new_findings)[:MAX_CONTEXT_FINDINGS]
        ]
        changes.extend(
            ContextChangeV1(
                kind=observation.kind.value,
                identity=str(observation.id),
                summary=_truncate(
                    _safe_untrusted(_evidence_summary_for(observation, evidence_summary)), 1_200
                ),
            )
            for observation in new_observations[:MAX_CONTEXT_EVIDENCE]
        )
        changes.extend(
            ContextChangeV1(
                kind="action",
                identity=str(index),
                summary=_truncate(_safe_untrusted(action), 300),
            )
            for index, action in enumerate(
                item
                for item in state.recent_actions[-16:]
                if not item.startswith(USER_REQUEST_ACTION_PREFIX)
            )
        )
        return InvestigationContextV1(
            view="delta",
            investigation_id=investigation_id,
            source=snapshot.source,
            investigation=investigation_context,
            delta=InvestigationDeltaV1(
                changes=tuple(changes[:32]),
                stable_hypotheses=hypothesis_payload,
                user_requests=user_requests,
            ),
            available_tools=tools,
        )

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
                payload["text"] = _truncate(_safe_untrusted(str(payload.get("text", ""))), 2_000)
                target = payload.get("target")
                if isinstance(target, dict):
                    target["value"] = _truncate(_safe_untrusted(str(target.get("value", ""))), 256)
                parsed.append(InvestigationUserRequestV1.model_validate(payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(parsed[-32:])

    async def _evidence_context(
        self, observations: tuple[EvidenceObservation, ...]
    ) -> tuple[EvidenceSummaryV1, ...]:
        result = []
        for observation in observations[:MAX_CONTEXT_EVIDENCE]:
            summary: Mapping[str, Any] | str | None = None
            if self._evidence_summaries is not None:
                summary = await self._evidence_summaries.summary(observation)
            if isinstance(summary, Mapping):
                text = _safe_untrusted(json.dumps(_sanitize(summary), ensure_ascii=False))
            elif isinstance(summary, str):
                text = _safe_untrusted(summary)
            else:
                text = _safe_untrusted(
                    f"{observation.source} observed {observation.object_type} "
                    f"{observation.object_id}"
                )
            result.append(
                EvidenceSummaryV1(
                    evidence_id=observation.id,
                    kind=observation.kind,
                    summary=_truncate(text, 1_000),
                )
            )
        # Reserve room for contradictions before confirming findings are added.
        return tuple(result)

    def _finding_context(
        self,
        findings: tuple[InvestigationFinding, ...],
        *,
        observation_ids: set[UUID],
    ) -> tuple[FindingSummaryV1, ...]:
        result = []
        for finding in self._order_findings(findings)[:MAX_CONTEXT_FINDINGS]:
            valid_refs = tuple(ref for ref in finding.observation_refs if ref in observation_ids)
            result.append(
                FindingSummaryV1(
                    finding_id=finding.id,
                    kind=finding.kind,
                    assessment=(
                        FindingAssessment.INCONCLUSIVE
                        if finding.assessment is FindingAssessment.SUPPORTED
                        and (not valid_refs or len(valid_refs) != len(finding.observation_refs))
                        else finding.assessment
                    ),
                    statement=_truncate(_safe_untrusted(finding.statement), 1_000),
                    observation_refs=valid_refs[:16],
                    claim_refs=finding.claim_refs[:16],
                )
            )
        return tuple(result)

    @staticmethod
    def _order_findings(
        findings: Sequence[InvestigationFinding],
    ) -> tuple[InvestigationFinding, ...]:
        # Refutations, qualifications, and open questions survive context bounds first.
        rank = {
            FindingAssessment.REFUTED: 0,
            FindingAssessment.QUALIFIED: 1,
            FindingAssessment.INCONCLUSIVE: 2,
            FindingAssessment.SUPPORTED: 3,
        }
        return tuple(
            sorted(
                findings, key=lambda item: (rank[item.assessment], item.created_at, str(item.id))
            )
        )

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
        return ArticleContextV1(
            short_summary=_truncate(
                _safe_untrusted(article.short_summary), MAX_ARTICLE_NARRATIVE_CHARS
            ),
            claims=tuple(
                ArticleClaimV1(
                    id=item.id,
                    statement=_truncate(_safe_untrusted(item.statement), 1_200),
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
                {"summary": _truncate(json.dumps(_sanitize(item), ensure_ascii=False), 600)}
                for item in _prioritize_context_rows(article.known_chain)[:32]
            ),
            open_questions=tuple(
                _truncate(_safe_untrusted(item), 800) for item in article.open_questions[:16]
            ),
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
    "ArticleSnapshot",
    "ArticleSnapshotPort",
    "EvidenceSummaryPort",
    "InvestigationGraphReadPort",
    "InvestigationReadPort",
    "RepositoryInvestigationGraphReadPort",
    "RepositoryInvestigationReadPort",
    "ResearchContextBuilder",
    "SampleFeaturePackPort",
]
