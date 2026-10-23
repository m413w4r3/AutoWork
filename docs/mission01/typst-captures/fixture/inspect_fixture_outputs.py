"""Check shared fixture text and expected historical/current section differences."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


FIXTURE_ROOT = Path(__file__).resolve().parent
CAPTURE_ROOT = FIXTURE_ROOT.parent
VARIANTS = ("current", "historical", "historical-pipeline")
PDF_ROOT = Path(os.environ.get("M01T_PDF_ROOT", CAPTURE_ROOT))
SHARED_TEXT = (
    "M01-T parity fixture — synthetic only",
    "Synthetic event; no external observations.",
    "Synthetic lead paragraph for layout comparison.",
    "Synthetic IOC status matrix — test labels only",
    "All values use RFC 5737 documentation ranges; they are not indicators or evidence.",
    "192.0.2.17",
    "sourcé (test only)",
    "198.51.100.21",
    "validé (test only)",
    "203.0.113.31",
    "candidat (test only)",
    "192.0.2.41",
    "écarté (test only)",
    "Synthetic flow diagram; no observed infrastructure.",
    "Analyst note fixture; no external observations or evidence.",
    "https://example.invalid/reference",
)


def run(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout


def pages(pdf: Path) -> int:
    info = run("pdfinfo", str(pdf))
    match = re.search(r"^Pages:\s+(\d+)$", info, re.MULTILINE)
    if match is None:
        raise AssertionError(f"pdfinfo did not report a page count for {pdf}")
    return int(match.group(1))


def text(pdf: Path) -> str:
    return re.sub(r"\s+", " ", run("pdftotext", "-layout", str(pdf), "-")).strip()


def pdf_for(variant: str) -> Path:
    if "M01T_PDF_ROOT" in os.environ:
        return PDF_ROOT / f"{variant}.pdf"
    return CAPTURE_ROOT / variant / "fixture.pdf"


def main() -> None:
    texts: dict[str, str] = {}
    page_counts: dict[str, int] = {}
    for variant in VARIANTS:
        pdf = pdf_for(variant)
        output = text(pdf)
        missing = [value for value in SHARED_TEXT if value not in output]
        if missing:
            raise AssertionError(f"{variant} is missing shared fixture text: {missing!r}")
        texts[variant] = output
        page_counts[variant] = pages(pdf)
        pngs = tuple((CAPTURE_ROOT / variant).glob("page-*.png"))
        if len(pngs) != page_counts[variant]:
            raise AssertionError(
                f"{variant} has {len(pngs)} page captures for {page_counts[variant]} pages"
            )

    if "Vue d’ensemble" not in texts["historical"] or "ARTICLE" not in texts["historical"]:
        raise AssertionError("historical article() metadata or overview is missing")
    if "Vue d’ensemble" not in texts["historical-pipeline"]:
        raise AssertionError("historical pipeline overview is missing")
    if "Vue d’ensemble" in texts["current"] or "ARTICLE" in texts["current"]:
        raise AssertionError("current V4 fixture unexpectedly contains legacy-only fields")
    if "Pistes non confirmées" not in texts["current"]:
        raise AssertionError("current candidate section is missing")
    if "Écarté (test only)" in texts["current"]:
        raise AssertionError("excluded synthetic value was promoted to the current IOC list")
    if texts["current"].count("192.0.2.41") != 1:
        raise AssertionError("current excluded synthetic value should appear only in the status table")
    if "Adresses IP" not in texts["historical-pipeline"]:
        raise AssertionError("historical CSV IOC output is missing")
    if "IOC originaux" in texts["historical-pipeline"]:
        raise AssertionError("legacy binary fixture unexpectedly includes original IOC rows")
    for value in ("203.0.113.31", "192.0.2.41"):
        if texts["historical-pipeline"].count(value) != 1:
            raise AssertionError(f"legacy CSV unexpectedly rendered candidate/discarded value {value}")

    historical_first_page = run(
        "pdftotext", "-f", "1", "-l", "1", str(pdf_for("historical")), "-"
    ).strip()
    if historical_first_page:
        raise AssertionError("standalone historical article() no longer begins with a blank page")

    for variant in VARIANTS:
        print(f"{variant}: {page_counts[variant]} pages; shared text and PNG pages verified")
    print("historical article(): category/number/overview; standalone leading page is blank")
    print("current renderer: category/number/overview absent; excluded value remains in status table only")


if __name__ == "__main__":
    main()
