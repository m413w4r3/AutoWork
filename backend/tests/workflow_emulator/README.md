# Workflow emulator

## Niveaux de preuve

- **Unitaire** : décisions locales avec doubles ciblés.
- **Émulation workflow** : routes et services applicatifs réels selon le scénario, état canonique PostgreSQL, dépendances externes déterministes.
- **Rendu réel** : compilation et inspection du document final ; non couverte ici.

## Scénarios

- `test_w2_inclusion_exclusion.py` (`workflow_fast`) : runs READY insérés et artefacts VERIFIED préparés par le helper PostgreSQL existant, exclusion via API, Review, manifeste V2 et release.
- `test_w1_progressive_abc.py` (`workflow_full`) : Discovery fixturée (sorties persistées via les repositories, ni route, ni parseur, ni service Discovery) ; Fusion cumulative via le vrai `CumulativeDiscoveryService.reconcile_batch` avec planner déterministe ; Selection via route officielle ; Production via vrais jobs ; Composition, Review, Acceptation et Assembly réels.
- `test_w3_unproduced_subject_excluded.py` (`workflow_fast`) : Discovery fixturée, Fusion et Selection réelles ; A passe par les vrais jobs de production déterministes ; B reste sans run. Le scénario principal vérifie le diagnostic Review `subject_deferred`, le refus du gel avec `composition_incomplete`, puis l’exclusion explicite sans `production_run_id`, l’absence de tout run/job/model run pour B et son absence du blob assemblé. Le test séparé `test_w3_deferred_subject_review_does_not_offer_acceptance` vérifie que `Review.can_accept` reste faux. Il échoue actuellement avec `can_accept=true`, bien que le diagnostic Review existe et que le gel refuse l’acceptation.
- `test_w4_discovery_after_production.py` (`workflow_fast`) : Discovery D1/D2 fixturée avec captures uniques ; Fusion cumulative réelle ; Selection et production de A réelles ; composition épinglée avant D2. D2 apporte un candidat distinct, réconcilié dans un nouveau snapshot, puis `FusionService.merge` le rattache à l’identité Discovery canonique sélectionnée pour A. Le snapshot et les membres changent, `discovery_update_available=true`, sans changer le run, l’artefact, le snapshot de production historique ni la composition épinglée. A reste publiable.
- `test_w6_concurrency_and_resume.py` (`workflow_fast`) : READY runs et artefacts VERIFIED insérés par le helper PostgreSQL ; mutations de Composition, acceptation, Review et Assembly réelles. Vérifie conflit de version, accept concurrent/idempotent, reprise après écriture de blob interrompue côté test, rejet d’un artefact `stale` et hashes des manifestes historiques.
- `test_w8_stale_composition_version.py` (`workflow_fast`) : READY runs et artefacts VERIFIED insérés par le helper PostgreSQL ; Composition, Review, Acceptation et Assembly réels. Un `accept` avec la version obsolète renvoie le conflit typé sans manifeste/release ; la version courante publie A/C et conserve B comme exclusion explicite.
- `test_network_guard.py` (`workflow_fast`) : vérifie sans base que les tentatives socket, DNS et HTTP sont refusées avant tout accès réseau.

Les lots Discovery, réponses de recherche, captures HTTP, réponses d’extraction et modèles sont locaux et versionnés. Les octets de capture utilisés par W1, W3 et W4 sont uniques grâce à `uuid4`; W2, W6 et W8 n’utilisent pas de capture car leurs runs/documentaires sont fixturés. Les routes, repositories, Review, Composition, Publication et Assembly sont réels ; les transitions de production sont réelles dans W1, W3 et W4. L’émulation n’exécute ni modèle distant, ni Bridge réel, ni compilateur PDF/Typst. Le DSN PostgreSQL est contrôlé avant la préparation de la base. Le garde Python remplace `connect`, `connect_ex`, `getaddrinfo`, `gethostbyname`, `gethostbyname_ex` et `gethostbyaddr` pour refuser les cibles non locales. Il ne couvre pas les sous-processus, le code natif qui contournerait ces appels Python, ni les sockets déjà connectées avant le test.

Pour rendre obligatoire l’exécution des scénarios PostgreSQL dans un job de validation, définir `WORKFLOW_EMULATOR_REQUIRED=1`. Si `TEST_POSTGRES_ADMIN_DSN` manque et qu’un test d’intégration est sauté par une fixture, ce saut devient un échec qui nomme le scénario et sa cause. Sans cette variable, les scénarios PostgreSQL gardent leur comportement local habituel et peuvent être sautés si le DSN est absent.

## Ce qui n’est pas encore prouvé

W1 et W2 ne contiennent ni sujet `DEFERRED` ni manifeste historique avant acceptation ; leurs appels correspondants restent tracés avec `checked=0` et ne sont pas une preuve de ces propriétés. W3 vérifie le diagnostic `subject_deferred` et le refus du gel, mais son test Review séparé prouve que `can_accept` est actuellement vrai à tort pendant que B reste deferred. W6 crée d’abord une release historique et vérifie `content_sha256` pendant les mutations et reprises avec `checked>0` (1, puis 2 et 3 manifestes historiques). Le rendu PDF/Typst réel, l’interruption et la relance d’un job de rendu, restent hors profil FAST : l’app du driver ne câble pas le routeur/dispatcher de rendu et le compilateur réel est absent. Le cas d’une décision d’exclusion périmée après changement de génération n’est pas couvert avec les seeds READY directs de W8 : ils ne possèdent pas de runner de retry/recovery, et l’app FAST ne câble pas le routeur Production. Il reste à couvrir dans un flux doté d’une transition de génération réelle.

Lancer depuis `backend/` :

```sh
make test-integration INTEGRATION_TEST_PATH=tests/workflow_emulator INTEGRATION_PYTEST_ARGS="-q --tb=short"
```
