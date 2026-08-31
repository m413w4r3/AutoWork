"""Static invariants for the single fresh-schema Alembic baseline."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CHAIN = ("0001_baseline",)


def test_alembic_chain_has_one_short_head_and_exact_revisions() -> None:
    config = Config(BACKEND_ROOT / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    revisions = list(scripts.walk_revisions())
    revision_ids = [script.revision for script in revisions]

    assert scripts.get_heads() == [EXPECTED_CHAIN[-1]]
    assert all(revision_id for revision_id in revision_ids)
    assert all(len(revision_id) <= 32 for revision_id in revision_ids)
    assert len(revision_ids) == len(set(revision_ids)), "duplicate Alembic revision IDs"
    assert set(revision_ids) == set(EXPECTED_CHAIN)
    assert {script.revision: script.down_revision for script in revisions} == {
        "0001_baseline": None,
    }
