# Diagnostic backend — Mission 07c

Date : 2026-10-30  
Base examinée : `c921848c301407aa8718b833ab35793d3e0bbeca`  
Périmètre : lecture seule du code backend et des journaux/outbox `.autowork-runs/m07c`. Aucun accès à `.env`, `.secrets`, `var` ou aux blobs runtime. Aucun test n’a été exécuté ; aucun test PostgreSQL/Docker n’est proposé.

## 1. `rule.test` absent et workbench désactivé

**Vérifié dans les traces.** L’inventaire `a001-status.json` marque `rule.test`, `rule.propose`, `rule.revise` et `rule.compare` indisponibles avec la raison `rule_workbench_disabled`. Les pages d’outils (`a003`–`a005`) ne montrent que `rule.get` dans cette famille. L’opérateur confirme l’état initial en `operator_log.md:31,864`. L’enveloppe G2 de cette enquête était approuvée ; l’outillage de règles est enregistré comme calcul local, pas comme consultation externe.

**Cause vérifiée dans le code.** Le champ `rule_workbench_enabled` vaut `False` par défaut ([config.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/config.py:422)). Le port runtime n’annonce le workbench que si ce réglage est vrai et qu’un moteur YARA ou worker est disponible ([investigation_runtime_ports.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/infrastructure/investigation_runtime_ports.py:858)). La composition n’enregistre la famille complète que si le réglage, le service workbench, le port corpus et le dépôt de candidats sont présents ; sinon, avec les ports de lecture présents, elle n’enregistre que `rule.get` ([investigation_composition.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_composition.py:1433)). Les outils de test nécessitent aussi les ports candidats, associations, octets du corpus et enregistrement ([investigation_rule_tools.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_rule_tools.py:609)).

**Corpus goodware et sécurité.** L’absence d’échantillons goodware mesurables n’est pas la raison du masquage de `rule.test` : elle limite le statut de validation obtenu après exécution. Le statut pleinement testé exige un goodware réel mesuré ([rule_workbench.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/domain/rule_workbench.py:561)). Il faut donc activer la configuration et fournir les dépendances/ports YARA et corpus ; un corpus goodware réel est nécessaire pour une validation complète, pas pour exposer la famille. Il ne faut pas assouplir G2, TLP, les approbations ou les limites de publication. Les tool definitions disent explicitement qu’aucun statut approuvé ou publié n’est produit par ces outils ([investigation_rule_tools.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_rule_tools.py:682)).

**Diff minimal proposé — configuration de déploiement, à appliquer après vérification du worker isolé et des ports requis :**

```diff
- rule_workbench_enabled = false
+ rule_workbench_enabled = true
```

Aucun changement de code n’est nécessaire pour lever ce flag. Fournir un worker YARA attesté/confiné ou le moteur local isolé déjà supporté, plus le port de corpus et les dépôts nécessaires ; ne pas rendre le flag actif par défaut.

**Tests ciblés à lancer ensuite :** `backend/tests/test_investigation_composition_families.py::test_rule_get_is_registered_without_the_rule_workbench_when_read_ports_exist`, `::test_composition_registers_all_optional_families_with_fake_ports`, et les tests unitaires de `investigation_rule_tools`. Ajouter/adapter un test de composition vérifiant que `rule.test` apparaît avec le flag et les ports requis, et reste indisponible si l’un d’eux manque. Aucun test PG requis.

## 2. Observation « project-content » : GET 404

**Vérifié dans les traces.** `a221-project-observation-review.json` renvoie HTTP 404 avec `operator_observation_not_found`. Le journal situe l’appel pour l’investigation courante (`journal.jsonl:223`) ; le checkpoint confirme que le corps canonique n’a pas été récupéré (`FINAL_CHECKPOINT.md:52`).

**Cause vérifiée dans le code, branche exacte non déterminable avec la réponse seule.** La route commence par vérifier l’investigation, charge ensuite l’observation par UUID et renvoie ce 404 si la ligne manque **ou** si son `investigation_id` ne correspond pas à celui de la route ([api/investigation.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/api/investigation.py:1591)). Le dépôt SQL fait une recherche par UUID seul ([repositories/investigation.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/infrastructure/database/repositories/investigation.py:364)). Ainsi, une référence/UUID présentée comme « project-content » ne garantit pas qu’une observation canonique existe dans cette enquête. Les `response_refs` de l’invocation sont un tableau JSON, sans clé étrangère vers les observations ([models/investigation.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/infrastructure/database/models/investigation.py:78)). La trace 404 ne permet pas de départager une ligne absente d’une observation appartenant à une autre enquête ; attribuer le défaut à un producteur précis serait une inférence.

**Diff minimal proposé — invariant à corriger dans le producteur de la référence, dont le chemin n’est pas identifié par cette trace :**

```diff
- response_refs.append(observation_id)
+ observation = await evidence_observations.get(observation_id)
+ if observation is None or observation.investigation_id != investigation_id:
+     raise ReferenceError("canonical observation missing or out of scope")
+ response_refs.append(str(observation.id))
```

Si le producteur crée l’observation dans la même opération, il doit référencer l’ID renvoyé par cette création et vérifier son enquête. Ne pas élargir la route à une recherche inter-enquêtes et ne pas fabriquer une observation à partir du texte de commande : cela cacherait une rupture de provenance et pourrait révéler une existence hors périmètre.

**Tests ciblés à ajouter/lancer :** tests API de la route vérifiant qu’une observation de la même enquête est lisible, qu’un UUID absent renvoie toujours 404 et qu’une observation d’une autre enquête renvoie également 404. Le test de pagination canonique existant est `backend/tests/test_investigation_api.py::test_operator_observation_route_paginates_sanitized_canonical_json`. Aucun test PG requis pour ces cas unitaires/API avec dépôts simulés.

## 3. `archive.extract_member` sur le ZIP

**Vérifié dans les traces.** `a106-zip-member-project-s7.json` montre que `zip_members` a trouvé un membre correspondant et l’a haché (`archive_status=ok`). L’opération d’extraction `a104-extract-project-s7.json` a échoué : le message exact exposé est `Archive member extraction failed (extract_failed).` et le code d’invocation est `tool_failed`. Une autre opération du même run, `a102-archive-list-s7.json`, retourne `archive_status=7z_unavailable`, zéro membre lu ; le journal note cette séquence (`operator_log.md:612,621-624`). `zip_members` et l’extraction ne suivent pas le même chemin d’implémentation.

**Cause vérifiée et inférence.** Le worker `zip_members` utilise la bibliothèque Python `zipfile` ([archive_commands.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/workers/archive_commands.py:79)). `archive_list` et `archive.extract_member` passent par `/usr/bin/7z` ([archive_commands.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/workers/archive_commands.py:261)) ; l’échec au démarrage est traduit en `7z_unavailable` ([archive_commands.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/workers/archive_commands.py:659)). L’adaptateur applicatif ne lit que `member_status` et, s’il manque, substitue `extract_failed` ([investigation_acquisition_tools.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_acquisition_tools.py:764)). Cela explique exactement l’erreur de surface. L’outbox de `a104` ne contient pas le résultat brut du worker : l’absence de `member_status` masque probablement un `archive_status=7z_unavailable`, corroboré par `a102`, mais une erreur réelle de l’extracteur (par exemple stderr non reconnu, aussi ramené à `extract_failed`) reste possible. La cause basse précise de `a104` n’est donc pas prouvée par les traces disponibles. Le succès de `zip_members` ne prouve pas que 7z sait extraire le membre.

**Diff minimal proposé — propager le statut d’archive lorsque le statut de membre manque, en n’acceptant que les valeurs connues :**

```diff
- status = str(worker_result.get("member_status", "extract_failed"))
+ status = validated_member_status(worker_result.get("member_status"))
+ if status is None:
+     status = validated_archive_status(worker_result.get("archive_status"))
+ if status is None:
+     status = "extract_failed"
```

Cela rendrait visible `7z_unavailable` sans présenter l’extraction comme réussie. Pour extraire, réparer l’image/configuration du worker confiné afin que le binaire attendu soit disponible et attesté, puis vérifier les limites d’exécution ; ne pas extraire hors du worker de confiance.

**Tests ciblés à lancer ensuite :** test unitaire de l’adaptateur où le worker renvoie `archive_status=7z_unavailable` sans `member_status`, qui doit préserver ce motif ; cas d’échec d’extraction avec `member_status=extract_failed` conservant son statut ; tests worker archive existants dans `backend/tests/test_archive_commands.py`. Aucun test PG requis.

## 4. Reparse Bridge : `unsupported_legacy_format`, aucune YARA récupérable

**Vérifié dans les traces.** L’appel archivé est enregistré comme `advisor.consult` avec `consultation_type=yara_draft` (le nom `rule.propose_bridge` figure dans le brief opérateur, pas comme nom d’outil de cette invocation). La réponse normalisée est partielle, sans `rule_source`, et avec avertissements sur l’en-tête wire, la prose ignorée, le type/statut et le bloc source manquants. L’outbox expose une taille de réponse et des références/hash de sortie, mais le texte brut Bridge n’y figure pas ; le champ de texte visible est un résumé généré, pas la réponse. Je ne peux donc pas certifier si le blob brut contenait un bloc clôturé précis sans accéder au stockage runtime, accès hors périmètre.

Le reparse `a147-reparse-yara-source.json` rapporte `advisor_response_schema=v3`, `stored_parser_version=null`, `selected_parser_version=advisor-source-parser.v4`, puis `unsupported_legacy_format`, sans nouveau tour Bridge ni résultat écrit.

**Cause vérifiée dans le code.** La route de reparse valide d’abord la provenance de l’appel archivé, puis refuse tout ce qui n’est pas `advisor-response.v4` avec `advisor-source-parser.v4` ([investigation_advisor_tools.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_advisor_tools.py:4591)). Le refus à `unsupported_legacy_format` survient avant la lecture du blob brut, qui n’intervient qu’après ce garde-fou ([investigation_advisor_tools.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/investigation_advisor_tools.py:4628)). Le parseur courant accepte un JSON uniquement sous schéma v4, sinon passe par le format wire texte ; même cette voie exige les champs `schema`, `type` et `status` ([advisor_semantic_parser.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/advisor_semantic_parser.py:428)). Or Bridge fournit ici du texte/prose, pas du JSON selon le contrat opérationnel. L’extracteur de blocs sait reconnaître un bloc complet ```yara``` et exige exactement un bloc ([advisor_wire.py](/home/nill/perso/work/AutoWork/backend/src/cti_app/application/advisor_wire.py:276)), mais le décodage wire peut rejeter la réponse avant cette extraction. Le format **requis par le chemin actuel** est donc l’enveloppe v4 (ou son wire avec les champs obligatoires) ; le format **archivé observé** est l’enveloppe v3 et aucun numéro de parseur stocké. Le format brut Bridge est annoncé comme prose ; son contenu exact n’est pas récupérable depuis l’outbox examinée.

**Diff minimal proposé — versionner le nouveau comportement ; après validation de la provenance et de l’empreinte, autoriser la relecture locale de v3 et extraire une unique source YARA clôturée dans le texte, sans exiger le wire/JSON :**

```diff
- if schema != V4 or stored_parser != PARSER_V4:
+ if not supported_legacy_v3(schema, stored_parser) and not current_v4(schema, stored_parser):
      return unsupported_legacy_format
  raw_text = read_and_verify_blob(output_sha256)
- response = decode_json_or_required_wire(raw_text)
+ response = decode_json_or_required_wire(raw_text)
+ if response is None and consultation_type == "yara_draft":
+     response = prose_with_exactly_one_complete_fenced_yara_block(raw_text)
```

Le nouveau parseur doit produire un candidat non approuvé/partiel avec provenance vers l’appel et le blob vérifié. Zéro bloc, plusieurs blocs, clôture incomplète ou langage ambigu doit rester sans source récupérable. La source reste une entrée non fiable : pas de validation implicite, approbation ou publication. Ne pas réémettre l’appel Bridge.

**Tests ciblés à lancer ensuite :** mettre à jour `backend/tests/test_advisor_output_reparse.py::test_legacy_v3_reparse_is_auditable_without_blob_read_or_new_observation` pour distinguer « v3 non vérifiable » de « v3 avec provenance/empreinte valides » ; ajouter les cas prose avant/après un unique bloc ```yara```, aucun bloc, deux blocs et clôture incomplète dans `backend/tests/test_advisor_wire.py`. Vérifier la lecture du blob seulement après les contrôles d’identité/provenance/hash, zéro tour Bridge, et qu’aucun statut approuvé/publié n’est produit.

## Synthèse des limites

- Vérifié : disponibilité du workbench commandée par un flag runtime et des dépendances ; 404 de la route pour une ligne absente ou hors enquête ; erreur d’extraction exposée `extract_failed` ; refus v3 avant lecture du blob Bridge.
- Inféré/non déterminé : cause basse exacte de l’échec `a104` faute de payload brut du worker ; absence de ligne versus mauvaise enquête pour le 404 ; contenu brut exact Bridge, non présent dans l’outbox.
- Aucun correctif n’a été appliqué et aucun test n’a été lancé. Les recommandations de test sont locales et sans PostgreSQL/Docker.
