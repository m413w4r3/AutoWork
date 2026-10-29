# Mission 07 — handoff à l'orchestrateur #6 (Sonnet 5.5 medium)

Rédigé le 2026-10-29 par l'orchestrateur #5. **Fait autorité sur `M07_HANDOFF.md`** (historique) pour l'état courant.
Contrat utilisateur : `M07_MISSION.md`, `M07_CONTRACT_PHASE_C.md`. Règles de dépôt : `AGENTS.md`, `backend/AGENTS.md`.

## 0. Ton rôle

Tu es l'orchestrateur : tu diriges, tu ne mènes pas l'enquête CTI toi-même. Tous les autres agents sont `codex/gpt-6-luna`,
`auto-review`, `xhigh`, **une session neuve par tâche**, jamais réutilisée une fois terminée (création : `mcp__paseo__create_agent`
sans `workspaceId`). Les enfants n'atteignent ni PostgreSQL, ni Docker, ni le live : **tu exécutes toi-même** tests PG,
déploiements, appels live. Ils travaillent dans un clone `git clone /home/nill/perso/work/AutoWork /tmp/m07-fixNN`
et tu récupères leur branche par `git fetch /tmp/m07-fixNN <branche>:<branche>` puis `git merge --no-ff`.

Décisions et préférences de l'utilisateur (à respecter, elles priment sur les anciennes consignes) :
- **Simplifier, supprimer les frictions** : tout blocage qui n'est pas une vraie raison de sécurité (clé API, jeton, secret, TLP RED,
  `external_llm_allowed=false`, licence, révocation, double envoi) doit être supprimé, pas contourné.
- `do_not_submit` ne bloque plus rien pour Bridge/LLM/VT (champ conservé, sans veto). `tool_seconds` est une comptabilité,
  jamais un plafond. Shodan est désactivé (compte `oss` sans crédit ; `SHODAN_*_ENABLED=false` dans `.env`, géré par l'utilisateur).
- **Bridge ne sait pas produire du JSON de façon fiable** (relais ChatGPT web-UI) : réponses prose/JSON acceptées comme conseil
  utilisable ; le mot « JSON » est interdit dans les prompts Bridge (test d'architecture).
- Tu peux lancer toi-même les scripts de lecture/rendu qui utilisaient le jeton humain (`render_opinion_pdf.sh` : rendu d'un
  BROUILLON, sans effet sur l'état). Tu ne prends jamais une **décision humaine** (approuver/publier un avis, activer une politique,
  étendre une enveloppe G2) avec le jeton humain : tu prépares un script et l'utilisateur le lance (précédents :
  `activate_shodan.sh`, `extend_envelope_shodan.sh`, dans `.autowork-runs/m07/`).
- Ne jamais afficher/inspecter `.env`, jetons, clés. Ne pas pousser sur `origin` sans demande explicite. Ne rien publier.
- Réponds en français, concis, avec preuves (ids, SHA).

## 1. État vérifié au moment du handoff

- Dépôt : `main` local = `d2d22da0` au moment du handoff (suite de gates verte : ruff/format sur tout `backend/` migrations incluses, mypy 427 fichiers,
  5 346 tests unitaires dont rendus Typst réels, 460 tests PG) ;
  **`origin/main` = `2ea40d4f`, ~47 commits de retard** (rien n'a été poussé après `4a18e688` ; la CI GitHub n'a pas été relancée
  sur ces commits ; deux tests PG qui échouaient en CI sont corrigés localement, cf. §3).
- Déployé : vérifier avec `docs/mission07/tools/container_fingerprint.py` (dans chaque conteneur) == `git_fingerprint.py`.
  Alembic live `0006_tool_seconds_accounting`. Jetons opérateur présents (64/64) dans backend/worker/job-recovery.
  Si le dernier merge (rendu Typst) n'est pas déployé : image backend à reconstruire (`make up`, elle copie `chpTypst`).
- Stack : conteneurs `cti-bulletin-*` (backend, worker, analysis-worker, job-recovery, frontend, postgres, redis, minio) + `cti-bulletin-ghidra-1`
  (profil compose `ghidra`, démarré à la main : `docker compose --profile ghidra up -d --no-deps --no-build ghidra` ; sonde Ghidra faite
  **au démarrage** de backend/worker => les redémarrer si Ghidra démarre après) + `bridges-*` (relais ChatGPT, hors dépôt).
- Politique OSINT_OPEN `dc034634-0e36-57cd-ba4e-a61bb2bb736a` **v2** (providers bridge/shodan/vt ; budgets vt 10000, downloads 1000,
  bridge 200, tool_seconds 86400, shodan 1000).
- Enquête `a8845772-5025-4702-8850-2b6be776855b` (sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a`, run `27478dcc-69d6-4593-89fb-7bd45e9c3537`,
  synthèse `4d37c738-d78f-4087-b251-3c38a56e8e53` v1 SHA `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`),
  `operator_led`, `running`, état ~v172, enveloppe `human_g2` v322 (étendue par l'utilisateur : tool_seconds 345600, shodan 1000).
  Budgets : Bridge 11 consommés / 19 réservés / 200 ; VT 22 / 13 réservés / 10000 ; downloads 0 ; tool_seconds sans plafond.
- **Résultat analytique** (analystes 1-10) : findings `qualified` sauf 3 `supported` (ZIP `6e7d9b33…` contient `PsProxy.dll` 3fd810a3…
  et `RuntimeBroker.dll` 53f35e49… byte-identiques aux samples autonomes ; 2 rapports VT passifs nommant ces hashes). H1-H4 et la capacité
  statique restent `qualified` ; aucun IOC réseau, aucune exécution, aucune attribution indépendante, pas de constante crypto observée.
  Avis `128f991d-09c9-5cb5-905c-d0c044bfff00` **v9 DRAFT** SHA `cedb2157e6bfd06f0bfd8b61765e9e1b183e9dc76d43e5be6ae6e6b6e80047c8`
  (non approuvé, non publié ; relecture Bridge valide `8a15ea8a-7f7d-468f-84aa-d658ad3b122b`, 7 unités). Verdicts de remarques : `analyst_log.md`.
- PDF : v8 (ancien moteur, 18 p.) = QA FAIL (`pdf_qa_001.md`). **v9 rendu avec le nouveau moteur** (déployé, `main@d2d22da0`, empreinte
  `9c99e611cbca43ec` vérifiée sur les 4 services, Alembic 0006) : `.autowork-runs/m07/editorial/opinion-v9-draft.pdf` (22 p., 816 Ko, SHA-256
  `ba8157515326e6f76e864b782ee8413f6450c4bb534f1a6ade4abb6654888d33`, pages PNG dans `pages-v9/`). Constaté à vue (pages 1, 9, 10) : logo
  Chaps Vision en en-tête, carte de synthèse (statut/sources), sources consolidées, tableau d'inventaire réel en page paysage avec hashes
  complets (sur 2 lignes), pages d'annexe paysage. **Défauts restants repérés, non encore traités** : (a) les accents graves des paragraphes
  d'analyste s'impriment littéralement (`` `Resources/PsProxy.dll` ``) : rendre les spans de code inline hors tableaux ; (b) « Confiance : 90 % »
  affiche les `certainty` des blocs comme des probabilités (non calibrées, l'éditeur l'a déconseillé) : remplacer par un libellé qualitatif ou
  masquer ; (c) page 9 à moitié vide, ouverture (références/chronologie avant synthèse, p.1-2) ; (d) pas de bandeau TLP (aucun TLP canonique) ;
  (e) la QA TYPST-PDF n'a pas encore été refaite sur v9 ; (f) `pdfinfo` signale « Suspects object wrong type » (aussi sur la référence).
- Invocations `needs_review` (toutes avant-envoi ou ambiguës, **ne pas rejouer à l'aveugle**) : `9aaec14d` (vt.file_report du ZIP, humain seul),
  `36114580` (vt.relationships bundled_files), ~18 `advisor.consult` (refus avant envoi anciens, `c9-049` ambigu 0 octet, etc.),
  `4a59599e` (shodan.host refusé 403). Elles gardent des réservations (Bridge 19 réservés) : sans effet (200 tours) mais à réconcilier
  proprement à terme.

## 2. Outils d'orchestration (docs/mission07/tools/)

- `relay.py` : relais mécanique fichier→API (hôte, mono-fil). Lancé depuis `.autowork-runs/m07/` (`nohup python3 relay.py &`).
  Whitelist GET `/api/investigations/<INV>/...` et POST `.../operator/tools/<tool>` ; **`INV` est codé en dur** : à changer pour une
  nouvelle enquête. Journal append-only `journal.jsonl`. Un POST Bridge peut durer 2-20 min et bloque la file.
  **Les agents Paseo ne voient pas les processus de l'hôte** : leur dire de ne JAMAIS tester le relais avec `pgrep`/`ps`
  (faux négatif, a fait perdre l'analyste #7) mais par un GET `/operator-status` + attente 60 s.
- `relayget.py` (`get`, `post`), `smoke.py` (`run(tag, tool, args, purpose)`) : appels relais en tant qu'ORCHESTRATOR (corps
  `{"schema_version":"1","arguments":…,"purpose":…,"expected_version":N}`).
- `safe_deploy.sh` : attend que le relais n'ait aucun appel en vol, le met en pause (SIGSTOP), `make up`, reprend (SIGCONT).
  **Toujours** déployer ainsi quand un analyste est actif (un redémarrage pendant un appel crée un effet ambigu). Ne jamais forcer.
  Après chaque déploiement vérifier : jetons (`${#OPERATOR_API_TOKEN}`=64 dans backend/worker/job-recovery), empreinte identique
  (container vs git), `alembic current`, santé, Ghidra.
- `render_opinion_pdf.sh` : rend le brouillon courant de l'avis (jeton humain lu dans le conteneur, jamais affiché) vers
  `.autowork-runs/m07/editorial/opinion-v<N>-draft.pdf`.
- Recette tests PG (poste hôte) : `cd backend && export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
  TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH TMPDIR=$HOME/.cache/m06tmp
  FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst` puis `.venv/bin/python -m pytest tests/integration -q -p no:cacheprovider`
  (≈4-6 min, 460 tests) ; unitaires `tests --ignore=tests/integration --deselect tests/test_confined_analysis_dispatch.py` (3 échecs d'environnement connus).
  **`make lint` couvre aussi `migrations/`** : lancer `ruff check . && ruff format --check .` (pas seulement `src tests`) et `mypy src`.
  Ne pas utiliser `sleep` long ni Monitor inutile : les agents notifient leur fin.

## 3. Défauts trouvés et corrigés pendant M07 (tous mergés, tests verts)

D1-D2 VT `do_not_submit` ; D5 garde Bridge sur samples acquis ; D9-D10 hash VT arbitraire / faux positifs de préflight secret ; D11 corps d'observations
dans le dossier ; D12-D15 refus avant envoi non ambigus, réfutation jamais prérequis ; coordinateur S3 (raisons de refus conservées, retry
après réponse complète jamais « non exécuté ») ; libération unique des réservations sur refus prouvé avant envoi (secret_preflight,
sample_disclosure, evidence_preflight, pre_transport_validation) ; régression `TOOL_UNAVAILABLE` ; P1 secret dans la question persistée
et boucle de tours après refus ; bug API `/osint-open-policy/extend` avec Shodan (500) ; `tool_seconds` non plafonnant (migration 0006) ;
`do_not_submit` supprimé comme veto ; octets bruts d'échantillon citables dans un dossier Bridge ; références inconnues ignorées au lieu
de rejeter toute la consultation (+ cause interne exposée) ; **lenteur 8 min par consultation = boucle O(A×O) de relectures de blobs** (mémoïsée ;
**non vérifiée en live par une consultation chronométrée**) ; réponses Bridge JSON/prose/markdown acceptées (`partial` mais utilisable) ;
réessai automatique (3 essais) des échecs d'interface Bridge avant envoi + FAILED/libération si épuisé ; 7-Zip 16.02 dans l'image
analysis-worker + commandes `zip_members`, `archive_list`, `archive_extract_hash` (attestation de version corrigée : double bannière 7z) ;
`opinion.revise` accepte les lignes typées `implant_materials` (réservé aux clés/jetons extraits, pas aux inventaires) ;
faux positif du garde de publication (`/app` dans `/AppDomainManager` : comparaison de chemins d'hôte aux frontières) ; rendu Typst (cf. §1).

## 4. Backlog ouvert / dette (à trier, aucun n'est « prouvé en production » sauf mention)

1. **Échantillon dérivé** : aucun moyen de rendre un membre d'archive analysable (Ghidra/ILSpy/capa) — besoin d'un `archive.extract_member`
   (hand-off octets worker→quarantaine, hash vérifié, acteur+motif, association idempotente avec provenance parent+chemin ; l'association n'a pas de champ chemin).
2. **Relais fichier→API mono-fil** : fragile (file bloquée derrière un POST Bridge de 10-20 min, invisibilité depuis le bac à sable,
   `INV` en dur). Envisager un canal opérateur propre (API/outil exposé aux agents) ou un relais concurrent + statut des requêtes.
3. Réservations Bridge historiques fuitées (19) / ~22 invocations `needs_review` : procédure de réconciliation humaine à fournir (script préparé pour l'utilisateur).
4. Comptabilité `downloads` : 0 après un « téléchargement neuf » (réutilisation en quarantaine du sujet ⇒ 0 par conception) ; vérifier qu'un vrai téléchargement
   VT inédit incrémente `downloads`/`vt_units` (jamais prouvé en live dans ce run).
5. D7/D8 (restart-with-new-sources vers un lot terminé ; cancel sur job déjà annulé) : chemin « nouveau run » fragile (contournement : `cancel` puis `resume`).
6. Aucun TLP documentaire canonique dans la publication épinglée ⇒ pas de bandeau TLP rendu (volontaire, ne jamais inventer).
7. Dossier Bridge : ~1,8 Mo de prompt pour 9 consultations ; mesurer coût/valeur ; `advisor.reparse` à rendre plus simple ; réponse `partial` + « unités non traitées » incohérente observée.
8. Observations de l'AUDIT-OBSERVER/BRIDGE-EVALUATOR : voir `.autowork-runs/m07/observer/{report_001,report_002,bridge_eval_001}.md`.
9. CI GitHub non revérifiée sur la branche (46 commits non poussés) : décider avec l'utilisateur du push.

## 5. À faire (ordre recommandé)

1. **PDF** : vérifier que le dernier merge est déployé (`safe_deploy.sh` si l'analyste est actif), lancer `render_opinion_pdf.sh` (v9),
   TYPST-PDF (session neuve) pour QA page par page contre `docs/references/CN2609_typ` et `docs/references/chpTypst` (le `chpTypst` runtime =
   référence + `RENDERER/`, `CascadiaCode/`, manifestes), corriger les défauts restants par TECH-FIX, FINAL-REVIEW indépendant.
   Gel du livrable ⇒ COMPARISON avec l'enquête historique `5f66ebfb-1653-45e6-83cd-a29af6f87868` (lecture seule, jamais avant gel).
2. **Audit du comportement des opérateurs agentiques précédents** (analystes 1-10) : sources `journal.jsonl`, `analyst_log.md`, `observer/*`, `processed/`, `outbox/`.
   Questions : décisions pertinentes vs répétitives, usage de Bridge (laborieux, rejets, re-demandes), arrêts prématurés (#7), erreurs de protocole
   (relais, `pgrep`), sur-prudence (aucun finding `supported` avant #9), instructions de prompt qui ont aidé/nui, temps perdu, coût. Produire des consignes de prompt
   améliorées et un playbook opérateur à jour (`docs/operations/operator-playbook-medium.md`).
3. **Chantiers d'amélioration** : §4 par ordre de valeur (1, 2, 7 d'abord). Cycle : preuve → TECH-FIX (Luna) → tests (toi, PG) → CODE-REVIEW indépendant si
   sécurité/données → merge `--no-ff` → gates (§2) → `safe_deploy.sh` → vérification live.
4. **Nouvelle analyse repartant de zéro** (décision de l'utilisateur) : nouvelle enquête **avec de nouveaux opérateurs sans aucune connaissance** des conclusions
   précédentes (ne leur donner ni findings, ni avis, ni logs des runs a8845772). Une enquête par run : pour repartir de zéro il faut un nouveau
   `production_run` (`POST /api/production/subjects/{subject}/production/restart-with-new-sources`, contournement D7/D8 : `cancel` puis `resume`)
   puis création/démarrage d'une enquête `operator_led` adossée à OSINT_OPEN (action humaine : préparer un script pour l'utilisateur si le jeton humain est requis).
   Vérifier avant : relais adapté (`INV`), budgets, envoi aucun contexte historique, observateur indépendant lancé dès le début (AUDIT-OBSERVER lecture seule),
   et écrire à l'avance le prompt d'opérateur amélioré (§5.2). Puis éditeur → PDF → revue finale → comparaison avec a8845772 et l'historique (mesurer l'effet des
   améliorations logicielles séparément des différences de stratégie analytique).
5. Verdicts finaux §16 de `M07_MISSION.md` (statuts PASS/PARTIAL/FAIL/BLOCKED/NOT_TESTED avec preuve) et compte rendu final.

## 6. Verdicts provisoires (preuves)

| Verdict | Statut | Preuve / limite |
|---|---|---|
| OPERATOR_AUTHENTICATION | PASS | identity/status humain + client authentifiés ; B3/B4/B6 initialement sous identité locale `dev-analyst` (à signaler) |
| OSINT_OPEN_POLICY | PASS | v1 puis v2 active, pin sur l'enquête puis enveloppe `human_g2` étendue par l'utilisateur |
| DEPLOYED_CODE_INTEGRITY | PASS | empreintes container == git à chaque déploiement |
| CI_CURRENT | NOT_TESTED | non poussé ; gates locales vertes (unitaires, 460 PG, ruff, mypy) |
| VT_REAL_RESEARCH | PASS | ≥ 25 lectures réelles |
| VT_REAL_ACQUISITION | PARTIAL | 4 téléchargements réussis dont RuntimeBroker.dll ; comptabilité `downloads` non démontrée |
| LOCAL_STATIC_ANALYSIS | PASS (hors SMDA) | capa/FLOSS/ILSpy/Ghidra/strings/zip réels ; 7z ; SMDA indisponible |
| BRIDGE_REAL_SUBMISSION | PASS | `review_text`, `plan_investigation`, `pivot_strategy` réels, tours consommés, archives |
| BRIDGE_GUIDANCE_QUALITY | PARTIAL | évaluateur : voir `bridge_eval_001.md` ; conseils utiles mais coûteux et parfois non exploitables |
| ANALYST_DECISION_QUALITY / DECISION_TRACEABILITY | PARTIAL | journal + observateur ; trous (actions orchestrateur/utilisateur hors journal) |
| EXTERNAL_EFFECT_AUDIT / REPLAY_AND_RESERVATION_INTEGRITY | PARTIAL | fuites de réservation historiques ; corrections déployées non toutes rejouées en live |
| FRICTION_ASSESSMENT | FAIL→en cours | nombreuses frictions corrigées ; relais et latence Bridge à revérifier |
| FINAL_CTI_FINDINGS / FINAL_CTI_OPINION | PARTIAL | avis v9 brouillon relu par Bridge ; tout `qualified` sauf 3 `supported` |
| EDITORIAL_REFERENCE_COMPLIANCE / TYPST_PDF_RENDER / FINAL_PDF_QA | PARTIAL / NOT_TESTED | v8 FAIL mise en page ; nouveau rendu à valider |
| INDEPENDENT_ANALYTICAL_AUDIT | PARTIAL | observateurs #1-#2 + évaluateur Bridge ; observateur #3 à lancer après gel |
| HISTORICAL_COMPARISON | NOT_TESTED | après gel |
| REAL_RUN_COMPLETED | PARTIAL | analyse + avis brouillon ; livrable PDF non validé ; avis non approuvé |

## 7. Références rapides

- Journaux locaux (ignorés par Git) : `.autowork-runs/m07/{journal.jsonl,analyst_log.md,checkpoint.md,observer/,editorial/}`.
- Brief éditorial : `.autowork-runs/m07/editorial/editor_brief_001.md` (SHA `a5ac87ed…`) ; QA PDF : `pdf_qa_001.md` (SHA `c18099f1…`).
- Sauvegardes DB : `/home/nill/perso/work/AutoWork-backups/` (dernier : `live-pre-0006-*.dump`).
- Rotation des secrets recommandée à l'utilisateur (valeurs opérationnelles imprimées dans une transcription d'orchestrateur antérieure).
- Mémoire persistante de l'utilisateur : `/home/nill/.claude/projects/-home-nill-perso-work-AutoWork/memory/` (lire `MEMORY.md`).
