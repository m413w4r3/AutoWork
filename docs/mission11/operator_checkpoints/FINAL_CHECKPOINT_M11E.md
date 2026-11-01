# Checkpoint final — M11E

**État :** les deux candidates YARA antérieures ont été remesurées; Bridge a renvoyé une critique YARA partielle avec une source de règle non canonisée. La consultation Suricata a été arrêtée au préflight, sans envoi. La candidate Suricata reste non testée. Aucun avis n’a été composé, aucun finding ni échantillon nouveau n’a été ajouté.

## État canonique et autorisations

- Enquête `operator_led`, toujours `running`; working state final v187, SHA-256 `ea8faec20794413cc2a100f996945ce6d89e8d3ec03b976e898700f48fbb95f6`.
- Au départ de M11E, status : G2 v679 approuvé, non révoqué; le contexte indiquait `external_llm_allowed=true`. TLP:CLEAR était consigné pour ce même G2 v679 dans le checkpoint précédent. Au dernier status, G2 v693 est approuvé/non révoqué et le résumé de contexte confirme encore `external_llm_allowed=true`; ce résumé final n’expose pas le TLP.
- Aucun échantillon exécuté, aucun C2 contacté, aucune acquisition VT ou publication pendant M11E.

## YARA

### Candidates mesurées

| Candidate | Version et source | Test M11E | Mesure / statut |
|---|---|---|---|
| Générique `7f5a2e55-0ab4-4658-b3cc-911588f64c75` | v1; SHA-256 `768fd89988221a49afbcc1367010c8d228821560badb5d591424a25a4349883e` | Invocation `e8f6aabc-8037-4988-b3e9-01fddf6e06ca`, observation `2794bf6f-8d64-5fe1-9f28-26ba4a92c58b` | YARA 4.5.0; 6/6 `new_candidate_unconfirmed`; `ONE_RULE_NOT_VALIDATED` / `real_partial`. |
| Ciblée `6ba2f790-d288-40a7-8fee-220d1c129249` | v1; SHA-256 `1c8b5bbf32edc25074a805373ab7f038340ed22a9fe813fa9d7bb12c4fa769d1` | Invocation `00fa2ac9-cd19-4905-a995-601dad6d6321`, observation `95dfd9d6-ca68-5c75-88d3-7541adbd40a5` | YARA 4.5.0; A/B correspondent, 4 autres candidats non confirmés ne correspondent pas; `ONE_RULE_NOT_VALIDATED` / `real_partial`. |

Les mêmes observations canoniques avaient déjà été mesurées; les invocations M11E ont réussi et ont retourné ces résultats. Aucun positif humainement confirmé, goodware qualifié ou holdout n’est disponible. Les rapports ne donnent donc ni taux de faux positifs en population ni sensibilité de famille.

### Consultation Bridge `yara_draft`

- Requête opclient `m11e-016-yara-bridge`; invocation opérateur `bb3b4ad2-c858-4e3f-98c7-41a026597331`; tour Bridge `283896f9-476b-4dfa-bc2f-4392951d5567`; réponse référencée `3398eaf9-e53f-5301-bf8c-9920e8dfd606`.
- Envoi confirmé, réponse reçue, 1 tour consommé; statut `partial`, `response_state=response_unusable`, `usable=false`. Durée de soumission Bridge rapportée : 228139 ms; usage fournisseur indisponible. Le dossier faisait 815622 octets; préflight `READY`, 213 références envoyées, 417 omises, 0 manquante.
- Parsing partiel : erreurs `$decoder:measurement_bytes_not_found`, `$pool_A:measurement_bytes_not_found`, `$pool_B:measurement_bytes_not_found`; avertissements `wire_header_missing`, `wire_prose_ignored`, `type_missing`, `status_missing`, `yara_measurements_unmatched`, `invariants_missing`, `structured_parse_unavailable`. Ce n’était pas un refus : `failure_stage`, `failure_reason` et `detail_code` ne s’appliquent pas.
- Le texte Bridge qualifie la générique de pivot exploratoire et la ciblée de partitionnement expérimental. Il souligne que 6/6 et 2/6 ne mesurent que les six candidats sélectionnés, pas la performance en population; il relève des faux positifs plausibles dans les bibliothèques .NET, protecteurs et obfuscateurs. La séquence est fragile aux réécritures CIL, même si les jokers de tokens tolèrent certaines variations de métadonnées. Ces éléments sont des conseils non fiables, pas de nouvelles preuves CTI.
- La critique a identifié la faiblesse de `2 of them` : elle n’exige pas le décodeur. La sortie brute contenait une proposition distincte de source SHA-256 `03567991140115f27bf01a0ef5b11c60db1c495e602a5d7de80244968060d530`, avec condition `uint16(0)==0x5A4D and $decoder and 1 of ($pool_*)`.
- Cette source n’a pas été créée comme candidate du workbench ni testée : la réponse était `response_unusable`, et `rule.propose_bridge` exige une réponse YARA canonisée/utilisable. `rule.propose` ne permet pas d’exprimer la combinaison exacte décodeur ET un des deux marqueurs via sa condition enum.
- Reparses locaux v4 (`bc2079f9-8557-457a-be59-6507ebde1886`) et v3 (`0d2db5ac-f3bd-497f-9b47-816552cf6054`) : `raw_advisory_text_available`, avertissement `parser_version_mismatch_raw_text_returned`; `canonical_promotion=false`, `new_observation_written=false`, 0 tour Bridge. Aucun retry externe.

## Protocole et Suricata

- Modèle canonique `documented_not_observed`, observation `d52f0840-4a01-5dbb-a9c2-71217bf5ae27`, blob `3d414d99-091f-42c0-9e3c-b9313f12b92f`, SHA-256 `ac0615fc51364345990a7835d57276d5789b05315c24bbd534f9ff720dda1117`; rendu `392c099b-8f6d-51d1-8dc9-3a9582ebac9f`.
- Code statique de A : helper GitHub Contents GET lit `sha`/`content`; helper séparé construit un commit et utilise PUT; TLS 1.2 est configuré. Références : GET `a08a5772-b78f-5895-816d-a8073a78cbc6`, PUT `456952e8-c263-5c1e-a465-8e051b85f87e`, TLS `a3b992bb-1261-52b3-b9c0-ae6e93896ff6`. Aucun paquet, handshake, SNI, requête réalisée ou commit réussi n’est observé. `api.github.com` est une valeur statique; la règle SNI ne décrit que ce qui pourrait être visible si le SNI était présent.
- Candidate opérateur existante : `alert tls $HOME_NET any -> $EXTERNAL_NET any (msg:"M11 A outbound GitHub API SNI candidate"; flow:established,to_server; tls.sni; content:"api.github.com"; nocase; classtype:trojan-activity; sid:9901101; rev:2;)`. Portée limitée au nom d’hôte, faux positifs plausibles élevés sur l’usage légitime de GitHub.
- Consultation Bridge `suricata_draft`, requête `m11e-026-suricata-bridge`, invocation `1341855f-dfa5-4883-88ca-111c5972af2c`: échec avant envoi. Diagnostic exact : `advisor_preflight_insufficient_evidence`; `failure_stage=evidence_preflight`; `failure_reason=required_canonical_evidence_not_in_payload`; cause `ref=network-fact`, `reason=No measured network fact was loaded for Suricata drafting.` Un ref manquant, 0 source envoyée, 360 omises, `confirmed_non_execution=true`, 0 tour, `response_received=false`; aucun `detail_code` distinct. Dossier préflight de 13204815 octets, SHA-256 `b945ce74e0812a0af2063aeacc3eeaea2586faa588b99202be2d92e3e9ccb980`.
- Aucun PCAP n’est associé; le schéma runtime de `network.suricata_validate_rules` exige `capture_ref`. Validation et replay non exécutés; candidate toujours non testée. Pas de retry sur ce préflight.

## Findings, avis, budgets et frictions

- Aucun finding, hypothèse, expérience ou nouvel échantillon créé. `opinion.compose` non exécuté : aucun brouillon Suricata n’a été reçu et la réponse YARA n’était pas utilisable/canonisée.
- Budget initial → final : Bridge 0→1/200 tours; VT 66→66/10000; téléchargements 4→4/1000; outils 438→1362/86400 s; contexte 0→204617 tokens et 0→818466 octets. Aucun budget réservé; usage Bridge `usage_unavailable`. CAS incorrects : 0.
- Frictions : le normaliseur YARA n’a pas relié les motifs retournés aux mesures; les reparses n’ont pas promu la réponse. Le préflight Suricata demande un fait réseau mesuré que le dossier statique ne contient pas. La latence de retour opérateur des deux consultations a dépassé la fenêtre annoncée; la soumission Bridge du YARA rapporte pourtant 228139 ms et le préflight Suricata n’a soumis aucun contenu. Ces durées ne sont pas réconciliées.
- Prochaine étape utile pour M11F : si une autre consultation YARA est souhaitée, préparer les entrées `measurements` du schéma avec leurs valeurs et références canoniques pour permettre la normalisation; ne pas enregistrer la source brute non utilisable comme règle Bridge. Pour Suricata, attendre une capture ou une observation réseau mesurée avant de reprendre le préflight. Garder l’avis éditorial en attente tant qu’aucun nouveau résultat exploitable ne le justifie.
