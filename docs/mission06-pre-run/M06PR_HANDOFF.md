# Mission 06-P/R — handoff (2026-10-26)

Session parente : Claude Code `b078131f`, `claude/claude-opus-5-5` (non Luna).
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
