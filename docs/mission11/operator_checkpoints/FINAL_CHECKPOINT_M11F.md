# Checkpoint final — M11F

Investigation `519c2336-1fbb-4558-af6b-ea9c7519bf1a` — mode `operator_led`.

## Résultat

La consultation Suricata a été envoyée une fois (`m11f-018-suricata-bridge`, tour Bridge `de1b7817-6d8a-48bc-973a-fa478860824d`). La réponse normalisée est `partial`, `response_state=response_unusable`, `usable=false`; avertissements : `wire_header_missing`, `wire_prose_ignored`, `type_missing`, `status_missing`, `fact_references_missing`, `structured_parse_unavailable`. Sortie SHA-256 `a37b348a1fbf6a10b106e70db0a32d64a9667dc164a7119605f79baa752836ff`. Aucun candidat Suricata n'a été enregistré ou testé; aucun PCAP n'a été associé ou observé dans ce dossier.

La première consultation YARA (`m11f-065-yara-bridge`) a échoué en prétransport avec `advisor_input_invalid`, `failure_stage=pre_transport_validation`, `failure_reason=measurements_invalid`, `confirmed_non_execution=true`, 0 tour Bridge. Les quatre mesures combinaient des clés de schémas distincts, omettaient `blob_id` pour les objets mesure, et contenaient des clés supplémentaires. Aucun `detail_code` n'a été fourni.

La seconde consultation YARA (`m11f-076-yara-bridge-corrected`, invocation `93adba4c-6245-4fcf-95aa-a7679b59d2a0`) a aussi échoué en prétransport : mêmes `error_code=advisor_input_invalid`, `input_error_code=measurements_invalid`, `failure_stage=pre_transport_validation`, `failure_reason=measurements_invalid`; `confirmed_non_execution=true`, `provider_usage.status=not_submitted`, 0 tour Bridge, aucun `detail_code`. Erreurs précises : indices 0, 2, 4 et 6 `content_pattern_not_verified`; indices 5 et 7 `measurement_not_verified`. Les mesures décodeur A/B aux indices 1 et 3 n'ont pas été signalées. Les SHA-256 dérivés localement ont une forme valide mais ne sont pas des IDs canoniques vérifiés.

Mesures vérifiées localement : décodeur A offset 9965, 27 octets, SHA-256 `d9e3ded14f74f5295b19b70e99006e2c230c28c8fec0928ff84171f42cdd123f`; décodeur B offset 12829, 27 octets, SHA-256 `6b1b38025e3ea5efa406ebeb53fbce1015e6daec124f8911d34f71f2e8ae37b2`; pool A offset 10008, 1883 octets, SHA-256 `9facb9d3f9ed91bbd88acbf95af88eace47a1912d90048ccda6269e398eb9a42`; pool B offset 8472852, 964 octets, SHA-256 `45f4ba9a654fce1e9be2128e20a1f4beb8d5b4c1b4dd003810c127906a3983a1`. Les valeurs ont été extraites des observations canoniques et recroisées avec longueur, offset et SHA.

## Chaîne de règle et rédaction

Aucune réponse YARA Bridge exploitable ni candidate canonique n'a été produite. `rule.propose_bridge` et `rule.test` figurent dans `blocked_capabilities`; aucune voie de contournement n'a été utilisée. Aucun `rule.test` M11F n'a été lancé faute de candidate. `opinion.compose` n'a pas été appelé : les nouvelles sorties sont inutilisables et ne justifient pas de brouillon factuel. Les résultats historiques M11E (baseline générique 6/6, règle ciblée 2/6 sur six candidats non confirmés) ne constituent pas des tests M11F.

## État, autorisation et suite

État final GET `m11f-077-final-status` : investigation toujours `running`, working state v190, SHA-256 `47c4e353533f0b44ccdae2df6d5e9db11bc42e712fd520c26097b7525be986ae`. G2 v703 approuvé, non révoqué; TLP `clear`; `do_not_submit=false`; `external_llm_allowed=true`; Bridge autorisé. Budget Bridge : 2 tours consommés, 0 réservé, 198 disponibles.

Reprise possible si le workbench expose les `content_pattern_id` réellement vérifiés et rend vérifiables les mesures de pools FieldRVA. Toute nouvelle consultation doit utiliser un nouvel ID; les IDs M11F terminés ne sont pas rejoués. Le prochain pas analytique suggéré par l'API est un checkpoint `plan_investigation`; il n'a pas été exécuté, car la mission M11F était bornée à la chaîne Suricata/YARA/opinion.

Aucun statut global de validation n'est attribué. Aucun test réseau n'a été réalisé.
