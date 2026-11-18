"""Add immutable editorial workspace pin and block registry snapshots.

Revision ID: 0011_editorial_workspace_snapshots
Revises: 0010_acquisition_provenance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_editorial_workspace_snapshots"
down_revision: str | None = "0010_acquisition_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "editorial_workspace_snapshots"
_FUNCTION = "reject_editorial_workspace_snapshot_mutation"
_TRIGGER = "trg_editorial_workspace_snapshots_append_only"
_TRUNCATE_TRIGGER = "trg_editorial_workspace_snapshots_no_truncate"


def _assert_absent_preflight() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if _TABLE in tables:
        raise RuntimeError("refusing to create a pre-existing editorial workspace table")
    required = {
        "subjects",
        "production_runs",
        "production_artifacts",
        "production_routing_decisions",
    }
    missing = required - tables
    if missing:
        raise RuntimeError(
            "refusing editorial workspace migration with missing parent tables: "
            + ", ".join(sorted(missing))
        )
    function_exists = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = :name)"),
        {"name": _FUNCTION},
    ).scalar_one()
    trigger_exists = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger "
            "WHERE tgname IN (:row_trigger, :truncate_trigger))"
        ),
        {"row_trigger": _TRIGGER, "truncate_trigger": _TRUNCATE_TRIGGER},
    ).scalar_one()
    if function_exists or trigger_exists:
        raise RuntimeError("refusing to create partial editorial workspace guard objects")


def upgrade() -> None:
    _assert_absent_preflight()
    op.create_table(
        _TABLE,
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("synthesis_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("synthesis_version", sa.Integer(), nullable=False),
        sa.Column("synthesis_input_hash", sa.String(64), nullable=False),
        sa.Column("synthesis_canonical_sha256", sa.String(64), nullable=False),
        sa.Column("enrichment_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("enrichment_version", sa.Integer(), nullable=True),
        sa.Column("enrichment_input_hash", sa.String(64), nullable=True),
        sa.Column("enrichment_canonical_sha256", sa.String(64), nullable=True),
        sa.Column("publication_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("publication_version", sa.Integer(), nullable=True),
        sa.Column("publication_input_hash", sa.String(64), nullable=True),
        sa.Column("publication_canonical_sha256", sa.String(64), nullable=True),
        sa.Column("pipeline_generation", sa.Integer(), nullable=False),
        sa.Column("routing_decision_id", sa.Uuid(), nullable=True),
        sa.Column("routing_decision_hash", sa.String(64), nullable=True),
        sa.Column("publication_kind", sa.String(16), nullable=True),
        sa.Column("workspace_pin_hash", sa.String(64), nullable=False),
        sa.Column("registry_hash", sa.String(64), nullable=False),
        sa.Column("block_registry", JSONB(), nullable=False),
        sa.Column("retired_block_ids", JSONB(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name="pk_editorial_workspace_snapshots"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_editorial_workspace_snapshots_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["production_runs.id"],
            name="fk_editorial_workspace_snapshots_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["synthesis_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_workspace_snapshots_synthesis_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enrichment_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_workspace_snapshots_enrichment_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_artifact_id"],
            ["production_artifacts.id"],
            name="fk_editorial_workspace_snapshots_publication_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routing_decision_id"],
            ["production_routing_decisions.id"],
            name="fk_editorial_workspace_snapshots_routing_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_snapshot_id"],
            [f"{_TABLE}.snapshot_id"],
            name="fk_editorial_workspace_snapshots_parent",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "production_run_id",
            "workspace_pin_hash",
            name="uq_editorial_workspace_snapshots_run_pin",
        ),
        sa.CheckConstraint(
            "synthesis_version >= 1 AND char_length(synthesis_input_hash) = 64 AND "
            "synthesis_input_hash ~ '^[0-9a-f]{64}$' AND "
            "char_length(synthesis_canonical_sha256) = 64 AND "
            "synthesis_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_synthesis_hashes",
        ),
        sa.CheckConstraint(
            "(enrichment_artifact_id IS NULL AND enrichment_version IS NULL AND "
            "enrichment_input_hash IS NULL AND enrichment_canonical_sha256 IS NULL) OR "
            "(enrichment_artifact_id IS NOT NULL AND enrichment_version >= 1 AND "
            "enrichment_input_hash IS NOT NULL AND char_length(enrichment_input_hash) = 64 AND "
            "enrichment_input_hash ~ '^[0-9a-f]{64}$' AND "
            "enrichment_canonical_sha256 IS NOT NULL AND "
            "char_length(enrichment_canonical_sha256) = 64 AND "
            "enrichment_canonical_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_enrichment_pin",
        ),
        sa.CheckConstraint(
            "(publication_artifact_id IS NULL AND publication_version IS NULL AND "
            "publication_input_hash IS NULL AND publication_canonical_sha256 IS NULL) OR "
            "(publication_artifact_id IS NOT NULL AND publication_version >= 1 AND "
            "publication_input_hash IS NOT NULL AND char_length(publication_input_hash) = 64 AND "
            "publication_input_hash ~ '^[0-9a-f]{64}$' AND "
            "publication_canonical_sha256 IS NOT NULL AND "
            "char_length(publication_canonical_sha256) = 64 AND "
            "publication_canonical_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_publication_pin",
        ),
        sa.CheckConstraint(
            "routing_decision_hash IS NULL OR "
            "(char_length(routing_decision_hash) = 64 AND "
            "routing_decision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_routing_pin",
        ),
        sa.CheckConstraint(
            "publication_kind IS NULL OR publication_kind IN ('article', 'brief')",
            name="ck_editorial_workspace_snapshots_publication_kind",
        ),
        sa.CheckConstraint(
            "char_length(workspace_pin_hash) = 64 AND workspace_pin_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_pin_hash",
        ),
        sa.CheckConstraint(
            "char_length(registry_hash) = 64 AND registry_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_registry_hash",
        ),
        sa.CheckConstraint(
            "pipeline_generation >= 0",
            name="ck_editorial_workspace_snapshots_pipeline_generation",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(block_registry) = 'array'",
            name="ck_editorial_workspace_snapshots_registry_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(retired_block_ids) = 'array'",
            name="ck_editorial_workspace_snapshots_retired_array",
        ),
    )
    op.create_index(
        "ix_editorial_workspace_snapshots_subject",
        _TABLE,
        ["subject_id", "created_at"],
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'editorial workspace snapshots are append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE UPDATE OR DELETE ON {_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}();
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        raise RuntimeError("refusing to downgrade a missing editorial workspace table")
    guard_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM pg_trigger t "
            "WHERE t.tgrelid=to_regclass(:table) "
            "AND t.tgname IN (:row_trigger, :truncate_trigger) AND NOT t.tgisinternal"
        ),
        {
            "row_trigger": _TRIGGER,
            "truncate_trigger": _TRUNCATE_TRIGGER,
            "table": _TABLE,
        },
    ).scalar_one()
    function_exists = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname=:function)"),
        {"function": _FUNCTION},
    ).scalar_one()
    if guard_count != 2 or not function_exists:
        raise RuntimeError("refusing to downgrade a partial editorial workspace guard")
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    has_rows = bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")).scalar_one()
    if has_rows:
        raise RuntimeError("refusing to downgrade while editorial workspace snapshots exist")
    op.drop_index("ix_editorial_workspace_snapshots_subject", table_name=_TABLE)
    op.execute(f"DROP TRIGGER {_TRIGGER} ON {_TABLE}")
    op.execute(f"DROP TRIGGER {_TRUNCATE_TRIGGER} ON {_TABLE}")
    op.drop_table(_TABLE)
    op.execute(f"DROP FUNCTION {_FUNCTION}()")
