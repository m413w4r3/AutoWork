# FINAL_CHECKPOINT_B

**Opérateur :** CTI-OPERATOR-B  
**Enquête :** `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`  
**Date :** 2026-10-30  
**Nature :** checkpoint opérateur; les artefacts PostgreSQL/versionnés restent la source canonique.

## État final observé

- État opérateur : v148, SHA-256 `516f4b656d27499d70744b60e358e7d606b6e2b588945b02f3bb211d1ead3727`.
- G2 v292 : approuvé, non révoqué; TLP Clear; `external_llm_allowed=true`.
- Budgets : Bridge 3 consommés, 3 réservés, 194 libres; VT 72 consommés; téléchargements 8; 853 secondes outil consommées, 10 800 réservées.
- Les statuts `needs_review` de a228, b069 et b073 restent non résolus. Les listes de réconciliation du statut v148 sont pourtant vides; aucune voie de réconciliation n’est exposée dans l’inventaire d’outils.

## Archive et finding archive/projet

- La nouvelle tentative `archive_list` b013 a rendu `7z_unavailable` malgré une invocation locale réussie.
- La nouvelle extraction b016 a réussi en quarantaine : 3 045 octets; l’observation `25cd54a2-2fa0-5943-b36d-25bc17fcb29d` confirme l’identité exacte des octets avec le rapport projet. L’analyse b017 était partielle; elle n’a ni compilé ni exécuté le contenu.
- `finding.revise` b053 a répondu `succeeded`, mais les lectures canoniques b055/b056 montrent que l’entrée courante `be484795-ed7c-54bc-be5f-fc441d1f7ed3` demeure `inconclusive`; l’ancienne entrée l’est aussi. Ne pas présenter le finding comme `supported` et ne pas créer une autre version sans correction de schéma/subject. Demande locale : `orchestrator_requests/b006-finding-assessment.md`.
- Le blocage du listing 7z est soumis dans `orchestrator_requests/b002-archive-list-7z.md`.

## Analyse statique .NET

- **Candidat #3 :** `methods.list` couvre la méthode `PsProxy.PSExecutor.Execute(string command)`; son corps décompilé est un wrapper très court avec marqueurs URL/clé/IV, sans comportement réseau ou crypto établi. Ghidra a trouvé zéro import et une fonction native minuscule; décompilation Ghidra vide. CAPA n’a pas établi de catégorie réseau/crypto; la recherche PowerShell n’a trouvé aucune occurrence. La logique réelle reste non résolue.
- **Candidat #5 :** les 207 méthodes sont probablement renommées et le listing paginé est complet. Le corps RID 31 mentionne `System.Diagnostics.Process`, sans `Process.Start` ni `ProcessStartInfo`. Les vues/imports Ghidra sont limités; décompilation de la fonction substantielle vide. Les recherches directes AES, HttpWebRequest et FromBase64String n’ont rien trouvé. CAPA émet des labels statiques génériques; chaînes incomplètes et SMDA/goodware indisponibles empêchent de les traiter comme comportement confirmé.
- Aucun échantillon n’a été exécuté par l’opérateur.

## YARA et Bridge

- `rule.workbench` est absent de l’inventaire disponible et indisponible; `sample.extract_configuration` est indisponible avec `configuration_extraction_unavailable`.
- Les préflights YARA b060/b067 ne pouvaient charger les passages article cités. Un ref article brut a été rejeté par le schéma; l’observation UUID `article.get_claims` n’a pas satisfait le chargement du corps canonique. Détails sans texte article : `orchestrator_requests/b003-yara-article-reference.md`.
- b069, limité aux observations statiques, a passé le préflight 75/75 puis a fini `needs_review` au stade `provider_submission_ambiguous`; état externe inconnu, aucun ID Bridge ni sortie, `confirmed_non_execution=false`. Ne pas rejouer ce contenu. Demande locale : `orchestrator_requests/b004-b069-bridge-reconciliation.md`.
- Aucun bloc YARA utilisable n’a été reçu; `rule.propose_bridge` et `rule.test` n’ont donc pas été appelés. Aucun candidat n’a été testé.

## Draft éditorial et autres pivots

- b071 n’a pas été soumis au Bridge : prévalidation locale, assertions structurées manquantes et deux références mal saisies; `confirmed_non_execution=true`.
- b073 utilisait des assertions structurées et les références corrigées, mais son batch est `needs_review` avec `provider_submission_ambiguous`, aucun texte retourné et `usable=false`. Aucun `opinion.compose`/`opinion.revise` ni revue `review_text` n’a été réalisé. Demande locale : `orchestrator_requests/b005-b073-reconciliation.md`.
- a228 reste `needs_review`; aucun rejeu identique n’a été effectué.
- Aucun nouveau pivot VT n’était étayé : la guidance antérieure notait l’absence de catalogue `available_pivots` et les relations exactes déjà consommées. Aucun appel VT supplémentaire. Le PCAP a037 demeure unresolved.

## Requête dynamique autorisée

Conformément à `DYNAMIC_AUTH.md`, les demandes suivantes ont été déposées; elles ne sont ni une exécution opérateur ni des résultats d’analyse :

- `dynamic_requests/b001-s3-runtime-calltrace.md`
- `dynamic_requests/b002-s5-runtime-calltrace.md`

Elles demandent une seule exécution instrumentée par candidat sous QEMU/Frida isolé, sans egress, DNS, contact C2, persistance ni lancement de payload enfant. Au checkpoint, `dynamic_results/` ne contient aucun résultat. Les sorties éventuelles restent non canoniques tant qu’elles ne sont pas enregistrées par les outils AutoWork.

## Demandes locales à l’orchestrateur

- `orchestrator_requests/b001-rule-workbench.md`
- `orchestrator_requests/b002-archive-list-7z.md`
- `orchestrator_requests/b003-yara-article-reference.md`
- `orchestrator_requests/b004-b069-bridge-reconciliation.md`
- `orchestrator_requests/b005-b073-reconciliation.md`
- `orchestrator_requests/b006-finding-assessment.md`

## Prochaine action permise

1. Réconcilier b069/b073 auprès du coordinateur Bridge; ne pas les rejouer tant que l’état externe reste inconnu.
2. Fournir le ref canonique accepté pour les deux passages article si une nouvelle consultation YARA est encore requise.
3. Corriger l’assessment du finding archive/projet par le workflow canonique.
4. Ingérer les résultats dynamiques lorsqu’ils seront déposés; les qualifier comme non canoniques avant tout enregistrement.

Aucun test n’a été exécuté. `rule.test` a été omis faute de candidat YARA.
