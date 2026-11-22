# M24 lot 10-S — scénario d'acceptation synthétique (24 étapes)

Test unique : `backend/tests/integration/test_acceptance_scenario_postgres.py::test_full_24_step_scenario`
(classe séquentielle `ScenarioJourney`). Toutes les données sont synthétiques. Aucun
fournisseur, CLI, réseau ou corpus CTI réel n'est contacté.

## Pourquoi ce fichier est un test d'intégration

Le scénario exige PostgreSQL réel (migrations `0001`–`0026`), donc il vit sous
`backend/tests/integration/` et il est découvert par `make test-integration`
(`pytest -m integration <path>`). Le support partagé est dans
`backend/tests/acceptance/scenario_support/` ; il ne contient aucun test et ne
touche aucun autre fichier de `backend/tests/acceptance/`.

## Couches réelles / simulées

| Couche | Statut |
|---|---|
| API FastAPI `create_app` + `ASGITransport`, middleware d'identité, tous les routeurs M24 | réelle |
| PostgreSQL (UoW SQLAlchemy, toutes les tables/triggers M24), blobs content-addressed sur disque | réels |
| Sidecar hôte `sidecars/workspace_agent` sur AF_UNIX tmpdir : workspace, watcher, révisions, `base_samples` **et** superviseur de sessions H1 | réel |
| `HttpWorkspaceAgentClient` (workspace + samples) et `HttpHostSessionsClient` (sessions) | réels |
| Broker signé D7 (`/agent-sessions/{sid}/tool-calls`), HMAC par session, attribution tâche/session/fence | réel |
| `advisor.consult` via `OperatorToolInvocationService`, parser de réponse Bridge, persistance des observations | réel |
| Compilation PDF : `PublicationRenderService` + `TypstRenderer` + `TypstSubprocessCompiler` (`typst 0.15.1`) | réelle |
| VirusTotal | simulé : port mémoire (`AcceptanceVirusTotal`) + adaptateur de téléchargement `httpx.MockTransport` ; 5 rapports, 3 échantillons téléchargeables, 2 indisponibles |
| Bridge | simulé : `QueuedBridgeTransport` (sous-classe de `ScriptedTurnTransport`) derrière le vrai chemin `advisor.consult` |
| Codex CLI / Claude Code | simulés : profil `FakeProfile` H1 enregistré sous `codex_cli` / `claude_code`, scénario `hang` (aucun binaire réel) ; le probe est mis en cache dans le profil de test, comme les profils de production |

La consultation Bridge simulée ne prouve pas le transport Bridge réel ; le CLI
simulé ne prouve pas la sémantique Codex/Claude. Le scénario prouve la chaîne
applicative, la persistance, les portes et le rendu.

## Étapes et résultat observé

Journal d'un run PASS (`-s`) :

| # | Action mandatée | Couche réelle | Simulé | Résultat observé |
|---|---|---|---|---|
| 01 | Lancement UI/API d'une enquête `creation_profile=m24_workshop` | API + PG | — | `operator_led`, `prose_authority=bridge_only`, job durable soumis |
| 02 | Workspace local créé par le vrai sidecar | sidecar UDS + binding PG | — | manifeste `.autowork/manifest.json` pinné sur la synthèse, binding `ws-…` |
| 03 | Inventaire IOC | bootstrap + article snapshot | — | 5 hashes + 2 domaines + 2 IP ; synthèse épinglée |
| 04 | Enveloppe G2 initiale proposée puis approuvée | API + PG + décision humaine | — | `proposal_sha256` vérifié, enveloppe v1, décision `pivot_approve` |
| 05 | Collecte | bootstrap + dispatcher d'outils + quarantaine | fake VT | 5 `vt.file_report`, 4 `vt.object_report`, 3 téléchargements vérifiés SHA, 2 indisponibles |
| 06 | Matérialisation contrôlée des échantillons | octets canoniques + route réelle `base-samples` du sidecar | — | 3 fichiers `base_samples/<sha>` en `0440`, replay `already_present`, 2 absents |
| 07 | Plan Bridge accepté | `advisor.consult` + parser + matérialisation | fake Bridge (Markdown) | plan v1 parsé, accepté, 2 tâches matérialisées une fois |
| 08 | Tâche attribuée à Codex, session ouverte | task engine + handoff + sidecar H1 + HMAC D7 | fake Codex CLI | handoff v1, session `running`, bail `fence=1` |
| 09 | Codex inspecte un échantillon via le broker | route broker signée + `vt.file_report` + attribution | fake VT | invocation attribuée `task_id`/`agent_session_id`/`operator:codex:operator-1`, observation VT |
| 10 | Codex consulte Bridge | broker signé + `advisor.consult` + observations | fake Bridge | `advisor_call` + `advisor_response_received` persistés, réf. de réponse liée |
| 11 | Note Markdown humaine locale | watcher + révisions PG | — | révision `rev-…` et événement ingérés par le vrai scan |
| 12 | Note visible via l'API | routes workspace/files + note view | — | texte relu, SHA concordant, `is_evidence=false` |
| 13 | Transfert vers Claude Code par passation versionnée | transfert + handoff v2 + sidecar H1 | fake Claude CLI | destination `operator:claude_code`, Claude `running` ; Codex terminal (`stopped` ou `lost` selon la course de réconciliation, jamais adopté) |
| 14 | Claude approfondit une vérification | broker signé + `vt.file_report` | fake VT | invocation attribuée à la session Claude, nouvelle observation |
| 15 | Import observations/findings | `WorkContributionService` + `EvidenceRecorder` + `FindingService` | — | observation `workspace_import` ; finding importé `inconclusive` (jamais auto-`supported`) |
| 16 | Bridge rédige deux Notes à deux ancres | `advisor.consult` + propositions de notes | fake Bridge | l'ancre dédupliquée `section:0:paragraph:0001` est refusée `invalid_note_anchor` ; 2 propositions matérialisées à `lead:0001` et `section:1:paragraph:0001` |
| 17 | Acceptation par version/hash | `accept_note_proposal` + porte D4 + décision `note_approve` | — | note A acceptée v1 ; la 2e proposition pinnée sur la version vide est refusée `409 stale_note_proposal`, re-demandée sur v1 puis acceptée v2 |
| 18 | L'humain demande une correction | proposition de note pinée sur v2 | fake Bridge | proposition de correction matérialisée sur `lead:0001`, base v2/SHA |
| 19 | Bridge révise, l'humain accepte | porte D4 + versions d'avis | fake Bridge | avis v3 ; les deux notes restent à leurs ancres, texte A révisé |
| 20 | Vrai PDF Typst | `preview.pdf` + Typst 0.15.1 | — | `%PDF-1.7`, `Content-Disposition: inline` (aperçu iframe), `X-PDF-Lineage` avec SHA d'avis et compilateur |
| 21 | Notes aux deux ancres, références résolvables | `pdftotext` + `verify_opinion_citations` | — | chaque note suit immédiatement son paragraphe d'ancre et précède le paragraphe suivant ; violations de citation = 0 |
| 22 | Interruption | service de session + superviseur H1 | fake Claude CLI | session `stopped`, compteurs d'artefacts inchangés |
| 23 | Reprise sans perte | service de session + superviseur H1 | fake Claude CLI | `ready`, 2 runs hôte, tous les artefacts conservés |
| 24 | Audit final | timeline + findings + enveloppe + échantillons | — | Bridge non-preuve, aucune observation consultative citée, aucun G3 automatique, G2 humain et `bridge_only` conservés |

## Écarts et points à documenter

1. **Matérialisation `base_samples`** : aucune route backend n'expose encore
   `POST /v1/workspaces/{wid}/base-samples/{sha256}`. L'étape 06 appelle donc le
   **vrai** `HttpWorkspaceAgentClient.materialize_base_sample` (même client UDS,
   mêmes contrôles de SHA/taille/mode) avec les octets lus dans la quarantaine
   canonique. La couche API est simulée pour ce seul appel ; la couche sidecar et
   les octets sont réels.
2. **Ordre des deux notes** : la deuxième proposition est pinnée sur la tête
   d'avis au moment de la demande. Accepter la première déplace la tête ; la
   seconde est alors refusée `409 stale_note_proposal`. Le scénario prouve ce
   refus puis re-demande la note sur la nouvelle version — il ne contourne pas
   la porte. C'est le comportement attendu de la garde de version.
3. **Transfert Codex→Claude** : l'état terminal de l'ancienne session peut être
   `stopped` ou `lost` selon que le processus simulé sort avant ou après la
   réconciliation ; les deux sont terminaux et le test vérifie que les artefacts
   ne changent pas. Aucune relance automatique.
4. **Profils CLI factices** : `FakeProfile` est enregistré sous `codex_cli` et
   `claude_code`. Son probe est mis en cache dans le support de test, comme le
   cache de faits des profils de production, sinon le hash de probe pouvait
   changer entre le probe backend et le prepare hôte (échec de livraison du
   secret observé une fois, corrigé côté fixture).
5. **Pas de réseau** : le faux VT utilise `httpx.MockTransport` pour le
   téléchargement ; aucun adaptateur HTTP réel n'est instancié pour Bridge/LLM.
6. **Ancres de Notes** : la proposition et l'acceptation n'acceptent que les
   ancres de paragraphes que la projection lecteur rend réellement
   (`publication_renderable_body_anchors`, source unique partagée avec le
   renderer Typst). Un paragraphe de section dupliquant le lead — et, sur un
   article à « Vue d'ensemble » extraite, la section 0 — est refusé
   `invalid_note_anchor` au lieu d'être silencieusement déplacé en note de fin.
   L'étape 20 sert en outre l'aperçu en `Content-Disposition: inline` pour
   l'iframe du cockpit.

## Commandes exécutées

```bash
cd backend
export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst
export FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
export TMPDIR=$HOME/typst-tmp
flock /tmp/m24-pg.lock make test-integration \
  INTEGRATION_TEST_PATH=tests/integration/test_acceptance_scenario_postgres.py \
  INTEGRATION_PYTEST_ARGS="-q --tb=short"
# journal lisible :
#   INTEGRATION_PYTEST_ARGS="-q --tb=short -s"
```

Résultat observé : `1 passed in 37.33s` (deux runs complets consécutifs PASS,
35–37 s, après la correction du probe factice).

Lint/types :

```bash
PY=/home/nill/perso/work/AutoWork/backend/.venv/bin/python
$PY -m ruff check tests/acceptance/scenario_support/ tests/integration/test_acceptance_scenario_postgres.py
$PY -m ruff format --check tests/acceptance/scenario_support/ tests/integration/test_acceptance_scenario_postgres.py
PYTHONPATH=$PWD/src $PY -m mypy tests/acceptance/scenario_support/harness.py \
  tests/integration/test_acceptance_scenario_postgres.py
```

Résultat observé : `All checks passed!`, `3 files already formatted`,
`Success: no issues found in 2 source files`.

## Fichiers

- `backend/tests/acceptance/scenario_support/__init__.py`
- `backend/tests/acceptance/scenario_support/harness.py`
- `backend/tests/integration/test_acceptance_scenario_postgres.py`
- `docs/m24/acceptance-scenario.md` (ce journal)

Aucun code applicatif, migration, routeur ou autre fichier de
`backend/tests/acceptance/` n'a été modifié.

## Attestation réseau simulée (FIX-S)

Depuis la porte « CLI network mode » (R3-B1), le backend refuse d'ouvrir une session dont l'hôte n'atteste pas `network_mode=isolated_egress` + `loopback_isolated`. Le profil CLI factice du scénario (`FakeProfile(simulate_network_attestation=True)`) **simule** cette attestation : l'étape 08 prouve le câblage de la porte, jamais une isolation réseau réelle (celle-ci est vérifiée séparément par `tests/workspace_agent/test_sessions_network.py` et reste non prouvée avec un vrai CLI : spike H0).
