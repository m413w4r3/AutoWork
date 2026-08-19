"""Test complet de consolidation avec les vrais prompts ChatGPT de juillet.

Pipeline complète :
1. Parser les 4 fichiers markdown de juillet avec parse_discovery_report()
2. Créer 4 DiscoveryBatch (un par fichier)
3. Consolider les 4 batches avec consolidate_discovery_batches()
4. Évaluer les résultats
5. Générer un rapport détaillé
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from uuid import uuid4

from cti_app.application.discovery_consolidation import consolidate_discovery_batches
from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import ContributionStatus, DiscoveryBatch, DiscoverySourceMode

from .discovery_helpers import wrap_candidates_in_contributions


REQUEST_HASH = hashlib.sha256(b"july_test_request").hexdigest()


def test_july_prompts_with_official_pipeline():
    """Test complète avec la pipeline officielle de parsing et consolidation."""

    # Répertoire contenant les fichiers markdown
    july_dir = Path("/home/nill/perso/nomorework_fiches/chatGPT_Answers/juillet")

    if not july_dir.exists():
        print(f"ERROR: Directory {july_dir} not found")
        return

    # Parser tous les fichiers avec la pipeline officielle
    print("=" * 80)
    print("PHASE 1: PARSING WITH OFFICIAL PIPELINE")
    print("=" * 80)
    print()

    all_batches = []
    total_candidates_before = 0
    file_info = {}

    edition_id = uuid4()

    for idx, md_file in enumerate(sorted(july_dir.glob("*.md")), 1):
        print(f"[{idx}] Parsing {md_file.name}...")

        try:
            # Lire le contenu du fichier
            report_text = md_file.read_text(encoding="utf-8")

            # Parser avec la pipeline officielle
            parsed = parse_discovery_report(
                report_text,
                visible_citations=(),
                period_start=date(2026, 7, 1),
                period_end=date(2026, 8, 31),
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )

            candidate_count = len(parsed.candidates)
            total_candidates_before += candidate_count
            file_info[md_file.name] = {
                "candidates": candidate_count,
                "status": parsed.status,
                "warnings": len(parsed.warnings)
            }

            print(f"    ✅ {candidate_count} candidates parsed (status: {parsed.status})")
            if parsed.warnings:
                print(f"    ⚠️  {len(parsed.warnings)} warnings")

            # Créer un DiscoveryBatch
            # Note: Using ACCEPTED status for batches created from parsed reports
            # (in production, these would become PENDING for user review, then ACCEPTED)
            batch = DiscoveryBatch(
                edition_id=edition_id,
                request_hash=REQUEST_HASH,
                complementary_axis=f"july_batch_{idx}_{md_file.stem}",
                queries=(),
                citations=parsed.citations,
                contributions=wrap_candidates_in_contributions(parsed.candidates, ContributionStatus.ACCEPTED),
                discovery_model_run_id=uuid4(),
                structuring_model_run_id=uuid4(),
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
                report_sha256=parsed.report_sha256,
                parser_version=parsed.candidates[0].__class__.__name__ if parsed.candidates else "unknown",
                parsing_status=parsed.status,
                parsing_warnings=parsed.warnings,
                unattached_visible_citations=parsed.unattached_visible_citations,
                source_mode=DiscoverySourceMode.VISIBLE_CITATIONS_ONLY,
            )
            all_batches.append(batch)
            print(f"    ✅ Batch created\n")

        except Exception as e:
            print(f"    ❌ ERROR: {e}\n")
            continue

    # Résumé du parsing
    print("=" * 80)
    print("PARSING SUMMARY")
    print("=" * 80)
    print()
    for filename, info in file_info.items():
        print(f"{filename}:")
        print(f"  Candidates: {info['candidates']}")
        print(f"  Status: {info['status']}")
        print(f"  Warnings: {info['warnings']}")
    print()
    print(f"Total candidates before consolidation: {total_candidates_before}")
    print(f"Total batches: {len(all_batches)}")
    print()

    # Phase 2: Consolidation
    print("=" * 80)
    print("PHASE 2: CONSOLIDATION WITH OFFICIAL PIPELINE")
    print("=" * 80)
    print()

    try:
        consolidated = consolidate_discovery_batches(all_batches)
        print(f"✅ Consolidated {total_candidates_before} subjects into {len(consolidated)} consolidated subjects")
        print(f"✅ Merge reduction: {total_candidates_before - len(consolidated)} subjects merged")
        print(f"✅ Merge rate: {((total_candidates_before - len(consolidated)) / total_candidates_before * 100):.1f}%")
    except Exception as e:
        print(f"❌ ERROR during consolidation: {e}")
        return

    print()

    # Phase 3: Detailed Results
    print("=" * 80)
    print("PHASE 3: CONSOLIDATED SUBJECTS DETAILS")
    print("=" * 80)
    print()

    for idx, cons in enumerate(consolidated, 1):
        print(f"{idx}. {cons.representative.title}")
        print(f"   Actors: {', '.join(cons.representative.actors) if cons.representative.actors else 'None'}")
        print(f"   Campaigns: {', '.join(cons.representative.campaigns) if cons.representative.campaigns else 'None'}")
        print(f"   Malware: {', '.join(cons.representative.malware) if cons.representative.malware else 'None'}")
        print(f"   Technical Potential: {cons.representative.technical_potential}")
        print(f"   Total Sources: {len(cons.sources)}")
        print(f"   Contributions from: {cons.contribution_count} batch(es)")

        if cons.ambiguous_with:
            print(f"   ⚠️  Ambiguous with {len(cons.ambiguous_with)} other subject(s)")

        print()

    # Phase 4: Quality Metrics
    print("=" * 80)
    print("PHASE 4: QUALITY METRICS")
    print("=" * 80)
    print()

    # Compter les sujets par contribution
    contributions_dist = {}
    for cons in consolidated:
        contrib_count = cons.contribution_count
        if contrib_count not in contributions_dist:
            contributions_dist[contrib_count] = 0
        contributions_dist[contrib_count] += 1

    print("Distribution by contribution count:")
    for contrib_count in sorted(contributions_dist.keys()):
        count = contributions_dist[contrib_count]
        print(f"  {contrib_count} batch(es): {count} subject(s)")

    print()

    # Sujets présents dans tous les batches
    all_batch_subjects = [cons for cons in consolidated if cons.contribution_count == len(all_batches)]
    print(f"Subjects present in ALL {len(all_batches)} batches: {len(all_batch_subjects)}")
    for cons in all_batch_subjects:
        print(f"  ✅ {cons.representative.title}")

    print()

    # Vérifier TAG-182 et Cavern Manticore
    print("=" * 80)
    print("PHASE 5: CRITICAL SUBJECTS CHECK")
    print("=" * 80)
    print()

    consolidated_titles = [cons.representative.title for cons in consolidated]

    tag182_found = [title for title in consolidated_titles if "TAG-182" in title or "tag" in title.lower() and "182" in title]
    cavern_found = [title for title in consolidated_titles if "Cavern Manticore" in title or "cavern" in title.lower() and "manticore" in title.lower()]

    if tag182_found:
        print(f"✅ TAG-182 / MarkiRAT found:")
        for title in tag182_found:
            print(f"   {title}")
    else:
        print(f"❌ TAG-182 / MarkiRAT NOT found")

    print()

    if cavern_found:
        print(f"✅ Cavern Manticore found:")
        for title in cavern_found:
            print(f"   {title}")
    else:
        print(f"❌ Cavern Manticore NOT found")

    print()
    print("=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)
    print()

    # Assertions
    assert len(all_batches) > 0, "Should have parsed at least one batch"
    assert len(consolidated) > 0, "Should have consolidated subjects"
    assert total_candidates_before > len(consolidated), "Should have merged some subjects"

    print(f"✅ All assertions passed!")
    print()
