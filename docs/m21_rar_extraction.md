# Décompression des membres RAR dans `analysis-worker`

## Cause établie et limite du diagnostic

Le chemin confiné utilise `/usr/bin/7z` pour les trois commandes `archive_list`,
`archive_extract_hash` et `archive.extract_member`. Il lance d'abord `7z l -slt`
pour lire les en-têtes, puis `7z x -so` pour décompresser un membre sélectionné.
L'image installe `p7zip-full` et vérifie que `7z i` annonce la version 16.02 ;
elle n'installe pas le paquet distinct `p7zip-rar`, qui fournit le module RAR
non libre dans Debian, et elle ne vérifie pas la présence d'un décodeur RAR.
Debian classe explicitement ce module séparé en `non-free`.

La liste des membres ne prouve donc pas que le worker sait décompresser leur
flux. La cause dans la configuration suivie est l'absence du module RAR de
p7zip et de toute autre bibliothèque d'extraction RAR. C'est la cause racine
la plus probable des échecs concordants sur les membres compressés. Sans
lancer l'image, la sortie exacte de `7z` reste inconnue ; un flux membre
corrompu ou une méthode RAR non prise en charge produirait aussi un échec
d'extraction. Le nouveau diagnostic différencie désormais un message connu de
codec absent d'une erreur de décompression générique.

Avant ce changement, les erreurs stderr de `7z x` qui ne correspondaient pas à
CRC, mot de passe ou corruption devenaient `extract_failed`. L'adaptateur
`archive.extract_member` ne conservait que ce statut et ne transmettait pas le
diagnostic du worker. Le contrôle de version de l'image attestait uniquement
la version générale de 7z, pas la disponibilité du codec RAR.

## Diagnostic ajouté

Pour chaque échec de décompression, le rapport conserve un `detail_code`, la
méthode de compression et un extrait stderr sur une seule ligne, limité à
512 caractères. Les chemins absolus, le chemin temporaire de l'archive et le
nom du membre demandé sont remplacés avant d'être exposés. `archive.extract_member`
transmet ces champs à la réponse de l'outil et rend le code détaillé disponible
comme `error.detail_code`. Le contrôle de taille, le ratio maximal, le timeout,
la mémoire limitée et l'écriture des octets extraits uniquement vers le flux
standard restent inchangés.

Les codes pertinents sont `archive_codec_unsupported` lorsqu'un message connu
signale une méthode ou un codec indisponible, et
`archive_member_decompress_failed` lorsque stderr ne permet pas une
classification plus précise.

## Décodeur RAR et compatibilité attendue

Évaluer `bsdtar` fourni par `libarchive-tools` (libarchive sous licence BSD) dans
l'image `analysis-worker`. La documentation de libarchive annonce la lecture
des archives RAR, sans détailler chaque méthode RAR ; la compatibilité doit
donc être prouvée avec des archives bénignes contenant un membre stocké et des
membres compressés RAR 2.9/RAR4 utilisant les méthodes réellement observées
(`m3:20` et `m3:21`). Ne pas considérer la seule réussite d'un listage comme
preuve de décompression.

`libarchive-tools` est maintenant installé dans l'image à la version Debian
épinglée `3.6.2-1+deb12u5`. Le test d'image atteste le paquet, la présence de
`bsdtar` et une bannière `bsdtar --version` qui mentionne libarchive. La
décompression réelle d'une archive RAR reste à démontrer avec les échantillons
existants.

Le code amont libarchive v3.6.2 confirme la lecture des SFX RAR4 PE, mais son
lecteur cherche le marqueur dans une zone maximale de 128 Kio. Le worker
repère les signatures RAR4 et RAR5 dans le premier Mio du payload et, si un
préfixe les précède, crée sous `/tmp` une copie qui commence au marqueur avant
de lancer `bsdtar`. Le stub n'est jamais exécuté. Cette copie reste dans le
répertoire temporaire qui contient déjà l'archive et est supprimée à la fin de
l'opération.

Le recours à `bsdtar` est limité aux extractions dont le type indiqué par 7z
commence par `Rar` et dont l'échec 7z signale une méthode ou un codec non pris
en charge. Le membre ciblé est fourni comme argument après `--` à
`bsdtar -xOf`; le nom est validé et les motifs d'archive ambigus pour bsdtar
sont refusés. La sortie reste bornée par la taille déclarée du membre. Les
limites de taille membre/totale et de ratio, le délai maximal, RLIMIT_AS,
l'environnement réduit et le filtre seccomp réseau sont appliqués. Aucun
membre n'est extrait sur le système de fichiers.

Références du comportement SFX et du paquet épinglé : [lecteur RAR4 libarchive
v3.6.2](https://github.com/libarchive/libarchive/blob/v3.6.2/libarchive/archive_read_support_format_rar.c#L749-L829),
[paquet Debian Bookworm `libarchive-tools`](https://packages.debian.org/bookworm/amd64/libarchive-tools).

Après modification du Dockerfile, reconstruire et vérifier l'image avec :

```sh
docker compose build analysis-worker
ANALYSIS_WORKER_IMAGE_TEST=1 make test-backend \
  PYTEST_ARGS="tests/test_confined_analysis_image.py -q --tb=short"
```

Le test d'image existant lance un processus de vérification ponctuel, pas la
stack de services. L'image doit être reconstruite et ce test doit passer avant
de revendiquer le support RAR. `p7zip-rar`/`unrar` ne sont pas recommandés ici :
ils impliquent le module unRAR et ses restrictions de licence. `unrar-free`
ne doit pas être choisi sans démontrer qu'il décompresse ces méthodes précises.

Références : [libarchive](https://www.libarchive.org/) documente la lecture RAR
et sa licence BSD ; [le paquet Debian `p7zip-rar`](https://packages.debian.org/bookworm/p7zip-rar)
est un module distinct classé `non-free`.

## État de ce correctif

Le fallback bsdtar est implémenté pour `archive_extract_hash` et
`archive.extract_member`, avec les garde-fous décrits ci-dessus. Des tests
unitaires mockent le sous-processus pour vérifier le succès 7z sans fallback,
le succès ou l'échec du fallback, le membre absent, l'option injection et la
limite stdout. Aucun RAR factice n'est créé : l'environnement ne contient pas
d'outil `rar` pour produire des fixtures.

L'orchestrateur exécutera la vérification de l'image après :

```sh
docker compose build analysis-worker
ANALYSIS_WORKER_IMAGE_TEST=1 make test-backend \
  PYTEST_ARGS="tests/test_confined_analysis_image.py -q --tb=short"
```

Cette attestation ne prouve pas encore la décompression des méthodes RAR4
observées `m3:20` et `m3:21`. Il reste à la vérifier en live sur les
échantillons existants après le build et le test d'image. Aucun échantillon n'a
été ouvert et aucune décompression réelle n'a été lancée pendant ce correctif.
