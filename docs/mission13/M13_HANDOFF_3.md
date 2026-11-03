# M13 — handoff 3 (orchestrateur Sonnet 5.5 → suivant)

Lis `AGENTS.md`, `CLAUDE.md`, `backend/AGENTS.md`, `docs/mission13/M13_REPORT.md` §15-22. Ne répète pas les diagnostics.

## État exact
- `bridge-simp/integration` contient la fusion de 5 chantiers (voir rapport §20). **Déployé en production : `f1b32477` (ancien), PAS l'intégration actuelle.** PR #23 non fusionnée (refus du contrôle de permissions ; ne pas contourner).
- Sessions Paseo vivantes à la rédaction (récupère leurs rendus ; une session terminée ne se ré-interroge pas, en ouvrir une neuve par correction) :
  - `4d2edc1a` : correction du rejeu non idempotent (goodware) — branche `m13/fix-goodware-replay` (worktree `/home/nill/.paseo/worktrees/0y1qsxa1/m13-fix-goodware-replay`).
  - `9af62f1a` : correction du test C09 du corpus archivé — branche `m13/fix-archived-corpus-c09`.
  - `461a1e65` : revue finale n°3 de la garde MD5 (`9fe23a90`) ; `9aca3908` : revue finale n°2 de la soumission ambiguë (`30d86ad8`).
- Pour chaque rendu : rejoue TOI-MÊME les tests (les sandboxes Luna n'atteignent pas PostgreSQL ni le corpus Run12) : `cd <worktree>/backend && PYTHONPATH=src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest …` ; PostgreSQL `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres … -m integration`. Le test d'archive C09 ne s'exécute que dans `/home/nill/perso/work/AutoWork-bs-integ` (checkout principal). Référence de la base : 39 échecs préexistants (`/tmp/new_fail.txt`, à régénérer si absent : lancer la suite hors intégration sur `35d01975`).

## Prochaines actions permises (dans l'ordre)
1. Récupérer les 4 rendus ci-dessus ; si une revue donne FAIL : correction par session neuve, puis re-vérification. Si la 3e revue MD5 échoue encore, proposer à l'utilisateur de ne livrer que la partie sûre (messages de refus nommant la référence + validation stricte) ou de reporter la citation par MD5.
2. Fusionner les corrections dans `bridge-simp/integration`, rejouer toute la suite + PostgreSQL (7 fichiers listés au rapport §21), comparer aux 39 échecs de base, ruff.
3. Déployer (autorisation donnée : « déploiements nécessaires » ; `RULE_WORKBENCH_ENABLED=true`, checkout principal `/home/nill/perso/work/AutoWork` en `git checkout --detach <sha>` puis `make up`, vérifier les conteneurs backend/worker et `/health` du Bridge ; aucun opérateur en vol).
4. Charger la baseline goodware : `make goodware-build` (réutilise `.autowork-runs/m13-q1/goodware-index/v2`) puis `make goodware-load`, `make goodware-status`. **Autorisé par l'utilisateur** (« les goodware doivent toujours être chargés ») mais passe par le point d'entrée officiel uniquement ; sans binaires bénins réels, `rule.test` ne mesure pas de faux positifs (demander à l'utilisateur un jeu de fichiers bénins).
5. Chantier côté envoi (rapport §22) : F4, F5, F9 sans décision ; F1 et F6 attendent l'utilisateur.
6. Requalifier : essai sur enquête vierge, opérateur frais (espace neuf), prompt de trois phrases, moniteur `orch/watch5.sh` (alerte aussi sur erreurs de transport VT dans les journaux). Arrêt de l'opérateur seulement si VT/Bridge échouent de façon répétée SANS que l'opérateur s'adapte.

## Décisions qui reviennent à l'utilisateur
Fusion PR #23 ; route PCAP ; plan de reset ; mise en production ; jeu de binaires bénins ; F1 (réservation de contexte), F6, TLP AMBER (garde plus large que `backend/AGENTS.md`), filtre de secrets trop large ; arbitrage « clé d'idempotence stockée dans ModelRun (restreint), empreinte seulement dans messages » (choix de l'orchestrateur).

## Pièges
`pkill -f` tue le shell appelant (lire le PID). Pas de `sleep` en avant-plan. Erreurs d'outil dans `outbox/*.json` du relais. Les rapports d'agents Luna annoncent parfois des tests verts non exécutés : toujours rejouer. Ne jamais lire `.env`, `var`, `.secrets`, `chatGPT_Answers`.
