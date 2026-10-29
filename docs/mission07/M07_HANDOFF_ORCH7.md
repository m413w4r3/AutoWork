# Mission 07 — handoff à l'orchestrateur #7 (Sonnet 5.5 medium)

Rédigé le 2026-10-30 par l'orchestrateur #6. **Fait autorité** sur `M07_HANDOFF_ORCH6.md` pour l'état courant et la mission.
Lis dans cet ordre : `AGENTS.md`, `backend/AGENTS.md`, ce fichier, `M07_ORCH6_REPORT.md` (état, leçons, dette), `M07_MISSION.md` (verdicts §11) et
`M07_CONTRACT_PHASE_C.md`, puis la mémoire `/home/nill/.claude/projects/-home-nill-perso-work-AutoWork/memory/MEMORY.md` (surtout `mission07-orch6-state`,
`feedback-simplify-remove-friction`, `fresh-subagent-per-round`, `bridge-never-json`, `paseo-sandbox-hides-host-processes`) et `docs/operations/operator-playbook-medium.md`.

## 0. Rôle et règles (inchangées, rappel)
- Tu es l'orchestrateur (Sonnet 5.5 medium). **Tu orchestres ; tu n'enquêtes pas, et tu ne confies aucune tâche d'orchestration à un Luna** (outillage de relais,
  handoff, scripts de déploiement : c'est toi). Tous les autres agents : `codex/gpt-6-luna`, `thinkingOptionId=xhigh`, `modeId=auto-review`, **session neuve par tâche**,
  `mcp__paseo__create_agent` sans `workspaceId`. Les enfants n'atteignent ni PostgreSQL, ni Docker, ni le DNS/live : tu lances toi-même PG, déploiements, appels live.
  Pas plus de ~4 sessions simultanées (les sous-agents d'un opérateur comptent dans ce plafond).
- Ne jamais lire/afficher/modifier `.env`, jetons, clés. Ne jamais tester le relais avec `pgrep`/`ps`. Ne rien publier ni approuver (avis, publication, repin) : décisions humaines.
  Le jeton humain n'est utilisé que pour ce que l'utilisateur a autorisé (démarrer une enquête, restart de run) ; jamais pour approuver/publier.
- **Consignes utilisateur durables** : supprimer les frictions qui ne sont pas de vraies raisons de sécurité ; **aucune limite de taille sur les messages Bridge** ; Bridge ne produit pas de JSON
  (prose ; formats compacts tolérants ; mot « JSON » interdit dans les prompts Bridge) ; `do_not_submit` ne bloque pas Bridge/VT ; `tool_seconds` est une comptabilité ;
  l'utilisateur gère l'infra externe (proxy VT, Shodan, projet Bridge `/home/nill/perso/work/Bridges/chatgpt-bridge`, `.env`) : s'il y a un défaut là, le lui signaler avec preuves.
  Réponds en français, concis, avec preuves (ids, SHA, tests réellement lancés).
- **Pour l'instant : on met de côté les tests et le rendu Typst.** Pas de campagne de tests, pas de correctifs PDF. Si un défaut logiciel bloque l'analyse, corrige-le (TECH-FIX Luna,
  branche, tests CIBLÉS uniquement : `bash docs/mission07/tools/fast_unit.sh` ou fichiers ciblés, + PG ciblé si le correctif touche la base) puis déploie avec `safe_deploy.sh`.

## 1. État vérifié (2026-10-30) — à revérifier au démarrage
- `main` = `origin/main` (poussé le 2026-10-30), déployé `b450663f5b8beddf` (même empreinte dans backend/worker/analysis-worker/job-recovery), Alembic `0006`, Ghidra sain.
  Vérifs : `python3 docs/mission07/tools/git_fingerprint.py` vs `docker exec -i cti-bulletin-<svc>-1 python3 - < docs/mission07/tools/container_fingerprint.py` ;
  `docker exec cti-bulletin-backend-1 alembic current` ; `${#OPERATOR_API_TOKEN}`=64 dans backend/worker/job-recovery.
- Relais v2 vivant dans `.autowork-runs/m07b` (enquête `86ece81f`). Il faudra un NOUVEAU relais/répertoire pour la nouvelle enquête (§3).
- Sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a` : production et assemblage réussis (run 14 `126dad51-ada8-4f19-a297-094304240816`, synthèse `fa1440b8…`, SHA `8eb9cbe0…`).
  Enquête `86ece81f` (même run) : terminée par les opérateurs A-D, tout `qualified` ; ne pas la réutiliser (cf. rapport).
- Ghidra : démarré à la main (`docker compose --profile ghidra up -d --no-deps --no-build ghidra`) ; backend/worker sondent Ghidra au démarrage (les redémarrer si Ghidra démarre après).
- Déployer **uniquement** avec `RELAY_WORKDIR=<workdir du relais actif> UV_HTTP_TIMEOUT=120 docs/mission07/tools/safe_deploy.sh` (attend l'absence d'appel en vol, met le relais en pause). Réseau PyPI parfois instable : relancer.
- Outils d'orchestration : `docs/mission07/tools/` (`relay.py`, `relayget.py`, `smoke.py`, `safe_deploy.sh`, `fast_unit.sh`, `render_opinion_pdf.sh`, empreintes). `opclient.py` du dossier `.autowork-runs/m07b/` sert de client aux opérateurs (à recopier).

## 2. MISSION PRIORITAIRE (décidée par l'utilisateur)
Relancer une **phase d'analyse repartant de zéro** sur le sujet (synthèse + assemblage déjà réussis), conduite par des **opérateurs Luna xhigh sans aucune connaissance des résultats
précédents** (ni findings, ni avis, ni logs de `a8845772` / `86ece81f` / `5f66ebfb`), **autorisés à lancer des sous-agents** pour explorer des pistes, selon la chaîne ci-dessous.
Objectifs à valider de bout en bout : (a) téléchargement des échantillons, (b) planification Bridge, (c) construction d'une règle YARA avec Bridge + test,
(d) recherche de pivots VT, (e) affirmer ou infirmer les éléments CTI avancés et vérifiables dans les sources, avec nos échantillons et leur analyse (ILSpy / Ghidra / strings / capa / FLOSS…),
(f) rédaction de la « note de l'analyste » **exclusivement avec Bridge**, (g) rétro-conception des éléments cryptographiques et du protocole réseau (si observables sur les échantillons),
(h) graphique de représentation du protocole (à demander à Bridge).

### Chaîne de travail (schéma de l'utilisateur ; l'opérateur journalise chaque étape)
0. **Acquérir** : télécharger les échantillons disponibles (VT) + récupérer l'information VT disponible (rapports, relations, comportements).
1. **Plan avec Bridge + article** : identifier les éléments observables, réfutables ou confirmables (consultation de planification ; question précise, prose).
2. *(fusionné avec 3 dans le schéma original)*
3. **Analyser les échantillons** pour confirmer/infirmer les éléments intéressants (statique : ILSpy, Ghidra, capa, FLOSS, strings ; membres d'archive via `archive.extract_member`).
4. **Rétro du protocole réseau** ; retenir les éléments crypto et marquants (clés, IV, constantes, dérivations, structure des messages).
5. **Première règle YARA** : demander à Bridge en lui partageant les éléments de l'article + issus de la rétro réseau, crypto, strings.
6. **Valider la règle** (test sur échantillons connus + goodware).
7. **Pivots** : utiliser éléments trouvés + éléments de la règle + éléments de l'article ; demander à Bridge des stratégies de pivot VT ; exécuter les pivots VT.
8. **Télécharger les candidats**, **revalider** avec la règle, faire évoluer la règle si besoin → **boucle pivot/YARA** jusqu'à épuisement raisonnable.
9. **Avis de l'analyste rédigé via Bridge** (texte produit par Bridge, intégré/critiqué par l'opérateur), relecture Bridge.

### Outils déjà présents (vérifier dans `available_tools` de l'enquête ; noms indicatifs)
- VT : `vt.file_report`, `vt.relationships`, `vt.behaviour`, `vt.intelligence_search`, `vt.download_sample` (proxy VT de l'utilisateur ; lectures passives retentées automatiquement).
- Échantillons/analyse : `sample.analyze` (static/capa/SMDA), `sample.extract_obfuscated_strings`, `sample.extract_configuration`, `native.inspect`, `local.run_command` (strings, zip_members, archive_list…),
  `archive.extract_member` (membre d'archive → échantillon dérivé), `assembly.inspect` / `methods.list` / `method.decompile` / `types.list` / `references.imports` (ILSpy), `ghidra.*`.
- Réseau/protocole : `network.capture.inspect`, `network.stream.reassemble`, `network.messages.segment`, `network.protocol.compare|hypothesize|verify|document|render`, `network.suricata_*`.
- YARA/règles : `rule.propose_bridge`, `rule.propose_from_candidates`, `rule.propose`, `rule.revise`, `rule.test`, `rule.compare`, `rule.get`, `rule.workbench`, `samples.compare` / `features.common` (similarité ; `unavailable` ≠ négatif).
- Bridge : `advisor.consult` (modes plan/pivot_strategy/review/figure_plan…), `advisor.reparse` (schéma `advisor-reparse.v1`, local, ne renvoie rien à Bridge), `opinion.compose|revise`, findings (`finding.propose|revise`), hypothèses.
- **Frida / exécution dynamique : NON disponible dans le dépôt, et `AGENTS.md` interdit l'exécution de malware sans autorisation explicite séparée de l'utilisateur.** L'utilisateur a cité Frida :
  ne rien faire de dynamique ; demande-lui une autorisation explicite (portée, bac à sable) AVANT toute proposition ; en attendant, statique uniquement. Dis-le clairement.
- Graphique du protocole : demander à Bridge un plan de figure (`figure_plan`) et utiliser `network.protocol.render` ; le rendu Typst final est hors périmètre pour l'instant.

### Sous-agents de l'opérateur
Les opérateurs sont des sessions Paseo Luna ; vérifie ce que leur outillage permet (outils Paseo disponibles dans leur session ?) avant de promettre quoi que ce soit. Règles proposées :
l'opérateur « chef » garde seul les appels Bridge (POST) et toute mutation canonique (findings, avis) ; les sous-agents explorent en lecture + analyse locale (relais : préfixe d'id propre,
GET et analyses locales ; un seul POST en vol à la fois côté relais), journalisent dans `workers/<nom>.md`, et rendent un résumé avec références d'observations ; plafond global ~4 sessions
(donc au plus 2-3 sous-agents si l'opérateur + l'observateur tournent). Si l'outillage Paseo n'est pas exposé aux opérateurs, propose à l'utilisateur la variante « l'orchestrateur lance
les sous-agents sur demande de l'opérateur via un fichier de demandes » ; ne bricole pas de contournement de sécurité.

## 3. Procédure de lancement (à exécuter dans l'ordre)
1. Vérifications §1, budgets (`GET /operator-status` via `relayget.py`, relais actuel) ; arrêter proprement le relais de `m07b` si inutile (pas d'appel en vol).
2. **Nouveau run** : `POST /api/production/subjects/d22822d4-27ce-47d2-9cc5-f84cf312986a/production/restart-with-new-sources` (jeton humain lu DANS le conteneur backend, jamais affiché ; script d'exemple :
   `/tmp/hpost.py` n'est pas versionné → réécris un petit script `docker exec -i cti-bulletin-backend-1 python -` qui lit `OPERATOR_API_TOKEN` dans l'environnement). Le correctif D8 est déployé : le restart
   ne devrait plus échouer (sinon contournement : `cancel` puis `resume` du run). Les étapes précédentes sont réutilisées (quelques minutes). Attendre `status=ready`.
3. **Nouvelle enquête** : `POST /api/subjects/<sujet>/investigations` avec `{"production_run_id", "synthesis_artifact_id", "synthesis_sha256", "execution_mode":"operator_led"}` (synthèse vérifiée du nouveau run).
   L'enveloppe est approuvée par héritage de la politique OSINT_OPEN v2 (vérifier `GET /api/investigations/<id>/envelope` : `currently_authorized: true`). Budgets neufs (Bridge 200, VT 10000, downloads 1000).
4. **Nouveau répertoire de relais** `.autowork-runs/m07c/` : copier `docs/mission07/tools/relay.py` et `.autowork-runs/m07b/opclient.py`, lancer
   `RELAY_INVESTIGATION_ID=<inv> nohup python3 relay.py --workdir "$PWD" > relay.log 2>&1 &`, vérifier `relay-status.json` et un GET `/operator-status`.
5. **Brief opérateur** `.autowork-runs/m07c/OPERATOR_BRIEF.md` : repars de `.autowork-runs/m07b/OPERATOR_BRIEF.md` (catalogue de routes autorisées, isolation, journal, mises à jour d'état) + `docs/operations/operator-playbook-medium.md`
   et **ajoute la chaîne de travail §2 comme plan d'action en étapes numérotées**, la règle « ne t'arrête pas sur un outil en échec : contourne par un autre pivot et note la friction », « `unavailable`/`needs_review` ≠ preuve négative »,
   « un fait atomique directement observé peut être `supported` », « avis rédigé via Bridge », et la politique de sous-agents. **Ne mets dans le brief AUCUNE conclusion/finding/hash/nom propre des runs précédents.**
   Interdis-lui de lire `.autowork-runs/m07*` d'autres répertoires, `docs/mission07/`, `docs/operations/m07-*` et tout document d'enquêtes antérieures.
6. Lance **un opérateur Luna xhigh** (prompt : lire `AGENTS.md` + le brief ; `opclient.AGENT="CTI-OPERATOR-A"`, ids préfixés `a`) et **un AUDIT-OBSERVER indépendant** en lecture seule (rapports par lots dans `observer/`, vérifie isolation, usage Bridge,
   arrêts prématurés, qualité probatoire, frictions). Surveille uniquement les notifications ; en cas d'arrêt de l'opérateur, lis `FINAL_CHECKPOINT*.md` et le journal, corrige les frictions logicielles trouvées, puis relance un opérateur de continuité
   (même brief + note d'état) — c'est ainsi que A→D ont fonctionné.
7. À la fin : COMPARAISON indépendante (nouvelle analyse vs `86ece81f` vs `a8845772` vs historique `5f66ebfb`), audit final, verdicts §11 de `M07_MISSION.md`, compte rendu à l'utilisateur.

## 4. Points d'attention connus pour cette mission
- **Bridge** : prompts en prose, pas de limite de taille ; l'envoi de fichier a été réparé côté projet Bridge par l'utilisateur ; `conversation_unavailable` → attendre quelques minutes, retenter UNE fois, continuer sinon.
  Diagnostic Bridge côté base : `model_runs.submission_state/error_code/error_details`, taille du prompt dans `blobs.size` via `model_conversation_turns.input_blob_reference`.
- **VT** : si `needs_review` systématique, vérifier la joignabilité TCP du proxy (`195.154.30.203:3128`, configuré par l'utilisateur) avant tout ; ne jamais désactiver la vérification TLS dans un test ad hoc.
- **Échantillons** : `vt.download_sample` (budget `downloads`) ; membres d'archive : `archive.extract_member` (chemin réel complet, ex. dossier racine répété ; lister d'abord avec `local.run_command archive_list`/`zip_members`). Les échantillons restreints
  (TLP RED / `external_llm_allowed=false`) ne doivent pas passer par Bridge (lacune connue, §2 du rapport).
- **7-Zip** : bannière « 26.02 » dans l'image vs attestation 16.02 : à clarifier si des archives 7z posent problème.
- Les hypothèses/claims de l'article sont des données de source, pas des observations. `qualified` vs `supported` : voir playbook.
- Contexte : si tu dépasses ~60-70 %, mets à jour ce document (ou un successeur) et prépare la relève vers un nouvel orchestrateur Sonnet 5.5 medium (jamais un Luna).

## 5. Dette connue (voir `M07_ORCH6_REPORT.md` §5)
PDF v4 non corrigé (hors périmètre pour l'instant) ; redaction Bridge des échantillons restreints ; réservations historiques à purger ; CI GitHub à vérifier sur les commits poussés ; rotation des secrets.
