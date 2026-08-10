# Infrastructure locale

Le fichier Compose reste à la racine afin que `docker compose up` fonctionne sans option. Les Dockerfiles sont conservés ici. Tous les contrôles sont passifs et limités aux services du réseau Compose.

Les volumes `postgres_data`, `redis_data`, `minio_data` et `bridge_data` sont nommés. `make down` (et son alias `make stop`) ne les supprime pas ; leur suppression doit être une décision explicite.
