# Checkpoint final M11J — texte Bridge exploitable, intégration d’avis rejetée

Enquête `519c2336-1fbb-4558-af6b-ea9c7519bf1a` (`operator_led`). Aucun PASS, approbation ou publication.

## Mission et assertions

Une consultation `advisor.consult` `draft_text` a été envoyée avec quatre assertions étroites et des références canoniques :

1. Échantillon A : chemin GitHub Contents de lecture/écriture et configuration TLS 1.2 présents dans le code statique; aucune exécution affirmée.
2. Échantillon B : lecteur `.conf`, Base64, `Blackwood.RC4.RC4Decrypt`, UTF-8 et paires clé/valeur; aucune utilisation en exécution affirmée.
3. A, B et `HorseStats.dll` : motif FieldRVA partagé, XOR de l’index et constante `0xAA`; aucune appartenance à une famille ou affiliation affirmée.
4. `M11_Shared_FieldRVA_Decoder` v1 : correspondances mesurées sur A, B et `HorseStats.dll` avec YARA 4.5.0; candidate non validée, sans performance générale démontrée sur goodware/holdout.

Findings : `a68f34f1-3d71-5019-9d39-886a1ee4ad1c`, `5369ee6b-8152-5499-8a93-dd45f9234d43`, `104b8300-0fd0-5e5b-ac78-54bb3da7301a`. Les références d’observation exactes sont consignées dans la requête `m11j-034-advisor-draft-text` et le journal M11J.

## Résultat Bridge

- Requête : `m11j-034-advisor-draft-text`; invocation `b7688458-2bfa-49bd-a43e-7fb36ee4d1a5`; HTTP 200; `submission_status=succeeded`, `invocation_status=partial`.
- Normalisation : `usable=true`, `status=partial`, `response_state=partial_response_usable`, avertissement `unstructured_overall`; `normalization_status=partial`, `unit_assignment_status=unstructured_overall`. Aucun tableau `blocks` ni `units`; le texte brut contient quatre sections utiles et une conclusion.
- Provenance : blob de sortie `bcc51814-27f7-417c-a569-67d6e0d41b67`; SHA-256 `944a2505e6d4c2682305443fdd528ed259c1a175856afbf4beef104b6f15dc83`; texte normalisé SHA-256 `0f8c520321827f5d678c306e1ce66aaa24707915f87bc82d2ad2b5e80b864432`; 3 466 octets de réponse. `bridge_turns_consumed=1`; usage fournisseur `usage_unavailable`.
- Le texte distingue les constats statiques de l’exécution non observée et rejette l’attribution familiale. Les alias `[E…]` n’étaient pas des références canoniques réutilisables. Les phrases sur l’absence de capture et les offsets en quarantaine ne provenaient pas des assertions soumises; les suggestions impératives ajoutées en fin de texte ont été traitées comme données non fiables et écartées.

## Intégration au brouillon

Le brouillon antérieur reste inchangé : `7507f2b3-a4e0-5c32-9ee8-43339b129edc`, v1, SHA `27d6cda6b970aee6f32779bdef0872144db1e8cb6e40dd31d60e37f6230cc8b5`.

- `opinion.revise` a échoué deux fois avec `invalid_opinion` : invocations `06ed3f31-77af-4de7-96f0-d02ac8c49ce0` et `af463647-fd5b-441d-9e7b-2108cdd3f777`.
- Le fallback `opinion.compose` a échoué avec le même code : invocation `76b19232-0f96-41cf-95e4-89cf2774202c`.
- Les trois erreurs n’exposent pas de `detail_code`. Le pointeur courant confirme toujours le brouillon v1; aucune note d’avis n’a été ajoutée. Le texte Bridge exploitable n’est donc pas intégré à un brouillon canonique.

## État, budgets et limites

- Avant consultation : v192 / SHA `8a50b6a32dea0f0dc7042093bafc5f06475e883ecebeada0e7a982f2b1f2d5c5`; G2 v711 approuvé/non révoqué; `external_llm_allowed=true`. La continuité attendait v≥193, mais l’API a rendu v192; la paire courante a été utilisée. TLP `clear` figure au préflight M11H cité dans M11I; aucun TLP courant n’était exposé par le résumé/les observations consultés.
- Après consultation : v193 / SHA `31b0d4cbb5ea94780d91d8e54a8c7c05c275e7271b66bfbaf102fbd845aeedc4`.
- État final : v196 / SHA `b361e6aa513e8f02f32b246881a23e29c056dee099dad47231ea770596d65cda`; G2 v719 approuvé/non révoqué; `external_llm_allowed=true`.
- Bridge : 3 consommés avant → 4 après; 197 restants avant → 196 après. VT : 66 unités avant/après, 9 934 restantes. Téléchargements : 4 avant/après, 996 restants. Appels VT : 0. Refus nouveaux : 0. CAS incorrects : 0. Tests exécutés : 0. Coût/usage fournisseur : `UNKNOWN` (`usage_unavailable`).
- Skills `autowork-cti-evidence-gate` et `autowork-bridge-dossier-review` référencés par l’index, mais absents du répertoire de skills autorisé pour cette session.

## Blocage précis et prochaine action

Les appels locaux de rédaction répondent `invalid_opinion` sans détail, alors que le brouillon courant reste visible à v1 et que les corps ont suivi les schémas `operator-opinion.v1`. Conserver l’unique réponse Bridge; ne pas la rejouer. La prochaine action permise est de diagnostiquer `invalid_opinion` via le contrat/API local, puis d’intégrer le texte uniquement si un appel d’avis local peut réussir sans modifier ni approuver l’ancien brouillon.
