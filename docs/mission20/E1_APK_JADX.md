# M20-E1c — Documentation et skills APK/JADX

## Décision et provenance

Le worker exécute jadx en mode CLI headless pour l’analyse statique bornée des
APK. Il n’installe ni ne lance l’application. La version est fixée à `1.5.6`,
le JRE cible à Java 17, et le ZIP officiel
[`jadx-1.5.6.zip`](https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip)
est contrôlé par SHA-256 :
`545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974`.
Projet : [skylot/jadx](https://github.com/skylot/jadx/), licence Apache-2.0.

L’utilisateur a explicitement autorisé le 2026-11-10 le téléchargement de
jadx 1.5.6 depuis sa source officielle et l’installation de la JRE par apt
pour l’image `analysis-worker`. `infra/install_jadx.py` vérifie le SHA-256
avant extraction, rejette les chemins ZIP dangereux et inscrit la provenance
dans `/opt/jadx/PROVENANCE.json`. Le code et les paramètres opérateur sont
décrits dans [les outils locaux d’enquête](../investigation_local_tools.md#rétro-conception-dapk-jadx).

## Build et vérification de l’image

Recette déclarée par le test d’image :

```sh
docker compose build analysis-worker
cd backend
ANALYSIS_WORKER_IMAGE_TEST=1 \
  uv run --no-sync --python 3.12 pytest tests/test_confined_analysis_image.py -q
```

La JRE est épinglée dans `infra/analysis-worker.Dockerfile` à
`openjdk-17-jre-headless=17.0.21+9-1~deb12u1` (version relevée au premier build
du 2026-11-10). Le tag majeur 17 et `java -version` seuls ne suffisaient pas à
garantir la reproductibilité.
Le test `ANALYSIS_WORKER_IMAGE_TEST=1` vérifie notamment la présence de Java,
la version jadx `1.5.6`, son hash attesté et une JRE 17. Il démarre un processus
éphémère de vérification, pas le service complet.

Le worker utilise un heap Java de 768 MiB, `APK_ANALYSIS_ADDRESS_SPACE_LIMIT_BYTES`
à 8 GiB par défaut (espace virtuel, plage permise 8–32 GiB),
`ANALYSIS_APK_TIMEOUT_SECONDS` à 90 s par défaut (maximum 120 s), une limite
cgroup Compose de 2 GiB et `/tmp` tmpfs de 512 MiB. Voir le détail et les
plafonds de l’entrée APK dans
[la référence des outils d’enquête](../investigation_local_tools.md#rétro-conception-dapk-jadx).

Le changement de versions/familles attestées du worker change l’identité de
réutilisation des opérations confinées, car cette attestation et le schéma du
résultat participent à la clé. Un résultat terminé sous une attestation
différente n’est donc pas réutilisé sous la nouvelle identité. La connexion
de cette identité à la famille opérateur `investigation.apk` reste à confirmer
après intégration du code E1b.

## Fixture synthétique

La fixture versionnée
[décrite dans son README](../../backend/tests/fixtures/apk/README.md) est
synthétique et non malveillante. L’APK contient une permission `INTERNET`, une
activité exportée, un receiver non exporté, le marqueur
`AUTOWORK_APK_FIXTURE` et `https://fixture.example.invalid/`. Son SHA-256 est
`c97fb8885d1f65c7521b9ab6e052b1fdb0b3920488237d619f28f209706bd29e`.

Recette :

```sh
apktool b backend/tests/fixtures/apk -o /tmp/autowork-apk-fixture.apk
```

Pour retrouver les octets versionnés, normaliser le ZIP en triant les entrées
par nom, en utilisant `ZIP_DEFLATED` niveau 9, l’horodatage `1980-01-01
00:00:00`, le système créateur Unix 3 et l’attribut externe `0100644 << 16`,
puis écrire `backend/tests/fixtures/apk/autowork-apk-fixture.apk`. La recette
complète est dans le README de la fixture.

## Vérifié et restant à vérifier

**Vérifié (2026-11-10, orchestrateur M20)** :
- zip jadx 1.5.6 téléchargé après autorisation de l'utilisateur ; sha256 recalculé
  `545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974` (identique à l'API GitHub) ;
- image `analysis-worker` construite (JRE 17.0.21, `/opt/jadx` racine en lecture seule, `jadx --version` = `1.5.6`) ;
- les 5 opérations `apk.*` exécutées de bout en bout sur la fixture dans un conteneur aux contraintes de
  production (`--read-only`, `/tmp` tmpfs 512 MiB `noexec`, `--network none`, 2 GiB, 128 PID, `cap-drop ALL`) :
  toutes `SUCCEEDED` ; `ANALYSIS_WORKER_IMAGE_TEST=1 pytest tests/test_confined_analysis_image.py` : 1 passed
  (inclut maintenant une vraie décompilation de la fixture) ;
- défaut trouvé et corrigé grâce à ce test : jadx crée ses répertoires de config/cache sous `$HOME`/`XDG_*`
  (lecture seule dans l'image) et échouait avec `apk_decompilation_failed` ; le worker redirige maintenant
  `HOME`/`XDG_*` vers l'arborescence temporaire par appel (liste d'environnement de
  `analysis_subprocess.py` étendue, test dédié) ;
- schéma et noms des outils opérateur (E1b) : `apk.manifest`, `apk.classes.list`, `apk.strings.extract`,
  `apk.source.read`, `apk.source.search`, famille `investigation.apk`, 157 tests ciblés hors-ligne.

**Non vérifié** : aucun APK réel (a fortiori malveillant) n'a été décompilé ; comportement de jadx sur de
gros APK/obfuscation/multi-DEX (plafonds : 128 MiB d'arbre de sortie, 90 s) ; redéploiement de la pile avec la
nouvelle image (voir le rapport de mission).

## Correctifs E1d

- `apk.source.search` refuse les répétitions non bornées, limite les bornes à 32, les quantificateurs à trois,
  leur facteur de combinaison à 256 et rejette lookarounds, backreferences, alternations et groupes répétés contenant une répétition. Chaque
  ligne envoyée au moteur est limitée à 4096 caractères ; une ligne ou une recherche arrêtée par le budget
  cumulé de 16 MiB / 2 secondes rend le résultat `PARTIAL`.
- Mesure locale : le moteur `re.finditer` sur `a*~` et une ligne de 80 000 caractères a pris 2,225 s avant
  validation ; `_search_sources` sur une ligne de 4 MiB avec le motif borné `https?://` a pris 0,025 s,
  trouvé le préfixe et signalé `PARTIAL` ainsi que `text_truncated`.
- Le worker appelle `prctl(PR_SET_DUMPABLE, 0)` lors de l'import du module Dramatiq, avant le démarrage de ses
  processus de travail. Les sous-processus Linux appliquent aussi ce réglage dans leur fonction pré-exec.
- La clé de réutilisation des opérations `apk_jadx` inclut les budgets effectifs de timeout et de sortie ; les
  clés des autres outils gardent leur forme historique. L'observation APK est versionnée en v2 et enregistre
  les deux budgets avec le résultat.

## Limites résiduelles

Le test Linux vérifie que, après l'import du module Dramatiq, un enfant de même UID ne peut ouvrir ni
`/proc/<pid-parent>/environ` ni `/proc/<pid-parent>/mem`, et que `/proc/self/environ` reste lisible par le
processus enfant. Le Dockerfile lance Dramatiq directement et Compose remplace sa commande par un autre appel
direct à Dramatiq ; ce chemin importe `analysis_tasks` avant que le maître ne crée ses workers. Je n'ai pas
ajouté de wrapper PID 1 : un wrapper qui fait `exec` ne garantit pas la conservation du réglage, tandis qu'un
wrapper qui reste PID 1 devrait gérer le transfert des signaux et la récolte des enfants. La protection du
maître dépend donc du maintien de l'import `analysis_tasks` avant le démarrage des processus Dramatiq.

`PR_SET_DUMPABLE` bloque l'accès procfs selon les contrôles Linux entre UID identiques ; il ne protège pas
contre une compromission du processus worker lui-même, le superutilisateur ou l'hôte. L'environnement filtré
du sous-processus reste également limité à la liste explicitement autorisée par `analysis_subprocess.py`.
Un essai a observé que `PR_SET_DUMPABLE` appliqué au pré-exec peut être réinitialisé par l'exécutable après
`exec` (`PR_GET_DUMPABLE` valait alors 1 dans un enfant Python) ; le contrôle mesuré ici est le worker parent
non dumpable, qui bloque l'accès de l'enfant à ses propres fichiers procfs.
