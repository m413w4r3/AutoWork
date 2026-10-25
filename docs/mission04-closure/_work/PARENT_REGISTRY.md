# Mission 04 closure — registre parent (handoff SHA-based)

**Baseline vérifiée :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
(`origin/main`, `git ls-remote`).
**Branche de clôture :** `mission04/closure-after-main`.
**HEAD de clôture au dernier point de contrôle :** `2a5987e4`
(`fix(p10): accept valid closed credentials containing punctuation`).

## Intégré sur la branche de clôture

| SHA | Contenu |
|---|---|
| `ccff3444` | smoke Typst `document_label`, import navigateur `DEFAULT_SAMPLE_*`, Prettier frontend |
| `f92d848d` | L1-R5 : scanner borné LF/CRLF, P10 v5 |
| `a3b2860f` | CI-0a : dette mypy hors L4 (cherry-pick de `664674a4`) |
| `e106adc2`, `76209ea9` | documentation de clôture |
| `5a23a291` | correctif R5 R1 : refus explicite des credentials non bornés/ambigus |
| `2a5987e4` | correctif R5 R2 : accepte les scalaires fermés valides contenant `}`/`]`/`,` |

## Branches de travail non encore intégrées

| Lot | Branche | Worktree | SHA / état |
|---|---|---|---|
| L4 D1–D4 | `mission04/l4-context` | `/home/nill/perso/work/AutoWork_l4` | en cours (PG réel + test integration ajouté) |
| L5 D5–D6 | `mission04/l5-pdf` | `/home/nill/perso/work/AutoWork_l5` | commit `c71e5c4a`, 56 tests ciblés verts, revue `cd40c7cf` en cours |
| L21 D8–D9 | `mission04/l21-advisor` | `/home/nill/perso/work/AutoWork_l21` | commit `981632a7`, revue `fa293266` REVISE ; réparation R2 en cours |
| CI-0b FAST | `mission04/ci0-workflow` | `/home/nill/perso/work/AutoWork_ci0b` | en cours (W13-D5/D7/W14-A verts ; W14-E qualité retrohunt) |
| CI-0c FULL | `mission04/ci0-protocol` | `/home/nill/perso/work/AutoWork_ci0c` | en cours (W15/W18 corrigés ; pilote FULL) |
| SAMPLE D7 | à créer après L4 | — | non commencé |
| Replay offline | à créer après intégration | — | non exécuté |

## Environnement de test

```sh
TEST_POSTGRES_ADMIN_DSN='postgresql+asyncpg://postgres:postgres@localhost:55432/postgres'
TYPST_BINARY=/tmp/aw-tools/typst/typst
D2_BINARY=/tmp/aw-tools/d2/d2
FONT_BUNDLE_ROOT=/tmp/aw-tools/typst-fonts
TMPDIR="$HOME/tmp/aw-m04"
```

PostgreSQL jetable : `cti-bulletin-postgres-test-1` port 55432. Typst 0.15.1,
D2 0.9.0, polices verrouillées dans `/tmp/aw-tools`. Ne jamais toucher la
stack live (`cti-bulletin-postgres-1`, MinIO, workers).

## Vérifications parent déjà faites

- `mypy src tests` : 60 → **35 erreurs**, toutes dans les 5 fichiers L4 après
  CI-0a (`a3b2860f`).
- 104 tests CI-0a ciblés verts ; 99 tests P10 verts après `5a23a291`.
- Frontend : lint, typecheck, **293 tests**, build Vite verts.
- Compilation Typst de la fixture CI corrigée : PDF + texte extraits.
- Sonde L1 stricte : 44/44 PASS (LF/CRLF, refus `}`/`]`/`,`, quotes internes,
  antislashs 0–7, auteur innocent, linéarité).
- Préflight push : seul workflow `CI`, aucun déploiement/hook/environnement ;
  `main` non protégée (aucun ruleset).

## Prochaines actions

1. Attendre/valider les commits L4, L5, CI-0b, CI-0c ; revues xhigh L4/L5.
2. Intégrer L21 après `APPROVED`.
3. Créer SAMPLE depuis le HEAD intégré, implémenter D7, revue xhigh.
4. Créer le test de replay offline transverse.
5. Gates croisés sur HEAD intégrateur : mypy 0, Ruff, suite backend complète
   avec Typst/fonts, workflow FAST/FULL, J2 navigateur, intégrations PG,
   frontend e2e, smoke Typst.
6. Merge non destructif dans `main`, préflight répété, push non forcé, vérifier
   le run CI sur le SHA poussé.
