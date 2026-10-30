# Lot 006 — checkpoint C et dernière attente de stabilité

Instantané : 2026-10-29 18:07:38 UTC. `FINAL_CHECKPOINT_C.md` existe, SHA-256 `09101e5f0b5d1b35ec77a4ae6ca106f1aa31d3b799b6725d2a33313632ff4a53`. Dernière entrée journal `c116-final-opinion` à 17:59:04.416242Z; aucune nouvelle entrée pendant 8 min 33.584 s au snapshot. Le seuil des 15 min n’est pas encore atteint. `relay-status.json`: queue et in_flight vides.

## Faits finaux de C

- C finit sur l’état API v61/SHA `ec20349e4309b8bcf1138c87cc177bef9623fb3431b648751d660c273d5fbe2d`, investigation toujours `running`, sans job ni stop; G2 v119 approuvé/non révoqué, TLP clear, `external_llm_allowed=true`. `c115` relit les 3 findings, tous `qualified`; `c116` confirme opinion v3, SHA `4af95801ce005ce87c1bae4e2c19475259a4bef3da3327c528e010ff8d033f0e`, deux blocs de 1 709 et 915 caractères, 13 refs chacun, statut draft. Aucun approval/publication.
- Bridge a trois POST C (`c100`, `c111`, `c113`), aucun texte de revue ni transport confirmé. c100 et c111 : adapter pré-soumission `bridge_server_error`, `confirmed_non_execution=true`; c113 (`15519094-bf09-4e8c-b058-dfe3571c807d`) : `conversation_unavailable`, `retry=false`, `prior_submission_made=false`, 0 unité transmise, 0 tour consommé, 0 octet de réponse Bridge. Le payload c113 était READY en préflight; aucun résultat de reviewer. Ses refus ne sont pas des avis sur le draft.
- Budget Bridge final : 1 tour historique consommé, 2 réservés, 197 restants; tool_seconds 380 consommées/7 200 réservées; contexte 9 151 tokens/2 941 079 réservés et 36 604 octets/11 764 313 réservés. c113 a libéré sa réservation; deux réservations restent alors que c100/c111 confirment la non-exécution. Coût fournisseur de C non encouru/confirmé; coût monétaire du tour historique inconnu.
- Le statut c114 dit `ambiguous_invocations=[]` et réconciliation vide, mais `invocations` contient encore 5 lignes `needs_review`: trois VT anciennes (`1c426…`, `845d…`, `1a183…`) et deux consultations Bridge (`26e2d980…`, `07cb85d4…`). Les détails Bridge confirment non-exécution; les réservations et le statut global ne sont pas réconciliés. Le premier rapport de C traitait aussi ces états séparément.
- Les trois findings restent correctement qualifiés; la co-association DNS est reconnue comme directement observée, l’achat reste hypothèse; la configuration exacte/mesure hôte ETW manque; les analyses de deux samples sont partielles/candidates, et aucune attribution organisationnelle n’est établie. Le checkpoint maintient ces limites et l’absence de tests/approbation/publication.

## Verdict provisoire

Aucun changement de contenu ne paraît nécessaire avant le final; attendre seulement la stabilité des fichiers. Faits de checkpoint tirés de `FINAL_CHECKPOINT_C.md`; budgets/statuts corroborés par `c114-final-status.json` et réponses c113/c115/c116. SHA journal au dernier relevé précédent : `c2ca001cd65a3b6f1208779870ad967c3722bf14f40c29fc8227e6f856aed7e2`.
