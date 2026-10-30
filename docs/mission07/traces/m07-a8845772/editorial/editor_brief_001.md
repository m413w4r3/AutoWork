# Brief éditorial — opinion 128f991d, v6

Périmètre : proposition de texte pour l’opinion, sans modification des faits canoniques. Les marqueurs `[F:…]`, `[O:…]` et `[D:…]` ci-dessous sont des repères de traçabilité pour l’analyste : les mapper aux `finding_refs`/`evidence_refs` et aux notes de source du rendu, puis les retirer du texte public s’ils seraient imprimés littéralement. Les valeurs `certainty` de v6 ne sont pas une échelle calibrée de confiance analytique.

## A. Écarts v6 / références, par priorité

1. **Hiérarchie de lecture.** v6 expose cinq blocs denses, sans page de synthèse ni jugement principal immédiatement lisible. Le bulletin CN2609 ouvre sur un sommaire, une « vue d’ensemble » thématique puis une synthèse avant les sections techniques. Pour M07, placer d’emblée le résultat matériel vérifié, sa portée et les limites qui empêchent d’en déduire exécution ou attribution.
2. **Provenance et niveau de preuve.** v6 formule plusieurs réserves pertinentes, mais les constats statiques, les associations passives VT et les affirmations reprises de Unit 42 restent dans de longs paragraphes. Les séparer explicitement en « constat statique », « association de source », « affirmation rapportée » et « déduction ». Le PDF de référence rattache les affirmations aux notes de source et utilise des notes d’analyste pour expliquer les limites et les divergences.
3. **Matériel et IOC.** v6 donne quelques noms, hashes et relations en prose, mais pas d’inventaire tabulaire compact. Le bulletin regroupe ses IOC par type et utilise aussi des tableaux dans le corps. Pour M07, ajouter un tableau de fichiers avec hash SHA-256 complet, taille, chemin/nom, relation à l’archive et provenance. Séparer les deux DLL identiques aux samples standalone des autres membres d’inventaire; aucun IOC réseau n’est établi ici.
4. **Chaîne et profondeur technique.** Les éléments disponibles justifient une explication des capacités statiques, pas une chaîne d’infection séquencée. Le bulletin CN2609 alterne synthèse, sections techniques, figures légendées, tableaux et notes. Une petite figure d’appartenance de fichiers est possible; ne pas relier PowerShell, transport HTTP, AES et GitHub Issues par des flèches d’exécution.
5. **Confiance et attribution.** Employer des qualificatifs attachés à chaque proposition, plutôt qu’une confiance globale. Les nombres `0.78–0.90` de v6 ne doivent pas être rendus comme une probabilité de vérité. Réserver « étayé » aux deux associations VT passives entre hashes documentés et noms de fichiers; cela ne valide ni l’exécution ni l’attribution.
6. **Décision opérationnelle.** v6 propose des corrélations utiles, mais aucune règle ni limite de détection. Ajouter une chasse locale candidate, étiquetée non validée, et préciser qu’aucun domaine, IP ou URL de C2 n’est confirmé. Ne pas transformer des méthodes génériques en signature de détection.

**Références comparées.** Le PDF `docs/references/CN2609_typ/20261005_NP_TLP WHITE_ChapsVision_Bulletin-APT-CN-2609.pdf` compte 120 pages. Il présente un sommaire, des résumés, des sections à niveaux de titres cohérents, des figures légendées, des tableaux, des notes d’analyste et des références cliquables en notes de bas de page; les IOC sont groupés par IP, domaine, URL et fichier. La source Typst CN2609 règle A4, Hanken Grotesk, titres colorés, en-têtes/pieds de page, notes, liens, tableaux et listes d’IOC. `docs/references/chpTypst` fournit un exemple de composition distinct. La comparaison de fichiers montre que le runtime `/home/nill/perso/work/AutoWork/chpTypst` reprend son squelette et ses modèles, mais ajoute `RENDERER/`, `CascadiaCode/`, les manifestes et `UTILS/document_style.typ`, et diffère aussi dans `main.typ`, `UTILS/helpers.typ`, `UTILS/colors.typ` et `UTILS/header_footer.typ`. Les manifestes runtime déclarent les templates `chp-article-v33-literal-analyst-notes` et `chp-edition-v25-c8-spacing-compat`; traiter `chpTypst` comme la base d’exécution évoluée, pas comme une copie byte à byte du dossier de référence. Cette comparaison porte sur la forme éditoriale uniquement et ne transfère aucun contenu du bulletin au cas M07.

## B. Plan de document compatible avec les blocs

| Ordre | Bloc | Rôle éditorial | À écrire / générer |
|---|---|---|---|
| 1 | `key_findings` | Résultat majeur, portée et jugement global | Texte par l’analyste; encadré de synthèse rendu par le template. |
| 2 | `implant_materials` | Tableau d’appartenance, hashes, tailles et provenance | Données/relations par l’analyste; tableau et typographie monospace par le rendu. |
| 3 | `infection_chain_and_capabilities` | Capacités statiques séparées, sans ordre d’exécution supposé | Texte par l’analyste; figure facultative d’appartenance, pas de chaîne d’exécution. |
| 4 | `additional_samples` | Membres secondaires et statut non confirmé du `.csproj` | Texte par l’analyste; renvoi au tableau de matériel. |
| 5 | `network_and_infrastructure` | État des observables réseau et absence d’IOC réseau confirmé | Texte par l’analyste; ne pas inventer de tableau réseau vide rempli d’exemples. |
| 6 | `claim_verification` | ETW, repli GitHub Issues et attribution, chacun qualifié | Texte et notes de source par l’analyste; notes numérotées générées depuis les références approuvées. |
| 7 | `proposed_detections` | Chasse locale candidate, non validée | Texte par l’analyste; aucune règle YARA/Suricata à générer sur ces seuls éléments. |
| 8 | `uncertainties_and_next_leads` | Questions irréductibles et vérifications locales possibles | Texte par l’analyste; liste/encadré facultatif rendu par le template. |
| 9 | `analyst_observations` | Comment lire les niveaux de preuve et la provenance | Texte par l’analyste; métadonnées de sources, pagination et en-têtes générés. |

Pour les blocs mixtes, conserver `qualification: inference`; n’appliquer `supported` qu’aux assertions effectivement couvertes par les associations VT passives. Rattacher les UUID aux champs de références du bloc. Le titre, les en-têtes/pieds de page, la pagination, le style des notes, les légendes et la mise en page du tableau relèvent du rendu; la conclusion, les limites, les relations d’artefacts et les recommandations doivent rester des décisions rédactionnelles explicites.

## C. Textes proposés, prêts à reprendre

### `key_findings`

```text
Constat matériel — L’analyse statique de l’archive acquise `DubaiAirport_Carrers_IT_Test.zip` (SHA-256 6e7d9b33f1e72ea1ede71373a604ecdb060dab7d42055179c1eede9ecd1fd239) identifie deux membres dont les octets sont identiques aux échantillons standalone analysés : `Resources/PsProxy.dll` (SHA-256 3fd810a3aa0039993393741b32287c367a9a5037a41e826906440887cdd3ed13) et `Resources/RuntimeBroker.dll` (SHA-256 53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402). [F:4ef7b836-731f-5f51-96d4-f364640b8e3b; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]

Portée — Cette identité établit l’appartenance de ces fichiers à l’archive et rattache à ces octets l’analyse statique des DLL; elle ne démontre ni leur exécution ou déploiement dans un environnement ciblé, ni une communication réseau, ni l’identité d’un opérateur. [F:4ef7b836-731f-5f51-96d4-f364640b8e3b; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]

Évaluation — L’analyse statique décrit des capacités PowerShell, HTTP et AES-CBC; le mécanisme GitHub Issues, le réglage ETW et l’évaluation d’attribution restent rapportés par la source ou non vérifiés indépendamment. [F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4; F:91d9ff4d-193a-5773-96b0-56bc3b1efd97; F:fef1e600-fa76-5274-9a9e-acd6cc892702; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:746907e4-ccde-53a5-b073-0e189976758c]

Qualification — Les associations VT passives soutiennent les noms de fichiers liés aux deux hashes documentés; elles ne constituent pas une observation d’exécution ni une preuve d’attribution à une campagne ou à un acteur. [F:ce1d9868-60bb-563f-9753-50df9cbeb53d]
```

### `implant_materials`

```text
L’inventaire statique a relevé les membres ci-dessous dans l’archive; les deux DLL en gras ont en outre une identité SHA-256 exacte avec les samples standalone analysés. Les autres lignes décrivent l’inventaire de l’archive et ne prouvent pas une exécution. [F:4ef7b836-731f-5f51-96d4-f364640b8e3b; F:f6f0a34e-8207-5e1c-9040-b17cb49b95d8; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]
```

| Objet | SHA-256 | Taille | Relation et provenance |
|---|---|---:|---|
| `DubaiAirport_Carrers_IT_Test.zip` | `6e7d9b33f1e72ea1ede71373a604ecdb060dab7d42055179c1eede9ecd1fd239` | 134 661 o | Archive acquise; inventaire statique de 53 entrées. `[F:f6f0a34e-8207-5e1c-9040-b17cb49b95d8]` |
| **`DubaiAirport_Carrers_IT_Test/DubaiAirport_Carrers_IT_Test/Resources/PsProxy.dll`** | `3fd810a3aa0039993393741b32287c367a9a5037a41e826906440887cdd3ed13` | 6 144 o | Membre; identité exacte avec le sample standalone. `[F:4ef7b836-731f-5f51-96d4-f364640b8e3b; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]` |
| **`DubaiAirport_Carrers_IT_Test/DubaiAirport_Carrers_IT_Test/Resources/RuntimeBroker.dll`** | `53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402` | 33 280 o | Membre; identité exacte avec le sample standalone. `[F:4ef7b836-731f-5f51-96d4-f364640b8e3b; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]` |
| `DubaiAirport_Carrers_IT_Test/DubaiAirport_Carrers_IT_Test/Resources/RuntimeBroker.exe` | `8875a41ead3b17eb8538466bfecb211a52d7ee56fa6e496b4407bdb0caa51dc5` | 16 832 o | Membre inventorié; aucun comportement d’exécution établi par cette entrée. `[O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]` |
| `DubaiAirport_Carrers_IT_Test/DubaiAirport_Carrers_IT_Test/Resources/RuntimeBroker.exe.config` | `0c9869b4aa7dde55936ab8726c442368b722eb69b785ef5c8e24938a01fdee89` | 551 o | Membre inventorié; contenu non extrait, donc hash seul ne vérifie pas le réglage ETW rapporté. `[F:fef1e600-fa76-5274-9a9e-acd6cc892702; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]` |
| `DubaiAirport_Carrers_IT_Test/DubaiAirport_Carrers_IT_Test/Resources/RuntimeBrokerApi.dll` | `7f04062340c1c238c0641b6421ec5eba53d7320b405502e8eb6fcad33aaf8607` | 23 056 o | Membre inventorié; relation d’exécution non établie. `[O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]` |
| `FlightManager.cs` | `2ce188f5e38fb00a6b88f95bdd86bfc99512b828af3400bb8eb8b57f97b855da` | 1 390 o | Membre rapporté dans l’inventaire; ne pas le confondre avec le projet standalone. `[F:f6f0a34e-8207-5e1c-9040-b17cb49b95d8; F:925c6249-47f0-596f-983d-999cbe855024]` |

Le rapprochement du `FlightManager.csproj` standalone avec le membre `FlightManager.cs` reste non confirmé; ne pas présenter le projet comme membre vérifié ni comme IOC associé à cette archive. [F:925c6249-47f0-596f-983d-999cbe855024]

### `infection_chain_and_capabilities`

```text
Capacités statiques — Dans `PsProxy.dll`, le corps décompilé de `Execute` crée un runspace PowerShell, y ajoute un script fourni par l’appelant et une commande opaque, invoque PowerShell puis collecte les résultats. [F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]

Capacités statiques — `RuntimeBroker.dll` contient un usage générique de `WebClient` permettant de télécharger des données, d’analyser une réponse UTF-8 et d’effectuer un téléversement conditionnel; l’analyse ne fournit pas d’URL concrète ni de trafic observé. [F:91d9ff4d-193a-5773-96b0-56bc3b1efd97; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]

Capacité cryptographique — Des méthodes de `RuntimeBroker.dll` réalisent un déchiffrement AES-CBC sur des données fournies ou lues depuis un chemin; cette capacité générique n’établit pas que les données soient des commentaires GitHub ou des données de routage C2. [F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484]

Interprétation — Présenter ces éléments comme capacités de composants vérifiés dans l’archive, et non comme étapes successives d’une infection observée. [D:F:4ef7b836-731f-5f51-96d4-f364640b8e3b; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]
```

### `additional_samples`

```text
L’inventaire de l’archive mentionne également `FlightManager.cs` (1 390 octets; SHA-256 2ce188f5e38fb00a6b88f95bdd86bfc99512b828af3400bb8eb8b57f97b855da). [F:f6f0a34e-8207-5e1c-9040-b17cb49b95d8]

Le rapprochement du `FlightManager.csproj` standalone avec le membre `FlightManager.cs` n’a pas été confirmé; conserver cette question comme incertitude, sans présenter le projet comme membre vérifié. [F:925c6249-47f0-596f-983d-999cbe855024; F:4ef7b836-731f-5f51-96d4-f364640b8e3b]

Les autres membres `RuntimeBroker.exe`, `RuntimeBroker.exe.config` et `RuntimeBrokerApi.dll` sont recensés dans le tableau matériel avec leurs tailles et hashes; leur présence dans l’archive ne suffit pas à prouver leur exécution. [O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]
```

### `network_and_infrastructure`

```text
État des observables — Aucun domaine, adresse IP, URL de C2 ni échange réseau n’est confirmé par les éléments de cette analyse; les capacités HTTP génériques ne fournissent pas à elles seules un indicateur réseau. [F:91d9ff4d-193a-5773-96b0-56bc3b1efd97; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484]

La synthèse de Unit 42 rapporte un recours possible à GitHub Issues après échec du canal principal ou réponse HTTP 401; cette plateforme ne constitue pas ici un IOC confirmé et le chemin d’appel n’a pas été relié statiquement aux helpers AES examinés. [F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]
```

### `claim_verification`

```text
ETW — La source rapporte un paramètre `etwEnable` avec `enabled="false"` dans la configuration associée à `RuntimeBroker.exe`. [F:fef1e600-fa76-5274-9a9e-acd6cc892702]

Le membre `RuntimeBroker.exe.config` a un hash SHA-256 connu, mais son contenu n’a pas été extrait; ni la valeur effective du paramètre ni son effet à l’exécution ne sont donc vérifiés indépendamment. [F:fef1e600-fa76-5274-9a9e-acd6cc892702; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]

GitHub Issues — Unit 42 rapporte une voie de repli possible en cas d’échec du C2 principal ou de réponse HTTP 401; les analyses statiques disponibles ne relient pas les méthodes AES observées à la recherche d’issues, à l’extraction d’un commentaire ou à cette condition. [F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]

Ne pas étendre cette description à HTTP 403 ni présenter le mécanisme comme observé en exécution. [F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484]

Attribution — La synthèse rapporte l’évaluation Unit 42 d’un acteur « Iran-aligned »; la mention de Blackwood reste source-rapportée et aucun élément indépendant de cette analyse n’établit une attribution à Blackwood APT. [F:746907e4-ccde-53a5-b073-0e189976758c]
```

### `proposed_detections`

```text
Chasse locale candidate, non validée — Rechercher les hashes exacts de l’archive et des deux DLL dans les inventaires et télémétries déjà autorisés, puis examiner les événements du même hôte et de la même période avant toute conclusion d’exécution. [D:F:4ef7b836-731f-5f51-96d4-f364640b8e3b; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]

Si ces journaux existent, corréler chargement des DLL, création/invocation d’un runspace PowerShell et événements réseau du processus; la combinaison est un axe de triage dérivé des capacités statiques, pas une règle validée ni une séquence observée. [D:F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4; F:91d9ff4d-193a-5773-96b0-56bc3b1efd97]

Ne pas déclencher une alerte sur l’usage générique de `WebClient`, AES-CBC ou GitHub seul; aucune règle YARA/Suricata ni aucun IOC réseau n’est étayé par les éléments présentés. [D:F:91d9ff4d-193a-5773-96b0-56bc3b1efd97; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4]
```

### `uncertainties_and_next_leads`

```text
Limites irréductibles — Les éléments actuels n’établissent pas que l’archive ou ses membres aient été exécutés ou déployés dans un environnement ciblé, ni qu’une communication ait eu lieu. [F:4ef7b836-731f-5f51-96d4-f364640b8e3b; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484]

Le contenu de `RuntimeBroker.exe.config` reste inconnu malgré son identité cryptographique; le réglage ETW et son effet à l’exécution restent donc à confirmer. [F:fef1e600-fa76-5274-9a9e-acd6cc892702; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]

Aucune chaîne d’appel complète ne relie le traitement HTTP 401, la recherche GitHub Issues, l’extraction d’un commentaire et le déchiffrement AES-CBC; aucune attribution indépendante à Blackwood n’est établie. [F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4; F:746907e4-ccde-53a5-b073-0e189976758c]

Pistes proportionnées — Si des journaux déjà disponibles dans un cadre autorisé couvrent la même machine et la même période, corréler chargements de modules, activité PowerShell, télémétrie ETW et journaux DNS/proxy/HTTP; l’absence d’une trace isolée ne démontrerait pas la désactivation d’ETW ou l’absence de communication. [D:F:4ef7b836-731f-5f51-96d4-f364640b8e3b; F:fef1e600-fa76-5274-9a9e-acd6cc892702; F:91d9ff4d-193a-5773-96b0-56bc3b1efd97]

Une revue statique interprocédurale des seuls binaires déjà autorisés à l’analyse pourrait préciser les liens entre helpers; elle ne remplacerait pas une preuve d’exécution. [D:F:c9ab2d7c-c1fa-5bc5-ab28-25913c88bfb4; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484]
```

### `analyst_observations`

```text
Lecture des preuves — Le résultat direct le plus solide est l’identité SHA-256 entre deux membres de l’archive et les échantillons standalone; il permet d’affirmer l’appartenance, pas l’exécution. [F:4ef7b836-731f-5f51-96d4-f364640b8e3b; O:ac2fc5ca-65e9-5a9e-81a1-1a9c7b9fa64d]

Les associations VT sont passives et limitées aux noms/hashes documentés; les éléments ETW, GitHub Issues et attribution proviennent d’affirmations reprises de la source ou restent non vérifiés par cette analyse. [F:ce1d9868-60bb-563f-9753-50df9cbeb53d; F:fef1e600-fa76-5274-9a9e-acd6cc892702; F:266bf1f9-14ec-58ef-94bf-0f8ceabfb484; F:746907e4-ccde-53a5-b073-0e189976758c]

Présenter chaque niveau séparément et ne pas transférer la qualification d’une association de hash à une conclusion d’exécution, de campagne ou d’acteur. [D:F:ce1d9868-60bb-563f-9753-50df9cbeb53d; F:4ef7b836-731f-5f51-96d4-f364640b8e3b]
```

## D. Proposition de liste IOC et de provenance

Utiliser le tableau de `implant_materials` comme **inventaire d’artefacts** avec une colonne de qualification explicite. Pour une courte liste de chasse, retenir d’abord l’archive acquise et les deux DLL dont l’identité avec les samples standalone a été vérifiée; étiqueter les autres hashes comme membres inventoriés, sans implication d’exécution. Garder la taille du `.config` et son hash, mais ne pas en tirer sa valeur XML. N’ajouter aucun domaine, IP, URL ou endpoint GitHub. Exclure le `FlightManager.csproj` de la liste confirmée jusqu’à résolution du rapprochement; ne pas le confondre avec le membre `FlightManager.cs`.

Le rendu doit préserver les hashes complets en monospace et leur provenance par ligne. Ne pas raccourcir les SHA-256 dans l’appendice; si la largeur manque, utiliser une ligne dédiée ou un tableau sur largeur pleine plutôt qu’un retour qui pourrait être pris pour un séparateur.

## E. Notes de rendu et typographie

- Reprendre la hiérarchie CN2609 : page de titre/synthèse, titres contrastés, corps lisible, en-têtes et pieds paginés, légendes de figures/tableaux, notes et références numérotées. La référence utilise Hanken Grotesk et une police monospace pour le code; vérifier la disponibilité des polices du runtime avant rendu.
- Garder les hashes dans une police monospace, taille suffisante et cellules sans écrasement; vérifier retours, césures, marges, répétition des en-têtes de tableau et lisibilité à l’impression.
- Une figure utile montrerait seulement `ZIP acquis → deux DLL membres identiques aux samples standalone`, avec une légende « appartenance et identité d’octets; aucune exécution représentée ». Ne pas dessiner une flèche réseau ou une chaîne de charge utile non démontrée.
- Les repères internes `[F:…]`/`[O:…]` doivent devenir des notes de source ou des références de bloc, pas rester visibles comme jetons dans le PDF final. Un encadré peut distinguer « établi », « rapporté » et « non vérifié » sans coder les trois comme un score unique.
- Le PDF de brouillon `opinion-v6-draft.pdf` n’était pas présent dans le dossier éditorial lors de la revue; aucun défaut de pagination ou de mise en page du brouillon ne peut être constaté à ce stade.

## F. Surinterprétations retirées ou à éviter

- Identité SHA-256 et appartenance à l’archive ≠ exécution, déploiement, infection ou lien avec une cible.
- Méthodes statiques PowerShell, WebClient et AES-CBC ≠ chaîne d’appel GitHub Issues, URL/C2 concrète ou comportement runtime.
- `enabled="false"` rapporté par Unit 42 + hash du fichier de configuration ≠ contenu vérifié, ETW effectivement désactivé ou désactivation globale de télémétrie.
- HTTP 401 rapporté ≠ HTTP 403; GitHub Issues n’est pas un IOC confirmé.
- Évaluation « Iran-aligned » de Unit 42 et mention de Blackwood ≠ attribution indépendante à Blackwood APT.
- Membre `FlightManager.cs` ≠ confirmation que `FlightManager.csproj` standalone appartient à l’archive.
- Absence de télémétrie observée dans ce dossier ≠ preuve d’absence de communication ou de trace ETW.
- Une règle de détection fondée seulement sur des API génériques ou sur les affirmations source serait prématurée; les propositions restent des pistes locales à valider.
