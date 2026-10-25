# Replay transverse hors ligne — plan d'exécution

**Contraintes :** PostgreSQL jetable `localhost:55432`, transports/fakes
uniquement, Typst/D2/polices locaux, aucun fournisseur/Bridge/ModelGateway/VT/
Shodan/Retrohunt/acquisition, aucune pile live, aucun MinIO live.

## Séquence minimale

1. **P10 refused** — une consultation dont la réponse est un refus terminal
   produit zéro avis/candidat, un statut auditable, et aucun traitement P09.
2. **P10 partial** — unités explicitement incomplètes : statut partial, données
   non résolues marquées manquantes, aucun fait validé.
3. **P10 succeeded** — wire compact valide : P09 traite le candidat une fois,
   provenance/version/SHA exacts et rejouables sans second envoi.
4. **Advisor prepare** — dossier avec octets/contenu CTI natif, hex/Base64,
   métadonnées, hashes et associations ; transport fake, payload intact.
5. **Advisor parse/reparse v4** — refused/partial/invalid/ambiguous, alias,
   `source_ref` opinion versionné exact, bytes/hash bruts ; reparse lit
   uniquement les artefacts persistés, zéro nouvelle émission.
6. **Contexte full/paged** — même ordre et même `context_sha256` pour full et
   pages 1/N ; curseur stale → 409 ; offset legacy non prouvé → 422.
7. **Audit export** — décisions, invocations, observations, refs et hash
   présents ; aucune promotion d'une sortie modèle en preuve primaire.
8. **PDF avec/sans avis** — PDF de base V5 et augmenté V6 réellement compilés ;
   marqueurs canoniques, absence d'identités reviewer/approver dans le texte
   visible, lineage acteurs référencé.

## Preuves à consigner

- commande exacte, nombre de tests, statuts et refs observés ;
- tout chemin non atteint → `OFFLINE_REPLAY_PARTIAL` ou
  `BLOCKED_OFFLINE_E2E` avec l'étape et la cause précises ;
- aucun test live, aucun `skip` masquant une étape non exécutée.
