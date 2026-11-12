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

## Option recommandée pour activer RAR 2.9 / RAR4

Évaluer `bsdtar` fourni par `libarchive-tools` (libarchive sous licence BSD) dans
l'image `analysis-worker`. La documentation de libarchive annonce la lecture
des archives RAR, sans détailler chaque méthode RAR ; la compatibilité doit
donc être prouvée avec des archives bénignes contenant un membre stocké et des
membres compressés RAR 2.9/RAR4 utilisant les méthodes réellement observées
(`m3:20` et `m3:21`). Ne pas considérer la seule réussite d'un listage comme
preuve de décompression.

Si l'essai est concluant, épingler la version Debian exacte de
`libarchive-tools` dans le Dockerfile et attester `bsdtar` ainsi que la
décompression des fixtures dans le test d'image. Le chemin d'extraction doit
continuer à écrire le membre ciblé sur stdout, avec les limites et les
timeouts existants ; il ne doit pas extraire des chemins de l'archive sur le
système de fichiers.

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

Ce changement rend les échecs observables et documente une voie de résolution ;
il n'ajoute aucun décodeur RAR à l'image. Les membres compressés resteront donc
indisponibles tant qu'une option n'aura pas été ajoutée au worker et validée
avec les fixtures RAR bénignes dans l'image construite. Aucun conteneur n'a été
lancé pour ce diagnostic.
