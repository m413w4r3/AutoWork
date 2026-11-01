# Checkpoint final M11I — brouillon créé, origine Bridge non établie

Enquête `519c2336-1fbb-4558-af6b-ea9c7519bf1a` (`operator_led`). Session limitée à la lecture de l’état et à un POST `opinion.compose`. Aucun statut PASS.

## Résultat de l’unique appel

- Requête relais: `m11i-010-opinion-compose`; invocation `b2bf996e-15c8-43c5-af43-13517bf5bf76`, outil `opinion.compose`, schéma `operator-opinion.v1`.
- HTTP 200; invocation `succeeded`, `error_code=null`, `evidence_missing=[]`, `resubmission_allowed=false`. L’appel était déjà terminal lors du premier contrôle d’état; aucun rejeu et aucune attente prolongée nécessaire.
- Brouillon: `7507f2b3-a4e0-5c32-9ee8-43339b129edc`, version 1, SHA contenu `27d6cda6b970aee6f32779bdef0872144db1e8cb6e40dd31d60e37f6230cc8b5`; `omitted_observation_refs=[]`.
- Sélection: findings `a68f34f1-3d71-5019-9d39-886a1ee4ad1c`, `5369ee6b-8152-5499-8a93-dd45f9234d43`, `104b8300-0fd0-5e5b-ac78-54bb3da7301a`; zéro claims; blocs findings clés, réseau/infrastructure, détections proposées et incertitudes/prochaines pistes.
- La réponse n’expose pas `usable`, avertissements, `failure_stage`, `failure_reason` ou `detail_code`. `advisor_output=null` et `provider_response_id=null`. Le budget Bridge n’a pas changé (3 consommés, 197 restants). En conséquence, l’existence du brouillon est vérifiée, mais sa rédaction par Bridge ne l’est pas. L’opération a réussi localement sans consommation Bridge visible; cette friction ne peut pas être réparée dans la limite d’un seul appel.
- `current_opinion` pointe sur ce brouillon, mais `opinion_notes` est vide dans la lecture de contexte; le texte du brouillon n’a pas été récupéré. Aucun approbation ni publication.

## Éléments canoniques fournis

- Finding `a68f34f1-3d71-5019-9d39-886a1ee4ad1c`: code statiquement relié lisant/écrivant du contenu GitHub et configuration TLS 1.2. Observations `456952e8-c263-5c1e-a465-8e051b85f87e`, `8dc42b08-bdb8-5006-9ad4-4cd6c3cfb73f`, `a08a5772-b78f-5895-816d-a8073a78cbc6`, `a3b992bb-1261-52b3-b9c0-ae6e93896ff6`, `c9571f84-f1a2-5658-aeef-663a4d6a571e`.
- Finding `5369ee6b-8152-5499-8a93-dd45f9234d43`: lecteur de configuration `.conf`, Base64, RC4, décodage UTF-8, clés/valeurs. Observations `3ee05c7c-cf1f-5a73-8607-405e434d8660`, `4b6da160-514f-5e4e-97d2-2920da100455`, `62e87c4b-8b4a-50ec-9555-afe7127c3e50`.
- Finding `104b8300-0fd0-5e5b-ac78-54bb3da7301a`: la candidate YARA `M11_Shared_FieldRVA_Decoder` v1 correspond à A, B et HorseStats.dll dans les mesures citées; le CIL montre un motif de décodeur partagé (XOR index et `0xAA`). Cela n’établit pas une famille. Observations `064805fe-ffb5-5ace-a772-e219d28cb65a`, `0a62e83b-74bf-5951-914a-3522d17739ab`, `21766413-9d5f-5612-8e56-e40cec6c98a9`, `63dbbe67-4492-5f26-b37d-68514605016f`, `813e0f6e-f20c-5b6d-be43-b5ac67aa1d70`, `824b9f0c-1352-57b9-9e76-eb8248d084e8`, `a1787d74-3afe-5e6d-80fb-9e8469195d12`, `bfda65f0-553e-5d33-b461-e54daaf60eac`.
- Réponse consultative Suricata M11H corrigée: invocation `6f61fb59-21da-413f-a941-7bafc11a5c36`, `partial`, mise à jour `2026-11-01T19:51:41.306488Z`; observation `a0c7b811-8c73-51a5-87fd-d1c5972d654d`; SHA sortie rapportée `5b94973c…98bf`; `usable=true`, `partial_response_usable`, `local_suricata_prose_v1`, un tour Bridge consommé. Candidate SNI `api.github.com`, références `nf001–nf019`. Règle expérimentale et non testée; aucune capture, validation Suricata non appelée.

## Budgets, contrôles et limites

- Avant compose: état v191 / SHA `7f3ea01d63ca890b36456c9af721b369ac40758dfa6a3940bfa4ad9faa4cbfa9`; G2 approuvé/non révoqué v709; `external_llm_allowed=true`; TLP `clear` lors du préflight M11H, aucune donnée contraire courante; avis courant vide.
- Après compose: état v192 / SHA `8a50b6a32dea0f0dc7042093bafc5f06475e883ecebeada0e7a982f2b1f2d5c5`; G2 approuvé/non révoqué v711. Bridge 3 consommés, 0 réservé, 197 restants (aucune consommation pour l’appel M11I); VT 66 consommés, 9934 restants; acquisitions 4 consommées, 996 restantes. Refus 0; CAS incorrects 0; coût fournisseur `UNKNOWN`.
- Tests lancés: 0. Aucun finding, expérience, règle, hypothèse ou publication supplémentaire. Source principale de friction: `opinion.compose` a enregistré un brouillon, mais ne fournit ni texte, ni réponse Bridge, ni consommation Bridge; aucune seconde requête n’a été envoyée.
- Prochaine action permise: nouvelle instruction/opération si un avis rédigé explicitement par Bridge ou le texte sauvegardé du brouillon doit être récupéré. Ne pas rejouer l’identique sans preuve de déduplication et sans lever la limite d’un appel.
