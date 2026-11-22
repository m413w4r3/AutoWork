"""Static invariants for the Alembic migration chain."""

import importlib
import re
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CHAIN = (
    "0001_baseline",
    "0002_c2_experiments",
    "0003_osint_open_policy",
    "0004_external_effect_claims",
    "0005_sample_acquisition_dispatch",
    "0006_tool_seconds_accounting",
    "0007_vt_retry_trust",
    "0008_production_routing",
    "0008b_version_length",
    "0009_acquisition_strategy_attempts",
    "0010_acquisition_provenance",
    "0011_editorial_workspace_snapshots",
    "0012_editorial_threads",
    "0013_bridge_generation_slots",
    "0014_editorial_proposals",
    "0015_editorial_proposal_impacts",
    "0016_editorial_drafts",
    "0017_editorial_proposal_bar_chart_operation",
    "0018_editorial_enrichment_element_operations",
    "0019_workspace_bindings",
    "0020_investigation_plans",
)

BAR_CHART_MIGRATION = importlib.import_module(
    "migrations.versions.0017_editorial_proposal_bar_chart_operation"
)
ENRICHMENT_ELEMENT_MIGRATION = importlib.import_module(
    "migrations.versions.0018_editorial_enrichment_element_operations"
)


def _operation_check_state_for(
    sqltext: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    migration: ModuleType = BAR_CHART_MIGRATION,
) -> str:
    class _Inspector:
        def has_table(self, table_name: str) -> bool:
            return table_name == "editorial_proposals"

        def get_check_constraints(self, table_name: str) -> list[dict[str, str]]:
            assert table_name == "editorial_proposals"
            return [
                {
                    "name": "ck_editorial_proposals_operation",
                    "sqltext": sqltext,
                }
            ]

    inspector = _Inspector()
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    return cast(
        str,
        migration._operation_check_state(cast(Connection, object())),
    )


def test_editorial_operation_preflight_parses_postgresql_and_in_check_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_values = re.findall(r"'([a-z_]+)'", BAR_CHART_MIGRATION._OLD_OPERATION_VALUES)
    new_values = re.findall(r"'([a-z_]+)'", BAR_CHART_MIGRATION._NEW_OPERATION_VALUES)
    postgres_values = ", ".join(f"'{value}'::character varying" for value in new_values)
    postgres_check = f"((operation)::text = ANY ((ARRAY[{postgres_values}])::text[]))"

    parsed_old = BAR_CHART_MIGRATION._parse_operation_check_values(
        f"operation IN ({BAR_CHART_MIGRATION._OLD_OPERATION_VALUES})"
    )
    parsed_postgres = BAR_CHART_MIGRATION._parse_operation_check_values(postgres_check)

    assert parsed_old is not None
    assert set(parsed_old) == set(old_values)
    assert len(parsed_old) == len(set(parsed_old)) == 24
    assert parsed_postgres is not None
    assert set(parsed_postgres) == set(new_values)
    assert len(parsed_postgres) == len(set(parsed_postgres)) == 25
    assert (
        _operation_check_state_for(
            f"operation IN ({BAR_CHART_MIGRATION._OLD_OPERATION_VALUES})", monkeypatch
        )
        == "old"
    )
    assert _operation_check_state_for(postgres_check, monkeypatch) == "new"


@pytest.mark.parametrize(
    ("sqltext", "message"),
    (
        ("operation <> 'rewrite_title'", "unexpected definition"),
        ("other_column IN ('rewrite_title')", "expected only"),
        (
            "operation IN ('rewrite_title', 'rewrite_lead') OR TRUE",
            "unexpected definition",
        ),
        (
            "operation IN ('rewrite_title', 'unexpected_operation')",
            "unexpected definition",
        ),
        (
            "operation IN ('rewrite_title', 'rewrite_title')",
            "duplicate operation values",
        ),
    ),
)
def test_editorial_operation_preflight_rejects_noncanonical_checks(
    sqltext: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _operation_check_state_for(sqltext, monkeypatch)


def test_enrichment_element_operation_preflight_extends_bar_chart_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar_chart_values = re.findall(r"'([a-z_]+)'", BAR_CHART_MIGRATION._NEW_OPERATION_VALUES)
    old_values = re.findall(r"'([a-z_]+)'", ENRICHMENT_ELEMENT_MIGRATION._OLD_OPERATION_VALUES)
    new_values = re.findall(r"'([a-z_]+)'", ENRICHMENT_ELEMENT_MIGRATION._NEW_OPERATION_VALUES)

    assert set(old_values) == set(bar_chart_values)
    assert len(old_values) == len(set(old_values)) == 25
    assert len(new_values) == len(set(new_values)) == 28
    assert set(new_values) - set(old_values) == {
        "update_visual_caption",
        "move_enrichment_element",
        "reorder_enrichment_elements",
    }

    postgres_values = ", ".join(f"'{value}'::character varying" for value in new_values)
    postgres_check = f"((operation)::text = ANY ((ARRAY[{postgres_values}])::text[]))"
    parsed_old = ENRICHMENT_ELEMENT_MIGRATION._parse_operation_check_values(
        f"operation IN ({ENRICHMENT_ELEMENT_MIGRATION._OLD_OPERATION_VALUES})"
    )
    parsed_postgres = ENRICHMENT_ELEMENT_MIGRATION._parse_operation_check_values(postgres_check)

    assert parsed_old is not None
    assert set(parsed_old) == set(old_values)
    assert parsed_postgres is not None
    assert set(parsed_postgres) == set(new_values)
    assert (
        _operation_check_state_for(
            f"operation IN ({ENRICHMENT_ELEMENT_MIGRATION._OLD_OPERATION_VALUES})",
            monkeypatch,
            migration=ENRICHMENT_ELEMENT_MIGRATION,
        )
        == "old"
    )
    assert (
        _operation_check_state_for(
            postgres_check, monkeypatch, migration=ENRICHMENT_ELEMENT_MIGRATION
        )
        == "new"
    )
    with pytest.raises(RuntimeError, match="unexpected definition"):
        _operation_check_state_for(
            "operation IN ('rewrite_title', 'update_visual_caption')",
            monkeypatch,
            migration=ENRICHMENT_ELEMENT_MIGRATION,
        )


def test_alembic_chain_has_one_short_head_and_exact_revisions() -> None:
    config = Config(BACKEND_ROOT / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())
    revision_ids = [script.revision for script in revisions]

    assert scripts.get_heads() == [scripts.get_current_head()]
    assert scripts.get_current_head() == "0020_investigation_plans"
    assert all(revision_id for revision_id in revision_ids)
    assert {revision_id for revision_id in revision_ids if len(revision_id) > 32} == {
        "0009_acquisition_strategy_attempts",
        "0011_editorial_workspace_snapshots",
        "0017_editorial_proposal_bar_chart_operation",
        "0018_editorial_enrichment_element_operations",
    }
    assert len(revision_ids) == len(set(revision_ids)), "duplicate Alembic revision IDs"
    assert set(revision_ids) == set(EXPECTED_CHAIN)
    assert {script.revision: script.down_revision for script in revisions} == {
        "0001_baseline": None,
        "0002_c2_experiments": "0001_baseline",
        "0003_osint_open_policy": "0002_c2_experiments",
        "0004_external_effect_claims": "0003_osint_open_policy",
        "0005_sample_acquisition_dispatch": "0004_external_effect_claims",
        "0006_tool_seconds_accounting": "0005_sample_acquisition_dispatch",
        "0007_vt_retry_trust": "0006_tool_seconds_accounting",
        "0008_production_routing": "0007_vt_retry_trust",
        "0008b_version_length": "0008_production_routing",
        "0009_acquisition_strategy_attempts": "0008b_version_length",
        "0010_acquisition_provenance": "0009_acquisition_strategy_attempts",
        "0011_editorial_workspace_snapshots": "0010_acquisition_provenance",
        "0012_editorial_threads": "0011_editorial_workspace_snapshots",
        "0013_bridge_generation_slots": "0012_editorial_threads",
        "0014_editorial_proposals": "0013_bridge_generation_slots",
        "0015_editorial_proposal_impacts": "0014_editorial_proposals",
        "0016_editorial_drafts": "0015_editorial_proposal_impacts",
        "0017_editorial_proposal_bar_chart_operation": "0016_editorial_drafts",
        "0018_editorial_enrichment_element_operations": (
            "0017_editorial_proposal_bar_chart_operation"
        ),
        "0019_workspace_bindings": "0018_editorial_enrichment_element_operations",
        "0020_investigation_plans": "0019_workspace_bindings",
    }
