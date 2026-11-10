# M20 — Environnement de test sain, rétro-conception APK (jadx), audit des capacités « Avis de l'analyste », puis merge + push

Statut : plan prescriptif pour l'orchestrateur M20 (Sonnet 5.5 High pilotant des sous-agents GPT-6 Luna xhigh).
Demande de l'utilisateur du 2026-11-10. État de départ : `main` local `0b345d27` (M17→M19 intégrés, NON poussé), stack déployée.
Contexte/outillage hérité : `.autowork-runs/m18/` et `.autowork-runs/m19/` (HANDOFF/REGISTRY/JOURNAL/tools), `docs/mission19/M19_ARTICLE_BREVE_FORMAT_PLAN.md`
(§3 « Méthode d'orchestration » : règles Luna, suite hôte, disque, déploiement, API). Lire `AGENTS.md`, `backend/AGENTS.md`, `.claude/CLAUDE.md`.

## Lots demandés

### E0 — Corriger « 30 échecs préexistants + 48 erreurs de collecte `pefile` »
Jusqu'ici la suite hôte (`/usr/bin/python3.12 -m pytest tests -q --ignore=tests/integration --continue-on-collection-errors …`) se termine avec
**30 échecs préexistants** (rule_workbench, yara_runner, investigation_advisor_tools, investigation_rule_tools, signature_bridge, rule_corpus_import,
advisor_checkpoint, archive_commands, logging, collection_api, selection_api, network_capture_import, supervised_first_run_preflight, …) et
**48 erreurs de collecte** (`ModuleNotFoundError: No module named 'pefile'` : test_analysis_worker, workflow_emulator/*, test_workers_tasks, test_static_analysis…).
Ce n'est pas acceptable comme « normal ». Tâche : **établir la cause racine de chacun** dans l'ordre du `.claude/CLAUDE.md` (mauvais interpréteur → dépendance
manquante → infra/outil absent → fixture → vrai défaut de code) puis les **corriger** :
- Environnement : `uv sync` est impossible hors réseau ; chercher une voie légitime (cache uv `~/.cache/uv`, wheels locales, venv Python 3.12 dédié
  construit depuis `uv.lock` / `pyproject` — `uv sync --locked --python 3.12` si le réseau/cache le permet ; sinon demander l'autorisation de l'utilisateur
  via AskUserQuestion pour une installation de dépendances déclarées). Ne jamais `pip install` ad hoc, ne jamais utiliser le Conda actif, ne pas modifier le code applicatif
  pour s'adapter à un mauvais Python. `pefile` est une dépendance déclarée du backend : l'environnement de test doit la fournir.
- Outils externes absents de l'hôte (yara, etc.) : distinguer test d'intégration légitimement dépendant d'un binaire (alors : marqueur/skip **explicite et justifié**
  seulement si la politique du projet le prévoit, jamais pour masquer un défaut) et vrai défaut ; documenter la recette d'environnement de test dans `backend/AGENTS.md`
  ou un document dédié (commande exacte, prérequis) et ajouter, si utile, une cible `make` (`make test` existe : l'utiliser en priorité).
- Objectif de sortie : `make test` ou la commande documentée **sans échec ni erreur de collecte** (ou, pour tout test réellement impossible hors Docker/Postgres,
  une exclusion documentée et justifiée), et `make lint` / `make typecheck` si disponibles.
- Les agents Luna n'ont pas les mêmes outils que l'hôte : l'orchestrateur lance lui-même la suite complète.

### E1 — Capacité de rétro-conception d'APK (jadx)
Ajouter une capacité **d'analyse statique d'APK par décompilation jadx** aux outils d'analyse locale (confinée) d'AutoWork, utilisable par les sessions
d'investigation (`operator_led`/`bridge_led`) comme les capacités existantes (FLOSS, MWCP, Ghidra, TShark, dotnet…). Existants à lire d'abord :
`docs/local_analysis_and_ghidra.md`, `docs/investigation_local_tools.md`, `docs/investigation_contracts.md`, `backend/src/cti_app/application/local_analysis.py`,
`backend/src/cti_app/infrastructure/local_command_runner.py`, `backend/src/cti_app/domain/local_analysis.py` (LocalCommandSpec / options), le conteneur `analysis-worker`
(Dockerfile/compose, confinement bwrap/seccomp, limites RLIMIT), le catalogue d'outils opérateur et le catalogue de compétences (`.claude/skills/autowork-*`,
dont `autowork-pe-native-analysis`, `autowork-dotnet-analysis`) ; `apktool` est déjà présent sur l'hôte (`/usr/bin/apktool`), `jadx` NON.
Exigences : outil(s) typé(s) et bornés (entrée = blob APK archivé par hash ; sorties bornées : liste de classes/packages, manifest et permissions, composants exportés, chaînes/URL/
domaines/clés extraits, sources décompilées d'une classe ou d'un chemin demandé par plage, recherche de motifs) ; exécution confinée sans réseau, limites mémoire/temps/octets,
**aucune exécution du sample**, résultats archivés comme observations canoniques (hash/provenance, G2/TLP respectés, jamais de promotion de sortie modèle en preuve) ;
documentation (`docs/investigation_local_tools.md` + doc du lot), tests hors-ligne avec un petit APK/fixture synthétique non malveillant (ne pas télécharger de malware), intégration dans le catalogue
d'outils/skills (une compétence `autowork-apk-analysis` ou extension de `autowork-tool-strategy`/`autowork-artifact-triage` pour le triage par format APK/DEX).
**Fourniture du binaire jadx** : vérifier d'abord toute source locale (cache, paquet déjà présent, artefact d'image Docker). Si l'installation requiert un téléchargement réseau,
**ne pas le faire sans autorisation explicite de l'utilisateur** : demander via AskUserQuestion (version + source officielle + empreinte attendue) ; l'ajouter à l'image `analysis-worker` de façon
reproductible (version et sha256 épinglés), puis redéploiement contrôlé (`make up` seulement quand aucune étape de production ne tourne). Pas d'acquisition CTI live, pas de contact C2.

### E2 — Audit et synthèse des capacités d'analyse pour la section « Avis de l'analyste »
Auditer (code + docs, lecture seule d'abord) **toutes les capacités d'analyse** qui alimentent un avis de l'analyste, de bout en bout : sessions d'investigation (`operator_led`/`bridge_led`,
boucle d'agent), catalogue d'outils (VirusTotal : lecture/pivots/téléchargement ; Shodan ; locaux : strings/FLOSS/MWCP/Ghidra/TShark/dotnet/PE/ELF/archives/YARA ;
règles YARA/Suricata ; protocole/PCAP ; Bridge/ChatGPT et conseillers), observations/findings/expériences, gouvernance G2/G3/TLP, rédaction de l'avis (`analyst_opinion_service`,
`PublicationDocument` opinion V6/V7, QA, rendu Typst, figures de protocole/diagrammes, bande de détection, prévisualisation brouillon). Livrer une **synthèse** :
`docs/mission20/ANALYST_CAPABILITIES_AUDIT.md` — matrice capacité × (entrée, sortie, bornes, état : opérationnel / partiel / absent / cassé, preuves : fichier:ligne + tests + docs),
limites connues, dettes, et ce qui est vérifié vs inféré. Docs de départ : `docs/analyst_investigation.md`, `docs/analyst_opinion_service.md`, `docs/investigation_*.md`,
`docs/local_analysis_and_ghidra.md`, `docs/advisor_preflight_catalog.md`, ADR concernés, skills `.claude/skills/autowork-*`.

### E3 — Comparaison avec les « avis de l'analyste » de nos références : qualité au moins équivalente ?
Identifier dans les références (`docs/references/`, gitignoré : lire sous `/home/nill/perso/work/AutoWork/docs/references/` ; notamment `livrable-IR2609/` (IR2609.pdf, `articles/`, `iocs/`, `typst-source/`),
les blocs `noteAnalyste` des bulletins CN/RU (`CN2609_typ`, `RU2609_typ`, PDF `20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-{CN,RU}-2609.pdf`) et tout autre document d'avis/rapport d'analyse
de référence) ce qu'est un **avis de l'analyste de référence** : structure, profondeur technique (rétro-ingénierie, extraction de configuration/clé, protocole C2, règles de détection, pivots d'infrastructure,
attribution nuancée, figures/tableaux/code), conventions typographiques (outils en gras, éléments techniques surlignés en gris, noms propres/anglais en italique), niveau de preuve.
Puis : **évaluer si AutoWork sait produire une analyse de qualité au moins équivalente** — rejouer, sur des données/fixtures existantes autorisées (pas de nouvelle acquisition live, pas de
requêtes fournisseur non autorisées), la production d'un avis (aperçu brouillon) sur un sujet déjà enquêté si disponible, comparer critère par critère (grille de comparaison explicite et mesurable :
profondeur technique, exhaustivité des artefacts, qualité des figures/protocole, détections YARA/Suricata testées, traçabilité, lisibilité, format Typst) et produire
`docs/mission20/ANALYST_QUALITY_GAP.md` : écarts classés par gravité, causes (capacité manquante, prompt, rendu, données), recommandations **chiffrées** et un backlog priorisé.
Corriger dans ce lot les écarts de format/rendu des avis qui sont bornés et sûrs (mêmes conventions typographiques que les articles/brèves M19 si absentes), sans toucher aux avis existants
ni lancer d'enquête live ; les gros chantiers (nouvelles capacités) restent en backlog documenté, sauf E1 (jadx) qui est demandé.

### E4 — Clôture : tout merger sur `main` et pousser
L'utilisateur autorise explicitement, **en fin de mission seulement**, le merge de tout le travail M20 sur `main` local puis le **`git push` de `main`** vers `origin`
(`git@github-m413w4r3:m413w4r3/AutoWork.git`). Préconditions obligatoires avant le push : suite de tests propre (E0) + ruff + lint/typecheck disponibles ; `git status` propre (ne pas ajouter les fichiers
non suivis `decouverte*.md` ni rien sous `.autowork-runs/`, `docs/references/`, `var/`, `.env`, `.secrets`) ; contrôle qu'aucun secret/identifiant n'est dans les commits à pousser (`git diff origin/main..HEAD`
parcouru par motifs de secrets, pas de dump de `.git`) ; vérifier la branche distante (`git fetch`, divergence ; **jamais de force-push**) ; si le remote a divergé ou si la CI/protection de branche bloque,
s'arrêter et demander à l'utilisateur. Aucun déploiement distant, aucune release/publication/repin. Résumer dans le rapport final le nombre de commits poussés (M17→M20) et le SHA distant.

## Contraintes non négociables
`AGENTS.md` : G2/G3, TLP, `external_llm_allowed`, pas de secrets dans prompts/logs, pas d'acquisition/téléchargement/exécution malveillante sans autorisation explicite, jamais de DELETE en base/MinIO,
Bridge : un seul appel modèle à la fois, migrations Alembic seulement selon ADR-0007 (ne jamais éditer `0001_baseline`). Les changements sont persistants et versionnés (code, contrats, docs, tests).

## Méthode
Reprendre intégralement §3 de `docs/mission19/M19_ARTICLE_BREVE_FORMAT_PLAN.md` (orchestrateur Sonnet High ; implémenteurs/relecteurs Luna xhigh `codex/gpt-6-luna` via Paseo, worktrees `m20/<lot>` ;
pas de repli Claude pour l'implémentation ; limite d'usage Luna → commit WIP, attendre, NOUVEL agent ; jamais re-prompter un agent terminé ; temporaires `--basetemp` nettoyés ; `df -h /` surveillé ;
suite complète lancée par l'orchestrateur sur l'hôte ; déploiement `make up` entre deux étapes de production). **Ne rends jamais la main en attendant un agent/une tâche : attends avec des boucles
`timeout 590 bash -c 'while …; do sleep 20; done'`.** Lots parallèles seulement sans chevauchement de fichiers : E0 (environnement/tests) d'abord (il conditionne la validation), puis E1 ‖ E2 (lecture seule) ,
puis E3, puis E4. Lot M20-0 de cadrage en lecture seule recommandé (cartographie E0/E1/E2/E3). Registre/journal/handoff dans `.autowork-runs/m20/`.

## Critères d'acceptation
- E0 : commande de test documentée → 0 échec, 0 erreur de collecte (ou exclusions justifiées par écrit) ; cause racine de chaque ancien échec dans le rapport.
- E1 : outils jadx bornés/confinés testés hors-ligne ; image `analysis-worker` reproductible (version + sha256) ; docs/catalogue/skills à jour ; aucun téléchargement non autorisé.
- E2 : `ANALYST_CAPABILITIES_AUDIT.md` complet (matrice avec preuves).
- E3 : `ANALYST_QUALITY_GAP.md` avec grille, écarts, backlog chiffré ; correctifs bornés livrés.
- E4 : `main` poussé sans force, SHA distant = SHA local, rapport final (commits/tests réels, hashes, ce qui n'a pas été vérifié, décisions en attente).
