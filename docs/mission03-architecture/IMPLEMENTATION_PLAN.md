# Mission 03 / Phase 1 — Plan d'implémentation de la mission suivante

**SHA de départ :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Nature :** planification. Aucun lot de ce document n'est exécuté dans la
phase actuelle.

## 1. Principes

- Lots **verticaux** et petits, chacun avec un oracle RED déterministe, un
  correctif minimal, un GREEN ciblé et un rollback.
- Aucun lot ne mélange un refactoring structurel et un changement de politique.
- Les lots `NARROW_FIX` indépendants peuvent démarrer avant les seams S3 et
  advisor.
- Les lots S3 et advisor ne sont exécutables qu'après le verdict M7
  `APPROVED_FOR_IMPLEMENTATION` (ou une revue `REVISE` refermée).
- Un lot qui touche PostgreSQL doit inclure un test PostgreSQL réel sur base
  jetable ; un fake ne clôt pas le lot.
- Aucun lot ne modifie `0001_baseline` ni ne réécrit `0002`. Toute migration
  est additive après le head courant (ADR-0007).
- Un lot bloqué s'arrête après 3 tentatives de réparation ou 2 sans progrès
  mesuré, puis passe en revue indépendante (`autowork-ralph-gate`).

## 2. Graphe de dépendances

```text
L1 (S2 escaped JSON)  ──────────────┐
L2 (P10 refusal outcome) ───────────┤
L3 (audit PG oracle) ───────────────┤
L4 (context full/paged + PG) ───────┼──> L8 (validation transverse)
L5 (publication completeness) ──────┤
L6 (reparse Suricata/composition) ──┘
L7 (policy decision: sample metadata) ── decision humaine, peut bloquer L9+

S3: L10 (prepared effect identity) ─> L11 (attempt/consume + migration)
    ─> L12 (mandatory gateway/direct entry) ─> L13 (Bridge scope/consume)
    ─> L14 (reuse lineage/budget)

ADV: L20 (PreparedConsultation expand) ─> L21 (parse/reparse dual path)
     ─> L22 (cutover/delete) ; L20 dépend du contrat S3 pour le binding
```

Les lots S3 et ADV restent conditionnels jusqu'au verdict M7.

## 3. Lots indépendants (exécutables sans attendre S3/advisor)

### L1 — S2 étroit : credential JSON échappé

**Objectif :** refuser un credential AutoWork sérialisé avec guillemets
échappés dans le prompt P10, sans effacer les preuves CTI adjacentes.

- **Seam :** `_normalize_context` → `_render_p10_prompt`
  (`invariant_proposals.py:2301-2364`).
- **Fichiers/appelants :** `invariant_proposals.py`, tests
  `test_invariant_proposals.py`, consommateur `ProposalConversationService`.
- **Préconditions :** le diagnostic M4-A reste valide ; aucun changement de
  politique de divulgation.
- **Contrats préservés :** labels sensibles existants ; preuves CTI
  permissives. Décider explicitement si le changement de rendu exige
  `P10_PROMPT_VERSION=5` (idempotence des prompts) ou si la version 4 reste
  acceptable parce que seules des entrées auparavant non redactées changent.
- **Migration :** aucune.
- **Premier test RED :** rendu de `{\"auth\": \"FIXTURE_ESCAPED_AUTH\"}` via
  `_normalize_context` → `_render_p10_prompt` : le secret est absent, la CVE
  voisine et un `api_key` de preuve CTI restent byte-for-byte.
- **GREEN :** tests de rendu existants + nouveau cas ; test positif de preuve
  CTI `api_key`/`auth` pour éviter le faux positif.
- **PostgreSQL réel :** non requis (pur).
- **Compatibilité :** aucun changement de schéma/API.
- **Rollback/arrêt :** revert du matcher ; si le nouveau matcher élargit la
  perte de preuves CTI, arrêt et revue.
- **Critères d'acceptation :** le secret échappé est absent ; la CVE et le
  `api_key` CTI adjacents sont byte-for-byte intacts ; aucun nouveau faux
  positif sur les preuves CTI ; les tests P10 ciblés passent.
- **Reviewer :** Luna xhigh indépendant, lecture seule.
- **Autorisation :** code backend local uniquement.

### L2 — P10 : un refus reste un refus

**Objectif :** empêcher qu'un `STATUS: refused` P10 devienne un warning et que
des candidats soient enregistrés dans P09.

- **Seam :** parsing wire P10 → `ProposalResponse` → handoff P09
  (`invariant_proposals.py:1226-1342,262-275,884-918` ;
  `domain/invariant_proposals.py:229-243`).
- **Fichiers/appelants :** `invariant_proposals.py`,
  `domain/invariant_proposals.py`, tests P10, consommateur P09.
- **Préconditions :** décision de contrat : un refus explicite est terminal
  pour ce tour ; les candidats éventuels ne sont pas enregistrés.
- **Contrats préservés :** schéma wire, codes de refus, `ProposalResponse`
  versionné (ajout additif d'un statut/outcome).
- **Migration :** aucune (champ de domaine additif, pas de DB).
- **Premier test RED :** wire `STATUS: refused` + une ligne candidate →
  `propose` retourne un refus et P09 n'enregistre aucun candidat.
- **GREEN :** tests P10 existants + refus sans candidats + refus avec
  candidats + réponse partielle sans refus.
- **PostgreSQL réel :** non requis.
- **Compatibilité :** les réponses sans refus gardent leur comportement.
- **Rollback/arrêt :** revert du gate ; si un consommateur aval dépendait du
  warning, documenter avant de changer.
- **Critères d'acceptation :** un refus reste un refus ; aucun candidat n'est
  enregistré dans P09 ; une réponse partielle sans refus garde son
  comportement ; les tests P10 existants passent.
- **Reviewer :** Luna xhigh.
- **Autorisation :** code backend local.

### L3 — Oracle PostgreSQL réel pour l'export d'audit

**Objectif :** prouver que `read_investigation_audit_snapshot` +
`build_audit_bundle` produisent un bundle cohérent depuis PostgreSQL.

- **Seam :** `infrastructure/database/investigation_audit_reader.py:48-209` →
  `investigation_audit_export.py:2568-2659`.
- **Fichiers/appelants :** nouveau test d'intégration ; éventuellement
  `backend/scripts/export_investigation_audit.py` (lecture seule).
- **Préconditions :** base jetable `postgres-test` ; aucune donnée live.
- **Contrats préservés :** `RUN_EVENTS/RUN_TRACE/RUN_INDEX/RUN_MANIFEST`,
  hashes/counts, `UNKNOWN`/`unavailable_resources`, reprise.
- **Migration :** aucune.
- **Premier test RED :** seed d'une invocation, observation, finding, état,
  avis et tour modèle ; l'export omet ou mal-scope une ligne → test rouge.
- **GREEN :** IDs/counts identiques aux requêtes DB ; refs résolues dans la
  même investigation ; hash mismatch → payload unavailable ; reprise sans
  relecture DB.
- **PostgreSQL réel :** oui, obligatoire.
- **Compatibilité :** aucun changement de production si le test passe.
- **Rollback/arrêt :** si le reader révèle un défaut, arrêter et ouvrir un lot
  de correction séparé ; ne pas modifier l'export pour faire passer le test.
- **Critères d'acceptation :** IDs/counts identiques aux requêtes DB ; refs
  résolues dans la même investigation ; hash mismatch → payload unavailable ;
  reprise sans relecture DB ; test PG réel vert.
- **Reviewer :** Luna xhigh.
- **Autorisation :** PostgreSQL jetable uniquement.

### L4 — Cohérence contexte complet / sections paginées

**Objectif :** décider et aligner la sémantique `findings` et `evidence_index`
entre le contexte Bridge complet et les sections opérateur paginées.

- **Seam :** `ResearchContextBuilder.build` vs
  `read_context_section_page` ; `repositories/investigation.py` `latest_only`
  et ordres `(timestamp, UUID)`.
- **Fichiers/appelants :** `research_context_builder.py`,
  `operator_tool_invocation.py`, repositories, tests.
- **Préconditions :** décision de contrat documentée : « findings » = versions
  courantes ou toutes versions ? « evidence_index » = ordre UUID ou
  observed_at/id ? Le lot doit trancher explicitement, pas par accident.
- **Contrats préservés :** payload/hash du contexte, pagination/omissions,
  compatibilité API v1.
- **Migration :** aucune.
- **Premier test RED :** fixture avec versions superseded et timestamps égaux ;
  la comparaison full vs paged échoue sur l'ensemble/ordre attendu.
- **GREEN :** full et paged partagent la définition tranchée ; l'historique des
  findings reste adressable séparément ; omissions/manifestes cohérents.
- **PostgreSQL réel :** oui pour la chaîne multi-page et les ties.
- **Compatibilité :** changement de payload potentiel → vérifier les hashes de
  contexte et les consumers Bridge/opérateur.
- **Rollback/arrêt :** revert du changement de projection ; si le contrat
  choisi casse un consommateur, arrêt et décision humaine.
- **Critères d'acceptation :** full et paged partagent la définition tranchée ;
  l'historique des findings reste adressable ; omissions/manifestes cohérents ;
  chaîne multi-page PG verte.
- **Reviewer :** Luna xhigh.
- **Autorisation :** code + PG jetable.

### L5 — Complétude publication et lineage

**Objectif :** ajouter un oracle de complétude canonique → PDF et exposer les
acteurs reviewer/approver dans la lineage.

- **Seam :** projection `TypstRenderer.render` / `PublicationRenderService`
  (`publication_rendering.py:714,756,847` ; `typst_rendering.py:801,971`).
- **Fichiers/appelants :** tests Typst réels, `publication_rendering.py`,
  template si nécessaire.
- **Préconditions :** binaire Typst 0.15.1 et bundle `chpTypst/` disponibles.
- **Contrats préservés :** V6 → V5 exact, `base_publication` vs
  `augmented_publication`, `opinion=None` pour une base, hashes de render.
- **Migration :** aucune.
- **Premier test RED :** document V6 publié avec une chronologie complète ;
  une entrée canonique absente du PDF → test rouge.
- **GREEN :** toutes les entrées apparaissent une fois ; base V5 inchangée ;
  avis/version exacts ; lineage avec acteurs.
- **PostgreSQL réel :** oui pour le V6 publié ; Typst réel en plus.
- **Compatibilité :** ne pas changer le style éditorial ; seule la complétude
  et la lineage sont ajoutées.
- **Rollback/arrêt :** revert du test/template ; toute décision éditoriale
  reste hors lot.
- **Critères d'acceptation :** chaque entrée canonique apparaît une fois ;
  base V5 inchangée ; avis/version exacts ; lineage avec acteurs ; suite
  Typst ciblée verte.
- **Reviewer :** Luna xhigh + validation éditoriale humaine si le rendu change.
- **Autorisation :** rendu offline uniquement.

### L6 — Reparse Suricata et composition

**Objectif :** aligner les types acceptés par le reparse et vérifier que le
tool est enregistré dans la composition.

- **Seam :** `ReparsePersistedAdvisorOutputTool.execute`
  (`investigation_advisor_tools.py:3442-3487,4188-4225`) ;
  `investigation_composition.py`.
- **Préconditions :** le contexte réseau stocké contient les faits/refs exacts ;
  sinon le reparse doit retourner un refus raisonné, pas deviner.
- **Premier test RED :** un output `suricata_draft` valide stocké n'est pas
  reparseable aujourd'hui.
- **GREEN :** reparse accepté seulement avec contexte vérifié ; pas de Bridge ;
  pas de relecture d'évidence ; observation versionnée.
- **PostgreSQL réel :** non requis ; test de composition avec fake store.
- **Rollback/arrêt :** revert ; si le contexte est incomplet, documenter
  l'exclusion plutôt que d'inventer.
- **Critères d'acceptation :** `suricata_draft` reparse seulement avec
  contexte vérifié ; zéro Bridge ; zéro relecture d'évidence ; observation
  versionnée ; test de composition vert.
- **Reviewer :** Luna xhigh.

## 4. Décision de politique bloquante

### L7 — Portée des métadonnées d'échantillon restreint

- **Question :** la restriction d'échantillon couvre-t-elle les métadonnées
  d'association (IDs, hashes courts, MIME, taille, statut, moteurs) ou
  seulement les octets/contenus ?
- **Preuve :** `investigation_advisor_tools.py:2376-2446,1638-1648,2143-2155`.
- **Décision humaine requise :** oui. Sans elle, aucun code ne doit être
  modifié.
- **Alternative offline :** un test de non-régression qui capture l'inventaire
  actuel et documente l'écart, sans changer la politique.

## 5. Lots S3 (conditionnels au verdict M7)

> Les spécifications détaillées seront finalisées après la sélection et la
> revue. Les lots ci-dessous sont les tranches minimales envisagées.

### L10 — Identité d'effet préparée, route figée et claim

- **Objectif :** normaliser et persister l'intent exact, avec version de
  normalizer/serializer et `route_id`, avant toute décision d'envoi ; aucune
  production activée.
- **Seam :** nouveau module `external_effects` (préparation/route),
  `send_one(prepared)` non encore branché aux transports de production.
- **Fichiers/appelants pressentis :** `investigation_tools.py`,
  `investigation_service.py`, `model_gateway.py`, modules VT/Shodan/Retrohunt/
  acquisition/advisor, `investigation_composition.py`.
- **Préconditions :** R7-S3-R2 approuvé ; aucune route de production activée.
- **Contrats/versionnement :** `PreparedExternalRequest` avec bytes/artifact
  ref, digest sémantique, digest de requête applicative, `route_id`,
  `occurrence_id`, `parent_effect_id`, `step`, `reuse_policy`, refs de preuve.
- **Migration :** aucune dans ce lot (identité en mémoire/artefact) ; la table
  de dispatch arrive en L11.
- **Responsabilités remplacées :** `normalized_parameters=arguments` cesse
  d'être l'identité d'effet ; les adapters préparent une seule fois.
- **Premier test RED :** deux requêtes équivalentes sous des clés clientes
  différentes produisent le même `effect_key` ; une requête matériellement
  changée produit un digest distinct ; une route absente/stale échoue avant
  tout I/O.
- **GREEN :** goldens d'octets/digests par route, table de routes immuable,
  refus des versions inconnues, zéro transport.
- **PostgreSQL réel :** non requis pour la préparation pure.
- **Compatibilité :** aucune route de production branchée.
- **Rollback/arrêt :** supprimer le module dormant ; aucun état persisté.
- **Critères d'acceptation :** même effet → même `effect_key` ; requête
  changée → digest distinct ; route absente/stale → refus avant I/O ; goldens
  d'octets/digests verts ; zéro transport.
- **Reviewer :** Luna xhigh.
- **Autorisation :** code backend local.

### L11 — Décision durable, dispatch et consume

- **Objectif :** ajouter la table/état `external_effect_dispatches`, les
  événements append-only et la transaction de consume.
- **Seam :** `send_one(prepared)` + réconciliation CAS.
- **Fichiers/appelants :** nouveaux repositories/UoW, `investigation_service`,
  `investigation_tools`, `model_conversations`, `model_gateway`, modules
  famille.
- **Préconditions :** L10 vert ; R7-S3-R2 approuvé ; head Alembic vérifié.
- **Contrats/versionnement :** `PREPARED → CONSUMED → SUCCEEDED |
  NEEDS_REVIEW` ; `CONFIRMED_NOT_SENT` sur preuve framework ;
  `NEEDS_REVIEW → RECONCILED_* | ABANDONED`.
- **Migration :** additive après le head courant ; unicité
  `(investigation_id, effect_key)`, FKs typées, CHECK d'état, index de
  reprise ; pas de backfill.
- **Ordre de locks :** enveloppe → working state (stop) → invocation →
  dispatch → famille → Bridge turn/run ; réservation autoritative par chemin.
- **Premier test RED :** deux sessions PostgreSQL : A commit `RUNNING` +
  réservation + prepared et pause avant consume ; B commit `NEEDS_REVIEW` ;
  A reprend → zéro consume/permis et zéro appel du fake.
- **GREEN :** révocation-first/consume-first, claim unique sous clés
  distinctes, absence de lock pendant le transport, pas de deadlock.
- **PostgreSQL réel :** oui, obligatoire.
- **Compatibilité :** aucune ligne existante réécrite.
- **Rollback/arrêt :** code rollback uniquement vers un binaire permit-aware ;
  les tables additives restent.
- **Critères d'acceptation :** course `RUNNING → NEEDS_REVIEW` à zéro egress ;
  revocation-first/consume-first conformes ; claim unique sous clés
  distinctes ; pas de lock tenu pendant le transport ; pas de deadlock.
- **Reviewer :** Luna xhigh.
- **Autorisation :** PostgreSQL jetable.

### L12 — Gateway obligatoire et entrées directes

- **Objectif :** rendre le gate mandatory au dernier mètre et fail-closed
  pour chaque transport physique, y compris Bridge et acquisition.
- **Seam :** `send_one` + `PermitAwareTransport` ; route figée serveur.
- **Premier test RED :** appel direct sans permis à chaque entrée brute
  (VT, Shodan, Retrohunt, acquisition, advisor, Bridge) → refus typé,
  zéro appel provider.
- **GREEN :** avec permis correct → un seul appel ; avec permis copié/stale/
  mal lié → zéro ; retry interne ModelGateway désactivé ou re-consommé.
- **PostgreSQL réel :** oui pour le consume ; fakes pour les transports.
- **Rollback/arrêt :** deny-all si une famille n'est pas prête ; jamais de
  fallback vers l'ancien chemin.
- **Critères d'acceptation :** chaque entrée brute sans permis → refus typé et
  zéro appel ; permis valide → un appel exact ; retry gateway désactivé ou
  re-consommé.
- **Reviewer :** Luna xhigh.

### L13 — Portée Bridge / retry / correctif

- **Objectif :** définir et tester chaque requête physique comme un consume
  distinct, avec `BRIDGE_TURNS` logique chargé une fois.
- **Premier test RED :** révocation après la première soumission → deuxième
  egress refusé ; correction avec contexte changé → nouveau digest/consume.
- **GREEN :** initial/retry/correctif liés à leurs tours/runs exacts ; un
  reçu de mesure par dispatch ; pas de double charge.
- **Critères d'acceptation :** révocation après la première soumission →
  deuxième egress refusé ; correction changée → nouveau digest/consume ;
  `BRIDGE_TURNS` logique chargé une fois.
- **PostgreSQL réel :** oui.
- **Reviewer :** Luna xhigh.

### L14 — Réemploi, filiation et settlement

- **Objectif :** réutiliser un résultat terminal sans nouvelle requête, avec
  filiation invocation/observation et budget chargé une fois.
- **Premier test RED :** réemploi A→B sans `external_effect_uses` → test
  rouge ; résultat ambigu réutilisé → refus.
- **GREEN :** use row source/consumer, observations sources, delta zéro ;
  settlement unique par réservation et par dispatch ; transition de
  `normalizer_version` explicite.
- **Critères d'acceptation :** réemploi sans transport et sans charge ;
  provenance source intacte ; résultat ambigu jamais réutilisé ; settlement
  unique.
- **PostgreSQL réel :** oui.
- **Reviewer :** Luna xhigh.

## 6. Lots advisor (conditionnels au verdict M7)

### L20 — `PreparedConsultation` (expand, tranche pure `prepare`)

- **Statut M7 :** approuvé par R7-ADV-R2 comme première tranche sûre, sans
  cutover. Les autres lots advisor restent conditionnels.
- **Objectif :** ajouter le module `advisor_consultation.py` et son catalogue
  de refs, à côté de l'ancien chemin, sans le brancher dans
  `InvestigationAdvisorTool.execute`, sans archive/envoi, sans nouveau champ
  de contexte persisté, sans changer le parse/reparse ni l'avis.
- **Seam :** `prepare(projection) -> PreparedConsultation |
  PreparationRefusal` ; réutiliser `build_advisor_dossier`, le secret guard et
  le préflight de complétude.
- **Premier test RED :** deux sources de même `(kind,id)` avec des SHA
  valides différents, introduites séparément via projection, manifeste et refs
  transmises, doivent produire `ref_reference_conflict` ; les alias absents,
  restreints, stale ou de mauvais kind ne résolvent pas ; une cible de review
  versionnée résout vers le `source_ref` exact ; bytes/hash déterministes avec
  Unicode et CRLF.
- **GREEN :** oracle de conflit, oracle d'alias subset, oracle de cible
  versionnée, oracle de déterminisme ; tests existants de dossier/secret/
  complétude inchangés.
- **PostgreSQL réel :** non requis (pur).
- **Garde :** aucun egress, aucun stockage, aucun impact historique.
- **Rollback :** supprimer le module dormant ; aucun état persisté.
- **Critères d'acceptation :** conflit `(kind,id)`/SHA → refus ; alias subset
  et cible versionnée exacts ; bytes/hash déterministes Unicode/CRLF ;
  dossier/secret/complétude inchangés ; aucun egress.
- **Reviewer :** Luna xhigh indépendant.
- **Autorisation :** code backend local uniquement.

### L21 — Parse/reparse partagé (dual path)

- **Objectif :** faire passer le parse live et le reparse par le même contrat,
  avec corpus v2/v3 gelé.
- **Premier test RED :** un refus v2 ou un contexte legacy n'est pas préservé.
- **Garde :** l'ancien chemin reste autoritaire jusqu'à parité.
- **Critères d'acceptation :** corpus v2/v3 gelé vert ; refus/partial/invalid
  préservés ; contexte manquant → `legacy_context_unavailable` ; zéro
  rechargement d'évidence ; zéro Bridge ; parité live/reparse.

### L22 — Cutover et suppression

- **Objectif :** basculer préparation puis parse, puis supprimer les helpers
  privés remplacés.
- **Premier test RED :** une obligation de préparation/parse réapparaît dans
  deux callers après suppression → deletion test échoue.
- **Garde :** rollback par wiring ; blobs/observations immuables conservés.
- **Critères d'acceptation :** les deux callers traversent le même module ;
  l'ancienne agrégation de refs et l'ancien dispatch sémantique sont
  inaccessibles ; aucun dual-send ni shadow parse ; avis inchangé.

## 7. Validation transverse (L8)

- Rejouer les suites ciblées offline de la phase.
- Exécuter les tests PostgreSQL jetables des lots concernés.
- Exécuter les tests Typst réels pour la publication.
- Vérifier `make lint-backend` et le typage ciblé des modules modifiés.
- Ne pas lancer d'E2E CTI live sans autorisation séparée.
- Rapport final : SHA, tests exécutés, tests non exécutés, risques, UNKNOWN.

## 8. Ce que ce plan n'autorise pas

- Aucun run CTI réel, aucun appel fournisseur, aucune mutation PostgreSQL/
  MinIO live, aucune publication/G3.
- Aucune migration live.
- Aucun merge/push sans demande explicite de l'utilisateur ou de
  l'orchestrateur propriétaire.
