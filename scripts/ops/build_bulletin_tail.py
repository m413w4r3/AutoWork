#!/usr/bin/env python3
"""Append the closing pages of a bulletin (non-processed articles + enriched IOC tables).

Reproduces what the reference bulletin (docs/references/CN2609_typ) ends with:
  one landscape page set with the enriched IOC tables, produced by
  docs/references/typstPipeline/ioc_reporter_typst.py from one ``article.csv`` per article.
  (No "articles retenus et non traités" page: AutoWork has none.)

The source tree (``typst_manuel``) is never modified: it is copied to ``<manuel-dir>_complet``,
the extra Typst files are added there, and ``bulletin_manuel_complet.pdf`` is compiled so page
numbering stays coherent.

Safe by default: WITHOUT ``--live`` every provider is pointed at 127.0.0.1 (no external traffic),
so the tables only contain "-" and the layout can be checked. ``--live`` sends the IOC (hashes,
IPs, domains: lookups only, no upload) to Team Cymru, Shodan, MalwareBazaar, Tria.ge, Hybrid
Analysis and Joe Sandbox, and checks sample presence on VirusTotal THROUGH THE AUTOWORK PROXY
(scripts/ops/run_ioc_reporter.py; VIRUSTOTAL_PROXY_URL, no VT key needed here). Only use it when
that external consultation is authorised.

Secrets are read from the environment or from ``--env-file`` (default ``.env``; values are never
printed): VIRUSTOTAL_PROXY_URL, SHODAN_API_KEY, MALWAREBAZAAR_AUTH_KEY, HYBRID_ANALYSIS_API_KEY,
JBX_API_KEY. Team Cymru (whois) and Tria.ge (public search) need no key.

Usage:
  scripts/ops/build_bulletin_tail.py --manuel-dir ~/Downloads/tmp/FINAL_BULLETIN/typst_manuel \
      --ioc-csv-root ~/Downloads/tmp/FINAL_BULLETIN/annexes [--live]

``--ioc-csv-root`` is the annexes directory (``ArticleNN/ioc_csv/ioc_pipeline_article.csv``) or a
directory of ``artNN/ioc_pipeline_article.csv``.
Needs: requests, PyYAML, tqdm, httpx (e.g. ``uv run --with requests --with pyyaml --with tqdm
--with httpx ...``) and
the ``typst`` binary (variable TYPST to override).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "docs" / "references" / "typstPipeline"
SECRET_VARS = {
    "VIRUSTOTAL_PROXY_URL": "VirusTotal (proxy)",
    "SHODAN_API_KEY": "Shodan",
    "MALWAREBAZAAR_AUTH_KEY": "MalwareBazaar",
    "HYBRID_ANALYSIS_API_KEY": "Hybrid Analysis",
    "JBX_API_KEY": "Joe Sandbox",
}
DOC_HEAD = re.compile(r'#doc-head\(\[(?P<title>.*?)\],\s*"(?P<number>\d+)"', re.DOTALL)
TAIL_PAGE = """
#set page(
  margin: (top: 2.3cm, bottom: 2.3cm, left: 2.2cm, right: 2.2cm),
  flipped: true, width: 8.27in, height: 23.39in,
)

#align(center)[
  #text(size: 20pt, weight: "extrabold", fill: purple)[{title}]
]

#include "reports/table_reporting.typ"
"""


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in SECRET_VARS or key == "VIRUSTOTAL_PROXY_INSECURE":
            values[key] = value.strip().strip("'\"")
    return values


def article_title(manuel_dir: Path, number: str) -> tuple[str, str] | None:
    source = manuel_dir / "articles" / f"article{number}" / f"article{number}.typ"
    if not source.is_file():
        return None
    match = DOC_HEAD.search(source.read_text(encoding="utf-8"))
    if match is None:
        return None
    title = match.group("title").replace("\\[", "[").replace("\\]", "]").replace("\\", "")
    group = re.match(r"\[(?P<g>[^\]]+)\]\s*(?P<t>.*)", title, re.DOTALL)
    if group is None:
        return None
    return group.group("g").strip(), " ".join(group.group("t").split())


def write_article_csvs(work: Path, manuel_dir: Path, csv_root: Path) -> list[str]:
    numbers: list[str] = []
    layouts = [(p.name[3:], p / "ioc_pipeline_article.csv") for p in csv_root.glob("art[0-9][0-9]")]
    layouts += [
        (p.name[7:], p / "ioc_csv" / "ioc_pipeline_article.csv")
        for p in csv_root.glob("Article[0-9][0-9]")
    ]
    for number, source in sorted(layouts):
        if not source.is_file():
            continue
        lines = source.read_text(encoding="utf-8").splitlines()
        meta = lines[0].strip("[]").split("|")
        if len(meta) != 4:
            raise SystemExit(f"{source}: metadata line must be [Acteur|Titre|Éditeur|Lien]")
        refreshed = article_title(manuel_dir, number)
        if refreshed is not None:
            meta[0], meta[1] = refreshed  # titles in the CSV can predate the final wording
        target = work / "articles" / f"article{number}"
        target.mkdir(parents=True, exist_ok=True)
        (target / "article.csv").write_text(
            "\n".join(["[" + "|".join(meta) + "]", *lines[1:]]) + "\n", encoding="utf-8"
        )
        numbers.append(number)
    if not numbers:
        raise SystemExit(f"no ioc_pipeline_article.csv under {csv_root}")
    return numbers


def write_config(work: Path, live: bool, vt_delay: float) -> Path:
    text = (PIPELINE / "config.example.yaml").read_text(encoding="utf-8")
    if live:
        # The proxy is a plain-HTTP forward proxy: only http:// URLs go through it.
        text = text.replace("https://www.virustotal.com/api/v3", "http://www.virustotal.com/api/v3")
        text = text.replace("rate_delay_seconds: 15.0", f"rate_delay_seconds: {vt_delay}")
        # Shodan pages sit behind Cloudflare: go slower, and keep the cache between runs.
        text = re.sub(
            r"(shodan:.*?rate_delay_seconds: )1\.0", r"\g<1>4.0", text, count=1, flags=re.S
        )
        text = text.replace("directory: ./.ioc-cache", f"directory: {work.parent / '.ioc-cache'}")
        # Cloudflare in front of some APIs challenges browser-like user agents.
        text = re.sub(
            r"user_agent: \"Mozilla[^\"]*\"", 'user_agent: "IOC-Article-Reporter/1.0"', text
        )
    else:
        text = text.replace("whois.cymru.com", "127.0.0.1").replace("port: 43", "port: 9")
        text = re.sub(r"https?://[^\s\"']+", "http://127.0.0.1:9", text)
        text = re.sub(r"(rate_delay_seconds:\s*)[0-9.]+", r"\g<1>0", text)
        text = text.replace("retries: 2", "retries: 0").replace("enabled: true", "enabled: false")
    config = work / "config.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manuel-dir", required=True, type=Path)
    parser.add_argument("--ioc-csv-root", required=True, type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--live", action="store_true", help="really query the IOC providers")
    parser.add_argument(
        "--vt-delay", type=float, default=15.0, help="seconds between VirusTotal requests"
    )
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--tail-title", default="Actualité des codes et infrastructures iraniennes")
    args = parser.parse_args()

    manuel_dir = args.manuel_dir.resolve()
    if not (manuel_dir / "main.typ").is_file():
        raise SystemExit(f"{manuel_dir} does not look like a typst_manuel directory")
    work = (args.out_dir or manuel_dir.parent / f"{manuel_dir.name}_complet").resolve()
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(manuel_dir, work, ignore=shutil.ignore_patterns("*.bak", "*.pdf"))
    reports = work / "reports"
    reports.mkdir(exist_ok=True)
    for icon in (PIPELINE / "pays-YYMM_template" / "reports").glob("*.png"):
        shutil.copy2(icon, reports / icon.name)

    numbers = write_article_csvs(work, manuel_dir, args.ioc_csv_root.resolve())
    env = dict(os.environ)
    if args.live:
        env.update({k: v for k, v in load_env(args.env_file).items() if k not in env})
        missing = [f"{name} ({var})" for var, name in SECRET_VARS.items() if not env.get(var)]
        if missing:
            print("WARNING keys missing, these providers stay empty: " + ", ".join(missing))
    else:
        print("Offline stub: no external request, tables will be empty. Use --live to enrich.")
    config = write_config(work, args.live, args.vt_delay)

    reporter = [
        sys.executable,
        str(Path(__file__).with_name("run_ioc_reporter.py")),
        "--config",
        str(config),
        "--input",
        str(work / "articles"),
        "--reports-dir",
        str(reports),
        "--sandbox-icons-dir",
        str(reports),
        "--output-csv",
        str(work / "out" / "iocs_normalized.csv"),
        "--no-cache" if not args.live else "--fail-fast",
    ]
    subprocess.run(reporter, check=True, env=env, cwd=work)

    main_typ = (work / "main.typ").read_text(encoding="utf-8").rstrip() + "\n\n#pagebreak()\n"
    main_typ += TAIL_PAGE.replace("{title}", args.tail_title)
    (work / "main_complet.typ").write_text(main_typ, encoding="utf-8")

    typst = os.environ.get("TYPST", "typst")
    output = work / "bulletin_manuel_complet.pdf"
    subprocess.run(
        [
            typst,
            "compile",
            "--root",
            ".",
            "--font-path",
            "fonts",
            "--ignore-system-fonts",
            "--creation-timestamp",
            "0",
            "main_complet.typ",
            output.name,
        ],
        check=True,
        cwd=work,
        env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    print(f"articles: {', '.join(numbers)}\nécrit : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
