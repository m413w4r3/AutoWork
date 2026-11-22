# Atelier d’enquête

Vocabulaire du travail d’enquête partagé entre un humain, Bridge et des opérateurs agentiques : plan, tâches, sessions, passations, contributions et autorité éditoriale. ADR de référence : [ADR-0011](../../adr/ADR-0011-investigation-workshop-coordination.md).

## Acteurs et autorité

**Atelier d’enquête** : espace de travail durable d’une enquête, où plusieurs acteurs se transmettent le même contexte sans perdre plan, tâches, notes, résultats ni décisions.
_À éviter_ : plateforme d’agents générique, session de chat, nouveau pipeline de preuve.

**Humain** : acteur identifié qui fixe les priorités, accepte le plan, assigne les tâches, accepte les résultats, exerce G2/G3 et approuve ou publie. L’humain accepte ; il ne fabrique pas de provenance Bridge.
_À éviter_ : opérateur, modèle, agent autonome.

**Bridge** : conseiller externe de confiance éditoriale qui produit plans et prose publiable ; son influence passe par `advisor.consult` et des observations persistées, jamais par une route d’écriture directe.
_À éviter_ : auteur implicite, agent local, seconde source de preuve.

**Opérateur agentique** : Codex CLI ou Claude Code exécuté dans un profil confiné, qui travaille une tâche sous bail, produit des contributions techniques et consulte Bridge ; il ne décide ni G2/G3, ni approbation, ni publication.
_À éviter_ : humain, Bridge, shell hôte non borné.

**Système** : transitions internes non humaines — expiration de bail, promotion de dépendances, perte de session — tracées comme telles. Le système ne prend aucune décision réservée à l’humain.
_À éviter_ : acteur humain implicite, opérateur.

**Mode d’exécution** (`operator_led` ou `bridge_led`) : mode de la boucle d’enquête, distinct de l’autorité de prose. Toute enquête M24 est créée `operator_led` ; `bridge_only` avec `bridge_led` est refusé.
_À éviter_ : mode déduit d’une route, mode modifiable par requête.

**Autorité de prose** (`prose_authority`) : propriété immuable d’une enquête qui détermine qui peut créer de la prose publiable. `bridge_only` exige une provenance Bridge vérifiée ; `legacy` conserve le comportement antérieur, déprécié. Elle est appliquée par une unique porte serveur, `require_bridge_only_provenance()`, à tous les écrivains et tous les chemins d’écriture.
_À éviter_ : indicateur d’interface, droit accordé par requête, `bridge_authored` déclaratif, contrôle fondé sur la seule session.

**Provenance Bridge** : preuve serveur qu’un texte provient d’une consultation `draft_text`/`review_text` persistée, avec invocation, SHA du texte et cible vérifiés, adossée à une observation exactement `advisor_response_received`. Elle ne se déclare pas, elle se relit.
_À éviter_ : champ `bridge_authored` seul, résumé d’agent, texte recopié par un opérateur, « une preuve quelconque ».

## Planification et tâches

**Plan d’enquête** (`InvestigationPlan`) : agrégat versionné des leads proposés par Bridge, lié à l’invocation et à l’observation de consultation qui l’ont produit.
_À éviter_ : texte de conseil non versionné, tâche, liste libre de pivots.

**Lead de plan** : élément du plan `investigation-plan.v1` décrivant un travail vérifiable (objectif, justification CTI, claims, références de départ, outils, préconditions, intérêt, critères de conclusion et champs associés). Un lead parsé devient au plus une tâche, identifiée par un `task_key` déterministe.
_À éviter_ : tâche déjà assignée, pivot isolé, texte libre non parsé.

**Parseur de plan** : lecture tolérante de la sortie Bridge en texte ou JSON partiel ; `parse_status` vaut `parsed` ou `partially_parsed`, et une sortie `unparsed` reste un avis, jamais une version de plan.
_À éviter_ : exiger un JSON garanti de Bridge, promouvoir un avis non parsé.

**Version de plan** : contenu immuable et hashé d’un plan à une version donnée ; elle peut être acceptée, jamais réécrite.
_À éviter_ : brouillon de plan, plan courant, export `plans/`.

**Tâche d’enquête** (`InvestigationTaskV1`) : unité de travail vérifiable avec question, objectif, priorités, références, capabilities, chemins autorisés, politique d’exécution et résultats attendus.
_À éviter_ : pivot, sous-requête de chat, étape de pipeline.

**Sous-tâche** : tâche enfant d’une tâche, née `proposed`, bornée en profondeur, capabilities, chemins et budget par son parent ; elle n’ajoute aucune autorisation.
_À éviter_ : second plan, tâche indépendante, élargissement de périmètre.

**Statut de tâche** : état métier parmi `proposed`, `ready`, `assigned`, `running`, `awaiting_input`, `awaiting_review`, `blocked`, `completed`, `cancelled`, `failed`.
_À éviter_ : statut de session, statut de vérification, état de processus.

**Statut technique de session** : état du processus agentique (`provisioning`, `ready`, `running`, `awaiting_instruction`, `interrupt_requested`, `stopped`, `lost`, `reconciling`, `closed`, `failed`), distinct du statut métier.
_À éviter_ : statut de tâche, résultat de tâche.

**Statut de vérification du résultat** : `unverified`, `pending_review`, `accepted` ou `rejected`. Seul l’humain accepte ; un opérateur ne fait que soumettre.
_À éviter_ : `completed` implicite, auto-évaluation de l’agent.

**Événement de tâche** (`TaskEvent`) : entrée append-only d’un changement métier, avec acteur, version de statut, fence, clé d’idempotence et décision humaine éventuelle.
_À éviter_ : journal de session, log de CLI, événement de job backend.

**Version de statut** (`status_version`) : compteur CAS qui rend une transition obsolète détectable.
_À éviter_ : numéro de version d’artefact, timestamp.

**Bail de tâche** (`lease`) : droit temporaire d’un opérateur d’écrire sur une tâche, renouvelé par heartbeat conditionnel en `running` comme en `awaiting_input`, et lié à une session. Une tâche assignée à l’humain n’a aucun bail.
_À éviter_ : verrou de base générique, propriété de la tâche, session.

**Enveloppe G2 initiale** : proposition unique et bornée (lectures VT, acquisition d’échantillons, coûts/quotas, usage de modèle externe, limites de divulgation) soumise à l’approbation humaine avant le bootstrap ; une fois approuvée, elle couvre les opérations prévues sans clic par hash.
_À éviter_ : auto-approbation, enveloppe par outil, contournement de G2.

**Jeton de clôture** (`lease_fence`) : compteur monotone incrémenté à chaque acquisition, reprise, révocation ou transition qui libère le bail (expiration, `awaiting_review`, `blocked`, `failed`, `completed`, `cancelled`) ; un jeton dépassé ne peut plus écrire.
_À éviter_ : identifiant de session, token d’authentification.

**Politique d’exécution** (`execution_policy`) : plafonds et préconditions propres à une tâche, toujours appliqués en plus de l’enveloppe G2, jamais à sa place.
_À éviter_ : nouvelle enveloppe, budget autonome, autorisation implicite.

**Dépendance** : lien orienté entre tâches de la même enquête ; seule une tâche `completed` satisfait ses dépendants. Le système promeut `proposed → ready` quand la dernière dépendance est terminée ; une dépendance `cancelled`/`failed` ne la satisfait jamais et met le dépendant `blocked` (`blocked_reason='dependency'`).
_À éviter_ : ordre d’affichage, dépendance inter-enquête, cycle.

## Sessions et passations

**Session d’opérateur agentique** (`AgentSession`) : occurrence technique d’un adaptateur CLI liée à une tâche et à une passation ; son état est technique et ne vaut pas statut métier.
_À éviter_ : tâche, conversation Bridge, process sans identité.

**Secret de session agentique** : secret tiré à `prepare_session`, dérivé avec une clé serveur et un nonce, dont seul le hash est stocké ; il signe en HMAC chaque route opérateur et chaque appel broker, et n’apparaît jamais dans le navigateur, le workspace, un prompt ou un log.
_À éviter_ : jeton `OPERATOR_CLIENT` partagé comme seule preuve, bearer en clair, UUID de session nu.

**Préflight de capacités** : constat réel, versionné et hashé, de ce que l’adaptateur sait faire (exécutable, modes, reprise, sortie structurée, arrêt, permissions).
_À éviter_ : flags supposés, matrice commerciale, promesse d’interface.

**Réconciliation de session** : classification typée, après perte ou redémarrage, entre session vivante, arrêt propre, perte avec passation, perte non réconciliée et reprise disponible ou non.
_À éviter_ : relance automatique, succès supposé, réessai aveugle.

**Passation** (`InvestigationHandoffV1`) : version immuable et hashée du contexte nécessaire au prochain acteur — objectif, hypothèses, tentatives, résultats, limites, fichiers utiles, références et prochaine action.
_À éviter_ : transcription de conversation, copie d’octets, preuve, fichier de workspace.

**Fraîcheur de passation** : comparaison entre les références enregistrées dans une passation et les sources canoniques courantes ; toute dérive produit un diff visible, jamais un repli silencieux.
_À éviter_ : hash de fichier local, confiance dans l’horodatage seul.

**Contribution de travail** : note, script, rapport ou hypothèse importé comme travail non vérifié ; elle n’est pas une preuve. Un import rejeté reste une ligne terminale ; une nouvelle tentative de la même révision crée une nouvelle ligne, jamais une mise à jour.
_À éviter_ : observation, finding, note de l’analyste.

**Observation importée** : datum technique archivé avec objet, méthode, outil, version, hash, paramètres, origine, date et limites, écrit par le service d’observation canonique ; son `EvidenceKind` vient d’un mapping fermé `(object_type, method)` et sa `source` vaut `workspace_import`.
_À éviter_ : fichier local, log brut, contribution de travail, `EvidenceKind` fournisseur (`vt_*`, `shodan_*`) réutilisé.

**Import hôte** : import d’un fichier observé sur disque, sans session ni bail ; il est `origin='host'`, jamais attribué à un opérateur, et ne peut pas revendiquer l’autorité d’un bail expiré.
_À éviter_ : auteur déduit du dossier, import attribué à une session absente.

**Finding** : conclusion supported/refuted/qualified/inconclusive fondée sur des observations de la même enquête ; jamais automatiquement `supported`.
_À éviter_ : note, hypothèse, résultat de script non vérifié.

## Consultations et publication

**Consultation Bridge** : appel `advisor.consult` typé, persisté comme invocation et observations, avec texte utilisable même partiellement parsé mais jamais promu en preuve.
_À éviter_ : message de chat, proposition éditoriale, finding.

**Observation consultative** : observation persistée d’une consultation Bridge (`source` `advisor.consult`/`advisor.consult.response`, `object_type` `advisor_call`/`advisor_response_received`). Elle documente un avis, n’est jamais citable comme `evidence_ref`, et ne sert de provenance qu’exactement comme `advisor_response_received`.
_À éviter_ : preuve, finding, source de citation, `EvidenceKind` ordinaire.

**Registre des consultations** : vue typée en lecture sur les invocations et observations existantes ; il n’écrit rien.
_À éviter_ : nouvelle table de consultations, second stockage de preuve, journal éditable.

**Interprétation** : consultation Bridge consultative libre sur un résultat ou un ensemble d’observations, sans cible éditoriale.
_À éviter_ : `review_text`, rédaction, avis publiable.

**Critique** : consultation Bridge consultative qui éprouve une hypothèse, un plan ou un raisonnement, sans produire de texte publiable.
_À éviter_ : `review_text`, approbation, correction d’avis.

**Proposition de note** : proposition non canonique de texte Bridge pour une note ancrée, avec base d’avis éventuelle, SHA, provenance et état ; elle attend une acceptation humaine.
_À éviter_ : version d’avis, texte d’opérateur, brouillon de chat.

**Acceptation de note** : décision humaine qui recopie le texte Bridge stocké dans une nouvelle version canonique de l’avis, via la même porte `require_bridge_only_provenance()` que tout écrivain ; l’humain est accepteur, pas auteur.
_À éviter_ : édition humaine silencieuse, `bridge_authored` déclaré par le client, publication automatique.

## Termes à distinguer

| À éviter | Employer |
|---|---|
| session et tâche comme synonymes | session d’opérateur agentique (technique) ou tâche d’enquête (métier) selon l’objet |
| statut de tâche et statut de session comme synonymes | statut de tâche, statut technique de session ou statut de vérification du résultat |
| bail et jeton de clôture comme synonymes | bail de tâche (droit temporaire) ou `lease_fence` (preuve de non-obsolescence) |
| secret de session et `lease_fence` comme synonymes | secret de session (authentification HMAC) ou `lease_fence` (non-obsolescence d’un bail) |
| mode d’exécution et autorité de prose comme synonymes | `operator_led`/`bridge_led` (boucle) ou `legacy`/`bridge_only` (prose) |
| plan et tâche comme synonymes | version de plan (proposition Bridge) ou tâche d’enquête (unité exécutable) |
| lead de plan et tâche comme synonymes | lead de plan (proposition parsée) ou tâche (unité matérialisée, unique par `task_key`) |
| observation consultative et observation canonique comme synonymes | observation consultative (avis Bridge, jamais preuve) ou observation canonique |
| contribution, observation et finding comme synonymes | contribution de travail, observation importée ou finding selon la preuve produite |
| passation et preuve comme synonymes | passation (contexte) ou observation/finding (preuve canonique) |
| consultation et proposition de note comme synonymes | consultation Bridge (avis) ou proposition de note (texte éditorial en attente) |
| provenance déclarée et provenance vérifiée | provenance Bridge vérifiée depuis l’observation persistée |
| `legacy` et `bridge_only` comme préférences d’affichage | autorité de prose immuable de l’enquête |
