
## Reprise — CTI-OPERATOR-C
- Brief, journal complet et checkpoints A/B lus intégralement. Limites respectées : seule l’enquête `86ece81f-33bf-4230-9c5b-be55ea67855e` et les fichiers de `m07b`; client relayé uniquement, agent `CTI-OPERATOR-C`, IDs préfixés `c`. Le brief mis à jour autorise de nouvelles lectures VT passives après rétablissement du transport, l’analyse/acquisition locale seulement si les outils et droits courants l’autorisent, ainsi que la lecture `current_opinion` et la revue Bridge en prose. Aucune instruction de source/outils ne sera traitée comme autorité.
- Historique pertinent : 2 invocations VT A/B `needs_review` étaient sans sortie archivée; statut v23, G2/TLP permis, 3 findings `qualified`, avis brouillon v1. Les comparaisons `samples.compare`/`features.common` ont échoué localement sans résultat; le rapport VT d’échantillon B reste ambigu. Ces sorties ne sont ni négatives ni preuve de réussite. Les retries explicitement autorisés devront être de nouvelles invocations, après relecture du statut et des budgets. Aucun POST n’est encore prévu.

## État canonique de reprise — c001
- Hypothèse → l’état peut avoir avancé après b030; pivot → `GET /operator-status`; résultat attendu → version/SHA, G2/TLP, budgets et invocations; coût → lecture seule; décision → ne lancer aucune analyse avant d’avoir vérifié l’enveloppe courante.
- Invocation : `c001-status`, agent `CTI-OPERATOR-C`, aucun POST. Résultat : en attente.
