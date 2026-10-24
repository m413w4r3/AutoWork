# Mission 02 V2 — handoff

## SHA et état remis

- SHA de base de mission/S0 :
  `e4abba495d9f5fdc296e2ba80b65442855625474`.
- SHA exact du code intégré audité et du gate final :
  `8574328c` (le commit documentaire final est au-dessus, sur `main`).
- La branche `mission02/final-docs` part de `d4c4c2ed`; le lot de style
  mécanique `1eaad508` et les fusions locales la précèdent sur `main`.
  Pas de push ni déploiement.
- Registre des lots : [TASKS.md](TASKS.md). Résultats détaillés :
  [FINAL_AUDIT.md](FINAL_AUDIT.md), [TYPST_REAL_PDF_AUDIT.md](TYPST_REAL_PDF_AUDIT.md),
  [S1_PERMISSIVE_REVIEW.md](S1_PERMISSIVE_REVIEW.md),
  [S2_P10_REVIEW.md](S2_P10_REVIEW.md),
  [S3_DISPATCH_REVIEW.md](S3_DISPATCH_REVIEW.md),
  [S7_DEAD_CODE.md](S7_DEAD_CODE.md).

## Reproduction du gate backend

À lancer depuis la racine du dépôt, avec l'environnement backend déjà
disponible. `make test-backend` synchronise l'environnement depuis le lock,
puis exécute `pytest --ignore=tests/integration -m "not integration"`.
Configuration Typst utilisée pour le gate :

```sh
export PATH="/snap/typst/current/bin:$PATH"
export TYPST_BINARY=/snap/typst/current/bin/typst
export FONT_BUNDLE_ROOT=chpTypst
make test-backend
```

Résultat consigné sur le SHA ci-dessus : **4708 passed, 2 skipped,
30 deselected** en 129 s, après le lot de formatage. Sans ces variables,
cinq tests Typst échouent sur `/snap/bin/typst` cassé. Les 33 échecs
historiques liés à Typst sont résolus sur cet hôte avec le binaire
fonctionnel; ce résultat n'est pas une preuve sur un autre hôte. Ne pas
confondre ce gate hors intégration avec les 7 tests PostgreSQL exécutés
séparément sur une base jetable.

Lint backend : `make lint-backend` vert après `1eaad508` (`ruff check` et
`ruff format --check`, 842 fichiers). Le lot de style est AST-identique;
166 tests ciblés ont été rejoués. Mypy repo-wide conserve 58 erreurs
préexistantes dans 11 fichiers, hors modules Mission 02 modifiés.

## États, inconnues et risques ouverts

- S1 : revue indépendante `APPROVE`; set ciblé final 171 passed. `TASKS.md`
  conserve aussi un run parent antérieur de 135 passed et un échec lié au
  corpus `.autowork-runs/` absent.
- S2 : revue finale `REVISE`; résidu limité documenté de credentials dans
  du texte libre JSON échappé. Aucun cascade de décodeurs ajouté.
- S3 : `BLOCKED_DESIGN`; aucun code B1A1 implémenté. Le consume doit vérifier
  atomiquement l'état persistant de l'invocation/Bridge; portée Bridge et
  provenance d'un résultat réutilisé restent à décider.
- S4A : 7 tests PostgreSQL jetables passés; export d'audit réel-DB `UNKNOWN`.
- S5 : anti-slop 16 cas et oracles S1 passés; scénario complet avec/sans
  échantillon non rejoué; Q2 reste une hypothèse.
- S6 : PDF réel inspecté en lecture seule et fixtures rendues hors ligne;
  défaut des 5 entrées de chronologie corrigé. Identité, références et
  pagination de provenance attendent une décision éditoriale. Complétude des
  figures/sections hors contrôles déclarés `UNKNOWN`.
- Aucun fournisseur appelé ni run modifié. Aucun changement Docker,
  PostgreSQL live ou MinIO n'a été effectué.

## Prochaines actions permises

1. Décider les choix éditoriaux S6 avant de modifier le style.
2. Compléter S3 en design : CAS/lock au consume, périmètre du permis Bridge
   sur retries/correctifs, et lignage des résultats réutilisés; obtenir une
   revue du design avant implémentation.
3. Définir un oracle isolé pour l'export réel-DB et le scénario CTI complet
   avec/sans échantillon. Toute interaction fournisseur ou mutation live
   requiert une autorisation séparée conforme aux restrictions G2/TLP,
   `external_llm_allowed` et `do_not_submit`.

Les vérifications décrites sont celles réellement consignées dans les
rapports; ce handoff n'ajoute aucun résultat de test.
