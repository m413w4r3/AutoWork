"""Static invariants for the intentionally short Alembic chain."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CHAIN = (
    "0001_baseline",
    "0002_virustotal",
    "0003_analyst_workflow",
    "0004_sample_lifecycle",
    "0005_sample_acquisition",
    "0006_static_analysis",
    "0007_goodware_baselines",
    "0008_reference_corpus",
    "0009_capability_sets",
    "0010_code_features",
    "0011_invariant_registry",
    "0012_goodware_index_artifacts",
    "0013_production_batch",
    "0014_publication_review",
)


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
    assert {
        script.revision: script.down_revision
        for script in revisions
    } == {
        "0001_baseline": None,
        "0002_virustotal": "0001_baseline",
        "0003_analyst_workflow": "0002_virustotal",
        "0004_sample_lifecycle": "0003_analyst_workflow",
        "0005_sample_acquisition": "0004_sample_lifecycle",
        "0006_static_analysis": "0005_sample_acquisition",
        "0007_goodware_baselines": "0006_static_analysis",
        "0008_reference_corpus": "0007_goodware_baselines",
        "0009_capability_sets": "0008_reference_corpus",
        "0010_code_features": "0009_capability_sets",
        "0011_invariant_registry": "0010_code_features",
        "0012_goodware_index_artifacts": "0011_invariant_registry",
        "0013_production_batch": "0012_goodware_index_artifacts",
        "0014_publication_review": "0013_production_batch",
    }
