# S3 — implémentation et gates des effets externes

Statut : **CONCEPTION VALIDÉE, IMPLÉMENTATION NON TERMINÉE.** Ce document ne
déclare aucun `S3_*_READY`. La conception D6 a été confrontée au code réel :
[`recon/S3_SEAM.md`](recon/S3_SEAM.md) (SHA-256
`9f17611abf3a05166f069b745cfd546041e6748ccce1f4f36b6135f3774dbaa4`). La
spécification d'implémentation détaillée est
[`recon/S3_IMPLEMENTATION_SPEC.md`](recon/S3_IMPLEMENTATION_SPEC.md) (SHA-256
`ee82367b419a85ba1c6b90dd79a014907dc856647e4e016fffd22412fbe3a397`). Le code
actuel n'a **pas** de transaction de consume partagée, et les adaptateurs/
transports restent appelables sans permis.

## 1. Seam retenu

Un module applicatif profond, `ExternalEffectCoordinator.send_one`, est placé
immédiatement avant chaque requête physique sortante :

1. l'adaptateur prépare une requête immuable et versionnée ;
2. le coordinateur valide l'autorité et les digests ;
3. une transaction PostgreSQL verrouille l'enveloppe, l'invocation, la
   réservation, le claim et l'éventuelle tentative de famille, puis vérifie la
   politique, la révocation, les restrictions de source et les budgets ;
4. le commit de consume est le point d'autorisation ;
5. après commit seulement, un permis opaque à usage unique est émis ;
6. le transport permit-aware recompte le digest juste avant l'I/O ;
7. le résultat est journalisé ; une ambiguïté scelle le claim et l'invocation
   en `NEEDS_REVIEW` sans renvoi automatique.

Un appel direct à un adaptateur ou à un transport sans permis est un refus
typé, zéro émission. Chaque page, fallback, redirection, retry ou correction
consomme un permis distinct.

### Couplage avec la politique OSINT_OPEN

Pour une enveloppe `authority_kind=OSINT_POLICY`, la transaction de consume
vérifie, sous verrou, que :

- l'investigation porte toujours le même `subject_id` que le pin d'héritage ;
- l'événement/version de politique épinglé existe et n'est pas révoqué ;
- l'enveloppe, l'invocation et la réservation sont toujours dans l'état attendu.

Ordre de verrouillage : politique épinglée → enveloppe → invocation →
réservation → claim → tentative de famille → `ModelConversationTurn`/`ModelRun`.
La révocation de politique verrouille la politique puis les enveloppes
héritées. Un claim commité avant la révocation autorise un seul envoi ; un
claim qui observe la révocation est refusé avec zéro émission. Une
investigation annulée/reprise ne regagne aucun droit après révocation.

## 2. Modèle durable

La migration additive `0004_external_effect_claims` (après
`0003_osint_open_policy`) ajoute :

- `external_effect_claim` : identité d'effet, versions normalisateur/identité,
  digests de requête sémantique et d'application, occurrence, invocation/
  tentative source, réservation, version d'enveloppe/autorité, état, CAS,
  horodatages ;
- `external_effect_use` : lignée d'une réutilisation vers l'invocation
  consommatrice, références d'observation source, budget delta zéro.

Aucune réécriture de `0001_baseline` ou `0002_c2_experiments`. Aucun backfill.
Les identités d'effet ne contiennent jamais de credentials, tokens, mots de
passe, signed URLs ou en-têtes secrets.

## 3. Familles

| Famille | État | Fallback silencieux interdit |
|---|---|---|
| VT read (file report, relationships, search, behaviours) | NON MIGRÉE | Oui |
| VT download / acquisition | NON MIGRÉE | Oui ; la réutilisation locale vérifiée reste un cas sans émission |
| `advisor.consult` / Bridge turns | NON MIGRÉE | Oui ; chaque retry/correction est un envoi physique distinct |
| Shodan | NON MIGRÉE, hors périmètre prioritaire | Identifiée comme telle |
| Retrohunt | NON MIGRÉE, hors périmètre prioritaire | Identifiée comme telle |

Une famille non migrée reste explicitement non couverte. Elle ne doit pas être
présentée comme protégée par S3. Une famille migrée ne conserve aucun chemin
non protégé : en cas d'échec du coordinateur, elle refuse l'émission.

## 4. Gates obligatoires

Les oracles détaillés sont dans
[`TEST_LEDGER_AND_HANDOFF.md`](TEST_LEDGER_AND_HANDOFF.md) §2.2–2.3. Les
gates non négociables :

- PostgreSQL jetable réel et deux sessions indépendantes pour les courses ;
- révocation avant consume → refus et zéro send ;
- deux workers concurrents → un seul claim et un seul send ;
- rejeu de la même opération → réutilisation, zéro second send ;
- occurrence explicite → nouveau claim autorisé ;
- crash avant consume → aucun claim ; crash après consume / réponse ambiguë →
  `NEEDS_REVIEW`, jamais de resend ;
- mutation des octets préparés → refus du transport ;
- appel direct sans permis → refus typé ;
- retries/redirections/pagination → chaque envoi physique contrôlé ;
- budgets et preuves conservés ; réutilisation à budget delta zéro ;
- migration sur base jetable et downgrade gardé.

## 5. Non-claims

- Aucun test ne contacte un fournisseur réel ; les compteurs de sends prouvent
  l'absence d'appel AutoWork, pas la réception côté fournisseur.
- Aucune idempotence côté fournisseur n'est supposée.
- Les retries cachés des SDK/proxys restent `UNKNOWN` jusqu'à inspection de
  chaque transport.
- `S3_VT_READ_READY`, `S3_VT_DOWNLOAD_READY` et `S3_BRIDGE_READY` restent
  `BLOCKED` tant que leurs tranches verticales et leurs gates ne sont pas
  exécutés.
- `ADVISOR_CUTOVER_BLOCKED_BY_S3` reste en vigueur ; aucun cutover v3→v4.
