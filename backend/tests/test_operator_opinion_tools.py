from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionDraftView,
    OperatorOpinionToolAdapter,
    register_operator_opinion_tools,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
    validate_json_schema,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationState,
    AnalystOpinionStatus,
)
from cti_app.domain.investigation import InvestigationBudget, ToolInvocationStatus
from cti_app.domain.protocol_model import ProtocolDiagramKind


class OpinionService:
    def __init__(self, current):
        self.current = current
        self.revision = None

    async def get_draft(self, investigation_id):
        return AnalystOpinionDraftView(self.current, (self.current,), ())

    async def revise(self, opinion_id, expected_version, edits, actor, *, expected_sha256):
        self.revision = (opinion_id, expected_version, edits, actor, expected_sha256)
        self.current = SimpleNamespace(
            **{
                **vars(self.current),
                "version": self.current.version + 1,
                "observations": edits.observations or self.current.observations,
            }
        )
        return self.current


class ArticleSnapshots:
    def __init__(self, anchors):
        self.anchors = anchors

    async def resolve(self, synthesis_artifact_id):
        return SimpleNamespace(
            article=SimpleNamespace(
                publication_anchors=tuple(SimpleNamespace(anchor=item) for item in self.anchors)
            )
        )


def current_opinion(investigation_id):
    return SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        version=3,
        content_sha256="a" * 64,
        status=AnalystOpinionStatus.DRAFT,
        blocks=(),
        observations=(),
        unresolved_points=(),
        protocol_figures=(),
    )


def context(investigation_id):
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="operator-note:idem-1",
    )


@pytest.mark.asyncio
async def test_observed_note_requires_existing_anchor_and_real_evidence_refs() -> None:
    investigation_id = uuid4()
    evidence_ref = uuid4()
    opinion = current_opinion(investigation_id)
    service = OpinionService(opinion)
    adapter = OperatorOpinionToolAdapter(
        service=service,
        investigations=SimpleNamespace(
            get=async_get(SimpleNamespace(synthesis_artifact_id=uuid4()))
        ),
        article_snapshots=ArticleSnapshots(("section:methods",)),
    )
    args = {
        "action": "note",
        "anchor": "section:methods",
        "text": "The cited test covers this protocol statement.",
        "state": "observed",
        "relation": "qualifies",
        "expected_opinion_version": opinion.version,
        "expected_opinion_sha256": opinion.content_sha256,
        "finding_refs": [],
        "evidence_refs": [str(evidence_ref)],
    }

    result = await adapter.execute(args, context(investigation_id))

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert service.revision is not None
    note = service.revision[2].observations[0]
    assert note.anchor == "section:methods"
    assert note.state is AnalystObservationState.OBSERVED
    assert note.evidence_refs == (evidence_ref,)
    assert result.observation_refs == (evidence_ref,)


@pytest.mark.asyncio
async def test_note_rejects_unlisted_anchor_and_synthetic_observed_note() -> None:
    investigation_id = uuid4()
    opinion = current_opinion(investigation_id)
    service = OpinionService(opinion)
    adapter = OperatorOpinionToolAdapter(
        service=service,
        investigations=SimpleNamespace(
            get=async_get(SimpleNamespace(synthesis_artifact_id=uuid4()))
        ),
        article_snapshots=ArticleSnapshots(("section:methods",)),
    )
    base = {
        "action": "note",
        "anchor": "section:unknown",
        "text": "An unsupported operator statement.",
        "state": "observed",
        "relation": "unresolved",
        "expected_opinion_version": opinion.version,
        "expected_opinion_sha256": opinion.content_sha256,
        "finding_refs": [],
        "evidence_refs": [],
    }
    bad_anchor = await adapter.execute(base, context(investigation_id))
    base["anchor"] = "section:methods"
    no_evidence = await adapter.execute(base, context(investigation_id))

    assert bad_anchor.error_code == "invalid_note_anchor"
    assert no_evidence.status is ToolInvocationStatus.FAILED
    assert service.revision is None


def async_get(value):
    async def get(_key):
        return value

    return get


def test_compose_and_revise_schemas_describe_bounded_protocol_figure_selection() -> None:
    registry = InMemoryToolRegistry()
    register_operator_opinion_tools(
        registry,
        service=cast(Any, object()),
        investigations=object(),
        article_snapshots=cast(Any, object()),
    )
    selection = {
        "protocol_model_blob_id": str(uuid4()),
        "render_observation_id": str(uuid4()),
        "views": [item.value for item in ProtocolDiagramKind],
    }
    definitions = {}
    for name in ("opinion.compose", "opinion.revise"):
        registered = registry.get(name, "operator-opinion.v1")
        assert registered is not None
        definitions[name] = registered[0]

    for name, definition in definitions.items():
        schema = definition.input_schema
        protocol_schema = schema["properties"]["protocol_figures"]
        assert protocol_schema["maxItems"] == 1
        assert "documenté, non observé" in protocol_schema["description"]
        payload = (
            {
                "action": "compose",
                "selection": {"finding_ids": [], "claim_ids": [], "block_types": []},
            }
            if name == "opinion.compose"
            else {
                "action": "revise",
                "opinion_id": str(uuid4()),
                "expected_opinion_version": 1,
                "expected_opinion_sha256": "a" * 64,
                "blocks": [],
                "unresolved_points": [],
            }
        )
        validate_json_schema({**payload, "protocol_figures": [selection]}, schema)
        with pytest.raises(ValueError):
            validate_json_schema(
                {
                    **payload,
                    "protocol_figures": [{**selection, "views": selection["views"][:2]}],
                },
                schema,
            )
