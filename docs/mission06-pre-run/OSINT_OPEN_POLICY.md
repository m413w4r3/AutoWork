# Politique permanente `OSINT_OPEN` — conception et invariants

Statut : **BROUILLON DE CONCEPTION** (à confronter à la reconnaissance puis à
l'implémentation et aux tests). Aucune capacité n'est déclarée PASS par ce
document.

## 1. Objectif

Après une activation humaine explicite, une investigation OSINT ordinaire
enchaîne ses recherches, acquisitions et analyses locales sans approbation
répétée. L'autorisation est durable, versionnée, auditée, révocable et liée à
l'identité qui l'a activée. Elle ne fabrique jamais une fausse décision
`HUMAN` par investigation.

Le mode restrictif actuel reste le défaut. `OSINT_OPEN` est une seconde
politique, pas un contournement silencieux.

## 2. Modèle

### 2.1 Portée

- `INSTANCE` : toutes les investigations de l'instance.
- `SUBJECT_SET` : uniquement les investigations dont le `subject_id` figure
  dans une liste autorisée.

Une politique est identifiée par `(scope_kind, scope_ref)`. Les versions sont
append-only ; la version active est la dernière version non révoquée. Une
révocation crée une nouvelle version `REVOKED` avec droits externes à zéro.

### 2.2 Contenu autorisé

Une version contient uniquement des faits non secrets :

- `allowed_providers` (dont `vt` pour l'acquisition) ;
- `budget_limits` : `VT_UNITS`, `DOWNLOADS`, `BRIDGE_TURNS`, `TOOL_SECONDS`,
  `CONTEXT_BYTES`, `CONTEXT_TOKENS`, éventuellement `SHODAN_CREDITS` ;
- `acquire_hits_allowed` ;
- `disclosure` : TLP par défaut des nouvelles enveloppes, `do_not_submit`,
  `external_llm_allowed` ;
- `max_effect_claims_per_investigation` (coupe-circuit optionnel).

Aucun credential, token, mot de passe, signed URL ou en-tête secret n'entre
dans la politique, ses événements ou ses prompts.

### 2.3 Activation, extension, révocation

- `activate(actor_id, reason, correlation_id, …)` exige un acteur authentifié
  non vide, une raison et une corrélation. Le rejeu de la même corrélation et
  du même contenu renvoie la version déjà créée. Une corrélation réutilisée
  avec un contenu différent est refusée.
- `extend(expected_version, …)` suit la règle « croissance uniquement » :
  providers, budgets et droits ne diminuent pas ; une extension ne peut pas
  élargir TLP au-delà du défaut autorisé ni réactiver un droit révoqué.
- `revoke(expected_version, actor_id, reason, correlation_id)` écrit une
  nouvelle version `REVOKED` et propage la révocation aux enveloppes encore
  héritées (voir 2.5).

Chaque événement est append-only : type, acteur, raison, corrélation, hash du
payload, version précédente, horodatage. Le payload secret n'est jamais
journalisé.

### 2.4 Héritage vers une investigation

`ensure_inherited_envelope(investigation_id)` :

1. verrouille la politique active de la portée et vérifie qu'elle n'est pas
   révoquée ;
2. vérifie l'appartenance du `subject_id` à la portée ;
3. si une enveloppe existe déjà, la retourne sans la modifier ;
4. sinon, crée une décision d'audit `PIVOT_APPROVE` ciblant l'investigation.
   `actor_id` est **l'acteur humain qui a activé la politique**, et
   `details` contient `osint_policy_id`, `osint_policy_version`,
   `inherited: true`, `activation_correlation_id`. La raison cite la politique
   et sa version. Ce n'est pas une approbation individuelle déguisée : c'est
   une décision dérivée, tracée jusqu'à l'activation ;
5. crée l'enveloppe version 1 avec les providers, budgets, droits d'acquisition
   et bornes de divulgation de la politique ;
6. enregistre la lignée `osint_open_inheritances` (investigation, politique,
   version, décision d'héritage, statut `ACTIVE`).

La corrélation d'héritage est déterministe
(`osint-inherit:<policy_id>:<policy_version>:<investigation_id>`), ce qui rend
le rejeu idempotent. Deux créations concurrentes ne produisent qu'une enveloppe.

L'héritage est déclenché au démarrage de l'investigation et, en défense en
profondeur, à la première invocation opérateur si aucune enveloppe n'existe.

### 2.5 Révocation et courses

La révocation verrouille la politique, puis les lignées actives :

- si l'enveloppe porte encore la décision d'héritage, elle est révoquée selon le
  mécanisme G2 existant (nouvelle décision `PIVOT_REJECT`, nouvelle version
  d'enveloppe à droits externes nuls, TLP préservé) et la lignée passe
  `REVOKED` ;
- si un humain a depuis étendu ou réapprouvé l'enveloppe, l'approbation
  manuelle explicite l'emporte : la lignée passe `SUPERSEDED`, l'enveloppe
  n'est pas réécrite. La politique ne fournit plus de nouvelle autorisation.

Une nouvelle héritage après révocation est refusée. Le verrou de politique
sérialise « révocation » et « héritage » : si la révocation commite d'abord,
l'héritage est refusé ; si l'héritage commite d'abord, il est ensuite révoqué
par la propagation.

### 2.6 TLP

- Les nouvelles enveloppes héritées utilisent `TLP:CLEAR` par défaut lorsque
  les sources sont librement partageables.
- Les lignes de source existantes ne sont jamais reclassifiées. Une source
  GREEN/AMBER/RED, `do_not_submit=true` ou `external_llm_allowed=false`
  conserve sa restriction et continue de bloquer au dispatch ou à la
  consultation.
- `ensure_tlp_not_downgraded` reste en vigueur. Aucune baisse de TLP n'est
  déduite de l'activation.

### 2.7 G2 et G3

- G2 : l'enveloppe héritée remplace l'approbation répétée. Les budgets sont
  généreux, configurables et finis. Toute consommation reste comptabilisée.
- Acquisition : `acquire_hits_allowed=true` et budgets `DOWNLOADS`/`VT_UNITS`
  positifs autorisent `vt.download_sample` sans G3 par fichier.
- Analyse locale : `investigation.local_read` reste une capability locale ;
  aucune validation G3 n'est exigée par action analytique.
- G3 : l'agent peut proposer `candidate`, `related`, `uncertain` ; seuls
  `confirmed` et `rejected` restent réservés à un humain identifié. Aucune
  hypothèse de famille ne devient automatiquement une appartenance confirmée.

## 3. Interface courte

```text
OsintPolicyService
  activate(...) -> OsintPolicyVersion
  extend(...) -> OsintPolicyVersion
  revoke(...) -> OsintPolicyVersion
  current(scope) -> OsintPolicyVersion | None
  ensure_inherited_envelope(investigation_id) -> InvestigationEnvelope
  effective_for(investigation_id) -> OsintPolicyVersion | None
```

Types purs : `OsintPolicyMode`, `OsintScopeKind`, `OsintPolicyStatus`,
`OsintPolicyVersion`, `OsintCapabilities`, `OsintInheritanceStatus`.

L'évaluation de portée et la construction d'enveloppe sont pures ; la
persistance, les verrous et la propagation sont dans l'adaptateur PostgreSQL.

## 4. Persistance (migration additive `0003`)

- `osint_open_policies` : une ligne par version, clé
  `(id)`, contraintes `(scope_kind, scope_ref, version)` uniques, statut,
  payload non secret, acteur/raison/corrélation d'activation, horodatages.
- `osint_open_policy_events` : journal append-only des activations, extensions
  et révocations, avec hash de payload et version précédente.
- `osint_open_inheritances` : `investigation_id` PK/FK restrictive, politique,
  version, décision d'héritage, statut, horodatages.

Aucune modification de `0001` ou `0002`. Aucun backfill. Les enveloppes
historiques restent inchangées.

## 5. Protections conservées

- credentials opérationnels AutoWork, tokens, mots de passe, signed URLs ;
- séparation transport / données envoyées à Bridge ;
- SHA-256, versions, preuves, coûts, identités d'opération ;
- idempotence et réconciliation ;
- confinement des outils ;
- interdiction d'exécution d'échantillon et de contact C2 ;
- distinction observation / hypothèse / conclusion ;
- restrictions explicites de partage.

## 6. Oracles RED/GREEN

1. Activation sans acteur → refus ; activation avec acteur → version auditée.
2. Rejeu de la même corrélation → une seule version ; corrélation différente →
   conflit.
3. Héritage : une enveloppe, une décision dérivée, lignée `ACTIVE` ; rejeu →
   aucune duplication ; deux appels concurrents → une seule enveloppe.
4. Aucune identité `HUMAN` fabriquée : la décision dérivée porte l'acteur réel
   de l'activation et `inherited=true` ; aucun `QualificationActorKind.HUMAN`
   synthétique n'est créé pour un agent.
5. TLP : nouvelle source partageable → CLEAR ; source GREEN/RED existante →
   inchangée ; `do_not_submit`/`external_llm_allowed=false` → refus conservé.
6. G2 : budgets hérités positifs ; consommation comptabilisée ; budget épuisé →
   refus local sans émission.
7. Révocation avant émission → refus et zéro appel ; révocation après
   consommation S3 → l'émission déjà autorisée reste unique, toute suivante est
   refusée.
8. G3 : `candidate`/`related`/`uncertain` par l'agent → accepté ;
   `confirmed`/`rejected` par l'agent → refus ; humain identifié → accepté.
9. Migration : upgrade `0002 → 0003` sur base jetable, contraintes et index
   présents ; downgrade refusé si des lignées/événements existent.
10. Aucun test ne contacte un provider, PostgreSQL live, MinIO live ou un
    secret.

## 7. Non-claims

- L'activation de la politique n'exécute aucun appel fournisseur.
- Le mode ouvert ne prouve pas la connectivité VT/Bridge ; la prévol P3 le
  documente séparément.
- Les garanties S3 (permis, claim, concurrence) sont traitées dans
  `S3_IMPLEMENTATION_AND_GATES.md`, pas déduites de cette politique.
