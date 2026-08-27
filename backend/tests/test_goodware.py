import hashlib
import json
from pathlib import Path

import pytest

from cti_app.domain.goodware import Banality, BanalityScorer, BanalityThresholds
from cti_app.infrastructure.goodware_stage import GoodwareStageError, load_stage


def _stage(tmp_path: Path, records: bytes, source: bytes = b"database") -> tuple[Path, Path]:
    source_dir, stage_dir = tmp_path / "sources", tmp_path / "stage"
    source_dir.mkdir()
    stage_dir.mkdir()
    (source_dir / "good-strings.db").write_bytes(source)
    manifest = {
        "schema_version": "autowork-goodware-stage-v1",
        "source_format": "yargen-gzip-json-counter-v1",
        "source_set_sha256": hashlib.sha256(
            json.dumps(
                [
                    {
                        "filename": "good-strings.db",
                        "feature_kind": "string",
                        "sha256": hashlib.sha256(source).hexdigest(),
                        "size": len(source),
                    }
                ],
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "records_sha256": hashlib.sha256(records).hexdigest(),
        "record_count": 1,
        "occurrence_sum": 2,
        "sources": [
            {
                "filename": "good-strings.db",
                "feature_kind": "string",
                "sha256": hashlib.sha256(source).hexdigest(),
                "size": len(source),
            }
        ],
    }
    (stage_dir / "records.jsonl").write_bytes(records)
    (stage_dir / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
    )
    return stage_dir, source_dir


def test_stage_validates_and_streams_records(tmp_path: Path) -> None:
    records = b'{"feature_kind":"string","normalized_value":"hello","occurrence_count":2}\n'
    stage, source = _stage(tmp_path, records)
    loaded = load_stage(stage, source)
    assert next(iter(loaded.iter_features())).normalized_value == "hello"


def test_stage_rejects_path_traversal(tmp_path: Path) -> None:
    stage, source = _stage(
        tmp_path, b'{"feature_kind":"string","normalized_value":"hello","occurrence_count":2}\n'
    )
    manifest = json.loads((stage / "manifest.json").read_text())
    manifest["sources"][0]["filename"] = "../good-strings.db"
    (stage / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(GoodwareStageError):
        load_stage(stage, source)


def test_banality_thresholds_and_lookup_buckets() -> None:
    scorer = BanalityScorer(BanalityThresholds(suspicious_count=3, banal_count=5))
    assert scorer.score(None) is Banality.UNKNOWN
    assert scorer.score(1) is Banality.SPECIFIC
    assert scorer.score(3) is Banality.SUSPICIOUS_COMMON
    assert scorer.score(5) is Banality.BANAL


def test_thresholds_are_ordered() -> None:
    with pytest.raises(ValueError):
        BanalityThresholds(suspicious_count=0, banal_count=1)
