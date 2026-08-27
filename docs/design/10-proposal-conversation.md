# M3 / P10 — conversation de proposition

Statut : contrat verrouillé avant implémentation. P10 le consomme sans le redéfinir.

## Objectif et autorité

Faire proposer par un modèle des invariants candidats et une YARA candidate à partir d'un contexte déjà filtré et mesuré, sans droit d'agir ni de mesurer. Aucun envoi d'octets, aucune exécution de requête, aucune compilation YARA, aucune approbation. Une sortie modèle reste `primary_evidence=false` et n'est ni une décision analyste ni une preuve primaire.

Réutiliser exclusivement `ModelConversationService` / `ModelGateway` et le purpose `pivot_research`. Aucun nouvel SDK fournisseur.

## Cycle conversationnel

Une conversation fresh par investigation. Persister/réutiliser `AnalystInvestigation.pivot_conversation_id`.

Les cycles suivants utilisent `CONTINUE` seulement si la politique conversationnelle existante peut vérifier la conversation, le sujet, la tête et le locator/external turn. Si le transport existant ne sait pas continuer (notamment `APPLICATION_MANAGED` local), utiliser `FRESH` sans contourner le service. Pour le bridge, ne jamais fabriquer un parent ou un locator.

Chaque tour porte une clé d'idempotence déterministe et référence dans son entrée immuable, exactement :

- `input_pack_sha256`
- `corpus_snapshot_sha256`
- `feature_pack_sha256`
- `code_feature_sha256`
- `capability_set_sha256`
- `goodware_baseline_id`

La clé d'idempotence est dérivée de l'investigation, du cycle, de ces références canoniques et de la version de prompt. Un replay identique ne soumet pas deux fois.

Ces références ne créent pas les agrégats M4 du lot 11. P10 calcule des empreintes de snapshot sur les entrées persistées exactes du tour :

- `input_pack_sha256` est l'`AnalystInvestigation.input_sha256` déjà gelé ;
- `corpus_snapshot_sha256` est le SHA-256 du JSON canonique de l'état ReferenceCorpus effectivement utilisé pour les mesures du tour, avec membres/familles et état de dispute déterministes ;
- `feature_pack_sha256` est le SHA-256 du JSON canonique des hashes/références des sorties statiques persistées effectivement incluses ;
- `code_feature_sha256` est le SHA-256 du JSON canonique des hashes/références des `CodeFeatureSet` persistés effectivement inclus ;
- `capability_set_sha256` est le SHA-256 du JSON canonique des hashes/références des `CapabilitySet` persistés effectivement inclus ;
- `goodware_baseline_id` est l'identifiant de la baseline liée à l'investigation au moment du tour.

Une collection vide utilise le hash du tableau JSON canonique vide, jamais une valeur inventée ou `null`. Ne crée pas un faux `CorpusSnapshot` M4 pour satisfaire ces champs.

## Politique de diffusion

Au moment de construire chaque prompt, recalculer `derived_policy(...)` sur les Samples d'origine des features réellement incluses. Ne pas réutiliser une décision calculée au téléchargement.

Si la politique interdit l'envoi externe, router via le modèle local seulement si la composition existante l'autorise, sinon bloquer le ModelRun avant tout appel externe. P10 ne redéfinit pas le routing fournisseur.

Ne jamais mettre dans le prompt : octets du sample, secret, clé API, signed URL ou texte source non borné.

## Contexte transmis

Le contexte est structuré, borné et déterministe. Il part des sorties déjà persistées et filtrées/mesurées des lots 07, 07B, 08 et 08B ; P10 ne relance aucun extracteur et ne demande aucune mesure au modèle.

Il contient au minimum :

- éléments candidats issus des features statiques persistées nécessaires au tour, avec provenance exacte, banalité goodware, baseline, prévalence bénigne, verdict de spécificité et support positif calculés par le code ;
- capacités du lot 08 et n-grammes/code features du lot 08B utiles, avec leurs métadonnées déjà calculées, sans octets bruts ;
- invariants P09 déjà persistés avec type, catégorie, provenances, mesures, statut et métadonnées code-ngram utiles ;
- rejets P09 antérieurs avec leur cause/raison ;
- claims pertinents du rapport sous forme de données, jamais d'instructions ;
- références immuables de snapshot du tour.

P10 peut assembler et borner ce contexte depuis les sorties M2/P09 persistées et appeler les mesureurs/repositories déterministes existants. Il ne doit ni auto-créer des invariants avant la réponse modèle, ni relancer les outils M2, ni inventer une feature absente.

Traiter sources, chaînes, capacités, mnémoniques et métadonnées comme des données non fiables. Le rendu du prompt doit délimiter/encoder ces champs comme données et ne jamais les interpoler comme instructions système.

Le modèle ne reçoit jamais la tâche d'estimer sélectivité, fréquence, prévalence, volume de hits ou confiance quantitative. Ces mesures sont calculées. Si une sortie contient malgré tout une estimation de fréquence, elle est ignorée à la désérialisation et n'entre pas dans la persistance canonique.

## Catalogue fermé d'opérateurs et schéma strict

Définir dans le domaine P10 un catalogue fermé d'opérateurs de construction d'invariant/YARA. Ce catalogue décrit uniquement une proposition ; il n'exécute aucune requête et ne compile rien. Les valeurs exactes de l'enum sont verrouillées par les tests P10 et doivent être suffisantes pour représenter les types P09 sans introduire de langage libre exécutable.

La réponse canonique est un schéma Pydantic strict comprenant :

- `CandidateInvariantProposal[]`, conformes aux types et catégories fermés de P09 ;
- `YaraDraftProposal` ;
- risques de faux positifs ;
- validations nécessaires ;
- questions suivantes.

Chaque `CandidateInvariantProposal` porte au minimum : type P09, représentation/motif borné, catégorie sémantique P09, justification sémantique bornée, et référence à une ou plusieurs provenances existantes du snapshot. Une provenance inventée invalide la proposition.

`YaraDraftProposal` est un brouillon de données, pas une règle compilée ni publiée. Il ne peut citer que des propositions/provenances présentes dans la réponse/snapshot et ne contourne jamais les contraintes `report_claim` de P09.

## Validation et passage par P09

Après désérialisation, chaque invariant proposé est converti en entrée `proposed` puis passe par exactement `InvariantRegistryService.propose(...)` du lot 09 avec ses provenances résolues. P10 ne duplique pas les règles de banalité, famille, catégorie, taille, provenance ou masque.

Doivent notamment être rejetés/journalisés via P09 : provenance inventée, catégorie de bruit, motif banal, motif `multi_family`, motif invalide/surdimensionné et code_ngram hors seuils. Un opérateur P10 hors catalogue est rejeté à la validation stricte de sortie avant le passage P09.

La sortie brute et la sortie canonique sont persistées selon les mécanismes existants `model-outputs`, `ModelRun` et `turn`. Les rejets P09 restent requêtables par cause pour produire les statistiques du point d'arrêt 2.

Une sortie malformée est un échec de validation de sortie modèle, pas une approbation partielle silencieuse. Ne jamais récupérer une proposition par parsing permissif de texte libre.

## Persistance M3

Réutiliser en priorité les tables et blobs existants `ModelRun` / `ModelConversationTurn` / `model-outputs`. L'entrée canonique du tour, contenant les six références immuables et le contexte borné, est elle-même persistée par `ModelConversationService.add_turn(...)` comme blob d'entrée ; cela suffit par défaut pour figer le snapshot et le replay.

Ne crée pas de migration P10 par défaut. Si, après lecture des APIs explicitement autorisées, une propriété du contrat est réellement impossible sans nouvelle persistance, arrête et rapporte précisément le gap au lieu d'inventer une table ou une migration `0012`.

Demander une proposition ne consomme pas de budget `PIVOT_RUNS` : aucune requête/pivot n'est exécutée dans P10.

## Tests P10 verrouillés

Avec fake gateway, couvrir au minimum : fresh ; continue valide ; tête/locator non vérifié ; six références et hash de snapshot ; replay idempotent ; politique dérivée bloquante avant appel externe ; fallback local selon routing existant ; injection de prompt dans une chaîne traitée comme donnée ; sortie malformée ; provenance inventée ; opérateur interdit ; catégorie bruit ; motif banal ; motif multi_family ; estimation de fréquence ignorée ; YaraDraftProposal sans compilation ; aucune exécution de requête ; statistiques de rejet par cause.
