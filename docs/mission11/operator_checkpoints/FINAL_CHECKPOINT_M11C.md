# Checkpoint final — mission M11C

**État : analyses statiques persistées; candidate YARA expérimentale; Suricata non testée; consultation Bridge refusée avant transport.** Aucune activité d’exécution, de contact C2 ou de publication n’a eu lieu.

## Objet et invariants observés

| Échantillon | SHA-256 | Invariant pertinent | Références canoniques |
|---|---|---|---|
| A | `0354862d83a61c8e69adc3e65f6e5c921523eff829ef1b169e4f0f143b04091f` | FieldRVA token `0x0400000C`, 1883 octets; `.cctor` décode `raw[i] XOR (i & 0xff) XOR 0xAA`. | `.cctor` `a1787d74-3afe-5e6d-80fb-9e8469195d12`; pool `7b7896f3-d2fd-50f1-a2b1-23cbd05394d6` |
| B | `76273382e4252c1f60a2251141e108942494409c759358320735891762c0682e` | FieldRVA token `0x04000072`, 964 octets; même boucle CIL XOR/index/`0xAA`. | `.cctor` `064805fe-ffb5-5ace-a772-e219d28cb65a`; pool `02268f12-16a7-5c70-a167-e48e8af4e0a9` |
| HorseStats.dll | `87d3dcb12a68eabbfe852fcb384e4dcf4fc0b109a79a8ecdbfc7cc55b1d65c04` | PE/.NET x86, 8192 octets; `.cctor` contient le même schéma de décodage. | Inspection `408f6c6e-ac37-54c2-a4d6-dda6cdd3b41f`; `.cctor` `824b9f0c-1352-57b9-9e76-eb8248d084e8` |

Pour le motif de 27 octets, les métadonnées diffèrent : A à l’offset fichier 9965 (`0a62e83b-74bf-5951-914a-3522d17739ab`), B à l’offset 12829 (`63dbbe67-4492-5f26-b37d-68514605016f`). Les quatre octets de chaque token de champ sont donc wildcardés; les autres octets correspondent au CIL mesuré.

## YARA

Candidate `M11_Shared_FieldRVA_Decoder`, v1, candidate ID `7f5a2e55-0ab4-4658-b3cc-911588f64c75`, detection-candidate version/object ID `331582b1-1aa9-4ada-b5c2-816f4c5de456`, source SHA-256 `768fd89988221a49afbcc1367010c8d228821560badb5d591424a25a4349883e`.

```yara
rule M11_Shared_FieldRVA_Decoder {
  strings:
    $decoder = { 7E ?? ?? ?? ?? 06 7E ?? ?? ?? ?? 06 91 06 61 20 AA 00 00 00 61 D2 9C 06 17 58 0A }
  condition:
    $decoder
}
```

YARA 4.5.0 a compilé/mesuré la candidate : A et B correspondent dans l’observation `21766413-9d5f-5612-8e56-e40cec6c98a9`; HorseStats.dll correspond dans `bfda65f0-553e-5d33-b461-e54daaf60eac`. Il s’agit d’une candidate à motif de décodeur commun, pas d’une signature validée de famille. Les trois fichiers restent `new_candidate_unconfirmed`; le rapport VT de HorseStats.dll indiquait 0 détection et 72 undetected, ce qui ne confirme pas son innocuité. Aucun bon logiciel confirmé, positif humainement confirmé, holdout, faux négatif mesuré, taux de faux positifs ou sensibilité n’est disponible. Le statut outil reste `ONE_RULE_NOT_VALIDATED` / `real_partial`.

La vérification de compilation initiale à la proposition indiquait `engine_unavailable`; l’appel explicite `rule.test` a ensuite utilisé YARA 4.5.0. Une tentative de révision a été refusée avec `rule_revision_not_justified` en l’absence de faux négatif mesuré. Aucune révision supplémentaire n’a été faite.

Finding de détection final : `104b8300-0fd0-5e5b-ac78-54bb3da7301a` v2, assessment `supported` pour le fait mesuré des correspondances, avec limites et statut expérimental dans le finding.

## C2 statique et fiche protocole

### A — GitHub Contents

Le CIL statique établit un chemin helper HTTPS vers `api.github.com` : `DownloadData` lit un objet Contents et extrait `sha`/`content`; un helper distinct construit un corps de commit et utilise `UploadData` avec `PUT`. TLS 1.2 est configuré (`SecurityProtocol=3072`). Références : RID10 `a08a5772-b78f-5895-816d-a8073a78cbc6` et `456952e8-c263-5c1e-a465-8e051b85f87e`; RID11 `2a93594e-c6c2-5bd1-a622-7827ebfd49c3` et `c9571f84-f1a2-5658-aeef-663a4d6a571e`; appelant `8dc42b08-bdb8-5006-9ad4-4cd6c3cfb73f`; TLS `a3b992bb-1261-52b3-b9c0-ae6e93896ff6`.

Les segments `/Info.txt` et `/License.txt`, la branche `main` et l’URL paramétrée sont des chaînes/valeurs observées statiquement; des paramètres du helper restent dynamiques. L’origine d’un argument d’en-tête n’est pas établie. Aucun paquet ne démontre une requête, un commit réussi ou une exécution.

Finding final `a68f34f1-3d71-5019-9d39-886a1ee4ad1c` v2, assessment `supported`, scope statique méthode par méthode; il complète le finding A existant `9f0e7e4a-ba0f-5543-9bcc-878552290b40` sans affirmer de trafic observé.

### B — lecteur de configuration

RID28 lit un fichier `.conf`, effectue Base64 → `Blackwood.RC4.RC4Decrypt` → UTF-8; RID29 sépare les lignes en paires clé/valeur. Références RID28 `4b6da160-514f-5e4e-97d2-2920da100455`, RID29 `62e87c4b-8b4a-50ec-9555-afe7127c3e50`, RC4 `3ee05c7c-cf1f-5a73-8607-405e434d8660`. Aucune valeur déchiffrée, clé, utilisation effective ou activité réseau n’est affirmée.

Finding final `5369ee6b-8152-5499-8a93-dd45f9234d43` v1, assessment `supported`.

### Carte protocole structurée

Carte `documented_not_observed`, invocation `cdd537f4-f80a-49e8-899a-334b6aea457d`, observation `d52f0840-4a01-5dbb-a9c2-71217bf5ae27`, model blob `3d414d99-091f-42c0-9e3c-b9313f12b92f`, modèle SHA-256 `ac0615fc51364345990a7835d57276d5789b05315c24bbd534f9ff720dda1117`. Séquence décrite : requête GET Contents, réponse JSON avec `sha`/`content`, puis requête PUT de commit et réponse texte/UTF-8. Quatre messages, trois états, huit champs, deux transitions, quatre caractéristiques et deux sources de fonctions statiques sont persistés. Le diagramme a été rendu par `network.protocol.render`, invocation `787babc6-f560-4d51-bf3c-23c60a462ea5`, observation `392c099b-8f6d-51d1-8dc9-3a9582ebac9f`.

C’est un modèle dérivé du code, pas une reconstruction de paquets : aucun PCAP n’est associé, aucune transaction API n’a été observée, et l’origine d’un argument d’en-tête reste inconnue.

## Suricata — candidate TLS corrigée

```suricata
alert tls $HOME_NET any -> $EXTERNAL_NET any (
    msg:"M11 A outbound GitHub API SNI candidate";
    flow:established,to_server;
    tls.sni; content:"api.github.com"; nocase;
    classtype:trojan-activity;
    sid:9901101; rev:2;
)
```

Cette candidate remplace l’ancienne candidate `alert http` inadaptée au transport TLS. Elle signale le SNI du nom d’hôte, pas le chemin Contents, la méthode PUT ou une activité malveillante. SNI est inféré du hostname configuré; aucun PCAP ne permet de confirmer le champ en trafic. Aucune compilation ni replay Suricata n’a été exécuté; les outils de validation/replay exigent une capture. Le risque de faux positifs est élevé sur l’usage légitime de GitHub.

## Pivot VT et nouvel échantillon

Le pivot passif sur la séquence CIL exacte d’A (`content:{7e0d000004067e0d0000040691066120aa00000061d29c0617580a}`) a retourné 80 résultats sur 3 pages, pagination complète, 50 descripteurs, invocation `10ec8995-f103-4a79-b123-614b019a325a`. Ce n’est pas un retrohunt YARA; les descripteurs n’établissent pas une famille.

Le rapport passif `HorseStats.dll` indiquait PE/DLL, 8192 octets, 0 détection et 72 undetected. L’échantillon a été acquis sans exécution, sample ID `7e733bf2-28cc-47db-9a4a-1b3e0d8a6442`, association `226dbbb9-69a0-570d-9663-7532e395f10d`, observation d’acquisition `7aa1e1ec-aa98-51b8-a64b-3e8a4d1f6d7b`. L’inspection confirme PE/CLI managé. Il demeure `new_candidate_unconfirmed`, sans attribution ni qualification bénigne. Le rapport VT est une observation à la date de collecte, pas une preuve d’innocuité.

Le pivot passif `content:"HTTPApi.dll" AND content:"MicrosoftWebApi"` est revenu vide (invocation `033985ea-207a-4d2f-a85c-6775492beae7`); cela ne réfute que cette requête et son index.

## Findings M11C persistés

| ID final | Type / assessment | Assertion bornée |
|---|---|---|
| `a68f34f1-3d71-5019-9d39-886a1ee4ad1c` v2 | `network_behaviour` / `supported` | Helper GitHub Contents GET/PUT configuré TLS 1.2, code statique A seulement. |
| `5369ee6b-8152-5499-8a93-dd45f9234d43` v1 | `capability` / `supported` | Lecture et transformation Base64/RC4/UTF-8 de configuration B, code statique. |
| `104b8300-0fd0-5e5b-ac78-54bb3da7301a` v2 | `detection` / `supported` | Candidate YARA v1 correspond au motif de décodeur commun mesuré sur A, B et HorseStats.dll; limites d’échantillon et de validation conservées. |

Les subject_ref finaux sont alignés sur les `object_id` canoniques renvoyés par `evidence.get`; les révisions ont retiré les rétrogradations automatiques dues à un hash/sujet non correspondant.

## Bridge, politique et frictions

L’état G2 relu avant partage et à la clôture était approuvé, non révoqué, TLP:CLEAR, `external_llm_allowed=true`, fournisseur Bridge autorisé. La première consultation `m11c-072-bridge-yara` a échoué localement `invalid_tool_arguments` à cause du champ `rule_source_sha256` hors schéma; aucun tour Bridge n’a été consommé. La seconde, corrigée et prose uniquement (`m11c-077-bridge-yara`, invocation `a4e71a39-e8d5-4813-a8ec-ba7e4d22d815`), s’est terminée `advisor_unavailable`, stage `coordinator_consume_refused`, raison `osint_policy_denied`. Le résultat persistant confirme `confirmed_non_execution=true`, `prior_submission_made=false`, `response_received=false`, `bridge_turns_consumed=0`. Aucun élément CTI n’a été envoyé; aucun avis Bridge n’a été reçu. Aucune nouvelle tentative n’a été faite contre ce refus de politique.

Le préflight du dossier était `READY`, mais le paquet faisait 12,927,828 octets : 170 éléments requis, 419 présents, 314 omis, 0 manquant, 0 envoyé. Identifiants de calcul conservés dans l’outbox; le prompt n’a pas été transmis. L’usage fournisseur est `usage_unavailable`. Le budget status final indique 348 secondes outil consommées, alors que le résultat de consultation rapporte 470 secondes et 3,232,437 tokens de contexte consommés; cet écart n’a pas été réconcilié et aucune mesure de coût fournisseur n’est revendiquée.

Autres frictions : GET 409 `stale_working_state` corrigé en utilisant le SHA de working state; `vt.file_report` 422 pour version attendue omise, réponse persistée et nouvel ID unique ensuite; première proposition de fiche réseau 422 `operator_payload_invalid_or_sensitive`, corrigée en retirant le nom d’en-tête sensible du modèle; B RID31 404; proposition de révision YARA refusée faute de faux négatif mesuré; les deux premières propositions de findings ont été automatiquement rétrogradées faute d’un subject_ref canonique, puis corrigées avec `evidence.get` et révisées.

## État opératoire final

- Working state : version 132, SHA-256 `0cfc9e7259dd3faf273557a5b71a396b71f84c438860ce52c71663e66537e0e3`.
- G2 : version 537, approuvé, non révoqué; TLP:CLEAR; `external_llm_allowed=true`; aucun job actif.
- Budget status : VT 15/10000, Bridge 0/200, téléchargements 1/1000, Shodan 0/1000, outils 348/86400 secondes; aucune réservation.
- Fournisseur/tokens/coûts : `UNKNOWN` / usage Bridge indisponible; l’écart de métriques ci-dessus reste ouvert.
- Aucune exécution de malware, aucun contact C2, aucune publication ou release.

## Suite autorisée

Réexaminer la policy coordinator qui a produit `osint_policy_denied` avant toute nouvelle consultation Bridge. Pour la candidate YARA, rechercher des positifs indépendants, du bon logiciel et un holdout avant d’interpréter sa portée; le motif partagé seul est générique à ce stade. Éprouver la candidate Suricata seulement si une capture réseau adaptée devient disponible.
