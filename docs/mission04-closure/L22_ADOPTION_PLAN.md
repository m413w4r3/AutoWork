# L22 — plan d'adoption advisor (préparation uniquement)

**Statut :** `PREP_ONLY` — aucun cutover production.
**Gate :** `ADVISOR_CUTOVER_BLOCKED_BY_S3`.
**Base :** L21 `5707a8d9` (parseur sémantique v4 partagé), L20 `prepare`
inchangé et sans autorité de soumission.

## 1. État actuel vérifié

- Le parseur v4 est un module local partagé
  (`advisor_semantic_parser.py`, schémas `advisor-response.v4` /
  `advisor-source-parser.v4`). Il ne décide pas de G2 et n'appelle pas Bridge.
- Les chemins **parse** et **reparse** acceptent explicitement v4 dans les
  tests et les tools hors réseau. Le reparse lit uniquement l'artefact persisté
  et le contexte enregistré ; aucun nouveau collecteur, aucune relecture
  externe, aucun envoi.
- Le wiring de soumission courant reste **v3 par défaut** et autoritaire.
  Aucun dual-send, aucun shadow parse, aucun changement de source canonique,
  aucune réécriture de blobs, refs ou logs historiques.
- Les tests d'adoption existants couvrent la sélection explicite v4 via
  `_parse_advisor_output` et le reparse persisté via
  `ReparsePersistedAdvisorOutputTool`.

## 2. Conditions d'entrée d'un cutover futur

Un cutover ne peut être planifié qu'après :

1. un verdict S3 **approuvé** et ses oracles, en particulier la revalidation
   d'autorisation au moment du **consume** ;
2. une preuve que le caller de production utilise la même identité
   d'autorisation qu'au moment de la soumission, sans élargissement G2/TLP ;
3. une revue indépendante de stabilité du caller réseau ;
4. une politique explicite de conservation/reparse des artefacts v2/v3
   historiques.

Tant que ces conditions ne sont pas réunies, le gate reste
`ADVISOR_CUTOVER_BLOCKED_BY_S3` et aucun parser v4 ne doit devenir la source
canonique de production.

## 3. Étapes d'adoption proposées

1. **S3 approuvé** : implémenter la revalidation d'autorisation au consume,
   avec tests de refus (G2/G3, TLP, `external_llm_allowed`, `do_not_submit`,
   révocation) et preuve d'absence d'émission supplémentaire.
2. **Adoption hors production** : rejouer les fixtures d'adoption sur le vrai
   caller avec le parseur v4, comparer statuts/refs/bytes/hash, sans écriture
   de nouvelle conclusion.
3. **Bascule contrôlée** : sélectionner v4 pour le caller autoritaire, conserver
   un rollback explicite et auditable, et vérifier zéro dual-send.
4. **Nettoyage différé** : après la période de rétention convenue, supprimer la
   branche de parsing v3 et `_parse_advisor_wire_legacy`. Les blobs, refs et
   logs historiques restent inchangés ; un reparse historique incompatible
   produit un refus explicite et auditable.

## 4. Retraits candidats (non exécutés)

- branche de parsing v3 dans `investigation_advisor_tools.py` ;
- `_parse_advisor_wire_legacy` ;
- tout fallback tolérant qui ne serait plus atteint après la bascule.

Aucun retrait n'est effectué dans Mission 04.

## 5. Preuves à produire avant tout cutover

- tests d'adoption du vrai caller (statuts, `source_ref` versionné exact,
  provenance, bytes/hash, Unicode/CRLF) ;
- tests de refus S3 au consume et de révocation ;
- diff de comportement v3/v4 sur les fixtures approuvées ;
- revue indépendante et autorisation propriétaire explicite.
