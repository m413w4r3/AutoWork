# Audit du code mort et de la dette documentaire — Lot C

Date : 12 octobre 2026. Branche : `worktree/singing-trail`, HEAD initial `806d214`.
Mode : analyse uniquement. Aucun code applicatif n’a été supprimé ni modifié.

## Résultat

Deux candidats ressortent, avec des niveaux de preuve différents. Le premier est
un module Python sans importeur dans le dépôt. Le second est une interface
frontend et son client API qui ne sont pas atteignables depuis l’entrée de
l’application ; le contrat API correspondant reste documenté et actif.

Le contrôle `ruff` des imports inutilisés n’a trouvé aucun `F401`/`F811`. Aucun
routeur, job, script ou paquet n’est proposé à la suppression. Les vérifications
ne prouvent pas l’absence de données de jobs persistées : aucune base n’a été
interrogée.

## Candidats de suppression

### Lot 1 — helpers typographiques Python isolés

- **Chemin / symboles :** `backend/src/cti_app/application/french_typography.py` —
  `_MONTHS`, `format_french_date`, `format_french_month`.
- **Preuve :** le graphe d’imports AST de `backend/src/cti_app` (269 modules) ne
  trouve aucun importeur de ce module ; zéro erreur de parsing. La vérification
  AST ci-dessous parcourt aussi `backend/tests`, `backend/tools` et les autres
  fichiers Python sous `backend` : `matching_imports=[]`, zéro erreur. La
  recherche des noms exacts ne trouve leurs déclarations que dans le fichier
  candidat. Le test `test_french_publication_dates_use_first_day_typography`
  vérifie le rendu actif, mais n’importe aucun de ces helpers. Le module n’a ni
  route, ni acteur Dramatiq, ni point d’entrée CLI, ni inscription dans un
  registre.

  ```sh
  UV_CACHE_DIR=/tmp/aw-chantier-ctx-cache uv run --no-project --no-sync --python 3.12 python - <<'PY'
  import ast
  from pathlib import Path
  candidate = Path('backend/src/cti_app/application/french_typography.py')
  needles = {'format_french_date', 'format_french_month', 'french_typography'}
  hits = []
  for path in Path('backend').rglob('*.py'):
      if path == candidate:
          continue
      tree = ast.parse(path.read_text(encoding='utf-8'))
      for node in ast.walk(tree):
          if isinstance(node, ast.ImportFrom):
              if node.module and 'french_typography' in node.module:
                  hits.append((str(path), node.lineno, node.module))
              hits.extend((str(path), node.lineno, alias.name) for alias in node.names if alias.name in needles)
          elif isinstance(node, ast.Import):
              hits.extend((str(path), node.lineno, alias.name) for alias in node.names if 'french_typography' in alias.name)
  print('parse_errors=0; matching_imports=', hits)
  PY
  ```

  Sortie : `parse_errors=0; matching_imports= []`.
- **Dépendances vérifiées :** aucun importeur statique, aucune référence du nom
  de module ou des deux fonctions dans les tests, scripts, outils ou frontend ;
  aucune inscription dynamique connue. La date de publication actuellement
  testée est rendue par le chemin Typst, sans appel à ce module.
- **Impact attendu :** aucun changement du comportement actuellement accessible
  dans l’application ; suppression d’un helper qui n’a pas de consommateur
  local démontré.
- **Tests de protection :** garder le test existant
  `backend/tests/test_typst_rendering.py::test_french_publication_dates_use_first_day_typography`
  pour le rendu public. Pour une suppression ultérieure, rejouer ce test et le
  contrôle Ruff. Aucun nouveau test du helper ne serait justifié puisqu’il n’a
  aucun appelant.
- **Restauration :** `git revert` du commit de suppression isolé.
- **Confiance :** **démontré** pour l’absence de consommateur dans ce dépôt.

### Lot 2 — panneau de conversations non monté dans le frontend

- **Chemins / symboles :**
  `frontend/src/components/AnalysisConversations.tsx` —
  `AnalysisConversations` ; `frontend/src/api/modelConversations.ts` — client
  consommé uniquement par ce panneau.
- **Preuve :** `rg -n 'AnalysisConversations' frontend/src` ne retourne que la
  définition dans le panneau et son import/utilisation dans
  `AnalysisConversations.test.tsx`. L’entrée `main.tsx` monte `App`; les imports
  de `App.tsx` ne mentionnent pas ce panneau. Le graphe des imports littéraux
  des fichiers TS/TSX sous `frontend/src` donne le panneau comme n’ayant aucun
  importeur runtime ; le seul import entrant vient de son test. Le client
  `modelConversations.ts` est importé par le panneau. La recherche
  `rg -n 'import\.meta\.glob|React\.lazy|lazy\(' frontend/src` ne retourne
  rien ; aucun chargement différé connu ne le monte.
- **Dépendances vérifiées :** le test du composant prouve qu’il est exercé en
  isolation ; il ne le rend pas accessible depuis l’application. En revanche,
  `backend/src/cti_app/api/model_conversations.py` est inclus par
  `api/main.py:424`, et `docs/model_conversations.md:27-34,129-140` documente les
  routes. Ces routes et le contrat serveur ne font donc pas partie du candidat.
- **Impact attendu :** enlever le panneau et son client n’affecterait pas le
  bundle frontend actuellement atteignable. Cela ferait disparaître une
  interface de conversations persistantes qui peut servir au futur pivot ; la
  décision produit n’est pas démontrée par le graphe.
- **Tests de protection :** le test existant
  `frontend/src/components/AnalysisConversations.test.tsx` protège le panneau
  s’il est conservé ou monté. Si sa suppression est validée, vérifier le build,
  le typecheck et les tests frontend ; garder les tests et la documentation
  backend de l’API.
- **Restauration :** `git revert` du commit isolé qui retirerait le panneau et
  son client, sans toucher au contrat serveur.
- **Confiance :** **probable** comme code actuellement orphelin ; ne pas
  supprimer avant arbitrage sur son rôle dans le futur pivot.

Les deux lots sont indépendants. Ordre recommandé : Lot 1, puis Lot 2 après
arbitrage produit.

## Vérifications négatives et périmètre non prouvé

- **Imports Python :**
  `UV_CACHE_DIR=/tmp/aw-chantier-ctx-cache uv run --offline --no-project --no-sync --python 3.12 ruff check --select F401,F811 backend/src/cti_app backend/tools scripts`
  a répondu `All checks passed!`.
- **Fonctions/classes Python :** Vulture n’est pas installé dans l’environnement
  local ; l’essai hors ligne a répondu `Failed to spawn: vulture`. Le graphe AST
  reste une heuristique d’imports et ne suffit pas à supprimer un symbole
  consommé par introspection externe.
- **Frontend :** le contrôle demandé avec
  `pnpm --dir frontend --pm-on-fail=ignore exec tsc --noEmit --noUnusedLocals --pretty false -p tsconfig.app.json`
  n’a pas démarré. Le worktree déclare pnpm `10.15.0`, Corepack fournit
  `11.20.0` et tente d’installer la version attendue ; l’accès réseau est
  indisponible. Knip n’a pas été lancé.
- **FastAPI :** aucun module routeur orphelin identifié : les routeurs définis
  sous `cti_app.api` sont inclus dans `api/main.py:415-429`. Aucun handler
  individuel n’est recommandé à la suppression sur cette passe.
- **Dramatiq et kinds de jobs :** `workers/broker.py`, `workers/tasks.py`,
  `workers/analysis_tasks.py` et le `JobRegistry` couvrent enregistrement et
  dispatch. Aucun acteur/kind n’est proposé comme mort. L’état des lignes de
  jobs persistées n’a pas été consulté ; l’absence de dispatch actuel ne prouve
  pas qu’un ancien kind n’existe plus en base.
- **Scripts et outils :** pas de script démontré abandonné. Exemples de
  consommateurs vérifiés : `Makefile:433` appelle `scripts/diagnostics.py`, la
  CI appelle `scripts/typst_edition_smoke.py`, la doc goodware décrit
  `scripts/import_goodware.py`, les tests hors ligne chargent les gardes de
  configuration/fixtures, et `infrastructure/smda.py` appelle
  `backend/tools/smda_extract.py`. Les outils avec une interface CLI explicite
  ne sont pas déclarés morts sur la seule absence d’un appel interne.
- **Dépendances :** aucun paquet supprimable avec confiance élevée. Certains
  paquets sont nécessaires par configuration ou CLI plutôt que par import
  Python direct : `asyncpg` via le DSN, `python-multipart` via
  `request.form()`/`multipart/form-data`, `uvicorn` via la commande de lancement,
  et CAPA via le sous-processus `capa`. Les dépendances frontend sont aussi
  référencées dans les composants, tests ou configurations Vite/ESLint/Playwright.
  L’absence de Knip/typecheck frontend empêche de conclure à l’exhaustivité.
- **Doublons :** aucun doublon de logique n’est retenu ; aucune simple similarité
  de formatage/parsing n’a été traitée comme preuve d’un doublon.

## À ne pas supprimer

- Les mécanismes de récupération et réconciliation — dont
  `recover_abandoned_jobs`, les probes/adoptions contrôlées et les reprises à
  clé d’idempotence — protègent contre perte de travail et double soumission.
  L’état persistant des jobs reste non vérifié.
- `backend/src/cti_app/application/production_extraction.py` et les lecteurs
  historiques adjacents : `docs/production.md:234-236` les désigne comme
  frontière de lecture utilisée par l’export d’état et certaines vues du Repair
  Desk. Ne pas confondre compatibilité de lecture avec voie d’écriture canonique.
- Les adaptateurs du Repair Desk, l’export/import d’état, les lecteurs de
  snapshots/artifacts historiques et leurs validateurs ; ils gardent des
  artefacts conservés accessibles et contrôlent les réparations.
- Le routeur et le client serveur des conversations modèles : le contrat est
  documenté et inclus dans l’application ; le sujet peut rester utile au futur
  pivot même si le panneau frontend est actuellement débranché.
- Les contrôles de TLP, d’identité vérifiée, SSRF, d’autorisation d’envoi externe,
  d’immutabilité, d’intégrité par hash, de reprise et d’idempotence. Aucun n’a
  été classé mort.
- `backend/src/cti_app/workers/scheduler.py` et
  `backend/src/cti_app/model_run_diagnostics.py` ont zéro importeur dans le graphe, mais sont
  des points d’entrée explicites : README backend, respectivement Makefile.
- Les assertions de baseline unique dans `backend/README.md:109` et
  `docs/persistence_and_storage.md:167` sont cohérentes avec le Lot B ; ne pas
  les réécrire sur la base d’un état antérieur.

## Documentation obsolète

### Documents historiques — marquer, ne pas supprimer

- `docs/design/real-run-cti-quality-recovery-plan.md` est un audit daté du
  2 octobre 2026, qui indique à la ligne 2 l’état examiné `08c89f0`. Le document
  le qualifie lui-même de plan/contextualisation à la ligne 19, mais n’a pas de
  bandeau `HISTORIQUE`. Ajouter après le titre :

  > **HISTORIQUE — 2026-10-02.** Audit ponctuel de l’état `08c89f0` ; ses constats
  > ne décrivent pas nécessairement le code courant.

  La ligne 19 lie `../references/plan.md`, cible absente de l’inventaire
  `docs/`. Remplacer cette phrase par :

  > Les numéros L0 à L9 ci-dessous désignent les lots de ce document, sans créer
  > de nouveaux tickets AW. Les constats sont ceux de l’état examiné et doivent
  > être revérifiés contre le code courant avant toute reprise.

- `docs/brief_workflow.md:3-5` porte déjà le bandeau `Document historique /
  legacy` et dit que ses endpoints sont hors du workflow cible. Le conserver ;
  ne pas supprimer sur cette base (constat daté). Ce fichier legacy a depuis été retiré lors du nettoyage agentique du 2026-10-23 ; l'original reste consultable dans Git à `1e825f84f08e806df495fc17c937ab19ba696b4c`.

### Contrats actifs — corrections

- La recherche ciblée des formulations « AutoWork ne produit pas d’articles »
  et équivalents dans `README.md`, `docs/` et `backend/README.md` n’a trouvé
  aucune ligne correspondante ; aucune correction n’est justifiée par cette
  preuve.
- `backend/README.md:109` et `docs/persistence_and_storage.md:167` décrivent
  `0001_baseline` comme baseline unique. C’est l’état demandé par le Lot B ;
  aucune correction à proposer.
- L’unique correction documentaire démontrée est le lien absent et le statut
  historique du plan du 2 octobre ci-dessus. Aucun autre contrat actif n’a été
  modifié ni déclaré obsolète sur simple intuition.

## Non vérifié

Aucune suite de tests n’a été exécutée. Le typecheck frontend est bloqué par le
décalage Corepack/pnpm et l’absence d’accès réseau. Aucune base PostgreSQL n’a
été démarrée ou consultée ; les kinds de jobs persistés restent donc hors de la
preuve. Vulture et Knip n’étaient pas disponibles. L’audit ne couvre pas les
consommateurs externes au dépôt qui importeraient directement le module Python
ou piloteraient volontairement un ancien écran.
