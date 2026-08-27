from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_invariant_registry"
down_revision = "0010_code_features"
branch_labels = None
depends_on = None


INVARIANT_TYPES_SQL = (
    "'literal_string', 'hex_pattern', 'code_ngram', 'opcode_sequence', 'import_name', "
    "'export_name', 'section_name', 'capability', 'similarity_hash', "
    "'structural_metadata', 'relation'"
)
INVARIANT_CATEGORIES_SQL = (
    "'c2_indicator', 'mutex_or_event', 'pdb_or_build_path', 'config_marker', "
    "'crypto_constant', 'custom_protocol', 'ransom_or_ui_text', 'code_sequence', "
    "'capability_pattern', 'similarity_key', 'library_noise', 'packer_artifact', "
    "'compiler_artifact', 'generic_winapi', 'unknown'"
)
INVARIANT_STATUSES_SQL = (
    "'proposed', 'approved_for_pivot', 'validated', 'rejected', 'unselective', 'shared_component'"
)
INVARIANT_PROVENANCE_KINDS_SQL = (
    "'sample_feature', 'code_feature', 'tool_output', 'capability', 'report_claim', 'analyst_manual'"
)
INVARIANT_REJECTION_CAUSES_SQL = (
    "'provenance_invalid', 'invalid_category', 'library_noise', 'packer_artifact', "
    "'compiler_artifact', 'generic_winapi', 'banal', 'multi_family', 'empty_pattern', "
    "'pattern_too_long', 'code_ngram_mask_ratio', 'code_ngram_contiguous_fixed_run'"
)


def upgrade() -> None:
    op.create_table(
        "candidate_invariants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("proposal_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("banality_verdict", sa.String(32), nullable=False),
        sa.Column("banality_occurrence_count", sa.BigInteger),
        sa.Column(
            "goodware_baseline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("goodware_baselines.id", ondelete="RESTRICT"),
        ),
        sa.Column("corpus_verdict", sa.String(32), nullable=False),
        sa.Column("corpus_malware_sample_count", sa.BigInteger),
        sa.Column("family_labels", postgresql.JSONB, nullable=False),
        sa.Column("benign_prevalence", sa.BigInteger),
        sa.Column("positive_support", sa.BigInteger),
        sa.Column("positive_sample_confirmed", sa.Boolean, nullable=False),
        sa.Column("masked_pattern", sa.Text),
        sa.Column("byte_count", sa.Integer),
        sa.Column("fixed_byte_count", sa.Integer),
        sa.Column("masked_byte_count", sa.Integer),
        sa.Column("longest_fixed_run", sa.Integer),
        sa.Column("likely_packed", sa.Boolean),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("proposal_key", name="uq_candidate_invariants_proposal_key"),
        sa.CheckConstraint(f"type IN ({INVARIANT_TYPES_SQL})", name="ck_candidate_invariants_type"),
        sa.CheckConstraint(
            f"category IN ({INVARIANT_CATEGORIES_SQL})", name="ck_candidate_invariants_category"
        ),
        sa.CheckConstraint(
            f"status IN ({INVARIANT_STATUSES_SQL})", name="ck_candidate_invariants_status"
        ),
        sa.CheckConstraint(
            "char_length(proposal_key) = 64 AND proposal_key ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_invariants_proposal_key",
        ),
        sa.CheckConstraint(
            "banality_occurrence_count IS NULL OR banality_occurrence_count > 0",
            name="ck_candidate_invariants_banality_count",
        ),
        sa.CheckConstraint(
            "benign_prevalence IS NULL OR benign_prevalence >= 0",
            name="ck_candidate_invariants_benign_prevalence",
        ),
        sa.CheckConstraint(
            "positive_support IS NULL OR positive_support >= 0",
            name="ck_candidate_invariants_positive_support",
        ),
    )
    op.create_index(
        "ix_candidate_invariants_investigation", "candidate_invariants", ["investigation_id"]
    )
    op.create_index("ix_candidate_invariants_status", "candidate_invariants", ["status"])
    op.create_index("ix_candidate_invariants_type", "candidate_invariants", ["type"])
    op.create_index("ix_candidate_invariants_category", "candidate_invariants", ["category"])

    op.create_table(
        "candidate_invariant_provenances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invariant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate_invariants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("sample_sha256", sa.String(64)),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"kind IN ({INVARIANT_PROVENANCE_KINDS_SQL})",
            name="ck_candidate_invariant_provenances_kind",
        ),
    )
    op.create_index(
        "ix_candidate_invariant_provenances_invariant",
        "candidate_invariant_provenances",
        ["invariant_id"],
    )
    op.create_index(
        "ix_candidate_invariant_provenances_sample_sha256",
        "candidate_invariant_provenances",
        ["sample_sha256"],
    )
    op.create_index(
        "ix_candidate_invariant_provenances_kind",
        "candidate_invariant_provenances",
        ["kind"],
    )

    op.create_table(
        "candidate_invariant_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invariant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate_invariants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.CheckConstraint(
            f"from_status IN ({INVARIANT_STATUSES_SQL})",
            name="ck_candidate_invariant_transitions_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({INVARIANT_STATUSES_SQL})",
            name="ck_candidate_invariant_transitions_to_status",
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 500", name="ck_candidate_invariant_transitions_reason"
        ),
    )
    op.create_index(
        "ix_candidate_invariant_transitions_invariant",
        "candidate_invariant_transitions",
        ["invariant_id", "occurred_at"],
    )
    op.create_index(
        "ix_candidate_invariant_transitions_status",
        "candidate_invariant_transitions",
        ["to_status"],
    )

    op.create_table(
        "invariant_rejections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cycle_number", sa.Integer),
        sa.Column("cause", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("proposal_key", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("proposal_key", name="uq_invariant_rejections_proposal_key"),
        sa.CheckConstraint(
            f"cause IN ({INVARIANT_REJECTION_CAUSES_SQL})",
            name="ck_invariant_rejections_cause",
        ),
        sa.CheckConstraint(
            "char_length(proposal_key) = 64 AND proposal_key ~ '^[0-9a-f]{64}$'",
            name="ck_invariant_rejections_proposal_key",
        ),
        sa.CheckConstraint("char_length(reason) <= 500", name="ck_invariant_rejections_reason"),
        sa.CheckConstraint("cycle_number IS NULL OR cycle_number >= 1", name="ck_invariant_rejections_cycle"),
    )
    op.create_index(
        "ix_invariant_rejections_investigation",
        "invariant_rejections",
        ["investigation_id", "cycle_number"],
    )
    op.create_index("ix_invariant_rejections_cause", "invariant_rejections", ["cause"])
    op.create_index("ix_invariant_rejections_type", "invariant_rejections", ["type"])
    op.create_index("ix_invariant_rejections_category", "invariant_rejections", ["category"])


def downgrade() -> None:
    op.drop_index("ix_invariant_rejections_category", table_name="invariant_rejections")
    op.drop_index("ix_invariant_rejections_type", table_name="invariant_rejections")
    op.drop_index("ix_invariant_rejections_cause", table_name="invariant_rejections")
    op.drop_index("ix_invariant_rejections_investigation", table_name="invariant_rejections")
    op.drop_table("invariant_rejections")
    op.drop_index(
        "ix_candidate_invariant_transitions_status", table_name="candidate_invariant_transitions"
    )
    op.drop_index(
        "ix_candidate_invariant_transitions_invariant",
        table_name="candidate_invariant_transitions",
    )
    op.drop_table("candidate_invariant_transitions")
    op.drop_index(
        "ix_candidate_invariant_provenances_kind", table_name="candidate_invariant_provenances"
    )
    op.drop_index(
        "ix_candidate_invariant_provenances_sample_sha256",
        table_name="candidate_invariant_provenances",
    )
    op.drop_index(
        "ix_candidate_invariant_provenances_invariant",
        table_name="candidate_invariant_provenances",
    )
    op.drop_table("candidate_invariant_provenances")
    op.drop_index("ix_candidate_invariants_category", table_name="candidate_invariants")
    op.drop_index("ix_candidate_invariants_type", table_name="candidate_invariants")
    op.drop_index("ix_candidate_invariants_status", table_name="candidate_invariants")
    op.drop_index("ix_candidate_invariants_investigation", table_name="candidate_invariants")
    op.drop_table("candidate_invariants")
