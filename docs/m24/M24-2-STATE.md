# M24.2 — état de reprise (pause du 2026-11-24)

Document de travail de l'orchestrateur. Ce n'est PAS le rapport final
(`docs/m24/M24-2-FINAL-REPORT.md` reste à écrire) et il ne déclare aucun GO.

## Où en est le dépôt

- Branche : `m24-2/integration`, locale, **rien de poussé, rien de déployé**.
- HEAD à la pause : `188c2f61` (65 commits après la base `dfa25c54`).
- Seule modification hors commit : suppression de `paseo.json`, antérieure à la mission, non touchée.
- Branches et worktrees des sous-agents conservés (28 branches `m24-2/*`, worktrees sous `~/.paseo/worktrees/0y1qsxa1/`). Aucun n'a été supprimé.
- Artefacts hors dépôt, sauvegardés : `/home/nill/m242-artifacts/` (journaux de gate, 445 captures PNG de toutes les campagnes).

## Lots intégrés (tous fusionnés dans `m24-2/integration`)

| Lot | Contenu |
|---|---|
| A1 | Test CI rouge : horloge de test partagée (puis correctif de suivi `188c2f61`, voir plus bas) |
| A2 | R2B-4 : plus de bind `/etc/hosts`, `/etc/resolv.conf` hôte ; `sandbox-etc` jetable 0700 ; TLS limité aux chemins de certificats |
| A3 | Collect : contrôle des noms et des octets AVANT le filtre du manifeste, refus 403 conservé, plafonds 1 MiB / 4 MiB ; procédure `docs/m24/M24-2-CLI-REATTESTATION.md` (non exécutée) |
| A4 | Trois tests instables rendus déterministes (tests seulement) |
| B1–B4, glue | Shell 1600 px, barre de contexte, menu « Actions de l'enquête », vue d'ensemble orientée décisions, liste + détail de tâche (`task=`), états d'instruction |
| C1–C5 | Workspace master/detail, lecture annotable de l'article, revue Bridge, endpoint `GET .../observations/{id}/excerpt` + sélecteur visuel de code |
| D1–D5 | Aperçu PDF (4 états + repli), parcours `article_element`, annexe technique Typst, G2 lisible, échantillons/résultats |
| E1–E4 | Rotation équitable des livraisons, I/O hôte hors transaction (état `RECONCILING`), tests de reprise/concurrence, parcours navigateur à 32 étapes |
| UX-A à UX-D | Retouches après examen des captures réelles |

## Vérifié par l'orchestrateur (rejoué dans le checkout principal)

- `make test-workflow-full` : 82 passés, garde workflow OK, sur `fd4691a6` (résultats dans `/tmp`, car `var/` n'est pas inscriptible).
- `make test-investigation-browser` : parcours 32 étapes PASSÉ (87 s) sur l'arbre intégré avant UX-D ; UX-D l'a rejoué de son côté (1 passé).
- Frontend : `pnpm exec vitest run` 123 fichiers, 930 tests ; typecheck ; lint 0 erreur (2 avertissements existants dans `AnchorPicker.tsx`).
- Backend : `tests/workspace_agent` + harness : 754 passés ; lint et typecheck verts ; migrations 0027–0029 (186 tests de chaîne/migration) ; ShellCheck sans erreur (`-S error`).
- Suite backend complète (commande CI `-m "not workflow_fast and not workflow_full"`) : dernier passage sur `b7918620` = 9 934 passés, 1 échec.

## NON clos — à faire à la reprise

1. **Suite backend complète à rejouer à zéro échec** sur le dernier commit. L'échec restant
   (`test_signed_tool_call_route_attributes_and_refuses_token_alone`) est corrigé par `188c2f61`
   (preuve : horloge d'import décalée de 11 min, avant 1 failed / après 9 passed) mais la suite complète n'a PAS été relancée depuis.
   Cause : le correctif A1 avait ancré le bail par défaut sur l'heure d'import alors que la route signée utilise l'heure murale. C'est un oubli de ma revue de A1.
2. **Revues indépendantes R1 à R6 : annulées en cours, aucun verdict.** À relancer en sessions fraîches (Luna Audit, xhigh) sur le commit final : R1 sécurité, R2 concurrence, R3 provenance, R4 architecture, R5 UX (ouvrir réellement les captures), R6 éditorial/PDF. Une revue NO-GO impose correctif + re-revue fraîche. Points déjà identifiés à soumettre :
   - plafond 4 MiB (`MAX_ARTIFACT_BYTES`) ajouté à la lecture du blob du vérificateur `canonical_code` (comportement existant modifié) ;
   - un fichier trop gros sous `scratch/notes` fait refuser tout le collect (fail-closed, peut gêner) ;
   - état `RECONCILING` : un `collect` est refusé tant que la session n'est pas réconciliée — vérifier qu'une sortie est garantie ;
   - chevauchement scanner/suppression de projections du sidecar (A4 corrige le test seulement) : vrai défaut de production possible ? ;
   - SHA du binaire Typst local différent de `infra/typst.lock` (à trancher : artefact local ou défaut de verrou) ;
   - motif de la dernière décision G2 non fourni par `GET /envelope` (affiché « non transmis par l'API ») ;
   - `SamplesResultsView.tsx` réécrit (~2000 lignes de diff).
3. **`docs/m24/M24-2-FINAL-REPORT.md`** : à écrire, avec commit/commande/environnement/statut/niveau de preuve/restriction pour chaque résultat, tableau des capacités attestées / non attestées, procédure d'activation contrôlée des CLI, liste des autorisations externes restantes.
4. Écart d'ordre assumé : les lots B ont démarré avant la clôture du gate du lot A (fichiers disjoints). À consigner dans le rapport.

## Non attesté (limites connues)

- Isolation bwrap réelle : le noyau de cette machine refuse les namespaces utilisateur ; seuls les `argv` construits et des faux CLI sont testés.
- Vrais CLI Codex et Claude, VT, Shodan, Bridge réels : jamais appelés (autorisation séparée requise). `M24_AGENT_SESSION_KEY` non activé.
- Lecteur PDF intégré : Chromium headless ne l'affiche pas ; le repli et le statut sont visibles, le PDF Typst est vérifié par `pdftotext`/`pdftoppm`.
- Test PG de la rotation équitable : couvert par E3 (à confirmer par R2).

## Recettes d'environnement (pièges vérifiés)

- Chemins courts obligatoires pour les sockets UNIX (limite 108 octets) : `--basetemp=/tmp/pt…` et `TMPDIR=/tmp/m242-tmp`. Un `TMPDIR` ou `basetemp` long fait échouer les tests « real sidecar » avec « session secret handoff cannot be armed ».
- Typst : `TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst` (le `typst` snap est cassé).
- PG de test réservé (jamais la base vivante) : `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`.
- `make test-workflow-full` : ajouter `WORKFLOW_RESULTS_DIR=/tmp/…` (le répertoire `var/` n'est pas inscriptible).
- `make test-investigation-browser WORKFLOW_RESULTS_DIR=/tmp/… INVESTIGATION_BROWSER_API_PORT=<port libre> M24_REAL_CAPTURE_DIR=/tmp/…` (`UV_SYNC=:` si `uv sync` tente le réseau).
- Sous-agents en worktree : pas de venv ni de `node_modules` ; lier `backend/.venv` et `frontend/node_modules` du checkout principal (ne pas committer les liens) et lancer pytest avec `PYTHONPATH=src:.`. Plusieurs agents ont livré des tests jamais exécutés : tout rejouer dans le checkout principal avant de le compter.
- Les tests de timing sous charge : ne jamais conclure « flake » sans le rejouer seul ET en suite complète.

## Reprise, dans l'ordre

1. `git status` et `git log --oneline -3` (attendu : HEAD `188c2f61`).
2. Relancer la suite backend complète (commande CI, chemins courts) ; corriger toute régression.
3. Relancer R1–R6 sur le HEAD (agents frais).
4. Corriger les NO-GO éventuels, re-revue fraîche.
5. Écrire le rapport final, rejouer le parcours navigateur, commit local final.
6. Rien n'est poussé ni déployé sans autorisation explicite.
