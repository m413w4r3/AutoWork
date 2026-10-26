# M06-P/R — Prévol runtime de la prochaine investigation (lecture code/docs)

Statut : **PARTIAL — aucune lecture live.** Produit par l'enfant
`RUNTIME-PREFLIGHT` (Paseo `3b650779`, codex/gpt-6-luna high, lecture seule)
sur `main@af819b2e`, consolidé par le parent. Aucun accès PostgreSQL/MinIO/
Redis live, aucun appel VT/Bridge/Shodan, aucun script de prévol exécuté.
Libellés : **CODE** = vérifié dans le code à cette base ; **DOC** = constat
daté, non revérifié ; **UNKNOWN** = exige une lecture live autorisée ou une
décision.

## 1. Scénario : nouvelle investigation sur le même sujet

Sujet : Blinder Tunnel, `subject_id = d22822d4-27ce-47d2-9cc5-f84cf312986a`.
L'historique (Investigation 12 `5f66ebfb…`, Run 12 `ec7a6807…`) n'est ni
réinitialisé ni réécrit.

1. **Lignée source — UNKNOWN bloquant.** La nouvelle investigation exige un
   `production_run` **distinct** (`analyst_investigations.production_run_id`
   est unique — CODE `models/production.py:336-340`,
   `api/investigation.py:923-952`) portant son propre artefact de synthèse
   `VERIFIED`, stage `synthesis`, et un `canonical_sha256` égal au contenu gelé
   (CODE `api/investigation.py:845-864,2708-2721`). La possibilité de porter la
   synthèse approuvée sur un nouveau run sans recalcul ni changement de lignée
   n'est pas établie : **décision de conception requise** ; ne pas copier ni
   réétiqueter un artefact.
2. **Handoff — CODE.** `POST /api/subjects/{id}/investigations` avec
   `production_run_id`, `synthesis_artifact_id`, `synthesis_sha256` ; écrit et
   dispatch un job : action future autorisée, pas un prévol
   (`api/investigation.py:179-186,923-1005`).
3. **Politique OSINT_OPEN — CODE / état UNKNOWN.** Lire
   `GET /api/osint-open-policy/current?scope_kind=subject_set&scope_ref=<subject>`.
   Si aucune politique active adaptée : **une** activation par un opérateur
   humain vérifié (`POST /api/osint-open-policy/activate`, capacités
   explicites ; refus si une politique active existe déjà —
   `api/osint_policy.py:221-257`, `application/osint_open_policy.py:78-139`).
4. **Démarrage unique — CODE.** Au démarrage, la politique applicable est
   intersectée avec la divulgation du snapshot source et épinglée dans
   l'enveloppe héritée ; l'opérateur ne crée pas l'enveloppe à la main
   (`api/investigation.py:866-902`, `application/osint_open_policy.py:300-371`).

## 2. Lectures live nécessaires (après autorisation distincte)

- Inventaire SQL en transaction `READ ONLY` déjà rédigé, jamais exécuté :
  [`RUN_STATE.md`](RUN_STATE.md) §inventaire (l. 122-330) — runs/synthèses
  (id, version, blob, SHA), investigations du sujet, échantillons et blobs,
  qualifications d'association, jobs, invocations `NEEDS_REVIEW`, tentatives
  d'acquisition, statut des transports Bridge, enveloppes G2, versions
  d'opinion. Exécution : `psql "$READONLY_DSN" -X -v ON_ERROR_STOP=1`.
- Politique courante : endpoint de lecture ci-dessus (non couverte par le SQL).
- Intégrité MinIO (existence + SHA des objets) : vérification de stockage
  séparément autorisée ; une ligne de blob ne la prouve pas.
- Une fois le tuple canonique connu :
  `make preflight-supervised-first-run PREFLIGHT_ARGS="--subject-id … --production-run-id … --synthesis-artifact-id … --version … --sha256 … --tlp … --format markdown"`.
  La cible `-stack` sonde aussi Redis, MinIO (HEAD) et Bridge `GET /models`,
  et exige une base attestée jetable : **non couverte** par une simple
  autorisation de lecture DB.

## 3. Analyse locale et confinement (configuration non sensible)

- CODE : `analysis-worker` UID/GID 10001, rootfs en lecture seule, `/tmp`
  `noexec,nosuid,nodev`, capacités supprimées, `no-new-privileges`, 2 GiB /
  128 PIDs / 2 CPU ; `ANALYSIS_WORKER_ENABLED=false` par défaut
  (`compose.yaml:405-431,452-489`).
- CODE : bwrap (net/pid/ipc/uts) + seccomp anti-réseau quand disponible ;
  l'échec du confinement empêche l'annonce « measured-ready »
  (`infrastructure/local_command_runner.py:233-270,446-460,676-708`). La sonde
  `bwrap --version` du prévol n'atteste pas le confinement.
- CODE : Ghidra désactivé par défaut, attend Ghidra 12.1.4 / Java 21.0.12.1.
- UNKNOWN : disponibilité réelle des workers, versions, rulesets, attestation
  de confinement, image Ghidra. Aucune capacité locale READY vérifiée en live.
- VT/Bridge : voir le registre S3 ; aucune famille n'est READY sans tranche
  S3 intégrée et approuvée.

## 4. Anomalies documentées à réconcilier (DOC)

- Run 12 : invocation Bridge refusée à l'admission budgétaire
  (`tool_seconds`) **avant** transport — non transmise, pas une ambiguïté.
- Invocations `needs_review` laissées à revue humaine ; IDs/états actuels
  UNKNOWN.
- Opinion v61 en brouillon ; revue, PDF QA et audit final inachevés ; YARA
  `ONE_RULE_NOT_VALIDATED` ; Suricata `SURICATA_NOT_JUSTIFIED` ; rendus C14/C15
  au-delà de 96 000 octets (`docs/operations/run12-editorial-closure-2026-10-22.md`).

## 5. Observateur indépendant

Un observateur humain nommé, accès lecture seule, journal de défauts séparé
en ajout seul (horodatage, checkpoint, job/requête, version/hash source,
défaut, référence de preuve). Il ne répare, ne relance ni n'autorise aucun
effet externe (`RUN_RESTART_PLAN.md:102-109`).

## Prochaine action permise

Inventaire DB en lecture seule **après autorisation explicite**, puis
contrôles d'intégrité objet et de capacités runtime séparément autorisés.
Aucune nouvelle investigation avant résolution de la lignée, de la
réconciliation, de la politique/divulgation et des capacités requises.
