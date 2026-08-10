# ADR-0003 — Passerelle de modèles typée et runs persistés

- Statut : accepté
- Date : 2026-08-08

## Contexte

Les modèles servent plusieurs rôles avec des politiques de diffusion différentes. Le transport
OpenAI disponible en développement passe par une interface ChatGPT locale qui n'offre pas
nativement toutes les garanties de Responses API. Qwen peut être local ou exposé par un gateway
distant.

## Décision

Les rôles métier dépendent de quatre ports applicatifs. Un routeur choisit un adaptateur après
sanitation et avant tout accès réseau. Toute exécution crée un `ModelRun` PostgreSQL sans prompt
en clair ; les sorties complètes sont des blobs adressés par contenu.

Les concepts Responses restent confinés à l'adaptateur OpenAI et le protocole Chat Completions
à l'adaptateur Qwen. `ChatGPTBridgeTransport` traduit les premiers vers le contrat natif
`/v1/bridge/*` ; la façade Responses du bridge reste explicitement non native. Les réponses
structurées sont toujours revalidées localement.

## Conséquences

Le changement de fournisseur ou le remplacement du bridge n'affecte pas le domaine. Les appels
sont simulables sans réseau et les reprises sont compatibles avec les jobs existants. En
contrepartie, le bridge ne peut fournir ni snapshot réel, ni sources structurées, ni durabilité
de fond équivalente au service officiel ; ces limites sont conservées comme telles.
