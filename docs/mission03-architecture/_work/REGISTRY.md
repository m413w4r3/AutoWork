# Mission 03 — registre compact des lots (parent)

**SHA étudié :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Branche :** `mission03/architecture-design`
**Date :** 2026-10-24

| Lot | Owner | Agent ID | Statut | Artefact | Décision | Risque | Dépendance suivante |
|---|---|---|---|---|---|---|---|
| M1A — flux effets externes | Luna xhigh | `7dbbbcd5-aa9e-4ef1-83ac-71dfee34a1a5` | artefact reçu | `_work/M1A_EXTERNAL_EFFECTS.md` | aucun point de décision sérialisé ; advisor classé EXTERNAL_READ à tort | contournements directs, course RUNNING→NEEDS_REVIEW, portée Bridge | M5, M6-S3 |
| M1B — flux dossier advisor | Luna xhigh | `23419893-2f03-40db-b936-17a1dfd98249` | artefact reçu | `_work/M1B_ADVISOR_FLOW.md` | refus P10 effaçable ; métadonnées échantillon à vérifier | deux parseurs ; refs dispersées | M5, M6-advisor |
| M1C — flux contexte/audit | Luna xhigh | `a5713606-ff41-4bcc-9b5b-0402935a812a` | artefact reçu | `_work/M1C_CONTEXT_AUDIT_FLOW.md` | KEEP + NARROW_FIX (projections full/paged, reader PG réel) | recompositions divergentes full vs paged ; lecteur PG non testé | M5, M7 |
| M1D — flux publication | Luna xhigh | `5e5c4c7d-f457-48a5-a36b-c48eab76d046` | artefact reçu | `_work/M1D_PUBLICATION_FLOW.md` | KEEP + NARROW_FIX (complétude) ; edition DEFER | rendu sans oracle universel de complétude | M5 |
| M2A — surface tests effets externes | Luna xhigh | `cb86f1d8-d50b-4b25-ac89-9abd1cb40074` | artefact reçu | `_work/M2A_TEST_SURFACE_EXTERNAL.md` | 0/10 oracles de permis au consume ; 18 cas PG réels | trous critiques #1–#7, #10 | M5, M6-S3, M7 |
| M2B — surface tests advisor/audit/pub | Luna xhigh | `db9f803a-bf79-4321-b09a-882533c13267` | artefact reçu | `_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md` | 4 oracles manquants majeurs (S2, PG audit, pagination chaînée, PDF final) | F6 couvert au transport factice ; reparse couvert au seam outil | M5, M7 |
| M3 — diagnostic S3 | Luna xhigh | `7869d248-be49-479c-af31-c8fb97c04fb1` | artefact reçu | `_work/M3_S3_DIAGNOSIS.md` | **S3_BLOCKED_DESIGN** | consume non sérialisé ; 7 décisions manquantes | M5, M6-S3, M7 |
| M4A — diagnostic advisor/disclosure | Luna xhigh | `a9939d20-16db-4776-8cd9-b8d334ef287b` | artefact reçu | `_work/M4_ADVISOR_DIAGNOSIS.md` | dossier KEEP ; S2 NARROW_FIX ; parseur REPLACE_CANDIDATE | compatibilité legacy v2 | M5, M6-advisor |
| M4C — diagnostic audit/contexte | Luna xhigh | `15dbf42c-0179-41d7-9ce3-aa7b8bafa2ae` | artefact reçu | `_work/M4_AUDIT_DIAGNOSIS.md` | aucun REPLACE ; 2 NARROW_FIX ; page identity BLOCKED_DESIGN | contrat de snapshot de page non tranché | M5, M7 |
| M5 — sélection des seams | parent | — | fait | ce registre + `DESIGN_OPTIONS.md` | S3 + advisor retenus ; max 2 seams | — | M6 |
| D6-S3-A — minimal | Luna xhigh | `880252bb-7bd5-42eb-b8e9-1ad15acea0c6` | artefact reçu | `_work/D6_S3_A_MINIMAL.md` | gateway 1 requête/consume | inventaire transports | M6 arbitrage |
| D6-S3-B — flexibilité | Luna xhigh | `4e9023bc-7377-4010-a919-cb467f2326ea` | artefact reçu | `_work/D6_S3_B_FLEX.md` | matrice variantes + budget | complexité matrice | M6 arbitrage |
| D6-S3-C — migration | Luna xhigh | `35a32850-caac-4342-bcdf-656fa3a63e2c` | artefact reçu | `_work/D6_S3_C_MIGRATION.md` | send_one + expand/contract | claim non préparé | M6 arbitrage |
| D6-S3-D — fiabilité | Luna xhigh | `eb077ce6-1f8a-4386-8ca6-8877ee2b4b5f` | artefact reçu | `_work/D6_S3_D_RELIABILITY.md` | machine d'état + events | ordre locks vs tables réelles | M6 arbitrage |
| D6-ADV-A — minimal | Luna xhigh | `c245b0e4-3ac5-4123-85f9-593bcf589814` | artefact reçu | `_work/D6_ADVISOR_A_MINIMAL.md` | 2 opérations prepare/parse | corpus v2/v3 incomplet | M6 arbitrage |
| D6-ADV-B — flexibilité | Luna xhigh | `78df9e3f-12c2-4b90-9a4d-e8ec8781b718` | artefact reçu | `_work/D6_ADVISOR_B_FLEX.md` | 3 opérations + matrice variantes | contexte legacy/Suricata | M6 arbitrage |
| D6-ADV-C — migration | Luna xhigh | `3e28fd34-4928-4477-8486-b638bae27c3f` | artefact reçu | `_work/D6_ADVISOR_C_MIGRATION.md` | hybride A+C+B sélectionné provisoirement | contexte historique v2/v3 incomplet | R7 |
| R7-ADV — revue indépendante advisor | Luna xhigh | `afb533fc-e73c-4025-b753-c251505d87c2` | **REVISE** | `_work/R7_ADVISOR_REVIEW.md` | 3 HIGH : refs, octets, autorisation ; + migration/batching | à refermer en R2 | R7-ADV-R2 |
| R7-ADV-R2 — deuxième revue advisor | Luna xhigh | `410f3bdc-1a5c-40b7-a9fc-abd30bda12d7` | **REVISE (cutover)** ; prepare slice OK | `_work/R7_ADVISOR_REVIEW_R2.md` | refs/bytes CLOSED ; auth/contexte/migration PARTIAL | slice pure `prepare` autorisée | M8 |
| R7-S3 — revue indépendante S3 | Luna xhigh | `07cc91db-e9a7-4c21-9b3c-c109f75fd64f` | **REVISE** | `_work/R7_S3_REVIEW.md` | 2 BLOCKER (transport caller, locks) + 2 HIGH (claim/reuse, recovery/budget) | à refermer en R2 | R7-S3-R2 |
| R7-S3-R2 — deuxième revue S3 | Luna xhigh | `c4dd63ac-422b-4aa8-a73c-6938be91b283` | **REVISE ; gate NOT MET** | `_work/R7_S3_REVIEW_R2.md` | F1 CLOSED ; F2–F4 PARTIAL ; N1–N4 HIGH | pas de production S3 ; slice test-only conditionnel | M8 |

## Règles du registre

- Le parent est seul intégrateur et rédacteur des six livrables.
- Les agents M1/M2/M3/M4 sont en lecture seule ; leur seul écrit autorisé est leur artefact `_work/`.
- Aucun agent ne crée de sous-flotte, ne commit, ne merge, ne pousse.
- Un lot bloqué n'arrête pas les autres lots indépendants.
