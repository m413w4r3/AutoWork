# Workflow emulator

## Niveaux de preuve

- **Unitaire** : décisions locales avec doubles ciblés.
- **Émulation workflow** : routes et services applicatifs réels selon le scénario, état canonique PostgreSQL, dépendances externes déterministes.
- **Rendu réel** : compilation et inspection du document final ; non couverte ici.

## Scénarios

- `test_w2_inclusion_exclusion.py` (`workflow_fast`) : runs READY insérés et artefacts VERIFIED préparés par le helper PostgreSQL existant, exclusion via API, Review, manifeste V2 et release.
- `test_w1_progressive_abc.py` (`workflow_full`) : Discovery fixturée (sorties persistées via les repositories, ni route, ni parseur, ni service Discovery) ; Fusion cumulative via le vrai `CumulativeDiscoveryService.reconcile_batch` avec planner déterministe ; Selection via route officielle ; Production via vrais jobs ; Composition, Review, Acceptation et Assembly réels.
- `test_network_guard.py` (`workflow_fast`) : vérifie sans base que les tentatives socket, DNS et HTTP sont refusées avant tout accès réseau.

Les lots Discovery, réponses de recherche, captures HTTP, réponses d’extraction et modèles sont locaux et versionnés. Les octets de capture sont uniques par sujet. Les routes, repositories, Review, Composition, Publication, Assembly et, pour W1, transitions de production sont réels. L’émulation ne lance ni modèle distant, ni Bridge réel, ni compilateur PDF/Typst. Le DSN PostgreSQL est contrôlé avant la préparation de la base. Le garde Python remplace `connect`, `connect_ex`, `getaddrinfo`, `gethostbyname`, `gethostbyname_ex` et `gethostbyaddr` pour refuser les cibles non locales. Il ne couvre pas les sous-processus, le code natif qui contournerait ces appels Python, ni les sockets déjà connectées avant le test.

Pour rendre obligatoire l’exécution des scénarios PostgreSQL dans un job de validation, définir `WORKFLOW_EMULATOR_REQUIRED=1`. Si `TEST_POSTGRES_ADMIN_DSN` manque et qu’un test d’intégration est sauté par une fixture, ce saut devient un échec qui nomme le scénario et sa cause. Sans cette variable, les scénarios PostgreSQL gardent leur comportement local habituel et peuvent être sautés si le DSN est absent.

## Ce qui n’est pas encore prouvé

Dans W1 et W2, les invariants d’immuabilité historique du manifeste et `deferred` distinct d’une exclusion sont exécutés et tracés avec `checked=0` : ces scénarios ne contiennent aucun manifeste historique ni sujet `DEFERRED`. Ils ne constituent donc pas une preuve de ces cas. Les scénarios W3, W6 et W8 du lot suivant porteront cette couverture. Le rendu réel PDF/Typst reste également hors périmètre.

Lancer depuis `backend/` :

```sh
make test-integration INTEGRATION_TEST_PATH=tests/workflow_emulator INTEGRATION_PYTEST_ARGS="-q --tb=short"
```
