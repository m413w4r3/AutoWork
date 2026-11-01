# Checkpoint final — mission M11B

**État : analyse statique documentée, résultats de détection non validés.** Ce checkpoint ne déclare jamais « PASS ».

## Échantillons et décodage des pools

| Échantillon | SHA-256 PE | FieldRVA | Taille | SHA-256 des octets du champ | Observation |
|---|---|---:|---:|---|---|
| A | `0354862d83a61c8e69adc3e65f6e5c921523eff829ef1b169e4f0f143b04091f` | `0x0400000C` | 1883 | `9facb9d3f9ed91bbd88acbf95af88eace47a1912d90048ccda6269e398eb9a42` | `7b7896f3-d2fd-50f1-a2b1-23cbd05394d6` |
| B | `76273382e4252c1f60a2251141e108942494409c759358320735891762c0682e` | `0x04000072` | 964 | `45f4ba9a654fce1e9be2128e20a1f4beb8d5b4c1b4dd003810c127906a3983a1` | `02268f12-16a7-5c70-a167-e48e8af4e0a9` |

Les SHA-256 des plages fichier indépendantes concordent avec les FieldRVA : A à l’offset fichier 10008 (réf. `7f46f854-e314-54c1-90be-659507422bb2`), B à l’offset 8472852 (réf. `4b4a6e7a-b679-5e46-a0c8-8c8bb28c0943`). Les RVA étaient respectivement 17688 et 8480532 ; elles n’ont pas été confondues avec les offsets fichier.

Le CIL des deux initialisateurs applique `decoded[i] = raw[i] XOR (i & 0xff) XOR 0xAA`. A : `.cctor` RID109, observation `a1787d74-3afe-5e6d-80fb-9e8469195d12`, SHA du résultat `717d04c92b7a27930aee3dbbb5a81cf058c2518d0a7e22dbcb0bc74c364f3f8d`. B : `.cctor` RID130, observation `064805fe-ffb5-5ace-a772-e219d28cb65a`. Le décodeur A RID31 traite des tranches UTF-8 et les met en cache (`72e8184c-eaae-5049-b3bf-3a2097b1f4bc`).

## Invariants et modèle C2

### A — API GitHub Contents

Les chaînes et le CIL établissent un chemin de code qui construit `https://api.github.com/repos/{arg0}/{arg1}/contents/{arg2}`, lit le contenu via `WebClient.DownloadData`, extrait les valeurs JSON `sha` et `content`, puis envoie un corps JSON de commit via `UploadData` avec méthode `PUT`. Le corps inclut un message de commit et le contenu encodé ; les en-têtes comprennent `Accept: application/vnd.github.v3+json`, `Content-Type: application/json`, `Authorization: Token {argument}` et un User-Agent Chrome. L’accessor de méthode à l’offset pool 433, longueur 3, a été décodé en `PUT`.

Observations CIL : RID10 `a08a5772-b78f-5895-816d-a8073a78cbc6` (suite `456952e8-c263-5c1e-a465-8e051b85f87e`), RID11 `2a93594e-c6c2-5bd1-a622-7827ebfd49c3` (suite `c9571f84-f1a2-5658-aeef-663a4d6a571e`), appelant RID8 `8dc42b08-bdb8-5006-9ad4-4cd6c3cfb73f`. Le code appelant gère une erreur en modifiant le proxy puis en réessayant.

Le pool contient aussi un marqueur de persistance Run Windows et des chemins `/Info.txt` et `/License.txt`. Une chaîne statique ressemblant à un jeton est présente, mais son passage à l’argument d’autorisation n’a pas été établi. Sa valeur est volontairement omise. Ces éléments décrivent du code observé, pas une requête réseau, une écriture GitHub ou une persistance exécutée.

### B — configuration et chargement

`Blackwood.ConfigReader.GetValue` lit un fichier `.conf` adjacent au binaire, décode Base64, déchiffre avec une implémentation RC4 et transforme les lignes `clé=valeur` en options. Les références principales sont `4b6da160-514f-5e4e-97d2-2920da100455` (RID28), `62e87c4b-8b4a-50ec-9555-afe7127c3e50` (RID29) et `65046492-ffb8-5eb2-89f0-18977f2b8e03` (RID31). La clé statique de configuration n’est pas reproduite.

Les méthodes RC4 observées couvrent l’initialisation/KSA, les permutations et le PRGA/XOR : `3ee05c7c-cf1f-5a73-8607-405e434d8660`, `1f21bd25-38ac-5c8c-909a-bc5a88c10f64`, `a576b750-4572-5cff-bb72-44e4a8ac3627`, `6915377d-71e5-5ac5-a9ab-90259061326e`, `76b631c5-07d9-5de2-97cf-30a34e5bd763`. Le pool mentionne `Blackwood.Cheese.xml`, `Cheese.dll` et des options client/server ; un chargeur PE manuel est présent. Aucune activation du chargeur n’est démontrée. Les pages RID32 examinées n’établissent pas de protocole réseau ; la couverture partielle n’autorise pas à conclure à son absence dans tout l’assemblage.

## Candidats de détection

### YARA

| Candidate | Source SHA-256 | Test | Mesure observée | Conclusion |
|---|---|---|---|---|
| B `3fc996ca-a71d-4ef9-a34b-00b0ded9e464` v1 | `7e87131b3d88a6d9302a0ea536f35afae09405e365f834c0cefe235b8aab9a85` | `49e53c11-b650-5c28-a22b-b70eefd4f5ab` | YARA 4.5.0 : match B, non-match A | candidate expérimentale, non validée |
| A `3ca2e56d-8b8a-4e48-87ea-44d7cc11ce6e` v1 (`a7b4a82c-8de0-4413-8ae6-5540d14a8d8f`) | `678104c86de88ab96f8f0cc6c44869620f337e45fd5ca4b0603125ca94ded8b4` | `0a497b2e-65c9-555e-b6b3-30ae263899b4` | YARA 4.5.0 : match A, non-match B | candidate expérimentale, non validée |

La candidate A utilise 30 octets encodés provenant du pool, à l’offset fichier 10148 ; leur SHA-256 est `803d8677d3c6b61f270153d03248e56b9db17c8897f0ddb66e2199b1d09aba91` (FieldRVA de référence `7b7896f3-d2fd-50f1-a2b1-23cbd05394d6`). La candidate B utilise le motif de 8 octets mesuré à l’offset fichier 24538. Les tests couvrent seulement A et B au rôle `new_candidate_unconfirmed`. Il n’y a ni goodware, ni holdout, ni positif confirmé ; les deux résultats sont `real_partial`, pas une validation de famille. Le contrôle de compilation initial de la proposition A indiquait `engine_unavailable`, mais le test explicite a compilé et mesuré la règle avec YARA 4.5.0.

### Suricata — candidate non éprouvée

```suricata
alert http $HOME_NET any -> $EXTERNAL_NET any (
    msg:"M11B A GitHub Contents PUT candidate";
    flow:established,to_server;
    http.host; content:"api.github.com"; nocase;
    http.uri;
    content:"/repos/"; nocase;
    content:"/contents/"; nocase;
    http.request_method; content:"PUT";
    http.request_header; content:"application/vnd.github.v3+json"; nocase;
    classtype:trojan-activity;
    sid:9901101; rev:1;
)
```

Cette candidate reprend les marqueurs observés statiquement. Elle peut également correspondre à des clients légitimes de l’API GitHub. Aucun PCAP n’était associé ; ni compilation ni replay Suricata n’a été effectué. Ne pas la déployer sans validation locale sur du trafic adapté.

## Pivot et état opératoire

Le pivot VT passif `content:"Blackwood.Cheese.xml"` s’est achevé `empty` : zéro hash observé, invocation `9b610ddf-4388-46d0-ae89-d0d8837dd2a2`, références `2e9e210f-8f7e-5bac-8c5a-c8c2b97a685d` et observation négative `841c696b-133e-5fa9-8dd2-85bdadeb57d5`. Le résultat dépend de la requête et de l’index VT au moment de la collecte. Aucun nouvel échantillon n’a été acquis. Aucun tour Bridge, exécution de malware, contact C2 ou publication n’a eu lieu.

État lu après le pivot : working state version 117, SHA-256 `f3f1dada6f8163dafe4c88dbc680d029b479f1918096fe917863154a3ae2a5c4`. G2 approuvé, non révoqué, version 467. Quotas : VT 7/10000 ; Bridge 0/200 ; téléchargements 0/1000 ; outils 312/86400 secondes. Le TLP n’était pas exposé par l’état compact consulté.

Coûts fournisseur et consommation de tokens du modèle : UNKNOWN, non exposés par les outils consultés.

La demande de revue B est dans `subagent_requests/m11b-b-pool-review.md` ; aucun livrable correspondant n’a été trouvé sous `workers/` au moment de la rédaction. Aucun finding canonique supplémentaire n’a été créé : les faits et leurs limites restent référencés aux observations ci-dessus.

## Suite autorisée et utile

Si une capture réseau associée devient disponible, examiner le flux GET/PUT et éprouver la candidate Suricata. Pour renforcer les règles YARA, ajouter des échantillons indépendants et du goodware mesuré ; les candidates actuelles servent uniquement de motifs par échantillon. Tout futur pivot doit rester passif et utiliser des chaînes non sensibles, avec les contrôles G2/TLP applicables.
