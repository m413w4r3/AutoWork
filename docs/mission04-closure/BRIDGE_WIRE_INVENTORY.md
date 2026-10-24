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
| P10 invariants | ACTIVE (Bridge) | texte compact : `advisor-wire.v1`, `TYPE: p10_proposal`, `STATUS`, `CANDIDATE`, `YARA`, lignes `RISK/VALIDATION/QUESTION` | `_parse_proposal_response_details` + `ProposalOutcome` ; refus terminal, partial, ambiguïtés explicites | `ProposalConversationService` ; tour et réponse bruts persistés | déjà compact ; aucun JSON demandé | `invariant_proposals.py:981`, `:1196` |
| Advisor consult (planification, pivots, YARA, Suricata, texte, figures) | ACTIVE (Bridge) | `advisor-wire.v1` : lignes de champs, blocs source, prose balisée | `parse_typed_advisor_response`, `parse_advisor_wire`, dossier/completeness, secret guard | `InvestigationAdvisorTool.execute` → `submit_bridge_consultation_once` | déjà compact ; le parseur accepte du JSON legacy **en entrée** comme compatibilité, sans le demander | `investigation_advisor_tools.py:2859`, `:2924`, `:3689`, `:4601` |
| Advisor reparse hors ligne | OFFLINE | aucun envoi ; relit la sortie brute persistée | `ReparsePersistedAdvisorOutputTool` + parseur typé ; L21 ajoute un parseur partagé v4 | lecture d'artefacts persistés uniquement | aucune émission ; JSON présent dans une preuve = contenu | `investigation_advisor_tools.py:3414` |
| Extraction de production Q2 | ACTIVE (Bridge) | blocs texte `@@FACT`, `@@EVENT`, `@@IOC`, `@@RULE`, `@@UNCERTAINTIES` | parseur de blocs + gates de source | `production_extraction.py`, `production_prompts.py` | déjà compact ; aucun JSON demandé | `production_prompts.py:150-175` |
| Annotation sémantique éditoriale | ACTIVE (Bridge) | blocs texte `TERM`, `ROLE`, `PARAGRAPH_ANCHOR` | parseur de blocs | `production_prompts.py`, enrichissement éditorial | déjà compact | `production_prompts.py:257` |
| Extraction de claims legacy (`ExtractionService.extract_claims`) | **DORMANT** | schéma Pydantic `EvidenceExtractionOutput` ; l'adaptateur Qwen ajoute le schéma au prompt et `response_format: json_object` | `ModelGateway.extract` + validation Pydantic | **aucun caller dans `backend/src`** ; seuls des tests | non converti : API typée de gateway sans consommateur actif ; suppression L22/dead-code après preuve de non-usage | `extraction.py:239`, `model_gateway.py:521`, `integrations/models.py:1113` |
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

- Tous les parcours **actifs atteignables par Bridge** demandent un format
  textuel compact ; aucun ne demande de JSON au modèle.
- Les deux seuls endroits qui manipulent du JSON de modèle sont (a) le
  parseur legacy tolérant de l'advisor, qui accepte du JSON sans l'exiger, et
  (b) l'extraction de claims legacy, **dormante et sans caller de production**.
- Aucune preuve CTI n'est perdue ou rejetée du simple fait qu'elle contient du
  JSON ; les octets bruts restent conservés.
- Aucune modification du dépôt externe `m413w4r3/bridge` n'a été faite.
