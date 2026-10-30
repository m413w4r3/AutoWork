# Lot 002 — reprise C : VT passif et analyse d’un échantillon

Instantané : 2026-10-29 17:16:51 UTC. Référence dépôt `4d09c51a9d91e3a3f04e915d4673eac19ec506a9`. Le journal couvre ici `c001`–`c060`; SHA-256 au relevé : `2383b10803ddc30d7b9b5b67b3400a9c65b3d4bb798d57060f72abffb39df515`. Aucun test, API, relais ou contenu `inbox/` utilisé par l’observateur.

## Faits chiffrés et IDs

- Entre `c001` (`17:03:57.617641Z`) et `c060` (`17:16:47.508449Z`), 59 requêtes C sont journalisées : 40 GET et 19 POST. Les POST ont 16 statuts `succeeded`, 2 `failed`, 1 `partial`. Intervalle entre le premier et le dernier événement : 769.891 s; intervalle moyen entre horodatages consécutifs : 13.274 s (minimum 0.603 s, maximum 74.134 s). Ce sont des temps entre écritures journalisées, pas des durées exactes de transport; le journal ne donne pas d’heure de début par requête.
- Le POST réussi d’ORCHESTRATOR `orch-vtres5-1793293300` (invocation `ad03ea94-7bd4-479c-a287-ab9e44858225`) a une observation VT de 8 résolutions historiques, lue par C via `c004`/`c005`. C limite l’interprétation à la co-association DNS; aucune preuve d’achat, de contrat ou de contact par un hôte compromis n’est affirmée. C `c007` écarte un doublon de relation déjà couvert.
- C lit `current_opinion` (`c003`) et obtient bien le corps/2 blocs, mais leurs ancres sont `null`. `c015` constate 3 findings `qualified`; la décision notée est de préciser le finding revendeur avec l’observation DNS et de maintenir l’achat au rang d’hypothèse. Aucune révision de finding ou d’avis n’est encore journalisée.
- 15 rapports passifs `vt.file_report` distincts, tous `succeeded` et archivés :
  - `c020` / `db66b6e3-df3b-485f-ab3d-acbeaa54cdf4`
  - `c022` / `2f18ce23-08d2-4aa2-af0b-9c9ea13a7e19`
  - `c024` / `9b97674a-0482-4563-aff2-d1fb794dcff7`
  - `c026` / `d74a8d28-1469-4649-810a-1565536bae07`
  - `c028` / `9bc251ec-fc20-4f12-a4e8-72cb4985fda4`
  - `c030` / `c4291853-bd99-4ddc-a08c-f2fa24b41625`
  - `c032` / `df8fb398-b888-4681-a84a-e038be2970de`
  - `c034` / `174ce0e1-143d-4ed0-ba93-cb2f97197169`
  - `c036` / `d13616a2-bbae-4164-b805-2bbec1c3f538`
  - `c038` / `8f2e3572-4550-4a20-986c-54f45fe65d29`
  - `c040` / `b749cb15-b5db-4215-a601-849646394f94`
  - `c042` / `0138674f-ff2c-4f75-b7d1-3e4853b01bf9`
  - `c044` / `1ec07564-b9fa-4383-ae46-708f90222a83`
  - `c046` / `e98744c7-1033-4609-8937-f89eb42b8f63`
  - `c048` / `c7e33e6e-d8cb-43cf-9de9-a40bf27f0d41`
  Ces rapports couvrent les 15 SHA-256 du corpus documenté, selon `c008`, `c017` et le récapitulatif de C. La série s’étend de 17:09:57.248Z à 17:12:02.830Z (125.582 s entre horodatages des deux POST extrêmes). Les rapports VT sont des observations fournisseur de fichiers, pas preuve d’exécution ou de livraison.
- Deux lectures MD5 distinctes, `c050` / `ca206a6b-77c4-49aa-824e-499483f2520b` et `c052` / `1770a0e4-a66a-4d6a-8551-190c4e3169cb`, ont échoué avec preuve `external_effect_refused`, `provider_routes=[]`, `pages=0`. Ce ne sont pas des résultats négatifs. Aucun rejeu identique n’apparaît.
- Friction locale corrigée : tentative `c019` n’a pas été transmise (TypeError local : mauvais emplacement de l’argument `purpose` dans `opclient.post()`); elle ne figure pas au journal. C a créé l’ID neuf `c020`, déplacé `purpose` dans le corps et utilisé le CAS courant. Les pages d’outils/inventaire ont suivi les curseurs opaques exacts. C a parcouru 53/53 outils et 59/59 indicateurs, mais seulement la première page des pivots disponibles (20/72) au relevé.
- Acquisition : `c054`, `vt.download_sample`, invocation `c6defc0a-d4c5-4ac8-be20-1412b41ed97e`, `succeeded`. Le SHA attendu est confirmé; l’association `2d798ae2-2a4d-5a73-92c0-22099dc0925e` réutilise un échantillon existant (`reused_existing_sample=true`), statut `candidate`, nouveauté non confirmée. Aucun exécutable lancé. Le statut pré-acquisition `c053` donne G2 v83 approuvé/non révoqué, acquisition permise, downloads 0/1000.
- Analyse statique : `c056`, invocation `387fffa0-8550-4071-9e9b-5fc8b3f20b92`, `partial`. CAPA et FLOSS ont tourné en mode statique; SMDA est unavailable, `code_features` indisponible (architecture non prise en charge), baseline goodware absente. `c057`–`c059` lisent l’observation en 3 pages et valident le JSON concaténé (38 996 caractères, SHA `a3bd9591…bda422`). L’analyse recense des capacités .NET/WMI mais ne prouve ni exécution ni appartenance à un cluster. La sortie FLOSS est tronquée; l’absence de marqueur précis n’est donc pas établie. C maintient explicitement H2 ETW `qualified`. `c060` est un préflight avant acquisition du fichier de configuration documenté; résultat d’action non encore présent.
- Bridge : aucun nouveau POST Bridge par C jusqu’à `c060`; nouveaux tours C consommés = 0, taille/réponse/latence Bridge C = sans objet. Le total canonique reste 1 tour consommé, 0 réservé, 199 restant dans les statuts (`c001`, `c060`).

## Ambiguïtés, inférences et qualité

- Les statuts `c001`, `c053` et `c060` affichent `ambiguous_invocations=[]` et `invocations_requiring_reconciliation=[]`, mais leurs listes `invocations` conservent trois lignes `needs_review` : `1c42606f-a088-4c53-a124-6bb9909d74bf`, `1a183d52-8ac5-4dd6-8e69-db2dc80f149d`, `845d7645-ce5d-47ab-8d77-1b1a8426b4de`. Les nouvelles observations réussies ne changent pas, à elles seules, le statut de ces anciennes invocations. Entre `c001` et `c053`, le compteur VT passe de 12 consommés + 3 réservés à 30 consommés + 0 réservé, et le temps outil de 235+120 à 372+0; une réservation de 3 unités/120 s est enregistrée ensuite `committed`. Les objets de réservation ne donnent pas ici un lien probant vers une réponse pour ces trois `needs_review`. Coût réservé devenu consommé et résultat fournisseur restent donc deux questions distinctes.
- Inférence provisoire : les choix de C sont pertinents et proportionnés jusqu’ici. La série de 15 hashes est exhaustive au sein de l’inventaire documenté et non répétitive par cible; l’acquisition est ciblée sur un échantillon associé au claim ETW et les limites de l’analyse sont conservées. La répétition des GET de statut (20 au total) sert principalement aux contrôles G2/budget avant les POST; elle ajoute de la latence de procédure, sans effet externe.
- Isolation : la section C déclare s’être limitée à l’enquête et au répertoire `m07b`; aucune référence C à une autre enquête, un ancien avis ou un document externe n’a été trouvée dans les recherches de chemins. L’historique antérieur du même `operator_log.md` contient toutefois le hash de commit non expliqué déjà signalé par le premier audit; aucun usage de ce hash par C n’est observé dans sa section, donc il reste un point de provenance hérité, pas une preuve de fuite par C.
- Au relevé, `FINAL_CHECKPOINT_C.md` est absent, le relais n’a ni file ni requête en vol et la session C continue vers la lecture du fichier de configuration. Aucun arrêt prématuré n’est constaté.

Sources : entrées `c001`–`c060` et réponses correspondantes dans `outbox/`, `operator_log.md` lignes 429–717, `OPERATOR_BRIEF.md`, checkpoint B et premier rapport d’observation. Aucun test exécuté.
