# Mission 04 closure — décisions du propriétaire et périmètre

**Date d'ouverture :** 2026-10-24
**Baseline de départ vérifiée :** `origin/main@d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Nature :** implémentation locale contrôlée, tests hors ligne, PostgreSQL et
Typst/PDF jetables. Aucun déploiement, aucun run CTI, aucun appel fournisseur,
aucune migration live, aucun push avant les gates de clôture.

## 1. Décisions D1–D11 : traduction contractuelle

| Décision | Traduction dans le code / les contrats | État de clôture |
|---|---|---|
| **D1** — findings courants | Le contexte courant (full et section `findings`) ne retient que la dernière version de chaque finding. Les versions remplacées restent lues par `finding_versions` et l'audit ; aucune suppression en base. | Lot L4 |
| **D2** — ordre des preuves | `evidence_index` : `observed_at ASC, UUID ASC`, `NULLS LAST` documenté. Même sélection, même ordre et mêmes références entre full et pages, indépendamment de la taille de page. | Lot L4 |
| **D3** — curseur périmé | Curseur opaque lié à l'identité/version déterministe du contexte représenté. Si le contexte canonique change, erreur explicite de curseur périmé ; aucun mélange de versions entre pages ; aucun snapshot durable supplémentaire. | Lot L4 |
| **D4** — `context_sha256` | SHA-256 de la représentation canonique complète du contexte, sérialisation/version/champs documentés ; indépendant du découpage ; référencé par la pagination ; pas un hash de page ni un simple état DB. | Lot L4 |
| **D5** — reviewer/approver PDF | `pdf_lineage` expose les identifiants d'acteur canoniques, rôles, décisions et références/version/SHA appropriés. Aucun nom inventé, aucune substitution par le modèle. Métadonnée seule : aucune signature nominative ajoutée au PDF visible. | Lot L5 |
| **D6** — complétude PDF | Style éditorial conservé ; références de preuve traçables ; pas de refonte. Oracle de complétude sémantique et occurrence par occurrence sur le PDF réellement compilé : ni omission, ni double ajout, ni faux positif dû à un texte répété légitimement. `base_publication` et publication avec avis restent distingués ; aucun avis attribué s'il est absent ou non approuvé. | Lot L5 |
| **D7** — échantillons CTI | `ALLOW` par défaut pour octets, contenu, métadonnées, hashes, MIME, taille, statut, moteurs, traces, fragments et associations dans une consultation déjà autorisée. Suppression des interdictions génériques par type/nom de champ/taille. Les vetos explicites restent : G2/G3, `external_llm_allowed=false`, `do_not_submit`, TLP RED, restriction source/propriétaire, credentials opérationnels AutoWork, URL signées, révocation. Aucun override silencieux. | Lot SAMPLE |
| **D8** — pas de compatibilité v2/v3 | Nouvelle version de contrat pour les nouvelles consultations. Les artefacts historiques restent intacts, lisibles et auditables ; un reparse historique incompatible retourne un statut explicite (`unsupported_legacy_format` / `legacy_context_unavailable`) sans réinterprétation spéculative ni accès externe. Rupture documentée, non masquée par un PASS. | Lot L21 |
| **D9** — advisor L21/L22 | L21 : parse/reparse partagé hors ligne sur le nouveau format, cas refus/partial/invalid/ambigu. L22 préparé (plan, tests d'adoption, code supprimable) sans cutover de production ni envoi réel tant que S3 n'est pas approuvé, notamment la revalidation d'autorisation au consume. `prepare` L20 reste sans autorité de soumission. | Lot L21 |
| **D10** — formats Bridge | Aucun JSON demandé au modèle là où AutoWork contrôle le prompt. Les enveloppes HTTP/JSON et artefacts JSON canoniques AutoWork ne sont pas des « JSON générés par le LLM » et ne sont pas visés. Preuves CTI contenant du JSON = preuves admissibles. Tolérance de présentation, stricte sur références/statuts/refus/invariants. | Lots WIRE + L1 |
| **D11** — nouvelle version | `P10_PROMPT_VERSION` passe de `"4"` à `"5"` : nouvelle identité d'idempotence. Les tours v4 restent auditables et ne sont jamais réinterprétés ni resoumis sous v5. Aucun mode rétrocompatible obligatoire dans le nouveau pipeline. | Lot L1-R5 |

## 2. Exclusions et interdictions maintenues

- S3 reste `S3_BLOCKED_DESIGN` ; aucun code S3, aucune migration, aucune route
  S3, aucun cutover L22, aucun egress.
- Aucun appel Bridge/ModelGateway externe/VT/Shodan/Retrohunt/acquisition,
  aucun scan, upload, exécution d'échantillon ou run CTI réel.
- Aucune mutation PostgreSQL/MinIO live, aucune restauration/destruction.
- Aucune réouverture des décisions D1–D11 sans contradiction factuelle
  majeure ; dans ce cas un verdict `BLOCKED_CONTRACT` ciblé est rendu.
- Aucun secret opérationnel AutoWork, `.env`, `.llms_key`, `.secrets`, `var`
  ou archive privée dans les prompts, logs, commits ou rapports.

## 3. Portée Git

Le merge `d7cc6b59` est déjà publié et n'est pas rejoué. La clôture ajoute
uniquement les nouveaux deltas approuvés, sur une branche basée sur le
`main` distant vérifié, puis un merge explicite non destructif et un push
ordinaire après les gates. Aucun force-push, aucune réécriture d'historique.
