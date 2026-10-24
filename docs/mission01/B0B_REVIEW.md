# Revue indépendante M01-B0B

## Re-revue après 41c8869 (M01-B0B-REVIEW-R2)

- **Cible** : `41c88698143f7858dada01097cd22e42e8d1cfa1`, re-vérifiée sur le delta `289903beab182f552d8b8f016d500fbab57139fb..41c88698143f7858dada01097cd22e42e8d1cfa1`.
- **TASK_ID** : `M01-B0B-REVIEW-R2`.
- **Verdict** : **PASS_WITH_CHANGES**. Les quatre lacunes de projection de la revue initiale sont corrigées. Un texte statique de `RUN_TRACE.md` affirme toutefois encore que les métadonnées et références désormais projetées n'existent pas; corriger cette contradiction avant de considérer le livrable B0.7 comme final.
- **Constats actifs** : 3 (1 `CONFIRMED / MEDIUM`, 1 `UNKNOWN / MEDIUM`, 1 `PLAUSIBLE / LOW`). Les quatre constats fonctionnels F1–F4 de la revue initiale sont reclassés comme fermés.
- **Portée** : revue adversariale des sources d'export, du lecteur, du script et des tests au commit cible, ainsi que des contrôles demandés. Le commit `41c8869` ajoute/modifie les quatre fichiers d'audit; aucun accès à une base, aucun blob de runtime, appel Bridge/fournisseur ou effet live n'a été effectué.

### Constats reclassés

#### F1 — Métadonnées de `operator-advisor-call.v1`

**REJECTED / LOW — défaut initial fermé par la réparation.** Le lecteur borne la lecture du blob et le projecteur compare son SHA-256 à celui de l'observation avant `json.loads`; il exige ensuite le schéma attendu (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:178-208`, `backend/src/cti_app/application/investigation_audit_export.py:619-659`). La projection produit une allowlist de valeurs typées et hachées, y compris pour le manifeste imbriqué, et n'émet pas les textes du dossier, prompt, réponse ni corps HTTP (`investigation_audit_export.py:391-458, 661-785`).

La chaîne explicite relie observation, référence(s) d'invocation, identifiant Bridge typé `conversation_turn`, tour chargé et ModelRun du tour (`investigation_audit_export.py:1080-1137`). Elle ne choisit pas une invocation quand plusieurs liens existent. L'idempotency key est hachée. Les matériaux exposent les hashes/tailles disponibles, états de complétude, durées, usage, hash de version de parseur et référence de reparse (`:495-616, 661-785`). Le payload HTTP observé et la copie expurgée restent `UNKNOWN` avec raison; l'admission demandée, l'historique de transitions, coût monétaire et certains liens restent aussi explicitement inconnus (`:923-977`). Aucun accusé de lecture Bridge ou reçu fournisseur n'est inventé.

Le test synthétique met des marqueurs privés dans les sorties, corps HTTP, raisons, libellés et contenu imbriqués, puis vérifie leur absence des quatre artefacts (`backend/tests/test_investigation_audit_export.py:442-493, 834-885`). L'allowlist contraint les chaînes de statut/type, hache les IDs opaques, valide les SHA et n'accepte que des entiers non négatifs aux emplacements de compteurs (`investigation_audit_export.py:253-279, 363-388, 391-458`). Aucun contournement par clé imbriquée, valeur numérique ou chaîne encodée n'a été identifié par inspection.

#### F2 — Décisions opérateur

**REJECTED / LOW — défaut initial fermé par la réparation.** Le lecteur charge `AnalystDecisionRow`; l'événement expose type, type/cible, heure, et empreintes SHA de l'acteur, de la raison, de l'identifiant de corrélation et des détails (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:77-79, 138-149`; `backend/src/cti_app/application/investigation_audit_export.py:1140-1167`). Les détails libres ne sont pas copiés. Le manifeste dit maintenant que les raisons sont hachées (`:2466-2471`) au lieu d'affirmer qu'aucune raison n'est persistée.

#### F3 — Paquet d'entrée

**REJECTED / LOW — défaut initial fermé par la réparation.** Blob ID, `input_sha256`, artefact de synthèse, version/état de l'investigation et métadonnées vérifiées du record de paquet sont chargés (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:80-102`) puis projetés dans l'événement et `source_snapshot.input_pack` du manifeste (`backend/src/cti_app/application/investigation_audit_export.py:1170-1197, 2484-2505`). Le contenu du paquet reste exclu.

#### F4 — Résultat négatif

**REJECTED / LOW — défaut initial fermé par la réparation.** `negative_result` exige désormais `not_observed`, couverture `complete`, verdict `refutes` et critère de falsification non vide (`backend/src/cti_app/application/investigation_audit_export.py:1259-1267`). Le rendu dit `not observed` pour les couvertures limitées ou sans observation (`:2248-2259`). Les cas de couverture limitée/nulle et critère vide sont testés (`backend/tests/test_investigation_audit_export.py:221-288`).

### Constats actifs

#### F7 — Le texte statique de `RUN_TRACE.md` contredit les nouveaux événements

**CONFIRMED / MEDIUM.** La trace affiche d'abord le `bridge_invocation_id`, les hashes prompt/dossier/réponse et l'état extraits du blob (`backend/src/cti_app/application/investigation_audit_export.py:2261-2275`). Plus loin, elle affirme que ces valeurs sont `UNKNOWN`, que le schéma ne stocke pas les six hashes de matériau et que l'index ne développe pas les références de prompt/sortie (`:2350-2359`). Ces deux dernières affirmations sont directement contredites par l'allowlist et les entrées d'index pour blobs prompt et réponse (`:693-779, 1925-1941`). Le texte explicatif n'a pas suivi la réparation et rend la trace lisible mais contradictoire. Le test de fuite/corrélation ne vérifie pas cette section statique. Mettre à jour ce passage pour distinguer hashes retrouvés, payload HTTP/copie expurgée toujours inconnus et références indexées.

#### F5 — Runtime PostgreSQL et scénarios de reprise non démontrés

**UNKNOWN / MEDIUM.** Le module annonce explicitement que son comportement runtime PostgreSQL n'est pas testé (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:1-5`). L'inspection confirme que les lignes canoniques de l'investigation sont lues dans une transaction `REPEATABLE READ READ ONLY` (`:167-176`) sans chemin d'écriture. La lecture des blobs intervient après cette transaction et le catalogue fait un `get` dans un UoW séparé sans `commit` (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:178-208`; `backend/src/cti_app/application/blobs.py:49-54`; `backend/src/cti_app/infrastructure/database/uow.py:410-425`). Il n'y a donc pas de preuve d'un unique snapshot RR couvrant également la lecture du catalogue; chaque payload est cependant vérifié contre le SHA de son observation. Aucun test PostgreSQL n'a été exécuté.

Les tests prouvent les projections avec données synthétiques, le corpus de 778 invocations, une reprise depuis le fichier événements après suppression des fichiers dérivés, deux exports concurrents en threads, des checkpoints avec événement manquant/ID périmé, l'absence de marqueurs de contenu et un manifeste de 2 050 entrées tronqué à 2 048 tout en gardant les compteurs complets (`backend/tests/test_investigation_audit_export.py:62-149, 152-219, 291-940`). Ils n'exercent pas PostgreSQL, un arrêt de processus pendant le remplacement, plusieurs processus, une corruption gzip brute ou un bundle à manifeste incohérent. Le code écrit le checkpoint événements atomiquement d'abord et le valide avant de reconstruire les autres fichiers (`investigation_audit_export.py:1787-1834, 2577-2648`); ces scénarios restent des limites de preuve, pas des défaillances observées.

#### F6 — Taille et dispersion des responsabilités

**PLAUSIBLE / LOW.** Au commit cible, le projecteur, les règles de sûreté des champs, le rendu Markdown, manifeste/index, compression, validation de checkpoint et écriture restent regroupés dans un module de 2 655 lignes; le lecteur fait 209 lignes et le fichier de tests 940 (`backend/src/cti_app/application/investigation_audit_export.py:1-2655`; `backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:1-209`; `backend/tests/test_investigation_audit_export.py:1-940`). Le texte statique périmé (F7) illustre le risque de faire diverger documentation de sortie et événements. Une séparation ultérieure des projections, formats et reprise, et une source unique pour les descriptions de lacunes, réduirait ce risque; ce point seul ne bloque pas le lot.

### Contrôles sans régression

- **REJECTED / LOW — `self_sha256: NOT_COMPUTED`** : le manifeste documente cette limite; les SHA du gzip, de la trace et de l'index sont calculés (`backend/src/cti_app/application/investigation_audit_export.py:2509-2525`).
- **REJECTED / LOW — gzip ou index révélant le contenu** : la politique dit que gzip est une compression et non une protection; l'index ne contient que des références, sans payload ni URL signée, et rappelle l'autorisation applicative requise (`:1925-1971, 2544-2559`).
- **CONFIRMED / LOW — déterminisme** : gzip avec `mtime=0`, événements triés et ID événementiel stable (`:824-825, 871-880, 1744-1746, 1779-1784`). Le test 778 vérifie égalité des quatre artefacts; le test de concurrence vérifie la cohérence finale via les hashes de manifeste (`backend/tests/test_investigation_audit_export.py:62-89, 127-149`).
- **CONFIRMED / LOW — périmètre DB/schema du commit cible** : le commit cible ne modifie que le script, l'exporteur, le lecteur et son test; pas de migration, table ou journal canonique ajouté. Un verrou local `.RUN_AUDIT_EXPORT.lock` est créé dans le répertoire d'export; ce n'est pas un journal de données.
- L'ordre de chronologie n'est pas présenté comme causalité (`investigation_audit_export.py:871-880, 1744-1769, 2555-2559`); les corps complets ne sont pas recopiés dans chaque événement. L'index ne constitue pas une capacité d'accès.

### Commandes et résultats R2

Le commit exact a été extrait avec `git archive` dans `/tmp/autowork-b0b-r2-dFHwOt`; `backend/.venv` y pointe vers l'environnement du worktree B0B demandé. Les sources testées sont donc celles de `41c8869`. Aucun `uv run`, Conda, pytest nu, DB, réseau ou fournisseur n'a été utilisé.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_audit_export.py -q --tb=short
14 passed in 2.40s

PYTHONPATH=$PWD/src .venv/bin/python scripts/export_investigation_audit.py --help
exit 0; aide CLI affichée

.venv/bin/python -m ruff check scripts/export_investigation_audit.py src/cti_app/application/investigation_audit_export.py src/cti_app/infrastructure/database/investigation_audit_reader.py tests/test_investigation_audit_export.py
All checks passed!
```

### Inconnues et coût

- **UNKNOWN** : comportement de l'adaptateur contre un PostgreSQL réel; aucune base ni investigation n'a été lue.
- **UNKNOWN** : transport Bridge réellement remis/reçu, payload HTTP observé, sémantique de lecture prompt-as-file et reçu fournisseur. Le code ne les déduit pas des IDs ou de l'ordre temporel.
- **UNKNOWN** : résultat live, complétude sémantique d'un modèle, contenu YARA/VT ou QA/PDF hors des enregistrements fournis au projecteur.
- **UNKNOWN** : coût fournisseur; aucun fournisseur n'a été appelé et aucun coût token n'a été mesuré.

## Revue initiale sur 289903b — portée et verdict

- **TASK_ID** : `M01-B0B-REVIEW`
- **Cible** : `289903beab182f552d8b8f016d500fbab57139fb`, parent `977468078379cdc90ab7897d80bf0e739968e8f8`, base Mission 01 `058bb4af2278e3941956e523c5a1d560208de228`.
- **Verdict initial à 289903b** : **FAIL** pour la chaîne B0.4/B0.6 et le gate B0. La re-revue ci-dessus reclassifie F1–F4 après la réparation `41c8869` et relève le résiduel F7.
- **Constats** : 6 constats (4 confirmés, 1 plausible de maintenance, 1 inconnu sur la couverture des oracles), plus 1 contrôle rejeté.
- **Périmètre** : lecture du commit archivé et de ses sources liées, tests hors ligne. Aucun accès DB/runtime, aucune lecture de blob canonique, aucun appel fournisseur ou live, aucune modification de code.
- Le registre `docs/mission01/TASKS.md` n'existe ni à la base ni dans l'arbre du commit cible. J'ai consulté sa copie dans l'intégration `8096523` : lignes 38–41 indiquent que B0A est en échec de revue avec R1 en cours, et que B0B est encore `RUNNING`. Le commit cible descend directement de `9774680`; son intégration avec la réparation R1 n'est pas établie par les vérifications ci-dessous.

## Constats

### F1 — Métadonnées canoniques d'appel Bridge ignorées

**CONFIRMED / HIGH** — L'export affirme que les étapes dossier/prompt/réponse ne sont pas persistées (`backend/src/cti_app/application/investigation_audit_export.py:399-436`, `1665-1680`) et le lecteur précise qu'il ne lit pas les blobs (`backend/src/cti_app/infrastructure/database/investigation_audit_reader.py:34-37`). Pourtant, l'appel `ProductionEvidenceRecorder.record_json` persiste un `operator-advisor-call.v1` lié à l'observation avec `bridge_invocation_id`, `idempotency_key`, références/hashes/tailles du prompt, du dossier et de la sortie, état de soumission, durées et manifeste de complétude (`backend/src/cti_app/application/investigation_advisor_tools.py:751-784`, `822-880`; `backend/src/cti_app/application/evidence_recorder.py:168-187`). L'observation référence explicitement l'invocation et son blob (`investigation_audit_export.py:514-541`).

Le format standard peut conserver sa règle de ne jamais exporter les textes ou corps : une projection à liste blanche des métadonnées du blob permettrait de corréler ce qui est déjà stocké. En l'état, `UNKNOWN` ne signifie pas toujours « jamais persisté » : pour ces champs, la donnée existe mais cette projection ne la lit pas. Les détails d'un payload HTTP réellement observé et un reçu fournisseur restent à laisser `UNKNOWN` s'ils ne figurent pas dans ce contrat.

### F2 — Le journal des décisions opérateur existant est omis

**CONFIRMED / HIGH** — `AnalystDecisionRow` persiste le type de décision, la cible, l'acteur, la raison, le `correlation_id`, l'heure et les détails (`backend/src/cti_app/infrastructure/database/models/production.py:415-443`). Le dépôt expose `list_for_investigation` (`backend/src/cti_app/infrastructure/database/repositories/production.py:118-155`) et le code écrit notamment les décisions d'approbation/rejet de pivots et de réconciliation d'invocation (`backend/src/cti_app/application/investigation_governance.py:197-205`, `backend/src/cti_app/application/investigation_service.py:1714-1731`). Le lecteur d'audit ne charge pas ce dépôt (`investigation_audit_reader.py:13-32, 44-121`).

Le manifeste affirme donc à tort qu'aucune raison de décision n'est stockée (`investigation_audit_export.py:1683-1686`), tandis que l'état opérateur ne garde que des empreintes de questions/actions et donne les raisons de transition comme inconnues (`:458-509`). Ce choix omet les décisions de pistes retenues/écartées et leurs liens explicites. Les raisons libres et identités peuvent rester expurgées ou hachées dans l'export standard; les décisions typées, cibles, références de corrélation et empreintes doivent toutefois être projetées.

### F3 — Le manifeste ne lie pas l'export au paquet d'entrée canonique

**CONFIRMED / MEDIUM** — La ligne d'investigation possède déjà `input_pack_blob_id`, `input_sha256`, `synthesis_artifact_id`, version et état (`backend/src/cti_app/infrastructure/database/models/production.py:375-412`). Le lecteur récupère cette ligne mais ne transmet que `pivot_conversation_id` et les autres sous-ensembles listés (`investigation_audit_reader.py:38-40, 76-120`). Le manifeste référence le digest projeté, les versions, les références des événements exportés et le pivot (`investigation_audit_export.py:1701-1706`), mais pas l'identité du paquet d'entrée ni son hash. Il ne lie donc pas l'audit au paquet initial pourtant disponible dans la ligne déjà lue.

### F4 — Une non-observation avec couverture limitée est rendue comme résultat négatif

**CONFIRMED / MEDIUM** — `_experiment_event` définit `negative_result` par le seul fait que `actual_outcome == not_observed` (`investigation_audit_export.py:586-606`). Le rendu Markdown transforme ensuite ce booléen en libellé `negative` (`:1498-1504`). Le test construit précisément une expérience `inconclusive`, `not_observed`, à couverture `limited`, puis attend `negative_result == true` (`backend/tests/test_investigation_audit_export.py:217-235, 488-493`). Le domaine interdit de réfuter par non-observation lorsque la couverture est limitée ou nulle (`backend/src/cti_app/domain/investigation_experiment.py:274-282`). La trace expose aussi verdict et couverture, mais le libellé « négatif » reste plus fort que les données; afficher `not observed` évite cette ambiguïté.

### F5 — Les oracles couvrent le projecteur synthétique, pas le chemin canonique complet

**UNKNOWN / MEDIUM** — Les cinq tests passent. Le test de 778 invocations construit des objets synthétiques et vérifie déterminisme et reprise après suppression de la trace, de l'index et du manifeste (`backend/tests/test_investigation_audit_export.py:61-88`). Deux tests refusent un checkpoint avec événement manquant ou identifiant périmé (`:91-124`), et un test concurrent exerce deux threads sur des snapshots synthétiques (`:126-148`). La fixture de confidentialité est aussi faite de `SimpleNamespace` et ne contient pas de blob `operator-advisor-call.v1` (`:151-396`).

Ces oracles ne vérifient pas le lecteur SQL, la transaction PostgreSQL, les décisions persistées, l'identité du paquet d'entrée, ni la projection expurgée des blobs d'appel. Ils ne simulent pas non plus une interruption au milieu des remplacements atomiques ou plusieurs processus. Le code exécute bien `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` avant les lectures (`investigation_audit_reader.py:124-131`) et les écritures SQL ne sont pas présentes dans ce lecteur; cette propriété est confirmée par inspection, mais pas par un test DB. La reprise depuis un checkpoint complet et la détection de corruptions testées sont positives, sans prouver toute la cohérence d'un bundle canonique mixte de 778 invocations.

### F6 — Le module d'export cumule trop de responsabilités

**PLAUSIBLE / LOW** — Le module d'application fait 1 852 lignes et contient les projecteurs d'événements, règles d'expurgation, index, rendu Markdown, manifeste, sérialisation gzip, validation de checkpoint, verrouillage et écritures (`investigation_audit_export.py:1-1852`). Les lacunes sont décrites dans plusieurs projecteurs puis répétées dans le manifeste; un ajout peut donc faire diverger la donnée exportée et son explication. Scinder le projecteur source, les formats d'export et l'écriture/reprise, et centraliser les raisons de lacune, réduirait le coût de revue. Ce point de maintenance ne bloque pas seul le lot.

## Contrôles adversariaux satisfaits

- **REJECTED / LOW — SHA du manifeste** : `RUN_MANIFEST.json` annonce explicitement `self_sha256: NOT_COMPUTED` avec raison; les SHA de `RUN_EVENTS.jsonl.gz`, `RUN_TRACE.md` et `RUN_INDEX.json` sont calculés (`investigation_audit_export.py:1708-1723`). C'est acceptable et documenté.
- Le manifeste dit que gzip est une compression, pas une protection de confidentialité (`:1741-1751`).
- L'index contient des références, pas les contenus ni des URL signées; sa politique dit que l'autorisation applicative existante reste requise et que les références ne sont pas des capacités porteuses (`:1367-1372`, `:1746-1749`). C'est une indication de politique, pas un contrôle d'accès appliqué par le fichier lui-même.
- Le lecteur est en lecture seule par transaction et le commit n'ajoute ni table ni journal canonique. Les contenus libres des invocations, observations, expériences, avis et conversations ne sont pas sérialisés par le projecteur; les fixtures testent l'absence de marqueurs synthétiques privés (`investigation_audit_export.py:1-5, 514-541, 572-623, 627-707, 840-925`, `tests/test_investigation_audit_export.py:546-586`). L'absence de fuite via un blob non lu n'a toutefois pas été démontrée par ces tests.
- Les événements sont triés par horodatage/type/identité et le document précise que l'ordre ne prouve pas la causalité (`investigation_audit_export.py:335-342, 1058-1085, 1451-1458`). Les messages complets ne sont pas recopiés dans chaque événement.

## Commandes et résultats

Le commit cible a été extrait par `git archive` dans `/tmp`; `backend/.venv` y pointe vers l'environnement demandé du dépôt. Pas de `uv run`, Conda, pytest nu, DB, fournisseur ni réseau live.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_investigation_audit_export.py -q --tb=short
5 passed in 2.02s

PYTHONPATH=$PWD/src .venv/bin/python scripts/export_investigation_audit.py --help
exit 0; aide CLI affichée

.venv/bin/python -m ruff check scripts/export_investigation_audit.py src/cti_app/application/investigation_audit_export.py src/cti_app/infrastructure/database/investigation_audit_reader.py tests/test_investigation_audit_export.py
All checks passed!
```

## Inconnues constatées à la revue initiale

- **UNKNOWN** : contenu et état réel de toute investigation; aucune ligne DB ni aucun blob runtime n'a été ouvert.
- **UNKNOWN** : transport Bridge réellement remis/reçu, lecture effective d'un fichier prompt, payload HTTP observé et reçu fournisseur. L'export ne doit pas déduire ces faits d'un ID ou d'un ordre temporel.
- **UNKNOWN** : fonctionnement du lecteur contre PostgreSQL réel; seuls les tests du projecteur sont exécutés.
- Les actions alors attendues — projection allowlistée, décisions, paquet d'entrée, correction des non-observations et oracles synthétiques — sont réévaluées dans la section R2 ci-dessus. Le constat F7 et les inconnues PostgreSQL/reprise restent ouverts.

## Coût

**UNKNOWN** — coût de tokens/fournisseur non mesuré; aucun fournisseur n'a été appelé.
