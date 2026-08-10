# Infrastructure locale

Le fichier Compose reste à la racine afin que `docker compose up` fonctionne sans option. Les Dockerfiles sont conservés ici. Tous les contrôles sont passifs et limités aux services du réseau Compose.

Les volumes `postgres_data`, `redis_data` et `minio_data` sont nommés. `make stop` ne les supprime pas ; leur suppression doit être une décision explicite.

