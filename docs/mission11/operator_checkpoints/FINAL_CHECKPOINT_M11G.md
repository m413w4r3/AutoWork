# Checkpoint final — M11G : relecture locale des réponses Bridge

Investigation `519c2336-1fbb-4558-af6b-ea9c7519bf1a` (`operator_led`), statut `running`. État canonique avant/après les deux reparses : working state v190, SHA-256 `47c4e353533f0b44ccdae2df6d5e9db11bc42e712fd520c26097b7525be986ae`. Dernier status : G2 v707 approuvé, non révoqué.

## Suricata M11F

- Réponse existante : observation `a5dd4c1e-4496-50df-b49e-ee72890e28da`, tour Bridge `de1b7817-6d8a-48bc-973a-fa478860824d`, SHA source `a37b348a1fbf6a10b106e70db0a32d64a9667dc164a7119605f79baa752836ff`.
- Reparse `advisor-source-parser.v4`, invocation locale `ca4d30fe-946b-4ddd-b26a-38f2501aa2ce`, observation de résultat `ac18d748-690f-5d97-b339-631faf3f39f5`, SHA `7d3e41d97ccca2fccd6d4a9089d52093a9316dc15d28a7b4da50be36c53cb797`.
- Valeurs exactes : `status=raw_advisory_text_available`, `usable=true`, `canonical_promotion=false`, `new_observation_written=false`, `stored_parser_version=null`, `selected_parser_version=advisor-source-parser.v4`, `warnings=["parser_version_mismatch_raw_text_returned"]`.
- Le texte brut propose une règle expérimentale sur le SNI `api.github.com`. Les citations `[E7]`, `[E9]`, `[E46]`, `[E8]`, `[E282]`, `[E295]` restent dans la prose; aucune `fact_refs` canonique n’est retournée. La réponse n’a pas été promue en règle canonique.
- `network.suricata_validate_rules` non appelé : pas de règle canonisée à tester. Aucun schéma de validation ni capture n’a été utilisé/créé. La règle reste non testée.

## YARA M11E

- Réponse existante : observation `3398eaf9-e53f-5301-bf8c-9920e8dfd606`, tour Bridge `283896f9-476b-4dfa-bc2f-4392951d5567`.
- Reparse `advisor-source-parser.v4`, invocation locale `8174a589-879a-4184-a700-decff718007a`, observation de résultat `09e83470-e8eb-5a81-9979-e074181d64f9`, SHA `f398e2a7d371eb27959fc6111d04ff3a2a75cd3f5d11a087dbb3d919325cf5c5`.
- Valeurs exactes : `status=raw_advisory_text_available`, `usable=true`, `canonical_promotion=false`, `new_observation_written=false`, `stored_parser_version=null`, `selected_parser_version=advisor-source-parser.v4`, `warnings=["parser_version_mismatch_raw_text_returned"]`.
- La règle proposée `uint16(0)==0x5A4D and $decoder and 1 of ($pool_*)` n’est pas canonisée. Limite mesurée dans M11E : `measurement_bytes_not_found` pour `$decoder`, `$pool_A`, `$pool_B`, plus `yara_measurements_unmatched`. La tentative corrigée M11F avait aussi `content_pattern_not_verified` aux indices 0, 2, 4, 6 et `measurement_not_verified` aux indices 5, 7. Le schéma d’entrée réel est `advisor-consult.v1`; ses mesures sont des objets avec champs `blob_id`, `kind`, `offset`, `value`, `content_pattern_id` (extras permis). Les motifs ou octets mesurés n’étaient pas liés/vérifiés comme requis par la normalisation. Ne pas contourner ces contrôles.
- Pas de `rule.propose_bridge`, `rule.test`, nouvelle consultation Bridge ou nouveau tour.

## Activité et compteurs

- Actions M11G : 2 reparses locaux; 0 Bridge; 0 VT; 0 appel de validation Suricata; 0 capture; 0 règle/finding/expérience créée.
- Erreurs de refus : 0. CAS incorrects : 0. Une erreur locale de préparation `TypeError` avant écriture a été corrigée; elle n’a déclenché aucun POST. Les deux POST reparses valides ont répondu HTTP 200, statut invocation `partial`.
- Budget final lu : Bridge 2/200 consommés, 0 réservé; VT 66/10000; téléchargements 4/1000; secondes outil 2283/86400. Usage fournisseur : UNKNOWN. Aucun partage externe en M11G.
- Consigner dans `operator_log_M11G.md`.

## Faits et limites

Faits vérifiés : les reparses v4 ont conservé le texte brut des deux réponses, et ont explicitement refusé toute promotion canonique (`canonical_promotion=false`, `new_observation_written=false`). Le résultat Suricata n’expose aucune `fact_refs`; la réponse YARA demeure affectée par l’absence de mesures vérifiables pour décodeur et pools.

Limites : les deux réponses ne sont pas des candidates canoniques; aucune validation réseau ou test de règle n’a été effectué. Les citations dans la prose Bridge ne deviennent pas des références factuelles canoniques. Aucune conclusion PASS n’est attribuée.
