from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID, uuid4

from cti_app.application.edition_classification import (
    BRIEF_MAX_WORDS,
    classify_edition_publications,
    is_brief,
)
from cti_app.application.edition_ioc_export import (
    build_edition_ioc_archive,
    project_publication_iocs,
    refang,
)
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.publication import ArtifactType


def _indicator(value: str, artifact_type: ArtifactType, normalized: str | None = None):
    return SimpleNamespace(
        value=value,
        normalized_value=normalized or value,
        artifact_type=artifact_type,
    )


def _group(artifact_type: ArtifactType, *values: str):
    return SimpleNamespace(
        artifact_type=artifact_type,
        indicators=tuple(_indicator(value, artifact_type) for value in values),
    )


def _publication(
    title: str,
    *,
    indicators: tuple[object, ...] = (),
    originals: tuple[object, ...] = (),
    headings: tuple[str, ...] = (),
    body: str = "",
):
    sections = tuple(
        SimpleNamespace(
            heading=heading,
            paragraphs=(SimpleNamespace(text=body or "Section text"),),
        )
        for heading in headings
    )
    source = SimpleNamespace(
        tier="core",
        kind="publication",
        canonical_url="https://vendor.example/article",
        publisher="Vendor",
        source_document_id=uuid4(),
    )
    return SimpleNamespace(
        title=title,
        lead=(SimpleNamespace(text=body),) if body else (),
        sections=sections,
        indicators=indicators,
        original_indicators=originals,
        sources=(source,),
    )


def _edition_document() -> EditionDocumentV2:
    article = _publication(
        "[Group] Threat report",
        indicators=(
            _group(ArtifactType.IP, "206.166.251.164"),
            _group(ArtifactType.DOMAIN, "secboxes[.]com", "secboxes.com"),
            _group(ArtifactType.URL, "hxxps[:]//zka.example[.]com/a,b|c"),
            _group(
                ArtifactType.HASH,
                "A" * 32,
                "B" * 40,
                "C" * 64,
                "0123456789abcdef",
            ),
            _group(ArtifactType.EMAIL, "analyst@example.test"),
            SimpleNamespace(
                artifact_type=ArtifactType.CVE,
                indicators=(_indicator("CVE-2026-1234", ArtifactType.CVE),),
            ),
        ),
        originals=(_group(ArtifactType.IP, "119.59.116.61"),),
        headings=("Technique",),
        body="A technical report with sufficient detail.",
    )
    brief = _publication("Short update", body="Une brève courte.")
    return EditionDocumentV2(
        edition={
            "id": "11111111-1111-4111-8111-111111111111",
            "country": "France",
            "country_code": "FR",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "version": 5,
        },
        publications=(
            EditionPublicationV2(position=1, subject_id=UUID(int=1), document=article),
            EditionPublicationV2(position=2, subject_id=UUID(int=2), document=brief),
        ),
    )


def _replay_legacy_shell_logic(csv_files: dict[str, bytes]) -> bytes:
    """Run the reference converter's find/sort/tail/tr/awk logic on these CSVs."""
    converter = r"""#!/bin/bash
set -euo pipefail
INPUT_DIR="$1"
OUTPUT="legacy_iocs_$(basename "$INPUT_DIR").txt"
: > "$OUTPUT"
find "$INPUT_DIR" -type f -name '*.csv' -print0 |
sort -z |
while IFS= read -r -d '' file; do
    metadata=$(head -n 1 "$file" | tr -d '\r')
    if [[ ! "$metadata" =~ ^\[.*\]$ ]]; then
        continue
    fi
    printf '%s\n' "$metadata" >> "$OUTPUT"
    tail -n +3 "$file" |
        tr -d '\r' |
        awk -F',' 'NF >= 3 && $3 != "" { print $3 }' >> "$OUTPUT"
    printf '\n' >> "$OUTPUT"
done
"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        input_dir = root / "FR2608"
        for relative_path, content in csv_files.items():
            target = input_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        subprocess.run(
            ["bash", "-c", converter, "legacy-converter", str(input_dir)],
            cwd=root,
            check=True,
            capture_output=True,
        )
        return (root / "legacy_iocs_FR2608.txt").read_bytes()


def test_refang_converts_common_defanging_forms_exactly() -> None:
    assert refang("HXXPS[:]//a[.]b/path") == "https://a.b/path"
    assert refang("hxxp://a(dot)b") == "http://a.b"


def test_projection_maps_hashes_deduplicates_and_excludes_unsupported_types() -> None:
    document = _edition_document().publications[0].document

    projection = project_publication_iocs(document)

    assert [(row.is_new, row.type, row.value) for row in projection.rows] == [
        (0, "ip", "206.166.251.164"),
        (0, "domain", "secboxes.com"),
        (0, "url", "https://zka.example.com/a%2Cb%7Cc"),
        (0, "file", "a" * 32),
        (0, "file", "b" * 40),
        (0, "file", "c" * 64),
        (1, "ip", "119.59.116.61"),
    ]
    assert projection.counts == {"ip": 2, "domain": 1, "url": 1, "file": 3}
    assert {(item.type, item.reason) for item in projection.excluded} == {
        ("hash", "unsupported_hash_algorithm_or_format"),
        ("email", "unsupported_type_by_ioc_reporter"),
        ("cve", "unsupported_type_by_article_csv_pipeline"),
    }
    assert all(not any(char in row.value for char in ",|\r\n") for row in projection.rows)


def test_article_csv_zip_manifest_legacy_replay_and_bytes_are_deterministic() -> None:
    document = _edition_document()
    archive = build_edition_ioc_archive(document, edition_sha256="a" * 64)
    second = build_edition_ioc_archive(document, edition_sha256="a" * 64)

    assert archive.content == second.content
    assert archive.filename == "iocs_FR2608.zip"
    assert archive.article_count == 2
    assert archive.ioc_count == 7
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        names = zipped.namelist()
        assert names == [
            "articles/article01/article.csv",
            "breves/breve02/article.csv",
            "legacy_iocs_FR2608.txt",
            "manifest.json",
        ]
        csv_files = {name: zipped.read(name) for name in names if name.endswith("article.csv")}
        article_csv = csv_files["articles/article01/article.csv"].decode("utf-8")
        assert article_csv.startswith(
            "[Group|Threat report|Vendor|https://vendor.example/article]\nisNew,type,value\n"
        )
        assert "1,ip,119.59.116.61\n" in article_csv
        assert zipped.read("legacy_iocs_FR2608.txt") == _replay_legacy_shell_logic(csv_files)
        manifest = json.loads(zipped.read("manifest.json"))

    assert manifest["edition_sha256"] == "a" * 64
    assert manifest["edition_identifier"] == "FR2608"
    assert manifest["publications"][0]["counts"] == {
        "ip": 2,
        "domain": 1,
        "url": 1,
        "file": 3,
    }
    assert manifest["publications"][0]["excluded"]


def test_brief_rule_is_heading_free_and_inclusive_at_word_limit() -> None:
    under = _publication("Short", body=" ".join(["mot"] * BRIEF_MAX_WORDS))
    over = _publication("Long", body=" ".join(["mot"] * (BRIEF_MAX_WORDS + 1)))
    titled = _publication("Titled", headings=("Synthèse",), body="court")

    assert is_brief(under)
    assert not is_brief(over)
    assert not is_brief(titled)
    classified = classify_edition_publications(
        (
            EditionPublicationV2(position=1, subject_id=UUID(int=1), document=over),
            EditionPublicationV2(position=2, subject_id=UUID(int=2), document=under),
        )
    )
    assert [(item.kind, item.number, item.anchor) for item in classified] == [
        ("article", "01", "article01"),
        ("brief", "02", "breve02"),
    ]
