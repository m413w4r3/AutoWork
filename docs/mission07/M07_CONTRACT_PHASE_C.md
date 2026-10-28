# Mission 07 — contrat utilisateur phase C (copie condensée fidèle, 2026-10-27/28)

Objectif prioritaire : nouvelle investigation CTI réelle `a8845772…` depuis la synthèse vérifiée ; recherches/téléchargements VT réels, analyses locales, consultations Bridge utiles, journal d'audit, avis analytique complet et sourcé, PDF final conforme aux références, audit indépendant, comparaison avec l'investigation précédente. Ne pas s'arrêter au premier test vert ; terminer après le livrable final évalué, sauf blocage réel documenté. Pas de retour à une préparation interminable.

## Rôles
- ORCHESTRATEUR = Claude Sonnet 5.5 medium : organise, vérifie auth/config, supervise reprises, délègue corrections, arbitre bloquant/non bloquant, maintient journal et handoffs, supervise comparaison finale. Ne mène PAS l'enquête, ne remplace pas l'opérateur, ne décide pas du fond analytique ; contexte compact.
- Tous les autres = `codex/gpt-6-luna` xhigh, mode auto-review, session NEUVE par tâche : CTI-ANALYST (opérateur, conduit réellement l'enquête, utilise VT/Bridge/outils locaux via l'API opérateur, Bridge = conseiller, justifie accept/adapt/rejet), TECH-FIX, CODE-REVIEW (indépendant), CTI-RESEARCH, EDITOR, TYPST-PDF, AUDIT-OBSERVER (audit technique et analytique indépendant, append-only, ne modifie pas silencieusement l'enquête), FINAL-REVIEW, COMPARISON. Instancier à la demande. Enregistrer les modèles réellement utilisés.

## Reprise / indépendance
Investigation historique `5f66ebfb` (sujet d22822d4) : conservée intégralement, référence de la comparaison finale, ne jamais la supprimer/modifier. Nouvelle : run 27478dcc, inv `a8845772`, synthèse 4d37c738 v1, OSINT_OPEN dc034634 v1, `operator_led`. Ne pas créer de troisième run/investigation. L'opérateur construit son raisonnement depuis la synthèse du nouveau run, sans importer l'ancienne analyse ; la comparaison se fait APRÈS gel du nouveau livrable par un agent distinct.

## Politique (friction minimale, OSINT_OPEN déjà active)
Autorisés sans approbation par opération : pivots/recherches VT, téléchargements VT, consultations Bridge, transmission à Bridge des contenus CTI partageables, analyses statiques locales/commandes cataloguées, analyse d'un `candidate` sans G3. `do_not_submit` ancien n'empêche pas une recherche VT passive. Recherche VT ≠ upload. Secrets opérationnels/credentials/droits tiers explicites restent distincts. Conserver S3, références, digests, coûts, états, reprise. Aucune exécution de malware, aucun contact C2, aucun shell arbitraire. Ne pas reconstruire G2/G3/TLP/S3. Décisions humaines (qualification, réconciliation, publication…) restent à l'identité humaine : ne jamais attribuer une décision humaine à un agent ; ne jamais exposer/écrire les jetons. Demander à l'utilisateur seulement pour un vrai blocage hors cadre, opération destructive ou publication externe.

## Conduite de l'analyse
Boucle hypothèse → pivot → critère → outil → résultat → interprétation → suite. Bridge = conseiller actif (revue d'hypothèses, contradictions, interprétation de résultats statiques, pivots, solidité des findings, attribution/confiance, revue critique avant rédaction) avec références exactes ; enregistrer ce qui est omis et pourquoi. Outil choisi pour une question analytique ; ne jamais déclarer un succès sur unavailable/partial/erreur ; ne pas attribuer un résultat non observé.

## Audit des décisions (pendant le run)
Journal durable append-only (agent, date, investigation/run/job/invocation/version/corrélation, hypothèse, fait motivant, justification, outil+paramètres, résultat/statut/SHA, dossier Bridge, conseil et décision, coûts, interprétation/confiance/limites, décision suivante). Réutiliser événements/références existants, pas de nouveau framework.

## Références qualité
`/home/nill/perso/work/AutoWork/docs/references/CN2609_typ` (qualité documentaire/qualitative : structure, profondeur, preuves, tableaux/IOC/figures, incertitudes, rédaction) et `/home/nill/perso/work/AutoWork/docs/references/chpTypst` (format/composition Typst : charte, titres, marges, tableaux, figures, en-têtes/pieds, citations, annexes). Vérifier l'existence et le contenu RÉELS (fichiers non suivis possibles) ; ne pas inventer ni substituer silencieusement un template ; si le moteur utilise aussi `/home/nill/perso/work/AutoWork/chpTypst`, établir la relation avant adaptation. Ne pas recopier le fond analytique des références.

## Livrable
EDITOR (récit cohérent, faits/inférences/hypothèses séparés, redondances éliminées, refs vérifiées, contradictions/limites conservées, jamais de hausse artificielle de confiance) → TYPST-PDF (compilation, fontes, pagination, débordements, tableaux, figures, IOC/SHA exacts, inspection page par page Poppler ; PDF compilé ≠ validé) → FINAL-REVIEW indépendant (conclusions, preuves, contre-hypothèses, exactitude technique, rédaction, rendu vs références). Production interne ≠ diffusion publique.

## Comparaison (après gel)
Agent COMPARISON indépendant : profondeur, qualité des preuves, pertinence des pivots, analyse locale, apport de Bridge, incertitudes, traçabilité, livrable, friction (interruptions/refus inutiles). Distinguer améliorations dues au code et dues au raisonnement ; ne pas présupposer que la nouvelle est meilleure.

## Incidents
Déterminer si bloquant ; enregistrer ; non bloquant ⇒ poursuivre ; bloquant ⇒ correctif minimal délégué à Luna + test ciblé + revue indépendante + reprise au dernier état canonique. Ne pas rejouer une émission VT/Bridge ambiguë ; ne pas refaire des recherches réutilisables ; pas de grandes suites pour correction éditoriale. D7/D8 production et avertissement « nom de fichier trop long » : enregistrés, non réparés préventivement.

## Reprise entre sessions
Handoff compact à chaque checkpoint (SHA, déployé, inv/run/synthèse, état/version/job, actions, appels réels, émissions ambiguës, hypothèses ouvertes, dernière décision, prochaine action, défauts, livrables). Nouvel opérateur depuis données canoniques + handoff, sans répéter les émissions externes.

## Verdicts finaux (avec preuves : ids, versions, SHA, fichiers)
OPERATOR_AUTHENTICATION PASS|BLOCKED ; OSINT_OPEN ACTIVE|BLOCKED ; NEW_INVESTIGATION RUNNING|COMPLETED|BLOCKED ; VT_REAL_PIVOTS PASS|PARTIAL|BLOCKED ; VT_DOWNLOADS PASS|REUSED_ONLY|PARTIAL|BLOCKED ; LOCAL_STATIC_ANALYSIS PASS|PARTIAL|BLOCKED ; BRIDGE_CONSULTATIONS PASS|PARTIAL|BLOCKED ; DECISION_AUDIT PASS|PARTIAL|BLOCKED ; ANALYTICAL_FINDINGS COMPLETE|PARTIAL|BLOCKED ; FINAL_CTI_OPINION PRODUCED|BLOCKED ; TYPST_REFERENCE_COMPLIANCE PASS|PARTIAL|BLOCKED ; FINAL_PDF_QA PASS|PARTIAL|BLOCKED ; INDEPENDENT_REVIEW APPROVED|REVISE|BLOCKED ; HISTORICAL_COMPARISON COMPLETED|BLOCKED ; REAL_ANALYST_RERUN COMPLETED|IN_PROGRESS|BLOCKED. Ne pas confondre test hors ligne/appel réel, fichier présent/analysé, conseil Bridge/preuve indépendante, finding rédigé/démontré, PDF généré/validé, nouvelle investigation/réutilisation de l'ancienne.
