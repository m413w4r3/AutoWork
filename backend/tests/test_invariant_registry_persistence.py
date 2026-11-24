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


def test_invariant_registry_tables_remain_in_the_frozen_baseline() -> None:
    migrations = Path(__file__).parents[1] / "migrations"
    migration_source = (migrations / "versions" / "0001_baseline.py").read_text()
    frozen_source = (migrations / "frozen" / "baseline_0001.py").read_text()
    assert 'revision: str = "0001_baseline"' in migration_source
    assert "down_revision: str | None = None" in migration_source
    assert "BASELINE_CREATE_DDL" in migration_source
    assert "Base.metadata" not in migration_source
    assert "cti_app.infrastructure.database.models" not in frozen_source
    assert "IMMUTABLE — ne pas éditer; toute évolution = nouvelle révision." in frozen_source
    assert "candidate_invariants" in frozen_source
