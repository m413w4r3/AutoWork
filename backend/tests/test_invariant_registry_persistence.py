from pathlib import Path

from cti_app.infrastructure.database.models.invariants import (
    CandidateInvariantProvenanceRow,
    CandidateInvariantRow,
    CandidateInvariantTransitionRow,
    InvariantRejectionRow,
)


def test_registry_tables_are_queryable_and_do_not_own_blobs() -> None:
    tables = (
        CandidateInvariantRow.__table__,
        CandidateInvariantProvenanceRow.__table__,
        CandidateInvariantTransitionRow.__table__,
        InvariantRejectionRow.__table__,
    )
    assert {table.name for table in tables} == {
        "candidate_invariants",
        "candidate_invariant_provenances",
        "candidate_invariant_transitions",
        "invariant_rejections",
    }
    assert all("blob_id" not in table.c for table in tables)
    assert "investigation_id" in CandidateInvariantRow.__table__.c
    assert "status" in CandidateInvariantRow.__table__.c
    assert "type" in CandidateInvariantRow.__table__.c
    assert "proposal_key" in CandidateInvariantRow.__table__.c
    assert "cause" in InvariantRejectionRow.__table__.c
    assert {index.name for table in tables for index in table.indexes} >= {
        "ix_candidate_invariants_investigation",
        "ix_candidate_invariants_status",
        "ix_candidate_invariants_type",
        "ix_invariant_rejections_investigation",
        "ix_invariant_rejections_cause",
    }


def test_migration_is_the_single_next_revision() -> None:
    migration = Path(__file__).parents[1] / "migrations" / "versions" / "0011_invariant_registry.py"
    source = migration.read_text()
    assert 'revision = "0011_invariant_registry"' in source
    assert 'down_revision = "0010_code_features"' in source
