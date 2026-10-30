# Checkpoint final — opérateur D

Date : 2026-10-30. Enquête : `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`.

## État canonique et autorisation

Le GET final `d120` confirme l’état de travail **v183**, SHA-256 `cddba7178a608442e8e763d22f1c6c2dc12a8ed63f38a6b96ece72f27e71353b`, enquête toujours `running`, aucun job actif. G2 **v360** est approuvé, non révoqué, TLP Clear, `external_llm_allowed=true`; Bridge, Shodan et VT figurent dans les fournisseurs autorisés. L’inventaire courant compte 60 outils disponibles et 10 indisponibles. L’autorisation reste bornée au périmètre passif et statique du brief.

| Budget | Consommé | Réservé | Restant |
|---|---:|---:|---:|
| Bridge turns | 4 | 5 | 191 |
| VT units | 74 | 0 | 9 926 |
| Shodan credits | 0 | 0 | 1 000 |
| Downloads | 8 | 0 | 992 |
| Tool seconds | 1 303 | 18 000 | 67 097 |
| Context tokens | 217 465 | 807 445 | 14 975 090 |
| Context bytes | 869 852 | 3 229 770 | 63 009 242 |

La facturation externe réelle de VT/Bridge n’est pas exposée. Les réservations Bridge non libérées restent à expliquer.

## Avancement des étapes 0–9 du brief

### 0. Acquisition et renseignements VT

Les échantillons disponibles étaient déjà associés et huit téléchargements restent au compteur; aucun nouveau candidat découvert pendant cette passe n’a justifié un téléchargement. La recherche SHA-256 a été rapprochée de la couverture après coup : les 15 candidats disponibles étaient déjà couverts. Trois recherches D (`d050`, `d065`, `d067`) ont donc répété des recherches existantes; aucun nouveau lien ou échantillon n’en est sorti. L’usage fournisseur associé n’est pas connu.

- `d091` — `vt.behaviour` sur le candidat #3 : invocation `0f0d822f-f877-4fc5-858f-230d0fc2c4f2`, `needs_review`, `virustotal_external_effect_needs_review`; aucune conclusion fournisseur confirmée. Pas de rejeu.
- `d093` — `vt.behaviour` sur le candidat #5 : succès, invocation `baacbc8c-07b6-4440-b792-7f6e0db39575`, observation `8f7dcc20-98f5-5208-a65f-4d53f7f33202`. La sortie ne contient que quatre descripteurs de bac à sable et des sections disponibles; aucun événement de processus/réseau n’a été lu. Elle ne prouve pas que le candidat a été exécuté.
- Deux objets `ip_address/resolutions` différents ont été refusés par le garde de références source (`d084`, `d086`). Les deux réponses portent `external_effect_refused`, sans résultat VT. Cette famille est fermée en attendant la correction du chemin de provenance (`d003`, `d004`).
- Le PCAP passif antérieur `1096baf1-1abf-4d71-ac73-4c596cae0a37` reste `needs_review`; aucun rejeu n’a été envoyé.

### 1. Plan avec Bridge et article

`d029` a échoué au préflight de preuve : les passages S3P2.1/S4P1.1 sont absents du snapshot canonique chargé, malgré leur mention dans la requête. Aucun tour Bridge n’a été consommé. La correction du résolveur est signalée dans `orchestrator_requests/d001-article-claim-preflight.md`.

`d042`, YARA statique sans claims, a rencontré `advisor_unavailable` / `bridge_unreachable`, non-exécution confirmée. Une nouvelle consultation distincte `d096` a été tentée après l’attente; elle a aussi échoué avant transport (`preflight=READY`, `pretransport_failure`, `prior_submission_made=false`, 0 tour consommé et 0 octet réponse). Aucun plan Bridge article n’a été obtenu. Pas d’autre soumission Bridge avant réconciliation/retour de disponibilité.

### 2. Étape non numérotée

Le plan d’action du brief passe de 1 à 3; il ne définit pas d’étape 2 séparée.

### 3. Analyse statique des échantillons

Aucune exécution d’échantillon n’a eu lieu. Les observations statiques déjà obtenues pour le candidat #3 montrent une intégration PowerShell en processus dans un corps de méthode; elles ne démontrent pas que ce corps a été exécuté.

Pour le candidat #5, des décompilations locales ont complété les catégories CAPA déjà rapportées :

- RID 19 valide des longueurs de clé/IV, décode une entrée Base64 et lit un texte déchiffré via AES-CBC/PKCS7.
- RID 21 calcule SHA-256 depuis une chaîne, utilise le hash comme clé AES et ses 16 premiers octets comme IV, lit les octets d’un chemin fourni puis déchiffre vers un flux mémoire.
- RID 20 contient un chemin `System.Net.WebClient` avec `DownloadData` et `UploadData`, proxy système et credentials proxy par défaut. C’est une capacité réseau présente dans le code, pas un échange observé.
- Les handlers de timer RID 6 et RID 9 arrêtent/règlent/redémarrent des timers et manipulent des valeurs registre/chemins, mais ne montrent pas d’appel direct à RID 20 ni aux helpers crypto. Le lien d’appel et l’usage runtime restent inconnus.

Le finding capacité #5 a été révisé et vérifié : ID courant `3c8481b5-9e95-5cb1-8602-ed32f01e498f`, version 3, assessment `qualified`, 11 références d’observation et aucun claim. Le finding distingue capacité statique de comportement observé et n’attribue pas le rôle d’implant décrit dans l’article. Les findings d’infection-chain restent `inconclusive` et limités à l’identité de bytes déjà consignée.

Observations directes ajoutées au finding #5 : `da9a871e-1986-5970-a189-5e6eed723d4b`, `5665fc14-f87e-545c-ac13-daf06b3b6b4a`, `89fc9e5a-d36c-5a8a-939a-2e80af47534c`, `0e0883ff-366f-59c3-8933-9855a2b6782e`, `cb90701e-6952-5bbc-98d0-d39dae9e5e3b`, `06c96cc3-4606-5fd5-9259-ac6258a928c7`, `17ba9540-ddc4-5a53-890a-f8b723fc00a5`. Les quatre observations antérieures de finding restent rattachées.

### 4. Protocole réseau, éléments crypto et figure

Les méthodes statiques révèlent déchiffrement AES et appels WebClient, mais aucune structure de message, capture exploitable ou transaction observée. Les constantes et valeurs de réseau ne sont pas consignées ici. Bridge étant indisponible, aucun `figure_plan` ni rendu `network.protocol.render` n’a été produit.

### 5. Première règle YARA

Aucune règle YARA n’a été rédigée ni proposée. La consultation Bridge n’a pas abouti et aucune suite d’octets/string invariant n’a été validée comme discriminante. Pas de `rule.propose_bridge` ni d’activation/publication de règle.

### 6. Validation YARA

Aucune règle candidate : `rule.test` n’a pas été appelé. Il n’y a donc pas de résultat goodware/faux positif à rapporter.

### 7. Pivots VT

Les 15 recherches SHA disponibles correspondaient à des recherches déjà réussies; les relations `resolutions` sont arrêtées après deux refus distincts de provenance. Le comportement du candidat #5 a fourni seulement des métadonnées de rapports de sandbox. Le comportement du candidat #3 est ambigu et non rejoué. Aucun nouveau lien de famille ou artefact canonique n’a été confirmé.

### 8. Téléchargements et boucle de validation

Aucun nouvel échantillon n’a été acquis, car les candidats de recherche étaient déjà associés et aucune règle validée n’est disponible pour revalidation. Les budgets de téléchargements restent à 8 consommés / 0 réservé. Aucun autre candidat ne justifie une boucle YARA/VT avant la résolution des frictions de provenance et l’obtention d’un résultat de règle.

### 9. Avis de l’analyste

L’avis doit être rédigé exclusivement par Bridge. Aucun draft ni relecture n’a été obtenu. L’avis reste absent/brouillon; aucune approbation, publication, release ou repin n’a eu lieu.

## Invocations ambiguës et demandes de reprise

Le statut v183 répertorie **7** invocations `needs_review`; chacune indique `resubmission_allowed=false` :

- VT PCAP : `1096baf1-1abf-4d71-ac73-4c596cae0a37` (version d’invocation 3).
- Bridge : `24554df0-65b6-4253-8514-2d6d3af0278d` (3), `f80bf41b-4792-4d0c-b681-f84840f257b7` (4), `1b74a196-2e5e-4e86-86e2-c61f6a4f864f` (4), `39a2c0b8-1dba-4ccf-9dc6-a0ae6fc2b6a3` (3), `1b54dc77-842a-4e5c-95bf-30ae11b7c6b2` (3).
- VT comportement #3 : `0f0d822f-f877-4fc5-858f-230d0fc2c4f2` (4).

La réconciliation unique `d100` de `d096` a retourné `needs_review`, `non_execution_sealed=false`, aucune réponse provider et `resubmission_allowed=false`; sa réservation Bridge demeure. Aucun autre POST de réconciliation n’a été répété.

Demandes locales destinées à l’orchestrateur :

- `orchestrator_requests/d001-article-claim-preflight.md` — passages article absents du snapshot.
- `orchestrator_requests/d002-bridge-unavailable.md` — indisponibilité Bridge antérieure.
- `orchestrator_requests/d003-vt-resolution-source-refusal.md` et `d004-vt-resolution-source-refusal-second.md` — garde de références source VT.
- `orchestrator_requests/d005-vt-behaviour-s3-ambiguous.md` — résultat VT comportement #3 à réconcilier.
- `orchestrator_requests/d006-bridge-yara-unavailable.md` — non-exécution Bridge pré-transport et réservation conservée.

## Limites et prochaine action permise

Les observations restent statiques ou des métadonnées passives; aucune preuve runtime du déchiffrement, du trafic, de la création de processus, de l’usage de PowerShell ou du rôle d’implant n’existe ici. `needs_review`, `unavailable` et `external_effect_refused` ne sont pas des résultats négatifs.

Prochaine action : l’orchestrateur résout les demandes D005/D006 et les autres invocations ambiguës, corrige le chemin de références VT, puis reprend Bridge si disponible. Après un draft YARA justifié et correctement sourcé, un opérateur autorisé pourra le tester sur échantillons connus et goodware. Les demandes dynamiques B001/B002 restent à l’orchestrateur; D n’a ni lancé de VM, ni exécuté d’échantillon, ni attendu ces runs. Aucun test logiciel n’a été exécuté.
