from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryBatchStatus,
    DiscoveryContribution,
    DiscoveryIocStatus,
    DiscoveryIocType,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    ProvisionalDiscoveryIoc,
    ProvisionalIocPublicationRelation,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.infrastructure.database.models import DiscoveryBatchRow


class SqlAlchemyDiscoveryBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        statement = (
            insert(DiscoveryBatchRow)
            .values(**_discovery_batch_values(batch))
            .on_conflict_do_nothing(
                index_elements=[DiscoveryBatchRow.edition_id, DiscoveryBatchRow.request_hash]
            )
            .returning(DiscoveryBatchRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None:
        row = await self._session.get(DiscoveryBatchRow, batch_id)
        return _discovery_batch_from_row(row) if row else None

    async def get_by_request_hash(
        self, edition_id: UUID, request_hash: str
    ) -> DiscoveryBatch | None:
        from sqlalchemy import select

        row = await self._session.scalar(
            select(DiscoveryBatchRow).where(
                DiscoveryBatchRow.edition_id == edition_id,
                DiscoveryBatchRow.request_hash == request_hash,
            )
        )
        return _discovery_batch_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryBatch]:
        from sqlalchemy import select

        rows = await self._session.scalars(
            select(DiscoveryBatchRow)
            .where(DiscoveryBatchRow.edition_id == edition_id)
            .order_by(DiscoveryBatchRow.created_at, DiscoveryBatchRow.id)
        )
        return [_discovery_batch_from_row(row) for row in rows]

    async def save(self, batch: DiscoveryBatch) -> None:
        row = await self._session.get(DiscoveryBatchRow, batch.id)
        if row is None:
            raise LookupError(f"Discovery batch {batch.id} does not exist")
        batch.updated_at = datetime.now(UTC)
        for field_name, value in _discovery_batch_values(batch).items():
            setattr(row, field_name, value)
        await self._session.flush()


def _discovery_batch_values(batch: DiscoveryBatch) -> dict[str, object]:
    return {
        "id": batch.id,
        "edition_id": batch.edition_id,
        "request_hash": batch.request_hash,
        "complementary_axis": batch.complementary_axis,
        "status": batch.status.value,
        "discovery_model_run_id": batch.discovery_model_run_id,
        "tlp": batch.tlp.value,
        "sensitivity": batch.sensitivity,
        "external_llm_allowed": batch.external_llm_allowed,
        "payload": {
            "report_sha256": batch.report_sha256,
            "parser_version": batch.parser_version,
            "parsing_status": batch.parsing_status,
            "parsing_warnings": list(batch.parsing_warnings),
            "unattached_visible_citations": list(batch.unattached_visible_citations),
            "parsing_revision": batch.parsing_revision,
            "supersedes_batch_id": (
                str(batch.supersedes_batch_id) if batch.supersedes_batch_id else None
            ),
            "replaced_by_batch_id": (
                str(batch.replaced_by_batch_id) if batch.replaced_by_batch_id else None
            ),
            "source_mode": batch.source_mode.value,
            "bridge_capabilities": batch.bridge_capabilities,
            "citation_count": batch.citation_count,
            "source_coverage_complete": batch.source_coverage_complete,
            "source_coverage_incomplete_reason": batch.source_coverage_incomplete_reason,
            "queries": list(batch.queries),
            "citations": list(batch.citations),
            "candidates": [_candidate_payload(candidate) for candidate in batch.candidates],
            "contributions_meta": [
                {
                    "candidate_id": str(contrib.candidate.id),
                    "status": contrib.status.value,
                    "created_at": contrib.created_at.isoformat(),
                    "accepted_at": contrib.accepted_at.isoformat() if contrib.accepted_at else None,
                    "human_note": contrib.human_note,
                }
                for contrib in batch.contributions
            ],
        },
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _candidate_payload(candidate: CandidateTopic) -> dict[str, object]:
    return {
        "id": str(candidate.id),
        "title": candidate.title,
        "summary": candidate.summary,
        "novelty": candidate.novelty,
        "technical_potential": candidate.technical_potential,
        "event_date": candidate.event_date.isoformat() if candidate.event_date else None,
        "uncertainties": list(candidate.uncertainties),
        "relevance_reasons": list(candidate.relevance_reasons),
        "actors": list(candidate.actors),
        "campaigns": list(candidate.campaigns),
        "malware": list(candidate.malware),
        "cves": list(candidate.cves),
        "victims": list(candidate.victims),
        "sectors": list(candidate.sectors),
        "countries": list(candidate.countries),
        "iocs": list(candidate.iocs),
        "provisional_iocs": [_provisional_ioc_payload(ioc) for ioc in candidate.provisional_iocs],
        "likely_artifacts": list(candidate.likely_artifacts),
        "tlp": candidate.tlp.value,
        "sensitivity": candidate.sensitivity,
        "external_llm_allowed": candidate.external_llm_allowed,
        "editorial_status": candidate.editorial_status,
        "sources": [_source_payload(source) for source in candidate.sources],
        "incomplete_sources": [
            _incomplete_source_payload(source) for source in candidate.incomplete_sources
        ],
        "local_ref": candidate.local_ref,
        "actor_or_campaign": candidate.actor_or_campaign,
        "technical_potential_reason": candidate.technical_potential_reason,
        "parsing_warnings": list(candidate.parsing_warnings),
        "markdown_block": candidate.markdown_block,
        "context_only": candidate.context_only,
    }


def _source_payload(source: SourceCandidate) -> dict[str, object]:
    return {
        "id": str(source.id),
        "url": source.url,
        "raw_url": source.raw_url,
        "local_ref": source.local_ref,
        "source_ref": source.source_ref,
        "title": source.title,
        "publisher": source.publisher,
        "role": source.role.value,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "event_date": source.event_date.isoformat() if source.event_date else None,
        "citation": source.citation,
        "period_relation": source.period_relation.value,
        "ioc_presence": source.ioc_presence.value,
        "ioc_declared_count": source.ioc_declared_count,
        "ioc_visible_count": source.ioc_visible_count,
        "parsing_warnings": list(source.parsing_warnings),
        "markdown_block": source.markdown_block,
        "verification_status": source.verification_status.value,
        "relationship_status": source.relationship_status.value,
        "verification_changed_at": (
            source.verification_changed_at.isoformat() if source.verification_changed_at else None
        ),
        "verification_changed_by": source.verification_changed_by,
        "tlp": source.tlp.value,
        "sensitivity": source.sensitivity,
        "external_llm_allowed": source.external_llm_allowed,
    }


def _incomplete_source_payload(source: IncompleteSourceCandidate) -> dict[str, object]:
    return {
        "id": str(source.id),
        "title": source.title,
        "publisher": source.publisher,
        "raw_url": source.raw_url,
        "local_ref": source.local_ref,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "period_relation": source.period_relation.value,
        "role": source.role.value,
        "ioc_presence": source.ioc_presence.value,
        "ioc_declared_count": source.ioc_declared_count,
        "ioc_visible_count": source.ioc_visible_count,
        "parsing_warnings": list(source.parsing_warnings),
        "markdown_block": source.markdown_block,
    }


def _provisional_ioc_payload(ioc: ProvisionalDiscoveryIoc) -> dict[str, object]:
    return {
        "id": str(ioc.id),
        "raw_value": ioc.raw_value,
        "normalized_value": ioc.normalized_value,
        "declared_type": ioc.declared_type,
        "proposed_type": ioc.proposed_type.value,
        "status": ioc.status.value,
        "model_run_id": str(ioc.model_run_id) if ioc.model_run_id else None,
        "markdown_block": ioc.markdown_block,
        "warnings": list(ioc.warnings),
        "publication_relations": [
            {
                "publication_id": str(relation.publication_id),
                "publication_ref": relation.publication_ref,
                "raw_value": relation.raw_value,
                "markdown_block": relation.markdown_block,
            }
            for relation in ioc.publication_relations
        ],
    }


def _discovery_batch_from_row(row: DiscoveryBatchRow) -> DiscoveryBatch:
    payload = row.payload
    # Load candidates from payload
    candidates = [_candidate_from_payload(item) for item in payload["candidates"]]

    # Load contribution metadata
    contributions_meta = payload["contributions_meta"]
    contrib_map = {UUID(m["candidate_id"]): m for m in contributions_meta}

    # Reconstruct contributions from metadata
    contributions = []
    for candidate in candidates:
        meta = contrib_map[candidate.id]
        accepted_at_raw = meta["accepted_at"]
        contributions.append(
            DiscoveryContribution(
                candidate=candidate,
                status=ContributionStatus(meta["status"]),
                created_at=datetime.fromisoformat(meta["created_at"]),
                accepted_at=(
                    datetime.fromisoformat(accepted_at_raw) if accepted_at_raw else None
                ),
                human_note=meta["human_note"],
            )
        )
    return DiscoveryBatch(
        id=row.id,
        edition_id=row.edition_id,
        request_hash=row.request_hash,
        complementary_axis=row.complementary_axis,
        status=DiscoveryBatchStatus(row.status),
        discovery_model_run_id=row.discovery_model_run_id,
        tlp=TLP(row.tlp),
        sensitivity=row.sensitivity,
        external_llm_allowed=row.external_llm_allowed,
        queries=tuple(payload["queries"]),
        citations=tuple(payload["citations"]),
        contributions=contributions,
        report_sha256=(str(payload["report_sha256"]) if payload["report_sha256"] else None),
        parser_version=str(payload["parser_version"]),
        parsing_status=str(payload["parsing_status"]),
        parsing_warnings=_string_tuple(payload["parsing_warnings"]),
        unattached_visible_citations=tuple(payload["unattached_visible_citations"]),
        parsing_revision=int(payload["parsing_revision"]),
        supersedes_batch_id=(
            UUID(str(payload["supersedes_batch_id"]))
            if payload["supersedes_batch_id"]
            else None
        ),
        replaced_by_batch_id=(
            UUID(str(payload["replaced_by_batch_id"]))
            if payload["replaced_by_batch_id"]
            else None
        ),
        source_mode=DiscoverySourceMode(str(payload["source_mode"])),
        bridge_capabilities=cast(dict[str, object], payload["bridge_capabilities"]),
        citation_count=int(payload["citation_count"]),
        source_coverage_complete=bool(payload["source_coverage_complete"]),
        source_coverage_incomplete_reason=(
            str(payload["source_coverage_incomplete_reason"])
            if payload["source_coverage_incomplete_reason"] is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _candidate_from_payload(value: dict[str, object]) -> CandidateTopic:
    event_date = value.get("event_date")
    return CandidateTopic(
        id=UUID(str(value["id"])),
        title=str(value["title"]),
        summary=str(value["summary"]),
        novelty=str(value["novelty"]),
        technical_potential=int(str(value["technical_potential"])),
        event_date=date.fromisoformat(str(event_date)) if event_date else None,
        uncertainties=_string_tuple(value.get("uncertainties", [])),
        relevance_reasons=_string_tuple(value.get("relevance_reasons", [])),
        actors=_string_tuple(value.get("actors", [])),
        campaigns=_string_tuple(value.get("campaigns", [])),
        malware=_string_tuple(value.get("malware", [])),
        cves=_string_tuple(value.get("cves", [])),
        victims=_string_tuple(value.get("victims", [])),
        sectors=_string_tuple(value.get("sectors", [])),
        countries=_string_tuple(value.get("countries", [])),
        iocs=_string_tuple(value.get("iocs", [])),
        provisional_iocs=[
            _provisional_ioc_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("provisional_iocs", []))
        ],
        likely_artifacts=_string_tuple(value.get("likely_artifacts", [])),
        sources=[
            _source_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("sources", []))
        ],
        incomplete_sources=[
            _incomplete_source_from_payload(item)
            for item in cast(list[dict[str, object]], value.get("incomplete_sources", []))
        ],
        tlp=TLP(str(value["tlp"])),
        sensitivity=str(value["sensitivity"]),
        external_llm_allowed=bool(value["external_llm_allowed"]),
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        actor_or_campaign=str(value.get("actor_or_campaign", "unknown")),
        technical_potential_reason=str(
            value.get("technical_potential_reason", "Non précisé dans le rapport de découverte.")
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
        context_only=bool(value.get("context_only", False)),
        editorial_status=str(value.get("editorial_status", "proposed")),
    )


def _source_from_payload(value: dict[str, object]) -> SourceCandidate:
    published_at = value.get("published_at")
    event_date = value.get("event_date")
    changed_at = value.get("verification_changed_at")
    return SourceCandidate(
        id=UUID(str(value["id"])),
        url=str(value["url"]),
        title=str(value["title"]),
        publisher=str(value["publisher"]),
        role=SourceRole(str(value["role"])),
        published_at=date.fromisoformat(str(published_at)) if published_at else None,
        event_date=date.fromisoformat(str(event_date)) if event_date else None,
        citation=str(value["citation"]) if value.get("citation") is not None else None,
        raw_url=str(value["raw_url"]) if value.get("raw_url") else None,
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        period_relation=PeriodRelation(
            str(value.get("period_relation", PeriodRelation.UNKNOWN.value))
        ),
        ioc_presence=IocPresence(str(value.get("ioc_presence", IocPresence.UNKNOWN.value))),
        ioc_declared_count=(
            int(str(value["ioc_declared_count"]))
            if value.get("ioc_declared_count") is not None
            else None
        ),
        ioc_visible_count=(
            int(str(value["ioc_visible_count"]))
            if value.get("ioc_visible_count") is not None
            else None
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
        verification_status=SourceVerificationStatus(str(value["verification_status"])),
        relationship_status=SourceRelationshipStatus(
            str(value.get("relationship_status", SourceRelationshipStatus.PROVISIONAL.value))
        ),
        verification_changed_at=(datetime.fromisoformat(str(changed_at)) if changed_at else None),
        verification_changed_by=(
            str(value["verification_changed_by"])
            if value.get("verification_changed_by") is not None
            else None
        ),
        tlp=TLP(str(value["tlp"])),
        sensitivity=str(value["sensitivity"]),
        external_llm_allowed=bool(value["external_llm_allowed"]),
    )


def _incomplete_source_from_payload(value: dict[str, object]) -> IncompleteSourceCandidate:
    published_at = value.get("published_at")
    return IncompleteSourceCandidate(
        id=UUID(str(value["id"])),
        title=str(value["title"]),
        publisher=str(value.get("publisher", "unknown")),
        raw_url=str(value["raw_url"]) if value.get("raw_url") else None,
        local_ref=str(value["local_ref"]) if value.get("local_ref") else None,
        published_at=date.fromisoformat(str(published_at)) if published_at else None,
        period_relation=PeriodRelation(
            str(value.get("period_relation", PeriodRelation.UNKNOWN.value))
        ),
        role=SourceRole(str(value.get("role", SourceRole.UNKNOWN.value))),
        ioc_presence=IocPresence(str(value.get("ioc_presence", IocPresence.UNKNOWN.value))),
        ioc_declared_count=(
            int(str(value["ioc_declared_count"]))
            if value.get("ioc_declared_count") is not None
            else None
        ),
        ioc_visible_count=(
            int(str(value["ioc_visible_count"]))
            if value.get("ioc_visible_count") is not None
            else None
        ),
        parsing_warnings=_string_tuple(value.get("parsing_warnings", [])),
        markdown_block=(str(value["markdown_block"]) if value.get("markdown_block") else None),
    )


def _provisional_ioc_from_payload(value: dict[str, object]) -> ProvisionalDiscoveryIoc:
    relations = cast(list[dict[str, object]], value.get("publication_relations", []))
    return ProvisionalDiscoveryIoc(
        id=UUID(str(value["id"])),
        raw_value=str(value["raw_value"]),
        normalized_value=(
            str(value["normalized_value"]) if value.get("normalized_value") is not None else None
        ),
        declared_type=str(value.get("declared_type", "unknown")),
        proposed_type=DiscoveryIocType(str(value.get("proposed_type", "unknown"))),
        status=DiscoveryIocStatus(str(value.get("status", "provisional_visible"))),
        publication_relations=tuple(
            ProvisionalIocPublicationRelation(
                publication_id=UUID(str(item["publication_id"])),
                publication_ref=str(item["publication_ref"]),
                raw_value=str(item["raw_value"]),
                markdown_block=str(item["markdown_block"]),
            )
            for item in relations
        ),
        model_run_id=(
            UUID(str(value["model_run_id"])) if value.get("model_run_id") is not None else None
        ),
        markdown_block=str(value.get("markdown_block", "")),
        warnings=_string_tuple(value.get("warnings", [])),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[object], value))
