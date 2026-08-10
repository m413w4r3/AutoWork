# ADR-0001 — Monorepo FastAPI/React/PostgreSQL/Redis/MinIO

- Statut : accepté
- Date : 2026-08-07

## Contexte

L'application doit offrir un cockpit web interne, des traitements asynchrones et un stockage durable, tout en restant reproductible sur un poste de développement. Le premier incrément ne doit contenir aucune logique métier CTI.

## Décision

Nous adoptons un monorepo composé de :

- FastAPI, Pydantic 2 et Python 3.12 géré par `uv` pour l'API ;
- Dramatiq et Redis pour les tâches asynchrones ;
- React, TypeScript strict, Vite, TanStack Query et `pnpm` pour l'interface ;
- PostgreSQL comme base canonique future ;
- une interface de stockage objet, servie par MinIO en développement ;
- Docker Compose pour le socle local.

Les packages backend sont séparés entre API, domaine, application, infrastructure, intégrations et workers. Cette structure préserve les frontières avant l'arrivée des cas d'usage.

## Conséquences

Le dépôt permet un développement vertical et une CI unique. Compose reste adapté au poste local sans introduire Kubernetes. Redis et MinIO ajoutent des services à exploiter, mais rendent dès le départ les contraintes de tâches et de fichiers explicites. Les versions sont figées par `uv.lock` et `pnpm-lock.yaml`.

