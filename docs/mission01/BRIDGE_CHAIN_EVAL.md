# Mission 01 — évaluation de la chaîne Bridge AutoWork

## Chemins observés

### `bridge_led`

1. `ResearchContextBuilder.build` charge l’instantané d’article, les observations, les constatations et l’état de travail canonique : [research_context_builder.py](../../backend/src/cti_app/application/research_context_builder.py:1054). `InvestigationService` construit ce contexte puis le remet à `BridgeInvestigator.turn` : [investigation_service.py](../../backend/src/cti_app/application/investigation_service.py:565) et [investigation_service.py](../../backend/src/cti_app/application/investigation_service.py:654).
2. `BridgeInvestigator.turn` calcule l’identité du contexte, rend le prompt et appelle le transport : [bridge_investigator.py](../../backend/src/cti_app/application/bridge_investigator.py:888) et [bridge_investigator.py](../../backend/src/cti_app/application/bridge_investigator.py:928). Le rendu textuel est [render_investigation_prompt](../../backend/src/cti_app/application/bridge_investigator.py:1275); il ne rejette plus un prompt à 96 000 octets.
3. `submit_bridge_consultation_once` applique le pacing puis transmet le prompt et son empreinte à l’interface `InvestigationTurnTransport` : [bridge_investigator.py](../../backend/src/cti_app/application/bridge_investigator.py:183). L’état du transport et la réponse sont ensuite traités par `BridgeInvestigator.turn`.

Le contrat de contexte sérialisé conserve son plafond technique de 262 144 octets dans [investigation_contracts.py](../../backend/src/cti_app/application/investigation_contracts.py:35). Ce plafond porte sur l’objet canonique et n’est pas le plafond de 96 000 octets qui a été retiré du renderer.

### `operator_led` — `advisor.consult`

`InvestigationComposition` n’enregistre le conseiller que si un transport est explicitement injecté; il lui passe aussi les dépôts canoniques, dont l’état de travail : [investigation_composition.py](../../backend/src/cti_app/application/investigation_composition.py:1539). `InvestigationAdvisorTool.execute` valide les sept types, charge les références, vérifie `external_llm_allowed`, construit les données par consultation, applique la protection des secrets et crée le dossier : [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:154) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:242).

`build_advisor_dossier` projette les passages d’article, hypothèses, avis, observations, constatations, mesures et faits réseau dans des pièces lisibles, avec référence et empreinte par pièce : [advisor_dossier.py](../../backend/src/cti_app/application/advisor_dossier.py:242). Le rendu de fil réutilise exactement le dossier mesuré : [advisor_wire.py](../../backend/src/cti_app/application/advisor_wire.py:253) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:2558). Les références explicitement demandées restent exigées; les autres observations autochargées sont projetées par pertinence. Pour un plan large sans correspondance lexicale, le renderer choisit une preuve canonique de repli et consigne les autres comme omissions intentionnelles avec raison.

Le préflight s’exécute avant le transport. Il distingue preuve récupérable, preuve absente et preuve non vérifiée; le basculement Bridge au-dessus de 200 000 octets n’est pas un refus AutoWork. Le seuil technique configurable existant de 4 000 000 octets demeure une limite de traitement exacte; aucune estimation d’octets n’est utilisée pour décider la complétude. Après un succès ou un état indiquant une tentative/soumission, les statuts « envoyé » et l’état de transport sont ajoutés à la provenance; un refus `pre_submission` ne marque aucune pièce `SENT_TO_BRIDGE` : [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:292) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:623).

La réponse brute est archivée avant parsing; l’observation `operator-advisor-call.v1` consigne le statut de transport, les empreintes/taille prompt-dossier-sortie et le résultat parsé : [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:639), [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:721) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:831). L’identifiant de transport provient uniquement de `TurnSubmission`; aucun reçu fournisseur n’est construit par AutoWork.

### `advisor.reparse`

`ReparsePersistedAdvisorOutputTool.execute` relit l’observation locale `advisor_call`, contrôle son empreinte et reparcourt une sortie déjà archivée : [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:3089) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:3113). Il accepte les sorties `yara_draft`, `draft_text`, `review_text` et `figure_plan`, puis ajoute une observation versionnée avec `bridge_turns_consumed: 0` : [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:3336) et [investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py:3379). Il ne construit pas de nouveau prompt et n’appelle pas le transport.

## Complétude requise par question

| Type | Preuve minimale avant soumission |
|---|---|
| `plan_investigation` | État de travail versionné et empreinté, au moins une hypothèse active, puis au moins un corps d’observation, de constatation ou de passage d’article avec référence canonique. Pour un claim explicite, son passage doit être présent. Une contradiction demandée exige une constatation `refuted` dont `claim_refs` contient le claim demandé. |
| `pivot_strategy` | Même état/hypothèse et preuve canonique qu’un plan; joindre la mesure discriminante avec empreinte de l’observation, échantillon, valeur et offset lorsqu’elle est disponible. |
| `review_text` | Avis exact par ID, version, empreinte de l’avis, type de bloc et empreinte du texte relu; joindre le corps et la référence de chaque observation/constatation citée. Les passages de claim nommés dans la question sont aussi exigés. |
| `draft_text` | Au moins une assertion validée, avec texte et une ou plusieurs références canoniques observation/constatation à SHA valide; le prompt doit rendre l’assertion et les références. Les passages de claim explicitement cités sont requis. |
| `yara_draft` | Mesure vérifiée avec SHA d’échantillon, offset non négatif, valeur, ID d’observation et SHA de cette observation. Les métadonnées existantes de version de règle/corpus sont transmises et tracées lorsqu’elles sont disponibles; aucune version ni empreinte de corpus n’est déduite si elle manque. |
| `suricata_draft` | Fait réseau mesuré, champ/valeur et SHA de l’observation source; les détails de direction ou de temps ne sont exposés que s’ils existent dans les faits canoniques projetés. |
| `figure_plan` | Au moins une constatation canonique sélectionnée, avec SHA, plus les observations ou passages de claim qui étayent la figure; conserver mesures, unités et offsets canoniques lorsqu’ils existent. Une contradiction demandée exige une constatation `refuted` liée au claim demandé dans `claim_refs`. |

La projection d’état utilise les objets existants; aucune table ni ontologie CTI n’est ajoutée. Le snapshot d’article fournit claim, titre, texte et index de section quand présent; il ne fournit actuellement pas d’offset de passage, donc aucun offset n’est inventé. Pour YARA, une référence d’observation n’est pas le corps de mesure. De même, une observation canonique référencée dont le corps n’a pas été chargé peut être `RETRIEVABLE`, mais jamais `PRESENT_IN_CANONICAL` ni `SENT_TO_BRIDGE`.

Chaque pièce expose son `source_ref`, son SHA canonique, le SHA et la taille du corps projeté ainsi que le SHA et la taille de son rendu. Le dossier et le prompt complet exposent leur taille UTF-8 mesurée et leur SHA-256. La sortie archivée expose séparément son SHA et sa taille.

## États de complétude et préflight

La projection est définie dans [advisor_completeness.py](../../backend/src/cti_app/domain/advisor_completeness.py:6) et calculée dans [evaluate_advisor_evidence_preflight](../../backend/src/cti_app/application/advisor_dossier.py:885).

| État | Signification |
|---|---|
| `REQUIRED` | Le contrat de la question exige la preuve. |
| `PRESENT_IN_CANONICAL` | Le corps est chargé avec empreinte valide et métadonnées requises. |
| `IN_DOSSIER` | La pièce complète figure dans le dossier rendu. |
| `SENT_TO_BRIDGE` | Après l’appel à l’interface, le prompt soumis contient le dossier exact et la pièce. |
| `RETRIEVABLE` | Une référence canonique existe mais son corps doit être récupéré, vérifié ou ajouté au dossier. |
| `INTENTIONALLY_OMITTED` | La pièce n’est pas requise par cette question et n’a pas été retenue. Une raison est jointe. |
| `MISSING` | Le corps requis n’est pas présent; une référence seule ne le remplace pas. |
| `NOT_VERIFIED` | Le corps existe mais son empreinte ou les métadonnées exigées ne sont pas vérifiables. |

Résultats du préflight : `READY` permet l’interface; `NEEDS_RETRIEVAL` indique les références à récupérer; `INSUFFICIENT_EVIDENCE` indique les preuves introuvables dans l’instantané chargé; `POLICY_BLOCKED` reflète le refus de divulgation existant; `TECHNICAL_BLOCKED` reflète une empreinte/métadonnée non vérifiée, une troncature critique déjà signalée ou le plafond technique configuré. Chaque refus local indique `missing_refs` et raisons lorsqu’il s’agit d’une preuve. Une contradiction indispensable absente ou seulement référencée bloque ou demande sa récupération avant l’interface. Si la question nomme un claim, une constatation `refuted` ne satisfait le contrat que lorsque son `claim_refs` inclut ce claim.

## Oracles hors ligne

Toutes les données de test sont synthétiques. Le transport testé est `ScriptedTurnTransport`; il ne contacte ni fournisseur ni service réseau.

1. **Preuve décisive absente puis récupérée** — une observation canonique avec ID/SHA mais sans corps donne `NEEDS_RETRIEVAL` (sans la compter présente); après ajout du corps et du passage `S1P3.1`, le préflight devient `READY` et le manifeste soumis contient `SENT_TO_BRIDGE`.
2. **Contradiction non projetée et liée au claim** — une constatation canonique `refuted` omise du dossier donne `NEEDS_RETRIEVAL`; une contradiction liée à un autre claim ne satisfait pas une question qui nomme `S1P3.1`; le dossier reconstruit avec une constatation dont `claim_refs` contient `S1P3.1` passe `READY` et rapporte `SENT_TO_BRIDGE`.
3. **Omission intentionnelle et échec avant transport** — une preuve canonique non requise reçoit `INTENTIONALLY_OMITTED` avec raison et alimente le compteur `omitted`; un résultat `pre_submission` ne marque pas les preuves comme envoyées. Les consultations synthétiques plan/YARA vérifient que des observations autochargées hors périmètre ne deviennent pas des références obligatoires.
4. **Tailles et intégrité** — le renderer `bridge_led` produit exactement 95 999, 96 000 et 96 001 octets; aucun seuil AutoWork à 96 000 ne les rejette. Un dossier synthétique supérieur à 200 000 octets est transmis intact au faux transport. Le choix `text_file` au-dessus de 200 000 est seulement une simulation du contrat Bridge établi dans les artefacts Mission 00; ce test ne prouve pas un appel ni un reçu Bridge réel. Les SHA et tailles de corps/pièce/dossier sont comparés.
5. **Reparse partiel** — `test_reparse_appends_verified_parse_without_a_bridge_transport` vérifie le reparse d’une réponse partielle déjà persistée, l’absence de nouvelle soumission et `bridge_turns_consumed == 0`.

Le préflight émet `required`, `present`, `sent`, `omitted` et `missing`, plus les décomptes d’assertions pour `draft_text`. Les corps restent dans le dossier/prompt déjà prévu; le manifeste de complétude ne duplique pas les corps sources.

## Faits établis et limites

- Les 144 lignes `UNKNOWN_SEMANTIC;metadata_only` du registre Mission 00 n’établissent pas 144 omissions.
- Le plafond AutoWork de 96 000 octets a été retiré du renderer `bridge_led`. Le plafond technique de contexte de 262 144 octets et le plafond configuré existant du conseiller restent distincts.
- Selon les faits Mission 00 fournis, Bridge bascule un prompt de plus de 200 000 octets UTF-8 vers une pièce jointe texte; cela ne signifie ni refus ni troncature. Ce changement ne modifie pas le dépôt Bridge.
- La simulation de mode fichier confirme seulement qu’AutoWork rend et remet le payload intact à une interface factice. Elle n’observe pas le comportement d’un Bridge réel.
- Les extractions canoniques existantes peuvent ne pas fournir de passage, offset, mesure réseau, version de règle ou empreinte de corpus. Ces cas restent explicitement récupérables, manquants ou non vérifiés; les tests ne les inventent pas.
