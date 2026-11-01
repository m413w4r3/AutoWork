# Checkpoint final M11D

**État à la rédaction :** boucle CIL générique confirmée sur trois nouveaux PE/CLI; candidate YARA ciblée mesurée sur six fichiers; la consultation Bridge a été refusée avant envoi par `osint_policy_denied`. Aucun avis Bridge n’a été reçu. Aucun comportement d’exécution ni trafic C2 n’a été observé. Aucune attribution de famille n’est posée.

## État canonique et autorisations

- État de travail final : version 185, SHA-256 `30557edbf1dc37716da4f0a2c844ea28c7bb6db1d451bbcccced0406f034319c`.
- G2 à la dernière lecture : version 679, approuvé, non révoqué. `external_llm_allowed=true`; TLP:CLEAR, `do_not_submit=false`.
- Budgets finaux : VT 66/10000, téléchargements 4/1000, Bridge 0/200, outils 438/86400 secondes; aucune réservation. Le refus a confirmé l’absence d’envoi; aucun coût de réponse fournisseur n’est connu.
- Sources : API de l’enquête, fichiers autorisés de la session et analyse locale statique. Aucun échantillon exécuté, aucun C2 contacté, aucune publication.

## Résultats du pivot et triage

Le checkpoint M11C rapportait 80 résultats VT sur trois pages et 50 descripteurs pour la requête exacte sur le corps CIL de 27 octets. Le parcours de l’index canonique a retrouvé 50 hashes distincts dans les lignes `vt_relationship` associées à cette requête. Les 47 rapports VT manquants ont été lus; trois avaient déjà des rapports dans l’enquête. Les 47 nouveaux rapports ont tous renvoyé des métadonnées. **40/47 portent le tag VT `assembly`**; ce tag n’établit pas à lui seul PE/CLI. Les groupes répétés par nom/taille comprennent `CarinaStudio.AppSuite.Proprietary.dll` autour de 12,8 Ko, `Product.Calculators.dll` autour de 18,8 Ko, `NDM.Service.Maintenance.dll` autour de 12,8 Ko et `JFE.Navisworks.Common.dll` autour de 13,8 Ko. Plusieurs sont à 0 détection VT, ce qui n’établit pas qu’ils sont bénins.

Trois candidats ont été acquis statiquement et inspectés :

| Candidat | SHA-256 | VT au rapport lu | Faits locaux vérifiés |
|---|---|---:|---|
| CarinaStudio.AppSuite.Proprietary.dll | `ade84567c9d5edcb87f32f1ff98a225d3ee65f5c8aa26548bb7db652759136a7` | 0/71 | PE/CLI x86, 12 800 octets; `.cctor` initialise 699 octets puis applique la boucle XOR(index)/`0xAA`. |
| GetDataForStocks.dll | `357bb277b7b750fab840204b4f829ba715a1c076a61de9a1aa1a96dd31759a1a` | 0/73 | PE/CLI x86, 24 312 octets; `.cctor` initialise 85 octets puis applique la même boucle. |
| Injector.dll | `186376bcef64b68842b23b78e34857cac2805b663efc47f84119337a391cd9d0` | 35/38 | PE/CLI x86, 24 588 octets; `.cctor` initialise 130 octets puis applique la même boucle. |

Références canoniques d’acquisition/association : Carina `afabd131-4aef-58c5-bb75-952d5f1d09c6` / association `f61b16ef-d855-578a-879b-1aa3ca9a1310`; Stocks `0a1adad5-4d6f-54b5-a8d3-30c4cc99f162` / `6608e23c-1244-59ed-b2f6-79cef8c5d585`; Injector `fa5cfb22-bfbd-5110-9074-ef445c224559` / `b15e2928-6fec-5f9f-84eb-bc98e3a6bb2a`. Les rapports VT sont des observations datées, pas de la vérité terrain.

L’obfuscation par renommage est probable dans les trois méthodes listées (Carina : 35/41 noms; Stocks : 22/36; Injector : 27/28). Le décodeur seul relie ces candidats au niveau d’une routine d’obfuscation; le code observé ne suffit pas à confirmer une relation de famille. Les UserStrings lus pour les six échantillons étaient vides ou non concluants (`strings.list` ne restituait que l’en-tête du tas); les pools FieldRVA et leurs transformations restent la piste utile.

## YARA

### Candidate générique

- Candidate `7f5a2e55-0ab4-4658-b3cc-911588f64c75`, v1, source SHA-256 `768fd89988221a49afbcc1367010c8d228821560badb5d591424a25a4349883e`.
- Mesure explicite `rule.test`, YARA 4.5.0, observation `2794bf6f-8d64-5fe1-9f28-26ba4a92c58b` : correspondance sur A, B, HorseStats et les trois nouveaux candidats (6/6). Le décodeur partagé est donc trop générique pour servir seul de séparateur sur ce corpus.

### Candidate ciblée par taille de pool

- Candidate distincte `6ba2f790-d288-40a7-8fee-220d1c129249`, v1, source SHA-256 `1c8b5bbf32edc25074a805373ab7f038340ed22a9fe813fa9d7bb12c4fa769d1`.
- Sources observées : A alloue 1883 octets (`20 5B 07 00 00` avant `newarr byte`), B alloue 964 (`20 C4 03 00 00`). La règle persistée utilise `condition: 2 of them` sur les trois motifs : la mesure A/B correspond au décodeur plus un marqueur de taille, mais la condition autorise aussi théoriquement les deux marqueurs de taille sans le décodeur. Cette subtilité est une limite de la candidate.
- Mesure explicite `rule.test`, YARA 4.5.0, observation `95dfd9d6-ca68-5c75-88d3-7541adbd40a5` : A et B matchent; HorseStats, Carina, GetDataForStocks et Injector ne matchent pas (2/6 contre 6/6 pour la règle générique). Il s’agit d’une comparaison des rapports de deux candidates distinctes, pas de `rule.compare` entre versions d’un même candidate.
- Statut workbench `candidate` / `real_partial` / `ONE_RULE_NOT_VALIDATED`. Aucun positif confirmé, goodware qualifié, holdout positif ou holdout négatif; sensibilité et taux de faux positifs non calculables. Les résultats distinguent les six objets testés, sans estimer une performance populationnelle. Le rapport note aussi `matched_string_offsets_incomplete:new_candidate_unconfirmed`. La proposition initiale disait `engine_unavailable`; l’exécution explicite de `rule.test` a bien utilisé YARA 4.5.0.
- La révision du candidate générique n’a pas été tentée : le contrat `rule.test` indique qu’une révision exige un faux négatif mesuré sur un positif confirmé, absent ici. La candidate ciblée a donc son propre identifiant/version. Elle reste expérimentale et ne valide aucune famille.

## C2 et Suricata

Faits antérieurs inchangés : A contient du code statique qui construit des lectures GitHub Contents (`DownloadData`) et un PUT de commit (`UploadData`), avec TLS 1.2 configuré; B lit un `.conf`, puis code Base64 → RC4 → UTF-8. Références et limites figurent dans `FINAL_CHECKPOINT_M11C.md`. Aucun paquet ou comportement réseau n’a été observé. Les trois nouvelles variantes n’ont pas révélé ici d’indicateur réseau plus spécifique.

La candidate Suricata TLS/SNI `api.github.com` du checkpoint M11C reste **non testée** et à fort risque de faux positifs. Aucun PCAP n’était associé; aucune compilation/relecture Suricata n’a été exécutée.

## Bridge, findings et suite

- Une seule consultation prose `advisor.consult(yara_draft)` a été soumise sous la requête `m11d-103-bridge-yara`, demandant une critique de spécificité, fragilité et prochains critères pour séparer obfuscateur générique et famille; aucun JSON n’était demandé.
- Invocation `5a5cc8c3-ea7e-4752-8dd6-fe598732fb8d` : `advisor_status=unavailable`, `failure_stage=coordinator_consume_refused`, `failure_reason=osint_policy_denied`, `confirmed_non_execution=true`, `prior_submission_made=false`, `response_state=pre_send_failure`, `response_received=false`, `retry=false`; aucun tour Bridge consommé. Aucun avis n’a été renvoyé et aucune nouvelle consultation ne doit être soumise sur la base de cette réponse.
- La provenance persistée pour l’invocation contient 203 références (1 article, 2 findings, 196 observations, 4 documents source), alors que la demande visait un dossier réduit à cinq observations. Le refus n’identifie pas quelle référence a déclenché la politique; ne pas attribuer une cause plus précise que `osint_policy_denied`.
- Findings et expériences : aucun ajouté. Les observations et mesures de cette session suffisent à garder la qualification prudente.
- Suite permise : corriger, côté orchestrateur, l’assemblage du contexte Bridge pour qu’il respecte réellement le dossier réduit et réévaluer l’autorisation avant toute nouvelle invocation; la réponse actuelle interdit le retry. Comparer d’autres groupes VT de tailles répétées avec le même `.cctor`; inspecter les fonctions d’usage/chargement des pools si un détail de comportement aide à séparer les candidats. Rechercher des positifs indépendants et du goodware qualifié avant toute portée de famille ou performance statistique. Rejouer Suricata seulement si une capture associée apparaît.

## Compteurs et frictions

- Cette session : 48 lectures `vt.file_report` (dont 47 pour les hashes restants et une lecture ciblée supplémentaire), trois acquisitions VT, zéro refus VT/acquisition et zéro CAS incorrect. État final : VT consommé 66, téléchargements 4, Bridge consommé/réservé 0, outils 438 secondes. Refus Bridge avant envoi; coût/tokens fournisseur non applicable ou inconnu.
- Friction reproductible : `strings.list` sur ces assemblies .NET n’a retourné que l’en-tête du tas UserStrings; ne pas confondre cela avec l’absence de chaînes décodées dans FieldRVA. Première progression locale de rapports n’affichait pas les champs métier, car les métadonnées sont dans `output.data.result`; les 47 rapports persistés ont ensuite été agrégés depuis leurs réponses canoniques.
- Le blocage restant est la politique de consultation Bridge : le contexte enregistré dépasse le dossier réduit visé, mais le refus ne détaille pas les références responsables. Aucun autre élément opérationnel n’est en attente.
