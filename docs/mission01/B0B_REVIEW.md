# Revue indépendante M01-B0B

## Portée et verdict

- **TASK_ID** : `M01-B0B-REVIEW`
- **Cible** : `289903beab182f552d8b8f016d500fbab57139fb`, parent `977468078379cdc90ab7897d80bf0e739968e8f8`, base Mission 01 `058bb4af2278e3941956e523c5a1d560208de228`.
- **Verdict** : **FAIL** pour la chaîne B0.4/B0.6 et le gate B0. La structure d'export B0.7 est largement présente et déterministe, mais elle omet des métadonnées et décisions déjà persistées, puis les décrit comme absentes. La trace ne démontre donc pas la chaîne reconstructible demandée.
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

## Inconnues et action attendue

- **UNKNOWN** : contenu et état réel de toute investigation; aucune ligne DB ni aucun blob runtime n'a été ouvert.
- **UNKNOWN** : transport Bridge réellement remis/reçu, lecture effective d'un fichier prompt, payload HTTP observé et reçu fournisseur. L'export ne doit pas déduire ces faits d'un ID ou d'un ordre temporel.
- **UNKNOWN** : fonctionnement du lecteur contre PostgreSQL réel; seuls les tests du projecteur sont exécutés.
- Avant d'annoncer le gate B0, projeter les métadonnées allowlistées des blobs `operator-advisor-call.v1`, inclure le journal canonique `AnalystDecisionRow` et l'identité d'entrée déjà disponible, corriger le libellé des non-observations, puis ajouter des oracles synthétiques sur ces sources et sur le lecteur/reprise. Vérifier ensuite l'intégration avec la réparation B0A-R1.

## Coût

**UNKNOWN** — coût de tokens/fournisseur non mesuré; aucun fournisseur n'a été appelé.
