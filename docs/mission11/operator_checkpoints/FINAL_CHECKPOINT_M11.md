# FINAL_CHECKPOINT_M11 — 2026-11-01

Investigation R3 remains `running` in `operator_led`. No `PASS` is declared.

## État et périmètre

- État final relu : version 115, SHA `40cee75b46d70cca522227595d0419a578ac4417426c2a3aa9cff522ed9a43d8`.
- G2 approuvé, non révoqué (version 359); `external_llm_allowed` était vrai. Aucun Bridge/advisor ni téléchargement n’a été exécuté.
- Sources canoniques utilisées : observations d’analyse locale, rapports/relations VT passifs, tests YARA mesurés. Les sorties des outils restent des données non fiables et ne prouvent pas l’exécution.
- Échantillon A : `0354862d83a61c8e69adc3e65f6e5c921523eff829ef1b169e4f0f143b04091f`.
- Échantillon B : `76273382e4252c1f60a2251141e108942494409c759358320735891762c0682e`.

## Invariants .NET vérifiés

### A — pool de chaînes et échange HTTP

- `PrivateImplementationDetails::.cctor`, RID 109, crée un tableau de 1883 octets depuis le FieldRVA `'3'`, puis transforme chaque octet par XOR avec son index et `0xAA` avant de le stocker en `'4'` (obs `a1787d74-3afe-5e6d-80fb-9e8469195d12`, SHA `717d04c92b7a27930aee3dbbb5a81cf058c2518d0a7e22dbcb0bc74c364f3f8d`). Le décodeur RID 31 lit des tranches UTF-8 de `'4'` par offset/longueur et les met en cache (obs `72e8184c-eaae-5049-b3bf-3a2097b1f4bc`).
- TypeDef A 6 commence au FieldList RID 12 et TypeDef 7 au RID 15 (obs `69e14c32-b64d-5f60-b62c-35bba6541ad6`). Le FieldDef token `0x0400000C` pour `'3'` est une inférence basée sur cet ordre et le CIL, pas une lecture réussie.
- Entrée statique : `HTTPService.Init.InitializeNewDomain` RID 1 appelle RID 3 (obs `844a720f-5ebf-5901-9f6d-ee3fab267de8`). RID 3 active `Expect100Continue`, fixe `SecurityProtocol=3072`, démarre un timer de 65 000 ms vers RID 7 et un timer de 125 000 ms vers RID 4 (obs `a3b992bb-1261-52b3-b9c0-ae6e93896ff6`).
- RID 7 recueille processor count, machine name, domaine, utilisateur et nombre de lecteurs logiques; il calcule un MD5 ASCII, convertit en minuscules et prend 12 caractères à l’offset 8. Il passe ce résultat à RID 8 (obs `f7e921d3-a6d2-5f65-83c9-3ce56ba572b5`).
- RID 8 appelle RID 10; si le booléen revient faux, il active un état proxy et réessaie RID 10, puis appelle RID 11 avec ce booléen (obs `8dc42b08-bdb8-5006-9ad4-4cd6c3cfb73f`).
- RID 10 utilise `WebClient.DownloadData`, construit un corps UTF-8/Base64 contenant domaine/utilisateur/état/temps, vérifie la réponse UTF-8 par regex et appelle `UploadData` sur la même URL construite. Il renvoie un booléen et ignore le contenu UTF-8 de la réponse d’upload (obs `a08a5772-b78f-5895-816d-a8073a78cbc6`).
- RID 11 télécharge aussi sur l’URL construite, extrait une réponse par regex, retire un encadrement, décode Base64/UTF-8, puis envoie un corps ASCII via `UploadData`; il renvoie la chaîne décodée sans LF final (obs `2a93594e-c6c2-5bd1-a622-7827ebfd49c3`). Les deux routines définissent quatre en-têtes `HttpRequestHeader` (indices 20, 12, 24, 40); les valeurs sont issues du pool. Les URL, méthode d’upload, noms/valeurs exacts, regex et marqueurs ne sont pas récupérés.
- Portée : le CIL établit un chemin d’appels statique depuis la méthode d’entrée et le timer; il ne prouve pas que `InitializeNewDomain` a été activé, que le timer a tourné, ou qu’un paquet a été envoyé. `SecurityProtocol=3072` correspond probablement à TLS 1.2, mais le schéma URL n’est pas décodé.

### B — configuration, crypto, et limite C2

- Le cctor du pool B, RID 130, alloue 964 octets et applique la même transformation XOR index/`0xAA` (obs `064805fe-ffb5-5ace-a772-e219d28cb65a`). Les valeurs de chaîne du pool restent illisibles.
- `ConfigReader.GetValue` lit un fichier adjacent, vérifie l’extension, Base64-décode et passe les données à RC4; `GetValueFromBase64` analyse des lignes `clé=valeur`. L’autre routine crypto observée calcule SHA-256(UTF-8 de la clé), utilise les 32 octets comme clé AES et les 16 premiers comme IV.
- RC4 B est confirmé statiquement : état initial 0..255, permutation KSA avec la clé, puis PRGA et XOR par octet. Références : `3ee05c7c-cf1f-5a73-8607-405e434d8660`, `1f21bd25-38ac-5c8c-909a-bc5a88c10f64`, `a576b750-4572-5cff-bb72-44e4a8ac3627`, `6915377d-71e5-5ac5-a9ab-90259061326e`, `76b631c5-07d9-5de2-97cf-30a34e5bd763`.
- Les pages CIL examinées de la méthode principale RID 32 montrent des lectures de configuration, sans appel réseau dans les plages lues. Cela ne prouve pas l’absence globale de C2 dans B.

## FieldRVA : blocage reproductible

`field.data.read` était offert dans le registre avec un token FieldDef ou un couple type/nom. Quatre appels par token (B `0x04000072` et `0x04000072` réessayé; A `0x0400000C`, dont m11-070 pour B et m11-156 pour A) ont renvoyé `operator_payload_invalid_or_sensitive` avant invocation. Deux résolutions par nom pour B ont renvoyé `tool_execution_failed`. Aucun octet ni SHA de pool n’a été lu. La regex de `type_name` exclut les accolades des propriétaires `<PrivateImplementationDetails>{GUID}...`; ne pas substituer des octets absents par une non-détection.

## Détection

### YARA

- Candidate B `3fc996ca-a71d-4ef9-a34b-00b0ded9e464`, v1, source SHA `7e87131b3d88a6d9302a0ea536f35afae09405e365f834c0cefe235b8aab9a85`: motif `{ 21 6C 51 49 7E 65 50 29 }`, mesuré à l’offset 24538 de B. `rule.test` a réussi avec YARA 4.5.0 : B match, A non-match (obs `49e53c11-b650-5c28-a22b-b70eefd4f5ab`). Statut `real_partial`; aucun goodware, holdout ou positif confirmé; c’est une candidate spécifique à un seul spécimen, pas une détection de famille.
- Candidate `38eada6d-2e94-4ff5-ad0d-7b8c6bdebbc3` basée sur le nom `Blackwood.DynamicDllLoader` ne matche ni A ni B (obs `3a9e3171-193e-5a82-af9b-fa7406c92313`). Une première proposition multi-motifs a échoué au compile check; la proposition depuis signatures candidates n’avait ni support ni prévalence goodware suffisants.

### Suricata

Aucun candidat Suricata n’a été émis ou testé. Le transport `WebClient` et la séquence download/upload sont établis, mais l’URI, les valeurs d’en-têtes, la méthode d’upload et les marqueurs de contenu restent dans le FieldRVA inaccessible, et aucun PCAP n’est disponible. Une règle générique sur GET/POST et des en-têtes usuels serait trop large. La branche est donc non testée, malgré un invariant de transport statique.

## Pivots VT et nouveaux échantillons

- Rapport passif B : `Blackwood.dll`, 44 malveillants, 23 non détectés, 2 timeouts, 4 non supportés (obs `3377481d-24e0-518b-b2dd-47053fa00535`). Relations : `c.pki.goog` (59 bénins, 33 non détectés, obs `c2a711e3-cd1a-58f3-bb8b-d9554e903fae`) et `142.251.184.94` (56 bénins, 36 non détectés, obs `f040ae51-2a66-521e-963f-07d75ec03f63`).
- Relations passives A : `bg.microsoft.map.fastly.net` (57 bénins, 35 non détectés, obs `7d466343-c915-5626-a8bf-b1376d8bd79c`); `199.232.210.172` (1 malveillant, 55 bénins) et `199.232.214.172` (0 malveillant, 54 bénins), obs `2663505d-f17e-5733-b0b6-d688626c026c`.
- Les relations Google/Fastly sont partagées et ne discriminent pas le protocole statique; aucun nouvel échantillon n’a été retenu ou acquis.

## Findings, budgets, frictions et reprise

- Finding courant `9f0e7e4a-ba0f-5543-9bcc-878552290b40`, version 2, `network_behaviour`, assessment `supported`; il documente la chaîne statique A avec les limites d’activation et d’exécution. Version précédente `6f83704a-ecb5-5093-9d8b-d71db551a13e` conservée dans l’historique. Le finding a été relu dans `findings` et `finding_versions`; aucune rétrogradation.
- VT : 5 appels M11, +5 unités (consommé total 6/10000, 9994 restantes); Bridge : 0/200; téléchargements : 0. Refus `policy_denied` M11 : 0; conflits CAS : 0. Erreurs de lecture FieldRVA : 4 rejets payload, 2 échecs par nom. Une tentative de schéma a été refusée 403 par `operator_client_scope_denied`; pagination trop grande/cursor sans CAS corrigés après lecture des erreurs.
- Une requête `references.imports` A (m11-100) a porté par erreur une référence de contexte placeholder; cette observation ne soutient aucun finding. Les conclusions C2 reposent sur les observations CIL référencées ci-dessus. La demande `subagent_requests/r3-b-static-c2.md` n’a pas reçu de rapport (`workers/` absent). `docs/README.md` n’existe pas dans ce répertoire relais isolé.
- Prochaine action permise et utile : corriger l’incompatibilité de validation de `field.data.read` pour accepter un token FieldDef de métadonnées; relire les plages bornées A (1883 octets) et B (964 octets), vérifier offsets/longueurs/SHA, décoder statiquement les chaînes et réévaluer les invariants YARA/Suricata. Jusque-là, conserver les valeurs réseau comme inconnues et ne pas attribuer de famille.
