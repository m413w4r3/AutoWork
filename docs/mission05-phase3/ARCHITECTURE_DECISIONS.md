<!-- Integrated verbatim from .prompts/AUDIT_ARCHITECTURE_AUTOWORK_PHASE3_DECISIONS_2026-10-25.md (owner decision, 2026-10-25). Addendum implemented/not-implemented appended after integration. -->
# AutoWork — audit architectural décisionnel pour Mission 05 / Phase 3

**Date :** 2026-10-25  
**Référentiel :** `m413w4r3/AutoWork`, `main@55fba67d5882c1ae6c4abff4e74c53a24e57649f`.  
**Méthode :** revue statique ciblée du code et de ses tests via GitHub, lecture des ADR/rapports de Mission 03–04 et des skills `codebase-design` / `improve-codebase-architecture`. Les lignes citées correspondent à ce SHA. **Je n'ai exécuté ni tests locaux, ni PostgreSQL, ni typst, ni appel réseau CTI.** La CI distante sur `main` (#40188808858) est `success`, mais elle ne démontre pas à elle seule la qualité de l'architecture.

## Décision : TARGET_A — approfondir le module de préparation Advisor

### Évidence de l'architecture actuelle

1. `backend/src/cti_app/application/advisor_consultation.py:120-158` expose `PreparedConsultation`, `PreparationRefusal` et une entrée `prepare(projection: Mapping[str, Any])`. La préparation effectue déjà une partie substantielle du travail : secrets, dossier, vérification de complétude, référence canonique, octets UTF-8 et SHA-256.
2. Dans le même fichier, **lignes 197–202**, cette fonction importe à l'intérieur de son implémentation `InvestigationAdvisorTool`, puis appelle **`InvestigationAdvisorTool._prompt`**, méthode privée définie dans `investigation_advisor_tools.py:3582` : une implémentation de préparation prétendument autonome dépend de l'outil historique à fortes dépendances d'infrastructure.
3. `investigation_advisor_tools.py:136-174` initialise le gros outil orchestration avec transport, settings, investigations, observations, findings, états, opinions, samples, blobs, snapshots, store, registry (et autres). Le même fichier porte l'exécution réseau (`execute` autour de 304), le renderer de prompt, `_fit_advisor_prompt` (~4587), le reparse (~4147) et le parsing historique (~5607, ~5942).
4. La voie effective produit et archive son prompt séparément : `_fit_advisor_prompt` est appelé vers la **ligne 726**, puis encodage, réservation, archivage et transport dans l'outil. La nouvelle voie `prepare()` n'est pas la source autoritative de cet envoi. Le contrat de réservation doit conserver le **coût mesuré sur les octets réellement expédiés**.
5. `docs/mission04-closure/L22_ADOPTION_PLAN.md` indique explicitement **`PREP_ONLY`**, soumission v3 autoritative, v4 parse/reparse hors ligne et `ADVISOR_CUTOVER_BLOCKED_BY_S3`.

### Décision irréversible de périmètre (hors problème factuel prouvé)

**Refactoriser uniquement le *seam de préparation sans effet externe*** pour que les deux utilisateurs (préparation nouvelle et rendu historique) appellent un renderer **pur** commun, sans import de `InvestigationAdvisorTool` depuis le module de préparation. Ranger les règles d'assemblage et d'aliases près de leur implémentation pure ; exposer une **interface métier réduite et versionnée**. Ne pas faire migrer le caller de production vers v4, ni supprimer v3 / `_parse_advisor_wire_legacy`, ni toucher à la politique d'envoi S3.

**Forme cible :**

```text
InvestigationAdvisorTool (orchestration historique) ─┐
                                                     ├──> moteur interne de rendu pur
prepare(ConsultationProjection) ─────────────────────┘           │
                                                                v
                                            dossier, aliases, texte wire, bytes, SHA

prepare(...) -> PreparedConsultation | PreparationRefusal
    contrat externe : résultat immuable, statut/refus explicite, octets exacts,
    hash, catalogue de refs, complétude; pas de réseau, budget ou stockage.
```

Un fichier supplémentaire n'est permis que s'il absorbe **réellement** du comportement utilisé par au moins deux chemins ; pas de wrapper de 3 lignes juste pour afficher une nouvelle couche. La structure physique finale est laissée à l'implémenteur tant que les critères d'interface sont respectés. **Ne pas transformer tous les `Mapping` du dépôt en dataclasses :** typer la véritable frontière d'entrée et celle du résultat là où cela réduit l'obligation cognitive des appelants. Les projections internes peuvent rester des structures validées à l'intérieur du module. Éviter le mot « autorisé » pour un objet de projection qui n'a pas traversé le gate S3.

**Critères de profondeur mesurables :** une seule source de vérité du rendu pour les deux chemins ; zéro dépendance de la préparation vers la classe legacy ; la préparation ne demande pas aux appelants de préassembler le dossier et les alias ; aucun recalcul de prompt/refs à l'extérieur du module ; le même rendu `str`/UTF-8/SHA/longueur/réservation qu'avant ; une interface testée en black-box (et non par appel à des helpers privés) ; aucune exposition de nouveau réglage gratuit.

**Régression obligatoire :** corpus figé par type `plan_investigation`, `pivot_strategy`, `yara_draft`, `review_text`, `draft_text`, `suricata_draft`, `figure_plan`, + plusieurs `units`, alias, Unicode/CRLF, texte CTI qui ressemble à du JSON, extrait Base64/hex, collisions `(kind,id,sha256)`, opinion versionnée, restrictions `external_llm_allowed`, TLP RED, `do_not_submit`, signed URLs et secrets réels de fixture. Comparator octet pour octet entre ancienne et nouvelle sortie ; différence requiert justification contractuelle, versioning et revue indépendante, sinon REJECT. **Aucune soumission Bridge réelle**.

**Test de suppression :** si on efface l'ancien `InvestigationAdvisorTool._prompt`, l'ensemble des callers de rendu doivent encore fonctionner via une seule implémentation pure; les tests ne doivent plus dépendre de l'ancienne méthode. Il est acceptable de conserver un alias interne temporaire si exigé par un test legacy, mais pas deux moteurs de rendu concurrents.

**Chemins de test existants :** `backend/tests/test_advisor_consultation.py`, `test_investigation_advisor_tools.py`, `test_advisor_wire.py`, `test_advisor_completeness.py`, `test_operator_tool_invocation.py`. Les oracles RED/GREEN doivent viser **l'interface et la voie qui archive réellement les octets**.

## Décision : TARGET_B — un seul moteur sémantique v4 ; ne pas faire le cutover

### Évidence

- `advisor_semantic_parser.py:55` expose `parse_advisor_semantic_response(raw, consultation_type, prompt_data, operational_secret_values) -> dict[str, Any]` et peut parser l'enveloppe texte compacte v4, avec JSON **accepté mais non exigé** (`:303–348`). Le schéma dicte refusable/partial/invalid/ambiguous et les références.
- `investigation_advisor_tools.py:5607–5658` choisit v4 ou le parse historique v3, lequel contient encore `_parse_advisor_wire_legacy` (`:5942`).
- Le reparse conserve la voie v4 persistée et refuse les vieux formats qui ne peuvent pas être prouvés (`test_advisor_output_reparse.py`).

### Décision

**KEEP v4 parser** : ne pas recréer un second parser. Pour le rendre profond : réduire les traductions de statuts/refs répétées chez les appelants **seulement lorsqu'une duplication est démontrée**. Il peut être utile de typer en interne l'issue `succeeded/partial/refused/invalid/ambiguous`, avec adaptateur vers les dictionnaires persistés existants. Pas de modification du format canonique sans version. La sortie d'un modèle reste non fiable et n'est jamais promue en preuve primaire. Pas de suppression de v3, même si le propriétaire n'exige pas de rétrocompatibilité *des nouvelles consultations*, car le caller v3 reste autoritaire jusqu'au gate S3.

Tests existants : `test_advisor_semantic_parser.py` (17 tests), `test_advisor_output_reparse.py` (7 tests), `test_advisor_completeness.py`. Ne pas transformer la tolérance d'entrée texte en dépendance obligatoire au JSON modèle.

## Décision : BLOCKED_C — ne pas « refactoriser S3 par opportunisme »

### Évidence

- `investigation_service.py:1184` gère l'admission et un chemin d'invocation durable, `:1437` appelle le dispatcher et `:2019` gère la réconciliation. Plusieurs invariants d'autorité, réservation et abandon de tentative restent répartis.
- `investigation_tools.py:645` prend un `asyncio.Lock` pendant `RegistryToolDispatcher.dispatch`; `_dispatch_once` attend l'adaptateur (`:805`) sous ce verrou. Ceci établit une sérialisation **en mémoire par instance**, pas une protection PostgreSQL durable ni une autorisation au dernier paquet envoyé.
- `docs/mission04-implementation/S3_UNBLOCKING_CONTRACT.md` et le plan Mission 03 identifient les enjeux: retries ModelGateway, inventaire de toutes les émissions physiques, transactions longues pendant polling, réponses tardives, version normalizer/settlement.

### Décision

**Pas d'implémentation S3 en Mission 05.** Ces constats sont des contraintes explicites pour la conception ultérieure. Ne pas « optimiser » le verrou d'instance isolément sans traiter les effets externes, les écritures durables et les scénarios de concurrence; cela peut exposer un double envoi. La Phase 3 peut démontrer le comportement de stop/replay/diagnostic sur transports factices, mais ne peut pas conclure `S3_READY` ni `ADVISOR_CUTOVER_READY`.

## Décision : KEEP_D — contexte et pagination

`ResearchContextBuilder.build` (~`research_context_builder.py:1079`), `read_context_section` (~1939) et `read_context_section_page` (~2251), et les fonctions de `context_pagination.py` constituent une implémentation substantielle. Les oracles Mission 04 ont fixé les sémantiques : version courante des findings en contexte, historique séparé, preuves `(observed_at, UUID)` avec NULLs last, SHA du contexte canonique complet, curseur périmé 409, offset legacy invalide 422.

**KEEP jusqu'à preuve du contraire dans les E2E.** La longueur du fichier n'est pas une preuve de faible profondeur. Si un test intégré trouve une duplication de logique full/paged, corriger derrière une interface existante, sans nouvelle abstraction globale ni changement silencieux de hash/cursor.

## Décision : KEEP_E — export d'audit et PDF

Le PDF V5/V6, sa filiation reviewer/approver et les tests PG/Typst/Poppler ont été durcis. La prochaine mission doit vérifier dans **le même scénario** l'observation canonique, l'opinion humaine approuvée, les blobs source SHA, la reconstruction du PDF et la lecture de l'export; **pas** lancer un redesign Typst ou des schémas canoniques. Le contenu canonique doit apparaître exactement une fois, les acteurs dans `pdf_lineage` mais non rendus automatiquement visibles.

## Deux preuves verticales imposées en Phase 3 (distinctes de l'audit)

**E2E-0 : sans échantillon** — une investigation cohérente, PostgreSQL jetable, stockage objet isolé si utilisé, opérateur G2 autorisé, observations et versionnement de finding, consultation Advisor simulée et parsing/reparse v4 hors réseau, refus/partial/succeeded, P10→P09 borné, export d'audit, opinion approuvée et V5/V6/PDF réel, SHA/lineage vérifiés, restart/replay `NEEDS_REVIEW`, zéro vraie émission.

**E2E-1 : avec échantillon *synthétique et inerte*** — les mêmes propriétés, et en plus des octets inertiels stockés dans un magasin objet isolé (MinIO jetable si possible), acquisition/import simulée, sample/association/quarantaine/provenance, extraction/mesures factices, payload D7 permissif quand autorisé, veto explicite `do_not_submit`/TLP RED/`external_llm_allowed=false`/restriction/signed URL, zéro exécution, zéro soumission. L'absence de MinIO testable est un blocage séparé, jamais compensé avec un bucket de production.

**Réutiliser et étendre** plutôt que tout réimplémenter : `backend/tests/integration/test_investigation_vertical_postgres.py` (vertical PG et publication V6), `test_investigation_acquisition_postgres.py`, `test_analyst_opinion_publication_postgres.py`, `tests/workflow_emulator/test_w14_agent_opinion.py`, `test_w15_protocol_pcap_to_opinion.py`, `test_w18_supervised_first_run_recovery.py`, `test_operator_context_summary_postgres.py`. Les nouveaux oracles doivent prouver **deux cycles cohérents chacun exécuté de bout en bout**, pas juxtaposer le nombre de tests de suites différentes. En cas d'effet irréductiblement externe, substituer uniquement l'adaptateur correspondant et décrire le point de simulation.

## Décisions non déléguées / portes de contrôle

- **Architecture cible arrêtée ci-dessus** : Paseo n'a **pas** à recommencer l'audit ou à produire une liste spéculative de candidats; il doit exécuter TARGET_A, KEEP/finition limitée TARGET_B, KEEP_D/E, BLOCKED_C.
- Si TARGET_A ne réduit pas les connaissances requises des appelants ou change des bytes/SHAs sans version et justification, verdict `DEEP_MODULE_REVISE`, pas `GO`.
- Si l'un des E2E n'exécute pas un cycle complet avec ses dépendances jetables, verdict `PHASE3_PARTIAL` / `BLOCKED_*`, jamais `PASS` par assemblage de tests isolés.
- Après TDD, revue xhigh indépendante, provenance des tests, replay complet et CI locale, intégrer les commits atomiques au **main local**, sans pousser sans nouvel accord spécifique. Toujours distinguer Git local de `origin/main`.
- Aucun appel CTI réel, Bridge/provider/ModelGateway externe, aucun live DB/MinIO, aucun déploiement, aucune migration live, aucune activation S3/L22.

## Évaluation actuelle

**GO pour implémenter TARGET_A et les deux E2E hors ligne**. Profondeur totale `PARTIAL` sur advisor ; S3 et advisor cutover `BLOCKED`. La CI distante `main@55fba67d` est verte, mais aucun test local n'a été exécuté pendant cet audit. L'exécution des deux scénarios et l'approbation des refactorings appartiennent à la Mission 05.
