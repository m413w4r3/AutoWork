# D6 — Brief commun advisor : consultation préparée et parseur sémantique

**SHA :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Nature :** brief de conception only. Aucun code, test, migration ou configuration
ne doit être modifié. Le seul écrit autorisé est l'artefact du designer.
**Sources à lire :** ce brief, `_work/M1B_ADVISOR_FLOW.md`,
`_work/M4_ADVISOR_DIAGNOSIS.md`, `_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md`,
`backend/AGENTS.md`, `docs/investigation_contracts.md`,
`.agents/skills/codebase-design/{SKILL.md,DEEPENING.md,DESIGN-IT-TWICE.md}`.
**Interdit :** lire les artefacts `D6_ADVISOR_*.md` des autres designers,
appeler un fournisseur/PostgreSQL/MinIO/Docker, exécuter des tests, créer des
sous-agents.

## 1. Faits vérifiés au SHA

### Chemin

`advisor.consult` → `InvestigationAdvisorTool.execute`
(`investigation_advisor_tools.py:164-778`) :
validation → limites/budget → chargement des preuves → restrictions
d'échantillon → contrôle `external_llm_allowed` → `_consultation_data`
(`:1605-2240`) → redaction (`publication_secret_guard`) → `build_advisor_dossier`
(`advisor_dossier.py:304-939`) → manifeste/text/hash → prompt sizing/preflight
(`:436-551`) → `_prompt` (`:2855-2947`) → archive du prompt → 
`submit_bridge_consultation_once` (`bridge_investigator.py:248-495`) →
`ModelConversationTurnTransport` → `ModelConversationService` →
`ModelGateway` → provider → archive du brut → parse typé/legacy → observation
`operator-advisor-call.v1` → reparse éventuel → avis versionné.

### Responsabilités et propriétaires actuels

- `advisor_dossier` : extraction, sélection, manifeste, complétude. **KEEP**
  (M4-A) ; fonctions pures, pas de replacement démontré.
- `publication_secret_guard` : secrets opérationnels AutoWork. **KEEP** avec
  audit de portée étroit.
- `submit_bridge_consultation_once` : pacing, idempotence, retry borné,
  ambiguïté. **KEEP** ; c'est un seam réel avec adapter de production et
  double scripté.
- `AnalystOpinionService` / `AnalystOpinionV1` : version, hash de contenu,
  provenance Bridge, citations. **KEEP**.
- Parseur sémantique : `advisor_contracts.py` (v3 typé) et parseur legacy v2
  dans `investigation_advisor_tools.py:4689-4780,5067-5105`. Le reparse
  n'utilise que le typé (`:3629-3637`). **REPLACE_CANDIDATE** : alias/refs,
  warnings, texte sûr et schémas divergent.
- Identités de refs : `_typed_transmitted_refs` (`:4453-4505`),
  `_transmitted_refs_for_dossier` (`:4630-4648`), `advisor_wire_aliases`
  (`advisor_wire.py:90-264`) et résolution d'alias du parseur
  (`advisor_contracts.py:166-346`) répondent à des questions différentes sans
  interface typée commune.
- `advisor_wire_aliases` reconstruit un dossier alors que `dossier_text` et
  `dossier_manifest` sont déjà fournis (`advisor_wire.py:203-228`).
  **NARROW_FIX**.
- Reparse : vérifie l'observation, le hash du blob, le contexte, ne rappelle
  pas Bridge, ajoute une observation versionnée (`investigation_advisor_tools.py:3414-3787`).
- Les types acceptés par le reparse n'incluent pas `suricata_draft`
  (`:3442-3487,4188-4225`). **NARROW_FIX** ou documenter.
- Le refus typé du parseur principal est préservé (`advisor_status=refused`).
  Le chemin P10 frère peut effacer `STATUS: refused` et laisser passer des
  candidats (`invariant_proposals.py:1226-1342`). C'est un défaut P10 séparé,
  pas le seam advisor ; il doit rester un `NARROW_FIX` hors de cette conception.

### Timing de préparation

La réservation générique est faite sur les maxima (`BRIDGE_TURNS=1`,
`CONTEXT_BYTES=max_input_bytes`, `TOOL_SECONDS` maximum) **avant** l'adaptateur
(`investigation_advisor_tools.py:3395-3405` ;
`investigation_service.py:1031-1188`). Le prompt final, les refs et le hash
sont calculés **après** la réservation mais **avant** le transport
(`investigation_advisor_tools.py:436-778`). La préparation peut donc produire
un objet `PreparedConsultation` avant le point de décision S3, mais elle est
aujourd'hui enfermée dans l'adaptateur.

### Dossiers/parseurs/versions

- Wire : `advisor-wire.v1` ; tool : `advisor-consult.v1` ; parseur typé :
  `advisor-response.v3` / `advisor-source-parser.v3` ; legacy
  `advisor-response.v2`.
- La sortie brute est archivée avant parsing ; une réponse volumineuse est
  conservée même si elle n'est pas parsée.
- Le reparse ne recharge pas les preuves canoniques et n'appelle pas Bridge.
- L'avis est une version append-only avec `content_sha256`, citations,
  identité de synthèse et de publication de base.

## 2. Garanties non négociables

- Dans une consultation déjà autorisée, les preuves CTI sont permissives :
  raw, hex, Base64, bytes, données réseau, IOC, extraits longs et données
  contradictoires sont des preuves potentielles. Ne pas réintroduire de
  plafond arbitraire AutoWork de 96 Ko, 512 octets ou 16 KiB ; ne pas
  tronquer ou effacer silencieusement.
- Les restrictions explicites restent contraignantes : G2/G3, TLP,
  `external_llm_allowed=false`, `do_not_submit`, TLP RED, restrictions
  source/échantillon, secrets opérationnels AutoWork et URL signées.
- Préserver IDs, SHA-256, offsets, longueurs, alias, provenance et idempotence.
- Un refus explicite reste un refus ; il ne devient pas un warning.
- La sortie brute reste une consultation, jamais une preuve CTI canonique.
- Le reparse réutilise l'output stocké sans nouvelle consultation.
- L'avis final reste versionné et validé par `AnalystOpinionService`.
- Aucun appel fournisseur réel, aucun accès PostgreSQL/MinIO/Docker.
- L'interface ne doit jamais rendre une consultation plus permissive que son
  autorisation effective, ni rendre les preuves autorisées moins observables.

## 3. Dépendances (DEEPENING)

- Sélection/dossier/complétude : in-process, testable directement.
- Redaction/secrets : in-process + configuration injectée.
- Parseur : in-process, mais doit rester compatible avec des réponses legacy
  persistées et avec le reparse.
- Transport : délégué au seam S3 / `submit_bridge_consultation_once` ; le seam
  advisor ne possède ni l'autorisation ni l'egress.
- Opinion : PostgreSQL canonique, déjà testé en intégration ; ne pas le
  remplacer.

## 4. Questions auxquelles la conception doit répondre

1. Où placer le seam ? (adaptateur, module de préparation, module de parsing,
   façade de consultation ?)
2. Contrat public exact : `prepare`, `parse`, `reparse`, types et invariants.
3. Comment produire un `PreparedConsultation` avec octets exacts, refs,
   manifeste, hash et version de parseur, avant le point de décision S3 ?
4. Comment unifier l'identité des refs entre préparation, wire, parse et
   persistance, sans casser les alias legacy ?
5. Comment préserver les refus, les réponses partielles, les ambiguïtés et
   les erreurs sans changer les statuts/outcomes existants ?
6. Comment garder la compatibilité v2/v3 et le reparse des outputs historiques ?
7. Comment traiter les consultations éditoriales multi-unités et le batching ?
8. Quelles obligations d'appelant disparaissent ? Quel code est réellement
   supprimable ?
9. Quels oracles (golden prompt/refs, corpus de parse, reparse, refus,
   preuves permissives, non-régression P10) prouvent le contrat ?
10. Quels `UNKNOWN` et risques de migration/rollback subsistent ?

## 5. Format de l'artefact (13 points obligatoires)

1. Seam actuel et seam proposé.
2. Contrat public (types et pseudo-code).
3. Invariants, timing, versions, erreurs, confidentialité.
4. Exemple d'appel depuis un consommateur réel.
5. Responsabilités cachées dans l'implementation.
6. Responsabilités supprimées des appelants.
7. Dépendances et adapters.
8. Source de vérité persistante et point de décision, si applicable.
9. Oracles positifs, négatifs, crash, replay et concurrence.
10. Compatibilité, migration et rollback.
11. Deletion test.
12. Comparaison avant/après avec éléments vérifiables.
13. Risques, `UNKNOWN` et limites.

Le seam advisor ne doit pas redéfinir le permis, le consume, la révocation ou
le transport : il prépare les octets et parse les résultats. Il doit expliquer
comment il s'interface avec le seam S3 sans créer une seconde source de vérité.
