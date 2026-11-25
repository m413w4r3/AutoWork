# M24.3 — Consolidation du socle avant le premier run réel

Branche locale `m24-3/consolidation`. Aucun push, merge dans `main`, déploiement,
`make up-clean` ni suppression de volume n'a été effectué. La stack vivante n'a pas été modifiée.

## 1. SHA

| | |
|---|---|
| SHA initial (`main`) | `d7dd3a2d2dc3f7340019063bad38e6ddeb274cb9` |
| Dernier commit de code testé | `5e5557e66095e84bfd3983014aa84472640a996e` |
| SHA final | le commit qui ajoute ce rapport (voir `git log -1`) ; il ne change que ce fichier |

Diff résumé `d7dd3a2d..5e5557e6` : 31 fichiers, +6 436 / −660, 26 commits (13 fichiers ajoutés, 18 modifiés).
Aucune révision Alembic ajoutée, aucune révision 0002–0029 modifiée, `safe_local_reset.py` et les modèles ORM intacts,
aucun endpoint, contrat Bridge ni fichier frontend applicatif modifié (seul le spec e2e J2 change côté frontend).

Les statuts ci-dessous distinguent **REJOUÉ** (commande lancée par l'orchestrateur sur le code intégré),
**ATTRIBUÉ** (rapporté par un sous-agent, non rejoué) et **NON EXÉCUTÉ**.

## 2. Lot 1A — J2

`frontend/e2e-real/investigation-opinion-repin.spec.ts` : le nouveau libellé « Accès externes : non accordés » est conservé.
L'assertion vérifie l'état sémantique : `GET /envelope` renvoie `approved=false`, `currently_authorized=false`,
`allowed_providers=[]`, `acquire_hits_allowed=false` ; le bouton « Approuver » est présent ; aucune opération externe n'a été exécutée
(`job_runs` vide, aucun compteur Bridge/VT/Shodan du serveur d'émulation, écrits par `investigation_browser_server.py` après exécution) ;
après approbation, l'état accessible passe à « Accès externes : actifs ».

Résultat REJOUÉ : `make test-workflow-browser-investigation` sur PostgreSQL jetable avec PDF Typst réel : 1 test passé (160 s),
vérification PDF Poppler passée. Exécuté depuis un worktree propre du commit `31776683` ; le delta ultérieur ne touche que des scripts et des docs.

## 3. Lot 1B — R1e

### Correction
Une **porte lecteurs/rédacteur par workspace** dans `SessionSupervisor` (pas de verrou global) :
- les écritures `agent`/`system` (PUT fichiers, routes générées, capture/collect) prennent un bail **partagé** de la lecture du corps jusqu'à la publication ;
- `close()` et `stop()` prennent le bail **exclusif** borné (5 s) avant d'effacer les formes de détection ; sur dépassement `close()` renvoie un 409 `session_busy` typé, réessayable, et conserve les formes ;
- `stop()` envoie d'abord le kill et coupe les canaux/egress (urgence jamais bloquée), puis attend le drain global, nettoie toutes les sessions et ne vide les formes que des workspaces drainés ;
- toute suppression d'une source de credential fournisseur capture d'abord ses formes ; elles restent en mémoire jusqu'à un effacement sous exclusion ;
- `interrupt()`, la finalisation du monitor, `prepare`/`start`/`resume`/`instruction` n'attendent jamais la porte ;
- `collect()` est refusé après `stop()` ; le rejeu après redémarrage garde le comportement historique (200) ;
- les écritures humaines (`ui`) et l'ordre des verrous (porte avant verrous de session) sont documentés dans le code.

Taille : sidecar +603 / −223 lignes (+380 net, dont `supervisor.py` +456/−156). Tests : `backend/tests/workspace_agent/test_sessions_r1e.py` (barrières `threading`, aucun sleep de minutage), prouvés rouges sur l'ancien code.

### Revues sécurité indépendantes (4 rounds, fresh session chacune)
| Round | Cible | Verdict | Résultat |
|---|---|---|---|
| R2-a | `13f386e7` (admission par session) | NO-GO | F1 session préparée après l'instantané ; F2 `collect()` sans bail après `stop()` ; F3 régression : workspace dont la seule session est `CLOSED` refusait toute écriture système ; F4 `stop()` timeout sans nettoyage |
| R2-b | `e72f30f0` (porte par workspace) | NO-GO | formes ajoutées entre contrôle et publication (corrigé) ; point « secret d'une session fermée non détecté » = décision documentée (§ 3 ci-dessous) |
| R2-c | `88c112a2` | NO-GO | `stop()` supprimait la source du credential sans capturer ses formes ; `interrupt()` attendait la porte ; monitor sans échéance ; contrat `collect` après redémarrage changé |
| R2-d | `6e4ce9c3` | GO avec réserves | aucune violation des 7 invariants ; réserve : `stop()` attendait le bail avant le kill — corrigé en `5d5109a2` (test rouge/vert) |

Le correctif final est `5d5109a2` (intégré `d0b1195f`). Suite sidecar : 788 tests selon l'agent (ATTRIBUÉ) ; rejouée dans la suite backend complète (§ 7).

### Décisions et réserves à confirmer par l'utilisateur
1. **Secret d'une session fermée.** `close()` efface les formes de détection (comportement historique et position M24.2 §10) : une écriture **ultérieure** contenant l'ancien secret est acceptée. Justification : le broker refuse toute session non active (`agentic_tool_broker.py` ~631). R1e concerne l'écriture **déjà admise** ; elle est fermée. Documenté dans le code et les noms de tests. Si le secret de session doit rester détecté après fermeture, cela exige de conserver les formes en mémoire et contredit l'effacement exigé.
2. **`stop()` et workspace sans session.** `stop()` ne draine que les workspaces de ses sessions. Les formes sont strictement par session ; un PUT admis dans un workspace sans session ne peut donc pas fuiter un secret de session, mais peut publier pendant ou juste après le retour de `stop()`. Classé mineur par R3b ; `stop()` ne garantit pas « aucun PUT admis ne publie après son retour ».
3. **`POST /base-samples`** (`materialize_sample`) écrit sans contrôle de secret, comme sur `d7dd3a2d`. Aucune session ne l'appelle ; un composant backend autorisé le peut. Hors périmètre R1e.
4. **Bornes** : la borne de 5 s couvre l'attente des baux, pas le nettoyage des canaux/processus (jusqu'à 2 s par relais).
5. **Point d'API** : `SessionSupervisor.admit_non_ui_file_write()` (ou équivalent) est une méthode publique Python appelée uniquement par le serveur HTTP ; aucun contrat HTTP n'est modifié hors erreurs 409 typées.

## 4. Lot 2 — Architecture Alembic avant / après

| | Avant (`d7dd3a2d`) | Après |
|---|---|---|
| `env.py` | 423 lignes ; ~20 ensembles `_*_TABLES/_*_COLUMNS/_*_CHECKS`, `_metadata_for_frozen_baseline()`, remplacement temporaire de `Base.metadata` pendant `run_migrations()` | 93 lignes ; `target_metadata = Base.metadata`, `context.run_migrations()` direct, modes offline et online conservés |
| `0001_baseline.py` | 444 lignes ; `Base.metadata.create_all` sur les tables ORM courantes | 413 lignes ; exécute le DDL figé ; fonctions/triggers de protection et gardes C2 inchangés ; plus aucun import de `cti_app.infrastructure.database.models` |
| Schéma 0001 | dérivé de l'ORM à l'exécution | `backend/migrations/frozen/baseline_0001.py` : 2 292 lignes de DDL statique (95 tables, 243 instructions), en-tête « IMMUTABLE », sans dépendance ORM, généré par `scripts/dev/freeze_0001_baseline.py` (SHA source `d7dd3a2d`, SHA-256 du module `84f4f3a08fe5cae84c5b6fad011265c2f136b99c4bbf3a8eb33d751a09f3d120`, régénéré deux fois à l'identique) |

Le module figé est de la donnée générée ; ce qui est supprimé, c'est la dépendance de la baseline à l'état futur des modèles. `0001` charge ce module par `importlib` car `prepend_sys_path = src` rend `migrations` non importable ; la revue R1 recommande de le garder (robuste en image Docker, hors cwd, mode `--sql`).

Audit des révisions 0002–0029 (agent + R1 indépendante) : aucune ne lisait `Base.metadata` ni ne dépendait de son remplacement (0004/0005 utilisent des objets `.__table__` importés ; 0007 n'a qu'un commentaire périmé, laissé tel quel — la mission interdit de retoucher les autres révisions).

## 5. Preuve comparative des schémas

Capture PostgreSQL (`backend/tests/support/schema_snapshot.py`, `scripts/dev/m24_3_schema_checkpoints.py`) : colonnes (type, défaut, nullabilité, identity, génération, collation), contraintes (`pg_get_constraintdef`), index, triggers et état, fonctions, séquences, enums, extensions, vues, commentaires, contenu de `alembic_version`. Normalisation explicite : OID, propriétaires/ACL et ordre des collections.

REJOUÉ indépendamment par l'orchestrateur sur le code intégré : 6 captures sur base jetable `m243_*`, comparées aux références capturées sur `d7dd3a2d` — **les 6 sont `identical` et leurs SHA-256 sont octet pour octet ceux de la référence** :

| Checkpoint | SHA-256 |
|---|---|
| `0001_baseline` | `92e4486f674c881a548111365740909e4574ec13034ec23a1ca7261cf3a0c0d8` |
| `0002_c2_experiments` | `dadc556993be4dbc6ae81d78a842b14c6123c95a619e07591447d2630c3a40cb` |
| `0008b_version_length` | `aebf9357ff9fa274137f3545855ce0d961934f34ea7ef2c5e0799c30dc4ea6bc` |
| `0018_editorial_enrichment_element_operations` | `8db02c3d62f1964fc38b9b2fe98e1f796c40404a8c3b798b38788c152ec8882d` |
| `0026_workspace_note_annotations` | `e2eabae58b9b2ecd1706dcd2a15126c03c682b5848135f4fd6f0f73649b29f4b` |
| `0029_note_anchor_identity` | `53518a906a63486c74994db1c690668d0694e1c8cb2dbbc2fa7a00666f4316f4` |

Non-vacuité REJOUÉE : en retirant un élément de colonnes, contraintes, index, triggers ou fonctions, ou en modifiant un champ de colonne dans une copie du snapshot `0029`, `diff_snapshots` détecte chaque écart (le diff est bruyant car positionnel ; cosmétique).

SQL offline de `0001` (`alembic upgrade --sql`) : ATTRIBUÉ (agent T2-1) et recoupé par R1 — équivalent à `d7dd3a2d` après normalisation de l'ordre non déterministe des statements/contraintes (291 statements comparés).

Angles morts de l'oracle, jugés absents du DDL 0001 par R1 : propriétaires/ACL, valeurs courantes des séquences, `reloptions`, tablespaces, replica identity, RLS, domaines.

## 6. Lot 3 — Résultats de migration

- Tests d'intégration migrations (`backend/tests/integration/test_migrations.py` + `test_schema_snapshot.py`) : REJOUÉS dans la suite d'intégration finale.
- Oracles 1–11 : 1, 4, 6 déjà couverts ; 2, 5, 7, 8, 9, 10, 11 renforcés ; 3 (tête unique == `0029_note_anchor_identity`) nouveau ; compatibilité C2 inchangée (base vide, estampillée `0001`, objets C2 manuels canoniques, partiels/incompatibles, downgrade refusé avec données).
- Oracle 12 : `make test-migrations-compose` — build systématique du service Compose `migrate` depuis le checkout sous un tag jetable, contrôle du SHA-256 de la baseline dans l'image, `upgrade head` sur PostgreSQL tmpfs dans un projet `m243-migcheck-*`, second upgrade et dump identique. REJOUÉ sur `5e5557e6` : tête unique `0029`, 131 tables, 29 fonctions, 71 triggers.
- **Dérive ORM↔migration préexistante révélée** par le nouvel oracle d'autogenerate : l'ORM déclare `server_default '[]'::jsonb` sur `investigation_note_proposals.cited_finding_refs` et `cited_observation_refs`, absents de la migration 0025. Le test épingle exactement ces deux écarts (toute autre dérive, ou leur disparition, le fait échouer). Pas de nouvelle migration (schéma final identique, interdit par la mission). Risque : un INSERT qui omettrait ces colonnes échouerait sur `NOT NULL` ; non vérifié si du code le fait. À décider par l'humain.
- Limite connue de cet oracle : SQLAlchemy ignore les FK des tables en cycle (avertissement) ; les FK restent couvertes par le comparateur structurel.

## 7. Résultats des tests (code intégré)

REJOUÉS sur `5e5557e6` : `lint-backend` OK (Ruff + format, 1 357 fichiers) ; `typecheck-backend` OK (1 297 fichiers) ; `test-backend` 9 246 passés, 5 skippés (jadx/Ghidra/image analysis non installés) ; `test-integration` **813 passés** ; `test-ci-scripts` OK ; `test-workflow-full` 82 scénarios passés ; `test-migrations-compose` OK.

REJOUÉS sur `d0b1195f` (les changements ultérieurs ne touchent ni frontend ni code applicatif) : `test-frontend` 980 passés ; `lint-frontend` OK (2 warnings) ; `typecheck-frontend` OK.

REJOUÉS sur `31776683` depuis un worktree propre : J2 (1 passé, 160 s, PDF Typst réel) et parcours navigateur M24 (`test-investigation-browser`, étapes 1–32, 1 passé, 98 s). Delta ultérieur : scripts de gate Compose et docs.

Deux défauts d'environnement rencontrés, sans lien avec le code :
- `typst` du `PATH` était `/snap/bin/typst` ; `test_postgres_localized_diagram_correction_and_rerender_loop` ignorait `TYPST_BINARY` et échouait aussi sur `d7dd3a2d`. Corrigé en `31776683` (une ligne, comme les autres tests d'intégration). Le chemin de rendu Typst de production n'est pas modifié.
- `var/workflow-results` non inscriptible et dossier de résultats sur un autre disque que le dépôt (le test M24 déplace des PNG vers `docs/m24/captures-m24-2`) : gates navigateur lancés depuis un worktree propre.

NON EXÉCUTÉ : **ShellCheck** (non installé, aucune image locale) — `bash -n` passe et le script Compose a été relu à la main (R3b : pas de SC2086/SC2155 évident) ; **CI GitHub** sur le commit candidat (aucun push autorisé) ; tests réels des CLI, VirusTotal, Shodan, Bridge ; isolation `bwrap` réelle (le noyau refuse les namespaces utilisateur selon M24.2).

## 8. Revues indépendantes

| Revue | Verdict final | Détail |
|---|---|---|
| R1 schéma et migrations | GO avec réserves | conformité 0001→0029, aucune mutation de metadata ; réserve : commentaire périmé de `0007` ; capture PG non exécutée par l'agent (PG injoignable depuis son sandbox) — REJOUÉE par l'orchestrateur (§ 5) |
| R2 sécurité (R1e) | GO avec réserves (R2-d) après 3 NO-GO corrigés et re-revus | § 3 |
| R3 intégration | NO-GO → corrigé → R3b GO avec réserves | M1 faux PASS du gate Compose (corrigé `fca72d8f`) ; M2 `stop()` hors workspaces sans session : mineur ; séquencement du runbook (corrigé `602d2f94`) |

## 9. Procédure de reset

`docs/operations/november-fresh-start.md` : effets exacts de `make up-clean` (volumes PostgreSQL/Redis/MinIO, `var/workspaces/subjects` et `editions`, `var/diagnostics/runs` et `events.jsonl`, puis `make up`), le workspace M24 sous `~/work/autowork/` n'est pas touché, le reset ne répare jamais une migration, refus de `safe_local_reset.py` jamais contourné, listes avant/après reset (sauvegarde restaurable, marqueurs du premier run, dry-run, migrations jusqu'à `0029`, tables/triggers, MinIO, Redis, Typst, `M24_AGENT_SESSION_KEY` désactivée). `safe_local_reset.py` n'est pas modifié. La commande dry-run du runbook a été alignée sur celle de `make clean` ; elle n'a **pas** été exécutée (non rejouée).

## 10. Risques résiduels

1. R1e : décisions 1 à 5 du § 3 à confirmer.
2. Dérive `server_default` de `investigation_note_proposals` (§ 6) : défaut latent possible, aucune migration.
3. **La stack vivante n'a pas 0029** : les images `cti-bulletin-*` ont ~7 jours et `cti-bulletin-migrate` ne contient pas `0029_note_anchor_identity`. L'état Alembic de la base vivante n'a pas été inspecté. Un déploiement avec DDL est un acte séparé.
4. Les migrations d'une base vivante déjà estampillée `0001` avec des données restent couvertes uniquement par des tests sur bases jetables ; `0001` ne protège pas ses tables contre un downgrade (comme avant).
5. Le dry-run de `safe_local_reset.py` n'a pas été exécuté ; il exige un PostgreSQL démarré.
6. Hors M24.3 : le CI GitHub de `main` (J2) est supposé rétabli par la correction du spec mais n'a pas été rejoué.

## 11. Autorisations encore nécessaires

Distinctes et explicites : push et rejeu de la CI GitHub ; merge dans `main` ; déploiement/redémarrage de la stack et DDL sur la base vivante ; `make up-clean` (décision humaine après le runbook) ; activation de `M24_AGENT_SESSION_KEY` ; appels CLI Codex/Claude réels, VirusTotal, Shodan, Bridge ; décision sur les réserves du § 3 et la dérive du § 6.

## 12. Gate final

Conditions de la mission pour déclarer **GO pour une installation locale propre** :

| Condition | Statut |
|---|---|
| R1e fermé | oui — R2-d GO avec réserves (réserves § 3) |
| J2 passe | oui — REJOUÉ |
| CI verte sur le commit candidat | **non vérifiable** — aucun push autorisé |
| Migrations équivalentes | oui — 6 checkpoints identiques à la référence, REJOUÉS |
| Schéma final identique | oui — snapshot `0029` octet pour octet identique |
| Installation sur PostgreSQL vierge fonctionne | oui — `upgrade head` sur bases vierges et gate Compose, REJOUÉS |
| Droits et protections existants inchangés | oui — fonctions/triggers de `0001` identiques ; suite complète verte |

**Verdict : GO local conditionnel.** Toutes les conditions vérifiables hors réseau sont remplies ; la condition « CI verte sur le commit candidat » exige un push et un run GitHub, non autorisés ici. La capacité à lancer les vrais fournisseurs reste un gate d'exploitation distinct.
