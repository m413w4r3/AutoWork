#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "openai>=1.60"]
# ///
"""Runner reproductible pour le benchmark de navigation ctx.py (R88).

Rejoue les requêtes de ``refacto_baseLine/ctx_benchmark.json`` contre
l'index ctx.py courant et calcule, MÉCANIQUEMENT (aucun rang codé en dur),
les métriques :

    first_relevant_rank, top3, top8, files_before_first_hit,
    lines_before_first_hit

et leurs agrégats (hit rates, médianes/moyennes).

Ce script n'implémente AUCUN moteur de recherche : il importe les
primitives existantes de ``scripts/ctx/ctx.py`` (``load_chunks``,
``rank_chunks``, ``select_results``, ``maybe_refresh``) et ne fait que les
appeler avec les mêmes paramètres que la CLI ``ctx.py query``.

La ground truth (requêtes + fichiers "owner" attendus) N'EST PAS dans ce
fichier : elle vit dans ``refacto_baseLine/ctx_benchmark.json``, un chemin
déjà exclu de l'index ctx.py (``EXCLUDE_PREFIXES`` dans ctx.py). La garder
hors de ce runner évite que scripts/ctx/benchmark.py — lui-même indexable —
n'injecte la réponse attendue dans le corpus que le benchmark évalue.
Le classement, les rangs et les métriques sont recalculés à chaque
exécution contre l'index courant.

Exemples :

    # Index lexical frais, puis benchmark en lexical-only :
    env -u BASE_URL -u EMBEDDING_API_KEY \\
        uv run scripts/ctx/ctx.py build --lexical-only
    uv run scripts/ctx/benchmark.py --lexical-only

    # Résumé JSON, sans vérifier les seuils :
    uv run scripts/ctx/benchmark.py --lexical-only --json --no-check
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ctx  # noqa: E402  (après sys.path.insert, cf. import local du module)

# --------------------------------------------------------------------------- ground truth (données externes)
#
# Chargée depuis refacto_baseLine/ctx_benchmark.json (exclu de l'index ctx.py)
# pour ne pas fuiter les queries/owners attendus dans le corpus indexé.
# Ne pas dupliquer ces données ici : modifier le fichier JSON, pas ce runner.

DEFAULT_GROUND_TRUTH_PATH = Path(__file__).resolve().parents[2] / "refacto_baseLine" / "ctx_benchmark.json"


def load_queries(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload["queries"]
    return [
        {
            "id": str(spec["id"]),
            "text": str(spec["text"]),
            "owners": tuple(str(o) for o in spec["owners"]),
        }
        for spec in queries
    ]


# --------------------------------------------------------------------------- mesure

@dataclass(frozen=True)
class QueryResult:
    id: str
    text: str
    owners: tuple[str, ...]
    first_relevant_rank: int | None  # None == MISS
    top3: bool
    top8: bool
    files_before_first_hit: int
    lines_before_first_hit: int
    ranked_paths: tuple[str, ...]


def run_query(
    text: str,
    *,
    model: str,
    k: int,
    per_file: int,
    relative_floor: float,
    lexical_only: bool,
    no_instruct: bool,
    refresh: bool,
) -> list[ctx.Ranked]:
    """Appelle les primitives ctx.py existantes exactement comme `ctx.py query`."""
    args = SimpleNamespace(
        model=model,
        text=text,
        k=k,
        per_file=per_file,
        path=None,
        lexical_only=lexical_only,
        refresh=refresh,
    )
    ctx.maybe_refresh(args)

    if lexical_only:
        chunks = ctx.load_chunks()
        matrix = __import__("numpy").empty((0, 0), dtype="float32")
    else:
        chunks, matrix, _ = ctx.load_index(model)

    ranked = ctx.rank_chunks(
        chunks,
        matrix,
        text,
        model=model,
        lexical_only=lexical_only,
        no_instruct=no_instruct,
    )
    return ctx.select_results(
        ranked,
        k=k,
        per_file=per_file,
        path_prefixes=None,
        relative_floor=relative_floor,
    )


def score_query(spec: dict[str, object], picked: list[ctx.Ranked]) -> QueryResult:
    owners = spec["owners"]
    assert isinstance(owners, tuple)

    first_relevant_rank: int | None = None
    for rank, item in enumerate(picked, start=1):
        if item.chunk.path in owners:
            first_relevant_rank = rank
            break

    top3 = first_relevant_rank is not None and first_relevant_rank <= 3
    top8 = first_relevant_rank is not None and first_relevant_rank <= 8

    seen_files: list[str] = []
    files_before_first_hit = 0
    lines_before_first_hit = 0
    hit_seen = False
    for item in picked:
        if item.chunk.path not in seen_files:
            seen_files.append(item.chunk.path)
        lines_before_first_hit += item.chunk.end - item.chunk.start + 1
        if item.chunk.path in owners:
            files_before_first_hit = len(seen_files)
            hit_seen = True
            break
    if not hit_seen:
        # MISS : nombre de fichiers distincts / lignes cumulées sur tout le
        # top-k retourné (le top8 par défaut de la spec R67).
        files_before_first_hit = len(seen_files)
        lines_before_first_hit = sum(item.chunk.end - item.chunk.start + 1 for item in picked)

    return QueryResult(
        id=str(spec["id"]),
        text=str(spec["text"]),
        owners=owners,
        first_relevant_rank=first_relevant_rank,
        top3=top3,
        top8=top8,
        files_before_first_hit=files_before_first_hit,
        lines_before_first_hit=lines_before_first_hit,
        ranked_paths=tuple(item.chunk.path for item in picked),
    )


# --------------------------------------------------------------------------- agrégats

def aggregate(results: list[QueryResult]) -> dict[str, object]:
    n = len(results)
    top3_hits = sum(1 for r in results if r.top3)
    top8_hits = sum(1 for r in results if r.top8)
    ranks = [r.first_relevant_rank for r in results if r.first_relevant_rank is not None]
    files_before = [r.files_before_first_hit for r in results]
    lines_before = [r.lines_before_first_hit for r in results]

    return {
        "n_queries": n,
        "top3_hit_rate": top3_hits / n if n else 0.0,
        "top8_hit_rate": top8_hits / n if n else 0.0,
        "median_first_relevant_rank": statistics.median(ranks) if ranks else None,
        "mean_first_relevant_rank": statistics.mean(ranks) if ranks else None,
        "n_miss": n - len(ranks),
        "median_files_before_first_hit": statistics.median(files_before) if files_before else None,
        "mean_files_before_first_hit": statistics.mean(files_before) if files_before else None,
        "median_lines_before_first_hit": statistics.median(lines_before) if lines_before else None,
        "mean_lines_before_first_hit": statistics.mean(lines_before) if lines_before else None,
    }


# --------------------------------------------------------------------------- rapport

def render_markdown(results: list[QueryResult], agg: dict[str, object], verdicts: dict[str, bool]) -> str:
    lines = ["| Q | rank | top3 | top8 | files-before-hit | lines-before-hit |", "|---|---|---|---|---|---|"]
    for r in results:
        rank = r.first_relevant_rank if r.first_relevant_rank is not None else "MISS"
        lines.append(
            f"| {r.id} | {rank} | {'PASS' if r.top3 else 'FAIL'} | {'PASS' if r.top8 else 'FAIL'} | "
            f"{r.files_before_first_hit} | {r.lines_before_first_hit} |"
        )

    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| top3_hit_rate | {agg['top3_hit_rate']:.1%} |")
    lines.append(f"| top8_hit_rate | {agg['top8_hit_rate']:.1%} |")
    lines.append(f"| median_first_relevant_rank (MISS excl.) | {agg['median_first_relevant_rank']} |")
    lines.append(f"| mean_first_relevant_rank (MISS excl.) | {agg['mean_first_relevant_rank']} |")
    lines.append(f"| n_miss | {agg['n_miss']} |")
    lines.append(f"| median_files_before_first_hit | {agg['median_files_before_first_hit']} |")
    lines.append(f"| mean_files_before_first_hit | {agg['mean_files_before_first_hit']} |")
    lines.append(f"| median_lines_before_first_hit | {agg['median_lines_before_first_hit']} |")
    lines.append(f"| mean_lines_before_first_hit | {agg['mean_lines_before_first_hit']} |")

    lines.append("")
    for name, ok in verdicts.items():
        lines.append(f"- {name}: {'PASS' if ok else 'FAIL'}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(prog="ctx-benchmark", description=__doc__)
    parser.add_argument("--model", default=ctx.DEFAULT_MODEL)
    parser.add_argument("-k", type=int, default=ctx.DEFAULT_K)
    parser.add_argument("--per-file", type=int, default=ctx.DEFAULT_PER_FILE)
    parser.add_argument("--relative-floor", type=float, default=0.72)
    parser.add_argument("--no-instruct", action="store_true")
    parser.add_argument(
        "--lexical-only",
        action="store_true",
        help="Mode gelé par R67_benchmark_spec.md : pas d'embedding, index lexical seul.",
    )
    parser.add_argument(
        "--refresh",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rafraîchit l'index s'il est obsolète avant de requêter (défaut: oui).",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON machine-readable au lieu du Markdown.")
    parser.add_argument("--no-check", action="store_true", help="Ne pas appliquer les seuils (toujours code 0).")
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=DEFAULT_GROUND_TRUTH_PATH,
        help="Chemin du fichier JSON de cas (défaut: refacto_baseLine/ctx_benchmark.json).",
    )

    parser.add_argument("--min-top3", type=float, default=0.80, help="Seuil top3_hit_rate (défaut: 0.80).")
    parser.add_argument("--min-top8", type=float, default=0.833, help="Seuil top8_hit_rate (défaut: 0.833).")
    parser.add_argument(
        "--max-median-files-before-hit",
        type=float,
        default=3.0,
        help="Seuil median_files_before_first_hit (défaut: 3).",
    )

    args = parser.parse_args()

    queries = load_queries(args.ground_truth)

    results = [
        score_query(
            spec,
            run_query(
                str(spec["text"]),
                model=args.model,
                k=args.k,
                per_file=args.per_file,
                relative_floor=args.relative_floor,
                lexical_only=args.lexical_only,
                no_instruct=args.no_instruct,
                refresh=args.refresh,
            ),
        )
        for spec in queries
    ]
    agg = aggregate(results)

    verdicts = {
        f"top3_hit_rate >= {args.min_top3:.1%}": agg["top3_hit_rate"] >= args.min_top3,
        f"top8_hit_rate >= {args.min_top8:.1%}": agg["top8_hit_rate"] >= args.min_top8,
        f"median_files_before_first_hit <= {args.max_median_files_before_hit:g}": (
            agg["median_files_before_first_hit"] is not None
            and agg["median_files_before_first_hit"] <= args.max_median_files_before_hit
        ),
    }

    if args.json:
        payload = {
            "lexical_only": args.lexical_only,
            "k": args.k,
            "per_file": args.per_file,
            "relative_floor": args.relative_floor,
            "queries": [
                {
                    "id": r.id,
                    "text": r.text,
                    "owners": list(r.owners),
                    "first_relevant_rank": r.first_relevant_rank,
                    "top3": r.top3,
                    "top8": r.top8,
                    "files_before_first_hit": r.files_before_first_hit,
                    "lines_before_first_hit": r.lines_before_first_hit,
                    "ranked_paths": list(r.ranked_paths),
                }
                for r in results
            ],
            "aggregate": agg,
            "verdicts": {name: ok for name, ok in verdicts.items()},
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(results, agg, verdicts))

    if args.no_check:
        return 0
    return 0 if all(verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
