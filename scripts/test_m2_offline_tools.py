#!/usr/bin/env python3
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


goodware = _load("import_goodware", "scripts/import_goodware.py")
compose_guard = _load(
    "check_analysis_worker_config", "scripts/check_analysis_worker_config.py"
)
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
                handle.write(
                    json.dumps({"ABCDEF0123456789ABCDEF0123456789": 1}).encode()
                )
            with gzip.open(sources / "good-exports-part1.db", "wb") as handle:
                handle.write(json.dumps({"CreateThing": 7}).encode())

            manifest = goodware.build_stage(sources, output)
            verified = goodware.verify_stage(output)
            self.assertEqual(manifest, verified)

            records = [
                json.loads(line)
                for line in (output / "records.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            alpha = next(
                item for item in records if item["normalized_value"] == "alpha"
            )
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


class GoodwareIndexTests(unittest.TestCase):
    @staticmethod
    def _write_source(sources: Path, filename: str, values: dict[str, int]) -> None:
        with gzip.open(sources / filename, "wb") as handle:
            handle.write(json.dumps(values).encode())

    def test_streaming_parser_handles_scalar_chunk_boundaries(self) -> None:
        expected = {
            "alpha": 1000,
            "beta": 23,
            "gamma": 456789,
            "delta": 1,
        }
        payload = json.dumps(expected, separators=(",", ":")).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)

            for compressed in (False, True):
                source = root / (
                    "good-strings-gzip.db"
                    if compressed
                    else "good-strings-plain.db"
                )

                if compressed:
                    with gzip.open(source, "wb") as handle:
                        handle.write(payload)
                else:
                    source.write_bytes(payload)

                # Size 1 guarantees boundaries inside every multi-digit
                # integer. Other small sizes exercise keys, separators,
                # strings and scalar endings at varying boundaries.
                for chunk_size in range(1, 17):
                    with self.subTest(
                        compressed=compressed,
                        chunk_size=chunk_size,
                    ):
                        with patch.object(
                            goodware._StreamingJsonObject,
                            "_CHUNK_SIZE",
                            chunk_size,
                        ):
                            actual = dict(
                                goodware._iter_source_items(
                                    source,
                                    max_decompressed_bytes=1024 * 1024,
                                )
                            )

                        self.assertEqual(actual, expected)

    def test_streaming_parser_malformed_error_names_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "good-strings-broken.db"

            with gzip.open(source, "wb") as handle:
                handle.write(b'{"alpha":1000x}')

            with patch.object(
                goodware._StreamingJsonObject,
                "_CHUNK_SIZE",
                3,
            ):
                with self.assertRaises(goodware.GoodwareImportError) as raised:
                    list(
                        goodware._iter_source_items(
                            source,
                            max_decompressed_bytes=1024 * 1024,
                        )
                    )

            self.assertIn(source.name, str(raised.exception))

    def test_v2_yargen_empty_string_sentinel_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "index"
            sources.mkdir()

            self._write_source(
                sources,
                "good-strings-part1.db",
                {
                    "": 1358,
                    "Legitimate-Goodware-String": 2,
                },
            )

            manifest = goodware.build_index(sources, output)

            self.assertEqual(goodware.verify_index(output, sources), manifest)
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["occurrence_sum"], 2)
            self.assertEqual(
                goodware.lookup_feature(
                    output,
                    "string",
                    "legitimate-goodware-string",
                ),
                2,
            )
            self.assertEqual(manifest["sources"][0]["entry_count"], 2)
            self.assertEqual(manifest["sources"][0]["occurrence_sum"], 1360)

    def test_v2_normalization_aggregation_and_read_only_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "index"
            sources.mkdir()
            self._write_source(
                sources,
                "good-strings-part1.db",
                {
                    "Hello": 2,
                    "hello": 3,
                    "UTF16LE:HELLO": 4,
                    "very-long-" + ("x" * 10000): 1,
                },
            )
            self._write_source(sources, "good-exports-part1.db", {"CreateThing": 5})
            self._write_source(
                sources,
                "good-imphashes-part1.db",
                {"": 100, " ABCDEF0123456789ABCDEF0123456789 ": 2},
            )

            manifest = goodware.build_index(sources, output)
            self.assertEqual(goodware.verify_index(output, sources), manifest)
            self.assertEqual(goodware.deep_verify_index(output), manifest)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {goodware.INDEX_FILENAME, goodware.MANIFEST_FILENAME},
            )
            self.assertEqual(goodware.lookup_feature(output, "string", "hello"), 9)
            self.assertEqual(
                goodware.lookup_count(
                    output,
                    "string",
                    "very-long-" + ("x" * 10000),
                ),
                1,
            )
            self.assertIsNone(
                goodware.lookup_feature(output, "string", "absent-feature")
            )
            self.assertEqual(
                goodware.lookup_feature(output, "export", "creatething"), 5
            )
            self.assertEqual(
                goodware.lookup_feature(
                    output,
                    "imphash",
                    "abcdef0123456789abcdef0123456789",
                ),
                2,
            )
            self.assertEqual(manifest["record_count"], 4)
            self.assertEqual(manifest["occurrence_sum"], 17)

            index_path = output / goodware.INDEX_FILENAME
            connection = sqlite3.connect(index_path)
            try:
                objects = list(
                    connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                )
                self.assertEqual(objects, [("features",), ("metadata",)])
                self.assertEqual(
                    [
                        row[1]
                        for row in connection.execute("PRAGMA table_info(features)")
                    ],
                    ["feature_key", "occurrence_count"],
                )
            finally:
                connection.close()
            self.assertFalse(os.access(index_path, os.W_OK))
            connection = sqlite3.connect(f"{index_path.as_uri()}?mode=ro", uri=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE unexpected(value TEXT)")
            finally:
                connection.close()

    def test_v2_normalization_contract_and_golden_vectors(self) -> None:
        self.assertEqual(goodware.normalize_value("string", "HELLO"), "hello")
        self.assertEqual(goodware.normalize_value("string", "UTF16LE:HELLO"), "hello")
        self.assertEqual(
            goodware.normalize_value("export", "CreateThing"), "creatething"
        )
        self.assertEqual(
            goodware.normalize_value("opcode_fragment16", "AA BB CC DD EE FF 00 11"),
            "aabbccddeeff0011",
        )
        with self.assertRaises(goodware.GoodwareImportError):
            goodware.normalize_value("string", "")
        with self.assertRaises(goodware.GoodwareImportError):
            goodware.normalize_value("imphash", "   ")
        with self.assertRaises(goodware.GoodwareImportError):
            goodware.normalize_value("imphash", "not-an-imphash")

        vectors = {
            (
                "string",
                "abc",
            ): "b8256d9306818d46c10e2eefc5aaa6c6e19d0456313703cab0f8201928f94e78",
            (
                "opcode_fragment16",
                "aabbccddeeff0011",
            ): "2b36722201be82a9087bbf4bd6b914479e892e65028015405d4824e492dc9d8c",
            (
                "imphash",
                "abcdef0123456789abcdef0123456789",
            ): "0e7eb14094332976109049787b98093ca695b9806144997506c39a7f9c2cf387",
            (
                "export",
                "createfilew",
            ): "7ca816bdfa9e2a6e0c23bb9cd990a36b7e465444012e5e923052ebe922f58784",
        }
        for (kind, value), expected in vectors.items():
            self.assertEqual(goodware.lookup_key(kind, value).hex(), expected)

        source_set = "a" * 64
        expected_fingerprint = (
            "7c3e47d47fc74f5aba473dec864a88c70bcdd477498a603c715501794dc07692"
        )
        self.assertEqual(
            goodware.baseline_fingerprint_sha256(source_set), expected_fingerprint
        )
        self.assertEqual(
            goodware.baseline_fingerprint_sha256(source_set), expected_fingerprint
        )

    def test_v2_ingestion_is_batched_and_rebuild_is_logically_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            first = root / "first"
            second = root / "second"
            sources.mkdir()
            self._write_source(
                sources,
                "good-strings-many.db",
                {f"value-{number}": 1 for number in range(7)},
            )

            original_batch_size = goodware.INGEST_BATCH_SIZE
            original_insert = goodware._insert_feature_rows
            batches: list[int] = []

            def record_batch(connection, rows):
                batches.append(len(rows))
                return original_insert(connection, rows)

            goodware.INGEST_BATCH_SIZE = 2
            goodware._insert_feature_rows = record_batch
            try:
                first_manifest = goodware.build_index(sources, first)
                second_manifest = goodware.build_index(sources, second)
            finally:
                goodware.INGEST_BATCH_SIZE = original_batch_size
                goodware._insert_feature_rows = original_insert

            self.assertGreater(len(batches), 2)
            self.assertLessEqual(max(batches), 2)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(
                goodware.lookup_feature(first, "string", "value-3"),
                goodware.lookup_feature(second, "string", "value-3"),
            )

    def test_v2_build_has_no_normalized_value_sqlite_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            sources.mkdir()
            self._write_source(sources, "good-strings-part1.db", {"Alpha": 2})
            statements: list[str] = []
            original_connect = goodware.sqlite3.connect

            def tracing_connect(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(goodware.sqlite3, "connect", tracing_connect):
                goodware.build_index(sources, root / "index")

            self.assertFalse(any("CREATE TABLE records" in sql for sql in statements))
            self.assertFalse(any("normalized_value" in sql for sql in statements))

    def test_routine_verify_skips_feature_scans_deep_verify_runs_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "index"
            sources.mkdir()
            self._write_source(sources, "good-strings-part1.db", {"alpha": 2})
            goodware.build_index(sources, output)

            original_connect = goodware.sqlite3.connect
            routine_sql: list[str] = []

            def trace_routine(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connection.set_trace_callback(routine_sql.append)
                return connection

            with patch.object(goodware.sqlite3, "connect", trace_routine):
                goodware.verify_index(output)
            self.assertFalse(
                any("COUNT(*)" in sql or "SUM(" in sql for sql in routine_sql)
            )
            self.assertFalse(
                any("SELECT 1 FROM features" in sql for sql in routine_sql)
            )

            deep_sql: list[str] = []

            def trace_deep(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connection.set_trace_callback(deep_sql.append)
                return connection

            with patch.object(goodware.sqlite3, "connect", trace_deep):
                goodware.deep_verify_index(output)
            self.assertTrue(
                any("COUNT(*)" in sql and "SUM(" in sql for sql in deep_sql)
            )
            self.assertTrue(any("PRAGMA integrity_check" in sql for sql in deep_sql))

    def test_v1_and_v2_decompression_defaults_are_independent(self) -> None:
        self.assertEqual(goodware.DEFAULT_V1_MAX_DECOMPRESSED_BYTES, 256 * 1024 * 1024)
        self.assertEqual(
            goodware.DEFAULT_V2_MAX_DECOMPRESSED_BYTES, 8 * 1024 * 1024 * 1024
        )
        parser = goodware._parser()
        self.assertEqual(
            parser.parse_args(["build", "sources", "output"]).max_decompressed_bytes,
            goodware.DEFAULT_V1_MAX_DECOMPRESSED_BYTES,
        )
        self.assertEqual(
            parser.parse_args(
                ["build-index", "sources", "output"]
            ).max_decompressed_bytes,
            goodware.DEFAULT_V2_MAX_DECOMPRESSED_BYTES,
        )

    def test_v2_manifest_index_and_source_corruption_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sources = root / "sources"
            output = root / "index"
            sources.mkdir()
            self._write_source(sources, "good-strings-part1.db", {"alpha": 1})
            goodware.build_index(sources, output)

            manifest_path = output / goodware.MANIFEST_FILENAME
            original_manifest = manifest_path.read_bytes()
            manifest = json.loads(original_manifest)
            manifest["normalization_version"] = "unknown"
            manifest_path.chmod(0o644)
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(goodware.GoodwareImportError):
                goodware.verify_index(output)
            manifest_path.write_bytes(original_manifest)
            manifest_path.chmod(0o444)

            self._write_source(sources, "good-strings-part1.db", {"changed": 1})
            with self.assertRaises(goodware.GoodwareImportError):
                goodware.verify_index(output, sources)

            shutil.copytree(output, root / "corrupt-index")
            corrupt_index = root / "corrupt-index" / goodware.INDEX_FILENAME
            corrupt_index.chmod(0o644)
            with corrupt_index.open("ab") as handle:
                handle.write(b"corrupt")
            with self.assertRaises(goodware.GoodwareImportError):
                goodware.verify_index(root / "corrupt-index")


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
            self.assertEqual(
                fixture_guard.find_binary_files(root), [root / "sample.bin"]
            )

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
