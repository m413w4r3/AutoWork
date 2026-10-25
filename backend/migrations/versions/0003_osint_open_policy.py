"""Add durable OSINT_OPEN policy versions and investigation pins."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision: str = "0003_osint_open_policy"
down_revision: str | None = "0002_c2_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENVELOPE_COLUMNS = {
    "authority_kind": ("varchar(32)", 32),
    "inherited_policy_id": ("uuid", None),
    "inherited_policy_version": ("bigint", None),
    "inherited_policy_event_id": ("uuid", None),
}
_ENVELOPE_CHECK = "ck_investigation_envelopes_authority_lineage"
_POLICY_EVENT_FUNCTION = "reject_osint_open_policy_event_mutation"
_POLICY_EVENT_TRIGGER = "trg_osint_open_policy_events_append_only"
_POLICY_VERSION_FUNCTION = "reject_osint_open_policy_version_mutation"
_POLICY_VERSION_TRIGGER = "trg_osint_open_policies_immutable"


def _ensure_envelope_lineage_columns() -> None:
    bind = op.get_bind()
    columns = {
        str(item["name"]): item for item in inspect(bind).get_columns("investigation_envelopes")
    }
    present = set(_ENVELOPE_COLUMNS).intersection(columns)
    if present and present != set(_ENVELOPE_COLUMNS):
        raise RuntimeError("Partial OSINT_OPEN envelope lineage columns; refusing migration")
    if not present:
        op.add_column("investigation_envelopes", _column("authority_kind"))
        op.add_column("investigation_envelopes", _column("inherited_policy_id"))
        op.add_column("investigation_envelopes", _column("inherited_policy_version"))
        op.add_column("investigation_envelopes", _column("inherited_policy_event_id"))
    else:
        for name, (expected_type, expected_length) in _ENVELOPE_COLUMNS.items():
            item = columns[name]
            actual_type = str(item["type"].compile(dialect=bind.dialect)).lower()
            if (
                actual_type != expected_type
                or item.get("nullable") is not True
                or (
                    expected_length is not None
                    and getattr(item["type"], "length", None) != expected_length
                )
            ):
                raise RuntimeError(f"Unexpected shape for investigation_envelopes.{name}")
    constraint_names = {
        str(item["name"]) for item in inspect(bind).get_check_constraints("investigation_envelopes")
    }
    if _ENVELOPE_CHECK not in constraint_names:
        op.create_check_constraint(
            _ENVELOPE_CHECK,
            "investigation_envelopes",
            "(authority_kind IS NULL AND inherited_policy_id IS NULL "
            "AND inherited_policy_version IS NULL AND inherited_policy_event_id IS NULL) OR "
            "(authority_kind = 'human_g2' AND inherited_policy_id IS NULL "
            "AND inherited_policy_version IS NULL AND inherited_policy_event_id IS NULL) OR "
            "(authority_kind = 'osint_policy' AND inherited_policy_id IS NOT NULL "
            "AND inherited_policy_version IS NOT NULL AND inherited_policy_event_id IS NOT NULL "
            "AND approval_decision_id = inherited_policy_event_id)",
        )


def _column(name: str) -> Any:
    from sqlalchemy import BigInteger, Column, String
    from sqlalchemy.dialects.postgresql import UUID

    column_types = {
        "authority_kind": String(32),
        "inherited_policy_id": UUID(as_uuid=True),
        "inherited_policy_version": BigInteger(),
        "inherited_policy_event_id": UUID(as_uuid=True),
    }
    return Column(name, column_types[name], nullable=True)


def upgrade() -> None:
    _create_policy_tables()
    _ensure_envelope_lineage_columns()
    bind = op.get_bind()
    fks = {str(item["name"]) for item in inspect(bind).get_foreign_keys("investigation_envelopes")}
    if "fk_investigation_envelopes_inherited_policy" not in fks:
        op.create_foreign_key(
            "fk_investigation_envelopes_inherited_policy",
            "investigation_envelopes",
            "osint_open_policies",
            ["inherited_policy_id", "inherited_policy_version"],
            ["policy_id", "version"],
            ondelete="RESTRICT",
        )
    if "fk_investigation_envelopes_inherited_event" not in fks:
        op.create_foreign_key(
            "fk_investigation_envelopes_inherited_event",
            "investigation_envelopes",
            "osint_open_policy_events",
            [
                "inherited_policy_id",
                "inherited_policy_version",
                "inherited_policy_event_id",
            ],
            ["policy_id", "policy_version", "id"],
            ondelete="RESTRICT",
        )
    op.execute(
        f"""
        CREATE FUNCTION {_POLICY_EVENT_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'osint_open_policy_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"CREATE TRIGGER {_POLICY_EVENT_TRIGGER} BEFORE UPDATE OR DELETE "
        "ON osint_open_policy_events FOR EACH ROW "
        f"EXECUTE FUNCTION {_POLICY_EVENT_FUNCTION}()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {_POLICY_VERSION_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'osint_open_policies is immutable by version'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"CREATE TRIGGER {_POLICY_VERSION_TRIGGER} BEFORE UPDATE OR DELETE "
        "ON osint_open_policies FOR EACH ROW "
        f"EXECUTE FUNCTION {_POLICY_VERSION_FUNCTION}()"
    )


def _create_policy_tables() -> None:
    from sqlalchemy import (
        BigInteger,
        Boolean,
        CheckConstraint,
        Column,
        DateTime,
        ForeignKey,
        ForeignKeyConstraint,
        String,
        UniqueConstraint,
    )
    from sqlalchemy.dialects.postgresql import JSONB, UUID

    op.create_table(
        "osint_open_policies",
        Column("policy_id", UUID(as_uuid=True), primary_key=True),
        Column("version", BigInteger, primary_key=True),
        Column("scope_kind", String(32), nullable=False),
        Column("scope_ref", String(4096), nullable=False),
        Column("mode", String(32), nullable=False),
        Column("status", String(16), nullable=False),
        Column("allowed_providers", JSONB, nullable=False),
        Column("budget_limits", JSONB, nullable=False),
        Column("acquire_hits_allowed", Boolean, nullable=False),
        Column("disclosure_tlp", String(16), nullable=False),
        Column("disclosure_do_not_submit", Boolean, nullable=False),
        Column("disclosure_external_llm_allowed", Boolean, nullable=False),
        Column("max_effect_claims_per_investigation", BigInteger, nullable=False),
        Column("actor_id", String(255), nullable=False),
        Column("reason", String(2000), nullable=False),
        Column("correlation_id", String(255), nullable=False),
        Column("activation_correlation_id", String(255), nullable=False),
        Column("previous_version", BigInteger),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "scope_kind", "scope_ref", "version", name="uq_osint_open_policies_scope_version"
        ),
        CheckConstraint("version >= 1", name="ck_osint_open_policies_version"),
        CheckConstraint(
            "previous_version IS NULL OR previous_version = version - 1",
            name="ck_osint_open_policies_previous_version",
        ),
        CheckConstraint(
            "scope_kind IN ('instance', 'subject_set')", name="ck_osint_open_policies_scope_kind"
        ),
        CheckConstraint("mode = 'osint_open'", name="ck_osint_open_policies_mode"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_osint_open_policies_status"),
        CheckConstraint(
            "jsonb_typeof(allowed_providers) = 'array'",
            name="ck_osint_open_policies_providers_array",
        ),
        CheckConstraint(
            "jsonb_typeof(budget_limits) = 'object'", name="ck_osint_open_policies_budgets_object"
        ),
        CheckConstraint(
            "disclosure_tlp IN ('white', 'clear', 'green', 'amber', 'red')",
            name="ck_osint_open_policies_tlp",
        ),
        CheckConstraint(
            "max_effect_claims_per_investigation >= 1", name="ck_osint_open_policies_effect_claims"
        ),
        CheckConstraint(
            "correlation_id <> '' AND activation_correlation_id <> ''",
            name="ck_osint_open_policies_correlations",
        ),
        ForeignKeyConstraint(
            ["policy_id", "previous_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_policies_previous",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "osint_open_policy_events",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("policy_id", UUID(as_uuid=True), nullable=False),
        Column("policy_version", BigInteger, nullable=False),
        Column("event_type", String(16), nullable=False),
        Column("actor_id", String(255), nullable=False),
        Column("reason", String(2000), nullable=False),
        Column("correlation_id", String(255), nullable=False),
        Column("payload_sha256", String(64), nullable=False),
        Column("previous_version", BigInteger),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("correlation_id", name="uq_osint_open_policy_events_correlation"),
        UniqueConstraint("policy_id", "policy_version", name="uq_osint_open_policy_events_version"),
        UniqueConstraint(
            "policy_id",
            "policy_version",
            "id",
            name="uq_osint_open_policy_events_version_id",
        ),
        CheckConstraint(
            "event_type IN ('activated', 'extended', 'revoked')",
            name="ck_osint_open_policy_events_type",
        ),
        CheckConstraint(
            "previous_version IS NULL OR previous_version >= 1",
            name="ck_osint_open_policy_events_previous_version",
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_osint_open_policy_events_payload_sha256"
        ),
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_policy_events_policy_version",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "osint_open_inheritances",
        Column(
            "investigation_id",
            UUID(as_uuid=True),
            ForeignKey(
                "analyst_investigations.id",
                name="fk_osint_open_inheritances_investigation",
                ondelete="RESTRICT",
            ),
            primary_key=True,
        ),
        Column(
            "subject_id",
            UUID(as_uuid=True),
            ForeignKey(
                "subjects.id", name="fk_osint_open_inheritances_subject", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        Column("policy_id", UUID(as_uuid=True), nullable=False),
        Column("policy_version", BigInteger, nullable=False),
        Column("policy_event_id", UUID(as_uuid=True), nullable=False),
        Column("correlation_id", String(255), nullable=False),
        Column("activation_correlation_id", String(255), nullable=False),
        Column("status", String(16), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("policy_version >= 1", name="ck_osint_open_inheritances_policy_version"),
        CheckConstraint(
            "status IN ('active', 'revoked', 'superseded')",
            name="ck_osint_open_inheritances_status",
        ),
        UniqueConstraint("correlation_id", name="uq_osint_open_inheritances_correlation"),
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["osint_open_policies.policy_id", "osint_open_policies.version"],
            name="fk_osint_open_inheritances_policy_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_id", "policy_version", "policy_event_id"],
            [
                "osint_open_policy_events.policy_id",
                "osint_open_policy_events.policy_version",
                "osint_open_policy_events.id",
            ],
            name="fk_osint_open_inheritances_policy_event",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("osint_open_inheritances", "osint_open_policy_events", "osint_open_policies"):
        if bind.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{table}")')).scalar_one():
            raise RuntimeError("Cannot downgrade populated OSINT_OPEN policy storage")
    if bind.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM investigation_envelopes WHERE "
            "authority_kind = 'osint_policy' OR inherited_policy_id IS NOT NULL OR "
            "inherited_policy_version IS NOT NULL OR inherited_policy_event_id IS NOT NULL)"
        )
    ).scalar_one():
        raise RuntimeError("Cannot downgrade while envelope authority lineage is in use")
    op.execute(f"DROP TRIGGER IF EXISTS {_POLICY_EVENT_TRIGGER} ON osint_open_policy_events")
    op.execute(f"DROP FUNCTION IF EXISTS {_POLICY_EVENT_FUNCTION}()")
    op.execute(f"DROP TRIGGER IF EXISTS {_POLICY_VERSION_TRIGGER} ON osint_open_policies")
    op.execute(f"DROP FUNCTION IF EXISTS {_POLICY_VERSION_FUNCTION}()")
    op.drop_constraint(
        "fk_investigation_envelopes_inherited_event", "investigation_envelopes", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_investigation_envelopes_inherited_policy", "investigation_envelopes", type_="foreignkey"
    )
    op.drop_constraint(_ENVELOPE_CHECK, "investigation_envelopes", type_="check")
    op.drop_column("investigation_envelopes", "inherited_policy_event_id")
    op.drop_column("investigation_envelopes", "inherited_policy_version")
    op.drop_column("investigation_envelopes", "inherited_policy_id")
    op.drop_column("investigation_envelopes", "authority_kind")
    op.drop_table("osint_open_inheritances")
    op.drop_table("osint_open_policy_events")
    op.drop_table("osint_open_policies")
