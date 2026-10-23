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
- **Verdict final** : **FAIL**. Les corrections F1–F4 sont confirmées et les drapeaux de troncature F5 sont rendus. F6 reste un défaut HIGH : un canal séparé de mesures hex legacy laisse passer des valeurs sans borne.

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
