# Checkpoint final M11K — avis Bridge non intégré

## Résultat

La réponse Bridge existante a été récupérée et son texte brut vérifié. L’intégration au brouillon n’a pas abouti : les trois appels valides à `opinion.revise` ont renvoyé `invalid_selection`. Aucun nouvel appel Bridge n’a été effectué. Aucun PASS, approbation ou publication n’est déclaré.

## Provenance Bridge et extraits

- Invocation d’avis : `b7688458-2bfa-49bd-a43e-7fb36ee4d1a5`.
- Blob texte : `bcc51814-27f7-417c-a569-67d6e0d41b67`; SHA-256 brut UTF-8 `944a2505e6d4c2682305443fdd528ed259c1a175856afbf4beef104b6f15dc83`; 3 466 octets. Le SHA normalisé indiqué par la réponse est `0f8c520321827f5d678c306e1ce66aaa24707915f87bc82d2ad2b5e80b864432`.
- Les blocs préparés utilisaient des extraits exacts, `bridge_authored=true`, le `bridge_draft_ref` portant le SHA de chaque texte de bloc, et les références canoniques existantes. Les alias Bridge `[E…]` présents dans les extraits ne remplaçaient pas les `finding_refs` et `evidence_refs` canoniques.
- Alternative locale sans marqueurs Bridge également composée et comparée; elle n’a pas été soumise. Les extraits Bridge préservaient les formulations et réserves originales.
- SHA des extraits : A `98650d98940aa02db06b6b69f47993e88577d589ce00e13620b338b8c53d647f`; B `798b02d17b51f7d1dd915c3f4eebf740cd939b9192d8025ed31466d55edfa3ac`; motif partagé `af46e10576cc3b8e14aa62aa6da770e72153ecf8481748e6604023f7eb32f988`; résultats YARA `f2fd483289b2fbb5674e46bd5c347efa03c6853d622f10fb0a60c9ac4b6b460c`. L’essai final combinait motif + YARA dans le bloc `proposed_detections`, SHA `234cbf1cdde55e00241cb8456f0173b4fb96c7db530612387ff218a4e847c62d`.

## Avis canonique

- Brouillon : `7507f2b3-a4e0-5c32-9ee8-43339b129edc`, toujours v1 / SHA `27d6cda6b970aee6f32779bdef0872144db1e8cb6e40dd31d60e37f6230cc8b5`, statut `draft`.
- Le pointeur final confirme que les trois blocs (`key_findings`, `network_and_infrastructure`, `proposed_detections`) n’ont pas changé. Aucun nouvel avis ou finding n’a été créé.

## Refus et friction

| Requête opérateur | Invocation | Observation de refus | Résultat |
| --- | --- | --- | --- |
| `m11k-023-revise-bridge-excerpts` | `92566c8c-5911-46b5-8f98-f2fa047dcf39` | `14931ac1-1aa4-5807-ba69-cc1a51187545` | `invalid_selection` |
| `m11k-027-revise-existing-blocks` | `f7914aa2-11b4-4fe5-9201-bab3fac1efd0` | `57a5c253-5139-50e3-905c-736e1ca802c8` | `invalid_selection` |
| `m11k-032-revise-preserve-selection` | `6b434ecc-52fb-4eeb-a759-f823d19d4e81` | `8fe12e17-e94d-57b3-bbce-c315a8ec7321` | `invalid_selection` |

Pour les trois refus, le corps ne fournit pas de champ `output.detail`. Chaque observation persistée contient seulement `result.error_code=invalid_selection`; la raison générique est : “The tool could not complete the request. Review this error code and current investigation context, correct the inputs, then retry.” Les deux premiers changements de blocs n’ont pas résolu le code. Le troisième conservait les types de blocs d’origine et la sélection complète des références du bloc `key_findings`; il a également échoué. La prochaine étape nécessite un diagnostic du contrat local de sélection avant toute autre mutation.

Une requête `m11k-030` n’a pas atteint l’outil : HTTP 422 a signalé un CAS `expected_sha256` mal formé (65 caractères). La lecture suivante a confirmé le pointeur v198 et fourni le SHA valide; cette erreur de requête n’est pas un refus d’outil.

## État et compteurs

- État initial : v196 / `b361e6aa513e8f02f32b246881a23e29c056dee099dad47231ea770596d65cda`.
- État final : v199 / `1759171f3745ff49ed10d5c9b24ce2882d239c061863e42af2ea71ba1eb86fd3`.
- G2 : approuvé, non révoqué, version 731 au dernier GET.
- Appels Bridge dans M11K : 0; VT : 0 appels / 0 unités; refus d’outil : 3; CAS incorrects : 1 requête locale HTTP 422; tests : 0.
- Budgets finaux : Bridge 196 restants; VT 9 934 unités restantes; acquisitions 996 restantes. Usage fournisseur/coût : `UNKNOWN`.

## Prochaine action permise

Faire diagnostiquer le code `invalid_selection` par rapport au contrat courant de `opinion.revise`. Après correction documentée de la sélection, réviser le même brouillon avec les extraits vérifiés, sans nouvel appel Bridge; garder le statut `draft`.
