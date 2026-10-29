# Boucle agentique d’investigation

## Responsabilités

`ResearchContextBuilder` résout à chaque tour la synthèse canonique liée à
l’enquête et le `AnalystInputPackV1` immuable. Le premier contexte est `full`
(narration, claims avec leurs références de preuve, IOC/échantillons et
questions ouvertes). Les suivants sont `delta` : observations, findings,
actions récentes et hypothèses stables. Le contexte est canonique, borné et
rejette les octets, secrets et URL signées. Les valeurs d’article, de binaire ou
de fournisseur sont marquées comme données non fiables.

`BridgeInvestigator` n’a pas de client fournisseur. Son transport de production
crée ou reprend une conversation `pivot_research` et soumet le JSON structuré
uniquement par `ModelConversationService.add_turn`. La clé est déterministe par
enquête, index de tour, hash du contexte et version de prompt. Un résultat déjà
réussi est relu ; un tour `needs_review` ou encore `running` n’est pas soumis à
nouveau. `ScriptedTurnTransport` permet les tests sans réseau.

`InvestigationService` lance l’enquête depuis une synthèse vérifiée via
`AnalystPostSynthesisService`, puis pilote contexte → décision → appels d’outils
proposés → observations et findings → brouillon d’avis. Les appels sont
dispatchés dans l’ordre choisi par Bridge. Zéro appel, plusieurs appels et des
ordres différents sont valides. La boucle ne consulte pas `current_stage` pour
autoriser une opération.

## État, preuves et reprise

`InvestigationWorkingState` est un JSON canonique `investigation-state.v1`,
versionné avec CAS. Il conserve agenda souple, hypothèses et leurs références,
index de tour, hash du contexte/de la décision, compte de tours sans progrès,
raison d’arrêt et le pointeur/hash d’un snapshot de contexte en attente. Le
snapshot immuable est stocké à part : l’état reste petit et permet de recalculer
la même identité de soumission après un crash, même si le budget a été réservé
avant l’arrêt du processus.

Chaque outil reçoit une `tool_operation_key` stable. `InvestigationService`
inscrit l’invocation en `pending`, puis `running`, avant l’adaptateur. Une
invocation `succeeded` ou `empty` relue depuis `ToolInvocationRepository` rend
ses références persistées sans réexécution. Une invocation laissée `running`
après crash devient `needs_review` : l’application ne suppose pas que l’effet
externe n’a pas eu lieu. Les sorties JSON bornées peuvent être archivées comme
observations via `ProductionArtifactObservationWriter`; les octets de binaire
ne sont jamais ajoutés au contexte.

Les propositions de findings sont contrôlées par code. Les références
d’observation et de claim doivent exister dans la même enquête et le snapshot
de l’article. Un finding `supported` sans observation existante, avec une
observation inexistante, ou avec un claim inexistant devient `inconclusive` et
porte une limitation explicite. Les citations d’avis sont vérifiées avec
`verify_opinion_citations` avant l’append de version dans
`AnalystOpinionRepository`. L’avis reste un brouillon et n’altère aucun artefact
de publication.

## Autorisations, budget et arrêts

Avant chaque tour, le service relit la politique du pack d’entrée et vérifie
`tlp`, `do_not_submit` et `external_llm_allowed`, ainsi que l’enveloppe G2. Si
l’envoi externe n’est pas permis, aucun appel modèle n’est fait ; le contexte et
les lectures locales restent déterministes, puis l’état porte
`model_unavailable` pour une reprise ultérieure après évolution des droits.

Le dispatcher applique les capacités réellement enregistrées, les schémas,
l’enveloppe et les budgets. Une permission manquante arrête l’enquête en
`awaiting_review` sans transformer l’incident en échec technique. Un outil
indisponible ou refusé localement produit son résultat et laisse l’agent
continuer par une autre piste. Un dépassement de budget produit
`budget_exhausted` sans exception globale. Les résultats sans progrès répétés
aboutissent à `no_new_information`. `max_turns_per_run` borne la durée d’un appel
worker ; si cette tranche se termine avant un arrêt métier, l’état reste actif
et `run` peut être rappelé.

Les raisons typées `agent_finished`, `budget_exhausted`,
`no_new_information`, `user_interrupt`, `model_unavailable`,
`permission_required` et `submission_needs_review` sont projetées sur les
statuts persistés existants de `AnalystInvestigation`. Les stages et leurs
valeurs persistées restent disponibles pour l’affichage seulement.

## Points d’extension

Les outils se déclarent dans `ToolRegistry` avec leur schéma, effet, capacité,
confidentialité et coût. `evidence.get`, `article.get_claims`,
`sample.get_feature_pack` et `graph.neighbors` sont des lectures locales
enregistrables comme les autres tools. Le port article lit les artefacts
vérifiés `SYNTHESIS`/`EXTRACTION` et le pack ; un fournisseur absent donne
`unavailable` pour l’outil concerné.

L’état SQL sera un adaptateur futur de `InvestigationStateRepository`. Le lot B
fournit le contrat CAS et son fake en mémoire ; il ne modifie ni `persistence.py`
ni l’infrastructure ni le schéma.

## Preuves et nouveauté

`EvidenceRecorder` écrit les petits payloads JSON assainis dans
`ProductionArtifactStore`, puis ajoute une `EvidenceObservation` immuable au
repository. Les blobs déjà archivés sont référencés par `blob_id` et SHA-256,
sans relire ni copier leurs octets. `record_negative` conserve aussi le motif
du constat et ses limites : une requête vide, une capacité absente ou une
recherche sans hit ne signifie pas une absence mondiale.

Chaque observation porte une clé déterministe. Si l’adaptateur fournit
`idempotency_key`, l’identifiant est un UUIDv5 de cette clé dans le namespace
du recorder ; réutiliser cette clé avec un contenu différent échoue. Sans clé
explicite, l’UUIDv5 est calculé sur l’enquête, le type, le SHA-256, la
provenance, l’objet exact, les limites et l’invocation. `observed_at` ne fait
pas partie de la clé afin qu’un rejeu ne crée pas une nouvelle observation.
Les payloads sont limités à 128 Kio après suppression des champs secrets et
binaires et masquage des URLs ; octets, valeurs de secret et URLs ne sont pas
archivés dans les observations JSON.

Les adaptateurs injectent le recorder par constructeur et renvoient leurs
identifiants typés dans `ToolResult.observation_refs`. Le writer générique de
la boucle garde son observation `OTHER` pour les anciens outils qui ne
renvoient que `output` ; dès qu’un résultat cite des observations, il
n’archive pas une seconde observation générique.

`FindingService.propose` et `revise` vérifient les observations dans la même
enquête et les claims dans le snapshot de son article. Une proposition
`supported` sans observation devient `inconclusive` avec une raison typée ;
une référence de claim inconnue ou une observation d’une autre enquête est
refusée. Les révisions sont append-only et relient leurs versions avec
`supersedes`. `knowledge_of_claim` expose l’historique, les preuves favorables,
contradictoires ou qualifiantes, les observations ignorées et les références
manquantes. Une sortie Bridge seule ne peut donc pas produire le verdict
« confirmé ».

`ArticleNoveltyService.classify` compare samples, IOC, comportements et
affirmations au seul `ArticleSnapshot` demandé. Les hashes sont comparés par
famille (MD5, SHA-1, SHA-256), sans conversion entre familles. Un candidat
absent du snapshot n’est classé `new_to_article` qu’avec une observation
persistée de la même enquête ; la portée reste « non documenté dans cet
article/ce corpus » et n’établit jamais l’absence mondiale. Un hash déjà cité
ne devient pas `new_to_article` ; une affirmation corrigée doit référencer un
claim du snapshot et une observation de l’enquête.

## Reprise et validation de la robustesse

Avant le dispatch, la boucle enregistre la réservation de budget et l’invocation
`running`, puis valide ce point de commit. Elle persiste le résultat, les
observations et la consommation juste après l’appel. Un adaptateur reçoit
`operation_key` comme clé d’idempotence via `ToolExecutionContext` et le
dispatcher. Si une invocation `running` est retrouvée après crash, elle passe à
`needs_review` avec sa clé d’opération ; elle n’est jamais relancée. La reprise
attend une réconciliation durable avant de reprendre l’enquête.
Les ports existants reçoivent cette identité via le contexte d’exécution ; si un
port fournisseur n’accepte pas de clé native, la couche d’application ne peut
pas garantir l’issue distante d’un timeout et conserve l’invocation en revue
sans rappeler le fournisseur.

La réconciliation accepte `executed_with_result`, `not_executed` ou `abandon`.
Elle valide les observations éventuelles, applique le CAS de l’invocation,
termine sa réservation et ajoute une décision analyste auditée. Après
`not_executed`, le budget réservé est libéré ; les issues qui peuvent avoir
produit un effet externe comptent la réservation comme consommée.

Avant chaque outil externe ou acquisition, le service relit l’enveloppe G2. Une
révocation ou réduction survenue depuis le chargement du tour crée une
invocation `policy_denied` avec les alternatives locales disponibles, puis
termine le tour sans démarrer les autres opérations externes proposées.

Une réponse Bridge qui est soumise sans ambiguïté mais invalide au schéma reçoit
au plus deux tours correctifs. Chaque correction porte l’erreur de validation
bornée dans un contexte révisé et une nouvelle clé d’idempotence. Après le
troisième résultat invalide, `submission_needs_review` est persisté ; une
nouvelle demande utilisateur est nécessaire pour reprendre. Une soumission
Bridge ambiguë reste en revue sans nouvelle soumission.

La nouveauté des findings `NEW_SAMPLE` et `NEW_TO_ARTICLE` est recalculée côté
service contre le snapshot complet, qui inclut aussi les hashes trouvés dans la
narration, les claims et les sources. `NEW_SAMPLE` exige un `subject_ref`
identifiable ; sinon le finding devient `inconclusive`. Un hash déjà documenté
ne peut rester `new_to_article`.

Pour les findings à sujet identifiable, `supported` exige qu’une observation
cité porte exactement le `subject_ref`. `CLAIM_VERIFICATION` exige une claim du
snapshot et une observation autre que `CLAIM_SOURCE`. L’avis ne peut donc pas
présenter comme fait un finding rétrogradé. La correspondance exacte du sujet
ne prouve toutefois pas la pertinence sémantique de l’observation ; la revue
éditoriale humaine de l’avis reste nécessaire.

Le seuil `no_progress_limit` est injecté par le constructeur. À la limite, le
stop `no_new_information` est suspendu si le dernier tour change
d’hypothèse (`SWITCH_HYPOTHESIS`) ou propose une opération jamais vue. Répéter
la même `operation_key` ne compte pas comme une piste nouvelle.
