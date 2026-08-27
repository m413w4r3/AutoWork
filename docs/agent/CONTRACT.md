# Analyst investigation contract

- An investigation is unique for a production run and is anchored to that run's verified SYNTHESIS artifact for the same subject.
- Loop budget consumption is checked before mutation; a cap is never best-effort.
- A completed cycle with no validated new member exhausts the investigation. `failed` denotes technical failure only.
- Analyst decisions are investigation-scoped, append-only records.

## Discipline M2

La localisation est déjà faite par le prompt du lot.
Lis uniquement les fichiers explicitement listés dans `Lire`.
Ne lance pas ctx.py, rg, find, tree ni une exploration de répertoire.
Si un chemin listé n'existe pas ou si une abstraction directement requise est
introuvable, arrête et rapporte le nom précis du blocage au lieu d'explorer.

Ne modifie jamais les migrations 0001 à 0004.
Chaque lot M2 crée exactement la migration nommée dans son prompt et les lots
suivants ne la réécrivent pas.

Préserve les invariants validés de P04 :
- brief_auto reste inchangé ;
- major_assisted reste derrière son feature flag ;
- la synthèse et l'input pack restent immuables ;
- aucun modèle n'est appelé dans le handoff analyste ;
- aucune opération VT n'est placée dans la transaction canonique du handoff.

Ne garde jamais une transaction PostgreSQL ouverte pendant un appel réseau ou
un sous-processus d'analyse.

Toute idempotence annoncée doit être protégée par une contrainte/clé canonique
ou une primitive atomique en base, pas seulement par un pre-check Python.

Toute nouvelle table portant un blob_id doit être ajoutée au comptage de
références de BlobRepository afin que delete_unreferenced reste correct.

Aucun test ne contacte Internet, VirusTotal, OpenAI, Qwen, capa update ou SMDA
update. Aucun secret, API key, signed URL ou contenu binaire n'apparaît dans les
logs, erreurs, provenance ou paramètres de job.

Pas de frontend, pas de chatgpt-bridge, pas de refactor transverse de confort.
Ne crée pas d'endpoint ou de wiring anticipé pour un lot ultérieur.

Exécute uniquement les tests et checks explicitement listés dans le prompt.
Utilise pytest -q --tb=short. Pas de make test.

Rapport final : maximum 25 lignes, cinq rubriques seulement :
Fichiers modifiés ; Décisions ; Tests ; Écarts ; Risques.
Pas de citation du code, pas de diff recopié. Pas de commit, push ou PR.
