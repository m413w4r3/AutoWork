# RuleWorkbench local

`RuleWorkbench` construit et teste des drafts YARA locaux depuis des candidats
P09/P10 mesurés. Il ne publie ni n'archive de règle.

## API

- Domaine : `RuleMotif`, `RuleMotifKind`, `RuleMotifProvenanceKind`,
  `RuleTestCase`, `RuleTestCorpusRole`, `RuleTestReport` et
  `RuleVersionComparison` dans `cti_app.domain.rule_workbench`.
- Application : `RuleWorkbench.propose_from_features(...)`,
  `validate_motifs(...)`, `build_rule(...)`, `compile_and_test(...)`,
  `revise(...)`, `revise_with_report(...)` et `compare(...)` dans
  `cti_app.application.rule_workbench`.
- Ports : `RuleCorpusBytesPort` résout un SHA-256 vers les octets en quarantaine;
  `YaraEnginePort` compile et applique une règle. La persistance des versions
  passe par `DetectionCandidateRepository` d'A1.
- Infrastructure : `IsolatedYaraRunner` et `YaraRunnerLimits` dans
  `cti_app.infrastructure.yara_runner`.

`propose_from_features` projette les invariants P09 et les features SMDA vers
des motifs triés de façon déterministe. Les `SampleFeatureProvenance` et
`CodeFeatureProvenance` conservent les offsets mesurés; une feature SMDA n'est
retenue que depuis une extraction réussie. Les statuts rejetés/non sélectifs,
les catégories de bruit, la banalité connue, les invariants multi-familles et
les claims non confirmés sont exclus. Les mesures positives, goodware et de
prévalence restent attachées au motif. Les registres des motifs non
discriminants et des services légitimes sont injectés depuis leurs adaptateurs
existants.

`build_rule` n'accepte qu'un catalogue fermé de conditions (`any`, `all`,
`majority`). Les identifiants YARA sont dérivés du contenu; les valeurs de
motifs et métadonnées sont échappées. Un résultat de rendu n'est pas une
acceptation : `revise` compile et teste localement avant d'ajouter la version
immuable suivante. Une version avec un faux positif goodware est `rejected`;
une mesure incomplète reste `candidate`; aucune version n'est automatiquement
`approved`.

Les versions persistées requièrent des références vers des observations de
preuve pour chaque motif, conformément à `MotifProvenance` d'A1. Les offsets
peuvent être absents lorsque la source ne les mesure pas.

## Chargement et mesures goodware

`make goodware-build` construit l'index v2 hors dépôt depuis `GOODWARE_SOURCE_DIR` (défaut : `$HOME/tools/yarGen/dbs`) vers `GOODWARE_ARTIFACT_DIR` (défaut : `$HOME/.local/share/autowork/goodware/v2`). Un artefact existant est vérifié et réutilisé; `GOODWARE_REBUILD=1` demande explicitement sa reconstruction.

`make goodware-load` passe par `GoodwareService.import_index`, stocke les blobs avec leurs empreintes, puis prépare le cache configuré par `GOODWARE_CACHE_ROOT`. L'import et le lien optionnel `GOODWARE_INVESTIGATION_ID=<uuid>` sont idempotents. `make goodware-status` affiche les versions, empreintes et nombres de la base sans la modifier. Ces commandes utilisent la configuration backend courante pour PostgreSQL et MinIO.

Quand l'investigation a une baseline liée, `rule.propose` mesure les motifs chaîne et hexadécimaux pris en charge dans l'index. La version et sa signature exposent le nombre d'occurrences, le statut et la banalité; une fréquence au seuil `GOODWARE_BANAL_COUNT` est marquée `BANAL` et non discriminante. Sans baseline, le motif porte le statut `unmeasured` avec sa raison.

L'index de fréquences ne contient pas les octets des fichiers goodware. `rule.test` mesure les faux positifs sur les fichiers importés dans le corpus d'enquête avec le rôle `goodware`; son observation déclare les hashes qui matchent et le statut de mesure. Si aucun fichier goodware n'est importé, l'observation indique explicitement pourquoi la mesure n'a pas eu lieu. `rule.propose_bridge` ne fait qu'importer et compiler le brouillon Bridge; appeler `rule.test` pour mesurer son résultat.

Au démarrage, l'application journalise un WARNING si aucune baseline n'est enregistrée. `/api/health/ready` expose le statut et les métadonnées des baselines sans les inclure dans le calcul de disponibilité de l'application.

## Rapport et portée du corpus

Chaque `RuleTestReport` déclare, pour les rôles confirmed positive,
out-of-cluster candidate, goodware et holdout, les SHA-256 exacts, matches,
non-matches, cas non mesurables et erreurs. Il comprend la version moteur, le
biais de sélection et la déclaration de couverture. Une absence d'octets reste
non mesurable et n'est jamais comptée comme un non-match.

Le rapport est retourné à l'appelant; A1 ne fournit pas de dépôt de rapports
de test. `DetectionCandidateVersion.test_refs` référence les IDs des cas de
corpus remis à cette révision. `revise_with_report(...)` retourne ensemble la
version et son rapport; `revise(...)` expose la version seule. Le workbench ne
crée pas de table ni de migration dans ce lot.

`compare` reteste les deux sources sur le corpus transmis et calcule les
matches gagnés/perdus par rôle, ainsi que les faux positifs goodware ajoutés ou
retirés. Les holdouts et candidats hors cluster restent des mesures séparées;
leur composition et les biais de sélection doivent être décrits par
l'appelant.

## Isolation du moteur

Chaque compilation ou application démarre un worker Python distinct avec
`sys.executable -I`. La source et les échantillons sont écrits dans un
répertoire temporaire; les fichiers d'échantillons sont nommés par leur
SHA-256 et revalidés avant exécution. Le worker n'a pas de réseau, reçoit un
environnement réduit et applique `resource.setrlimit` pour le temps CPU, la
mémoire, la taille des fichiers et les descripteurs. Le parent applique aussi
un délai mural et une limite de sortie. Aucun octet d'échantillon n'est écrit
dans les logs.

Limites par défaut :

- 10 secondes de temps mural par opération, 8 secondes CPU ;
- 512 MiB de mémoire ;
- 64 KiB de sortie worker ;
- 1 MB de source, 256 échantillons, 64 MiB par échantillon et 256 MiB au total.

Une dépendance YARA absente retourne `engine_unavailable`. Compilation invalide,
timeout, ressource dépassée et crash ont des statuts distincts et locaux à la
règle.

## Hors périmètre

- Aucune publication/approbation ni archive de règles : ces opérations exigent
  une revue explicite dans un lot ultérieur.
- YARA-Signator et YaraGenerator ne sont ni installés, ni exécutés, ni copiés.
- Aucun accès à la base `yara_signator_postgres_testing`.
- Aucun scan réseau, appel fournisseur ou malware réel; les tests utilisent
  uniquement des octets synthétiques inoffensifs.
