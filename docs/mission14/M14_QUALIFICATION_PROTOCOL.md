# M14 — protocole de qualification d'un opérateur neuf (PRÊT, NON EXÉCUTÉ)

Cette qualification est de la **recherche CTI live** (VirusTotal, Bridge) : `AGENTS.md` exige une autorisation explicite distincte. Elle n'a **pas** été lancée dans M14. Ce document fixe ce qu'il faut avoir avant de la lancer, comment la mener et comment la juger.

## 1. Préalables (tous requis)

| # | Condition | État M14 |
| --- | --- | --- |
| 1 | Autorisation utilisateur écrite : recherches VT et consultations Bridge, déploiement du code M14 | **manquante** |
| 2 | Article de départ et hashes initiaux de l'enquête neuve (non fournis ; ne pas réutiliser M12/M13) | **manquants** |
| 3 | Code M14 intégré et revu (voir rapport final), déployé à l'identique sur backend, worker, job-recovery et relais ; `RULE_WORKBENCH_ENABLED=true` vérifié dans backend et worker | non déployé |
| 4 | Aucune invocation externe en vol (`tool_invocations` / `model_runs` non terminaux = 0), aucun opérateur ni relais actif | à vérifier juste avant |
| 5 | Archive historique faite et vérifiée (`docs/mission14/M14_RESET_PLAN.md` §2) ; stack neuve (option A) **ou** décision explicite de réutiliser la stack actuelle (enquête neuve, pool d'échantillons hérité du sujet à écarter) | archive faite ; stack non créée |
| 6 | VT joignable (lecture d'un hash public) et Bridge sain (`/health`, extension connectée) | à vérifier |
| 7 | Profil DeepSeek réellement configuré dans Paseo : « Orchestrator DeepSeek » = `deepseek-claude/deepseek-flash[1m]`, thinking `max` (à confirmer que c'est le « DeepSeek Max » voulu) | disponible |
| 8 | Jeu de binaires bénins (optionnel mais nécessaire pour une mesure de faux positifs) | **non fourni** : la limite goodware sera déclarée |

## 2. Déroulé

1. Créer sujet + enquête neufs et l'enveloppe G2 (approbation humaine). Noter le commit déployé (`git rev-parse HEAD` du checkout qui sert la stack) et l'ID d'enquête.
2. `python scripts/ops/stage_operator_cti_skills.py <workspace-neuf>` : workspace opérateur neuf, uniquement les skills génériques (aucune preuve, aucun run antérieur). Vérifier le manifeste de hashes.
3. Lancer l'opérateur via Paseo (profil DeepSeek Max) avec un mandat métier naturel de trois phrases (article + hashes, produire un avis CTI, règles YARA/Suricata, pivots VT) **sans** conclusions M12/M13, sans plan, sans résultat attendu, sans mention de défauts connus.
4. Moniteur passif (aucune intervention corrective de l'orchestrateur) : `GET /operator-status` (`advisor_guidance`), journaux VT/Bridge. Arrêt de l'opérateur **uniquement** si VT ou Bridge échouent de façon répétée sans que l'opérateur s'adapte ; signaler la cause.
5. Fin de run : générer l'avis brouillon, l'export d'annexes (`backend/scripts/export_investigation_annex.py`, vue publique puis archive restreinte explicite), l'aperçu PDF (ne déclenche ni approbation ni publication), l'export d'audit (`write_audit_bundle`, lecteur d'artefacts, option `include_operator_text` seulement si nécessaire).
6. Audit indépendant (session différente de l'opérateur et des développeurs ; Luna xhigh lecture seule) avec le skill `autowork-run-audit`.

## 3. Critères de qualification (M14 §7.3)

Cadrage initial avec Bridge · exploitation effective des conseils (disposition retrouvable par conseil) · analyse technique des samples · faits ≠ hypothèses · première YARA candidate précoce · compilation et tests réels · pivots VT pertinents · qualification des nouveaux candidats (acquis vs non acquis) · collaboration Bridge lors des révisions · **rédaction de chaque section analytique via `draft_text`/`review_text`** (couverture Bridge visible dans la pré-publication) · fichiers YARA et Suricata produits · annexes · PDF · journal d'audit reconstruisant les décisions.

Absence de variante nouvelle, de PCAP ou de goodware = **limite réelle à déclarer**, pas à compenser.

## 4. Classement des défauts (audit)

Plateforme · comportement opérateur · qualité des données · validation insuffisante · limite réelle de l'enquête. Un défaut plateforme ou de skill reproductible se corrige dans le code/skills/prompts (nouveau lot, session neuve), jamais par un conseil oral à l'opérateur d'essai.

## 5. Mesures à relever (état canonique, pas le récit de l'opérateur)

Temps de préparation et taille des dossiers Bridge par consultation (observations d'invocation : octets, lectures de blobs, SHA du dossier et du prompt), latence Bridge de bout en bout, nombre de consultations par type et leurs statuts, refus locaux (codes), incidents de transport/réconciliation, échantillons acquis (nouveaux vs détenus), règles YARA/Suricata (versions, tests), sections rédigées avec Bridge / total.
