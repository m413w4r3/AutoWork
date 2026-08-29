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
- la production courante est unifiée en articles ;
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

## Discipline M3

La pré-localisation et les contrats M3 sont déjà faits. Pour chaque lot M3, lis
uniquement les chemins explicitement listés par son prompt. Pas de ctx.py, rg,
find, tree, recherche web ni exploration de confort. Si une abstraction requise
n'existe pas dans ces fichiers, STOP avec le chemin/symbole manquant ; ne cherche
pas un substitut ailleurs.

Les migrations 0001 à 0010 sont immuables. Le lot 09 crée uniquement la migration
nommée par son prompt. Le lot 10 ne réécrit jamais cette migration et ne crée une
migration supplémentaire que si son prompt l'autorise explicitement.

M3 consomme les sorties persistées de M2. Le lot 09 ne relance ni analyse statique,
ni capa, ni SMDA, ni VirusTotal, ni modèle. Il ne relit pas les octets d'un sample
pour recalculer une feature déjà disponible en base.

Les taxonomies, provenances, règles de rejet et tests de P09 sont définis dans
`docs/design/09-invariant-registry.md`. Les contrats conversationnels et de sortie
P10 sont définis dans `docs/design/10-proposal-conversation.md`. Ne crée pas une
taxonomie parallèle et ne remplace pas une valeur inconnue/non mesurable par zéro.

Les seuils M3 sont des entrées de configuration/contrat humain. Aucun agent ne
choisit silencieusement une valeur pour la banalité, la longueur maximale de motif,
le ratio maximal d'octets masqués, la longueur fixe minimale ou `likely_packed`.
Si un seuil requis n'est pas fourni, STOP au lieu d'inventer un défaut.

Toute idempotence M3 est protégée par PostgreSQL ou par une primitive persistante
canonique existante. Les journaux de rejets et décisions restent inspectables et
append-only selon leur contrat. Une sortie modèle n'est jamais une approbation,
une preuve primaire ni une autorité analyste.

P10 réutilise `ModelConversationService`/`ModelGateway`. Ne contacte jamais un
provider ou le bridge directement. Recalcule `derived_policy` sur les Samples
réellement inclus à chaque tour. Une politique qui interdit l'externe doit être
bloquée avant l'appel externe ou utiliser le routing local déjà autorisé ; elle
n'est jamais affaiblie.

Ne garde jamais une transaction PostgreSQL ouverte pendant un appel modèle ou
réseau. Aucun octet de sample, secret, API key ou signed URL dans prompt, log,
erreur, provenance ou paramètre de job.

P10 propose seulement : aucune requête VT, acquisition, compilation YARA,
validation/approbation d'invariant, promotion de corpus ou consommation de budget
`PIVOT_RUNS`. Les propositions repassent par les rejets déterministes P09.

Pas de frontend, endpoint public, worker, queue ou wiring anticipé si le prompt du
lot ne le demande pas. Pas de refactor transverse de confort.

Exécute uniquement les tests/checks listés dans le prompt, avec pytest -q
--tb=short. Pas de suite globale ni make test. Rapport final : maximum 25 lignes,
cinq rubriques : Fichiers modifiés ; Décisions ; Tests ; Écarts ; Risques. Pas de
commit, push ou PR.
