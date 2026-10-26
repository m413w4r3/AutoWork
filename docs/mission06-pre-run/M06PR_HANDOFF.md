# Mission 06-P/R — handoff (2026-10-26)

## 0. Mission 06-R FINAL — état courant (prime sur §1–§5)

Parent : Claude Code `d6ef7e5e` (`claude-opus-5-5`, non Luna) ; enfants
`codex/gpt-6-luna` xhigh. Aucun push, déploiement, migration live, appel
fournisseur réel, téléchargement ni analyse de malware. Base de départ
`main` = `origin/main` = `0824df0d` (le §1 ci-dessous, « origin/main =
af819b2e », est historique).

**Git.** `main` local (non poussé) = `0824df0d` + merge `66d59766`
(correctif VT-READ, APPROVED à `4e6c4b55`) + docs `6f67f2cd`
([`recon/RUN_LINEAGE_PREFLIGHT.md`](recon/RUN_LINEAGE_PREFLIGHT.md)) et
`03083117` ([`E2E_OSINT_AUDIT.md`](E2E_OSINT_AUDIT.md)). Delta applicatif
net vs `0824df0d` : `backend/src` +267/−39 (4 fichiers modifiés, aucun
créé), tests +404/−22, migrations 0.

**CI #40363332282 (`0824df0d`) : FAILED** — job navigateur J2
(`investigation-opinion-repin.spec.ts:267`, `vt_file_reports` 0≠1).
Cause : S3-VT-READ refusait toute lecture VT d'un hash sans
`sample_association` (`sample_hash_not_associated_with_investigation`),
donc tout pivot sur un hash d'article ou un hit non téléchargé. Corrigé par
`66d59766` (ancrage par observation canonique de l'enquête, restrictions
sous verrou, HUMAN_G2 inclus) ; J2 local **1 passed** sur `66d59766`. CI
non relancée (push non autorisé).

| Lot | Branche @SHA | Revue indépendante | Statut |
|---|---|---|---|
| VT-READ fix | `m06f/vt-read-source-fix@4e6c4b55` | `3a868522` APPROVED (2ᵉ tour) | **intégré** |
| S3-BRIDGE | `m06f/s3-bridge@7825cde8` (+1219/−72 app, 0 migration, 0 fichier créé) | `842230ae` REVISE (2 cycles épuisés) | **BLOCKED**, non intégré |
| S3-VT-DOWNLOAD | `m06f/s3-vt-download@571aeeef` (+1133/−55 app, migration `0005` +288) | non revu (PG rouge) | **BLOCKED**, non intégré |
| E2E-AUDIT | `03083117` | `49cabcda` adversarial | rapport intégré |
| RUNTIME-PREFLIGHT | `6f67f2cd` | `145a831d` lecture seule | `EXISTING_PATH` |

**BRIDGE — défaut restant (BLOCKER).** Les liaisons de famille Bridge
(`infrastructure/investigation_runtime_ports.py:149-166`) ne déclarent ni
`requires_source_refs` ni résolveur : le consume
(`repositories/external_effects.py:1120-1129`) ne compare pas les
références attendues, et un appelant peut reconstruire la liaison avec des
`evidence_refs` vides. Repro PG rouge :
`m06f/s3-bridge-blocked-repro@62fdaa2c`,
`test_external_effects_postgres.py::test_bridge_submission_cannot_omit_restricted_prompt_source_reference`
(« restricted passage without a source ref was dispatched »). Correctif
attendu : dériver côté serveur les refs du ModelRun/tour persisté et les
comparer sous verrou. Acquis à `7825cde8` (PG 423, unitaires 5109, mypy
OK) : octets permis = octets envoyés (`client.send` de la requête figée),
seul un 429 complet est rejouable, autre erreur post-handoff ⇒
`NEEDS_REVIEW`, gateway de production restée sur le client simple,
conversations générales sur gateway simple, enquête (API+worker) sur
gateway coordonnée, refs `source_document` de l'advisor, OSINT_OPEN hérité
+ révocation testés.

**VT-DOWNLOAD — défaut restant.** PG `571aeeef` : 419 passed / 5 failed.
Le téléchargement est refusé avant transport dans la composition réelle
(raison du coordinateur non journalisée) ; contrat
`sample.external_llm_allowed` contradictoire (acquisition `False`, oracle
E2E `True`) — **décision de politique requise**. Repro minimal :
`test_investigation_vertical_postgres.py::test_real_postgres_tool_path_classifies_vt_download_route_404_as_needs_review`.
Migration `0005` (tentative de dispatch pré-envoi + liens claim) jugée
finale ; tests de migration verts sur la branche. Estimation : 3–5
fichiers, <100 LOC, sans schéma.

**Tests réellement exécutés par le parent sur `main@66d59766`**
(Typst épinglé, `FONT_BUNDLE_ROOT`, venv `/usr/bin/python3.12`, PG jetable
55432) : `make lint-backend` OK ; `make typecheck-backend` 843 OK ;
`make test-backend` 5093 passed/2 skipped ; `make test-integration` 414
passed (dont `test_migrations.py`, chaîne `0001→0004`) ; workflow FAST 22
et FULL 32 passed ; `make test-ci-scripts` OK ; J2 navigateur 1 passed
(`CHOKIDAR_USEPOLLING=1`, l'hôte a épuisé ses 128 instances inotify). Le
test PDF instable n'a pas échoué. Frontend non lancé (aucun chemin UI
modifié).

**`test_local_analysis_commands` (2 échecs hors checkout principal) :
environnement, pas code.** Un venv neuf créé par `uv … --python 3.12`
prend Miniconda (`/home/nill/miniconda3`), absent des montages bwrap ⇒
`BWRAP_PROBE_FAILED` ⇒ `filesystem_confinement_unavailable` (fail-closed
correct). Même archive avec `--python /usr/bin/python3.12` : 23/23.

**Contrainte d'outillage.** Le bac à sable des enfants Codex n'atteint pas
PostgreSQL ; le passage en `full-access` a été refusé par le classifieur
de sécurité. Tous les résultats PG ci-dessus sont exécutés par le parent.

### Verdicts

```text
S3_CORE                     : APPROVED (conservé)
S3_VT_READ                  : READY (conservé ; correctif d'ancrage APPROVED et intégré)
S3_BRIDGE                   : BLOCKED
S3_VT_DOWNLOAD              : BLOCKED
OSINT_OPEN_NO_REPEAT_G2     : PASS
CANDIDATE_LOCAL_ANALYSIS    : PARTIAL
E2E_OSINT_CONTINUOUS        : BLOCKED
AUDIT_DECISION_CHAIN        : PARTIAL
MIGRATION_CHAIN             : PASS (0001→0004 sur main ; 0005 non intégrée)
CI_LATEST                   : FAILED (0824df0d ; correctif local non poussé)
LOCAL_TEST_GATES            : PASS
ANALYSIS_WORKER_READINESS   : PARTIAL (code et sonde bwrap hôte OK ; worker live UNKNOWN)
REAL_RUN_TECHNICAL_GO       : NO_GO
REAL_RUN_OPERATIONAL_GO     : NO_GO
REAL_INVESTIGATION_STARTED  : NO
```

### Blocages réels du run

| # | Blocage | Preuve | Levée |
|---|---|---|---|
| 1 | Envois Bridge/advisor non soumis à S3 sur `main` ; branche candidate a un contournement des refs de source | repro `62fdaa2c` | correctif serveur des refs + revue |
| 2 | `vt.download_sample` non soumis à S3 sur `main` ; branche candidate refuse avant transport | 5 nœuds PG rouges `571aeeef` | décision `external_llm_allowed` + câblage d'admission + revue |
| 3 | Aucune preuve PG continue sous OSINT_POLICY (download + analyse + Bridge) | `E2E_OSINT_AUDIT.md` | après 1–2, un scénario PG continu |
| 4 | CI GitHub rouge sur `origin/main` | run 40363332282 | push autorisé de `main` local |
| 5 | Données live UNKNOWN (lignée, politique, échantillons, worker) | `recon/RUN_LINEAGE_PREFLIGHT.md` §Q2 | lectures autorisées |

Non bloquant pour le run : absence de motif persisté de transition
d'hypothèse et projection d'audit sans claims/acquisitions (PARTIAL,
documenté dans `E2E_OSINT_AUDIT.md`), cutover Advisor v4, Shodan/Retrohunt.

### Démarrage prévu (après levée des blocages et autorisations)

Nouvelle lignée par le chemin existant (`recon/RUN_LINEAGE_PREFLIGHT.md`) :
lectures read-only (inventaire `recon/RUN_STATE.md` §7, GET sujet/édition/
runs) → `POST /api/editions/{edition_id}/production/batches` (sujet
`d22822d4-…`, `Idempotency-Key` neuve ; réutilisation par `input_hash`,
appels LLM seulement sur cache manquant) → synthèse `VERIFIED` du nouveau
run (`canonical_sha256`) → GET politique OSINT_OPEN, activation par
opérateur humain vérifié si absente → `POST /api/subjects/{id}/investigations`.
Les investigations antérieures ne sont ni modifiées ni supprimées.

---

Session parente M06-P/R : Claude Code `b078131f`, `claude/claude-opus-5-5` (non Luna).
Tous les enfants : `codex/gpt-6-luna` (xhigh pour implémentation/revue,
high pour éclaireurs). **Aucun push, déploiement, migration live, appel
VT/Bridge/Shodan réel, téléchargement ni analyse de malware.** Décision
utilisateur (≈14:30) : terminer VT-READ dans cette session, puis passer
BRIDGE et VT-DOWNLOAD à une nouvelle session avec un parent frais.

## 1. Registre Git

`origin/main` = `af819b2e` (CI rouge, voir §3). `main` local, non poussé :

| SHA | Contenu | Revue indépendante |
|---|---|---|
| `a832902b` | Régression CI : hôtes loopback des émulateurs, revoke humain, mypy, test non-loopback épinglé | `b50f23f4` **APPROVED** |
| `796292a7` | S3-CORE (reprise de `2c6c6074` + 3 MAJOR + capacités) | `27e3f730` **APPROVED** (5ᵉ tour) |
| `044178b2`, `6eadb667` | Docs (carte des familles, prévol code-only, registre, ledger) | — |
| `1c28892c` | Merge S3-VT-READ (`b89a9191`) incluant les extensions core | core cumulé `27e3f730` **APPROVED** à `1df683cb` ; VT-READ `45de4bd5` **APPROVED** à `b89a9191` |

Les commits core cherry-pickés dans VT-READ portent d'autres SHA que sur
`m06r/s3-bridge` : `deb41cd3`=`c36bb116`, `d3860fae`=`911f8738`,
`6b404652`=`90f73648`, `ccf9ee7f`=`0d067f91`, `3d463edd`=`1df683cb`,
`15ee7e7b`⊃`1186c960`. Un rebase de BRIDGE sur `main` doit les écarter
(patchs identiques). Le commit `5e08413e` (wip de relais) est intégré avec
ses corrections ; seul l'arbre final `b89a9191` a été revu et testé.

Branches non intégrées :

- `m06r/s3-bridge@783c0ebb` — **REVISE implicite / non revu** :
  `b2751d8e` (tranche Bridge : `integrations/models.py`, `bridge_investigator.py`,
  `model_conversations.py`, `model_gateway.py`, tests) + `783c0ebb` wip
  (3 tests ciblés en échec sur le chemin HTTP gated : divergence d'octets de
  la requête figée dans le hook). Câblage `investigation_service.py` /
  `investigation_tools.py` / composition **non fait** (prévu après VT-READ).
- VT-DOWNLOAD : **non commencé** ; nécessite une migration additive `0005`
  (`sample_acquisition_dispatch_attempts` pré-envoi, FK du claim) — voir
  [`recon/S3_FAMILY_MAP.md`](recon/S3_FAMILY_MAP.md) §B.

## 2. Décisions d'arbitrage parent (à conserver)

1. Modèle de menace S3 : protection contre le code famille/adaptateur via API
   publiques ou `_privées` et contre les valeurs forgées/copiées/rejouées ;
   introspection en processus hors périmètre. Chaque revue de famille vérifie
   client brut privé, un seul appel juste après la capacité, ni retry ni
   redirection suivie ni chemin alternatif.
2. **Fail-closed après handoff** : aucune « preuve de non-envoi » après la
   remise au transport ; tout échec post-handoff = `NEEDS_REVIEW`.
3. HTTP 429 **complet** = claim `COMPLETED` (rate-limited) ; le retry borné
   existant est une **nouvelle occurrence** avec son propre claim.
4. Digest persisté sans credential : valeurs d'en-têtes masquées sauf liste
   sûre (content-type, content-encoding, accept, accept-encoding,
   content-length, user-agent, idempotency-key) ; paramètres masqués par
   alias exacts ou déclaration de famille (ex. VT `apikey`) ;
   `canonical_request` refuse tout nom de credential déclaré.
5. **S3 ajoute des gardes, ne les remplace jamais** (pre-dispatch, egress
   advisor, plafond de divulgation conservés).
6. Restrictions par source résolues sous verrou de consume
   (`sample`, `sample_association`, `sample_hash`, `source_document`,
   `evidence_observation`) ; référence non résolue ⇒ refus pour les
   soumissions modèle ; `license_restriction` = veto sans divulgation du texte.

## 3. Tests réellement exécutés par le parent (environnement : Typst épinglé, `FONT_BUNDLE_ROOT`, PG jetable 55432)

| SHA | ruff/format | mypy | backend | intégration PG | workflow FAST+FULL |
|---|---|---|---|---|---|
| `af819b2e` | — | ❌ (CI) | 4965 ✅ | 361 / 1 ❌ | 11 ❌ (CI) |
| `a832902b` (lot) | ✅ | ✅ 834 | — | 362 ✅ (`e775b30a`) | 31 ✅ (`db9b19f9`) |
| `796292a7` | ✅ | ✅ 840 | 5031 + 2 env | 373 ✅ | 31 ✅ |
| `1df683cb` (core cumulé) | ✅ | ✅ 840 | 5075 + 2 env | 394 ✅ | 31 ✅ |
| `bbe4c342` | ✅ | ✅ 843 | 5084 + 2 env | 409 ✅ | 31 ✅ |
| `b89a9191` | ✅ | ✅ 843 | `test_investigation_acquisition.py` 20 ✅ | (= `bbe4c342` + tests) | 31 ✅ |

Chaîne de migrations `0001→0004` (upgrade, downgrade `0003`/`0002`, réupgrade)
validée sur base jetable à `2d39fe34` ; `backend/migrations` inchangé depuis.
« 2 env » = `tests/test_local_analysis_commands.py` (2 tests), en échec à
l'identique sur une archive de `main` hors checkout principal (confinement
bwrap non mesurable sur cet hôte) ; non liés à S3.
`test_publication_render_repository.py::…[source_blob_id]` a échoué une fois
sous charge concurrente puis passé 3/3 isolément : instable, non lié.
Le CI GitHub n'a pas été relancé sur `main` local (pas de push autorisé).

## 4. OSINT_OPEN sans approbations répétées (preuves par tests, sans fournisseur réel)

- VT lectures : `tests/integration/test_virustotal_external_effects_postgres.py`
  exécute plusieurs lectures (`vt.behaviour` puis `vt.behaviour_pcap`) sous
  enveloppe héritée OSINT_OPEN, **sans nouvelle approbation humaine**, un
  claim par requête physique, credential absent des claims et journaux ;
  révocation / sources restreintes ⇒ refus et zéro envoi.
- Bridge, téléchargement admissible et analyse d'un `candidate` sans G3 :
  **non démontrés** via les routes migrées (familles non migrées).

## 5. Prochaine session (ordre)

1. Nouveau parent ; base `main@1c28892c` (local, non poussé).
2. BRIDGE : session Luna xhigh fraîche sur `m06r/s3-bridge@783c0ebb`,
   rebase sur `main` (écarter les commits core dupliqués), corriger les 3
   tests ciblés, câbler `InvestigationService.run` (enveloppe, réservation
   Bridge, sources) et l'advisor (autorité d'invocation), suites complètes,
   revue indépendante xhigh.
3. VT-DOWNLOAD : migration additive `0005`, tentative de dispatch pré-envoi,
   un claim par saut de redirection, réutilisation locale vérifiée hors S3,
   quarantaine/SHA-256 ; revue ; validation de chaîne sur base jetable.
4. E2E-REVIEW adversarial (PG, politique, non-régression) sur le `main`
   intégré ; démonstration OSINT complète (Bridge, download, G3 non bloquant).
5. Autorisations distinctes nécessaires avant le run réel : push/CI,
   lecture live en lecture seule (inventaire `recon/RUN_STATE.md`), décision
   de lignée de synthèse (nouveau `production_run`), activation OSINT_OPEN
   par un humain vérifié, smokes live VT/Bridge.
