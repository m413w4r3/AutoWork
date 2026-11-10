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

Avant le premier build, relever la version exacte du paquet Debian
`openjdk-17-jre-headless` dans le dépôt Bookworm utilisé, puis épingler cette
version dans `infra/analysis-worker.Dockerfile`. Le Dockerfile installe
actuellement ce paquet sans numéro de version ; le tag majeur 17 et le test
`java -version` ne garantissent pas à eux seuls une construction reproductible.
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

Le contrat applicatif, les limites du worker, l’épinglage SHA de jadx, la
recette de fixture et les contrôles présents dans le test d’image ont été lus.
Dans ce lot, l’image `analysis-worker` n’a pas été construite et le test
`ANALYSIS_WORKER_IMAGE_TEST=1` n’a pas été lancé. Aucun APK malveillant réel
n’a été exécuté. La vérification du schéma/noms des outils opérateur et de la
persistance de leurs observations canoniques attend l’intégration E1b ; le
code E1b n’est pas présent dans le checkout de base de ce lot.
