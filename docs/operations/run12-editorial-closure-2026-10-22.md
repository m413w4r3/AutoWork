# Run12 éditorial + Bridge — état pour audit externe

Date d’arrêt : 2026-10-22. Référence de départ : `main@2317440efba1c70b17870c56b6b23475a601239e`.

Cette matrice décrit l’état vérifié au moment de l’arrêt demandé. Un succès hors ligne ne vaut pas validation Bridge en production. Aucun malware n’a été exécuté, aucun C2 contacté et aucun upload non autorisé effectué.

## Matrice de clôture

| Lot | État vérifié | Preuve et limite restante |
|---|---|---|
| A — budgets et quotas Bridge | Implémenté et validé hors ligne. | Rapport local `lots/WAVE-A.md`. |
| B — dossiers Bridge textuels | Implémenté; rejeu archivistique C01–C13 validé. Le code est dans la pile locale après `make up`. | La consultation Bridge réelle à faible coût n’a pas atteint le transport : l’admission a refusé la réservation `tool_seconds` (266 s disponibles, 3 600 s réservées par défaut). Invocation observée : `da013050-6d13-49c9-9388-9b1bc75f74e8`. Aucun avis Bridge n’en est résulté. Les fixtures C01 synthétiques ne valident pas le contenu réel des blobs. |
| C — OR-4/OR-5 | Implémenté, testé par l’orchestrateur et déployé dans la pile locale. | Suites complètes après C/D : lint, formatage et typage réussis; backend 4 503 réussis, 2 ignorés, 30 désélectionnés; intégration 340 réussis; frontend 293 réussis. La coordination du pacer reste locale au processus; les erreurs pré-soumission autres que 429 restent limitées à trois essais. Aucun essai Bridge live n’a été fait. |
| D — BC-5/OR-6 | Implémenté, testé par l’orchestrateur et déployé dans la pile locale. | Investigator C9 : 255 389 → 41 323 octets (−83,82 %). P10 synthétique : 198 251 → 38 919 octets (−80,37 %), 128/128 faits et alias conservés. C01–C13 rejoués. Le renderer refuse explicitement tout prompt supérieur à 96 000 octets. |
| E — rejeu et livrable | Incomplet; arrêté sur demande. | Les entrées archivées réelles C14/C15 passent maintenant le parseur de compatibilité historique (`sample_deltas` absent devient vide), mais restent au-dessus de la borne : rendu mesuré en mode diagnostic à 157 108 et 167 422 octets. Il faut compacter ces dossiers de façon sémantique, sans couper arbitrairement les preuves, puis rejouer C14/C15. |
| Avis | Version live connue à la lecture : avis v61 brouillon, SHA-256 `82a1c86218ccb7759669a20f3001bd1c45f5683cec5dabbb1d7f34911428ee49`. | Les revues avec les nouveaux dossiers, la correction du jeton à 93 caractères et la nouvelle version ne sont pas faites. La valeur du jeton est volontairement omise. |
| PDF et QA | Non réalisés pour une nouvelle version. | Aperçu PDF versionné/hashé, contrôle visuel page par page et second audit final READ-ONLY indépendant restent à faire. Les PDF antérieurs ne sont pas requalifiés par ce travail. |
| YARA | État de départ : `ONE_RULE_NOT_VALIDATED`. | Le rapport de reprise indique 225 cas, avec PsProxy seul apparié. Aucune candidate finale rédigée par Bridge n’a été validée pendant cette reprise. Conserver ce statut jusqu’à validation complète positifs/nouveaux candidats/goodware/holdout. |
| Suricata et figures | `SURICATA_NOT_JUSTIFIED`; aucune figure nouvelle justifiée dans l’état de reprise. | Pas de règle inventée à partir de DNS de fond. Les figures héritées restent détachées du nouvel avis tant qu’une proposition Bridge et des preuves suffisantes ne les justifient pas. |
| Nouveaux échantillons et pivots VT | Pas de nouveau résultat produit pendant la vague E. | Les recherches `content:{...}`, acquisitions et analyses statiques bornées doivent être reliées aux octets distinctifs, acquisitions, nouveaux échantillons et tests YARA avant toute conclusion de famille. |

## Rejeu C14/C15 : mesure et correctif déjà présent

Les archives sont `C14_investigation_turn_bridge_2026-10-18.input.txt` et `C15_investigation_turn_bridge_2026-10-18.input.txt` dans le corpus local du run. Le contexte canonique mesure respectivement 177 275 et 186 187 octets; l’entrée complète mesure 199 592 et 213 400 octets. L’augmentation temporaire de la borne en mémoire n’a servi qu’à mesurer le rendu; elle n’a changé ni la configuration ni la base live.

Le changement de compatibilité dans `backend/src/cti_app/application/investigation_contracts.py` et son test dans `backend/tests/test_investigation_contracts.py` ont été validés avec le rejeu C01–C13 : **2 tests ciblés réussis**. La correction ne résout pas encore le dépassement de taille C14/C15.

## Vérifications effectivement exécutées

- Après les vagues C et D : `make lint typecheck test-backend test-integration test-frontend` a réussi. Backend : 4 503 réussis, 2 ignorés, 30 désélectionnés; intégration : 340 réussis; frontend : 293 réussis.
- Une assertion historique exigeant l’ancien champ JSON `view=full` a été remplacée par une vérification du dossier textuel canonique; le test ciblé a réussi.
- Après l’ajout de compatibilité E : le test historique C01–C13 et le nouveau test de compatibilité ont réussi (2 tests). Les suites complètes n’ont pas été relancées après ce dernier changement.
- `git diff --check` a réussi avant l’ajout de cette matrice; il est relancé avant le commit.
- Avant les mises à jour de la pile locale, le contrôle SQL des invocations `pending`/`running` retournait 0; `make up` puis `make status` ont confirmé les services locaux sains.

## Décisions et actions humaines restantes

1. Autoriser une enveloppe `tool_seconds` suffisante ou définir une admission bornée adaptée avant de tenter la consultation Bridge réelle; ne pas contourner le contrôle G2.
2. Décider G3 et approuver séparément l’avis, la publication V6, le repin et la release.
3. Réconcilier les invocations `needs_review` selon les preuves d’état persistées.
4. Traiter la rotation du jeton opérateur client mentionné dans l’incident OP-3; sa valeur n’est pas reproduite ici.
5. Valider la migration additive `0002_c2_experiments` en tenant compte de l’ADR-0007 et de la règle historique dans `backend/AGENTS.md`.

## Séquence reproductible après reprise

1. Réduire C14/C15 par sections et références canoniques; conserver tous les IDs, citations et valeurs IOC, avec un avis explicite pour chaque détail récupérable par outil.
2. Rejouer C01–C15 hors ligne et parser chaque réponse archivée; vérifier le plafond de 96 000 octets sans troncature silencieuse.
3. Quand le budget le permet, faire au plus six tours A/B live avec dossier textuel à contenu réel; consigner coûts et réponse Bridge. Reprendre ensuite les revues de l’avis v61.
4. Corriger le jeton à 93 caractères sans l’afficher dans les journaux, produire une nouvelle version et son aperçu PDF hashé, puis contrôler chaque page.
5. Faire un audit indépendant READ-ONLY; maintenir tous les échecs comme échecs dans la matrice et ne publier qu’après les approbations humaines.

Les journaux détaillés, réponses Bridge archivées et données live restent dans `.autowork-runs/run12-editorial-2026-10-20/` et ne sont pas recopiés dans `nimbalyst-local/`.
