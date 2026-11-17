#!/usr/bin/env python3
"""Rewrite the "IOC" and "IOC originaux" sections of the hand-edited Typst articles from the CSVs.

For each ``articles/articleNN/articleNN.typ`` of a ``typst_manuel`` tree, everything from
``#render-section-heading("IOC")`` to the end of the file is replaced by two sections built from
``<annexes>/ArticleNN/ioc_csv/ioc_pipeline_article.csv``: "IOC" (isNew=0, IOC of the article) and
"IOC originaux" (isNew=1, IOC discovered by the analysis; omitted when empty). Text above the IOC
heading (your edits) is untouched; the original file is saved under ``<manuel>_avant_ioc_<date>/``.

Usage: update_manuel_ioc_sections.py --manuel-dir typst_manuel --annexes FINAL_BULLETIN/annexes
"""

from __future__ import annotations

import argparse
import csv
import shutil
from datetime import datetime
from pathlib import Path

HEADING = '#render-section-heading("IOC")'
GROUPS = (("domain", "Domaines"), ("file", "Fichiers"), ("ip", "IPv4"), ("url", "URLs"))
GROUP_ORDER = {label: index for index, (_, label) in enumerate(GROUPS)}
LABEL = dict(GROUPS)


def typst_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def section(title: str, rows: list[tuple[str, str]]) -> list[str]:
    lines = [f'#render-section-heading("{title}")', ""]
    for label in sorted({LABEL[kind] for kind, _ in rows}, key=GROUP_ORDER.__getitem__):
        lines += [
            "#block(above: 5pt, below: 3pt, sticky: true)[",
            f'#text(size: 10pt, weight: "bold", fill: purple)[{label} :]',
            "]",
        ]
        for kind, value in rows:
            if LABEL[kind] == label:
                lines += [
                    "#block(above: 0pt, below: 2pt)[",
                    "#text(font: code-font-families, size: 8.75pt, hyphenate: false)"
                    f'[#breakable-text("{typst_string(value)}")]',
                    "]",
                ]
    return [*lines, ""]


def read_rows(path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    article: list[tuple[str, str]] = []
    new: list[tuple[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        handle.readline()  # [Acteur|Titre|Éditeur|Lien]
        for row in csv.DictReader(handle):
            (new if row["isNew"] == "1" else article).append((row["type"], row["value"]))
    return article, new


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manuel-dir", required=True, type=Path)
    parser.add_argument("--annexes", required=True, type=Path)
    args = parser.parse_args()
    backup = args.manuel_dir.parent / f"typst_manuel_avant_ioc_{datetime.now():%Y%m%d-%H%M%S}"
    for source in sorted(
        (args.manuel_dir / "articles").glob("article[0-9][0-9]/article[0-9][0-9].typ")
    ):
        number = source.stem[-2:]
        csv_path = args.annexes / f"Article{number}" / "ioc_csv" / "ioc_pipeline_article.csv"
        if not csv_path.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        index = text.find(HEADING)
        if index < 0:
            raise SystemExit(f"{source}: no {HEADING} found")
        article, new = read_rows(csv_path)
        lines = section("IOC", article)
        if new:
            lines += section("IOC originaux", new)
        backup.mkdir(exist_ok=True)
        shutil.copy2(source, backup / source.name)
        source.write_text(text[:index] + "\n".join(lines), encoding="utf-8")
        print(f"article{number}: IOC={len(article)} IOC originaux={len(new)}")
    print(f"sauvegarde : {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
