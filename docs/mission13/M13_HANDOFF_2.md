# M13 — handoff 2 (orchestrateur Sonnet 5.5 → suivant)

Lis d'abord `AGENTS.md`, `CLAUDE.md`, `backend/AGENTS.md`, `docs/mission13/M13_REPORT.md` §15-17. Ne répète pas les diagnostics du rapport.

## État
- Code déployé : `f1b32477` (fusion du chantier dossier ciblé), `RULE_WORKBENCH_ENABLED=true`. Aucun opérateur ni relais en vol. PR #23 non fusionnée (refus du contrôle de permissions ; ne pas contourner).
- Essais 3 (C), 4 (arrêt VT), 5 (D) audités ; fichiers hors dépôt : `/home/nill/perso/work/AutoWork/.autowork-runs/m13-q1/` (`orch/REGISTRY.md`, `audit_pack3|5/AUDIT_REPORT.md`, `operator3|4|5`). Lancement d'un essai : `M13_HANDOFF.md` §4 ; **espace opérateur neuf à chaque essai** ; le moniteur doit surveiller aussi les erreurs de transport VT dans les journaux (`orch/watch5.sh`), la réconciliation faisant passer des lectures vides en `succeeded`.
- Worktree du chantier : `/home/nill/.paseo/worktrees/0y1qsxa1/m13-dossier-relevance` (branche `m13/dossier-relevance` @ `9f6a3b06`, fusionnée). Tests : `PYTHONPATH=src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest …` ; PostgreSQL de test port 55432 (`TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`).

## Autorisations (inchangées)
Déploiements nécessaires ; VT et Bridge autorisés et obligatoires ; arrêt de l'opérateur et signalement si VT/Bridge échouent de façon répétée (un échec que l'opérateur corrige seul au retry n'est pas une panne : ne pas arrêter avant la fin de l'appel de reprise) ; enquête vierge à chaque essai. PCAP, goodware (import en base), reset, fusion : décisions de l'utilisateur. Sources goodware indiquées : `/home/nill/tools/yarGen/dbs`.

## Prochaines actions permises
1. Goodware : construire l'index hors ligne (`scripts/import_goodware.py build-index`, sans base) ; l'import en base canonique n'a lieu qu'avec l'autorisation explicite de l'utilisateur.
2. Défauts plateforme ouverts (rapport §17), par ordre : garde de citation Bridge MD5 (avec **revue Luna xhigh + mutation** ; ne pas accepter MD5 sans association canonique), classement `pre_submission` du `draft_text` (état de transport monotone avant la requête), réconciliation VT sans donnée, dossiers de rédaction, checkpoint `skipped` persistant, export d'audit. Un enfant = un sujet, SHA de base, chemins exacts, oracle déterministe ; refaire soi-même tous les tests et comparer les échecs à la base (39 échecs préexistants sur cette machine).
3. Requalifier (essai 6) après correctifs, opérateur frais, même prompt de trois phrases.

## Pièges
`pkill -f` tue le shell appelant : lire le PID. Pas de `sleep` en avant-plan (Monitor/until). Les erreurs d'outil sont dans les `outbox/*.json` du relais (la table `tool_invocations` n'a pas de colonne d'erreur). Ne jamais lire `.env`, `var`, `.secrets`, `chatGPT_Answers`.
