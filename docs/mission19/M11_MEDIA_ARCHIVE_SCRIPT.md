# M11 — archivage opérateur des images de sources

Le script lit, pour chaque sujet, le dernier artefact `EXTRACTION` vérifié et
les documents sources déjà archivés. Le mode par défaut est un aperçu hors
réseau : il compte les pages HTML/PDF, les observations d’images et les
enregistrements présents. Il n’appelle pas l’archiver.

L’image du worker ne copie pas `scripts/ops` en entier. Le Dockerfile inclut
uniquement ce script par une instruction `COPY` ciblée. Après reconstruction
du worker, l’aperçu se lance ainsi :

```sh
docker compose build worker
docker compose up -d worker
docker exec cti-bulletin-worker-1 python /app/scripts/ops/m19_archive_source_media.py <SUBJECT_UUID>
```

Pour autoriser les téléchargements d’images référencées par les pages déjà
archivées, les deux drapeaux explicites sont obligatoires :

```sh
docker exec cti-bulletin-worker-1 python /app/scripts/ops/m19_archive_source_media.py \
  --execute --i-confirm-user-authorized-image-fetch \
  --max-images-per-subject 200 --delay-seconds 5 <SUBJECT_UUID> [SUBJECT_UUID ...]
```

Les sujets sont traités séquentiellement. Le script affiche des comptes et des
statuts, sans URL ni clé. Les blocages d’accès ou SSRF arrêtent le traitement.
Les valeurs de configuration proviennent de l’environnement du conteneur; le
script ne lit pas `.env`.
