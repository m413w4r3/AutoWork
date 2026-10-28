"""Static invariants for the Alembic migration chain."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CHAIN = (
    "0001_baseline",
    "0002_c2_experiments",
    "0003_osint_open_policy",
    "0004_external_effect_claims",
    "0005_sample_acquisition_dispatch",
    "0006_tool_seconds_accounting",
)


def test_alembic_chain_has_one_short_head_and_exact_revisions() -> None:
    config = Config(BACKEND_ROOT / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())
    revision_ids = [script.revision for script in revisions]

    assert scripts.get_heads() == ["0006_tool_seconds_accounting"]
    assert all(revision_id for revision_id in revision_ids)
    assert all(len(revision_id) <= 32 for revision_id in revision_ids)
    assert len(revision_ids) == len(set(revision_ids)), "duplicate Alembic revision IDs"
    assert set(revision_ids) == set(EXPECTED_CHAIN)
    assert {script.revision: script.down_revision for script in revisions} == {
        "0001_baseline": None,
        "0002_c2_experiments": "0001_baseline",
        "0003_osint_open_policy": "0002_c2_experiments",
        "0004_external_effect_claims": "0003_osint_open_policy",
        "0005_sample_acquisition_dispatch": "0004_external_effect_claims",
        "0006_tool_seconds_accounting": "0005_sample_acquisition_dispatch",
    }
