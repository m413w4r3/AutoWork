from __future__ import annotations

import csv
import io
import re
from types import SimpleNamespace
from uuid import UUID

from cti_app.application.opinion_ioc_csv_export import build_opinion_ioc_csv_exports


def _group(artifact_type: str, *values: str) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_type=artifact_type,
        indicators=tuple(SimpleNamespace(value=value, normalized_value=value) for value in values),
    )


def _item(artifact_type: str, value: str, status: str) -> dict[str, object]:
    return {
        "artifact_type": artifact_type,
        "value": {"text": value},
        "indicator_status_label": status,
    }


def _fixture() -> tuple[SimpleNamespace, dict[str, object]]:
    source_id = UUID("11111111-2222-4333-8444-555555555555")
    source = SimpleNamespace(
        tier="core",
        kind="publication",
        canonical_url="https://vendor.example/report",
        publisher="Éditeur exemple",
        source_document_id=source_id,
    )
    document = SimpleNamespace(
        title="[Groupe] Rapport de menace",
        sources=(source,),
        indicators=(
            _group("ip", "198.51.100.7"),
            _group("domain", "source[.]example"),
            _group("domain", "11111111-2222-4333-8444-555555555555"),
            _group("hash", "a" * 64),
            _group("email", "analyste@example.test"),
        ),
        original_indicators=(_group("hash", "b" * 64),),
    )
    render_data: dict[str, object] = {
        "content_sections": [
            {
                "type": "ioc",
                "source_indicators": [
                    {
                        "heading": "Adresse IP",
                        "items": [_item("ip", "198.51.100.7", "Documenté par l'article")],
                    }
                ],
                "original_indicators": [
                    {
                        "heading": "URLs",
                        "items": [
                            _item(
                                "url",
                                "hxxps://new[.]example/path",
                                "Indicateur découvert dans cette analyse",
                            )
                        ],
                    }
                ],
                "candidate_indicators": [
                    {
                        "heading": "Domaines",
                        "items": [_item("domain", "candidate[.]example", "Piste non confirmée")],
                    }
                ],
            },
            {
                "type": "technical_annex",
                "ioc_rows": [
                    {
                        "type": "hash",
                        "value": "a" * 64,
                        "indicator_status_label": "Documenté par l'article",
                        "associated_file": "sample.bin",
                        "context_summary": (
                            "Bridge workbench enquête 11111111-2222-4333-8444-555555555555"
                        ),
                        "source_urls": [
                            {"label": "internal Bridge tool", "url": "https://internal.example"}
                        ],
                        "source_refs": [
                            {"target_id": "investigation-11111111-2222-4333-8444-555555555555"}
                        ],
                    },
                    {
                        "type": "filename",
                        "value": "sample.bin",
                        "indicator_status_label": "Documenté par l'article",
                    },
                    {
                        "type": "filepath",
                        "value": "C:\\Temp\\sample.bin",
                        "indicator_status_label": "Documenté par l'article",
                    },
                    {
                        "type": "domain",
                        "value": "candidate.example",
                        "indicator_status_label": "Piste non confirmée",
                    },
                ],
            },
        ]
    }
    return document, render_data


def _read_csv(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8"))))


def test_article_csv_matches_the_legacy_reporter_wire_format() -> None:
    document, render_data = _fixture()

    exports = build_opinion_ioc_csv_exports(document, render_data)
    rows = _read_csv(exports["ioc_pipeline_article.csv"])

    assert rows[0] == ["[Groupe|Rapport de menace|Éditeur exemple|https://vendor.example/report]"]
    assert rows[1] == ["isNew", "type", "value"]
    assert all(len(row) == 3 for row in rows[2:])
    assert {(row[0], row[1], row[2]) for row in rows[2:]} == {
        ("0", "ip", "198.51.100.7"),
        ("0", "domain", "source.example"),
        ("0", "file", "a" * 64),
        ("1", "file", "b" * 64),
        ("1", "url", "https://new.example/path"),
    }
    assert all(
        row[0] in {"0", "1"} and row[1] in {"ip", "domain", "url", "file"} for row in rows[2:]
    )
    assert "analyste@example.test" in exports["ioc_exclus.csv"].decode("utf-8")
    assert "Type non pris en charge" in exports["ioc_exclus.csv"].decode("utf-8")
    assert "Identifiant interne masqué" in exports["ioc_exclus.csv"].decode("utf-8")


def test_readable_csvs_use_french_labels_and_do_not_expose_internal_provenance() -> None:
    document, render_data = _fixture()

    exports = build_opinion_ioc_csv_exports(document, render_data)
    fichier_rows = _read_csv(exports["ioc_fichiers.csv"])
    reseau_rows = _read_csv(exports["ioc_reseau.csv"])
    chemins_rows = _read_csv(exports["noms_et_chemins_de_fichiers.csv"])
    pistes_rows = _read_csv(exports["pistes_non_confirmees.csv"])
    all_text = "\n".join(content.decode("utf-8") for content in exports.values()).casefold()

    assert fichier_rows[0] == [
        "Catégorie",
        "Algorithme",
        "Valeur",
        "Nom ou chemin associé",
        "Statut",
        "Source",
    ]
    assert all(row[0] == "Fichiers" for row in fichier_rows[1:])
    assert any(row[1] == "SHA-256" and row[3] == "sample.bin" for row in fichier_rows[1:])
    assert reseau_rows[0][0] == "Catégorie" and all(row[0] == "IOC" for row in reseau_rows[1:])
    assert any(row[2] == "https://new.example/path" for row in reseau_rows[1:])
    assert all(row[0] == "Fichiers" for row in chemins_rows[1:])
    assert all(row[0] in {"IOC", "Fichiers"} for row in pistes_rows[1:])
    assert "piste non confirmée" in "\n".join(",".join(row) for row in pistes_rows).casefold()
    assert (
        re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", all_text)
        is None
    )
    assert not any(
        marker in all_text for marker in ("bridge", "workbench", "enquête", "investigation-")
    )
    assert "hashes" not in all_text and "ioc de l'article" not in all_text


def test_exports_are_byte_for_byte_reproducible() -> None:
    document, render_data = _fixture()

    first = build_opinion_ioc_csv_exports(document, render_data)
    second = build_opinion_ioc_csv_exports(document, render_data)

    assert first == second
    assert set(first) == {
        "LISEZ-MOI.md",
        "ioc_pipeline_article.csv",
        "ioc_fichiers.csv",
        "ioc_reseau.csv",
        "noms_et_chemins_de_fichiers.csv",
        "pistes_non_confirmees.csv",
        "ioc_exclus.csv",
    }
