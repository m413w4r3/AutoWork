# Point de contrôle final — M07B, CTI-OPERATOR-B

## État canonique final
- Investigation `86ece81f-33bf-4230-9c5b-be55ea67855e`, mode `operator_led`, statut API `running`, `stop_reason=null`, aucun job actif. État de travail v23, SHA `7c679c5c990a1a0d50e5e4baf89e638f9aa318c2f54555247e7e8cae67209b15`.
- G2 v43 approuvé et non révoqué; TLP `clear`; `external_llm_allowed=true`. Aucun avis approuvé, publié, repinné ou diffusé.
- Source de contexte : article versionné `fa1440b8-849d-4eb9-8f92-f54d18092bf4`, SHA `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`. Le texte de l’article est une source, pas une observation indépendante des événements sous-jacents.

## Findings à la clôture
1. **Qualified** — `094509f0-413c-5418-89fe-d1b9a13d994a`, hypothèse revendeur, claim `S4P4.1`. L’article rapporte une association DNS historique entre le relais `91.107.156[.]29` et `ns1.soroshpasargad[.]com`; aucune preuve commerciale ne démontre un achat via un revendeur iranien. L’invocation historique VT `1c42606f-a088-4c53-a124-6bb9909d74bf` reste `needs_review`, observation `94b61d49-f455-5b2c-83c1-0bfbdf7f98af`; `adapter_output_archived=false`, `external_effect_unconfirmed`, aucun résultat fournisseur archivé.
2. **Qualified** — `69bc6fc4-c070-551c-bbb2-a7661fc200c4`, hypothèse de portée ETW, claim `S2P2.1`. L’article rapporte `etwEnable=false` dans `RuntimeBroker.exe.config` et le décrit dans le contexte .NET/processus cité; aucune analyse d’échantillon ni mesure d’hôte n’établit la désactivation globale de la télémétrie. Observation source `52bdd0ea-8bde-5084-9d52-8d46f64ec929`.
3. **Qualified** — `40ac6adc-1140-559f-8c9c-273241bbacd0`, hypothèse d’attribution, claim `S6P1.1`. L’attribution de Unit 42 à un acteur aligné sur l’Iran et les similarités rapportées restent des affirmations de l’article; aucune preuve de cette enquête n’identifie indépendamment Blackwood ou une organisation/personne. Observation source `a6b68b75-bcee-5bde-bebc-739cef3adfc5`.

Les trois statuts existants sont restés `qualified`. Aucun résultat direct nouveau ne justifie de les promouvoir à `supported` ou de les réfuter. Aucun finding supplémentaire n’a été créé.

## Pivots et résultats de l’opérateur B
- Lu l’inventaire complet : 15 échantillons documentés, 71 pivots disponibles et 59 indicateurs. Plusieurs pivots `vt.file_report`/`vt.relationships` portent sur des hashes ou indicateurs distincts de l’IP du revendeur; les requêtes VT n’ont pas été répétées après un nouvel état ambigu.
- `samples.compare` et `features.common` sur les hashes documentés ont échoué avec `similarity_comparison_failed:SampleSimilarityError`, sans observation ni résultat de comparaison. Cela ne prouve pas que les échantillons n’ont pas les mêmes caractéristiques. Aucun échantillon n’a été acquis ou exécuté.
- Le rapport VT passif `vt.file_report` de l’échantillon documenté, invocation `1a183d52-8ac5-4dd6-8e69-db2dc80f149d`, reste `needs_review`; observation `76caa5d9-1f49-515c-985a-e16ba69d2430`, SHA `04c8a58b4085914ecd6b19fd0faa180c5b2a0bd84e0802a694b29b39412a1e2e`, dit `adapter_output_archived=false` et `external_effect_unconfirmed`. Le rapport fournisseur est inconnu, pas négatif; ne pas renvoyer cette requête.
- L’appel VT A `eda38efa-b879-4ad1-97bc-31329d79d0a5` sur une autre IP a été refusé avant route fournisseur (`external_effect_refused`, `provider_routes=[]`, pages 0), observation `eb62ae7c-eec5-5853-a965-6a7cdb1ffcc1`; il ne constitue pas un résultat réseau. Shodan est indisponible.

## Avis, consultations et frictions
- Avis `ef1838cb-000f-53a1-bc0e-82dde8eb1179`, v1, SHA `222d05e86b5f32b998729e957ae7a78064e2098cf7e5b4b15639f3536c3ca192`, statut `draft`. La section permise `current_opinion` n’a renvoyé que l’ID, la version et le SHA; `opinion_notes=0`. Le corps et les ancres des blocs n’étaient pas accessibles par les routes autorisées. Le draft n’a donc pas été révisé à l’aveugle.
- Deux consultations B de revue Bridge ont été refusées avant transport : b023 pour schéma `anchor=null` invalide selon le validateur d’exécution; b025 pour `editorial_unit_target_invalid` malgré l’ancre de claim `S2P2.1`. Les deux confirment une non-exécution; aucune revue du texte du draft n’a eu lieu et aucun tour n’a été consommé. Les tentatives A précédentes avaient aussi été refusées. Un seul tour Bridge est consommé dans l’enquête (revue stratégique antérieure); usage fournisseur indisponible.
- Le statut final énumère `ambiguous_invocations=[]` et `invocations_requiring_reconciliation=[]`, mais les invocations `1c42606f-…` et `1a183d52-…` sont individuellement `needs_review` et gardent des réservations. Préserver cette distinction; ne pas compter leurs effets comme dépensés, négatifs ou réconciliés.

## Budgets et prochaine étape permise
- Bridge : 1 consommé, 0 réservé, 199 restants; usage fournisseur `usage_unavailable`. VT : 0 consommé, 13 réservés, 9987 restants. Temps outil : 112 s consommées, 240 s réservées, 86048 restantes. Shodan : 0/1000; téléchargements : 0/1000. Contexte : 9151 tokens et 36604 octets consommés; aucun coût fournisseur additionnel confirmé.
- Aucun test exécuté. Pour poursuivre, réconcilier les deux invocations VT existantes sans les rejouer. Une révision/revue Bridge du draft demandera ensuite un chemin autorisé qui expose son corps et ses ancres exactes. L’état actuel ne permet pas de conclure davantage.

Fin de session opérateur B. Aucun arrêt d’enquête, approbation ou publication n’a été demandé ou effectué.
