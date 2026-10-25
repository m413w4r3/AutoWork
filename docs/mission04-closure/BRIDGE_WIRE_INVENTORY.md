# Mission 04 closure — inventaire des formats demandés au modèle (D10 / G_WIRE)

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Méthode :** lecture des constructeurs de prompts, parseurs et callers dans
`backend/src/cti_app`, complétée par les tests et le workflow emulator.
**Distinction appliquée :**
- **LLM-JSON** = AutoWork demande au modèle de produire un objet JSON.
- **TECHNIQUE** = enveloppe HTTP/JSON du transport, artefact JSON canonique
  AutoWork, schéma persistant, ou preuve CTI contenant du JSON. Non visé par D10.
- **TEXTE COMPACT** = format applicatif textuel contrôlé par AutoWork.

## 1. Matrice des parcours

| Parcours | Classe | Format demandé au modèle | Parseur / validation locale | Caller / persistance | Conversion D10 | Réf. |
|---|---|---|---|---|---|---|
| P10 invariants | ACTIVE (Bridge) | texte compact : `advisor-wire.v1`, `TYPE: p10_proposal`, `STATUS`, `CANDIDATE`, `YARA`, lignes `RISK/VALIDATION/QUESTION` | `_parse_proposal_response_details` + `ProposalOutcome` ; refus terminal, partial, ambiguïtés explicites | `ProposalConversationService` ; tour et réponse bruts persistés | déjà compact ; aucun JSON demandé | builder `invariant_proposals.py:984`, parser `:1199` |
| Advisor consult (planification, pivots, YARA, Suricata, texte, figures) | ACTIVE (Bridge) | `advisor-wire.v1` : lignes de champs, blocs source, prose balisée | `parse_typed_advisor_response`, `parse_advisor_wire`, dossier/completeness, secret guard | `InvestigationAdvisorTool.execute` → `submit_bridge_consultation_once` | déjà compact ; le parseur accepte du JSON legacy **en entrée** comme compatibilité, sans le demander | prompt builder `investigation_advisor_tools.py:2865`, wire `:4578`, submission `:775` |
| Advisor reparse hors ligne | OFFLINE | aucun envoi ; relit la sortie brute persistée | `ReparsePersistedAdvisorOutputTool` + parseur typé ; L21 ajoute un parseur partagé v4 | lecture d'artefacts persistés uniquement | aucune émission ; JSON présent dans une preuve = contenu | `investigation_advisor_tools.py:3415` |
| Extraction de production Q2 | ACTIVE (Bridge) | blocs texte `@@FACT`, `@@EVENT`, `@@IOC`, `@@RULE`, `@@UNCERTAINTIES` | parseur de blocs + gates de source | `production_extraction.py`, `production_prompts.py` | déjà compact ; aucun JSON demandé | `production_prompts.py:306` |
| Annotation sémantique éditoriale | ACTIVE (Bridge) | blocs texte `TERM`, `ROLE`, `PARAGRAPH_ANCHOR` | parseur de blocs | `production_prompts.py`, enrichissement éditorial | déjà compact | `production_prompts.py:257` |
| Boucle d'investigation Bridge | ACTIVE (Bridge) | texte/Markdown, aucun JSON app demandé | parser de tours Bridge | `BridgeInvestigator` | conservé ; revu le 2026-10-25 | `bridge_investigator.py:1632` |
| Recherche de références de production | ACTIVE (Bridge) | texte/Markdown | parser de références | `production_workflow.py` | conservé ; revu le 2026-10-25 | `production_workflow.py:1208` |
| Recherche/fusion Discovery | ACTIVE (Bridge) | texte/Markdown, y compris le prompt de reprise de complétion | parser Discovery | `discovery/service.py`, `chatgpt_planner.py`, `discovery/recovery.py` | conservé ; aucun JSON app demandé | dispatches `discovery/service.py:357`, `chatgpt_planner.py:135` ; reprise `recovery.py:273` via `api/discovery_recovery.py:234` ; formats `discovery/prompts.py:30`, `cumulative/merge_wire_format.py:21` |
| Pertinence, synthèse et rédaction éditoriale | ACTIVE (Bridge) | texte/Markdown, y compris révision ciblée et enrichissements supplémentaires | parseurs de production | `production_relevance_model.py`, `production_synthesis.py`, `production_editorial_enrichment.py`, `production_enrichment_revision.py` | conservé ; aucun JSON app demandé | `production_relevance_model.py:883`, `production_synthesis.py:1965`, `production_editorial_enrichment.py:6522`, `:6989`, `:7103`, `:7232`, `production_enrichment_revision.py:1625` ; contrat Markdown des références `production_prompts.py:413` |
| API de conversation générale | ACTIVE, messages fournis par l'appelant | pas de contrat de sortie compact app ; relaie les messages de l'appelant au gateway | aucun contrat app | `api/model_conversations.py`, `model_conversations.py` | hors conversion D10 : aucun JSON app demandé ; route à scoper explicitement | `api/model_conversations.py:197`, `model_conversations.py:1267` |
| Extraction de claims legacy (`EvidenceExtractionService.extract_claims`) | **DORMANT** | schéma Pydantic `EvidenceExtractionOutput` ; l'adaptateur Responses demande un objet JSON dans le prompt (`models.py:938`) et Qwen ajoute en plus le champ transport `response_format: json_object` (`models.py:1115`) | `ModelGateway.extract` + validation Pydantic | **aucun caller de production dans `backend/src`** ; seul l'appel interne du service non instancié | non converti : API typée de gateway sans consommateur actif ; candidat L22 = chemin legacy typé et son wiring de schéma, en conservant le parsing partagé et l'extraction d'indicateurs | `extraction.py:214`, `:257`, `model_gateway.py:521`, `integrations/models.py:938`, `:1115` |
| Enveloppes HTTP Bridge / ModelGateway | TECHNIQUE | JSON de protocole | contrats HTTP | transport | conservé : ce n'est pas une sortie LLM | — |
| Artefacts canoniques (`render-data`, observations, payloads) | TECHNIQUE | JSON AutoWork | schémas/validation | PostgreSQL/MinIO | conservé | — |
| Preuve CTI contenant du JSON | TECHNIQUE / DATA | — | contenu traité comme donnée | observations | conservé, jamais interprété comme instruction | — |

## 2. Exceptions justifiées

1. **Extraction legacy typée** : le seul chemin qui demande encore du JSON au
   modèle. Il n'a aucun caller de production à ce SHA. Le convertir exigerait
   de changer une API de gateway typée (`ModelGateway.extract`) et son
   consommateur potentiel ; D10 demande de documenter, pas de casser un
   contrat externe sans besoin. Il reste donc **DORMANT**, explicitement
   nommé, et candidat à suppression en L22 après un dernier contrôle de
   non-usage et une revue.
2. **Parseurs advisor legacy** : `parse_typed_advisor_response` et
   `_parse_advisor_wire_legacy` acceptent du JSON historique en entrée. Ils ne
   le demandent pas. La nouvelle version L21 n'oblige pas à reproduire v2/v3 ;
   un reparse incompatible retourne un statut explicite (D8).
3. **Enveloppes et stockage JSON** : conservés tels quels. Les supprimer
   reviendrait à modifier le transport ou le schéma canonique, hors mandat.

## 3. Conclusion G_WIRE

- Tous les prompts **fixes et authored par l'application** sur les parcours
  actifs Bridge/ModelGateway demandent un format textuel compact ou du
  Markdown ; aucun ne demande un objet JSON au modèle. Cela inclut la boucle
  d'investigation Bridge, la recherche de références, Discovery, la pertinence,
  la synthèse et la rédaction éditoriale, en plus de P10, de l'advisor et de
  l'extraction Q2.
- L'API de conversation générale relaie des messages **fournis par
  l'appelant** ; elle n'a pas de contrat de sortie compact app et n'est donc pas
  un parcours JSON authored par l'application. Elle est néanmoins enregistrée
  comme route active à scoper explicitement.
- Les endroits qui manipulent du JSON de modèle sont :
  (a) le parseur P10, qui tente `json.loads` avant le wire textuel — compatibilité
  d'entrée, sans second envoi ;
  (b) les parseurs advisor legacy, qui acceptent du JSON en entrée sans
  l'exiger ;
  (c) l'extraction de claims legacy typée (`EvidenceExtractionService`), avec
  ses deux adaptateurs structurés, **dormante et sans caller de production**.
  Son candidat de retrait L22 est le chemin typé et son wiring de schéma, en
  conservant le parsing partagé et l'extraction d'indicateurs ;
  (d) `normalize_discovery_output` (`model_output_normalization.py:37`), un
  parseur JSON découvert par la revue, **sans caller de production dans
  `backend/src`** ; il n'ajoute aucune demande JSON active.
- L'inventaire vérifie que du JSON est accepté comme texte à
  `extraction.py:154`. Il ne re-prouve pas la rétention bout-en-bout des octets
  bruts, qui relève des tests de stockage canonique ; aucune conclusion de
  perte ou de rejet n'est tirée de ce seul inventaire.
- Aucune modification de ce checkout n'a touché le dépôt externe
  `m413w4r3/bridge` ; son état n'est pas vérifié ici.

## 4. Réconciliation de la revue indépendante (2026-10-25)

La première revue a relevé : inventaire actif incomplet, conclusion « deux
seuls endroits » inexacte, nom de service legacy incorrect et références Q2
obsolètes. Cette version ajoute les parcours Bridge/Discovery/production, la
route de conversation générale, la compatibilité JSON d'entrée P10, le nom
`EvidenceExtractionService`, les deux adaptateurs structurés et la référence Q2
`production_prompts.py:306`. La seconde revue a ensuite corrigé les références
advisor/P10, distingué la demande JSON dans le prompt Responses du
`response_format` Qwen, ajouté la reprise Discovery, les révisions éditoriales
et `normalize_discovery_output` (dormant, sans caller), et qualifié les
affirmations de rétention bout-en-bout et d'état du dépôt externe. Une
troisième revue doit confirmer la version courante.
