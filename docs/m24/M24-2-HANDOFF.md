# M24.2 — HANDOFF à un nouvel orchestrateur (Claude Sonnet 5.5 high)

Document de reprise écrit par l'orchestrateur précédent (Claude Sonnet 5.5, session
`3ab6d69a`, saturée en contexte). Il remplace `docs/m24/M24-2-STATE.md` pour la reprise ;
ce dernier garde l'historique de la première pause.

**Ce n'est PAS le rapport final** (`docs/m24/M24-2-FINAL-REPORT.md` reste à écrire) et il ne
déclare aucun GO. Ne présente comme vérifié que ce que tu as rejoué toi-même.

## 0. Règles de l'utilisateur (impératives)

1. **Éviter les frictions et les bornes arbitraires.** Aucun nouveau refus de fonctionnement normal,
   aucun seuil numérique nouveau, aucune validation stricte bloquant un usage légitime. Les
   garde-fous ne se déclenchent que sur une vraie faille. Dans chaque brief de sous-agent, recopie cette règle.
2. **Ne rien pousser, fusionner dans `main`, déployer, ni redémarrer la stack ; aucun DDL sur base vivante.**
   Pas d'appel réel VT / Bridge / Shodan, pas de vrai CLI Codex/Claude, `M24_AGENT_SESSION_KEY` non activé.
   L'utilisateur a dit « j'accepte toutes les commandes, ne me demande pas sans arrêt » : n'interromps pas pour des
   commandes de routine, mais ces limites du mandat restent.
3. **Sous-agents = GPT-6 Luna** (provider `codex`, model `gpt-6-luna`, mode `auto-review`, thinking `xhigh`
   pour les tâches conséquentes, `high` pour les petites). Max 6 simultanés. **Session fraîche par correction/revue** :
   ne re-prompte jamais un enfant terminé. Un enfant par question, un worktree par écrivain, fichiers disjoints.
4. **Ne jamais conclure « flake » sans rejouer le test seul ET en suite complète** (le test broker signé passait
   seul et échouait en suite : c'était une vraie dépendance à l'horloge d'import).
5. Rejoue **toi-même** dans le checkout principal tout ce que dit un sous-agent : plusieurs ont livré des tests qu'ils
   n'avaient pas pu exécuter (sandbox sans sockets AF_UNIX, pas de venv en worktree).
6. Ne déclare jamais GO ce qui n'est pas prouvé. Ne pas attribuer une revue à un modèle qui ne l'a pas exécutée :
   les reviewers se déclarent « Codex basé sur GPT-6, variante Luna/xhigh non vérifiable » — rapporte-le tel quel.

## 1. État du dépôt

- Dépôt `/home/nill/perso/work/AutoWork`, branche **`m24-2/integration`**, locale, **rien poussé**.
- Base M24.1 : `dfa25c54`. HEAD au moment de ce handoff : voir §7 (mis à jour en fin de document).
- Seul écart non commité : suppression de `paseo.json`, **antérieure à la mission, ne pas y toucher**.
- Branches `m24-2/*` et worktrees `~/.paseo/worktrees/0y1qsxa1/m24-2-*` : conservés (ne rien supprimer sans demande).
- Artefacts hors dépôt : `/home/nill/m242-artifacts/` (journaux de gate, ~445 captures de toutes les campagnes).
- Documents M24.2 dans `docs/m24/` : `M24-2-STATE.md` (1re pause), `M24-2-CAPTURES.md`, `M24-2-CLI-REATTESTATION.md`
  (procédure de ré-attestation CLI, NON exécutée), `captures-m24-2/` (jeu PNG), ce handoff.

## 2. Ce qui est fait (tout fusionné dans `m24-2/integration`)

| Lot | Contenu |
|---|---|
| A1–A4 | Test CI rouge (horloge de test), R2B-4 (plus de bind `/etc/hosts`/`resolv.conf` hôte ; `sandbox-etc` jetable 0700), credentials/collect, 3 tests instables déterministes |
| B1–B4 + glue | Shell 1600 px, barre de contexte, menu « Actions de l'enquête », vue d'ensemble orientée décisions, liste + détail de tâche (`task=`), états d'instruction |
| C1–C5 | Workspace master/detail, article annotable (`ArticleReadingPane`), revue Bridge, endpoint `GET …/observations/{id}/excerpt`, sélecteur visuel de code canonique |
| D1–D5 | Aperçu PDF (4 états + repli), parcours `article_element`, annexe technique Typst (manifestes article v49 / édition v38), G2 lisible, échantillons/résultats |
| E1–E4 | Rotation équitable des livraisons, I/O hôte hors transaction (état `RECONCILING`), tests de reprise/concurrence, parcours navigateur 32 étapes |
| UX-A…UX-F | Retouches après examen des captures RÉELLES (tuiles, libellés FR, sélecteurs lisibles groupés, débordements, onglets clavier…) |
| Correctifs de revue | Voir §4 |

## 3. Vérifications réellement rejouées par l'orchestrateur précédent

(Chaque ligne : ce qui a été exécuté dans le checkout principal, et sur quel commit.)

- Frontend : `pnpm exec vitest run` **124 fichiers / 980 tests**, `pnpm typecheck`, `pnpm lint` (0 erreur, 2 avertissements Fast Refresh existants dans `AnchorPicker.tsx`) — après le merge de fix-R3c (HEAD `4e7c1839`).
- Backend : `tests/workspace_agent` + `test_agentic_host_sessions.py` + `test_agentic_tool_broker.py` + `workflow_emulator/test_m24_browser_server.py` = **837 passés**, `make lint-backend` et `make typecheck-backend` verts — après le merge de fix-R1c.
- Typst/IOC/rendu/édition : 312 passés + 8 PG de rendu — après le correctif d'annexe R6.
- Migrations 0027–0029 : 186 tests de chaîne/migration verts ; **M24.2 n'ajoute aucune migration**.
- ShellCheck : aucune erreur (`-S error`) ; aucun `.sh` modifié par M24.2.
- Suite backend complète (commande CI) : **9 936 passés / 0 échec sur `18f9d0a9`** — mais **plusieurs commits ont suivi** : à REJOUER (§5).
- `make test-workflow-full` : 82/82 sur `fd4691a6` — à REJOUER.
- Parcours navigateur 32 étapes (`make test-investigation-browser`) : passé sur l'arbre intégré après UX-E (1,8 min) ; UX-F l'a rejoué de son côté. À REJOUER sur le HEAD final.

## 4. Revues indépendantes — historique et état

Reviewers = agents Luna Audit (read-only). Verdicts successifs :

| Revue | Résultat | Suite |
|---|---|---|
| R1 sécurité (18f9d0a9) | NO-GO (4 majeurs) | Corrigé par commits `3afe68ad` (6 correctifs étroits : garde bind `/` `/etc`, ancres TLS, `sandbox-etc` exclusif, secret dans chemin + feuilles JSON, gros fichier hors manifeste ignoré, nettoyage interruption) |
| R1b (fca03982) | NO-GO : `note.capture` contourne le filtre ; secret fragmenté | Corrigé par `63ad2777` (contrôle sur écritures `agent` + scan des sorties concaténées + `finally`) |
| R1c (dd89f5f5) | NO-GO : `note.capture(target="result")` (origine `system`) et écriture `agent` sans `task_id` contournent | Corrigé par `2522040d` (contrôle sur toute écriture non humaine, avec ou sans `task_id`, lecture paresseuse seulement si session active). **À re-revoir (R1d).** |
| R2 concurrence | GO avec réserves | 2 mineurs documentés (§6). Le « test bloqué » = refus de sandbox sur sockets, pas un deadlock |
| R3 provenance (18f9d0a9) | NO-GO | Corrigé : modèle d'API/domaine `canonical_code` non borné (`88d1ad4d`, `18f9d0a9`), plafond 4 MiB de l'endpoint retiré (`86d76820`), association d'échantillon par id d'abord (`cac43c92`) |
| R3b (fca03982) | NO-GO : affichage G2 `superseded` | Corrigé partiellement par `3b2e7978` |
| R3c (dd89f5f5) | NO-GO : G2 encore affiché « autorisé » dans la carte du journal (`InvestigationPanels.tsx`) et libellés de divulgation | Corrigé par `112c5557` : helper partagé `envelopeAccess.ts` (état jamais approuvée / active / révoquée / inactive) utilisé par tous les affichages. **À re-revoir (R3d).** |
| R4 architecture | GO avec réserves | Budget de collecte limité aux fichiers importables (`e125179f`), onglets clavier + garde-fou e2e (UX-F) |
| R5 UX | GO avec réserves (UX-01..19 attestés, UX-20 PARTIEL faute de captures sur le HEAD) | Captures régénérées (`c1bb3233`, 69 PNG, états PDF en cours/échoué/obsolète simulés) sur `dd89f5f5` ; elles ne reflètent pas le dernier correctif d'affichage G2 (`112c5557`) → re-capturer au besoin |
| R6 éditorial | GO avec réserves | Annexe `breakable: false` (manifestes v49/v38), garde `URL.createObjectURL`, assertions PDF du texte canonical_code |

## 5. À FAIRE (dans l'ordre)

1. **Aucun sous-agent n'est en cours** : tous les agents lancés par l'orchestrateur précédent ont terminé et sont FUSIONNÉS dans `m24-2/integration` (dernier : fix-R3c `112c5557`, captures `c1bb3233`). Vérifie quand même `mcp__paseo__list_agents` (y compris `includeBackground`) avant de lancer du neuf.
2. **Re-revues fraîches ciblées** (agents Luna Audit xhigh, lecture seule, avec la règle §0.1) :
   - **R1d** : écritures non humaines contrôlées contre les secrets des sessions actives (`sidecars/workspace_agent/files.py`, `http_server.py`, `sessions/supervisor.py`) ; aucune écriture sans secret n'est affectée ; origine `ui` exclue.
   - **R3d** : plus aucun affichage de l'autorité G2 n'appelle « autorisé » un état non actif (grep `allowed_providers`, `currently_authorized`, `envelope_approved`) ; autorité G2 inchangée.
   Une revue NO-GO → correctif ÉTROIT par un agent frais → re-revue fraîche. **Critère d'arrêt : une revue qui ne trouve plus que du MINOR.**
3. **Rejouer les gates sur le HEAD final** (recettes §8) :
   - suite backend complète (commande CI) → viser 0 échec ; **surveille `tests/workspace_agent/test_races.py::test_symlink_swap_loop_never_escapes_the_workspace`** (1 échec non reproduit sur ~170 exécutions ; il contient une assertion statistique `successes > 0` ; ne le déclare pas flake sans analyse) ;
   - `make test-workflow-full WORKFLOW_RESULTS_DIR=/tmp/…` ;
   - `make test-investigation-browser …` (32 étapes) ;
   - frontend complet, `make lint-backend`, `make typecheck-backend` ;
   - si le rendu G2 a changé de façon visible, régénère les captures G2 (`overview-revoked`, détail G2 révoqué) et mets à jour `M24-2-CAPTURES.md` (agent frais, il doit OUVRIR les images).
4. **Écrire `docs/m24/M24-2-FINAL-REPORT.md`** (mandat §25) : pour chaque résultat → commit, commande, environnement, statut, niveau de preuve, restriction. Inclure : causes de la CI M24.1, correctifs de sécurité, modifications UI, comparatif des captures, tableau des **capacités attestées / non attestées**, procédure d'activation contrôlée des CLI (`M24-2-CLI-REATTESTATION.md`), liste précise des **autorisations externes restantes**, six revues avec le modèle réellement utilisé, résidus (§6), écart d'ordre assumé (les lots B ont démarré avant la clôture du gate du lot A, fichiers disjoints).
5. Commit local final. **Ne rien pousser.** Rapporte à l'utilisateur.

## 6. Résidus connus (non corrigés par choix, sans friction) — à consigner dans le rapport

- Curseur de rotation des livraisons en mémoire, perdu au redémarrage d'un worker (R2) ; le CAS empêche tout double effet.
- Rejeu de `collect` après redémarrage du sidecar non strictement idempotent (cache d'idempotence en mémoire) (R2).
- Fragments de credential **réordonnés** non détectés ; la concaténation du scan peut, en théorie, refuser deux contenus bénins dont la jointure forme un fragment du secret (R1c).
- TOCTOU entre lecture du fichier temporaire et publication : workspace monté en lecture seule dans le sandbox, aucun chemin d'exploitation établi (R1c).
- CI job J2 : Typst installé par version seule, pas par `scripts/install-typst.sh` (SHA verrouillé) — préexistant (R6).
- `GET /envelope` ne fournit pas le motif de la dernière décision G2 : l'UI dit « non transmis par l'API ».
- Aucune figure `protocol_figure` dans le scénario navigateur ; seule une figure d'article M23 est couverte dans le PDF.
- Lecteur PDF intégré : Chromium headless ne l'affiche pas ; le statut + le repli sont visibles, le PDF Typst est vérifié par `pdftotext`/`pdftoppm`.
- bwrap réel : le noyau de la machine refuse les namespaces utilisateur ; seuls les `argv` construits et des faux CLI sont testés. Vrais CLI Codex/Claude, VT, Shodan, Bridge : **jamais appelés** (autorisation séparée requise).
- Densité des listes IOC / sélecteur de code, `SamplesResultsView.tsx` très gros (~2000 lignes) : souhaitable à découper, non bloquant.

## 7. Journal des derniers commits

HEAD à la passation : **`4e7c1839`** (92 commits après `dfa25c54`), branche `m24-2/integration`, arbre propre hors `paseo.json`.
Derniers merges, du plus ancien au plus récent : fix-R1b (`63ad2777`) → fix-R1c (`2522040d`) → captures finales (`c1bb3233`) → fix-R3c (`112c5557`).
Fait à vérifier : `git log --oneline -12` doit montrer ces merges.

## 8. Recettes d'environnement (pièges vérifiés)

- **Chemins courts obligatoires** (socket UNIX ≤ 108 octets) : `--basetemp=/tmp/pt…` et `TMPDIR=/tmp/m242-tmp`. Un `TMPDIR`/`basetemp` long fait échouer les tests « real sidecar » (« session secret handoff cannot be armed »).
- Typst (le `typst` snap est cassé) : `TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH TMPDIR=/tmp/m242-tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst`. Sans cette recette, les tests Typst réels échouent (« input file not found … publication.typ ») : c'est l'environnement, pas le code. Le SHA de l'exécutable local diffère de `infra/typst.lock` car le lock vérifie l'archive `.tar.xz`, pas l'exécutable : attendu.
- PG de test réservé (jamais la base vivante) : `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres` (+ `-m integration`).
- Suite backend (commande CI), depuis `backend/` : `uv run --python 3.12 pytest -m "not workflow_fast and not workflow_full" -q --tb=short -p no:cacheprovider --basetemp=/tmp/pt-gate` (~20 min ; lance-la en arrière-plan avec la sortie dans un fichier).
- `make test-workflow-full` : ajouter `WORKFLOW_RESULTS_DIR=/tmp/…` (le répertoire `var/` n'est pas inscriptible ; ne l'inspecte pas).
- `make test-investigation-browser WORKFLOW_RESULTS_DIR=/tmp/… INVESTIGATION_BROWSER_API_PORT=<port libre> M24_REAL_CAPTURE_DIR=/tmp/…` ; ajouter `UV_SYNC=:` si `uv sync` tente le réseau. Ports déjà utilisés par des agents : 18781–18788, 18790, 18791 : prends-en un autre.
- Sous-agents en worktree : pas de `node_modules` ni de venv. `pnpm install --frozen-lockfile --offline` ou lier `frontend/node_modules` ; lier `backend/.venv` et lancer `PYTHONPATH=src:. .venv/bin/python -m pytest …` (le `PYTHONPATH` prime sur l'installation éditable du checkout principal). Ne jamais committer ces liens. Dans leur sandbox : sockets AF_UNIX refusés, `git stash` parfois impossible (métadonnées Git en lecture seule), `uv sync` impossible : demande-leur de le dire et rejoue toi-même.
- Shell zsh : un motif sans correspondance (`rm *.log`) fait échouer toute la commande : utilise `bash -c '…'`.
- `ruff format` sur tout `sidecars/` reformate des fichiers sans rapport : ne formate que les fichiers modifiés.
- Pour nommer un test avant/après : exécute-le sur le code d'origine (`git stash push <fichiers source>`), montre l'échec, puis restaure.

## 9. Comment lancer un sous-agent

1. `mcp__paseo__create_workspace` : `isolation: "worktree"`, `mode: "branch-off"`, `baseBranch: "m24-2/integration"`, `branchName: "m24-2/<nom>"`, `path: "/home/nill/perso/work/AutoWork"`.
2. `mcp__paseo__create_agent` : `provider: "codex/gpt-6-luna"`, `workspaceId`, `settings: {"modeId":"auto-review","thinkingOptionId":"xhigh"}`, prompt complet (contexte, fichiers, règle §0.1, env, test avant/après, commit avec ligne `Co-Authored-By: GPT-6 Luna <noreply@openai.com>`, format du rapport).
3. Les reviewers read-only se lancent dans le workspace du checkout principal (`wks_a7edf38b94744aca`).
4. À la fin d'un agent : lis son diff (`git diff m24-2/integration...<branche>`), fusionne, rejoue dans le checkout principal.
5. Les commits de l'orchestrateur se terminent par `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## 10. Mémoire

`/home/nill/.claude/projects/-home-nill-perso-work-AutoWork/memory/mission-m24-2-state.md` pointe vers ce document
(à mettre à jour à la fin de ta mission). Voir aussi `fresh-subagent-per-round.md` et `feedback-simplify-remove-friction.md`.

## 11. Mise à jour de l'orchestrateur #2 (pause demandée par l'utilisateur, 2026-11-24)

HEAD : `b9c38d31` (branche `m24-2/integration`, locale, rien poussé). Arbre propre hors `paseo.json`.
Commits de cette session : `afaf557a` (libellé neutre budget d'enveloppe, `OperatorsPanel.tsx`),
`dbd524c0` (indice de collecte VT sans « approuvée », `SamplesResultsView.tsx`), merge `b9c38d31`
de `m24-2/fix-r1d-secret-guard` (commit agent `8ba83eea`, `sessions/supervisor.py` + 3 tests + ADR-0011).

### Revues de cette session (agents Luna Audit, lecture seule ; modèle déclaré « GPT-6, variante Luna/xhigh non vérifiable »)
| Revue | Cible | Verdict | Suite |
|---|---|---|---|
| R3d (cee76a09, xhigh) | affichage G2 après 112c5557 | NO-GO annoncé, 1 seul MINOR : `OperatorsPanel` « Enveloppe approuvée » pour une version révoquée | corrigé par `afaf557a` (libellé neutre « Enveloppe G2 liée au budget » + assertion de test) |
| R3e (b41a4c44, high) | re-revue fraîche | GO avec réserves, 1 MINOR : `SamplesResultsView.tsx:1773` « bornée par l'enveloppe G2 approuvée » | corrigé par `dbd524c0`. **R3 clos (que du MINOR).** |
| R1d (03f35f61, xhigh) | garde de secret non humain après 2522040d | NO-GO : (1) course écriture en vol vs révocation, (2) secret dans le chemin, (3) `task_id` facultatif non lié | corrigé par l'agent 740e7283 (`8ba83eea`), diff relu, mergé `b9c38d31`; `tests/workspace_agent` rejoué : **767 passés** |
| R1e (22f7d0b0, xhigh) | re-revue fraîche du correctif R1d | NO-GO : fenêtre plus étroite — écriture déjà en vol dont la session est révoquée puis **fermée** (`close()` efface `secret_forms`) avant son contrôle ; idem `server_close()` (supervisor.py:3344, files.py:260, http_server.py:1032) | **Décision de l'orchestrateur #2 : résidu ACCEPTÉ, non corrigé** — le broker refuse toute session non `is_active` (`agentic_tool_broker.py:633`), donc le secret d'une session fermée n'a plus d'autorité ; le correctif proposé (coordination d'admission avec close/stop) ajouterait verrous/complexité (règle « pas de friction »). À SIGNALER à l'utilisateur, qui peut demander le correctif. Hors cette fenêtre, R1e n'a trouvé ni fuite des formes, ni régression légitime (auth.json non refusé, seuil de fragment 16 octets identique pour le chemin), ni deadlock. |

### Gates rejoués (checkout principal)
- Frontend sur `63a594da` (avant les 2 libellés) : vitest 124 fichiers / 980 tests, `pnpm typecheck`, `pnpm lint` (0 erreur, 2 avertissements existants). Après libellés : `OperatorsPanel.test.tsx` 22 passés, `src/features/investigation/samples` 21 passés. **À rejouer en entier sur le HEAD final.**
- `make lint-backend` + `make typecheck-backend` : verts sur `63a594da` (code backend inchangé hors sidecar/supervisor.py depuis) — **à rejouer sur le HEAD final**.
- Suite backend complète n°1 (lancée sur `afaf557a`, le merge R1d a eu lieu EN COURS de run) : 9979 passés / 1 échec `tests/test_investigation_transfers.py::test_real_sidecar_ack_of_queued_instruction_is_refused` (« host capability probe changed between probe and prepare ») ; rejoué seul le fichier entier : 18 passés. Hypothèse NON prouvée : la source du sidecar sondée a changé pendant le run (merge). NE PAS déclarer flake.
- Suite backend complète n°2 lancée sur `b9c38d31` (arbre figé) : sortie dans **`/tmp/m242-full-backend2.log`** (commande CI, `--basetemp=/tmp/pt-gate2`, PG 55432). **Résultat non lu au moment de la pause** : lire `tail -n 5 /tmp/m242-full-backend2.log`. Si le log manque (redémarrage machine), relancer (recette §8). Si ce même test échoue encore : l'analyser (rejouer seul ET en suite) avant toute conclusion.

### Reste à faire (ordre)
1. Lire le résultat de la suite n°2 (0 échec attendu). 2. `make test-workflow-full WORKFLOW_RESULTS_DIR=/tmp/…` ; `make test-investigation-browser` (port libre, 32 étapes) ; frontend complet + typecheck + lint ; lint/typecheck backend, sur le HEAD final. 3. Captures G2 : le rendu a changé seulement par 2 libellés texte (budget d'enveloppe dans le détail de session, indice VT) ; les captures G2 (`overview-revoked`, `g2-revoked-detail`) n'affichent pas ces libellés → re-capture non requise, à confirmer. 4. Écrire `docs/m24/M24-2-FINAL-REPORT.md` (mandat §25 ; matériaux : §2–§6 de ce handoff, `M24-2-CAPTURES.md`, `M24-2-CLI-REATTESTATION.md`, ce §11 ; causes de la CI M24.1 : tests dépendant de l'horloge d'import, sockets AF_UNIX > 108 octets, shellcheck [M24-1-FINAL-REPORT §3], + oubli A1 corrigé par `188c2f61`). Ajouter aux résidus : fenêtre R1e ci-dessus. 5. Commit final (ligne `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`), mémoire, compte rendu. Rien à pousser/déployer.
Aucun sous-agent n'est en cours (R1d, R3d, R3e, R1e, fix-R1d terminés). Worktree `~/.paseo/worktrees/0y1qsxa1/m24-2-fix-r1d-secret-guard` conservé.
