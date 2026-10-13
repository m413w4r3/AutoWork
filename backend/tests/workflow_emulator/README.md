# Workflow emulator

## Niveaux de preuve

- **Fixturé** : des états ou réponses déterministes sont préparés localement ; le
  scénario nomme les routes et services réels qu’il traverse ensuite.
- **Service réel** : le service applicatif, les repositories PostgreSQL et les
  routes officielles indiquées par scénario sont exercés avec des dépendances
  externes déterministes.
- **Rendu réel** : le rendu passe par le vrai compilateur Typst et le PDF par les
  outils Poppler. Cela prouve l’exécution et les assertions de contenu faites,
  pas la qualité graphique ou éditoriale.
- **Navigateur réel** : Playwright Chromium traverse le frontend servi par Vite,
  son proxy API, les routes FastAPI et le PostgreSQL isolé du harness. J2 ajoute
  le vrai `JobExecutor`, la composition `build_investigation_composition`, le
  rendu Typst et l’extraction Poppler ; Bridge, VT et Shodan répondent avec des
  scripts déterministes et aucun fournisseur distant n’est contacté.

Tous les scénarios `workflow_full` et `workflow_fast` portent les deux marqueurs
`integration` et leur profil. Ils écrivent une trace JSONL pour chaque action,
utilisent des identifiants/octet uniques quand ils capturent des réponses, et
relisent les assertions canoniques en PostgreSQL.

## Planchers CI

`make test-workflow` exige les 10 scénarios `workflow_fast` (les 7 existants et
les 3 cas W13). La garde combinée `make test-workflow-full` exige 16 scénarios
`workflow_fast` et `workflow_full` (les 12 existants, W12 et les 3 cas W13).
Le job CI qui exécute uniquement `workflow_full` exige 6 scénarios ; W12 porte
ce compte de 5 à 6.

## Scénarios

- `test_w1_progressive_abc.py` (`workflow_full`) : le vrai `DiscoveryService`
  est piloté par un rapport local versionné via `ScriptedModelGateway` ; son
  parseur, son exécution par `JobExecutor` et sa persistance PostgreSQL sont
  réels. Fusion cumulative réelle avec planner déterministe, Selection par route,
  production par vrais jobs déterministes, puis Composition, Review, Acceptance
  et Assembly réels. Aucun service externe ni modèle distant n’est appelé.
- `test_w2_inclusion_exclusion.py` (`workflow_fast`) : runs READY et artefacts
  VERIFIED insérés par le helper PostgreSQL existant ; exclusion par route,
  Review, manifeste V2 et release réels.
- `test_w3_unproduced_subject_excluded.py` (`workflow_fast`, deux tests) :
  Discovery fixturée, Fusion et Selection réelles ; A passe par les vrais jobs
  déterministes, B reste sans run. Le scénario principal vérifie le diagnostic
  Review `subject_deferred`, le refus du gel avec `composition_incomplete`, puis
  une exclusion explicite sans `production_run_id`, l’absence de run/job/model
  run pour B et son absence du blob assemblé. Le test séparé vérifie que
  `Review.can_accept` est faux pour B différé.
- `test_w4_discovery_after_production.py` (`workflow_fast`) : Discovery D1/D2
  fixturée ; Fusion cumulative réelle rattache après production un candidat à
  l’identité canonique sélectionnée pour A. Le snapshot et ses membres changent,
  `discovery_update_available=true`, mais l’artefact de production historique et
  la Composition épinglée restent identiques.
- `test_w5_late_correction.py` (`workflow_full`) : A est produit dans P1 puis B
  dans P2 par les vrais jobs déterministes. Après composition et décision
  d’exclusion auditée pour A v1, une décision INCLUDE v1 est inscrite par le vrai
  `EditionReviewService`, puis `ProductionEditorialEnrichmentRevisionService`
  corrige A et crée un nouvel artefact PUBLICATION. v1 passe à `STALE` avant
  toute manipulation fixturée ; sa ligne, son identité, son blob et ses octets
  sont conservés. Deux décisions fixturées, persistées par le repository réel,
  ne doivent pas s’appliquer quand seule leur version ou leur hash diffère ; la
  lecture passe par le vrai Review. Un bref
  état Review fixturé remet v1 à VERIFIED pour isoler le blocage
  `superseded_artifact_available`, puis restaure l’état issu du parcours officiel.
  Le test vérifie les deux gardes d’acceptation et les codes exacts. La preview
  de l’artefact est un double d’enregistrement : W5 ne revendique aucune preuve
  Typst/PDF.
- `test_w6_concurrency_and_resume.py` (`workflow_fast`) : runs READY et artefacts
  VERIFIED insérés ; mutations de Composition, acceptation, Review et Assembly
  réelles. Vérifie conflit de version, accept concurrent/idempotent, reprise
  après écriture de blob interrompue côté test, rejet d’un artefact `stale` et
  hashes de manifestes historiques avec `checked>0`.
- `test_w7_discovery_fusion_conflicts.py` (`workflow_full`) : A et B sont
  sélectionnés, A est produit, puis le vrai `CumulativeDiscoveryService` park
  une proposition reliant un nouvel entrant C à A dans l’état NEEDS_REVIEW. Le
  scénario vérifie l’absence d’application silencieuse, le conflit quand deux
  sujets sélectionnés sont fusionnés, puis résout explicitement la revue par
  `FusionService.resolve_review(SEPARATE)`. Les runs, artefacts et Composition
  sont relus inchangés en base. La revue de fusion n’a pas de route HTTP dans
  l’application actuelle ; ce chemin est donc prouvé au niveau service.
- `test_w8_stale_composition_version.py` (`workflow_fast`) : runs READY et
  artefacts VERIFIED insérés ; Composition, Review, Acceptation et Assembly
  réels. Un `accept` avec une ancienne version renvoie le conflit typé sans
  manifeste/release ; la version courante publie A/C et conserve B comme
  exclusion explicite.
- `test_w9_real_typst_render.py` (`workflow_full`) : le helper PostgreSQL fournit
  trois publications synthétiques minimales, ensuite Composition, Review,
  Acceptation et Assembly suivent les services/routes réels. Le vrai
  `EditionRenderService` compile avec `TypstSubprocessCompiler`; le blob PDF,
  SHA-256, pagination et chaîne manifeste/release/document/rendu sont relus en
  base. Poppler inspecte le PDF et l'extraction vérifie les titres Article A et
  B. Un Typst, un template, un bundle de polices ou Poppler manquant fait échouer
  le test avec le prérequis nommé ; aucun saut n’est possible.
- `test_w11_analyst_opinion_repin.py` (`workflow_full`) : A, B et C ont des runs
  et publications V5 indépendants. Composition, exclusion, Review, Acceptation,
  Assembly et le premier rendu Typst/Poppler sont réels. Le vrai
  `PublicationAugmentationArtifactService` crée ensuite une nouvelle PUBLICATION
  V6 sur A ; sans mutation de composition, les octets du document V5, le manifeste,
  le release et le PDF v1 restent identiques. Le repin passe ensuite par le vrai
  `EditionCompositionService.replace_entries` avec compare-and-swap de version ;
  une seconde Composition/Review/Acceptation/Assembly et un second rendu
  Typst/Poppler vérifient que seul A porte l’avis, que B/C et l’exclusion restent
  inchangés, et que le manifeste/release/PDF historiques sont encore lisibles.
  Les octets canoniques V5 du document assemblé correspondent à la projection
  pré-H1b ; le SHA-256 du PDF v1 lu en base reste identique après approbation.
  Les preuves couvrent les services PostgreSQL réels et le rendu PDF réel, sans
  appel fournisseur.
- `test_w12_investigation_discovery_chain_and_repin.py` (`workflow_full`) : un
  seul scénario compose A/B/C à partir de runs indépendants, puis enquête sur A
  par l'API et exécute G2/G3, VT, acquisition, analyse locale, relations et avis
  via les vrais jobs/services PostgreSQL avec des ports synthétiques. D1 vérifie
  que S2 est nouveau uniquement pour l'article et reste candidat jusqu'à G3 ; D2
  cite `dropped_files`/`parents` pour une transition tout en laissant le rôle
  final hypothétique ; D3 confronte C1 avec les observations relationnelles et
  statiques et rétrograde une affirmation `supported` sans observation. Une
  citation d'avis inconnue est refusée par l'API. Publication V6 ne repin pas
  automatiquement : le manifeste, le release et le PDF Typst/Poppler v1 restent
  identiques. Le CAS humain repin uniquement A, puis le nouveau PDF réel contient
  l'avis et la vérification C1 ; B/C et les artefacts historiques restent
  inchangés (D6).
- `test_w13_investigation_infra_detection_resume.py` (`workflow_fast`, trois
  tests, sans Typst) : D4 part d'un article IP-only, lit les relations VT et
  l'index Shodan daté, limite explicitement l'attribution par certificat partagé
  et l'activité à la fenêtre observée, sans téléchargement ni YARA. Une seconde
  enquête désactive VT et confirme que Shodan permet toujours de composer un
  avis. D5 utilise le vrai `RuleWorkbench`, l'isolateur YARA, l'API de revue des
  règles et l'archive liée au manifeste : v1 matche le corpus goodware et reste
  rejetée/non publiée ; un nouvel échantillon est associé, v2 est distincte,
  retestée et comparée avec des matches gagnés/perdus. L'avis est publiable avant
  l'approbation de règle ; seule l'approbation humaine du hash/version v2 apparaît
  dans un nouveau manifeste qui retient l'article, sans brouillon ni v1 bruitée.
  D7 rejoue le même `ToolInvocation` sans second téléchargement, simule une
  soumission Bridge ambiguë et vérifie que la reprise ne la resoumet pas ; le
  budget téléchargements et `do_not_submit` bloquent seulement l'acquisition
  visée, tandis que Shodan continue. Un quota VT produit un résultat local
  `unavailable` et n'empêche pas la piste Shodan.
- `investigation-opinion-repin.spec.ts` (`test-workflow-browser-investigation`) :
  Playwright ouvre A, lance l’enquête depuis l’onglet Enquête/Avis, approuve G2,
  puis demande au endpoint local réservé au harness d’exécuter le vrai job par
  `JobExecutor`. Les outils VT/Shodan reçoivent des réponses déterministes ; les
  octets acquis sont une fixture synthétique inoffensive. Les gestes UI qualifient
  le candidat par G3, révisent et approuvent la version exacte de l’avis, proposent
  la PUBLICATION V6, vérifient la composition inchangée puis réépinglent A depuis
  Composition. Acceptation, jobs d’assemblage/rendu et Typst/Poppler aboutissent
  à un PDF téléchargé depuis l’écran Publication. Le vérificateur compare son
  hash au rendu canonique PostgreSQL, extrait le texte avec `pdftotext`, vérifie
  que l’avis apparaît seulement dans l’édition de A et compare les hashes du
  manifeste, du release, du document assemblé et du PDF historiques après repin.
- `test_network_guard.py` (`workflow_fast`) : vérifie sans base que les tentatives
  socket, DNS et HTTP sont refusées avant tout accès réseau.

Les lots Discovery, réponses de recherche, captures HTTP, réponses d’extraction
et prompts de production restent locaux et versionnés. W1 exécute le vrai service
Discovery avec une réponse déterministe locale ; W3 et W4 utilisent encore les
sorties Discovery fixturées. Aucun scénario n’appelle un modèle ou une source
externe. Le garde Python refuse les destinations réseau non locales ; il ne
couvre pas les sous-processus, du code natif qui contournerait les appels Python
ni les sockets connectées avant le test.

## Prérequis du rendu réel

W9, W11 et W12 requièrent Typst 0.15.1, le bundle de templates `chpTypst`, un bundle
de polices et son fichier `typst-fonts.lock`, ainsi que `pdfinfo`, `pdffonts`,
`pdftotext` et `pdftoppm` (Poppler). Les chemins peuvent être précisés avec
`TYPST_BINARY`, `CHP_TYPST_ROOT`, `FONT_BUNDLE_ROOT` et
`TYPST_FONTS_LOCK_FILE`.

Dans l’environnement local où Typst est fourni par snap, le snap ne peut pas lire
les fichiers sous `/tmp`. Utiliser un répertoire temporaire sous le home :

```sh
mkdir -p "$HOME/typst-tmp"
TMPDIR="$HOME/typst-tmp" WORKFLOW_EMULATOR_REQUIRED=1 make test-workflow-full
```

`WORKFLOW_EMULATOR_REQUIRED=1` rend les scénarios PostgreSQL bloquants si le DSN
manque et qu’une fixture tente de les sauter. Le DSN d’intégration est contrôlé
pour n’autoriser que PostgreSQL local.

## Ce qui n’est pas encore prouvé

Les scénarios ne prouvent pas toutes les permutations de Review, Composition,
Production et fusion Discovery. W7 vérifie une résolution explicite `SEPARATE`;
le produit n’expose actuellement pas de route HTTP de revue de fusion. W5 couvre
la correction ciblée par suppression déterministe d’un diagramme existant et
utilise un double pour la preview. L’ancienne PUBLICATION v1 est observée `STALE`,
car l’écriture du nouvel enrichissement invalide les étapes en aval. La mutation
W5-d qui stalerait aussi v1 lors de `append` est équivalente et non détectable par
construction ; les mutations qui annulent le blob canonique de v1 ou altèrent
son `input_hash` sont tuées par les assertions de conservation d’historique.
W9 prouve la compilation Typst, les pages, l’inspection sans erreur bloquante et
la présence des titres extraits, mais ne prouve ni la qualité de mise en page ni
la qualité éditoriale. Les chemins de
rendu interrompu puis repris et une décision d’exclusion périmée après une
transition réelle de génération restent hors de ces scénarios.

Depuis la racine du dépôt :

```sh
make test-workflow
make test-workflow-full
```

Le parcours navigateur W10 reste une cible séparée. J2 se lance avec
`make test-workflow-browser-investigation` et requiert lui aussi
`TEST_POSTGRES_ADMIN_DSN`. Le service réservé aux tests est celui du profil
`integration-test` sur le port `TEST_POSTGRES_PORT` (55432 par défaut), jamais
le Postgres applicatif ni `yara_signator_postgres_testing` :

```sh
timeout 120s flock /tmp/autowork-integration.lock make integration-db-up
mkdir -p "$HOME/typst-tmp"
timeout 20m flock /tmp/autowork-integration.lock env \
  TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  TMPDIR="$HOME/typst-tmp" make test-workflow-browser-investigation
```

La cible crée et supprime sa propre base `cti_j2_browser_<uuid>` sur ce serveur,
sert FastAPI uniquement sur `127.0.0.1`, puis ferme le serveur après la
vérification. Elle écrit l’état, les logs, le PDF téléchargé, les traces et le
rapport Playwright sous `var/workflow-results/browser-investigation/`.

Ces cibles délèguent à `make test-integration`, définissent
`WORKFLOW_EMULATOR_REQUIRED=1`, écrivent JUnit et traces dans
`var/workflow-results/`, puis vérifient que chaque scénario collecté est
`passed` dans le rapport. Pour utiliser directement `make test-integration`,
définir également `WORKFLOW_EMULATOR_REQUIRED=1`.
