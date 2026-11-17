from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "inject_discovered_iocs.py"
_spec = importlib.util.spec_from_file_location("inject_discovered_iocs", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_injector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_injector)


class InjectDiscoveredIOCsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.csv_root = self.root / "ioc-csv"
        self.csv_root.mkdir()
        self.hash = "d172138487a2e66398c7474e24849dbf19157bb30823de7df98556943de7f74f"
        self.source_item: dict[str, Any] = {
            "artifact_type": "ip",
            "type_label": "IPv4",
            "value": {
                "text": "203.0.113.7",
                "break_chunks": [],
                "break_after": [],
            },
            "source_refs": [{"number": 1, "target_id": "ref-1"}],
            "evidence": [{"context": "fixture evidence"}],
            "context": "fixture context",
            "draft_refs": [{"kind": "finding", "reference_id": "finding-1"}],
            "qualification": "fixture qualification",
            "relationship_status_label": "fixture relationship",
            "indicator_status_label": "fixture status",
        }
        self.data: dict[str, Any] = {
            "schema_version": "edition-render-data-fixture",
            "edition": {"version": 3},
            "publications": [
                self._publication("01", [copy.deepcopy(self.source_item)]),
                self._publication("02", []),
                self._publication("03", []),
            ],
        }
        self._write_csv(
            "01",
            (
                "0,file,00000000000000000000000000000000",
                f"1,file,{self.hash}",
                f"1,file,{self.hash}",
                "1,ip,203.0.113.7",
            ),
        )
        self._write_csv("02", ("0,domain,unchanged.example",))
        self._write_csv("03", ("0,url,https://unchanged.example/path",))

    def _publication(self, number: str, source_items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "number": number,
            "kind": "article",
            "content_sections": [
                {
                    "type": "ioc",
                    "source_indicators": (
                        [{"heading": "IPv4", "items": source_items}] if source_items else []
                    ),
                    "original_indicators": [],
                    "candidate_indicators": [
                        {"heading": "Pistes non confirmées", "items": [{"value": {"text": "keep"}}]}
                    ],
                }
            ],
        }

    def _write_csv(self, number: str, rows: tuple[str, ...]) -> None:
        article_directory = self.csv_root / f"art{number}"
        article_directory.mkdir()
        path = article_directory / "ioc_pipeline_article.csv"
        path.write_text(
            "[métadonnées de livraison]\nisNew,type,value\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )

    def test_art01_gets_one_original_hash_and_other_articles_are_unchanged(self) -> None:
        before = copy.deepcopy(self.data)
        result = _injector.inject_discovered_iocs(self.data, self.csv_root)

        art01 = result["publications"][0]
        ioc = art01["content_sections"][0]
        self.assertEqual(before["publications"][1:], result["publications"][1:])
        self.assertEqual(
            ioc["candidate_indicators"],
            before["publications"][0]["content_sections"][0]["candidate_indicators"],
        )
        self.assertEqual(len(ioc["original_indicators"]), 1)
        group = ioc["original_indicators"][0]
        self.assertEqual(group["heading"], "Fichiers")
        self.assertEqual(len(group["items"]), 1)

        item = group["items"][0]
        self.assertEqual(set(item), set(self.source_item))
        self.assertEqual(item["artifact_type"], "hash")
        self.assertEqual(item["type_label"], "Fichiers")
        self.assertEqual(item["value"]["text"], self.hash)
        self.assertEqual(
            item["value"]["break_chunks"],
            [self.hash[index : index + 8] for index in range(0, len(self.hash), 8)],
        )
        self.assertEqual(item["value"]["break_after"], [True] * 7)
        self.assertEqual(item["source_refs"], [])
        self.assertEqual(item["evidence"], [])
        self.assertEqual(item["context"], "")
        self.assertEqual(item["draft_refs"], [])
        self.assertEqual(item["qualification"], "")
        self.assertEqual(item["relationship_status_label"], "")
        self.assertEqual(item["indicator_status_label"], "")

    def test_non_file_types_use_reader_labels_and_plain_text_values(self) -> None:
        csv_path = self.csv_root / "art01" / "ioc_pipeline_article.csv"
        csv_path.write_text(
            "[métadonnées de livraison]\nisNew,type,value\n"
            "1,ip,203.0.113.8\n"
            "1,url,https://new.example/path\n"
            "1,domain,new.example\n",
            encoding="utf-8",
        )

        result = _injector.inject_discovered_iocs(copy.deepcopy(self.data), self.csv_root)
        groups = result["publications"][0]["content_sections"][0]["original_indicators"]

        self.assertEqual(
            [group["heading"] for group in groups],
            ["IPv4", "URLs", "Domaines"],
        )
        expected = (
            ("ip", "IPv4", "203.0.113.8"),
            ("url", "URLs", "https://new.example/path"),
            ("domain", "Domaines", "new.example"),
        )
        for group, (artifact_type, type_label, value) in zip(groups, expected, strict=True):
            item = group["items"][0]
            self.assertEqual(item["artifact_type"], artifact_type)
            self.assertEqual(item["type_label"], type_label)
            self.assertEqual(item["value"], {"text": value})

    def test_injection_is_idempotent(self) -> None:
        once = _injector.inject_discovered_iocs(copy.deepcopy(self.data), self.csv_root)
        twice = _injector.inject_discovered_iocs(copy.deepcopy(once), self.csv_root)

        self.assertEqual(twice, once)

    def test_output_option_writes_injected_json_without_rewriting_input(self) -> None:
        data_path = self.root / "edition-render-data.json"
        output_path = self.root / "edition-render-data-with-iocs.json"
        data_path.write_text(json.dumps(self.data), encoding="utf-8")

        result = _injector.main(
            [
                "--data",
                str(data_path),
                "--ioc-csv-root",
                str(self.csv_root),
                "--output",
                str(output_path),
            ]
        )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(data_path.read_text(encoding="utf-8")), self.data)
        written = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["publications"][0]["content_sections"][0]["original_indicators"][0]["heading"],
            "Fichiers",
        )

    def test_invalid_render_data_shape_fails(self) -> None:
        with self.assertRaises(_injector.InjectionError):
            _injector.inject_discovered_iocs({"edition": {}}, self.csv_root)


if __name__ == "__main__":
    unittest.main()
