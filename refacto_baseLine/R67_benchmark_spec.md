# R67 — Benchmark A/B pré-refactor vs final — Protocole gelé

## But

Mesurer honnêtement l'effet du refactor sur la navigation agent.

Ce task produit DEUX commits distincts :

    R67a — benchmark specification frozen
    R67b — benchmark results

INTERDICTION ABSOLUE de modifier la spec R67a après avoir exécuté
la première requête benchmark.

Aucun code applicatif.
Aucun changement de ctx.py.
Aucun tuning.

---

## Références gelées

Baseline pré-refactor :

    2c7a15d1bfe905554f64d560689c5f9e111fc400

État final à mesurer :

    882c8a695c8df8144aba194f8ad190ba36456865

Outil de mesure :

    scripts/ctx/ctx.py

pris EXACTEMENT au SHA final :

    882c8a695c8df8144aba194f8ad190ba36456865

Les commits R67 eux-mêmes ne font donc pas partie du système mesuré.

---

## R67a — GELER LE PROTOCOLE

AVANT toute query benchmark, créer :

    refacto_baseLine/R67_benchmark_spec.md

avec EXACTEMENT ce qui suit.

### Engine

Mode :

    --lexical-only

Index frais pour chaque snapshot :

    build --lexical-only

Aucun embedding.
Aucun BASE_URL.
Aucun EMBEDDING_API_KEY.
Aucun .env.

Paramètres :

    -k 8
    default per-file
    default relative floor
    default lexical/meta weights

Aucune option de ranking supplémentaire.

### Les 12 requêtes EXACTES

Q1

    conversation lifecycle release after discovery amendment production publication

Q2

    persist and reload model conversation state database repository

Q3

    recover incomplete discovery operation after failed or interrupted model run

Q4

    validate discovery merge conflicts before applying cumulative merge

Q5

    detect stale discovery merge and replan outdated merge run

Q6

    brief amendment repository indexing querying storage retrieval

Q7

    create edition frontend form submit API persistence

Q8

    production workflow orchestrate parse render publish stages

Q9

    ChatGPT bridge browser extension server request conversation routing

Q10

    published brief immutability amendment preservation rules

Q11

    replay edition workflow lineage mapping activation

Q12

    evidence pack coverage calculate contributions tracking

Ces chaînes sont immuables après le commit R67a.

---

## Règles de scoring gelées

### Relevant source hit

Un résultat est relevant uniquement s'il s'agit d'un fichier source qui :

1. implémente directement le comportement demandé ; ou
2. orchestre directement le comportement demandé.

Ne comptent PAS comme hit :

    test
    fixture
    migration
    ADR
    documentation
    benchmark
    AGENTS
    generated artifact

sauf si la requête demandait explicitement l'un d'eux
(aucune des 12 ne le fait).

Un simple import/transit ne suffit pas.

### first_relevant_rank

Rang du premier résultat relevant dans les résultats ctx.

Si aucun dans top8 :

    MISS

Aucun rang artificiel 9.

### top3

PASS query si :

    first_relevant_rank <= 3

Sinon :

    FAIL

Un MISS compte comme FAIL.

### top8

PASS query si au moins un hit relevant dans les 8.

MISS = FAIL.

---

## Mesure "fichiers nécessaires"

Pour chaque scénario et chaque snapshot, après localisation du comportement,
déterminer :

    minimal_owner_files

Définition :

Le plus petit ensemble de fichiers source qu'un agent doit réellement lire
pour comprendre où effectuer une modification bornée correspondant à la query.

Ne pas compter :

    AGENTS
    tests
    docs
    migration
    fichiers seulement importés/transitifs

Chaque fichier compté doit être nommé dans le rapport.

Ne jamais mettre 0 :

si aucun owner n'est retrouvé, valeur :

    n/a

---

## Mesure de bruit de navigation

Calculer également, de façon mécanique :

    files_before_first_hit
    lines_before_first_hit

`files_before_first_hit` :

nombre de fichiers distincts dans les résultats rang 1 jusqu'au premier hit,
hit inclus.

Si MISS :

    nombre de fichiers distincts présents dans top8

`lines_before_first_hit` :

somme de :

    end - start + 1

des résultats rang 1 jusqu'au premier hit, hit inclus.

Si MISS :

    somme sur top8.

Cette métrique ne nécessite aucune estimation de tokens.

---

## Métriques agrégées

Pour BASELINE et FINAL calculer séparément :

    top3_hit_rate
    top8_hit_rate

    median_first_relevant_rank
    mean_first_relevant_rank
        (MISS exclu uniquement de cette statistique de rang,
         mais jamais des hit rates)

    median_minimal_owner_files
    mean_minimal_owner_files
        (n/a explicitement rapporté)

    median_files_before_first_hit
    mean_files_before_first_hit

    median_lines_before_first_hit
    mean_lines_before_first_hit

---

## Cibles GELÉES

État final absolu :

    top3_hit_rate >= 80 %
    median minimal_owner_files <= 3

Amélioration comparative :

    >= 40 % reduction du median minimal_owner_files

Calcul :

    (baseline_median - final_median) / baseline_median * 100

Si la médiane ne permet pas de montrer les changements parce qu'elle est
identique des deux côtés, NE PAS inventer une autre cible.

Rapporter simplement le résultat.

Les autres métriques :

    files_before_first_hit
    lines_before_first_hit
    top8
    mean values

sont SECONDARY.

Elles expliquent le résultat mais ne remplacent jamais une cible primaire
ratée.

---

## Engagement méthodologique

> The queries, scoring rules, thresholds, baseline SHA, final SHA, and ctx.py
> version in this document were frozen before benchmark execution. They must
> not be changed in response to observed results.

---

## Commit R67a

Avant toute requête benchmark :

    git add refacto_baseLine/R67_benchmark_spec.md
    git commit -m "R67a: freeze navigation benchmark protocol"

STOP après le commit et vérifier :

    git show HEAD:refacto_baseLine/R67_benchmark_spec.md

Ne plus modifier ce fichier.

Ensuite seulement commencer R67b.

---

## R67b — EXÉCUTER LA SPEC GELÉE

Créer deux worktrees temporaires détachés.

Exemple :

    BASE_DIR=$(mktemp -d)
    FINAL_DIR=$(mktemp -d)

    git worktree add --detach "$BASE_DIR" \
      2c7a15d1bfe905554f64d560689c5f9e111fc400

    git worktree add --detach "$FINAL_DIR" \
      882c8a695c8df8144aba194f8ad190ba36456865

Ne jamais inspecter .git.

### Utiliser EXACTEMENT le même ctx.py

Extraire le script de l'état final dans un fichier temporaire :

    TOOL_DIR=$(mktemp -d)

    git show \
      882c8a695c8df8144aba194f8ad190ba36456865:scripts/ctx/ctx.py \
      > "$TOOL_DIR/ctx.py"

Ne modifier ce fichier.

Pour chaque worktree, lancer ce même fichier depuis le cwd du worktree.

Ainsi :

    find_root()

pointe vers le snapshot mesuré, mais le moteur de ranking est strictement
identique.

### Construire les deux index frais

Sans credentials :

    env -u BASE_URL -u EMBEDDING_API_KEY \
      sh -c 'cd "$BASE_DIR" && uv run "$TOOL_DIR/ctx.py" build --lexical-only'

    env -u BASE_URL -u EMBEDDING_API_KEY \
      sh -c 'cd "$FINAL_DIR" && uv run "$TOOL_DIR/ctx.py" build --lexical-only'

Adapter uniquement la mécanique shell de quoting si nécessaire.

Ne changer aucune option ctx.

Vérifier dans chacun :

    status

Attendu :

    source_index = current

`dense_embeddings` peut être incomplete :
ce benchmark est lexical-only.

### Exécuter les 12 queries

Pour chaque query EXACTEMENT telle que gelée :

    uv run "$TOOL_DIR/ctx.py" query \
      "<EXACT QUERY>" \
      -k 8 \
      --lexical-only \
      --json

Exécuter :

    une fois dans BASE_DIR
    une fois dans FINAL_DIR

Aucune reformulation.

Aucun retry avec d'autres mots.

Aucun `--path`.

Aucun targeted rg avant d'avoir sauvegardé le résultat brut de la query.

### Sauvegarde des résultats bruts

Créer temporairement hors repo les 24 sorties JSON.

Après exécution complète, elles peuvent être utilisées pour le scoring.

Ne pas modifier une query et la rejouer parce que le résultat semble mauvais.

Si une commande échoue techniquement :

    noter ERROR

et ne pas remplacer la query.

### Ground truth / vérification des hits

APRÈS avoir capturé les 24 résultats :

Pour chaque résultat candidat, inspecter uniquement les ranges retournées
nécessaires.

Si la pertinence reste ambiguë :

utiliser un targeted rg dans LE SNAPSHOT CONCERNÉ.

Ce targeted rg sert uniquement à déterminer :

    relevant / not relevant
    minimal_owner_files

Il ne change jamais le classement ctx enregistré.

Documenter chaque jugement non évident.

---

## Résultat

Créer :

    refacto_baseLine/R67_ab_results.md

Il doit commencer par :

    Benchmark spec commit: <SHA R67a>
    Baseline: 2c7a15d1...
    Final: 882c8a695...
    ctx.py: 882c8a695...

Puis tableau 12 lignes avec colonnes :

    Q
    baseline first rank
    final first rank
    baseline top3
    final top3
    baseline owner files
    final owner files
    baseline files-before-hit
    final files-before-hit
    baseline lines-before-hit
    final lines-before-hit

Sous le tableau, pour chaque Q :

    exact query
    baseline relevant owner(s)
    final relevant owner(s)
    notes de scoring

Puis métriques agrégées.

---

## Verdicts obligatoires

Évaluer SANS modifier les seuils :

### A — Final top3

    PASS si >=80 %
    FAIL sinon

### B — Final owner files

    PASS si median <=3
    FAIL sinon

### C — Reduction owner files

    PASS si >=40 %
    FAIL sinon

Aucun "PASS-ish".
Aucun seuil secondaire substitué.

Puis rapporter séparément les métriques secondaires.

---

## Interprétation autorisée

Après calcul seulement, expliquer POURQUOI un score est bon ou mauvais.

Mais :

- ne pas supprimer un scénario ;
- ne pas reclasser une query parce qu'elle est gênante ;
- ne pas changer une query ;
- ne pas modifier le seuil ;
- ne pas remplacer la médiane par la moyenne ;
- ne pas exclure les MISS du top3/top8 ;
- ne pas créer un score composite.

Si une limite méthodologique existe :

la documenter APRÈS le verdict.

---

## Aucun tuning dans R67

Même si FINAL échoue :

NE PAS modifier :

    ctx.py
    PATH_PRIORS
    weights
    chunking
    AGENTS.md
    architecture applicative

R67 mesure uniquement.

Un éventuel R68 pourra modifier le système sous test.

Il devra alors rejouer exactement la spec R67a.

---

## Cleanup worktrees

À la fin :

    git worktree remove --force "$BASE_DIR"
    git worktree remove --force "$FINAL_DIR"

Supprimer TOOL_DIR.

Ne pas inspecter .git.

---

## Write allowlist R67 complet

    refacto_baseLine/R67_benchmark_spec.md
    refacto_baseLine/R67_ab_results.md

Deux commits obligatoires :

    R67a: freeze navigation benchmark protocol
    R67b: record frozen navigation benchmark results

Aucun autre fichier.

---

## R73 — Runner reproductible

`scripts/ctx/benchmark.py` rejoue mécaniquement les 12 requêtes ci-dessus
contre l'index ctx.py courant, via les primitives existantes de `ctx.py`
(`load_chunks`, `rank_chunks`, `select_results`). Il ne modifie ni les
requêtes, ni la ground truth, ni le ranking.

Commande (protocole lexical-only gelé) :

    env -u BASE_URL -u EMBEDDING_API_KEY \
      uv run scripts/ctx/ctx.py build --lexical-only

    uv run scripts/ctx/benchmark.py --lexical-only

ou, équivalent :

    make ctx-benchmark

Sort avec un code non-zero si `top3_hit_rate < 80%`,
`top8_hit_rate < 83.3%`, ou `median_files_before_first_hit > 3`
(seuils ajustables via `--min-top3` / `--min-top8` /
`--max-median-files-before-hit`, ou désactivables via `--no-check`).

---

## Acceptance

- spec committée avant la première query ;
- SHA spec enregistré ;
- baseline exacte 2c7a15d1... ;
- final exact 882c8a695... ;
- même ctx.py exact sur les deux ;
- deux index lexicaux frais ;
- 12 queries identiques ;
- zéro reformulation ;
- 24 résultats capturés ;
- MISS comptés comme échecs top3/top8 ;
- seuils inchangés ;
- résultats publiés même s'ils sont mauvais ;
- aucun code modifié.
