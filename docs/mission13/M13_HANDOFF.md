# M13 — handoff vers un nouvel orchestrateur (Sonnet 5.5)

Lis d'abord `AGENTS.md`, `CLAUDE.md`, puis `docs/mission13/M13_REPORT.md` (sections 1 à 13). Ce fichier donne l'état exact et la suite. **Ne répète pas** ce que le rapport établit déjà.

## 1. Mandat restant

Objectif de la mission M13 : un AutoWork durablement meilleur, pas une enquête réussie une fois. Il reste à **requalifier** avec un nouvel opérateur DeepSeek sur une enquête vierge, après les correctifs du cycle 2, puis auditer, corriger, et livrer le rapport final.

Ne lance pas `make clean`. Le reset est une opération séparée : `scripts/ops/safe_local_reset.py` refuse dès qu'une enveloppe G2 ou une invocation existe, et `--confirm-initial-reset` ne lève pas cette garde. Prépare seulement le plan (voir rapport §8).

## 2. État du code

- Branche `bridge-simp/integration` (PR #23), fusionnée dans `main` à la demande de l'utilisateur : voir le SHA de fusion indiqué en fin de fichier.
- **Déployé en ce moment** sur les conteneurs : `619d0f10` (correctif Bridge/rapports VT). **Non déployés** : brouillon `draft_text` utilisable (`82e059fe`), import de plusieurs règles YARA (`cec56215`), déblocage du checkpoint de décompilation + export d'audit + indice d'erreur (`8b63991e`), skills (`391bfa49`, fichiers stagés, pas de déploiement).
- Le checkout principal `/home/nill/perso/work/AutoWork` est en HEAD détaché sur `619d0f10` (c'était le déploiement). Pour redéployer : se placer sur le commit de `main` voulu, `export RULE_WORKBENCH_ENABLED=true` (le `.env` de l'utilisateur force `false`, ne le lis pas), utiliser `docs/mission07/tools/safe_deploy.sh` avec `RELAY_WORKDIR` pointant sur un répertoire de relais en cours ; **sans relais actif** (aucun ne tourne), vérifie qu'aucun opérateur ni appel n'est en vol puis lance `make up`. Vérifie ensuite : variable `RULE_WORKBENCH_ENABLED=true` dans les conteneurs `backend` et `worker`, présence du nouveau code (`grep` dans `/app/src`), réseau `metaharness-models`, `GET http://chatgpt-bridge:8001/health` depuis le backend.
- Aucune migration dans cette branche.

## 3. Autorisations données par l'utilisateur (à respecter, pas à étendre)

- « J'autorise les déploiements nécessaires. »
- Les recherches VT et les consultations Bridge sont **autorisées et obligatoires**.
- « Si les requêtes VT échouent, arrête l'opérateur et signale-le-moi, je réactiverai le proxy. Même chose pour Bridge. » Un incident isolé (un seul outil, lectures voisines réussies) n'est pas une panne ; une panne répétée sur les lectures courantes l'est.
- « DeepSeek Max » = fournisseur Paseo `deepseek-claude`, modèle `deepseek-flash[1m]`, `thinkingOptionId=max`, `modeId=auto`.
- **Partir d'une analyse vierge** : nouveau run + nouvelle enquête + nouvel espace opérateur sans trace des précédents.
- PCAP : l'utilisateur a demandé de passer directement par l'API VT + clé si une route le permet. Constat (rapport §11) : l'API directe sert le PCAP (302 vers un CDN signé), mais l'application ne peut pas le suivre ni l'associer à l'enquête sans changement de code ; **ne contourne pas** par écriture en base. Le correctif de routage reste une décision à soumettre.
- Fusion de la PR #23 : demandée explicitement par l'utilisateur (fait). Ne fusionne rien d'autre sans demande.

## 4. Environnement d'essai (hors dépôt, ignoré par git)

Répertoire `/home/nill/perso/work/AutoWork/.autowork-runs/m13-q1/` :
- `orch/` (orchestrateur seulement, **jamais visible de l'opérateur**) : `hapi.py` (appels HTTP avec le jeton humain lu dans le conteneur, jamais affiché ; usage `python3 hapi.py POST <chemin> '<json>'`), `REGISTRY.md` (journal), `watch.sh` (moniteur ; édite la variable `INV` avant usage), `initial_corpus_hashes.txt` (15 hashes du corpus de l'article), `subject_sample_pool_before_attempt2.txt` (240 échantillons déjà acquis par des enquêtes antérieures du sujet).
- `operator/` (essai 1, abandonné), `operator2/` (essai 2, terminé : `FINAL_REPORT.md`, `operator_log.md`, relais arrêté), `audit_pack/` (lot d'audit de l'essai 2).
- Sujet unique : `d22822d4-27ce-47d2-9cc5-f84cf312986a`, synthèse SHA `8eb9cbe0…`. Un nouveau run : `POST /api/production/subjects/<sujet>/production/restart-with-new-sources` (`{}`), attendre `ready` (quelques dizaines de secondes, pas de coût modèle), puis `POST /api/subjects/<sujet>/investigations` avec `production_run_id`, `synthesis_artifact_id` (artefact `synthesis` du nouveau run), `synthesis_sha256`, `execution_mode=operator_led`. Enveloppe héritée de la politique OSINT : budgets neufs.
- Espace opérateur : copier `relay.py` (celui du dépôt), `opclient.py` (changer le label), `ENVIRONMENT.md`, `MANDATE.md` d'`operator2/`, `PLAYBOOK.md` extrait du commit déployé (`git show <sha>:docs/operations/operator-playbook-medium.md`), stager les skills depuis le dépôt à ce même commit avec `python3 scripts/ops/stage_operator_cti_skills.py --workspace <dir> --claude`, lancer le relais (`RELAY_INVESTIGATION_ID=<inv> nohup python3 relay.py --workdir "$PWD"`), créer le workspace Paseo puis l'agent. Le prompt initial d'`operator2` est de trois phrases (lire ENVIRONMENT, PLAYBOOK, MANDATE, index des skills ; enquêter ; ne pas terminer pendant un appel en vol).
- Ne donne à l'opérateur ni plan, ni résultats d'essais précédents, ni consigne corrective.

## 5. Résultats de référence (essai 2, avant correctifs)

Voir rapport §10. À comparer : première consultation Bridge, présence d'un `pivot_strategy`, usage de `vt.intelligence_search`, nombre de candidats réellement nouveaux, dispositions de recommandations tracées, checkpoints bloqués, qualité de l'avis (audit : C).

## 6. Oracle de l'essai 3 (comportements observables, pas de réponses imposées)

1. Une consultation de cadrage tôt, suivie d'actions (déjà acquis en essai 2).
2. Les raisons de checkpoint évoluent (plus de `first_significant_decompilation` figé).
3. Un brouillon `draft_text` de Bridge est **référencé** par l'avis avec sa provenance (texte exact + SHA), sans retranscription ; vérifier les blocs `bridge_draft_ref`.
4. Une réponse Bridge à plusieurs règles YARA est importée en candidates distinctes via `rule.propose_bridge`.
5. Les valeurs dérivées citées en `supported` ont une observation de sortie dans l'enquête.
6. Au moins un pivot VT par motif (`vt.intelligence_search`) ou une indisponibilité documentée ; les candidats sont qualifiés (corpus / connu de la plateforme / nouveau).
7. L'export d'audit (`include_operator_text=True`, avec lecteur d'artefacts configuré) permet de reconstruire outil, objectif et critère de chaque appel.
Ne juge pas le succès au nombre d'appels Bridge.

## 7. Export d'audit correct

Mon premier export a été généré **sans** lecteur d'artefacts, d'où des consultations `UNKNOWN`. Dans le conteneur backend : construire `MinioBlobStore` + `BlobCatalogService(blob_store, uow_factory)` + `ProductionArtifactStore` comme dans `backend/src/cti_app/workers/tasks.py` (~l.205-240), puis `read_investigation_audit_snapshot(session_factory, UUID(inv), artifact_store)` et `write_audit_bundle(snapshot, dir, inv, include_operator_text=True)`. Ajoute au lot d'audit : findings, règles, avis, observations citées (lecture MinIO depuis le conteneur), `FINAL_REPORT.md`. L'audit se fait avec une session Luna xhigh en lecture seule (profil « Luna Audit »), grille plateforme / comportement / limite réelle.

## 8. Règles et pièges rencontrés

- Ne lis, ne grep et n'affiche jamais `.env`, `var`, `.secrets`, `chatGPT_Answers` (le classifieur le refuse). La clé VT et les jetons se lisent dans le conteneur backend, jamais à l'écran.
- `pkill -f <motif>` se tue elle-même quand le motif figure dans sa ligne de commande : lis le PID dans `relay-status.json` et envoie le signal.
- zsh ne découpe pas une variable en mots : utilise des fonctions shell.
- Un enfant Paseo terminé ne se ré-interroge pas : nouvelle session par correction ou revue. Les sandboxes Luna ne lancent ni PostgreSQL ni Docker : refais toujours les tests toi-même.
- Une revue indépendante a déjà fait échouer une première version d'un correctif de sécurité : réserve toute modification de garde G2/TLP à une revue Luna xhigh avec tests de mutation.
- Les worktrees de Paseo n'ont pas les polices Typst ni le corpus Run12 ignorés par git (copie/lien depuis le checkout principal, sinon échecs ou skips d'environnement).
- Un changement qui touche la divulgation externe se teste sur les **vraies valeurs** émises par les recorders (`source="VirusTotal"`, `object_type="file"`), pas sur des fixtures inventées.

## 9. Tests de référence (échecs préexistants sur `main`, à ne pas imputer)

Unitaire : 36 échecs (rendus Typst réels et `test_advisor_prompt_golden`). FAST gate : W15 et W16. PostgreSQL : `test_analyst_opinion_publication_postgres`. Typage : 11 erreurs. CI GitHub : rouge sur `main` (mypy, FAST gate, J2, frontend e2e).

## 10. Prochaines actions permises

1. Vérifier l'état de `main` et redéployer (voir §2).
2. Lancer l'essai 3 (§4), surveiller (`watch.sh`), arrêter et signaler selon §3.
3. Auditer (§7), classer, corriger ce qui est corrigeable, requalifier une dernière fois si un correctif de comportement le justifie.
4. Soumettre à l'utilisateur : décision sur la route PCAP, plan de reset, verdict de déploiement en production.
5. Mettre à jour `docs/mission13/M13_REPORT.md`, committer, ne rien pousser sur `main` sans demande (la PR #23 est déjà fusionnée ; ouvre une nouvelle PR pour la suite).
