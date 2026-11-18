#!/usr/bin/env python3
"""Rebuild the IOC annex CSVs of an edition from the frozen V5 documents plus curated pivots.

Offline; run it with the backend environment (it reuses the platform's IOC value projection and
legitimate-services registry):

  cd backend && uv run --python 3.12 python ../scripts/ops/curate_ioc_annexes.py \
      --v5-dir ../.autowork-runs/m21/ioc_curation --annexes ~/Downloads/tmp/FINAL_BULLETIN/annexes \
      [--manuel-dir ~/Downloads/tmp/FINAL_BULLETIN/typst_manuel]

Inputs: ``<v5-dir>/v5_artNN.json`` (the V5 document of each article: the ``schema_version`` "5"
body nested in the publication artifact) and ``<v5-dir>/pivots.json``
(``{"NN": {"article_additions":
[...], "pivots": [...]}}``, each item ``{type, value, reason}``).

Rules (documented in the regenerated LISEZ-MOI.md of each article):
- IOC of the article (isNew=0): ip / domain / url / hash (SHA-256, SHA-1, MD5) listed by a PRIMARY
  source and either retained or "uncertain, link to the subject not demonstrated" (the article
  lists them), plus ``article_additions``.
- IOC discovered by the analysis (isNew=1): ``pivots``.
- Not IOC (kept only in ioc_exclus.csv): legitimate services (platform registry), context items
  whose
  malicious role is not demonstrated, non-routable IPs, fuzzy/long digests, independent-source
  digests of already listed samples.
- File names and paths are not IOC: noms_et_chemins_de_fichiers.csv is kept untouched.
"""

from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from cti_app.application.edition_ioc_export import _project_value, _safe_field, refang
from cti_app.domain.publication import ArtifactType
from cti_app.infrastructure.legitimate_services import load_legitimate_services

TYPE_ORDER = {"ip": 0, "domain": 1, "url": 2, "file": 3}
TYPE_LABEL = {"ip": "Adresse IP", "domain": "Domaine", "url": "URL"}
DOC_HEAD = re.compile(r'#doc-head\(\[(?P<title>.*?)\],\s*"(?P<number>\d+)"', re.DOTALL)
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
STATUS_ARTICLE = ("Documenté dans la publication", "Publication")
STATUS_PIVOT = ("Découvert dans l'analyse", "Avis de l'analyste")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def non_routable(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def title_metadata(manuel_dir: Path | None, number: str) -> tuple[str, str] | None:
    if manuel_dir is None:
        return None
    source = manuel_dir / "articles" / f"article{number}" / f"article{number}.typ"
    if not source.is_file():
        return None
    match = DOC_HEAD.search(source.read_text(encoding="utf-8"))
    if match is None:
        return None
    title = match.group("title").replace("\\[", "[").replace("\\]", "]").replace("\\", "")
    group = re.match(r"\[(?P<g>[^\]]+)\]\s*(?P<t>.*)", title, re.DOTALL)
    return (group.group("g").strip(), " ".join(group.group("t").split())) if group else None


def classify(details: list[dict], legit) -> tuple[dict, list, list]:
    """Return (article IOC by (type, key) -> value, exclusions, alias/independent digests)."""
    article: dict[tuple[str, str], str] = {}
    excluded: list[tuple[str, str, str]] = []
    aliases: list[tuple[str, str, str]] = []
    for detail in details:
        artifact_type = detail["artifact_type"]
        raw = detail["value"]
        if artifact_type in {"filename", "filepath"}:
            continue
        value, kind = _project_value(artifact_type, refang(detail.get("normalized_value") or raw))
        label = {"hash": "Empreinte de fichier"}.get(
            artifact_type, TYPE_LABEL.get(artifact_type, artifact_type)
        )
        if value is None:
            reason = (
                "Empreinte non exploitable comme IOC (format flou ou long)"
                if artifact_type == "hash"
                else "Type non pris en charge"
            )
            (aliases if artifact_type == "hash" else excluded).append((label, raw, reason))
            continue
        if kind == "ip" and value.count(":") == 1 and "." in value:
            value = value.rsplit(":", 1)[0]  # "1.2.3.4:443" -> the address only
        if kind == "ip" and non_routable(value):
            excluded.append((label, value, "Adresse non routable ou réservée"))
            continue
        if kind in {"domain", "url"} and legit.match_indicator(ArtifactType(artifact_type), value):
            excluded.append((label, value, "Service légitime"))
            continue
        classification = detail["relevance_classification"]
        reason_code = detail["relevance_reason_code"]
        if classification == "context" and reason_code == "malicious_role_not_demonstrated":
            excluded.append((label, value, "Contexte : rôle malveillant non démontré"))
            continue
        listed_by_article = detail["source_role"] == "primary" and (
            detail["disposition"] == "retained"
            or (detail["disposition"] == "uncertain" and classification == "indeterminate")
        )
        if listed_by_article:
            key = value.casefold() if kind in {"ip", "domain", "file"} else value
            article.setdefault((kind, key), value)
        else:
            aliases.append(
                (
                    label,
                    value,
                    "Empreinte d'une source indépendante (autre condensat d'un échantillon listé)",
                )
            )
    listed = {key[1] for key in article}
    aliases = [item for item in aliases if item[1].casefold() not in listed]
    return article, excluded, aliases


def curate(number: str, v5: dict, pivots: dict, annexes: Path, manuel: Path | None, legit) -> str:
    csv_dir = annexes / f"Article{number}" / "ioc_csv"
    backup = annexes.parent.parent / f"annexes_csv_avant_curation_{STAMP}" / f"Article{number}"
    shutil.copytree(csv_dir, backup)
    old_fichiers = {
        r["Valeur"].lower(): r["Nom ou chemin associé"]
        for r in read_csv(csv_dir / "ioc_fichiers.csv")
    }

    article, excluded, aliases = classify(v5["ioc_details"], legit)
    for item in pivots.get("article_additions", []):
        value, kind = _project_value(item["type"], refang(item["value"]))
        if value is None:
            raise SystemExit(f"invalid article addition {item}")
        article.setdefault(
            (kind, value.casefold() if kind in {"ip", "domain", "file"} else value), value
        )
    new: dict[tuple[str, str], tuple[str, str]] = {}
    for item in pivots.get("pivots", []):
        value, kind = _project_value(item["type"], refang(item["value"]))
        if value is None:
            raise SystemExit(f"invalid pivot {item}")
        key = (kind, value.casefold() if kind in {"ip", "domain", "file"} else value)
        if key not in article:
            new[key] = (value, item["reason"])

    old_meta = (
        (csv_dir / "ioc_pipeline_article.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    )
    meta = old_meta.strip("[]").split("|")
    refreshed = title_metadata(manuel, number)
    if refreshed is not None:
        meta[0], meta[1] = refreshed

    def ordered(mapping):  # type: ignore[no-untyped-def]
        return sorted(mapping.items(), key=lambda kv: (TYPE_ORDER[kv[0][0]], kv[0][1]))

    lines = ["[" + "|".join(meta) + "]", "isNew,type,value"]
    lines += [f"0,{k[0]},{_safe_field(v)}" for k, v in ordered(article)]
    lines += [f"1,{k[0]},{_safe_field(v[0])}" for k, v in ordered(new)]
    (csv_dir / "ioc_pipeline_article.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    network, files = [], []
    for (kind, _), value in ordered(article):
        (files if kind == "file" else network).append((kind, value, STATUS_ARTICLE))
    for (kind, _), (value, _) in ordered(new):
        (files if kind == "file" else network).append((kind, value, STATUS_PIVOT))
    write_csv(
        csv_dir / "ioc_reseau.csv",
        ["Catégorie", "Type", "Valeur", "Statut", "Source"],
        [["IOC", TYPE_LABEL[k], v, s[0], s[1]] for k, v, s in network],
    )
    write_csv(
        csv_dir / "ioc_fichiers.csv",
        ["Catégorie", "Algorithme", "Valeur", "Nom ou chemin associé", "Statut", "Source"],
        [
            [
                "Fichiers",
                {64: "SHA-256", 40: "SHA-1", 32: "MD5"}[len(v)],
                v,
                old_fichiers.get(v.lower(), ""),
                s[0],
                s[1],
            ]
            for _, v, s in files
        ],
    )
    write_csv(
        csv_dir / "pistes_non_confirmees.csv",
        ["Catégorie", "Type", "Valeur", "Statut", "Source"],
        [
            [
                "Fichiers",
                "Empreinte de fichier",
                v,
                "Empreinte complémentaire ou non exploitable",
                "Publication",
            ]
            for _, v, _ in aliases
        ],
    )
    write_csv(
        csv_dir / "ioc_exclus.csv",
        ["Type", "Valeur", "Motif"],
        [[t, v, m] for t, v, m in excluded + aliases],
    )
    notes = (csv_dir / "LISEZ-MOI.md").read_text(encoding="utf-8")
    notes = notes.split("\n## Curation")[0].rstrip() + (
        "\n\n## Curation des indicateurs\n\n"
        "- `isNew=0` : indicateurs listés par l'article source "
        "(IP, domaines, URL, empreintes SHA-256/SHA-1/MD5).\n"
        "- `isNew=1` : indicateurs découverts par l'analyse (pivots).\n"
        "- Ne sont pas des IOC : services légitimes, contexte sans rôle malveillant démontré, "
        "adresses non routables, empreintes floues ; ils figurent dans `ioc_exclus.csv`.\n"
        "- Les noms et chemins de fichiers restent dans `noms_et_chemins_de_fichiers.csv`.\n"
    )
    (csv_dir / "LISEZ-MOI.md").write_text(notes, encoding="utf-8")
    return (
        f"Article{number}: IOC article={len(article)} découverts={len(new)} "
        f"exclus={len(excluded)} empreintes écartées={len(aliases)} (sauvegarde {backup})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--v5-dir", required=True, type=Path)
    parser.add_argument("--annexes", required=True, type=Path)
    parser.add_argument("--manuel-dir", type=Path)
    args = parser.parse_args()
    pivots = json.loads((args.v5_dir / "pivots.json").read_text(encoding="utf-8"))
    legit = load_legitimate_services()
    for path in sorted(args.v5_dir.glob("v5_art[0-9][0-9].json")):
        number = path.stem[-2:]
        v5 = json.loads(path.read_text(encoding="utf-8"))
        print(curate(number, v5, pivots.get(number, {}), args.annexes, args.manuel_dir, legit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
