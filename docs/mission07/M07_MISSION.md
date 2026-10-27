# Mission 07 — mandat (copie condensée fidèle de la consigne utilisateur du 2026-10-27)

Objectif : passer de la préparation technique à une **véritable investigation
CTI supervisée** sur Blinder Tunnel jusqu'à un livrable final évalué. Ne pas
poursuivre indéfiniment le développement. Évaluer : pertinence des décisions
de l'analyste ; qualité/timing/preuves du guidage Bridge ; recherches et
téléchargements VT réels ; outils locaux, commandes cataloguées, confinement ;
complétude de l'audit rétrospectif ; qualité de l'avis et du PDF.

## Rôles (Paseo, `codex/gpt-6-luna` xhigh pour décision/code/enquête/revue ; identité effective enregistrée ; session neuve, jamais réutiliser un agent arrêté)
ORCHESTRATOR, PRE-RUN-FIX, PRE-RUN-REVIEW, CTI-ANALYST, BRIDGE-EVALUATOR,
AUDIT-OBSERVER (lecture seule, journal indépendant), FINAL-QA. Bridge =
conseiller, pas substitut de l'opérateur ; les sous-agents n'usurpent pas
l'identité de l'opérateur. Sessions implémentation/enquête/audit séparées.

## Politique OSINT_OPEN sans friction (après activation par opérateur vérifié)
- Recherches passives VT, téléchargements VT admissibles (SHA-256,
  quarantaine, provenance), consultations Bridge, transmission à Bridge des
  observations/extraits/configs/octets partageables, outils locaux catalogués,
  analyse d'un `candidate` sans G3 : **sans approbation par requête**.
- Crédits VT disponibles : pas de petites limites arbitraires ; plafonds
  techniques configurables + coupe-circuits anti-boucle. Ajuster les plafonds
  AVANT le run.
- Distinguer VT READ / VT DOWNLOAD / EXTERNAL UPLOAD ; `do_not_submit`
  historique ne bloque pas READ (fait : A2, `main@b4d5db8d`). Ne pas modifier
  en masse les 225 échantillons.
- Partage Bridge des données partageables sans filtre arbitraire de
  contenu/taille ; budget de contexte du modèle conservé, omissions
  enregistrées. Jamais les clés API.
- Conservé : credentials hors prompts/traces/livrables ; identités
  d'opération, budgets, versions, digests, claims S3, reprises ; TLP/droits
  restrictifs explicites ; confinement ; aucun shell arbitraire piloté par
  Bridge ; aucune exécution d'échantillon, aucun contact C2 ; pas d'upload
  tiers comme effet secondaire ; aucune qualification `confirmed` artificielle.

## Phase A (courte, bornée, pas de refactoring)
A2 VT READ `do_not_submit` (FAIT). A3 E2E continu
`test_real_postgres_osint_open_continuous_vt_analysis_advisor` :
OSINT_OPEN → pivots VT → DOWNLOAD → quarantaine → ANALYZE → BRIDGE →
observations/findings → audit → replay, assertions sur états persistés,
réutiliser W15, pas de second moteur. A4 : tests ciblés, PG jetable,
concurrence/replay, lint/typecheck, workflow FAST/FULL, suites pertinentes,
J2 si parcours touché ; revue Luna xhigh indépendante de chaque correction ;
n'intégrer que l'approuvé.
Gate : VT_PASSIVE_OSINT_READ, CANDIDATE_ANALYSIS, CONTINUOUS_E2E,
S3_REGRESSION, NO_REPEAT_APPROVALS, CI_CURRENT. Ne pas fabriquer de PASS ; si
rouge, blocage exact + décider s'il compromet le run.

## Phase B (opérationnelle)
B1 préflight sur stack réelle (SHA API/workers/UI, Alembic, jobs, opérations
ambiguës, lignée Run12, OSINT_OPEN, Redis/MinIO/VT/Bridge, analysis-worker,
Ghidra, confinement, intégrité SHA). Sauvegarde, préflight migration
`0003→0005`, rollback applicatif, procédure d'arrêt. **Migration seulement
après validation explicite de l'opérateur, sur la base ciblée ; ne jamais
déclarer la base live jetable.**
B2 réconcilier inv `5f66ebfb` (`running` sans job) et 19 `needs_review` si
elles peuvent interférer / double émission ; conserver historique et
réservations ; actions CAS motivées ; pas de remise en ordre exhaustive
artificielle.
B3 smokes après autorisation : 1 recherche VT réelle partageable ; 1
consultation Bridge réelle (dossier limité, sans secret) ; 1 téléchargement
réel si échantillon admissible absent de la quarantaine (sinon dire « validé
hors ligne seulement ») ; 1 test local inerte (octets, commandes, confinement).
Observer requêtes physiques, claims S3, réponses, archives, coûts, erreurs.
**Pas de `make smoke-live` aveugle (il inclut Shodan).**
Gate : DEPLOYED_SHA_MATCH, LIVE_SCHEMA_0005, LIVE_VT_READ_SMOKE,
LIVE_BRIDGE_SMOKE, ANALYSIS_WORKER_READY, OSINT_OPEN_ACTIVE_FOR_SUBJECT,
NEW_RUN_LINEAGE_READY → enchaîner directement sur l'enquête si autorisation.

## Phase C — nouveau run Blinder Tunnel
Sujet `d22822d4-27ce-47d2-9cc5-f84cf312986a` ; ancienne inv
`5f66ebfb-1653-45e6-83cd-a29af6f87868`. Ne rien supprimer/réinitialiser.
Chemin applicatif normal : nouveau `production_run`, réutilisation
content-addressée légitime, synthèse `VERIFIED` propre, vérifier
`(subject, run, synthesis_artifact_id, version, canonical_sha256)`, activer
OSINT_OPEN via opérateur authentifié, nouvelle investigation (IDs/journal
distincts). Ne pas recopier la synthèse historique ; ne pas forcer une
recherche si réutilisation canonique vérifiée.

## Conduite de l'analyse
Agenda d'hypothèses explicite ; IOC documentés puis pivots VT ; relations et
comportements VT ; télécharger le pertinent non présent ; réutiliser local
avec SHA ; analyses statiques guidées par une question ; Bridge quand
décisionnellement utile avec dossier complet ; accepter/modifier/rejeter ses
recommandations avec raison ; distinguer observation/hypothèse/interprétation/
conclusion ; garder résultats négatifs/partiels/limites ; avis cohérent,
détections justifiées, document final. Pas d'outil « parce qu'il existe ».

## Journal d'audit obligatoire (prioritaire), append-only, refs canoniques
Par décision/pivot : horodatage, agent/opérateur, IDs (investigation, run,
version, job, invocation, correlation), hypothèse, preuve motivante, objectif,
paramètres/contraintes, dossier Bridge, réponse et décision, outil et
résultat, coût/réservations, refs brutes + hashes, interprétation,
incertitudes, décision suivante + raison. Pas de credentials ni d'octets
sensibles. Réutiliser événements/tables existants ; motif non modélisé ⇒
journal supervisé avec IDs, pas de nouveau framework. Audit technique +
audit analytique ; l'observateur reconstruit la chronologie sans la
conversation privée de l'analyste.

## Autorisation (§8)
Accord de principe utilisateur pour OSINT ouvert sans approbations
répétitives, MAIS ce n'est pas une autorisation de modifier la base live,
redéployer, fournir des credentials ou émettre depuis un environnement non
vérifié. Phase A autonome (jetable) ; phase B préflight lecture seule
autonome, présenter les mutations live exactes ; après validation
déploiement/migration → activer OSINT_OPEN ; après approbation du départ →
opérations ordinaires sans approbation par requête. Nouvelle décision
seulement hors cadre (nouvelles catégories d'effets, destinations
interdites, droits contradictoires, destructif, émission irréconciliable).
Jamais contourner l'authentification. Présenter une demande précise et
bornée + handoff reprenable sans réémission ; ne jamais présenter une
autorisation manquante comme acquise.

## Livrables (§9)
Avis CTI final (confiance, alternatives, limites, discriminants) ; PDF réel
Typst/Poppler vérifié ; chronologie ; bilan Bridge ; bilan outils ; rapport
d'anomalies ; audit indépendant contradictoire. Pas d'attribution de famille
certaine sans preuve. Publication/diffusion externe = action distincte.

## Discipline (§10)
Pas de module générique, duplication S3/gouvernance, migration pour ce que les
événements existants portent, refactoring spéculatif, fournisseurs hors
périmètre. Non bloquant ⇒ consigné ; bloquant ⇒ correction minimale + test +
revue + preuve de reprise. Pas de boucles de revue interminables. Handoff
compact. Si contexte > 70 % : lancer un nouvel orchestrateur et s'arrêter.

## Verdicts finaux (§11)
PRE_RUN_FIXES, E2E_OSINT_CONTINUOUS, LIVE_SCHEMA, LIVE_PROVIDER_SMOKES,
ANALYSIS_WORKER, NEW_PRODUCTION_RUN, NEW_VERIFIED_SYNTHESIS,
OSINT_OPEN_AUTHORIZATION, NEW_INVESTIGATION, VT_REAL_RESEARCH,
VT_REAL_ACQUISITION, LOCAL_ANALYSIS, BRIDGE_GUIDANCE, AUDIT_DECISION_CHAIN,
FINAL_OPINION, FINAL_PDF_QA, INDEPENDENT_FINAL_AUDIT, REAL_RUN_COMPLETED.
Un PASS = preuve actuelle identifiable. CI verte ≠ fournisseur live ;
acquisition réutilisée ≠ nouveau téléchargement ; PDF généré ≠ analyse
pertinente. Ne pas s'arrêter après une série de tests verts.
