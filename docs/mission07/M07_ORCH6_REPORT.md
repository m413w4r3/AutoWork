# Mission 07 — rapport de l'orchestrateur #6 (Sonnet 5.5 medium), 2026-10-29 → 2026-10-30

Rédigé par l'orchestrateur #6 au moment de la passation. Complète `M07_HANDOFF_ORCH6.md` (historique) ; la suite est dans `M07_HANDOFF_ORCH7.md`.
Les journaux bruts vivent dans `.autowork-runs/` (ignoré par Git) ; les rapports d'audit utiles ont été copiés dans `docs/mission07/reports-orch6/`.

## 1. État final vérifié (2026-10-30)

- Dépôt : `main` local = `origin/main` après push (fast-forward depuis `2ea40d4f`, ~108 commits). La CI GitHub n'a **jamais** tourné sur ces commits.
- Déployé : empreinte `b450663f5b8beddf` identique dans backend / worker / analysis-worker / job-recovery (`docs/mission07/tools/container_fingerprint.py` ↔ `git_fingerprint.py`), Alembic `0006_tool_seconds_accounting`, jetons opérateur présents (64/64), Ghidra sain (`cti-bulletin-ghidra-1`, profil compose `ghidra`).
- Le code déployé est un peu en arrière du dernier commit (docs/outils seulement : `068c83a5`). Aucun correctif de code non déployé.
- Relais : v2 (`docs/mission07/tools/relay.py`) lancé dans `.autowork-runs/m07b` pour l'enquête `86ece81f…` (pid dans `relay-status.json`). L'ancien relais (`m07`, enquête `a8845772`) est arrêté.
- Politique OSINT_OPEN `dc034634-0e36-57cd-ba4e-a61bb2bb736a` v2 active (providers bridge/shodan/vt). Shodan inutilisable (compte sans crédit).
- Proxy VT : `VIRUSTOTAL_PROXY_URL` (hôte `195.154.30.203:3128`) est **maintenu par l'utilisateur** ; il était hors service (« No route to host ») et rétabli. Si les lectures VT finissent `needs_review` avec `adapter_output_archived=false`, tester d'abord la joignabilité TCP avant de chercher dans le code.

### Enquêtes sur le sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a` (« Blinder Tunnel »)
| Enquête | Run | État | Note |
|---|---|---|---|
| `a8845772-5025-4702-8850-2b6be776855b` | 13 `27478dcc` | `running` (v174, abandonnée) | 10 analystes (orch. #5), avis v9 brouillon, 3 findings `supported` |
| `5f66ebfb-1653-45e6-83cd-a29af6f87868` | 12 | `cancelled` par l'utilisateur | historique, sert de comparaison |
| **`86ece81f-33bf-4230-9c5b-be55ea67855e`** | 14 `126dad51` (synthèse `fa1440b8`) | `running` (v66, opérateurs terminés) | nouvelle analyse de cette session |

Run 14 : production terminée (`ready`), synthèse vérifiée SHA `8eb9cbe0…` (identique aux runs précédents).

## 2. Ce qui a été fait

### Correctifs logiciels mergés ET déployés (tous testés : unitaires + 499 tests PG/workflows verts avant déploiement)
1. **Audit comportemental des opérateurs 1-10** → `docs/operations/m07-operator-behavior-audit.md` + playbook générique `operator-playbook-medium.md`.
2. **Relais v2** : GET concurrents, POST sérialisés, `relay-status.json`, `inflight/`, INV et workdir en paramètres (`RELAY_INVESTIGATION_ID`, `--workdir`). `safe_deploy.sh` lit `RELAY_WORKDIR`.
3. **Rendu PDF** (QA v9) : backticks inline, plus de « Confiance : NN % » (certainty non calibré), notes liées aux tableaux, hashes complets ; l'ordre `references → chronology → synthesis` est un contrat de la QA de publication (une tentative de l'inverser a été annulée).
4. **`archive.extract_member`** : extraction confinée d'un membre d'archive → échantillon dérivé en quarantaine (hash vérifié, provenance parent + chemin, isolation inter-enquêtes, TLP effectif, verrou de révocation). Testé en live (membre `…/Resources/PsProxy.dll`, chemin réel doublement imbriqué). 4 revues de code ont été nécessaires.
5. **D8** : `restart-with-new-sources` / annulation d'un job déjà terminal idempotents ; un run n'affiche plus « running » après un échec de job.
6. **VT** : retry borné (2 tentatives) des lectures passives non confirmées ; une nouvelle invocation identique est acceptée après un résultat non confirmé.
7. **Lecture du brouillon d'avis** par l'opérateur : section `current_opinion` paginée (blocs, ancres, CAS) ; erreurs de revue Bridge explicites (`valid_targets`).
8. **Similarité** : `samples.compare` / `features.common` renvoient `unavailable` + raison par hash (`sample_not_acquired`, `feature_pack_missing`) au lieu d'une erreur opaque.
9. **Dossiers Bridge** : déduplication SANS PERTE (un corps distinct n'apparaît qu'une fois, références ensuite) ; un échec Bridge confirmé avant envoi (`pre_submission` + non-exécution attestée) fait échouer l'invocation (retentable) et libère la réservation.
10. Outils : `docs/mission07/tools/fast_unit.sh` (unitaires en ~75 s sur 8 processus), `render_opinion_pdf.sh` (variable `RENDER_INVESTIGATION_ID`).

### Tentatives annulées (ne pas refaire telles quelles)
- **Limite/compaction de taille des dossiers Bridge** : refusée par l'utilisateur (« ça va induire une régression »). Aucune troncature, aucun plafond, aucun refus sur la taille.
- **Filtre/redaction des hashes d'échantillons restreints dans le contexte Bridge** (4 branches, 5 revues, toujours des contournements) : tout annulé avant déploiement. Lacune connue et documentée dans `docs/investigation_operations.md` (section « Known gap ») : les hashes d'échantillons RED / `external_llm_allowed=false` ne sont pas filtrés au niveau du prompt Bridge. Contournement opérationnel : ne pas utiliser Bridge sur une enquête portant des échantillons restreints.

## 3. Résultat de la nouvelle analyse (enquête `86ece81f`) — ÉCHEC PARTIEL, à lire honnêtement
- Opérateurs A, B, C, D (Luna xhigh, sessions successives) sur la même enquête. A et B se sont arrêtés sur des défauts d'infrastructure/logiciels (routes devinées interdites, proxy VT hors service, revue d'avis impossible) ; C a été bloqué par un dossier Bridge de 11,5 Mo ; D a obtenu une revue Bridge valide de l'avis v3 (2 unités utilisables).
- Findings H1 (revendeur / DNS), H2 (portée ETW), H3 (attribution) : **tous `qualified`**, aucun `supported`. VT passif corrobore l'association DNS historique de l'IP de l'article (pas l'achat). Aucun échantillon nouvellement analysé.
- Avis v4 brouillon (jamais approuvé ni publié), PDF 19 p. : QA `FAIL` de mise en page (`reports-orch6/pdf_qa_004.md`) : pages quasi vides p.12/19, en-tête/logo absents sur p.2/10/11/14/15, libellés internes en anglais dans l'annexe, hash replié. Contenu : titre « infrastructures critiques irakiennes » plus large que la conclusion (cible individuelle) ; v4 non relue par Bridge ; mention CPX sans pointeur canonique direct vers `S6P2.1`.
- Comparaison indépendante (`reports-orch6/comparison_report.md`) : N moins bon que P (`a8845772`) sur résultats analytiques, qualité de l'avis, couverture Bridge ; meilleur sur le volume d'appels (302 vs 836 lignes de journal) et le coût Bridge (2 tours / ~321 Ko contre 9 tours / 1,8 Mo) ; H : INCONNU. Effet logiciel réel mais confondu avec la stratégie.
- Observateurs #1-#3 : voir `reports-orch6/observer*_final_report.md`. Isolation : aucun contenu d'autres enquêtes détecté ; un hash de commit Git du dépôt cité par l'opérateur A (bénin, objet Git local).
- Les verdicts finaux de `M07_MISSION.md` sont sous **§11** (le handoff précédent disait §16 par erreur).

### Verdicts provisoires (mise à jour de `M07_HANDOFF_ORCH6.md` §6)
| Verdict | Statut |
|---|---|
| OPERATOR_AUTHENTICATION / OSINT_OPEN_POLICY / DEPLOYED_CODE_INTEGRITY | PASS |
| CI_CURRENT | NOT_TESTED (jamais exécutée sur ces commits) |
| VT_REAL_RESEARCH | PASS (proxy rétabli) |
| BRIDGE_REAL_SUBMISSION | PASS ; BRIDGE_GUIDANCE_QUALITY PARTIAL |
| LOCAL_STATIC_ANALYSIS | PASS (hors SMDA) ; `archive.extract_member` PASS en smoke |
| FINAL_CTI_FINDINGS / OPINION | PARTIAL (tout `qualified` sur la nouvelle analyse) |
| TYPST_PDF_RENDER / FINAL_PDF_QA | FAIL sur v4 (défauts de rendu connus, correctif non mergé) |
| INDEPENDENT_ANALYTICAL_AUDIT | PASS (3 observateurs) |
| HISTORICAL_COMPARISON | PARTIAL (H inconnu) |
| REAL_RUN_COMPLETED | PARTIAL |

## 4. Observations et leçons (à retenir)
1. **Frictions logicielles = arrêt prématuré des opérateurs.** Chaque blocage non sécurité (route 403, `needs_review` sur une lecture, avis illisible) fait stopper l'opérateur. Le brief doit dire de contourner par un autre pivot, et le catalogue de routes doit figurer dans le brief (les opérateurs devinent sinon des routes interdites).
2. **Bridge** : (a) jamais JSON ; (b) aucune limite de taille ; (c) en base, `model_conversation_turns.input_blob_reference → blobs.size` donne la taille réelle des prompts (hier 98-407 Ko = succès) ; (d) `model_runs.submission_state=not_submitted` + `error_details.phase=pre_submission` = rien n'est parti ; (e) `conversation_unavailable` (fenêtre dédiée impossible) est une fragilité côté projet Bridge (`/home/nill/perso/work/Bridges/chatgpt-bridge`, géré par l'utilisateur ; l'envoi de fichier a été réparé par lui) : attendre quelques minutes et retenter une fois.
3. **Tests lents** : utiliser `fast_unit.sh` ; PG (`tests/integration tests/workflow_emulator`) ~6-7 min uniquement avant un déploiement ; tuer les processus `pytest` périmés d'anciens agents (ils font monter la charge et créent de faux échecs) ; les sous-agents n'accèdent ni à PG ni à Docker ni au DNS (leurs tests PG/Typst réels sont à relancer par l'orchestrateur).
4. **Sous-agents Luna** : tâche bornée, clone `/tmp/m07-xxx`, branche, `git fetch /tmp/m07-xxx <branche>` puis `merge --no-ff`. Toujours vérifier chez soi (mypy, ruff sur tout `backend/`, fast_unit) : plusieurs livrables arrivaient avec mypy/imports cassés ou des attentes de test périmées. Ne jamais utiliser un Luna pour de l'orchestration (outillage de relais, handoff) : l'utilisateur l'a rappelé.
5. **Sécurité** : le contrôle automatique a refusé un script de test qui désactivait la vérification TLS vers le proxy VT ; ne pas contourner. `.env` ne se lit ni ne se modifie ; l'utilisateur gère proxy VT, Shodan, secrets (rotation des secrets déjà recommandée).
6. **Isolation des opérateurs** reposant sur des consignes (pas d'isolation technique) : les opérateurs peuvent lire le dépôt. L'observateur doit vérifier.
7. `7z` dans l'image analysis-worker affiche aujourd'hui « 7-Zip 26.02 » alors que l'attestation d'outil dit 16.02 : à vérifier ; formats 7z de liens physiques/fichiers spéciaux pour `archive.extract_member` : INCONNU (non testé avec le binaire déployé).
8. `render_opinion_pdf.sh` et `safe_deploy.sh` sont des outils d'orchestration : maintenus par l'orchestrateur, pas par un Luna.

## 5. Dette ouverte
- Correctif de rendu PDF v4 (en-têtes de continuation, pages quasi vides, libellés FR de provenance, coupures de hash) : **non mergé**, travail interrompu (`/tmp/m07-pdf4`, rien de committé). Mis de côté par l'utilisateur.
- Redaction/filtre des échantillons restreints côté Bridge (voir §2).
- Réconciliation des réservations historiques / invocations `needs_review` (VT ×2 de `86ece81f`, Bridge ×2, ~22 sur `a8845772`) : sans effet réel, mais à purger proprement.
- `archive.extract_member` : seulement validé en smoke par l'orchestrateur, aucun opérateur ne l'a utilisé ; les outils `unavailable` de similarité non exercés en live.
- CI GitHub non vérifiée sur les ~108 commits poussés.
- Secrets : rotation recommandée (valeurs opérationnelles imprimées dans une ancienne transcription).
