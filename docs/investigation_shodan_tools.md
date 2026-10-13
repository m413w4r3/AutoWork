# Outils Shodan de l'enquête

Le module `investigation_shodan_tools` enregistre quatre lectures passives dans
le `ToolRegistry` : `shodan.host`, `shodan.search`, `shodan.domain` et
`shodan.count`. Les tools ont chacun une capacité (`shodan.<tool>`), un schéma
JSON Schema Draft 2020-12 fermé aux propriétés supplémentaires et une
réservation de crédits issue de `ShodanBudgetEstimate`. Aucun tool ne lance de
scan ni ne modifie une ressource Shodan.

`shodan_tool_capabilities(ShodanCapabilities(...))` projette uniquement les
capacités activées. Le registre doit recevoir ces clés dans l'ensemble de
capacités transmis à la boucle. Une capacité absente ou un port non configuré
rend l'outil concerné `unavailable` sans appel fournisseur.

## Enregistrement

`register_shodan_tools` accepte les ports et dépôts en arguments nommés. Il
accepte aussi le bundle positionnel de `ToolRegistrar` de K1 ; dans ce cas, il
construit un `ProductionEvidenceRecorder` depuis son store et son repository
d'observations.

Le port `ArticleSnapshotPort` existant sait résoudre un snapshot par
`synthesis_artifact_id`, mais le contexte d'exécution ne transporte que l'ID
d'enquête. Pour renseigner les IOC de l'article, l'appelant fournit donc soit
`article_snapshot_resolver(investigation_id)`, soit
`synthesis_artifact_id`, soit un snapshot déjà résolu. Sans cette donnée,
l'observation indique `article_ioc_relation.resolution = "unavailable"` et ne
présente pas une absence comme vérifiée.

## Paramètres et preuves

- `shodan.host` prend une adresse IP ; `shodan.domain` un domaine.
- `shodan.search` prend une requête passive, une justification, jusqu'à 32
  `evidence_refs` vérifiées et une page facultative. Les filtres de scan actif
  sont refusés par les validations E1.
- `shodan.count` prend une requête, une justification et des références
  vérifiées. Il n'est enregistré comme capable que si le port de comptage est
  fourni.

Chaque `evidence_ref` doit être un UUID d'observation ou de finding de la même
enquête. Toute référence inconnue ou étrangère refuse la requête avant l'appel
Shodan. Les relations VirusTotal ne sont lues que dans les observations
`VT_RELATIONSHIP` de cette enquête et ne déclenchent aucun appel VT.

## Observations et interprétation

Une lecture réussie crée une observation `SHODAN_OBSERVATION` avec l'objet
exact, le hash de réponse Shodan, le timestamp de capture, l'âge calculé par
service, les crédits rapportés, les bornes de couverture, les IOC correspondants
du snapshot et les relations VT déjà présentes. Les payloads renvoyés sont des
résumés bornés : les bannières tronquées portent un marqueur explicite. Les
secrets et URL sont supprimés du résumé persisté et de la projection de résultat.
Avec `raw_archive`, le corps JSON exact est écrit par `put_bytes` dans le bucket
`shodan-raw`, puis une observation `SHODAN_OBSERVATION` référence ce blob via
`record_existing_blob` et conserve le SHA-256 du corps. Une seconde observation
du même type conserve le résumé borné ; les deux références sont renvoyées à
l'outil. Le résumé borné figure aussi dans son résultat. La taille est contrôlée
par `shodan_max_response_bytes` (limite du port E1, ou `max_response_bytes`
fourni explicitement à `register_shodan_tools`). Le bundle K1 réutilise son
`ProductionArtifactStore` pour l'archive brute. Un corps au-delà de cette limite
n'est pas archivé et produit un résultat `partial` avec une limitation explicite.

L'archiveur est optionnel : sans `raw_archive`, le comportement de persistance
reste celui du résumé JSON et les limitations indiquent « réponse brute non
archivée ». Dans E1, la clé Shodan est ajoutée aux paramètres de la requête GET,
alors que `raw_response` provient uniquement des octets de réponse HTTP ; E2 ne
reçoit ni n'archive l'URL de requête ni la clé.

Les limites incluent toujours les points suivants :

- l'index Shodan n'est pas un scan en temps réel ;
- un service vu à une date donnée ne prouve pas qu'il était actif pendant la
  période de l'attaque ;
- un certificat ou une bannière partagés ne démontrent pas l'attribution d'une
  IP à une famille.

Les tags cloud/CDN et le nombre d'hôtes partageant un certificat ou un ASN dans
la réponse sont signalés quand ces champs sont présents. Ce sont des indices
d'hébergement mutualisé, pas des preuves d'attribution.

## États de retour

| Résultat E1 | État outil | Preuve |
| --- | --- | --- |
| Capacité absente ou port non configuré | `unavailable` | Aucune lecture fournisseur |
| Quota Shodan épuisé | `unavailable`, `output.reason = "quota"` | Coût consommé rapporté à zéro |
| 404 / aucune information | `empty` | `record_negative` |
| Résultats paginés ou plafond local atteint | `partial` | Observation bornée si une réponse existe |
| Transport ou erreur fournisseur 5xx | `failed` | Coût réservé conservé pour cet essai ambigu |
| Fournisseur interdit ou divulgation bloquée | `policy_denied` | Alternatives locales proposées |

Le dispatcher B réserve le coût défini dans `ToolDefinition` avant l'adaptateur
et engage les crédits effectivement rapportés au retour. Dans le code B actuel,
un dépassement de budget pendant cette réservation est transformé par le
dispatcher en `needs_review` avant l'adaptateur ; l'adaptateur Shodan retourne
`policy_denied` pour le même cas lorsqu'il est appelé directement. Ce décalage
de statut requiert un changement du dispatcher, interdit par le périmètre E2.

## Validation

Les tests de `test_investigation_shodan_tools.py` utilisent uniquement
`FakeShodanPort` et des fakes locaux de persistance ; ils vérifient notamment
la référence du blob brut, son SHA-256 et le rejeu idempotent. Le parcours D4 passe par la
boucle `InvestigationService`, `ScriptedTurnTransport`, les outils enregistrés,
les repositories en mémoire et le recorder ; il couvre l'avis qualifié avec
Shodan, puis la poursuite locale lorsque la capacité manque.
