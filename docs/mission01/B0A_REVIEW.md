# Revue adversariale M01-B0A

## Portée et verdict

- **TASK_ID** : M01-B0A-REVIEW
- **Cible** : `977468078379cdc90ab7897d80bf0e739968e8f8`, base `058bb4af2278e3941956e523c5a1d560208de228`.
- **Verdict** : **FAIL**.
- **Périmètre** : revue du commit archivé, des contrats Mission 01 et des tests hors ligne. Aucun appel fournisseur, aucune lecture de contenu runtime/secret, aucune modification de code.

Le plafond AutoWork de 96 000 octets a bien été retiré, et les tests dédiés aux trois tailles demandées passent. Toutefois, le run demandé reste rouge (24 échecs), le préflight peut déclarer prête une question qui demande des contradictions pour plusieurs claims alors qu’un seul est couvert, et plusieurs états du manifeste ne correspondent pas à leur définition. Un chemin de projection peut aussi placer un hexdump arbitrairement long dans le prompt; aucun échantillon réel n’a été inspecté, donc l’existence d’une divulgation effective reste inconnue.

## Constats

### F1 — Le lot de tests demandé échoue sur le commit cible

**CONFIRMED / HIGH** — La commande de validation requise collecte 107 cas et se termine avec **24 failed, 83 passed**. Les échecs touchent notamment les refus de préflight, les consultations YARA/pivot/figure, les lots éditoriaux, les erreurs de transport et la préservation d’une sortie au-delà de l’ancien plafond. Certains fixtures peuvent devoir être adaptés au nouveau contrat de preuve; cela ne rend pas le lot vert et aucune analyse dans le commit ne marque ces régressions comme attendues.

Les échecs incluent `test_yara_draft_sends_only_verified_offsets_and_returns_untrusted_text`, `test_pivot_strategy_rejects_a_basis_reference_absent_from_observations`, `test_verified_measurement_candidates_reach_pivot_and_v3_prompts[figure_plan]`, `test_draft_text_batches_units_and_keeps_provenance_per_unit`, `test_draft_text_preserves_bridge_output_above_legacy_limit`, `test_advisor_transport_failures_are_explicit_and_never_retried[...]` (3 paramètres) et `test_suricata_draft_refuses_before_bridge_without_measured_network_facts`.

**Preuve** : commande et résultat ci-dessous; la politique de refus elle-même est appelée avant le transport dans `backend/src/cti_app/application/investigation_advisor_tools.py:274-321`.

### F2 — Une contradiction liée à un seul claim peut satisfaire une demande multi-claim

**PLAUSIBLE / MEDIUM** — `_requested_claim_ids` collecte tous les identifiants cités, mais `_finding_matches_requested_claims` ne demande qu’une intersection non vide (`backend/src/cti_app/application/advisor_dossier.py:1761-1784`). Le préflight cherche ensuite une unique constatation `refuted` (`:1341-1351`). Un cas synthétique hors ligne avec une demande pour `S1P3.1` et `S1P3.2`, les deux passages présents et une seule constatation liée à `S1P3.1`, produit `READY` sans références manquantes.

Le contrat de consultation demande une constatation liée au claim nommé (`BRIDGE_CHAIN_EVAL.md:31,58,65`). Il ne précise pas explicitement la quantification pour une demande qui nomme plusieurs claims; en lecture adversariale, une seule contradiction ne devrait pas certifier les deux demandes. Il manque un test multi-claim.

### F3 — `MISSING` est attribué à une référence facultative sans corps, puis aussi `INTENTIONALLY_OMITTED`

**CONFIRMED / MEDIUM** — Le code attribue `MISSING` à tout corps absent, sans vérifier que la pièce est requise (`backend/src/cti_app/application/advisor_dossier.py:1420-1440`), puis attribue `INTENTIONALLY_OMITTED` à une pièce facultative sans corps (`:1523-1527`). Le cas synthétique observé passe `READY`, n’ajoute aucune référence manquante, mais donne les deux statuts à une référence facultative avec identifiant et SHA seulement.

Cela contredit les définitions : `MISSING` signifie « corps requis absent », tandis que `INTENTIONALLY_OMITTED` est une pièce non requise, avec raison (`BRIDGE_CHAIN_EVAL.md:53-55`). Le statut `RETRIEVABLE` mérite aussi vérification : le booléen `retrieval_available` est déduit du type observation, d’un identifiant et d’un SHA au format valide (`advisor_dossier.py:1067-1071`), sans prouver que le blob peut être lu. Le lecteur de preuve peut échouer ou refuser le descripteur (`investigation_advisor_tools.py:2400-2435`). L’indisponibilité effective du stockage n’a pas été simulée dans le test du manifeste.

### F4 — La sélection lexicale peut classer comme facultative une preuve sémantiquement pertinente

**PLAUSIBLE / MEDIUM** — Les extraits sont sélectionnés par recouvrement lexical de tokens (`advisor_dossier.py:418-425`); le fallback ne s’applique que si aucun candidat de preuve n’a déjà été retenu (`:699-729,792-805`). Ainsi, dès qu’une preuve correspond lexicalement, une autre preuve pertinente par alias, symbole ou relation sémantique peut disparaître du dossier. Le préflight la qualifie ensuite de non requise si elle n’a pas été sélectionnée (`:1026-1047`), et l’omission reçoit une raison qui affirme que la question ne la requiert pas (`:1523-1527`). Le manifeste évite une omission totalement silencieuse, mais son motif n’est pas une vérification de pertinence robuste. Le test actuel ne couvre qu’un exemple clairement sans rapport (`tests/test_advisor_completeness.py:193-231`).

### F5 — Les drapeaux de troncature d’inventaire IOC ne sont pas rendus

**CONFIRMED / MEDIUM** — Le constructeur d’inventaire conserve au plus 64 IOC et 32 768 octets, et renseigne `truncated` en cas de dépassement (`backend/src/cti_app/application/research_context_builder.py:2219-2235`; limites définies dans `backend/src/cti_app/domain/investigation_contracts.py:38-40`). Le renderer Bridge omet explicitement `truncated`, `truncation_counts` et les clés finissant par `_truncated` (`backend/src/cti_app/application/bridge_investigator.py:1512-1525,1561-1578`). Le modèle peut donc recevoir un inventaire partiel sans l’indication que des IOC ont été omis. Ce comportement est antérieur à B0A, mais il reste pertinent à l’affirmation qu’aucun autre plafond n’omet silencieusement un IOC ou un fait requis.

### F6 — Les garde-fous de divulgation précèdent le transport; un hexdump non borné peut toutefois entrer dans le prompt

**CONFIRMED / PLAUSIBLE — HIGH** — Le contrôle `external_llm_allowed`/`do_not_submit`/TLP rouge bloque avant la construction du dossier (`investigation_advisor_tools.py:274-289`; implémentation dans `investigation_service.py:2437-2452`). Les fournisseurs autorisés, TLP et `do_not_submit` sont également vérifiés par l’enveloppe d’outil (`investigation_service.py:2884-2901`). Le nettoyage des secrets opérationnels précède le dossier et l’envoi (`investigation_advisor_tools.py:310-320`), puis la soumission n’a lieu qu’aux lignes `:624-632`. Le redacteur reconnaît les champs opérationnels ainsi que les URL avec identifiants ou paramètres sensibles (`publication_secret_guard.py:118-151,228-283`). Le manifeste contient des références, tailles et SHA, pas les corps (`advisor_dossier.py:847-865`).

En revanche, `extract_content_excerpts` copie les chaînes sous `hex`, `hex_bytes`, `bytes_hex` ou `hexdump` sans limite de taille (`advisor_dossier.py:174-185`) et les extraits sélectionnés sont cités dans le dossier (`:418-423`); le prompt complet est archivé puis soumis (`investigation_advisor_tools.py:599-632`). Un champ de mesure courte et vérifiée peut être nécessaire pour YARA, conformément au contrat. Mais le chemin générique ne démontre pas qu’un hexdump complet/raw est rejeté ou borné. **Aucun contenu canonique réel n’a été lu; une divulgation effective de bytes d’échantillon est UNKNOWN.** Ajouter un cas synthétique qui vérifie le comportement sur un champ hexdump complet et sur une mesure de signature est nécessaire pour fermer ce point.

### F7 — Le test >200 000 octets prouve l’intégrité vers un faux transport, pas le comportement Bridge réel

**CONFIRMED / LOW** — Le test vérifie le SHA et la taille du corps, construit un prompt de plus de 200 000 octets, choisit localement l’étiquette `text_file`, puis transmet le prompt intact à `ScriptedTurnTransport` (`tests/test_advisor_completeness.py:271-328`). C’est une preuve déterministe utile pour l’absence de découpage dans ce chemin simulé. Le test ne traverse pas `InvestigationAdvisorTool.execute` ni une interface fichier Bridge; le commentaire d’évaluation précise que `text_file` est une simulation, sans appel ni reçu réel (`BRIDGE_CHAIN_EVAL.md:67`). La livraison/réception réelle d’un tel dossier est **UNKNOWN** et n’est pas revendiquée ici.

### F8 — Pas de réimplémentation du protocole Bridge; deux wrappers restent des no-op trompeurs

**CONFIRMED / LOW** — Le patch ne touche ni migrations, ni service/repository Bridge, ni pagination, chunking, API de récupération, reçu fournisseur ou accusé de lecture. Le reparse relit une sortie persistée sans nouvel appel et inscrit `bridge_turns_consumed: 0` (`investigation_advisor_tools.py:3390`; test `tests/test_advisor_output_reparse.py:155-179`). La logique de lots éditoriaux demeure dans l’application existante.

Amélioration de maintenabilité : `_compact_advisor_data` copie les données sans compaction et `_fit_advisor_prompt` ignore `max_input_bytes` (`investigation_advisor_tools.py:3491-3509`). Renommer/supprimer ces wrappers ou leur donner un contrat effectif éviterait de suggérer qu’un plafonnement/ajustement se produit. La politique de consultation est aussi répartie entre la sélection, le préflight et la détection de troncature critique.

## Vérification du plafond et des tests d’acceptation

- Le diff retire le plafond/rejet AutoWork de 96 000 octets du renderer `bridge_led`; le renderer retourne le prompt directement (`backend/src/cti_app/application/bridge_investigator.py:1275-1428`). Les tailles exactes 95 999, 96 000 et 96 001 sont produites par test et passent (`tests/test_bridge_investigator.py:162-210`). Aucun chunking ni nouvelle API n’a été ajouté.
- Les six tests de `test_advisor_completeness.py` et le test ciblé du renderer passent : **7 passed**. Ils couvrent les références manquantes/récupérées, contradiction simple, omission avec motif, absence de faux statut envoyé avant soumission, seuil et intégrité >200k. Ils ne couvrent pas les statuts facultatifs sans corps, l’accès blob indisponible, les contradictions multi-claim, les erreurs de sélection sémantique ni les bytes/hexdumps complets.
- `test_reparse_appends_verified_parse_without_a_bridge_transport` passe dans le test ciblé de reparse : **4 passed** pour le fichier, dont vérification qu’un seul appel de persistance est fait et `bridge_turns_consumed == 0`.
- Ruff ciblé sur les neuf fichiers Python modifiés : **All checks passed**.
- L’intégration simulée reste hors ligne; ces résultats ne prouvent aucun transport réel.

## Commandes et résultats

Toutes les commandes utilisent le Python de l’environnement virtuel du dépôt (et retirent les variables Conda actives), jamais `uv run` ni pytest nu. Le commit a été testé depuis une archive dans `/tmp`, avec liens vers le `.venv` et `.autowork-runs` demandés; aucun contenu runtime n’a été consulté.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py -q --tb=short
24 failed, 83 passed

PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py::test_rendered_prompt_has_no_autowork_96000_byte_ceiling -q --tb=short
7 passed

PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_output_reparse.py -q --tb=short
4 passed

PYTHONPATH=$PWD/src .venv/bin/python -m ruff check <neuf fichiers Python modifiés>
All checks passed!
```

Le registre indique 17 tests dans `docs/mission01/TASKS.md:38`; le run demandé collecte 107 tests au total. Le test « text_file » du dossier >200k est explicitement simulé, et le reparse est testé séparément. Les commandes ciblées ne neutralisent pas les 24 échecs du lot requis.

## UNKNOWN et suite requise

- **UNKNOWN** : réception réelle par Bridge d’un prompt >200 000 octets et preuve d’un reçu fichier; aucune interface réelle n’a été invoquée, conformément au périmètre.
- **UNKNOWN** : présence d’un hexdump intégral ou de bytes d’échantillon dans les artefacts canoniques; ils n’ont pas été ouverts. Le code source autorise néanmoins la projection sans borne du champ.
- **UNKNOWN** : accessibilité réelle des blobs associés aux références utilisées dans les données runtime; aucune donnée runtime n’a été consultée.
- Pour lever le verdict : corriger ou expliquer les 24 échecs, rendre les états MISSING/RETRIEVABLE/INTENTIONALLY_OMITTED mutuellement cohérents, préciser le contrat multi-claim, et ajouter les tests négatifs ci-dessus. Une validation réelle du transport nécessiterait une autorisation distincte et n’est pas requise pour cette revue locale.

## Coût

**UNKNOWN** — aucun coût de tokens/fournisseur disponible.

## Re-revue après fbc0f24

- **Cible** : `fbc0f241408a649f0024464a07d2ee4b6a528fe1`; delta examiné `977468078379cdc90ab7897d80bf0e739968e8f8..fbc0f241408a649f0024464a07d2ee4b6a528fe1`.
- **Verdict à la clôture R2** : **FAIL**. Les corrections F1–F4 sont confirmées et les drapeaux de troncature F5 sont rendus. F6 reste un défaut HIGH : un canal séparé de mesures hex legacy laisse passer des valeurs sans borne.

| Constat | Reclassification R2 | Preuve et résultat |
|---|---|---|
| F1 — suite rouge | **CONFIRMED, corrigé / HIGH** | Le run demandé donne **116 passed**. La table `BRIDGE_CHAIN_EVAL.md:87-100` couvre exactement les 24 échecs initiaux, avec causes et actions cohérentes. Le diff ne supprime pas simplement des assertions : le compte `present` monte de 1 à 2 après ajout d’un passage requis; le motif lexical est corrigé; le test Suricata remplace l’ancien résultat `FAILED/SURICATA_NOT_JUSTIFIED` par le contrat désormais exigé : blocage `INSUFFICIENT_EVIDENCE` au préflight, référence exacte, zéro tour et zéro transport (`BRIDGE_CHAIN_EVAL.md:36,58,99`; `tests/test_advisor_completeness.py:48-76,366-405`; `tests/test_investigation_advisor_tools.py:4199-4218`). Ce test ne couvre donc plus le chemin de refus Suricata en aval, mais la condition absente est maintenant arrêtée plus tôt, comme le demande le contrat. Les autres assertions de rejet, normalisation, hash, parsing et absence de retry restent actives. Aucun test n’est désactivé ou marqué skip. |
| F2 — contradiction multi-claim | **CONFIRMED, corrigé / MEDIUM** | Le préflight produit une référence distincte par claim non couvert (`advisor_dossier.py:1341-1387`). Avec `S1P3.1` et `S1P3.2`, une seule constatation liée à `S1P3.1` bloque sur `S1P3.2`; l’ajout de la seconde constatation donne `READY`. Test `test_multi_claim_contradiction_requires_each_named_claim` (`tests/test_advisor_completeness.py:194-243`), inclus dans les 116 succès. |
| F3 — états MISSING/RETRIEVABLE | **CONFIRMED, corrigé / MEDIUM** | `MISSING` n’est ajouté que si l’élément est requis et sans corps (`advisor_dossier.py:1430-1450`). Une observation facultative sans corps est seulement `INTENTIONALLY_OMITTED` (`:1558-1575`). `RETRIEVABLE` exige maintenant `blob_read_verified is True` (`:1069-1076`); un échec de lecture devient `NOT_VERIFIED`, `TECHNICAL_BLOCKED` et aucun transport n’est appelé (`:1507-1548`, `:1583-1594`). Tests d’absence facultative et d’échec de lecture (`tests/test_advisor_completeness.py:246-318`) ainsi que le test d’échec simulé du lecteur canonique (`tests/test_investigation_advisor_tools.py:1786-1832`) passent. |
| F4 — pertinence lexicale | **CONFIRMED, corrigé / MEDIUM** | Une preuve présente mais écartée par la sélection bornée reçoit `NOT_VERIFIED` et `INTENTIONALLY_OMITTED`; le motif indique expressément que la pertinence sémantique n’a pas été vérifiée (`advisor_dossier.py:1492-1506`). Le cas `CreateMutexW`/alias non lexical le teste et vérifie que le motif ne déclare pas la preuve sans pertinence (`tests/test_advisor_completeness.py:320-365`). |
| F5 — troncature IOC | **CONFIRMED pour les drapeaux; UNKNOWN pour un compte IOC / LOW** | Le renderer ne supprime plus `truncated`, `truncation_counts` ni les clés `*_truncated` (`bridge_investigator.py:1512-1578`). Le test voit l’état tronqué de l’inventaire, l’index tronqué et le compte d’éléments d’index omis (`tests/test_bridge_investigator.py:213-234`). Mais `IndicatorInventoryV1` expose seulement `items` et `truncated` (`investigation_contracts.py:193-198`); `_indicator_inventory` retourne un booléen sans nombre d’IOC supprimés (`research_context_builder.py:2219-2235`). Le test ne prouve donc pas l’envoi d’un compte numérique d’IOC omis; le contrat ne le fournit actuellement pas. |
| F6 — bytes/hexdump | **CONFIRMED, non corrigé / HIGH** | Le filtre générique bloque les champs nommés `hex`/`hexdump`, bytes et lignes hexdump avec offsets (`advisor_dossier.py:120-128,156-213`); le MD5 décodé et le xxd vérifié court sont préservés (`tests/test_advisor_completeness.py:408-433`, `tests/test_investigation_advisor_tools.py:1740-1782`). Cependant, le chemin legacy `_legacy_measurement_rows` accepte toute valeur hex syntaxiquement valide sans longueur maximale (`investigation_advisor_tools.py:2482-2536,5492-5498`); `verified_measurements` peut l’ajouter après association SHA (`investigation_advisor_tools.py:1637-1685`), puis le dossier place sa valeur entière dans le corps de la mesure (`advisor_dossier.py:545-601,847-868`). Oracle synthétique hors ligne : une valeur `41` répétée 262 144 fois produit une mesure de **524 288 caractères** et un prompt de **525 455 octets** contenant la valeur complète; cette taille reste sous la limite générale par défaut de 4 000 000 octets (`investigation_advisor_tools.py:85`, `config.py:110`). Un autre oracle avec `{"content": "41" * 16384}` produit un extrait de 32 768 caractères : la détection générique des seuls hexdumps avec offsets ne couvre pas non plus ce cas. Ces données sont synthétiques; aucun échantillon réel n’a été lu. La correction F6 ne ferme donc pas l’interdiction de transmettre des bytes bruts. |
| F7/F8 — Bridge et wrappers | **CONFIRMED / LOW** | La documentation dit explicitement que `text_file` reste une simulation et qu’aucun reçu Bridge réel n’est prouvé (`BRIDGE_CHAIN_EVAL.md:67,81-83`). Le delta ajoute seulement des ajustements application/tests/docs; aucun dépôt Bridge, migration, pagination, chunking, API de récupération, accusé de lecture ou reçu fournisseur. Les wrappers `_compact_advisor_data` et `_fit_advisor_prompt` sont toujours des no-op, mais sont maintenant documentés comme compatibilité sans fonction de transport/compactage (`BRIDGE_CHAIN_EVAL.md:101`; source `investigation_advisor_tools.py:3512-3530`). |

### Vérifications R2

Le commit a été archivé dans `/tmp`, avec les liens `.venv` et `.autowork-runs` demandés. Les commandes ont utilisé exclusivement le Python du venv, avec les variables Conda retirées.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py -q --tb=short
116 passed in 1.90s

PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_output_reparse.py -q --tb=short
4 passed in 0.50s

PYTHONPATH=$PWD/src .venv/bin/python -m ruff check <six fichiers Python modifiés>
All checks passed!
```

Les tests ciblés de F2–F6 sont inclus dans les 116; l’oracle F6 de 524 288 caractères a été exécuté séparément, sans transport. Le test F5 couvre les drapeaux et le nombre tronqué d’éléments de l’index, pas le nombre d’IOC. Le reparse reste sans nouvel appel, selon le test dédié et `bridge_turns_consumed == 0`.

### Portée, sécurité et inconnues R2

Le delta contient seulement les trois modules applicatifs B0A, trois fichiers de tests et `BRIDGE_CHAIN_EVAL.md`; aucun fichier runtime/secret, migration ou changement hors périmètre ne figure dans `git diff --name-status`. Les gates `external_llm_allowed`, `do_not_submit` et TLP rouge précèdent toujours la construction/soumission (`investigation_advisor_tools.py:274-289`; `investigation_service.py:2437-2452`); la frontière provider de l’enveloppe est inchangée (`investigation_service.py:2884-2901`). Le masque des secrets précède le transport (`investigation_advisor_tools.py:310-320,630-639`). Aucun transport réel n’a été appelé.

- **UNKNOWN** : présence effective de bytes/hexdumps complets dans des observations canoniques runtime; aucune donnée runtime n’a été lue. Le code permet néanmoins de les faire passer via les mesures hex legacy.
- **UNKNOWN** : comportement réel de Bridge pour un dossier >200 000 octets, et existence d’un compteur IOC omis exigé en aval.
- **UNKNOWN** : coût de tokens/fournisseur.

**Action requise avant PASS** : borner ou refuser les valeurs hex dans `_legacy_measurement_rows`, avec un test qui suit cette mesure jusqu’au dossier/prompt; préciser si un compte numérique d’IOC omis est exigé et l’ajouter au contrat/test si oui.

## Re-revue après c561f04

- **Cible** : `c561f04e5eaa29869ff53ec2372f7d7872c3a334`; delta examiné `fbc0f241408a649f0024464a07d2ee4b6a528fe1..c561f04e5eaa29869ff53ec2372f7d7872c3a334`.
- **Verdict final** : **FAIL**. Les trois types de mesures nommés sont maintenant bornés et leurs tests de refus sont solides, mais une clé neutre et le texte base64 générique contournent encore F6. Les deux chemins ont été suivis jusqu’au prompt avec le transport factice, sans appel fournisseur.
- **Portée** : le delta touche uniquement `backend/src/cti_app/application/advisor_dossier.py`, `backend/src/cti_app/application/investigation_advisor_tools.py` et leurs deux fichiers de tests. Aucun fichier hors B0A, aucune migration, aucun changement Bridge/provider/service.

| Constat | Statut / sévérité R3 | Preuve et résultat |
|---|---|---|
| F1–F4 — corrections précédentes | **CONFIRMED / MEDIUM, toujours fermés** | Le delta R3 ne touche pas leurs règles. La suite combinée compte 133 succès, dont les cas de couverture multi-claim, blob indisponible, omission honnête et reparse. Aucun changement observé ne réintroduit les défauts R2. |
| F5 — drapeaux et compte IOC omis | **CONFIRMED pour les drapeaux; UNKNOWN / LOW pour le compte IOC exact** | Le renderer et son test exposent toujours `truncated`, `truncation_counts` et le compte d’éléments de l’index omis (`bridge_investigator.py:1512-1579`; `tests/test_bridge_investigator.py:213-234`). Le contrat amont ne fournit toujours pas le nombre exact d’IOC supprimés, comme documenté en R2. C’est accepté comme limite non bloquante, sans compte inventé ni élargissement au-delà de B0A. |
| F6a — mesures hex/base64 nommées >512 octets | **CONFIRMED / corrigé** | Les mesures `hex`, `hex_bytes` et `base64` sont rejetées avec `binary_measurement_exceeds_512_byte_limit` avant la construction du dossier (`investigation_advisor_tools.py:2528-2545,5522-5534`). Les tests couvrent ces champs sur trois types de consultation, vérifient l’absence de la valeur et du dossier dans le résultat, et zéro appel au transport factice (`tests/test_investigation_advisor_tools.py:1785-1873`). Le xxd court et le MD5 décodé sont conservés : les tests existants les vérifient respectivement à l’étape mesure et à l’étape projection (`tests/test_investigation_advisor_tools.py:1740-1782`; `tests/test_advisor_completeness.py:445-451`). Oracle synthétique indépendant via `execute`/transport factice : chaque valeur courte apparaît bien dans le prompt. |
| F6b — plafond générique contourné par clés neutres/listes | **CONFIRMED / HIGH, non corrigé** | `add()` refuse une chaîne individuelle de plus de 16 384 octets (`advisor_dossier.py:134-141`), mais l’agrégat n’est compté que par `add_content()` (`:175-181`). Une liste sous clé neutre conserve `content_field=None` et appelle `add()` élément par élément (`:252-284`); deux chaînes de 15 907 octets passent donc séparément. Oracle synthétique via `extract_content_excerpts` puis `build_advisor_dossier` : 31 814 octets d’extraits sont retenus. Oracle `execute` avec une observation factice et le transport factice : **33 083 octets de prompt**, les deux valeurs présentes, un appel transport. Le test ajouté ne couvre que la liste sous `content` (`tests/test_investigation_advisor_tools.py:1910-1944`), pas une clé neutre ou une liste imbriquée neutre. Une valeur hex compacte de 600 octets sous clé neutre est également admise par la projection, car la regex ne reconnaît que les lignes hexdump avec offset (`advisor_dossier.py:138-139`). |
| F6c — contenu base64 non typé | **CONFIRMED / HIGH, non corrigé** | Les chemins génériques ne décodent ni ne bornent la taille binaire encodée. Un `content` base64 représentant 600 octets et une entrée `strings:[{value, encoding:"base64"}]` traversent tous deux `execute`; dans les deux cas le prompt factice contient l’encodage complet et un appel transport a lieu. Le champ `encoding` ne déclenche aucune validation dans le chemin legacy des chaînes (`investigation_advisor_tools.py:2505-2545`); le texte générique reste sous le plafond par chaîne (`advisor_dossier.py:232-250`). Ces valeurs sont entièrement synthétiques; aucun provider ni échantillon réel n’a été utilisé. |
| Valeurs numériques | **REJECTED / LOW** | Le parcours ignore les scalaires numériques et les listes d’entiers ne génèrent aucun extrait (`advisor_dossier.py:258-289`). L’oracle `{"content": [65] * 20_000}` produit zéro extrait. |
| F7/F8 — transport, protocole et scope | **CONFIRMED / LOW, aucun changement** | Le patch n’ajoute ni persistance de run, pagination, récupération, chunking, accusé de lecture ou reçu fournisseur. Les appels observés sont uniquement ceux de transports factices dans des tests/oracles hors ligne. Le diff se limite aux quatre fichiers listés ci-dessus; aucune migration ni modification des services Bridge. Les gates `external_llm_allowed`, `do_not_submit`, TLP rouge et provider restent avant le transport (`investigation_advisor_tools.py:278-293`; `investigation_service.py:2437-2452,2884-2901`). |

### Vérifications R3

Le commit `c561f04` a été archivé dans `/tmp/autowork-m01-b0a-review-c561f04`; les liens `.venv` et `.autowork-runs` prescrits ont été créés. Les variables Conda ont été retirées et les commandes ont utilisé le Python du venv.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py tests/test_advisor_output_reparse.py -q --tb=short
133 passed in 1.99s

PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_output_reparse.py -q --tb=short
4 passed in 0.52s

PYTHONPATH=$PWD/src .venv/bin/python -m ruff check --cache-dir /tmp/ruff-m01-b0a-c561 src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py
All checks passed!
```

Les tests de rejet F6 vérifient les résultats sans fuite de la valeur ni appel au transport. Les oracles synthétiques additionnels ont démontré une soumission au **transport simulé** de la liste neutre et des valeurs base64; ils ne prouvent aucune livraison réelle à Bridge. Aucun test ni oracle n’a utilisé réseau, PostgreSQL, Docker, fournisseur, donnée canonique réelle ou échantillon réel.

### Constats restants et UNKNOWN R3

- **HIGH / CONFIRMED** : F6 reste ouvert pour les listes sous clé neutre et les encodages base64 intégrés au contenu générique ou au chemin legacy `strings`. La limite par item ne couvre pas le total injecté dans le prompt, et une valeur encodée peut dépasser 512 octets binaires tout en restant sous 16 384 octets texte.
- **LOW / UNKNOWN non bloquant** : le producteur ne fournit pas le compte exact d’IOC omis; les drapeaux et comptes disponibles sont rendus.
- **UNKNOWN** : réception par un fournisseur réel, présence de tels contenus dans des observations runtime canoniques, et coût tokens/fournisseur. Aucune donnée runtime ni service externe n’a été consulté.

**Verdict final R3 : FAIL** — ne pas considérer F6 fermé avant qu’une borne cumulative et une règle pour les représentations encodées soient appliquées à tous les chemins d’extraction avant dossier/prompt, avec des tests de liste neutre imbriquée et de base64 générique vérifiant zéro transport.

## Re-revue finale après df1bd8f

- **TASK_ID** : M01-B0A-REVIEW-R4.
- **Cible** : `df1bd8f3d5f60ea10b645eb6614c9d4f0a891f9e`; delta `c561f04e5eaa29869ff53ec2372f7d7872c3a334..df1bd8f3d5f60ea10b645eb6614c9d4f0a891f9e`.
- **Verdict** : **FAIL** — F6b est fermé et le base64 pur est borné, mais une représentation mixte (data URL ou texte autour du payload) atteint encore le transport simulé.
- **Portée** : lecture seule du code et tests hors ligne. Le diff examiné touche les deux modules advisor et leurs tests. Aucun fournisseur, Bridge réel, donnée runtime, PostgreSQL ou échantillon n’a été consulté.

### Revue des standards

Aucun écart aux règles de code documentées dans `AGENTS.md` et `backend/AGENTS.md` n’a été relevé. Ruff et `git diff --check` sont propres.

### Vérification des constats

| Constat | Résultat final | Preuve |
|---|---|---|
| F6b — budget cumulatif des extraits | **CORRIGÉ** | L’extraction additionne les octets rendus, titres compris, à travers les clés et listes d’une observation (`advisor_dossier.py:159-195`); le dossier vérifie aussi le total des observations sélectionnées (`:888-894`). Les cas de deux chaînes de 15 907 octets sous clé neutre, dont une liste imbriquée dans un objet neutre, échouent avant le dossier : valeurs absentes du résultat, omission explicitée, `dossier_text` absent et zéro appel du transport factice (`test_investigation_advisor_tools.py:1957-2001`). |
| F6c — base64 pur >512 octets | **CORRIGÉ pour les valeurs entières** | La projection rejette le base64 pur avec ou sans padding ajouté (`advisor_dossier.py:48-67`). Les tests couvrent `content` et `strings[].value`, 600 octets décodés (forme canonique sans `=` car 600 est divisible par 3) et 514 octets sans padding. Ils vérifient l’omission explicite, l’absence de la valeur et du dossier, et zéro transport (`test_investigation_advisor_tools.py:2015-2062`). L’objet neutre avec une liste imbriquée et le contenu sous `content` imbriqué sont également rejetés par l’oracle de projection synthétique. |
| F6c — base64 mélangé à du texte ou à une data URL | **CONFIRMÉ / HIGH, encore ouvert** | Le détecteur vérifie que toute la chaîne (après suppression des espaces) est dans l’alphabet base64 (`advisor_dossier.py:54-65`). Avec une ponctuation comme `data:application/octet-stream;base64,`, il retourne sans rejet et l’extrait passe dans le dossier. Oracle `execute` avec observation synthétique explicitement référencée et transport factice : une valeur base64 de 600 octets dans une data URL a été incluse intégralement dans le prompt; **1 appel** au transport factice. La réponse `partial` provient de la réponse factice `unused`; aucune requête fournisseur réelle n’a eu lieu. |
| F1–F4 — régressions | **Aucune régression observée** | Le delta ne modifie pas les règles correspondantes et les quatre fichiers demandés passent ensemble, dont les cas de complétude, de blob, d’omission et de reparse. |
| F5 — nombre exact d’IOC omis | **UNKNOWN, accepté et non bloquant** | Le delta ne modifie pas le contrat d’inventaire ni le renderer Bridge et n’invente aucun compte. Les drapeaux et comptes effectivement fournis restent couverts par le test Bridge existant. |
| Dossier >200 000 octets et seuil Bridge | **SIMULATION INTACTE** | `test_large_dossier_crosses_bridge_file_threshold_without_omission` conserve un dossier d’article supérieur à 200 000 octets, choisit le mode simulé `text_file` et vérifie que le transport scripté reçoit le prompt entier (`test_advisor_completeness.py:521-565`). Cela ne prouve pas le comportement d’un Bridge réel. |
| Extraits courts | **PRÉSERVÉS** | Le test garde le xxd vérifié court comme mesure (`test_investigation_advisor_tools.py:1740-1782`), le MD5 décodé comme IOC textuel (`test_advisor_completeness.py:474-480`) et le test d’extrait générique court (`:436-443`). |

Point secondaire de ressources : le décodeur alloue les octets décodés avant de tester le seuil de 512 octets (`advisor_dossier.py:59-67`). La lecture canonique est bornée par `model_output_max_bytes` (512 MiB par défaut; `investigation_advisor_tools.py:2474-2480`), mais une valeur base64 entière très grande peut encore produire une allocation transitoire proportionnelle avant le refus. Ce point n’explique pas le contournement de transport ci-dessus.

### Vérifications R4

Le commit cible a été archivé sous `/tmp`, avec le lien `.venv` demandé; les variables Conda ont été retirées.

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py tests/test_advisor_output_reparse.py -q --tb=short
141 passed in 2.08s

PYTHONPATH=$PWD/src .venv/bin/python -m ruff check src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py
All checks passed!

git diff --check c561f04..df1bd8f
clean
```

L’oracle du cas mixte et le scénario neutre imbriqué utilisent uniquement des données synthétiques et un transport factice, sans effet externe.

### UNKNOWN R4 et coût

- **UNKNOWN** : le nombre exact d’IOC omis par la chaîne de production; aucun compte n’est fabriqué.
- **UNKNOWN** : livraison réelle d’un dossier >200 000 octets, présence de ce type de contenu dans les données runtime canoniques et coût éventuel de tokens/fournisseur.
- **Action avant PASS** : reconnaître et borner/refuser le base64 incorporé dans les chaînes mixtes, puis vérifier par un test explicite que l’observation est omise et que le transport reste à zéro appel.

**Coût : UNKNOWN** — aucun coût fournisseur n’est observable dans cette revue hors ligne.

**Verdict final R4 : FAIL** — F6b est corrigé et les cas base64 purs testés sont refusés, mais F6c reste contournable avec du base64 mélangé à du texte.

## Re-revue après cd69eca

- **TASK_ID** : M01-B0A-REVIEW-R5.
- **Cible** : `cd69eca29c0e2ab905254654d9a488e28422aab4`; delta `df1bd8f3d5f60ea10b645eb6614c9d4f0a891f9e..cd69eca29c0e2ab905254654d9a488e28422aab4`.
- **Verdict** : **FAIL** — les data URLs `;base64,` de forme reconnue sont maintenant bornées avant décodage, mais les métadonnées de titre, les data URLs sans `;base64` et le fractionnement entre champs laissent encore du contenu de plus de 512 octets dans le dossier.
- **Portée** : lecture du delta et oracle synthétique hors ligne. Aucun échantillon, runtime, fournisseur ni Bridge réel n’a été consulté. Le checkout de revue reste sur `ca78ded`; les tests ont été lancés sur une archive temporaire exacte de `cd69eca`.

### Vérification des constats R5

| Constat | Résultat | Preuve |
|---|---|---|
| F6c — data URL Base64 standard >512 octets, champs imbriqués | **CORRIGÉ pour la forme reconnue** | `_DATA_URL_BASE64` reconnaît `data:…;base64,`; le scanner compte les caractères Base64 décodés depuis les caractères encodés avant tout appel au décodeur (`advisor_dossier.py:33-34,57-118`). Le spy du décodeur échoue si un 600-octets atteint `b64decode`, pour la valeur pure, la data URL canonique, le payload percent-encodé, LF, espaces et tabulations (`test_advisor_completeness.py:463-503`). L’oracle d’exécution couvre `content` imbriqué et `strings[].value` imbriqué; la valeur complète est absente du résultat, `dossier_text` est absent et le transport factice a zéro appel (`test_investigation_advisor_tools.py:2064-2132`). L’omission est explicite via `data_url_base64_exceeds_512_byte_decoded_limit` (`investigation_advisor_tools.py:1144-1152`). |
| Base64 URL-safe et padding | **CORRIGÉ par l’oracle** | Le scanner accepte `-_` et calcule le seuil avant le décodage. Oracle sur la source exacte : 600 octets URL-safe, et 601/602 octets standard ou URL-safe avec padding et sans padding, sont tous refusés avec le code de dépassement. Ces variantes ne sont pas toutes des tests automatisés. |
| F6c — URL de 600 octets dans un titre (`strings[].name`) | **CONFIRMÉ / HIGH** | `add()` vérifie le contenu `value`, mais `display_name()` incorpore `name` dans le titre sans appliquer le contrôle; le titre rendu devient une partie de l’extrait (`advisor_dossier.py:260-265,284-303,350-368`). Oracle synthétique `extract_content_excerpts` puis `build_advisor_dossier` : la data URL complète de 600 octets apparaît dans `dossier.text`. Cela viole directement l’exigence d’absence de la valeur dans le dossier/prompt. |
| Data URL sans `;base64` | **CONFIRMÉ / MEDIUM** | `data:application/octet-stream,` suivi de `%41` répété 600 fois n’est pas reconnu par le seul motif `;base64,`; l’extrait de 1 830 caractères et l’URL complète restent dans le dossier (oracle synthétique). Une URI de données peut donc porter plus de 512 octets sans omission explicite. |
| Valeur Base64 fractionnée entre champs / plusieurs URL | **CONFIRMÉ / MEDIUM; portée agrégée UNKNOWN** | Les compteurs sont réinitialisés pour chaque correspondance et chaque appel à `add()` (`advisor_dossier.py:57-63,260-265`). L’oracle fractionne un Base64 valide de 600 octets en deux champs de 400 caractères; chaque champ passe et les deux parties apparaissent dans le dossier. Deux URL de 300 octets dans un même champ passent aussi séparément. Le contrat observé plafonne une valeur individuelle; l’intention d’un plafond agrégé sur les parties reconstituables n’est pas définie ici. |
| Double encodage percent | **ACCEPTÉ par la projection; UNKNOWN en aval** | Un payload dont chaque octet Base64 est doublement percent-encodé est conservé comme texte. Le scanner ne fait qu’un décodage percent à la fois (`advisor_dossier.py:81-104`); je n’ai pas trouvé de second décodage dans le chemin applicatif examiné. La présence de ce texte dans le prompt est vérifiée par l’oracle, mais son interprétation comme 600 octets décodés par le consommateur reste UNKNOWN. |
| Clé inconnue et listes imbriquées | **Pas de fuite pour un scalaire inconnu; structures parcourues** | Un scalaire direct sous `payload` ne produit aucun extrait. Les mappings/listes imbriqués sont parcourus, et leurs descendants `content`/`value` passent par le contrôle (`advisor_dossier.py:305-372`; test imbriqué ci-dessus). Le champ `name` constitue toutefois la voie de fuite décrite plus haut. |
| Budget 16 384, base64 pur, hexdump/xxd/MD5 courts | **PRÉSERVÉS** | Le budget d’extraits reste cumulatif et les cas >16 384 octets échouent (`advisor_dossier.py:260-296`; `test_advisor_completeness.py:505-530`; `test_investigation_advisor_tools.py:1900-2001`). Le base64 pur >512 reste refusé avant le prompt (`test_investigation_advisor_tools.py:2015-2062`). Les hexdumps sont omis; le xxd vérifié court reste une mesure et le MD5 décodé reste un IOC textuel (`test_advisor_completeness.py:411-435,533-539`; `test_investigation_advisor_tools.py:1740-1782`). |
| Dossier >200 000 octets et seuil Bridge | **SIMULATION INTACTE** | Le test conserve un dossier supérieur à 200 000 octets, choisit le mode simulé `text_file` et vérifie la réception complète par le transport scripté (`test_advisor_completeness.py:580-624`). Aucun Bridge réel n’a été sollicité. |
| F1–F4, F5, G2/TLP/provider | **F1–F4 sans régression; F5 UNKNOWN non bloquant; contrôles inchangés** | Les quatre suites passent ensemble. Le delta ne change pas les gates de divulgation `external_llm_allowed`, TLP ni la frontière provider; il ajoute seulement les codes d’omission au refus pré-transport. Le compte exact des IOC omis reste UNKNOWN, conformément à R4 (`bridge_investigator.py:1512-1579`; `tests/test_bridge_investigator.py:213-234`). |

### Vérifications R5

```text
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py tests/test_advisor_output_reparse.py -q --tb=short
153 passed in 2.00s

.venv/bin/ruff check src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py
All checks passed!

.venv/bin/ruff format --check src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py
ÉCHEC : advisor_dossier.py serait reformaté (3 différences de formatage signalées).

PYTHONPATH=$PWD/src .venv/bin/mypy src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py
Success: no issues found in 2 source files

git diff --check df1bd8f..cd69eca
clean
```

Les tests et oracles utilisent uniquement des données synthétiques et un transport factice. Aucun coût fournisseur n’est observable.

### UNKNOWN R5 et action suivante

- **UNKNOWN** : si la limite de 512 octets doit s’appliquer au total des segments encodés séparément; aucun contrat d’agrégation n’a été établi.
- **UNKNOWN** : si un consommateur en aval décodera le texte doublement percent-encodé.
- **À corriger avant PASS** : contrôler les champs utilisés dans les titres, borner les payloads `data:` non Base64, et définir/implémenter la limite attendue pour les valeurs fractionnées. Corriger aussi le formatage Ruff de `advisor_dossier.py`.
- **Coût : UNKNOWN** — aucun appel fournisseur réel n’a eu lieu.

**Verdict final R5 : FAIL** — le contournement R4 par data URL Base64 canonique est fermé avant décodage et soumission, mais des formes encore incluses dans le dossier maintiennent F6c ouvert.
