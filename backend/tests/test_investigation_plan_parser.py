"""Tolerant ``investigation-plan.v1`` text parser (ADR-0011 D2/D8, lot 04A2).

Bridge never guarantees JSON: these oracles feed realistic, deliberately messy
French and English plan prose (Markdown headings, bold labels, numbered leads,
fenced JSON, partial fields) and require a deterministic, per-field partial
parse.  An unstructured answer is never promoted: it stays advisory and
``to_plan`` refuses to build a canonical version.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from cti_app.application.investigation_plan_parser import (
    LEAD_FIELDS,
    PlanTextParseStatus,
    parse_investigation_plan_text,
)

INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000401")

FRENCH_MESSY_PLAN = """[UNTRUSTED ADVISORY]
Voici une proposition de plan d'investigation, à valider par l'analyste.

## Piste 1 — Vérifier la revendication de double chargeur

**Objectif** : Confirmer que le chargeur télécharge un second étage.
**Justification CTI** : L'article affirme une chaîne en deux étapes (S1P2).
**Claims** : S1P2, S1P3
**Preuves de départ** : 6f1f0c0a9b7e4d2c8a5b3e1f0d9c8b7a66554433221100ffeeddccbbaa998877
**Échantillons** : 1111111111111111111111111111111111111111111111111111111111111111
**Outils** : pe.strings, ghidra.decompile
**Préconditions** : échantillon disponible en quarantaine
**Intérêt** : élevé
**Critères de conclusion** : deux gates réseau indépendants prouvés par les octets
**Limites** : pas d'exécution hors profil confiné

## Piste 2 — Cartographier le protocole C2

Objectif: reconstruire les trames échangées
Justification CTI: le protocole C2 n'est pas documenté
Claims: S2P1
Preuves de départ: observation 9d4c2b6a-1111-2222-3333-444455556666
Échantillons: 2222222222222222222222222222222222222222222222222222222222222222
Intérêt: moyen
Critères de conclusion: une trame complète rejouable
Dépend de: Piste 1

### Lead 3 — Test a candidate YARA rule

- Objective: Test the candidate rule against the seed sample
- CTI justification: The article publishes no detection.
- Claim refs: S3P1
- Start evidence refs: observation 7e5d3c2b-1111-2222-3333-444455556666
- Sample refs: 3333333333333333333333333333333333333333333333333333333333333333
- Tools: yara, yara.compile
- Preconditions: rule candidate drafted
- Interest: low
- Conclusion criteria: the rule compiles and matches the seed without goodware hits
- Limits: bounded goodware corpus
- Depends on: Lead 2

### Lead 4 — Questions restantes

Objectif: lister les questions sans réponse.
"""

ENGLISH_FENCED_JSON_PLAN = """
I can propose this plan:

```json
{
  "leads": [
    {
      "lead_id": "L1",
      "title": "Trace the loader gate",
      "objective": "Prove the second-stage download from bytes.",
      "cti_justification": "The article claims a two-stage chain.",
      "claim_refs": ["S1P2"],
      "start_evidence_refs": ["obs-1"],
      "sample_refs": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
      "tools": ["pe.strings"],
      "preconditions": ["sample quarantined"],
      "interest": "high",
      "conclusion_criteria": "Two independent gates are proven.",
      "limits": "No execution outside the confined profile."
    }
  ]
}
```

Tell me if you want the plan reordered.
"""

UNPARSED_PLAN = """Bonjour,

Je ne peux pas produire un plan structuré pour le moment : la question reste
ambigüe et les éléments fournis ne permettent pas de choisir des vérifications
discriminantes. Merci de préciser l'objectif et les observations de départ.
"""


def _fields(lead_status) -> set[str]:
    return set(lead_status.present_fields)


def test_twelve_lead_fields_partial_parse() -> None:
    result = parse_investigation_plan_text(FRENCH_MESSY_PLAN)

    assert result.parse_status is PlanTextParseStatus.PARTIALLY_PARSED
    assert len(result.leads) == 4
    assert result.leads[0].title == "Vérifier la revendication de double chargeur"
    assert result.leads[0].lead_id == "lead-1"
    assert result.leads[0].objective.startswith("Confirmer que le chargeur")
    assert result.leads[0].claim_refs == ("S1P2", "S1P3")
    assert result.leads[0].start_evidence_refs == (
        "6f1f0c0a9b7e4d2c8a5b3e1f0d9c8b7a66554433221100ffeeddccbbaa998877",
    )
    assert result.leads[0].tools == ("pe.strings", "ghidra.decompile")
    assert result.leads[0].preconditions == ("échantillon disponible en quarantaine",)
    assert result.leads[0].interest == "élevé"
    assert result.leads[0].limits == "pas d'exécution hors profil confiné"

    first_status = result.lead_statuses[0]
    assert _fields(first_status) == set(LEAD_FIELDS) - {"depends_on"}
    assert first_status.missing_fields == ()

    second = result.leads[1]
    assert second.lead_id == "lead-2"
    assert second.title == "Cartographier le protocole C2"
    assert second.depends_on == ("lead-1",)
    second_status = result.lead_statuses[1]
    assert set(second_status.missing_fields) == {"tools", "preconditions", "limits"}

    third = result.leads[2]
    assert third.lead_id == "lead-3"
    assert third.title == "Test a candidate YARA rule"
    assert third.depends_on == ("lead-2",)
    assert third.interest == "low"
    assert "tools" in result.lead_statuses[2].present_fields

    fourth = result.leads[3]
    assert fourth.title == "Questions restantes"
    assert set(result.lead_statuses[3].missing_fields) > {"tools", "limits"}

    plan = result.to_plan(investigation_id=INVESTIGATION_ID, version=1)
    assert plan is not None
    assert plan.investigation_id == INVESTIGATION_ID
    assert plan.version == 1
    assert len(plan.leads) == 4
    assert plan.content_sha256 == hashlib.sha256(plan.canonical_bytes).hexdigest()


def test_fenced_json_plan_is_parsed() -> None:
    result = parse_investigation_plan_text(ENGLISH_FENCED_JSON_PLAN)

    assert result.parse_status is PlanTextParseStatus.PARSED
    assert len(result.leads) == 1
    lead = result.leads[0]
    assert lead.lead_id == "L1"
    assert lead.title == "Trace the loader gate"
    assert lead.claim_refs == ("S1P2",)
    assert lead.sample_refs == ("a" * 64,)
    assert result.lead_statuses[0].missing_fields == ()


def test_unparsed_plan_stays_advice() -> None:
    for text in (UNPARSED_PLAN, "", "[UNTRUSTED ADVISORY]\n\n"):
        result = parse_investigation_plan_text(text)
        assert result.parse_status is PlanTextParseStatus.UNPARSED
        assert result.leads == ()
        assert result.to_plan(investigation_id=INVESTIGATION_ID, version=1) is None


def test_partial_lead_gets_a_deterministic_title_fallback() -> None:
    text = """## Lead A — Vérifier le hash

Objectif: recalculer le SHA-256
Limites: lecture seule
"""
    first = parse_investigation_plan_text(text)
    second = parse_investigation_plan_text(text)
    assert first.parse_status is PlanTextParseStatus.PARTIALLY_PARSED
    assert first.leads[0].title == "Vérifier le hash"
    assert first.leads == second.leads
    assert first.lead_statuses == second.lead_statuses


def test_identifiers_are_not_corrupted_by_markdown_cleanup() -> None:
    text = """## Lead 1 — Inspecter le chargeur

Objectif: analyser cache_loader_payload
Outils: vt_file_report, file_read, **yara_compile**
Préconditions: **sample_quarantined**
"""
    result = parse_investigation_plan_text(text)

    lead = result.leads[0]
    assert lead.objective == "analyser cache_loader_payload"
    assert lead.tools == ("vt_file_report", "file_read", "yara_compile")
    assert lead.preconditions == ("sample_quarantined",)
