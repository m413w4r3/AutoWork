# Mission 04 — Phase 2 — Revue du scope L6

**Base inspectée :** `df9e091076cbfd3147690f184c5562d626d8ae66` (`mission04/optional-scouts`).
**Verdict : `REVISE_SCOPE`.**
**Périmètre de cette revue :** lecture seule; aucun test, PostgreSQL, MinIO, provider, Bridge, Docker ou service CTI lancé.

## Décision

Le correctif des deux listes d’admission et le test de composition respectent les limites L6. Le scope proposé doit toutefois ajouter une vérification fail-closed du contexte Suricata incomplet. Le test « contexte manquant » couvre le cas où `reparse_context` est absent, mais pas celui où la map existe sans ses `network_facts` : ce dernier chemin peut actuellement écrire une observation `partial`.

L’exposition du tool au runner worker standard n’est pas un blocker L6. Le plan cible le reparse Suricata et l’enregistrement du tool dans la composition, puis demande un test de composition avec fake; il ne requiert pas de changer le mode worker. Documenter ce comportement comme limite hors scope est approprié. Au niveau source, l’absence est vérifiée; le câblage effectif d’un déploiement reste inconnu.

## Faits vérifiés

- Le plan L6 demande d’aligner les types admis et de vérifier l’enregistrement du tool. Il exige le contexte réseau exact, aucun appel Bridge, aucune relecture d’évidence et une observation versionnée; si le contexte est incomplet, il faut l’exclure plutôt que l’inventer. PostgreSQL réel n’est pas requis; la composition doit être testée avec un fake ([`IMPLEMENTATION_PLAN.md:193-212`](../../mission03-architecture/IMPLEMENTATION_PLAN.md#L193)).
- `suricata_draft` manque dans les deux listes de reparse : admission de `original.object_id` à [investigation_advisor_tools.py:3449](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3449) et validation de `consultation_type` à [investigation_advisor_tools.py:3481](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3481). Le parser typé a déjà une branche Suricata ([advisor_contracts.py:85-86](../../../backend/src/cti_app/application/advisor_contracts.py#L85)) et vérifie les références de faits contre `network_facts` ([advisor_contracts.py:1685-1697](../../../backend/src/cti_app/application/advisor_contracts.py#L1685)).
- Le writer de l’appel advisor inclut déjà `suricata_draft` dans `_advisor_reparse_context` et y copie `transmitted_refs` et `network_facts` ([investigation_advisor_tools.py:4188-4217](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L4188)); le payload persisté inclut ce contexte ([investigation_advisor_tools.py:943,1032-1036](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L943)). Les sorties récentes disposant de ce contexte n’appellent donc pas une nouvelle préparation de preuves.
- Quand `reparse_context` est absent, la reconstruction legacy ne prend pas en charge `suricata_draft` et renvoie `advisor_reparse_context_unavailable` avant l’enregistrement ([investigation_advisor_tools.py:3615-3628](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3615), [4237-4255](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L4237)).
- Quand `reparse_context` est une map présente avec des `transmitted_refs` égaux mais sans faits réseau utiles, le contrôle passe au parser ([investigation_advisor_tools.py:3606-3629](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3606)). Le parser ne peut alors pas valider les `fact_refs` et retourne `None` ([advisor_contracts.py:1685-1697](../../../backend/src/cti_app/application/advisor_contracts.py#L1685)); le reparse construit une sortie partielle puis appelle quand même `record_json` ([investigation_advisor_tools.py:3638-3646](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3638), [3741-3755](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3741)). Ce comportement ne satisfait pas le refus sans écriture demandé pour un contexte incomplet.
- L’observation reparsée conserve le SHA de sortie, les références et la version du parseur; l’idempotence inclut cette version ([investigation_advisor_tools.py:3710-3728](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3710), [3741-3755](../../../backend/src/cti_app/application/investigation_advisor_tools.py#L3741)). Le diagnostic M4 confirme que ce chemin ne rappelle pas Bridge et ne recharge pas les preuves canoniques ([M4_ADVISOR_DIAGNOSIS.md:60-62](../../mission03-architecture/_work/M4_ADVISOR_DIAGNOSIS.md#L60)).
- La composition enregistre `advisor.reparse` seulement dans la branche `operator_led` ([investigation_composition.py:1489-1537](../../../backend/src/cti_app/application/investigation_composition.py#L1489)). La route de contexte operator-led construit explicitement ce mode ([api/investigation.py:1174-1181](../../../backend/src/cti_app/api/investigation.py#L1174)); le runner standard appelle `composition_factory.build` sans l’activer ([investigation_jobs.py:73-76](../../../backend/src/cti_app/application/investigation_jobs.py#L73), valeur par défaut `False` dans [investigation_composition.py:1773-1792](../../../backend/src/cti_app/application/investigation_composition.py#L1773)).
- L’audit de surface M2B indique que le test direct couvre le reparse sans Bridge, mais qu’aucun oracle de composition ne vérifie l’enregistrement du tool ([M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:27](../../mission03-architecture/_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md#L27)). C’est un constat de couverture; le plan L6 reste le contrat de scope.

## Conditions exactes pour accepter le scope révisé

1. Ajouter `suricata_draft` uniquement aux deux ensembles d’admission indiqués. Ne pas modifier le writer initial, le transport Bridge, les lecteurs de preuves, ni les admissions ou comportements des autres types.
2. Ajouter une garde locale au reparse Suricata : accepter seulement un `reparse_context` présent avec `transmitted_refs` identiques aux références normalisées et des `network_facts` complets et non vides permettant la validation des faits. Pour un contexte absent ou incomplet, retourner un refus explicite avant `record_json` et ne rien écrire. Un conflit de références doit aussi rester fail-closed.
3. Tester le chemin valide avec une sortie stockée vérifiée par hash : résultat Suricata utile, références source complètes conservées à l’identique, `fact_refs` résolus dans les faits stockés, aucun transport Bridge, aucune relecture des observations/preuves canoniques, et une seule observation append-only versionnée/idempotente. La lecture de l’observation advisor originale et de son blob de sortie reste nécessaire au reparse; elle ne compte pas comme relecture de preuves sources.
4. Tester les contextes absents et présents mais incomplets : refus explicite et zéro appel au recorder. Garder un cas de références discordantes en refus fail-closed si la garde le couvre.
5. Avec un store fake, tester que la composition `operator_mode=True` expose `advisor.reparse`. Documenter que le runner standard ne l’expose pas dans ce mode; ne pas modifier le mode worker dans L6.

## Tests minimaux et chemins

- `backend/tests/test_advisor_output_reparse.py` : cas Suricata valide; contexte absent et map incomplète; assertions sur le recorder, les références, le SHA/version et l’absence de relecture des preuves sources.
- `backend/tests/test_investigation_composition.py` : composition operator-led avec fakes exposant la définition/capacité `advisor.reparse`.
- Aucun test PostgreSQL, MinIO, provider ou Bridge réel n’est nécessaire pour ce lot. Les fakes valident le seam applicatif; ils ne démontrent pas le comportement des adaptateurs de stockage en production.

## Risques et hors scope

- Sans la garde supplémentaire, un contexte présent mais inutilisable produit une observation `partial` au lieu du refus sans écriture attendu.
- Les `fact_refs` Suricata représentent les faits réseau cités, tandis que les références complètes `(kind, id, sha256)` représentent les entrées transmises. Le test doit prouver la conservation des références complètes et la résolution des faits, sans exiger que ces deux listes de niveaux différents soient égales.
- L’absence d’exposition worker est une limite fonctionnelle connue du code source, pas un blocage de L6. L’exposition par un déploiement réel n’a pas été vérifiée.
- Hors scope : changement de mode worker; support de nouveaux types au-delà de Suricata; refonte du parser; écriture/transport Bridge; rechargement des preuves; DB/MinIO/provider; exécution live, publication ou actions CTI.

## Vérification effectuée

Inspection du plan L6, des références M2B/M4, des admissions/reparse/parser et de la composition API/worker à la base indiquée. Aucun test n’a été exécuté et aucun fichier de production ou de test n’a été modifié dans cette revue.
