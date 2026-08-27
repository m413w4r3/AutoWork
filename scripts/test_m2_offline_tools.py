#!/usr/bin/env python3
from __future__ import annotations

import gzip
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


goodware = _load("import_goodware", "scripts/import_goodware.py")
compose_guard = _load("check_analysis_worker_config", "scripts/check_analysis_worker_config.py")
fixture_guard = _load("check_no_binary_fixtures", "scripts/check_no_binary_fixtures.py")


class GoodwareStageTests(unittest.TestCase):
    def test_build_is_aggregated_canonical_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "stage"
            sources.mkdir()
            with gzip.open(sources / "good-strings-part1.db", "wb") as handle:
                handle.write(json.dumps({"alpha": 2, r"path\\name": 1}).encode())
            with gzip.open(sources / "good-strings-part2.db", "wb") as handle:
                handle.write(json.dumps({"alpha": 3, "UTF16LE:wide": 4}).encode())
            with gzip.open(sources / "good-opcodes-part1.db", "wb") as handle:
                handle.write(json.dumps({"AABBCCDDEEFF0011": 2}).encode())
            with gzip.open(sources / "good-imphashes-part1.db", "wb") as handle:
                handle.write(json.dumps({"ABCDEF0123456789ABCDEF0123456789": 1}).encode())
            with gzip.open(sources / "good-exports-part1.db", "wb") as handle:
                handle.write(json.dumps({"CreateThing": 7}).encode())

            manifest = goodware.build_stage(sources, output)
            verified = goodware.verify_stage(output)
            self.assertEqual(manifest, verified)

            records = [
                json.loads(line)
                for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            alpha = next(item for item in records if item["normalized_value"] == "alpha")
            self.assertEqual(alpha["occurrence_count"], 5)
            self.assertTrue(
                any(item["normalized_value"] == r"path\name" for item in records)
            )
            self.assertTrue(
                any(item["normalized_value"] == "aabbccddeeff0011" for item in records)
            )
            self.assertTrue(
                any(
                    item["normalized_value"] == "abcdef0123456789abcdef0123456789"
                    for item in records
                )
            )

    def test_invalid_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            sources.mkdir()
            with gzip.open(sources / "good-strings-part1.db", "wb") as handle:
                handle.write(json.dumps({"alpha": 0}).encode())
            with self.assertRaises(goodware.GoodwareImportError):
                goodware.build_stage(sources, root / "stage")


class ComposeGuardTests(unittest.TestCase):
    def test_good_analysis_worker_passes(self) -> None:
        payload = {
            "services": {
                "analysis-worker": {
                    "environment": {
                        "APP_ENV": "development",
                        "POSTGRES_DSN": "postgresql://x",
                        "REDIS_URL": "redis://x",
                        "S3_ENDPOINT": "minio:9000",
                        "ANALYSIS_MAX_SAMPLE_BYTES": "1",
                    },
                    "networks": {"analysis-internal": None},
                    "volumes": [],
                }
            },
            "networks": {"analysis-internal": {"internal": True}},
        }
        self.assertEqual(compose_guard.validate_compose(payload), [])

    def test_secret_and_default_network_fail(self) -> None:
        payload = {
            "services": {
                "analysis-worker": {
                    "environment": {"OPENAI_BRIDGE_API_KEY": "secret"},
                    "networks": {"default": None},
                    "extra_hosts": ["host.docker.internal:host-gateway"],
                    "volumes": ["subject_workspaces:/work/subjects"],
                }
            },
            "networks": {"default": {}},
        }
        errors = compose_guard.validate_compose(payload)
        self.assertGreaterEqual(len(errors), 4)


class FixtureGuardTests(unittest.TestCase):
    def test_binary_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "fixture.py").write_text("DATA = b'generated'\n", encoding="utf-8")
            self.assertEqual(fixture_guard.find_binary_files(root), [])
            (root / "sample.bin").write_bytes(b"MZ\x00\x01")
            self.assertEqual(fixture_guard.find_binary_files(root), [root / "sample.bin"])

    def test_historical_documents_are_allowed_and_pyc_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "fixture.pdf").write_bytes(b"%PDF\x00")
            (root / "fixture.html").write_text("<html></html>", encoding="utf-8")
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"\x00\x01")
            self.assertEqual(fixture_guard.find_binary_files(root), [])


if __name__ == "__main__":
    unittest.main()
