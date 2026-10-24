# Mission 03 / Phase 1 — Revue indépendante et handoff

**SHA étudié :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Branche :** `mission03/architecture-design`
**Nature :** synthèse des revues M7, verdicts, blocages, décisions humaines,
risques, UNKNOWN et prompt de reprise. Aucune implémentation.

## 1. Verdicts indépendants par seam

| Seam | Revue | Verdict | Portée |
|---|---|---|---|
| S3 — décision de dispatch et identité d'effet | R7-S3 | **REVISE** | 2 BLOCKER (transport fourni, locks) + 2 HIGH (claim/reuse, recovery/budget) |
| S3 — révision R2 | R7-S3-R2 | **REVISE** ; gate **NOT MET** | F1 CLOSED ; F2–F4 PARTIAL ; N1–N4 HIGH ; test-only slice conditionnel |
| Advisor — consultation préparée et parseur | R7-ADV | **REVISE** | 3 HIGH (refs, octets, autorisation), 1 HIGH migration, 2 MEDIUM |
| Advisor — révision R2 | R7-ADV-R2 | **REVISE (cutover)** ; tranche pure `prepare` approuvée | refs/bytes CLOSED ; autorisation/contexte/migration PARTIAL |

### 1.1 Advisor — synthèse de la première revue

La revue R7-ADV juge la direction approuvable mais le contrat insuffisant
pour une implémentation immédiate :

- **HIGH — identité de refs.** Le catalogue proposé ne garantit pas encore
  qu'un même `(kind, id)` ne porte qu'un SHA ; les agrégateurs actuels
  écrasent ou retiennent silencieusement un digest conflictuel
  (`investigation_advisor_tools.py:4453-4466,4630-4648` ;
  `advisor_wire.py:94-106,153-177,229-257`). Correction R2 : catalogue triple
  fail-closed, alias ⊆ refs transmises, aucune résolution par ID seul,
  `source_ref` d'opinion versionné complet.
- **HIGH — octets exacts.** Le caller actuel hache `encoded`, archive
  `prompt` et soumet une chaîne séparée
  (`investigation_advisor_tools.py:741-778` ;
  `production_artifact_store.py:50-53` ; `bridge_investigator.py:248-252`).
  Correction R2 : `prepare` retourne les octets, le caller archive ces
  octets, S3 décode une fois et vérifie l'égalité de hash.
- **HIGH — frontière d'autorisation.** `prepare` ne peut pas prouver que la
  projection a été filtrée ; le transport advisor truste le booléen du
  caller (`bridge_investigator.py:261-282`). Correction R2 : aucun booléen
  dans `prepare`, gates dans le caller, revalidation au consume S3, matrice
  caller-level de refus à zéro appel.
- **HIGH — migration.** Les tests actuels peuvent rester verts si le nouveau
  module n'est jamais utilisé. Correction R2 : dual-run fixtures sans envoi
  ni shadow parse, tests d'adoption caller-level, deletion test réel.
- **MEDIUM — batching.** `unit_plan` ne doit pas piloter le dispatch, le
  budget ou le découpage ; le caller garde le batching.
- **MEDIUM — omissions.** Réutiliser dossier/secret guard/préflight et
  exposer les omissions/redactions, sans nouveau plafond.

La révision R2 est dans `DESIGN_OPTIONS.md` §5.2.4. La deuxième revue doit
statuer sur sa clôture.

#### 1.1.1 Advisor — résultat de la deuxième revue

R7-ADV-R2 rend **REVISE pour le cutover** et **approuve une première tranche
pure `prepare`** :

- **CLOSED** : catalogue de refs fail-closed avec exception de cible de
  review versionnée ; liaison des octets préparés/archivés ; batching hors
  module ; observabilité des omissions.
- **PARTIAL** : frontière d'autorisation (le seam S3 actuel ne revalide pas
  l'enveloppe au consume), contexte de parse historique (pas de
  `parse_context_version` sur les enregistrements existants), gate de
  migration (corpus v2/v3 gelé absent).
- **MEDIUM** : l'oracle d'octets doit couvrir la normalisation du string
  adapter (`ModelConversationService.add_turn`), pas seulement le helper S3.
- **Première tranche sûre** : module `prepare` + catalogue de refs, non
  branché au caller, sans archive/envoi, sans nouveau champ persisté, sans
  changement de parse/reparse/avis.
- **Premier oracle RED** : conflit `(kind,id)`/SHA → `ref_reference_conflict` ;
  alias absent/restreint/stale/mauvais kind non résolu ; cible de review
  versionnée exacte ; bytes/hash déterministes Unicode/CRLF.

### 1.2 S3 — synthèse de la première revue

La revue R7-S3 rend **REVISE** et maintient S3 hors implémentation :

- **BLOCKER F1 — transport fourni par l'appelant.** `send_one(authority,
  prepared, transport)` laisse le caller choisir le transport ; un wrapper
  peut retenter, rediriger ou paginer, et le retry interne de
  `ModelGateway.execute` peut émettre une seconde requête
  (`model_gateway.py:1161-1179,1257-1264`). Correction R2 : route figée
  serveur, pas de transport public, permis vérifié au dernier mètre,
  retry gateway désactivé ou re-consommé.
- **BLOCKER F2 — locks non réconciliés.** L'arrêt est persisté dans
  `investigation_working_states` (`models/investigation.py:677-719`), la
  réservation opérateur est dans `envelope.budget_snapshot` mais des tables
  `investigation_budget_*` existent aussi, et le tour/run Bridge a sa propre
  transaction (`model_conversations.py:1282-1305` ;
  `model_gateway.py:1051-1061,1140-1144`). Correction R2 : ordre exact
  enveloppe → working state → invocation → dispatch → famille → Bridge
  turn/run, réservation autoritative par chemin, tous les writers alignés.
- **HIGH F3 — claim/occurrence/réemploi.** Il manque l'unicité par défaut,
  l'occurrence stable, la règle de version de normalizer et la ligne
  claimant/use. Correction R2 : claim unique, occurrences stables possédées
  par le workflow, `external_effect_uses`, règle de transition de version.
- **HIGH F4 — reprise et settlement.** Il manque la table de transitions
  complète, la perte d'acquittement, la réponse tardive/partielle, l'unicité
  de settlement et la relation budget acquisition. Correction R2 : table
  d'états/événements, `NEEDS_REVIEW` pour toute ambiguïté, preuve framework
  seule pour `CONFIRMED_NOT_SENT`, settlement unique par dispatch et par
  réservation.

La révision R2 est dans `DESIGN_OPTIONS.md` §5.1.4.

**Résultat R7-S3-R2 : REVISE, gate `S3_READY_FOR_IMPLEMENTATION` NOT MET.**

- **F1 CLOSED (design)** : route figée, pas de transport fourni, permis au
  dernier mètre. La couverture réelle de chaque send/redirect/SDK retry reste
  un gate d'implémentation.
- **F2 PARTIAL** : l'ordre de locks proposé entre en conflit avec les chemins
  actuels de réconciliation/settlement (invocation avant enveloppe), ce qui
  crée un risque de deadlock. Il faut aligner `reconcile_invocation`, la
  finalisation et le settlement sur un ordre unique, et définir le handoff
  d'état `ModelGateway`.
- **F3 PARTIAL** : la règle de `normalizer_version` est incohérente ; un bump
  de version change le claim par défaut et peut permettre un renvoi silencieux.
  Il faut une règle d'équivalence/occurrence explicite et testée.
- **F4 PARTIAL** : la relation budget acquisition (réservation d'opération,
  ledger VT-read/hit, réemploi local) reste à trancher ; le reste de la table
  de transitions est couvert.
- **N1–N4 HIGH** : inversion de l'ordre de locks, binding owner/route,
  protocole d'état `ModelGateway`, normalizer-version.
- **N5–N6 MEDIUM** : oracle du retry gateway désactivé, assertion
  d'exclusion production du route catalog.

La seule tranche conditionnellement autorisable est une composition de test
isolée `vt.file_report`, sans client VirusTotal live, avec production
deny-all. Elle ne peut être préparée qu'après correction du binding
owner/route et du contrat de transaction. Aucun egress de production,
migration ou cutover S3 n'est approuvé.

## 2. Décisions humaines nécessaires

| Décision | Pourquoi elle est bloquante | Preuve |
|---|---|---|
| Portée des métadonnées d'échantillon restreint | Savoir si l'inventaire (IDs, hashes courts, MIME, taille, statut, moteurs) est couvert par la restriction ou seulement les octets/contenus | `investigation_advisor_tools.py:2376-2446,1638-1648,2143-2155` |
| Sémantique de `findings` full vs paged | Choisir « versions courantes » vs « toutes versions », et l'ordre de l'evidence index | `research_context_builder.py:1101-1110,1325-1337,2334-2364,3186-3214` |
| Identité de page/cursor opérateur | `context_sha256` signifie SHA d'état ou SHA du contexte complet ; le curseur ne fige pas les lignes | `operator_tool_invocation.py:350-434,485-578` ; `M4C` §BLOCKED_DESIGN |
| Décision éditoriale publication | Identité, références, pagination de provenance ; ne pas changer le style sans validation | `docs/mission02/TYPST_REAL_PDF_AUDIT.md:38-44` |
| `P10_PROMPT_VERSION` après redaction | Le rendu change pour les entrées échappées ; décider si un bump d'idempotence est requis | `invariant_proposals.py:2301-2364` |

Ces décisions ne bloquent pas les lots de test indépendants (L3–L6) ni le
lot S2 étroit (L1). Elles bloquent les lots qui changent la politique ou le
payload de contexte.

## 3. Risques principaux restants

1. **Couverture des transports physiques S3.** Chaque retry SDK, redirect,
   ticket, fallback ou pagination doit traverser le consume. Un inventaire
   incomplet rend la garantie « un permis = une requête » fausse.
2. **Faisabilité des locks.** L'ordre proposé doit être réconcilié avec les
   tables réelles ; la réservation opérateur est aujourd'hui dans le snapshot
   d'enveloppe, pas nécessairement dans une ligne normalisée.
3. **Budget Bridge.** Une réservation `BRIDGE_TURNS` logique couvre plusieurs
   tentatives physiques ; la comptabilité doit rester une charge logique et
   un reçu de mesure par dispatch, sans double charge.
4. **Corpus historique advisor.** Aucun corpus v2/v3 gelé ne prouve que le
   parseur unifié reproduit les interprétations persistées.
5. **Idempotence fournisseur.** Inconnue ; aucune conception ne doit la
   supposer.
6. **Anciens workers.** Un binaire pré-cutover peut contourner le nouveau
   gate ; le déploiement doit drainer/fencer avant activation.
7. **Secrets dynamiques.** Les URL signées éphémères doivent être vérifiées
   par digest sans être persistées ni journalisées.

### 3.1 Conditions de déblocage

- **S3** : la révision R2 doit fermer F1 (route figée, permis au dernier
  mètre, retry ModelGateway) et F2 (set de locks avec working state, budget
  autoritatif par chemin, Bridge turn/run). Sans R7-S3-R2
  `APPROVED_FOR_IMPLEMENTATION`, S3 reste `S3_BLOCKED_DESIGN`.
- **Advisor cutover** : la matrice caller-level de refus doit prouver zéro
  appel S3 ; le consume S3 doit revalider la politique ou la frontière
  booléenne doit être explicitement acceptée ; le corpus v2/v3 gelé et le
  convertisseur de contexte legacy doivent exister ; l'égalité des octets
  doit être observée après `ModelConversationService.add_turn`.
- **Narrow fixes** : L1 (S2), L2 (refus P10), L3 (audit PG) et L20 (pure
  `prepare`) ne dépendent pas de ces déblocages.

## 4. UNKNOWN

- Comportement runtime exact de chaque SDK HTTP/modèle (retries cachés,
  redirects, mutation de bytes).
- Idempotence/canonicalisation côté VT, Shodan, Retrohunt, acquisition,
  Bridge/provider.
- Couverture complète des compositions production et des entrées directes.
- Faisabilité runtime de l'ordre de locks et du niveau d'isolation sur la
  base réelle.
- Contenu et version des contextes de reparse historiques.
- Coûts Paseo/tokens/fournisseurs de cette phase : **UNKNOWN** (non mesurés).

## 5. Tests et vérifications réellement exécutés

Voir `TEST_SURFACE.md` §2 pour les commandes exactes :

- 414 exécutions offline (375 tests uniques), dont les suites admission/
  dispatcher/advisor/P10/audit/contexte.
- 3 tests PostgreSQL jetables : concurrence `operation_key`, contexte
  opérateur, publication V6 (ce dernier après correction de l'environnement
  Typst).
- 43 tests Typst ciblés, dont la régression de chronologie.
- Aucun test d'export d'audit réel PostgreSQL (aucun test existant).
- Aucun fournisseur, MinIO, Bridge réel, run CTI ou migration live.

L'échec initial du test de publication V6 est un défaut d'environnement
(`/snap/bin/typst` cassé), pas un défaut de code ; il a été corrigé par
`TYPST_BINARY=/snap/typst/current/bin/typst`.

## 6. Verdict global

**GO_NARROW_ONLY.**

Justification :

- **S3** : `S3_BLOCKED_DESIGN`. La revue R7-S3-R2 maintient le gate
  `S3_READY_FOR_IMPLEMENTATION` **NOT MET** (F2–F4 PARTIAL, N1–N4 HIGH). Aucun
  egress de production, migration, cutover ou réemploi S3 n'est autorisé. La
  seule tranche envisageable est une composition de test isolée
  `vt.file_report`, conditionnelle à la correction du binding owner/route et
  du contrat de transaction.
- **Advisor** : `REVISE` pour le cutover. Seule la tranche **pure `prepare`**
  (module + catalogue de refs, non branché, sans archive/envoi/persistance/
  parse/reparse) est approuvée comme expand-only.
- **Correctifs étroits** : L1 (S2), L2 (refus P10), L3 (oracle PostgreSQL
  d'audit) sont prêts et indépendants.
- Le verdict global n'autorise que les lots explicitement listés ci-dessous.
  Il n'autorise aucun seam de production complet, aucune migration live, aucun
  appel fournisseur, aucune publication/G3.

Le verdict S3 est séparé : **`S3_BLOCKED_DESIGN`**. Un verdict positif advisor
ou audit ne débloque jamais S3.

## 7. Trois premiers lots recommandés

1. **L1 — S2 étroit : credential JSON échappé.**
   - **Seam à modifier :** `_normalize_context` → `_render_p10_prompt`
     (`invariant_proposals.py:2301-2364`).
   - **Premier oracle qui doit échouer :** un prompt rendu depuis
     `{\"auth\": \"FIXTURE_ESCAPED_AUTH\"}` contient encore le secret ; après
     correctif, le secret est absent et la CVE/le `api_key` CTI adjacents
     restent byte-for-byte.
   - **Pourquoi en premier :** fuite de credential opérationnel, correctif
     local, aucun schéma, oracle déterministe.

2. **L2 — P10 : un refus reste un refus.**
   - **Seam :** parsing wire P10 → `ProposalResponse` → handoff P09
     (`invariant_proposals.py:1226-1342,262-275` ;
     `domain/invariant_proposals.py:229-243`).
   - **Premier oracle qui doit échouer :** un wire `STATUS: refused` avec une
     ligne candidate enregistre encore un candidat dans P09 ; après correctif,
     le refus est préservé et zéro candidat n'est enregistré.
   - **Pourquoi ensuite :** défaut de politique/refus confirmé, correctif
     local, aucun provider, tests P10 existants à préserver.

3. **L20 — Advisor : tranche pure `prepare` (expand-only).**
   - **Seam :** nouveau module `advisor_consultation.py` avec
     `prepare(projection) -> PreparedConsultation | PreparationRefusal`,
     réutilisant `build_advisor_dossier`, le secret guard et le préflight de
     complétude.
   - **Premier oracle qui doit échouer :** deux sources de même `(kind,id)`
     avec des SHA valides différents, introduites via projection/manifeste/
     refs transmises, ne produisent pas encore `ref_reference_conflict` ; les
     alias absents/restreints/stale/mauvais kind ne sont pas refusés ; une
     cible de review versionnée ne résout pas le `source_ref` exact.
   - **Garde :** non branché dans `InvestigationAdvisorTool.execute`, aucun
     archive/envoi, aucun nouveau champ persisté, aucun changement de
     parse/reparse/avis.
   - **Pourquoi ensuite :** seule tranche de seam approuvée par R7-ADV-R2,
     réversible, sans egress.

**Lot suivant immédiat :** L3 — oracle PostgreSQL réel de l'export d'audit
(lecture seule, base jetable). Puis L4 (cohérence contexte full/paged), L5
(complétude publication), L6 (reparse Suricata). Les lots S3 et le cutover
advisor restent hors autorisation jusqu'à une nouvelle révision et revue.

## 8. Prompt de reprise pour la mission suivante

```text
Mission 04 — implémentation des lots approuvés de Mission 03.

Base code étudiée : aa0cef3ee07525ca77d6b1358252ebf5735a3dde.
Merge documentaire Mission 03 sur main local : 3cdb5de62931c4c18fc65ddcc1ed3e86bff45a4b.
Docs : docs/mission03-architecture/.
Lire AGENTS.md, backend/AGENTS.md, ADRs applicables, puis :
- BASELINE_AND_SCOPE.md
- ARCHITECTURE_MAP.md (registre A/B/C/D)
- TEST_SURFACE.md (oracles présents/manquants)
- DESIGN_OPTIONS.md (décisions retenues)
- IMPLEMENTATION_PLAN.md (lots, RED/GREEN, rollback)
- REVIEW_AND_HANDOFF.md (verdicts, blocages, autorisations)

Verdict global Mission 03 : GO_NARROW_ONLY. S3 = S3_BLOCKED_DESIGN ;
advisor cutover = REVISE ; seule la tranche pure `prepare` (L20) est
approuvée pour l'advisor. Lots autorisés : L1, L2, L3, L20.
Ne pas implémenter S3, le cutover advisor, une migration live, un appel
fournisseur, une publication/G3 ou un rollback non permit-aware.

Commencer par L1 (S2 credential JSON échappé), puis L2 (refus P10), puis L20
(advisor prepare expand-only). Pour chaque lot : branche locale, SHA de base,
oracle RED, correctif minimal, GREEN ciblé, test PostgreSQL réel si requis,
revue indépendante, rollback. Aucun merge/push sans autorisation explicite.
```

## 9. État de livraison

Les six documents demandés sont dans `docs/mission03-architecture/` :
`BASELINE_AND_SCOPE.md`, `ARCHITECTURE_MAP.md`, `TEST_SURFACE.md`,
`DESIGN_OPTIONS.md`, `IMPLEMENTATION_PLAN.md`, `REVIEW_AND_HANDOFF.md`.
Les artefacts détaillés, briefs et revues restent sous `_work/`.

- Commit documentaire de branche : `b7f554238a8abecaae3a2f89108f54c558b24315`.
- Merge local sur `main` : `3cdb5de62931c4c18fc65ddcc1ed3e86bff45a4b`.
- Aucun push vers `origin` ; `main` local est en avance sur `origin/main`.
- Aucun code, test, migration ou état live modifié.
