# Mission 04 / L6 — rapport scout

## Verdict

**GO_WITH_BOUNDED_FIX**, pour le parcours de réparse déclenché depuis la composition API en mode `operator_led`. Le contexte écrit pour les sorties récentes `suricata_draft` contient les faits, les refs et leurs SHA nécessaires; le parseur sait traiter ce type. Deux listes d’admission du lecteur de réparse l’excluent encore. Leur extension locale permettrait d’utiliser le chemin existant, sans Bridge ni relecture des observations sources.

La composition API de production enregistre et expose `advisor.reparse` en mode opérateur. Le runner worker standard le laisse absent parce qu’il construit la composition avec `operator_mode=False`. Si le contrat L6 exige aussi son exposition aux exécutions worker ordinaires, la précondition de composition n’est pas satisfaite et cette extension dépasserait le correctif local proposé.

**Référence de checkout :** l’arbre observé est à `HEAD=df9e091076cbfd3147690f184c5562d626d8ae66`, branches `mission04/optional-scouts` et `mission04/implementation`. Le SHA de base fourni (`df9e091007c2cbf3e5d8b2b4a0d9e2b6d8b0f2b0`) ne résout pas dans ce checkout. Les constats ci-dessous s’appliquent au `HEAD` observé; la correspondance exacte avec le SHA fourni reste à confirmer.

## Constats vérifiés

1. **Le type Suricata est prévu par la consultation et par le parseur typé.** `_TYPED_CONSULTATION_TYPES` le contient (`backend/src/cti_app/application/investigation_advisor_tools.py:82-84`); `parse_typed_advisor_response` route `suricata_draft` vers `_parse_suricata_draft`, qui valide les `fact_refs` contre les `fact_id` du contexte et vérifie le SHA de la règle (`backend/src/cti_app/application/advisor_contracts.py:46-89,1646-1725`).

2. **Les faits réseau récents sont précis et référencés.** `_consultation_data` produit chaque fait avec `fact_id`, `evidence_id`, `observation_sha256`, champ et valeur à partir des observations réseau déjà chargées (`backend/src/cti_app/application/investigation_advisor_tools.py:2160-2186`). Une consultation Suricata sans faits est refusée avant transport (`:553-567`).

3. **Le writer persiste le contexte utile.** Après construction du dossier, il place dans `prompt_data` le manifeste, son SHA et sa taille, puis les refs retenues par le manifeste (`backend/src/cti_app/application/investigation_advisor_tools.py:436-443`). `_advisor_reparse_context` copie `network_facts`, `transmitted_refs` et `dossier_manifest` (`:4188-4225`); l’observation d’appel conserve ce contexte, les `input_refs`, `input_sha256`, `output_sha256` et `dossier_sha256` (`:901-934,943,1032-1037`). Le SHA global du dossier est dans le payload d’observation, pas dans le sous-objet `reparse_context`.

4. **Les refs ont des SHA vérifiables.** Les faits réseau fournissent l’ID et le SHA de l’observation; `_typed_transmitted_refs` les projette en refs de type observation, et conserve aussi la ref article avec SHA (`backend/src/cti_app/application/investigation_advisor_tools.py:4453-4505`). Le payload persisté porte aussi le manifeste exact du dossier et son SHA global.

5. **Le lecteur protège l’entrée stockée avant parsing.** Il vérifie le SHA du payload d’observation, le SHA et la taille des octets bruts récupérés par adresse de blob, valide les refs persistées et exige l’égalité entre `reparse_context.transmitted_refs` et les `input_refs` normalisées (`backend/src/cti_app/application/investigation_advisor_tools.py:3455-3463,3516-3555,3557-3614`). Il ne relit pas les observations réseau référencées; il relit le payload de l’appel original et le blob de sortie advisor. Il ne soumet rien à Bridge.

6. **Les enregistrements récents et legacy sont distingués.** Quand le contexte récent est présent, le lecteur l’utilise après vérification des refs (`:3592-3614`). Sans contexte, `_legacy_advisor_reparse_context` ne reconstruit que `figure_plan`, `review_text` et `draft_text`; Suricata retourne `None` (`:4228-4255`). Une fois Suricata admis, le lecteur renverrait donc le refus explicite `advisor_reparse_context_unavailable` (`:3615-3628`), sans inventer de faits. Actuellement, les deux gardes antérieures empêchent d’atteindre ce refus spécifique.

7. **Le lecteur refuse Suricata dans deux listes.** La validation de l’observation autorisée exclut `suricata_draft` (`backend/src/cti_app/application/investigation_advisor_tools.py:3442-3453`, en particulier `:3449`); la validation du payload la réexclut (`:3475-3487`, en particulier `:3481`). Une sortie valide est donc actuellement refusée avant le parseur, avec un code générique d’observation invalide/absente.

8. **L’exposition de production dépend du mode d’exécution.** `build_investigation_composition` enregistre le tool dans la branche `operator_led` (`backend/src/cti_app/application/investigation_composition.py:1489-1491,1529-1538`). Sa capacité figure parmi les capacités opérateur et la projection d’exposition parcourt les tools enregistrés (`:1564-1569`; `backend/src/cti_app/application/investigation_tools.py:310-334`). La route API opérateur passe le mode de l’investigation à cette composition (`backend/src/cti_app/api/investigation.py:1636-1641`). En revanche, le runner worker appelle `composition_factory.build(uow, investigation_id)` sans `operator_mode` (`backend/src/cti_app/application/investigation_jobs.py:69-77`); la valeur par défaut est `False` (`backend/src/cti_app/application/investigation_composition.py:1048-1061`).

9. **Les tests actuels ne prouvent pas le type Suricata ni l’exposition en composition.** `test_advisor_output_reparse.py` construit directement `ReparsePersistedAdvisorOutputTool` avec des faux dépôts et un faux store (`backend/tests/test_advisor_output_reparse.py:66-152`); ses tests couvrent notamment le SHA du blob, l’idempotence, la version de parseur et `bridge_turns_consumed == 0` (`:155-197`). L’audit M2B signale déjà l’absence d’oracle d’enregistrement en composition (`docs/mission03-architecture/_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:27,44`).

## Oracle minimal proposé

Un oracle hors ligne à deux coutures, avec stores et dépôts factices, peut prouver les critères L6 sans PostgreSQL :

1. Dans `backend/tests/test_advisor_output_reparse.py`, ajouter d’abord un cas RED avec une sortie Suricata valide, un `reparse_context` complet (fait `nf001`, ref observation/article, manifeste et SHA), et un faux blob correspondant au SHA persisté. Avant correction, constater le refus par la liste d’admission. Après correction, exiger le parse utile, l’égalité des refs, aucun nouvel appel transport/Bridge, aucun accès aux observations sources pendant le réparse, et un seul enregistrement avec `source_version=ADVISOR_SOURCE_PARSER_VERSION`, SHA de sortie et clé d’idempotence versionnée.

2. Ajouter le cas correspondant sans `reparse_context`: après admission du type, exiger `advisor_reparse_context_unavailable` et zéro écriture dans le recorder.

3. Dans `backend/tests/test_investigation_composition_tools.py`, construire la composition en mode opérateur avec un UoW/store factice et vérifier que `advisor.reparse` est présent dans le registre et dans les tools disponibles. Tester séparément que la composition worker par défaut ne l’expose que si cette différence est bien le contrat retenu.

4. Pour prouver que le writer remplit réellement ce contexte, compléter l’oracle de consultation avec transport factice et observation réseau synthétique: lire le payload enregistré et vérifier `network_facts`, `transmitted_refs`, `dossier_manifest`, les SHA et leur correspondance exacte aux `input_refs`; réutiliser ensuite ce payload dans le test de réparse. Le transport factice sert uniquement à fabriquer l’observation initiale; le compteur ne doit pas changer pendant le réparse.

**Premier oracle RED :** `test_reparse_accepts_verified_suricata_context_without_bridge` dans `tests/test_advisor_output_reparse.py`; le code actuel le refuse à `:3449` avant tout parsing. C’est le bon point de départ puisque le parseur et le writer ont déjà le support requis.

**Correctif de production minimal :** ajouter `suricata_draft` aux deux ensembles de types acceptés autour de `:3449` et `:3481`. Ne pas modifier le writer, le transport Bridge, les lecteurs d’observations sources, ni les autres types. Le chemin legacy renverra déjà un refus explicite si le contexte manque.

**Commandes proposées après le correctif** (non exécutées dans ce scout) :

```sh
make test-backend PYTEST_ARGS="tests/test_advisor_output_reparse.py tests/test_investigation_composition_tools.py -q --tb=short"
```

## Risques, limites et question de contrat

- Le faux store et les faux dépôts ne prouvent pas le comportement du backend MinIO/PostgreSQL en production; aucune preuve live n’est recherchée ici.
- Le lecteur compare le manifeste du contexte via le payload protégé et valide les refs transmises; il ne recalcule pas `dossier_sha256` depuis `dossier_manifest`. Une mutation/reconstruction du payload portant un SHA d’observation cohérent n’est pas simulée par ce test unitaire.
- L’oracle ne prouve pas un appel Bridge live. Il prouve seulement que le réparse réutilise les octets archivés et ne demande pas un autre tour.
- Question de contrat à trancher : `advisor.reparse` doit-il être invocable uniquement depuis le parcours API `operator_led`, ou doit-il aussi apparaître dans la composition worker standard? Le second cas n’est pas satisfait par le code observé et ne doit pas être résolu en changeant le mode worker sans décision explicite.
- Aucun test ni service n’a été lancé pendant ce scout. Le seul fichier créé est ce rapport.
