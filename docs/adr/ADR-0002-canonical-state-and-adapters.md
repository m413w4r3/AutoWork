# ADR-0002 — État canonique en base et traitements externes par adaptateurs

- Statut : accepté
- Date : 2026-08-07

## Contexte

Le pipeline est evidence-first. Les sorties de modèles ou de services tiers sont des observations à tracer, jamais un état métier fiable. Les futures intégrations OpenAI, Qwen, VirusTotal, Shodan et stockage objet devront être simulables, soumises aux politiques TLP et sans capacité d'exécuter des actions arbitraires.

## Décision

PostgreSQL portera l'état canonique transactionnel. Les fichiers versionnés et evidence packs porteront les artefacts immuables et leur provenance. Une conversation LLM, un cache ou une réponse d'API ne sera jamais la source de vérité.

Les systèmes externes seront accessibles uniquement via des interfaces typées définies côté application et des adaptateurs remplaçables côté infrastructure/intégrations. Chaque appel futur devra appliquer provenance, TLP, sensibilité, `external_llm_allowed` et `do_not_submit`. Les opérations de découverte technique et les décisions d'attribution resteront séparées.

## Conséquences

Les tests pourront utiliser des doubles sans réseau ni secret. Les changements de fournisseur resteront localisés. Cette frontière impose davantage de modèles et de journalisation, mais permet l'idempotence, l'audit et la reprise. Aucun adaptateur CTI externe n'est implémenté dans le présent incrément.

