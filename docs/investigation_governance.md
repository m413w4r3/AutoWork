# Gouvernance G2/G3 et acquisition des hits

Cette couche sépare l'autorisation d'enquête, l'acquisition d'un fichier et la
décision de confiance sur son appartenance à un cluster. Un hit découvert reste
une association **candidate**. Son téléchargement et son analyse locale peuvent
être automatiques dans les limites du G2. Seul un humain identifié peut le
faire passer à **confirmed** ou **rejected** au G3.

## G2 : enveloppe d'enquête

**InvestigationEnvelopeService.propose_default(investigation_id)** retourne une
proposition locale, non persistée et non approuvée. Elle n'autorise aucun
provider ni acquisition, fixe les budgets **VT_UNITS** et **DOWNLOADS** à zéro,
interdit la soumission et l'envoi à un LLM externe. Elle enregistre aussi
**TOOL_SECONDS=3600** pour le suivi comptable et inclut les plafonds globaux
**CONTEXT_BYTES=4 000 000** et
**CONTEXT_TOKENS=1 000 000** par défaut. L'enveloppe persistée n'existe
qu'après une décision G2.

**approve(proposal, actor_id, reason, correlation_id)** écrit une décision
**PIVOT_APPROVE** ciblant **PIVOT** et enregistre l'enveloppe version 1 par CAS.
Le rejeu de la même corrélation et du même contenu renvoie l'enveloppe déjà
créée. Une corrélation réutilisée pour une décision différente est refusée.
**extend(...)** vérifie **expected_version**, ajoute une nouvelle décision
**PIVOT_APPROVE** et crée une nouvelle version. Chaque extension doit autoriser
explicitement les providers, droits, budgets et bornes de divulgation. Si la
proposition ou l'approbation omet un plafond global, les valeurs configurées
par `investigation_max_tool_seconds` et `investigation_max_context_bytes` sont
ajoutées; le plafond de tokens vaut le nombre d'octets divisé par quatre.
**reject(...)** persiste **PIVOT_REJECT** pour une proposition qui n'a pas encore
été approuvée. **revoke(...)** révoque une enveloppe approuvée en ajoutant une
décision auditée et une nouvelle version sans droits externes; les limites G2
externes passent à zéro. Les plafonds historiques LoopBudget restent inchangés
ou sont relevés pour rejoindre **DOWNLOADS**, conformément à la règle
« croissance uniquement ».

L'enveloppe d'acquisition doit autoriser le provider **vt**, l'acquisition de
hits et des limites finies non nulles pour **VT_UNITS** et **DOWNLOADS**. Les
réservations et consommations sont idempotentes par clé d'opération. Le plafond
G2 **DOWNLOADS** est la source canonique du nombre d'acquisitions : lors de
l'approbation ou d'une extension, les plafonds historiques
**max_hits_acquired**, **max_new_samples** et **max_vt_read_units** de
`AnalystInvestigation` sont relevés au moins à cette valeur, dans la même
transaction. Ils ne diminuent jamais, y compris lors d'une révocation; le G2
révoqué fixe ses droits externes à zéro et reste l'autorité d'accès.

Le dispatcher réserve et comptabilise **TOOL_SECONDS**, sans refuser un appel
quand ce montant comptable est épuisé. Les délais de garde configurés par appel
continuent de limiter la durée d'exécution. Les plafonds
**CONTEXT_BYTES** et **CONTEXT_TOKENS** sont inscrits dans l'enveloppe, mais
leur consommation par le constructeur de contexte n'est pas encore branchée.

Une association déjà présente dans l'instantané d'article retourne **empty**
sans nouveau téléchargement. Un succès d'acquisition déjà associé à la même
enquête est réutilisé sans deuxième téléchargement ni seconde consommation.
Si le même SHA-256 existe pour le sujet sans succès d'acquisition dans l'enquête
courante, l'outil demande une association explicite au lieu de réutiliser
silencieusement l'objet d'une autre enquête.

## Acquisition et analyse

**HitAcquisitionService.register_hit(investigation_id, sha256, origin,
evidence_refs)** n'effectue que l'observation et l'association candidate.
L'origine est l'une de **vt_pivot**, **relationship** ou **behavior**; le
dédoublonnage est par SHA-256 dans l'enquête. Les autres familles
d'empreintes ne sont pas acceptées comme identité d'association par cette API.

**acquire_hit(association_id, purpose, evidence_refs, operation_key, envelope)**
vérifie le G2 courant, TLP, **do_not_submit**, les budgets, les références et
l'idempotence, puis délègue exclusivement à
**VirusTotalSampleAcquisitionService**. Celui-ci vérifie le hash du contenu,
stocke le blob dans le bucket de quarantaine et crée une tentative
**HIT_REVIEW**. Le résultat reste **candidate**; acquisition ou analyse ne
constituent jamais une validation G3.

**LocalSampleAnalysisService** ne lit qu'un échantillon quarantiné dont le blob
correspond au SHA-256 de l'association. Il compose les services existants
Static Analysis, SMDA (**CodeFeatureService**), capa (**CapabilitiesService**) et
la mesure Goodware lorsque leurs dépendances sont disponibles. Les compositions
API et worker fournissent SMDA et la mesure Goodware si l'analyse locale est
activée. Un service ou corpus Goodware absent laisse l'outil utilisable avec
l'état **partial**. Les sorties sont bornées et
les versions des extracteurs, paramètres, limites et réserves sont enregistrés
dans des observations **BINARY_ANALYSIS**; les chaînes extraites produisent des
observations **BINARY_STRING**. La comparaison Goodware reste limitée au corpus
lié à l'enquête et ne qualifie pas l'échantillon de bénin.

Clés de capacité enregistrées par
**register_acquisition_tools(registry, hits, qualifications, analyzer)** :

| Outil | Effet | Capacité | Fonction |
| --- | --- | --- | --- |
| **vt.download_sample** | **ACQUIRE** | **vt.file_download** | Associer un hit puis demander son acquisition hash-vérifiée |
| **sample.analyze** | **LOCAL_COMPUTE** | **investigation.local_read** | Lancer les extracteurs locaux disponibles |
| **sample.set_qualification** | **LOCAL_COMPUTE** | **investigation.local_read** | Permettre à l'agent de proposer candidate, related ou uncertain |

Le contexte de dispatch doit fournir **vt.file_download** pour exposer le
téléchargement et **investigation.local_read** pour les outils locaux. Pour
**vt.download_sample**, K1 doit faire correspondre la clé de réservation du
dispatcher et **ToolExecutionContext.idempotency_key**; l'adaptateur transmet
cette dernière à la réservation G2. Cette identité stable évite une double
réservation sous deux clés différentes.

## G3 : qualification

**SampleQualificationService.qualify(association_id, new_state, actor, reason,
evidence_refs)** ajoute une version à l'historique de l'association. Les
références doivent appartenir à la même enquête. Elles sont incluses dans la
justification textuelle car **QualificationChange** ne possède pas encore de
champ **evidence_refs**.

Les transitions **confirmed** et **rejected** exigent un acteur **HUMAN** avec un
identifiant non vide et enregistrent respectivement **MEMBER_VALIDATE** ou
**MEMBER_REJECT**, cible **MEMBER**. Les propositions **candidate**, **related**
et **uncertain** sont disponibles à l'agent avec une justification. L'agent ne
peut pas envoyer une qualification G3 par **sample.set_qualification**.

Le raccord à **ReferenceCorpusService.promote** reste une action P09/P10
distincte : la promotion existante requiert une famille de référence. Le
service G3 ne fabrique pas cette famille et ne déclenche pas seul la promotion.

## États retournés et routes K2

Les adaptateurs utilisent les états **unavailable**, **policy_denied**,
**needs_review**, **empty**, **partial**, **failed** et **succeeded** de
**ToolInvocationStatus**. Une capability locale absente donne **unavailable**;
une association déjà documentée donne **empty**; l'absence de service
d'acquisition ou d'analyse donne **unavailable**; des extracteurs partiellement
disponibles donnent **partial**; une erreur d'extraction ou de téléchargement
donne **failed**. L'adaptateur d'acquisition retourne **needs_review**,
**request_permission: true** et des alternatives lorsque le G2 ne couvre pas la
demande.

K2 doit exposer des opérations distinctes pour :

- lire la version et les droits G2 courants;
- approuver une proposition avec acteur authentifié, raison, corrélation,
  providers, budgets, droit d'acquisition et divulgation;
- étendre l'enveloppe avec **expected_version** et une nouvelle décision;
- enregistrer un rejet de proposition G2;
- présenter les associations et leur historique;
- valider ou rejeter un membre au G3 avec une identité humaine issue de
  l'authentification, les preuves et la corrélation.

Le client ne doit pas pouvoir fournir un faux acteur humain. L'approbation G2
est distincte de l'appel **vt.download_sample**; ce dernier n'exige pas de
nouveau G3 par fichier.

## Limites d'intégration à traiter

Le dispatcher existant applique **POLICY_DENIED** aux outils **ACQUIRE** hors
provider ou droit G2 avant d'appeler leur adaptateur. L'adaptateur C2 peut
produire **needs_review** et **request_permission**, mais un appel via ce
dispatcher sera préempté. Le contrat **ACQUIRE** et l'état de dispatch devront
être réconciliés dans la couche d'orchestration autorisée avant que le chemin
complet puisse garantir le statut **needs_review**.

Le contexte de dispatch existant exige une **InvestigationEnvelope** approuvée.
La proposition locale non approuvée ne peut donc pas encore donner accès aux
outils locaux via ce dispatcher avant G2. Le modèle de contexte doit accepter
explicitement l'état local non approuvé, sans lui attribuer une fausse décision.

Le dépôt de domaine ne stocke pas **evidence_refs** dans chaque
**QualificationChange**; C2 les conserve dans **reason**. Le service
d'association est limité à SHA-256. **SampleAssociation** n'a pas non plus de
champ de preuve de découverte : **register_hit** valide ses références mais ne
peut pas les attacher durablement à l'association. La promotion au corpus
reste à relier explicitement à P09/P10.
