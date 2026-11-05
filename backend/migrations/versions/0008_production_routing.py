"""Add immutable subject publication routing decisions.

Revision ID: 0008_production_routing
Revises: 0007_vt_retry_trust
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_production_routing"
down_revision: str | None = "0007_vt_retry_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "production_routing_decisions"
_POINTER_COLUMN = "routing_decision_id"
_POINTER_CONSTRAINT = "fk_production_runs_routing_decision"
_TRIGGER = "trg_production_routing_decisions_append_only"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_exists = _TABLE in set(inspector.get_table_names())
    pointer_exists = _POINTER_COLUMN in {
        column["name"] for column in inspector.get_columns("production_runs")
    }
    if table_exists or pointer_exists:
        raise RuntimeError(
            "refusing to add production routing schema over pre-existing routing objects"
        )

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("production_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("pipeline_generation", sa.Integer(), nullable=False),
        sa.Column("extraction_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("extraction_artifact_hash", sa.String(length=64), nullable=True),
        sa.Column("relevance_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("relevance_artifact_hash", sa.String(length=64), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("publication_kind", sa.String(length=16), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("confirmed_ioc_count", sa.Integer(), nullable=False),
        sa.Column("contextual_ioc_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_ioc_count", sa.Integer(), nullable=False),
        sa.Column("critical_source_diagnostics", JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_production_routing_decisions_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_artifact_id"],
            ["production_artifacts.id"],
            name="fk_production_routing_decisions_extraction_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["relevance_artifact_id"],
            ["production_artifacts.id"],
            name="fk_production_routing_decisions_relevance_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_run_id",
            "pipeline_generation",
            "input_hash",
            name="uq_production_routing_run_generation_input",
        ),
        sa.CheckConstraint("pipeline_generation >= 0", name="ck_routing_generation"),
        sa.CheckConstraint("status IN ('decided', 'needs_review')", name="ck_routing_status"),
        sa.CheckConstraint(
            "publication_kind IS NULL OR publication_kind IN ('article', 'brief')",
            name="ck_routing_publication_kind",
        ),
        sa.CheckConstraint(
            "(status = 'decided' AND publication_kind IS NOT NULL) OR "
            "(status = 'needs_review' AND publication_kind IS NULL)",
            name="ck_routing_status_kind",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND decision_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_hashes",
        ),
        sa.CheckConstraint(
            "extraction_artifact_hash IS NULL OR extraction_artifact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_extraction_hash",
        ),
        sa.CheckConstraint(
            "relevance_artifact_hash IS NULL OR relevance_artifact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_relevance_hash",
        ),
        sa.CheckConstraint(
            "confirmed_ioc_count >= 0 AND contextual_ioc_count >= 0 AND ambiguous_ioc_count >= 0",
            name="ck_routing_ioc_counts",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(critical_source_diagnostics) = 'array'",
            name="ck_routing_diagnostics_array",
        ),
        sa.CheckConstraint("char_length(policy_version) > 0", name="ck_routing_policy_version"),
    )
    op.create_index(
        "ix_production_routing_decisions_run_created",
        _TABLE,
        ["production_run_id", "decided_at"],
        unique=False,
    )
    op.add_column(
        "production_runs",
        sa.Column(_POINTER_COLUMN, sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _POINTER_CONSTRAINT,
        "production_runs",
        _TABLE,
        [_POINTER_COLUMN],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OR DELETE ON {_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("LOCK TABLE production_runs, production_routing_decisions IN ACCESS EXCLUSIVE MODE")
    )
    decisions_exist = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM production_routing_decisions)")
    ).scalar_one()
    adopted = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM production_runs WHERE routing_decision_id IS NOT NULL)"
        )
    ).scalar_one()
    if decisions_exist or adopted:
        raise RuntimeError(
            "refusing to downgrade production routing while decision history is referenced"
        )

    op.execute(f"DROP TRIGGER {_TRIGGER} ON {_TABLE}")
    op.drop_constraint(_POINTER_CONSTRAINT, "production_runs", type_="foreignkey")
    op.drop_column("production_runs", _POINTER_COLUMN)
    op.drop_index("ix_production_routing_decisions_run_created", table_name=_TABLE)
    op.drop_table(_TABLE)
