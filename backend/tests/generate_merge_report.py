#!/usr/bin/env python3
"""Script standalone pour générer un rapport détaillé de consolidation.

Usage:
    python generate_merge_report.py > merge_report.txt
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from uuid import uuid4

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    DiscoveryBatch,
    DiscoverySourceMode,
)

# Import de la fonction depuis le test
import sys
sys.path.insert(0, str(Path(__file__).parent))
from test_real_prompts_merge import (
    parse_july_prompt,
    evaluate_merge_results,
    generate_merge_report,
)


def main():
    """Exécute la pipeline de consolidation et génère un rapport."""
    print("Démarrage de la consolidation des sujets de juillet...")
    print()

    july_dir = Path("/home/nill/perso/nomorework_fiches/chatGPT_Answers/juillet")
    REQUEST_HASH = hashlib.sha256(b"july_test_request").hexdigest()

    # Parser tous les fichiers
    all_candidates_by_file = {}
    file_counts = {}

    for md_file in sorted(july_dir.glob("*.md")):
        candidates = parse_july_prompt(str(md_file))
        all_candidates_by_file[md_file.name] = candidates
        file_counts[md_file.name] = len(candidates)
        print(f"OK Parsed {md_file.name}: {len(candidates)} subjects")

    # Créer un DiscoveryBatch par fichier
    edition_id = uuid4()
    batches = []

    print()
    for idx, (filename, candidates) in enumerate(all_candidates_by_file.items()):
        batch = DiscoveryBatch(
            edition_id=edition_id,
            request_hash=REQUEST_HASH,
            complementary_axis=f"july_batch_{idx}",
            queries=(),
            citations=(),
            candidates=candidates,
            discovery_model_run_id=uuid4(),
            structuring_model_run_id=uuid4(),
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
            source_mode=DiscoverySourceMode.VISIBLE_CITATIONS_ONLY,
        )
        batches.append(batch)
        print(f"OK Created batch for {filename}: {len(candidates)} candidates")

    # Consolider les batches
    print()
    consolidated = consolidate_discovery_batches(batches)
    print(f"OK Consolidated {sum(file_counts.values())} subjects into {len(consolidated)} consolidated subjects")

    # Évaluation
    expected_samegroups = {
        "TAG-182 / MarkiRAT": [
            "TAG-182 — diffusion de MarkiRAT",
        ],
        "Cavern Manticore": [
            "Cavern Manticore — framework C2",
        ],
    }

    evaluation = evaluate_merge_results(consolidated, expected_samegroups)

    # Générer et afficher le rapport
    report = generate_merge_report(consolidated, evaluation, file_counts)
    print()
    print(report)

    # Statistiques supplémentaires
    print()
    print("STATISTIQUES DETAILLEES:")
    print()

    # Sujets avec plus de 1 contribution
    multi_contrib = [cons for cons in consolidated if cons.contribution_count > 1]
    print(f"Sujets présents dans plusieurs fichiers: {len(multi_contrib)}")
    for cons in sorted(multi_contrib, key=lambda x: -x.contribution_count):
        print(f"  - {cons.representative.title[:60]}... ({cons.contribution_count} fichiers)")

    print()

    # Sujets avec ambiguïté
    ambiguous = [cons for cons in consolidated if cons.ambiguous_with]
    print(f"Sujets avec ambiguïtés détectées: {len(ambiguous)}")
    for cons in ambiguous[:10]:  # Top 10
        print(f"  - {cons.representative.title[:60]}... ({len(cons.ambiguous_with)} ambiguitys)")

    print()
    print("FIN DU RAPPORT")


if __name__ == "__main__":
    main()
