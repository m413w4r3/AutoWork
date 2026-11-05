from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from cti_app.application.publication_augmentation import (
    PublicationAugmentationError,
    PublicationAugmentationErrorCode,
    augment_publication_with_opinion,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V7_SCHEMA_VERSION,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    PublicationDocumentV7,
    parse_publication_document,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.test_publication_builder_v4 import _build_v4
from tests.test_publication_document_v6 import _approved_opinion, _base_v5


def _v6(base: PublicationDocumentV5) -> PublicationDocumentV6:
    return PublicationDocumentV6(
        base_publication=base,
        provenance=PublicationDocumentV6ProvenanceV1(
            base_publication_artifact_id=UUID(int=909),
            base_publication_sha256=publication_document_v5_sha256(base),
        ),
    )


def test_v7_round_trip_preserves_v6_base_and_explicit_routing_metadata() -> None:
    base = _base_v5()
    v7 = PublicationDocumentV7(
        base_publication=_v6(base),
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )

    payload = serialize_publication_document(v7)
    parsed = parse_publication_document(payload)

    assert isinstance(parsed, PublicationDocumentV7)
    assert parsed.base_publication == v7.base_publication
    assert parsed.base_publication.base_publication == base
    assert parsed.publication_kind is PublicationKind.BRIEF
    assert parsed.routing_decision_hash == "a" * 64
    assert parsed.routing_policy_version == "routing-policy-v1"
    assert payload["schema_version"] == PUBLICATION_DOCUMENT_V7_SCHEMA_VERSION


def test_v7_requires_kind_and_rejects_non_v6_bases() -> None:
    base = _base_v5()
    payload = serialize_publication_document(
        PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="b" * 64,
            routing_policy_version="routing-policy-v1",
        )
    )
    del payload["publication_kind"]
    with pytest.raises(ValueError):
        parse_publication_document(payload)

    with pytest.raises(ValueError, match="V6 base"):
        PublicationDocumentV7(
            base_publication=base,  # type: ignore[arg-type]
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="b" * 64,
            routing_policy_version="routing-policy-v1",
        )


def test_v4_v5_v6_payloads_remain_readable() -> None:
    base_v5 = _base_v5()
    opinion, context = _approved_opinion(base_v5)
    v6 = augment_publication_with_opinion(base_v5, opinion, context)
    v4_payload = serialize_publication_document(_build_v4())
    v5_payload = serialize_publication_document(base_v5)
    v6_payload = serialize_publication_document(v6)

    assert isinstance(parse_publication_document(v4_payload), type(_build_v4()))
    assert isinstance(parse_publication_document(v5_payload), PublicationDocumentV5)
    assert isinstance(parse_publication_document(v6_payload), PublicationDocumentV6)


def test_article_augmentation_preserves_v7_routing_and_briefs_reject_it() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    article = PublicationDocumentV7(
        base_publication=_v6(base),
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="e" * 64,
        routing_policy_version="routing-policy-v1",
    )
    artifact_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(article))
    ).hexdigest()
    opinion = replace(opinion, base_publication_sha256=artifact_sha256, content_sha256="")
    context = replace(context, base_publication_sha256=artifact_sha256)
    brief = replace(article, publication_kind=PublicationKind.BRIEF)

    augmented = augment_publication_with_opinion(article, opinion, context)

    assert isinstance(augmented, PublicationDocumentV7)
    assert augmented.publication_kind is PublicationKind.ARTICLE
    assert augmented.routing_decision_hash == article.routing_decision_hash
    assert augmented.base_publication.analyst_opinion == opinion
    assert augmented.base_publication.provenance.base_publication_sha256 == artifact_sha256
    assert (
        augmented.base_publication.provenance.base_publication_body_sha256
        == publication_document_v5_sha256(base)
    )
    assert parse_publication_document(serialize_publication_document(augmented)) == augmented
    with pytest.raises(PublicationAugmentationError) as failure:
        augment_publication_with_opinion(brief, opinion, context)
    assert failure.value.code is PublicationAugmentationErrorCode.PUBLICATION_KIND_INVALID
