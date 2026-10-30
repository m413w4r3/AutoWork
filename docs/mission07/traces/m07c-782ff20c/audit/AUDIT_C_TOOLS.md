# Audit M07c — outils hors VirusTotal et Bridge

Date : 2026-10-30  
HEAD vérifié : c921848c301407aa8718b833ab35793d3e0bbeca  
Périmètre : sources à HEAD et traces/outbox de .autowork-runs/m07c. Aucun appel live, test, accès aux secrets ou lecture de blob runtime. Ce rapport n’est pas une preuve CTI canonique.

Chronologie : FINAL_CHECKPOINT_B s’arrête à v148; le dernier snapshot complet d’outbox consulté, c084, est v162. Le journal contient ensuite c083 à v163, sans checkpoint final ultérieur. Les chiffres ci-dessous décrivent les réponses archivées, pas une assertion sur l’état canonique après v163.

## Résumé des constats

1. finding.revise peut enregistrer une nouvelle version avec une assessment dégradée tout en répondant succeeded. C’est valide pour l’invariant de provenance, mais la réponse opérateur masque l’assessment sauvegardée quand elle a des observations : elle ne retourne que leurs résumés/références. Les lectures archivées montrent un seul finding infection_chain courant inconclusive; l’ancienne version inconclusive reste dans l’historique, ce qui est normal pour une révision append-only.
2. archive_list suit le worker d’analyse, pas le chemin d’exécution locale bwrap/seccomp. 7z_unavailable signifie FAILED_TO_START après une OSError au lancement, pas que le paquet est absent. Les sources ne prouvent pas quel détail runtime diffère du test manuel.
3. Le 404 des observations protège contre les références absentes ou hors enquête. response_refs n’a qu’une contrainte « tableau JSON », sans FK vers les observations. Les outbox contiennent trois operator_observation_not_found et un operator_observation_not_json.
4. rule.workbench est une clé de capacité, pas un outil invocable. La configuration a bien false par défaut, mais le dernier inventaire complet archivé (v162) offre les outils rule.* locaux, dont rule.test.
5. Le routeur actuel permet les GET de sections de contexte avec un client opérateur. Les huit 403 du journal portent sur des routes de lecture historiques; le journal ne montre pas de 403 sur la route courante /operator/context/section/{section}. Une identité de délégation explicite manque néanmoins.
6. Bug confirmé de projection : needs_review avec des response_refs est exclu des listes d’invocations à réconcilier, alors que le chemin de rejeu dit explicitement qu’une telle invocation doit être réconciliée. Les checkpoints archivés montrent quatre invocations needs_review et des listes de réconciliation vides.

Les recommandations de diff et de test ci-dessous n’ont pas été appliquées; aucun test n’a été lancé.

## 1. finding.revise : succeeded, assessment sauvegardée inconclusive

### Vérifié

- La requête locale b006-finding-assessment rapporte un revise demandé à supported. L’outbox b053 montre l’invocation finding.revise à succeeded (journal.jsonl:281). Les lectures canoniques archivées b055 et b056 sont à l’état v140 : un seul finding infection_chain figure dans la projection courante et il est inconclusive; l’historique de versions contient deux versions inconclusive de ce finding.
- La révision est append-only : FindingService.revise crée un nouvel identifiant de version et renseigne supersedes (backend/src/cti_app/application/investigation_findings.py:567-596). La projection courante filtre les versions superseded (backend/src/cti_app/api/investigation.py:2844-2846). La présence de l’ancienne version dans finding_versions ne prouve donc pas un doublon courant.
- Pour les types qui identifient un objet, dont infection_chain, supported exige un subject_ref non vide et une observation dont object_id est exactement égal à ce sujet (backend/src/cti_app/application/investigation_findings.py:45-54, 390-425). Sans sujet, le garde rétrograde en inconclusive. Si le sujet existe, mais qu’aucune observation ne le vise, le résultat est qualified. À part, le service rétrograde supported à inconclusive quand aucune observation n’est citée (lignes 709-721).
- L’adaptateur retourne toujours un statut d’invocation succeeded après l’enregistrement et place l’assessment réellement sauvegardée dans ToolResult.output (lignes 197-206). Toutefois, OperatorToolInvocationService._result remplace ce résultat par des résumés d’observations dès qu’il trouve des observations persistées (backend/src/cti_app/application/operator_tool_invocation.py:1488-1518). La réponse b053 contient des références d’observations, mais pas le champ assessment. « succeeded » certifie l’enregistrement de la version, pas l’acceptation de l’assessment demandée.

### Cause et limite

La garde d’objet est une protection de provenance, pas une friction à enlever. La cause exacte de la rétrogradation n’est pas reconstructible depuis l’outbox autorisée : elle ne conserve ni le subject_ref fourni/récupéré, ni la comparaison entre object_id des observations et ce sujet. Si les références d’observations de b053 sont celles que l’adaptateur a chargées, l’absence de sujet récupérable est l’explication la plus probable : un sujet présent mais non concordant aurait produit qualified, pas inconclusive. C’est une inférence, à confirmer avec les paramètres canoniques de l’invocation.

### Diff minimal proposé

Conserver la garde d’assessment. Lorsqu’une réponse contient des observations, joindre aussi un résumé typé et borné du résultat des outils finding :

~~~~diff
- output_data = {"observations": persisted}
+ output_data = {
+     "result": select_finding_result(tool_result.output),
+     "observations": persisted,
+ }
~~~~

Pour finding.propose/revise, le résumé peut limiter les champs à finding_id, finding_version, kind et assessment. Ajouter assessment_requested et un code stable de rétrogradation permettrait d’expliquer « supported demandé, inconclusive enregistré » sans recopier du texte libre.

### Test ciblé proposé

Dans backend/tests/test_operator_tool_invocation.py, vérifier qu’une révision avec observations retourne à la fois les références et l’assessment sauvegardée. Dans backend/tests/test_operator_finding_tools.py, couvrir infection_chain avec sujet absent, sujet concordant et sujet non concordant; vérifier les assessments attendues inconclusive, supported et qualified. Les tests existants pertinents sont test_supported_operator_finding_without_real_observation_is_downgraded et test_supported_finding_requires_matching_observation_and_deduplicates.

## 2. archive_list : 7z_unavailable malgré le succès rapporté en conteneur

### Chemin réel vérifié

- ConfinedLocalCommands traduit archive_list en ConfinedAnalysisRequest et l’envoie par execution.execute (backend/src/cti_app/infrastructure/confined_local_commands.py:211-255). Le worker route ARCHIVE_LIST vers run_7z_archive_command avec les limites/configuration de la requête (backend/src/cti_app/workers/confined_analysis.py:423-441).
- Le worker écrit l’échantillon sous /tmp/cti-archive-*, mode 0400, puis appelle le chemin absolu /usr/bin/7z avec argv borné et PATH=/usr/bin:/bin, LANG=C, LC_ALL=C (backend/src/cti_app/workers/archive_commands.py:281-303). Le répertoire de travail temporaire de l’enfant vient de tempfile.TemporaryDirectory sans dir explicite (backend/src/cti_app/infrastructure/analysis_subprocess.py:59-69); il dépend donc du tempdir de l’environnement du processus worker, généralement /tmp.
- analysis_subprocess filtre l’environnement enfant aux clés autorisées et traduit toute OSError de create_subprocess_exec en FAILED_TO_START (analysis_subprocess.py:37-73). archive_commands mappe ensuite FAILED_TO_START vers 7z_unavailable (archive_commands.py:659-666). Ce code fusionne absence de fichier, refus d’exécution, erreur de loader et autres erreurs OS de lancement.
- Ce chemin n’applique pas le wrapper bwrap/seccomp du LocalCommandRunner. Ce dernier refuse explicitement les archives avec confined_archive_worker_required avant son lancement local (backend/src/cti_app/infrastructure/local_command_runner.py:173-188); son wrapper de namespace/seccomp est un autre chemin (lignes 226-269).
- b013 est une invocation local.run_command réussie, HTTP 200, state v114 (journal.jsonl:243). L’outbox expose le statut de l’invocation et une référence d’observation, pas le champ archive_status. La valeur 7z_unavailable et le succès du lancement manuel sont rapportés dans orchestrator_requests/b002-archive-list-7z.md et FINAL_CHECKPOINT_B.md; je ne les ai pas reproduits.

### Conclusion

Le nom 7z_unavailable est trop affirmatif : il prouve seulement que le sous-processus n’a pas démarré selon le wrapper. PATH n’explique pas la résolution du programme principal, puisque /usr/bin/7z est absolu. Si le lancement manuel utilisait réellement le même conteneur, UID, binaire, environnement, argv et limites, les sources n’expliquent aucune différence bwrap/seccomp entre les deux chemins. Restent non vérifiés l’image effective, l’UID, les droits de /usr/bin/7z et de ses loaders, le TMPDIR du processus parent, et la politique OS/container autour du worker. L’échec de création du tempdir ou d’écriture de l’échantillon arriverait avant la conversion en 7z_unavailable et pourrait donner un autre échec. Le mode 0400 du fichier ne peut causer ce code qu’indirectement; un refus de lecture après lancement donnerait plutôt un code de sortie 7z.

### Diff minimal proposé

Renommer la raison en 7z_start_failed et conserver un sous-code sûr issu de errno (not_found, permission_denied, other_os_error); ne jamais exposer stderr ou l’environnement complet. Le statut succeeded de l’enveloppe local.run_command doit rester distinct du archive_status du worker.

### Test ciblé proposé

Dans backend/tests/test_archive_commands.py, simuler FileNotFoundError et PermissionError depuis create_subprocess_exec et vérifier le code de lancement distinct. Ajouter un smoke test avec archive synthétique inerte dans l’image et l’UID exacts du worker, avec le même argv/env/limites. Le test list existant est test_7z_list_returns_bounded_metadata_without_extracting; il ne démarre pas 7z réel.

## 3. Observation project-content : 404 malgré une référence

### Vérifié

- L’appel a221 renvoie 404 (journal.jsonl:223). L’outbox compte trois réponses operator_observation_not_found et une operator_observation_not_json.
- La route canonique vérifie l’investigation, charge l’observation par UUID et renvoie le même 404 si la ligne est absente ou appartient à une autre investigation; elle ne révèle pas lequel des deux cas s’applique (backend/src/cti_app/api/investigation.py:1591-1609). Elle renvoie 415 si le blob n’est pas application/json, et refuse aussi les buckets binaires (lignes 1610-1618).
- response_refs est une colonne JSONB contrôlée comme un tableau, sans FK vers evidence_observations (backend/src/cti_app/infrastructure/database/models/investigation.py:68-99). La présence d’un UUID dans cette liste ne garantit donc pas qu’une observation lisible existe dans cette enquête.
- Le résolveur de réponse n’émet une URL d’observation qu’après avoir trouvé une ligne dans la même investigation; sinon il ignore cette référence. Si aucune observation n’est sérialisable, il reprend le résultat outil (backend/src/cti_app/application/operator_tool_invocation.py:1488-1518).

### Cause et limite

Le 404 est une garde correcte contre une référence absente/hors périmètre; l’assouplir risquerait une fuite inter-enquêtes. La trace seule ne permet pas de distinguer une référence stale, une observation non persistée ou un mauvais investigation_id. La répétition des deux erreurs différentes signale surtout que les références de réponse ne sont pas assez typées : certaines ne résolvent pas, d’autres pointent vers un contenu que cette route JSON ne sert pas.

### Diff minimal proposé

Valider les références avant de les publier dans response_refs et rapporter, dans un manifeste borné, les références omises avec un seul motif non révélateur not_found_or_out_of_scope. Ne jamais changer le 404 indistinguable de la route ni servir un blob binaire par ce point d’accès.

~~~~diff
- response_refs = tuple(result.observation_refs)
+ response_refs, omitted = await validate_same_investigation_observations(
+     investigation_id, result.observation_refs
+ )
+ output["omitted_observation_refs"] = omitted
~~~~

### Test ciblé proposé

Dans backend/tests/test_investigation_api.py, couvrir observation JSON même-enquête (200), UUID absent (404), observation autre-enquête (même 404), et observation non JSON (415). Ajouter un test producteur garantissant que toute référence publiée vient du repository d’observations de cette investigation. Le test existant est test_operator_observation_route_paginates_sanitized_canonical_json.

## 4. rule.workbench absent et réglage false par défaut

### Vérifié

- Settings.rule_workbench_enabled est False par défaut (backend/src/cti_app/config.py:422). Le port runtime déclare rule_workbench disponible seulement si le flag est vrai et qu’un moteur ou worker YARA existe (backend/src/cti_app/infrastructure/investigation_runtime_ports.py:856-866). La composition n’enregistre les outils d’écriture/test que si le workbench, corpus bytes et dépôt candidates sont présents (backend/src/cti_app/application/investigation_composition.py:1433-1458).
- rule.workbench est une capacité, pas un nom d’outil. Les outils locaux invocables sont rule.propose, rule.test, rule.revise, rule.compare et rule.get (docs/investigation_rule_tools.md:9-23, 38-51). À l’état initial a001, le diagnostic rapporte le flag désactivé. Mais le dernier inventaire complet c084, state v162, expose rule.compare/get/operator_syntax_fix/propose/propose_from_candidates/revise/test; aucun outil rule.workbench n’est attendu. La capacité est donc active dans ce snapshot, malgré le défaut de configuration source.
- Les outils locaux ne produisent pas de statut approved/published et la revue d’approbation reste humaine (docs/investigation_rule_tools.md:32-36). Le moteur local de secours n’est construit qu’après vérification de confinement filesystem (investigation_runtime_ports.py:306-312, 599-606). Ne pas confondre rule.propose_bridge avec cette famille locale; Bridge reste hors de cette recommandation.

### Diff minimal proposé

Activer le workbench par défaut pour les nouvelles configurations tout en gardant le refus fermé si le runner ou les ports manquent :

~~~~diff
- rule_workbench_enabled: bool = False
+ rule_workbench_enabled: bool = True
~~~~

Garde-fous à conserver : autoriser le runner local seulement après attestation bwrap/filesystem, sinon exiger le worker confiné; ne rien enregistrer si les dépôts/corpus manquent; ne créer que des candidats locaux, sans approbation/publication; garder l’environnement capable de désactiver explicitement le flag.

### Test ciblé proposé

Dans backend/tests/test_investigation_composition_families.py, mettre à jour le cas rule_get_is_registered_without_the_rule_workbench_when_read_ports_exist et ajouter : flag par défaut + ports confinés présents => famille exposée; ports absents => famille non disponible; flag explicitement false => rule.get seulement. Conserver les tests de backend/tests/test_investigation_rule_tools.py qui vérifient les états candidat/testés et l’absence d’approbation.

## 5. Sections de contexte refusées aux sous-agents/workers

### Vérifié

- Il n’existe que les scopes HUMAN_OPERATOR et OPERATOR_CLIENT (backend/src/cti_app/application/identity.py:10-28). Le StaticTokenIdentityProvider associe son jeton client à l’acteur fixe operator:operator-client; aucun actor de worker ni délégation liée à une investigation/section n’est porté dans cette identité (lignes 75-105).
- Le middleware renvoie operator_client_scope_denied pour les routes non allowlistées (backend/src/cti_app/api/main.py:670-675). Il autorise pourtant GET /api/investigations/{uuid}/operator/context/section/{section} et /operator/observations/{uuid} (lignes 805-873). Le handler de section accepte OPERATOR_CLIENT (backend/src/cti_app/api/investigation.py:1478-1508, 3958-3965).
- Les huit 403 de l’outbox ont le code operator_client_scope_denied sur les chemins historiques /article_claims, /working-state, /openapi.json, /evidence, /documented_samples, /article_long_texts et /known_chain. Je ne trouve pas de 403 archivé sur la route actuelle /operator/context/section/{section}; les appels c073-c075 et c082 sur cette route réussissent. Il y a donc un décalage entre la mention « sections de contexte 403 » du checkpoint et les chemins visibles dans le journal.

### Conclusion et diff minimal proposé

Pour ces traces, la première correction est côté client : viser la route de contexte par investigation plutôt que les endpoints historiques. Ne pas ajouter ces endpoints globaux à l’allowlist; le jeton operator_client a aussi des routes d’écriture et n’est pas lié à une seule investigation. Pour une vraie délégation, ajouter ultérieurement un grant signé à lecture seule, lié à worker_id + investigation_id + sections permises + expiration/révocation. Ne pas faire agir le worker sous l’identité humaine de l’opérateur.

~~~~diff
- GET /article_claims
+ GET /api/investigations/{id}/operator/context/section/article_claims
~~~~

### Test ciblé proposé

Étendre backend/tests/test_operator_client_routes.py : la route actuelle de section est autorisée au client opérateur, les anciennes routes restent refusées. Si le grant de délégation est ajouté, tester accès uniquement aux sections et investigation accordées, refus de POST/mutation, expiration et révocation.

## 6. needs_review masqué par la projection de statut

### Garde vérifiée

- investigation_operator_view sélectionne une invocation PENDING/RUNNING/NEEDS_REVIEW comme unresolved seulement si response_refs est vide; ambiguous est ensuite filtré depuis unresolved (backend/src/cti_app/application/investigation_operator_view.py:192-207). decision_expected impose la même condition (lignes 466-485).
- Le chemin de rejeu dit au contraire qu’un NEEDS_REVIEW avec response_refs exige une réconciliation explicite (backend/src/cti_app/application/investigation_service.py:3144-3152). Le validateur vérifie seulement que chaque observation existe dans la même investigation (lignes 3238-3246); il ne vérifie pas que c’est une sortie/provider response plutôt qu’une citation d’entrée.
- Les snapshots b074 et c084 ont chacun quatre invocations needs_review (3 advisor.consult et 1 vt.behaviour_pcap), toutes avec response_refs non vides, tandis que les tableaux ambiguous_invocations et invocations_requiring_reconciliation sont vides. Le checkpoint indique que les consultations ambiguës n’ont ni identifiant Bridge ni octets de sortie. Les références présentes ne prouvent donc pas que la réponse provider est connue.
- Il existe une route de réconciliation, mais elle n’est pas un outil opérateur : POST /api/investigations/{id}/invocations/{id}/reconcile exige un humain vérifié (backend/src/cti_app/api/investigation.py:4058-4087), un motif, une version attendue et un choix executed_with_result/not_executed/abandon. Le client operator_client ne peut pas y accéder; c’est une séparation de sécurité appropriée.

### Diff minimal proposé

Ne pas utiliser la présence de response_refs comme condition de sortie de NEEDS_REVIEW. Ces références restent affichables comme citations; l’invocation reste unresolved jusqu’à réconciliation auditée.

~~~~diff
- and not item.response_refs
+ and (
+     item.status is ToolInvocationStatus.NEEDS_REVIEW
+     or not item.response_refs
+ )
~~~~

Appliquer la même règle à decision_expected. La résolution reste réservée à l’humain via la route existante.

### Test ciblé proposé

Ajouter un test de projection d’invocation NEEDS_REVIEW avec au moins une observation valide : elle doit apparaître dans les deux listes avec decision_expected, puis disparaître seulement après une réconciliation auditée. Tester séparément NEEDS_REVIEW sans références. Ne pas tester par rejeu d’un appel externe.

## 7. Autres erreurs et fréquences observées

### Méthode de comptage

387 événements journal/outbox ont été rapprochés par identifiant; 162 contiennent une réponse d’invocation directe et tous ont un identifiant distinct. Le tableau compte ces 162 réponses, sans dédupliquer les invocations via les snapshots de statut. Les statuts partial et empty sans error_code sont séparés des erreurs.

| Outil | Statut | error_code | Nombre |
|---|---|---|---:|
| advisor.consult | failed | tool_failed | 11 |
| vt.relationships | empty | aucun — résultat vide | 8 |
| sample.analyze | partial | aucun | 6 |
| sample.extract_obfuscated_strings | partial | aucun | 5 |
| advisor.consult | partial | aucun | 4 |
| vt.relationships | failed | tool_failed | 3 |
| advisor.consult | needs_review | invocation_needs_review | 3 |
| ghidra.decompile_function | failed | tool_failed | 2 |
| vt.file_report | failed | tool_failed | 2 |
| archive.extract_member | failed | tool_failed | 1 |
| finding.revise | failed | tool_failed | 1 |
| local.run_command | unavailable | tool_unavailable | 1 |
| local.run_command | policy_denied | tool_policy_denied | 1 |
| method.decompile | unavailable | tool_unavailable | 1 |
| strings.list | failed | tool_failed | 1 |
| vt.behaviour_pcap | needs_review | invocation_needs_review | 1 |
| advisor.reparse | partial | aucun | 1 |

Les outils VirusTotal et Bridge sont hors de l’audit causal demandé. Pour les inclure dans le comptage seulement : les quatre needs_review directs sont 3 advisor.consult et 1 vt.behaviour_pcap. Les échecs advisor.consult ont des stages evidence_preflight (10), pre_transport_validation (1), provider_submission_ambiguous (3) et reconciliation (2); ces nombres couvrent plusieurs statuts. Je n’ai pas analysé leur contenu ni les transports. vt.relationships empty n’est pas un échec.

Les error_code imbriqués distincts comprennent advisor.consult invalid_tool_arguments (3) et advisor_timeout_or_ambiguous (2), et finding.revise finding_observation_not_found (1). Ces raisons imbriquées sont déjà incluses dans le statut top-level correspondant et ne s’ajoutent pas au total.

Les erreurs HTTP des réponses sans invocation, comptées séparément : operator_client_scope_denied 8, operator_observation_not_found 3, operator_observation_not_json 1, stale_working_state 1. Le 409 stale_working_state est une protection de version/pagination; le client doit rafraîchir le contexte et son curseur, pas réutiliser la page périmée.

Hors VT/Bridge, les erreurs invocables non uniques les plus visibles restent les deux tool_failed de ghidra.decompile_function; l’outbox ne fournit pas de raison structurée plus fine. sample.analyze partial (6) et sample.extract_obfuscated_strings partial (5) ne sont pas des échecs codés; l’archive de réponse ne suffit pas à attribuer leur cause sans lire les payloads d’observation.

## Limites et prochaine action sûre

- Les valeurs runtime de UID, binaire monté, TMPDIR, image/seccomp et errno ne sont pas archivées dans les emplacements lus. Le succès manuel de 7z est rapporté, pas reproduit.
- L’assessment canonique, le statut d’invocation et les états needs_review proviennent de réponses API archivées; aucun accès PostgreSQL/MinIO n’a été fait pour revérifier les lignes actuelles.
- Les branches VT/Bridge n’ont pas été auditées causalement et aucun appel ne doit être rejoué sur la base de ce rapport.
- Aucun test, conteneur, échantillon ou provider n’a été exécuté. Les tests cités sont des propositions ciblées seulement.
- L’action de réconciliation, si elle devient nécessaire, est une décision humaine via la route protégée; le correctif de projection permettrait d’abord de rendre les invocations réellement visibles.
