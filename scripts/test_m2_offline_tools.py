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


    def test_yargen_empty_imphash_sentinel_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "stage"
            sources.mkdir()

            with gzip.open(sources / "good-imphashes-part1.db", "wb") as handle:
                handle.write(
                    json.dumps(
                        {
                            "": 1358,
                            "ABCDEF0123456789ABCDEF0123456789": 2,
                        }
                    ).encode()
                )

            manifest = goodware.build_stage(sources, output)
            verified = goodware.verify_stage(output)

            self.assertEqual(manifest, verified)

            records = [
                json.loads(line)
                for line in (output / "records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(
                records,
                [
                    {
                        "feature_kind": "imphash",
                        "normalized_value": "abcdef0123456789abcdef0123456789",
                        "occurrence_count": 2,
                    }
                ],
            )

            # Aggregate feature statistics exclude the empty sentinel.
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["occurrence_sum"], 2)

            # Source statistics still describe the original yarGen shard.
            self.assertEqual(manifest["sources"][0]["entry_count"], 2)
            self.assertEqual(manifest["sources"][0]["occurrence_sum"], 1360)

    def test_yargen_empty_string_sentinel_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "stage"
            sources.mkdir()

            with gzip.open(sources / "good-strings-part1.db", "wb") as handle:
                handle.write(
                    json.dumps(
                        {
                            "": 1358,
                            "legitimate-goodware-string": 2,
                        }
                    ).encode()
                )

            manifest = goodware.build_stage(sources, output)
            verified = goodware.verify_stage(output)

            self.assertEqual(manifest, verified)

            records = [
                json.loads(line)
                for line in (output / "records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(
                records,
                [
                    {
                        "feature_kind": "string",
                        "normalized_value": "legitimate-goodware-string",
                        "occurrence_count": 2,
                    }
                ],
            )

            # The empty sentinel is not a usable baseline feature.
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["occurrence_sum"], 2)

            # But source metadata still describes the original yarGen shard.
            self.assertEqual(manifest["sources"][0]["entry_count"], 2)
            self.assertEqual(manifest["sources"][0]["occurrence_sum"], 1360)

    def test_nonempty_invalid_imphash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            sources.mkdir()

            with gzip.open(sources / "good-imphashes-part1.db", "wb") as handle:
                handle.write(json.dumps({"not-an-imphash": 1}).encode())

            with self.assertRaises(goodware.GoodwareImportError):
                goodware.build_stage(sources, root / "stage")



    def test_verify_does_not_reapply_lossy_yargen_string_unescaping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "stage"
            sources.mkdir()

            # Four literal backslashes in the yarGen source become two in the
            # normalized stage. Re-running yarGen unescaping during verify
            # would incorrectly collapse those two to one.
            source_value = "prefix" + ("\\" * 4) + "suffix"
            expected_value = "prefix" + ("\\" * 2) + "suffix"

            with gzip.open(sources / "good-strings-part1.db", "wb") as handle:
                handle.write(json.dumps({source_value: 3}).encode())

            manifest = goodware.build_stage(sources, output)
            verified = goodware.verify_stage(output)

            self.assertEqual(manifest, verified)

            records = [
                json.loads(line)
                for line in (output / "records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["feature_kind"], "string")
            self.assertEqual(records[0]["normalized_value"], expected_value)
            self.assertEqual(records[0]["occurrence_count"], 3)



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
