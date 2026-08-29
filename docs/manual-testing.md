# Tests manuels locaux

## Démarrer

```bash
docker compose up -d --build
```

Les workspaces explorables directement depuis l’hôte sont :

```text
./var/workspaces/subjects
./var/workspaces/editions
```

## Arrêter

Sans effacer les données :

```bash
docker compose stop
```

ou :

```bash
docker compose down
```

**NE PAS utiliser `docker compose down -v` pour arrêter un environnement contenant des données de test utiles.**
