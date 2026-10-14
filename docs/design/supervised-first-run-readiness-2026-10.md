# Readiness — premier run CTI technique supervisé

**Décision au 14 octobre 2026 : NO_GO pour un premier run réel aujourd'hui.**
Le chemin critique n'a pas de smoke réel, le Bridge local est inaccessible, le
proxy vtUI n'est pas configuré et l'identité locale `dev-analyst` n'est pas
vérifiée.

Le dépôt est **prêt à valider** : les contrôles et parcours offline sont verts.
Passer à `GO_PARTIEL`, puis à `GO`, exige une approbation explicite de
l'opérateur et des smoke live dans cet ordre : Bridge, VT via vtUI, puis Shodan.
Une CI verte ou `offline_simulated` ne vaut jamais un GO live. `NOT_VALIDATED`
n'est jamais `PASS`.

## Base et branche examinées

- Base : `main` à `f01acc9`; le workflow GitHub Actions `38553748944` était
  vert à cette date.
- Branche : `chantier/premier-run-technique-supervise-2026-10-14`, pas encore
  poussée.
- Le prévol réel conclut `NO_GO` : pas de DSN ni de référence exacte, Bridge
  local inaccessible, proxy vtUI non configuré, identité `dev-analyst` locale.

## Gates L0–L7

Les gates décrivent la validation live de la mission. Les validations offline
ci-dessous ne les font pas passer.

| Gate | Verdict | Justification |
|---|---|---|
| L0 — identité et référence | `NOT_VALIDATED` | Pas d'identifiants canoniques exacts pour le run; `/api/identity/status` annonce `verified=false` pour `dev-analyst`. Le gate d'identité du prévol reste non validé dans ce mode. |
| L1 — environnement et base | `NOT_VALIDATED` | L'environnement de test est corrigé et validé, mais aucune DSN/reference exacte ni base pilote identifiée n'est présente au prévol réel. |
| L2 — services | `FAIL` | Bridge local inaccessible et proxy vtUI non configuré. |
| L3 — gouvernance G2 | `BLOCKED_APPROVAL` | L'opérateur doit autoriser explicitement fournisseurs, budgets et divulgation. |
| L4 — chemin fournisseur | `NOT_VALIDATED` | Aucun appel Bridge, VT/vtUI ou Shodan réel; tout résultat fournisseur est `offline_simulated`. |
| L5 — quarantaine et analyse | `NOT_VALIDATED` | Les tests réels de confinement Bubblewrap passent sur échantillon inerte; aucun échantillon autorisé n'a été analysé dans ce run. FLOSS, MWCP et REA/Ghidra sont indisponibles sur cette machine. |
| L6 — avis, G3 et PDF | `FAIL` | L'API d'aperçu PDF du brouillon « Avis de l'analyste » n'a pas été vérifiée et aucune route correspondante n'est livrée; G3 reste à approuver. |
| L7 — publication | `NOT_APPLICABLE` | La publication est hors périmètre du premier run; aucune publication ne doit être lancée. |

Une CI verte n'est pas un GO live. Les gates `NOT_VALIDATED` ne peuvent pas
être interprétés comme `PASS`.

## Corrections de sécurité confirmées

La revue indépendante a confirmé quatre défauts, corrigés dans cette branche :

1. `live` rejoint `pilot`, `prod` et `production` dans la liste partagée des
   environnements protégés. Sans jeton, l'identité locale refuse un bind non
   loopback au démarrage et refuse les requêtes sur un bind exposé.
2. Avec `OPERATOR_API_TOKEN`, seuls les chemins exacts
   `/api/health/live` et `/api/health/ready` sont exempts de Bearer;
   `/api/health/models` et les routes métier restent protégés. Le healthcheck
   Compose utilise `/api/health/ready` sans jeton.
3. Le proxy du serveur Vite ajoute le Bearer configuré par
   `OPERATOR_API_TOKEN`. La variable est transmise au service frontend côté
   serveur; elle n'est pas préfixée `VITE_` et n'entre pas dans le bundle.
4. Bubblewrap isole les PID, IPC et UTS en plus du réseau. Le filtre reste
   installé par `bwrap --seccomp <fd>`, après le démarrage de Bubblewrap.

L'identité locale `dev-analyst` en loopback est conservée : l'accès local isolé
est accepté pour ce pilote. Elle reste `verified=false`, et son gate readiness
ne peut pas être `PASS`. Un bind non loopback est interdit avec identité locale
en `pilot`, `prod`, `production` et `live`.

Les ports publiés par défaut pour MinIO, backend et frontend sont liés à
`127.0.0.1`. `APP_ENV`, `API_BIND_HOST` et `OPERATOR_API_TOKEN` sont
paramétrables; le `CMD` Uvicorn lit `API_BIND_HOST`.

## Environnement et défaut P0

Le venv backend avait été construit sur Miniconda CPython 3.12.4, interdit par
`.claude/CLAUDE.md`; il provoquait deux timeouts TShark. Il a été reconstruit
avec `/usr/bin/python3.12` (CPython 3.12.3), sans Conda. Depuis, le
workbench réseau passe 16/16 tests. La commande de reconstruction utilisée :

```sh
rm -rf backend/.venv && cd backend && uv sync --locked --python /usr/bin/python3.12 --group dev --group analysis
```

Pour Typst sous Snap, `TMPDIR` doit être un chemin non caché sous `$HOME`;
dans cette session il vaut `$HOME/autowork-tmp`.

Le défaut P0 préexistant installait seccomp avant Bubblewrap. Ainsi,
`bwrap --unshare-net` ne pouvait pas monter `lo`; la sonde annonçait un
confinement qui échouait à la première exécution. Le filtre est maintenant
transmis avec `bwrap --seccomp <fd>`. Le test réel montre `socket()` → `EPERM`,
le PID du worker absent de `/proc`, l'environnement hôte inaccessible par ce
PID et les lecteurs catalogués confinés.

## Validations

Relancées après les corrections de cette branche :

- `make lint-backend` : OK, 729 fichiers.
- `make typecheck-backend` : OK, 706 fichiers.
- `make test-backend` : 3 613 passés, 22 désélectionnés.
- Tests ciblés sécurité/prévol/workbench : 60 passés; les tests Bubblewrap
  réels et `test_network_workbench.py` sont inclus.
- `cd frontend && pnpm lint && pnpm typecheck && pnpm test --run` : OK,
  37 fichiers de test, 289 tests passés.

Autres validations consignées le 14 octobre dans `.validation/` (journaux non
versionnés; non rejouées pendant cette passe de corrections) :

- `make test-integration` : 310 passés avec le PostgreSQL jetable Compose
  `postgres-test`.
- `make test-workflow` : 15 passés.
- `make test-workflow-full` : 23 passés; garde JUnit OK.
- `make test-workflow-browser-investigation` : 1 passé. Le manifeste et les
  releases historiques étaient inchangés avant repin; le PDF final a été
  vérifié avec Poppler.

## Capacités non validées

- Aucun appel fournisseur réel : Bridge, VT via vtUI et Shodan restent
  `LIVE_NOT_VALIDATED` / `NOT_VALIDATED`.
- Retrohunt n'est pas branché par défaut : flag OFF, aucun slot consommé. La
  limite de deux jobs par sujet et le refus du troisième sont testés offline.
- FLOSS et MWCP sont absents de cette machine. REA/Ghidra est absent; le runner
  REA confiné n'est pas livré.
- Aucune API n'a été vérifiée pour l'aperçu PDF de l'avis. L'aperçu d'édition
  existant ne prouve pas le rendu du brouillon d'avis.
- Le gate L5 « un autre développeur suit la doc sur un environnement vierge »
  est `NOT_VALIDATED` : cette vérification par un tiers n'a pas été faite.
- L'écart éditorial CN2609 reste : l'Avis de l'analyste est structuré, mais
  les sections n'ont pas de notes contextualisées par section.

## Décisions manuelles restantes

1. Fournir et vérifier les UUID, version, SHA-256 et TLP exacts, ainsi qu'une
   DSN explicitement jetable.
2. Résoudre Bridge/vtUI et choisir une identité vérifiée pour tout gate qui
   l'exige; l'identité locale seule ne peut le passer.
3. Après approbation explicite, exécuter et conserver les smoke live dans
   l'ordre Bridge, VT/vtUI, Shodan, avec une enveloppe G2 adaptée à chaque
   étape.
4. Relire l'avis, décider G3 et résoudre le chemin d'aperçu PDF. La publication
   reste exclue.

Risque P1 : le conteneur backend ne tourne pas sous un utilisateur dédié; le
pilote local tourne sous l'utilisateur non privilégié de la machine. Un
utilisateur dédié reste à mettre en place avant toute exposition.

Le runbook opératoire, ses commandes existantes et la commande de régénération
du prévol sont dans
[investigation-supervised-first-run.md](../operations/investigation-supervised-first-run.md).
Les rapports JSON et Markdown générés sont supprimés et ignorés par Git : ils
sont propres à une machine et à un SHA.
