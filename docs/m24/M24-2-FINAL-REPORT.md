# M24.2 — Rapport final

Orchestrateur #2 : Claude Sonnet 5.5 (effort high), qui reprend l'orchestrateur #1 (session saturée).
Sous-agents d'écriture et de revue : sessions Paseo `codex/gpt-6-luna` (mode `auto-review`, `xhigh`
sauf mention). Les reviewers se déclarent « Codex basé sur GPT-6, variante Luna/xhigh non
vérifiable » : c'est rapporté tel quel, aucune revue n'est attribuée à un autre modèle.

Légende des niveaux de preuve : **REJOUÉ** (commande exécutée par l'orchestrateur #2 dans le
checkout principal, sur le HEAD indiqué) ; **ATTRIBUÉ** (affirmé par l'orchestrateur #1 ou un
sous-agent dans `docs/m24/M24-2-HANDOFF.md` ou ses rapports, non rejoué par moi) ; **NON ATTESTÉ**.

## 1. SHA et périmètre

- Branche locale `m24-2/integration`, base M24.1 `dfa25c54`. **Rien n'est poussé, rien n'est fusionné
  dans `main`, rien n'est déployé, aucune stack redémarrée, aucun DDL sur base vivante.**
- Code final : `b9c38d31` ; le commit du présent rapport (voir `git log -1`) n'ajoute que de la documentation.
- 98 commits depuis la base ; 144 fichiers modifiés hors captures (+25 003 / −4 736 lignes). **M24.2 n'ajoute
  aucune migration** (Alembic 0027–0029 de M24.1 inchangées).
- Seul écart d'arbre : suppression de `paseo.json`, antérieure à la mission, non touchée.
- Branches `m24-2/*` et worktrees `~/.paseo/worktrees/0y1qsxa1/m24-2-*` conservés. Artefacts hors dépôt :
  `/home/nill/m242-artifacts/` (journaux de gate, ~445 captures de toutes les campagnes).

## 2. Résumé et verdict

Les lots A–E, les retouches UX-A..F et les correctifs issus des revues R1…R6 sont fusionnés et les gates
automatisés sont verts sur le HEAD final (§3), y compris la suite backend complète (9 983 passés, 0 échec) ; un test de minutage a échoué une fois dans une autre suite complète (§3, note 1).
Les capacités qui exigent des fournisseurs ou CLI réels, l'isolation `bwrap` réelle, le lecteur PDF intégré
et un déploiement **ne sont pas attestées** (§9). **Verdict : PASS hors-ligne sur le périmètre local ;
pas de GO production, pas de GO d'activation des CLI réels** — ils exigent les autorisations listées en §11.

## 3. Résultats par commande (environnement : machine locale, Python 3.12 via `uv`, PG de test jetable 127.0.0.1:55432, aucun fournisseur live)

| Résultat | Commit | Commande | Statut | Niveau de preuve | Restriction |
|---|---|---|---|---|---|
| Suite backend complète n°3 (commande CI) | `b9c38d31` | `uv run --python 3.12 pytest -m "not workflow_fast and not workflow_full" -q --tb=short -p no:cacheprovider --basetemp=/tmp/pt-gate3` | **9 983 passés, 0 échec**, 5 skipped, 82 désélectionnés, 18 min 31 s | REJOUÉ | 5 skipped (outils absents : jadx, Ghidra, image analysis-worker, prctl/proc) ; tests `workflow_fast`/`workflow_full` hors commande, couverts par `make test-workflow-full` |
| Suite backend complète n°2 | `b9c38d31` | idem (`/tmp/pt-gate2`) | **9 982 passés, 1 échec**, 5 skipped, 21 min : `tests/test_external_effects.py::test_capability_rechecks_expiry_at_physical_send` | REJOUÉ | voir note 1 |
| Suite backend complète n°1 | `afaf557a` (merge R1d pendant le run) | idem | 9 979 passés, 1 échec : `test_investigation_transfers.py::test_real_sidecar_ack_of_queued_instruction_is_refused` | REJOUÉ | passe seul (fichier : 18 passés) ; run invalide car la source du sidecar a changé en cours de route (hypothèse non prouvée) ; n'a pas échoué dans la suite n°2 |
| `make test-workflow-full` | `b9c38d31` | `make test-workflow-full WORKFLOW_RESULTS_DIR=/tmp/m242-wf` | **82 passés**, garde workflow OK (82 scénarios collectés, tous `passed` dans le JUnit) | REJOUÉ | émulateur de workflow : fournisseurs simulés |
| Parcours navigateur réel 32 étapes | `b9c38d31` | `make test-investigation-browser … INVESTIGATION_BROWSER_API_PORT=18851 UV_SYNC=:` | **1 passé (1,4 min)**, `elapsed_seconds=91` | REJOUÉ | Chromium headless ; FastAPI + PG de test + adaptateurs synthétiques ; lecteur PDF intégré non rendu |
| Frontend | `b9c38d31` | `pnpm exec vitest run` | **124 fichiers / 980 tests passés** | REJOUÉ | |
| Frontend typecheck / lint | `b9c38d31` | `pnpm typecheck` ; `pnpm lint` | typecheck exit 0 ; lint 0 erreur, 2 avertissements Fast Refresh préexistants (`AnchorPicker.tsx`) ; Prettier OK | REJOUÉ | |
| Backend lint / typecheck | `b9c38d31` | `make lint-backend` ; `make typecheck-backend` | ruff « All checks passed », 1349 fichiers formatés ; mypy « no issues found in 1291 source files » | REJOUÉ | |
| Sidecar (garde de secrets, tout `workspace_agent`) | `b9c38d31` | `uv run … pytest tests/workspace_agent` | **767 passés** | REJOUÉ | AF_UNIX court (`--basetemp=/tmp/pt-…`) |
| Correctifs R1d : 3 tests nouveaux | `8ba83eea` | `pytest tests/workspace_agent/test_sessions_r2fix_a.py` | 38 passés (fichier) ; agent : les 3 tests nouveaux échouent avant, passent après | ATTRIBUÉ (avant/après) + REJOUÉ (passage après) | l'avant-échec n'a pas été rejoué par moi |
| Typst / IOC / rendu / édition, 8 tests PG de rendu | `0a2adae0` (annexe R6) | recette Typst du handoff §8 | 312 passés + 8 PG | ATTRIBUÉ | non rejoué séparément (inclus dans la suite complète n°2/n°3) |
| Chaîne de migrations 0027–0029 | — | tests de chaîne/migration | 186 verts | ATTRIBUÉ | aucune migration ajoutée |
| ShellCheck | — | `shellcheck -S error` | aucune erreur ; aucun `.sh` modifié | ATTRIBUÉ | CI GitHub non rejouée |
| Revue de captures (69 PNG) | `dd89f5f5` | parcours réel + interception Playwright des états PDF | tous ouverts un par un par un agent ; voir `M24-2-CAPTURES.md` | ATTRIBUÉ | captures prises avant les 2 libellés `afaf557a`/`dbd524c0` et avant le correctif G2 `112c5557` ; voir §7 |

**Note 1 — `test_capability_rechecks_expiry_at_physical_send`.** Échec unique dans la suite n°2 :
`DefinitelyNotSubmitted` au lieu de `Ambiguous`. Ni ce test ni `application/external_effects*` n'ont été
modifiés depuis M24.1 (`git diff dfa25c54..HEAD` vide). Le test fixe une durée de vie de permis de **20 ms** face à
un délai de transport de 50 ms : il est sensible à l'ordonnancement. Rejoué : 3/3 seul, 8/8 seul sous charge CPU
saturée, fichier entier 256/256, et **suite complète n°3 sur le même HEAD : 0 échec**. Conclusion limitée aux faits :
intermittent (1 échec sur 3 suites complètes, non reproduit seul ni en suite), sensible à l'ordonnancement (permis de 20 ms),
non lié aux changements M24.2. Aucun correctif appliqué (hors périmètre ; changer les 20 ms serait une décision de test à part).

## 4. Causes de la CI rouge de M24.1 et leur traitement

Causes établies dans `M24-1-FINAL-REPORT.md` §3 : tests dépendant de l'horloge à l'import, chemins AF_UNIX de plus de
108 octets, `shellcheck`. Dans M24.2 :
- **A1** (`7ce79064`) : horloge de test cohérente pour les baux du broker ; **oubli de A1** découvert par la suite
  complète (le bail par défaut ancré sur l'heure d'import, alors que la route signée utilise l'heure murale : échec
  quand la suite durait plus de 180 s après collecte) — corrigé par `188c2f61` ; preuve ATTRIBUÉE à l'orchestrateur #1 :
  horloge d'import décalée de 11 min, avant 1 échec / après 9 passés.
- **A4** (`a1615883`) : trois tests instables rendus déterministes (synchronisation, tests seulement) ; `66f5f8a0`,
  `c6229977` (socket d'egress sous chemin court), `3670a2fd` (attente de l'état du harnais).
- La CI GitHub elle-même **n'a pas été rejouée** (NON ATTESTÉ) ; job J2 : Typst installé par version et non par
  `scripts/install-typst.sh` (SHA verrouillé), préexistant (R6).

## 5. Correctifs de sécurité

| Sujet | Commit(s) | Effet |
|---|---|---|
| R2B-4 : plus de bind `/etc/hosts`/`resolv.conf` de l'hôte | A2 `59895a44` | `sandbox-etc` jetable 0700 ; TLS limité aux chemins de certificats ; garde contre les binds `/` et `/etc` (`3afe68ad`) |
| Collecte : manifeste fermé, noms et octets contrôlés avant le filtre | A3 `619245a8`, `89b8500a` | refus 403 conservé ; sorties de scratch scannées avant filtrage |
| Secret de session non écrit par un agent (R1 → R1b → R1c → R1d) | `3afe68ad`, `63ad2777`, `2522040d`, `8ba83eea` | contrôle sur toute écriture non humaine (agent/system, avec ou sans `task_id`, `note.capture`), scan des sorties concaténées, comparaison du chemin, sessions échouées pour exposition contrôlées jusqu'au `close` ; origine `ui` et écritures sans secret actif inchangées ; budget de collecte limité aux fichiers importables (`e125179f`) |
| Provenance (R3) | `88d1ad4d`, `18f9d0a9`, `86d76820`, `cac43c92` | `canonical_code` non borné pour les blocs stockés ; plafond de 4 MiB de l'endpoint d'extrait **retiré** ; association d'échantillon par identifiant |
| Affichage de l'autorité G2 (R3 → R3b → R3c → R3d/e) | `3b2e7978`, `112c5557`, `afaf557a`, `dbd524c0` | un état jamais approuvé / révoqué / inactif n'est plus présenté comme « autorisé » ; autorité G2 **inchangée** (présentation seulement) |

Règle de l'utilisateur appliquée à chaque correctif : variante la plus étroite, aucun nouveau seuil ni refus de fonctionnement normal.

## 6. Modifications UI

Shell 1600 px et barre de contexte, menu « Actions de l'enquête », vue d'ensemble orientée décisions, liste et détail de
tâche (`task=`), états d'instruction (en file / transmise / acquittée), workspace master/detail, lecture annotable de
l'article, revue Bridge (diff de correction), sélecteur visuel de code canonique (endpoint
`GET …/observations/{id}/excerpt`), aperçu PDF à 4 états + repli, parcours `article_element`, G2 lisible, échantillons et
résultats (`SamplesResultsView`), onglets clavier, libellés FR. Détail par lot : `M24-2-HANDOFF.md` §2.

## 7. Comparatif des captures

`docs/m24/captures-m24-2/` (69 PNG, 1280×800, 1440×900, 1920×1080 + 3 pages PDF) contre `docs/m24/captures-m24-1/`
(28 PNG) : tableau avant/après, observation visuelle propre à chaque capture et défauts restants dans
**`docs/m24/M24-2-CAPTURES.md`**. Nouveaux états absents de M24.1 : décision en attente, détail G2 révoqué, liste de 30
tâches, instruction en file/transmise/acquittée, états PDF en cours/échoué/obsolète (simulés par interception), pages
PDF 2 et 3 (annexe). Niveau de preuve : ATTRIBUÉ (agent d'examen visuel qui a ouvert les images) ; la campagne est sur
`dd89f5f5`. **Postérieur aux captures** : le helper G2 (`112c5557`) et deux libellés texte (`afaf557a` budget d'enveloppe
dans le détail de session, `dbd524c0` indice de collecte VT). Les captures `overview-revoked`/`g2-revoked-detail`
n'affichent pas ces deux libellés, mais **elles n'ont pas été régénérées** après `112c5557` : écart assumé et signalé.
Défauts visuels restants : listes IOC et sélecteur de code denses, défilement ciblé qui coupe parfois l'en-tête,
lecteur PDF vide en Chromium headless, exemples synthétiques en anglais.

## 8. Les six revues et le modèle réellement utilisé

Toutes : agents Paseo `codex/gpt-6-luna`, lecture seule, déclarés « GPT-6, variante Luna/xhigh non vérifiable ».

| Revue | Cible | Résultat | Traitement |
|---|---|---|---|
| R1 sécurité | `18f9d0a9` | NO-GO (4 majeurs) | `3afe68ad` |
| R1b | `fca03982` | NO-GO (`note.capture` contourne ; secret fragmenté) | `63ad2777` |
| R1c | `dd89f5f5` | NO-GO (`target=result`, écriture sans `task_id`) | `2522040d` |
| R1d (cette session, `03f35f61`, xhigh) | `63a594da` | NO-GO : course écriture/révocation, secret dans le chemin, `task_id` non lié | `8ba83eea` (agent `740e7283`), mergé `b9c38d31`, relu et rejoué |
| R1e (`22f7d0b0`, xhigh) | `b9c38d31` | NO-GO sur une fenêtre plus étroite (voir §10) | **résidu accepté, non corrigé** — décision de l'orchestrateur, à confirmer par l'utilisateur |
| R2 concurrence | — | GO avec réserves (2 mineurs : §10) | documentés |
| R3 provenance | `18f9d0a9` | NO-GO | `88d1ad4d`, `18f9d0a9`, `86d76820`, `cac43c92` |
| R3b | `fca03982` | NO-GO (G2 `superseded` affiché autorisé) | `3b2e7978` |
| R3c | `dd89f5f5` | NO-GO (carte du journal, libellés de divulgation) | `112c5557` |
| R3d (`cee76a09`, xhigh) | `63a594da` | « NO-GO » avec **un seul MINOR** (`OperatorsPanel`) | `afaf557a` |
| R3e (`b41a4c44`, high) | `afaf557a` | GO avec réserves, **un seul MINOR** (`SamplesResultsView`) | `dbd524c0` — **R3 clos** |
| R4 architecture | — | GO avec réserves | `e125179f`, onglets clavier (UX-F) |
| R5 UX | `dd89f5f5` | GO avec réserves (UX-01..19 attestés, UX-20 partiel faute de captures sur le HEAD) | captures régénérées `c1bb3233` |
| R6 éditorial | — | GO avec réserves | annexe `breakable: false` (manifestes article v49 / édition v38), garde `URL.createObjectURL`, assertions PDF |

Les verdicts R1…R6 hors R1d/R1e/R3d/R3e sont ATTRIBUÉS (rapportés par l'orchestrateur #1 dans le handoff §4).

## 9. Capacités ATTESTÉES / NON ATTESTÉES

| Capacité | Statut | Preuve |
|---|---|---|
| Parcours atelier 32 étapes en navigateur réel (données/adaptateurs synthétiques) | ATTESTÉE | REJOUÉ, 1 passé |
| Workflows émulés (82 scénarios) | ATTESTÉE | REJOUÉ |
| Garde de secrets du sidecar sur écritures non humaines | ATTESTÉE hors résidus §10 | REJOUÉ (767 tests sidecar) + revues R1d/R1e |
| PDF Typst réel : texte et pages (références, tableau, code, annexe) | ATTESTÉE | pages rasterisées par `pdftoppm`/texte par `pdftotext` (ATTRIBUÉ) + tests Typst rejoués dans la suite complète |
| Affichage G2 cohérent avec l'état de l'enveloppe | ATTESTÉE | REJOUÉ (vitest) + revues R3d/R3e |
| Lecteur PDF intégré | NON ATTESTÉE | Chromium headless ne l'affiche pas ; statut + repli visibles |
| Isolation `bwrap` réelle | NON ATTESTÉE | le noyau refuse les namespaces utilisateur ; seuls les `argv` et de faux CLI sont testés |
| CLI réels Codex et Claude (session, collecte, reprise) | NON ATTESTÉE dans M24.2 | jamais appelés ; la ré-attestation de M24.1 (H0) n'est pas rejouée ; procédure §12 |
| VirusTotal, Shodan, Bridge réels | NON ATTESTÉE | jamais appelés |
| CI GitHub (dont `shellcheck`, job J2 Typst) | NON ATTESTÉE | non rejouée |
| Déploiement, DDL sur base vivante, redémarrage de stack | NON ATTESTÉE | non demandés |
| Récupération réelle après crash d'un CLI | NON ATTESTÉE | couverte par des oracles hors-ligne seulement |
| `protocol_figure` dans le PDF | NON ATTESTÉE | seule une figure d'article M23 est couverte |

## 10. Résidus (non corrigés par choix : sans friction)

- Curseur de rotation des livraisons en mémoire, perdu au redémarrage d'un worker (R2) ; le CAS empêche tout double effet.
- Rejeu de `collect` après redémarrage du sidecar non strictement idempotent (cache d'idempotence en mémoire) (R2).
- Fragments de credential **réordonnés** non détectés ; la jointure de concaténation peut en théorie refuser deux contenus bénins (R1c).
- TOCTOU lecture du fichier temporaire → publication : workspace monté en lecture seule dans le sandbox (R1c).
- **Fenêtre R1e (nouveau)** : une écriture non humaine déjà en vol dont la session est révoquée pour exposition puis
  **fermée** avant son contrôle peut publier le secret (`close()` efface les formes ; `server_close()` similaire).
  Jugée **non exploitable en autorité** : le broker refuse toute session non `is_active`
  (`backend/src/cti_app/application/agentic_tool_broker.py:633`), donc le secret d'une session fermée ne confère plus rien.
  Le correctif proposé (coordination d'admission avec `close`/`stop`) ajoute verrous et complexité ; non appliqué.
  **Le reviewer l'a classée BLOQUANT : décision à confirmer par l'utilisateur.**
- Test de minutage `test_capability_rechecks_expiry_at_physical_send` (permis de 20 ms), intermittent, hors périmètre (§3 note 1).
- `GET /envelope` ne fournit pas le motif de la dernière décision G2 : l'UI dit « non transmis par l'API ».
- Densité des listes IOC / sélecteur de code ; `SamplesResultsView.tsx` (~2000 lignes) à découper (non bloquant).
- Captures G2 non régénérées après `112c5557` (§7).

## 11. Autorisations externes restantes

Aucune n'est donnée par ce rapport. Séparément et explicitement :
1. Activation de `M24_AGENT_SESSION_KEY` (routes de session fermées en `503` sinon) et d'un backend réel.
2. Un appel Codex réel et un appel Claude réel (ré-attestation, un prompt synthétique chacun).
3. Tout appel VirusTotal / Shodan / Bridge réel.
4. Push, merge dans `main`, rejeu de la CI GitHub.
5. Déploiement / redémarrage de la stack ; DDL sur base vivante (aucune migration nouvelle dans M24.2 ; 0027–0029 de M24.1 restent à appliquer le cas échéant).
6. Décision sur le résidu R1e (§10) et sur l'ordre des correctifs de sécurité restant.

## 12. Procédure d'activation contrôlée des CLI

Voir **`docs/m24/M24-2-CLI-REATTESTATION.md`** — **préparée, jamais exécutée**. Elle impose : autorisation humaine explicite,
instance jetable sans session active, `isolated_egress` et relais du broker vérifiés, `M24_AGENT_SESSION_KEY` injectée
seulement pendant la fenêtre, chemins d'authentification fournis par le gestionnaire de secrets local, **exactement un prompt
synthétique par CLI** (« Réponds exactement `M24.2 synthetic attestation OK` »), collecte idempotente, vérification d'absence
de credentials dans résultat/notes/journaux, nettoyage des copies temporaires, retrait de la clé et redémarrage pour rétablir
la porte fermée. Tout écart est un échec : pas de second prompt de diagnostic.

## 13. Écart d'ordre assumé

Les lots B (cockpit) ont démarré avant la clôture formelle du gate du lot A ; les fichiers étaient disjoints (handoff §0 : « fichiers disjoints ») et les gates complets ont été rejoués sur l'arbre intégré.
