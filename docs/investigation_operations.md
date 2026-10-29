# Exploitation des enquêtes

## Capacités et configuration

Les drapeaux de capacité sont désactivés par défaut. Le Bridge reçoit seulement
les outils dont le port, l'archive de preuves et la capacité correspondante
sont présents. Une capacité absente retire l'outil du catalogue; un outil local
peut aussi retourner `unavailable` si une ressource précise manque.

| Famille | Réglages principaux | Dépendances construites par l'application |
| --- | --- | --- |
| VirusTotal | `VIRUSTOTAL_PROXY_URL`, `VIRUSTOTAL_API_KEY`, `VIRUSTOTAL_FILE_REPORT_ENABLED`, `VIRUSTOTAL_OBJECT_RELATIONSHIPS_ENABLED`, `VIRUSTOTAL_INTELLIGENCE_SEARCH_ENABLED`, `VIRUSTOTAL_BEHAVIOUR_REPORTS_ENABLED` | Port VT, capacités par route, `VirusTotalObservationService` et archive brute liée au catalogue de blobs. |
| Acquisition VT | `VIRUSTOTAL_FILE_DOWNLOAD_ENABLED`, `VIRUSTOTAL_DOWNLOAD_ALLOWED_HOSTS`, `INVESTIGATION_MAX_HITS_ACQUIRED` | Port d'acquisition, téléchargement hash-vérifié, `HitAcquisitionService`, `EvidenceRecorder` et quarantaine. |
| Shodan | `SHODAN_API_KEY`, `SHODAN_HOST_ENABLED`, `SHODAN_SEARCH_ENABLED`, `SHODAN_DOMAIN_ENABLED`, `SHODAN_COUNT_ENABLED` | Port Shodan, budget de crédits, archive de réponse brute et résolution de l'instantané d'article par `investigation_id`. |
| Analyse locale | `LOCAL_ANALYSIS_ENABLED`, `LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT`, `GOODWARE_CACHE_ROOT` et limites `LOCAL_ANALYSIS_*` | `LocalCommandService`, catalogue typé, lecture bornée des octets en quarantaine, analyse statique, SMDA/code features, capa, comparaison Goodware et recorder lorsqu'ils sont disponibles. `GOODWARE_CACHE_ROOT` vaut `/var/cache/autowork/goodware` par défaut. |
| Ghidra | `GHIDRA_ENABLED`, `GHIDRA_BASE_URL`, `GHIDRA_ALLOWED_HOSTS`, version attendue et chemins de routes `GHIDRA_*_PATH` | Adaptateur Ghidra; chaque endpoint doit être configuré avant d'annoncer l'outil correspondant. |
| YARA / règles | `RULE_WORKBENCH_ENABLED` | `RuleWorkbench`, `QuarantineRuleCorpusBytes`, `IsolatedYaraRunner`, candidats de détection et archive de preuves. |

Les budgets produit sont indépendants : `INVESTIGATION_MAX_CYCLES` vaut 12
tours Bridge par défaut; `INVESTIGATION_MAX_VT_READ_UNITS`,
`INVESTIGATION_MAX_SHODAN_CREDITS` et `INVESTIGATION_MAX_HITS_ACQUIRED`
définissent les plafonds locaux de configuration. `INVESTIGATION_NO_PROGRESS_LIMIT`
vaut 3 tours sans progression par défaut (valeur permise : 1 à 20). Les
budgets de l'enveloppe G2 sont canoniques pour les accès d'enquête. Sa limite
`DOWNLOADS` synchronise, dans la transaction d'approbation ou d'extension, les
trois plafonds historiques d'acquisition de `AnalystInvestigation`; cette
synchronisation ne les diminue jamais. Une révocation met les droits et budgets
externes G2 à zéro sans effacer les plafonds historiques.

À la création de l'enveloppe par défaut et lors de l'approbation G2, les
plafonds globaux sont ajoutés lorsqu'ils sont absents :

| Réglage | Défaut | Plafond G2 |
| --- | ---: | --- |
| `INVESTIGATION_MAX_TOOL_SECONDS` | 3 600 | `TOOL_SECONDS` |
| `INVESTIGATION_MAX_CONTEXT_BYTES` | 4 000 000 | `CONTEXT_BYTES` |
| dérivé | 1 000 000 | `CONTEXT_TOKENS` (`CONTEXT_BYTES / 4`) |

Le dispatcher réserve le coût déclaré en `TOOL_SECONDS` avant d'exécuter un
outil. Un dépassement renvoie `needs_review` pour cet outil sans faire échouer
les autres opérations. Les valeurs `CONTEXT_BYTES` et `CONTEXT_TOKENS` sont
présentes dans l'enveloppe, mais le constructeur de contexte ne les consomme
pas encore; sa limite locale actuelle reste distincte. Les outils de l'analyse
locale qui manquent d'un extracteur SMDA ou d'un corpus de référence Goodware
retournent `partial` avec les observations disponibles.

Les recherches externes restent aussi soumises au TLP et aux bornes de
divulgation de la synthèse vérifiée. La présence d'une clé API n'active pas une
capacité à elle seule. Configurez séparément le fournisseur, ses routes et le
droit G2 correspondant.

En production, activez `LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT=true`.
Sans Bubblewrap fonctionnel, les modes `unshare --net` et seccomp isolent le
réseau, mais pas le système de fichiers ; le réglage strict rend alors l'analyse
locale `unavailable` sans lancer l'outil.

## Actions automatiques et humaines

L'agent peut lire la synthèse et les preuves, rechercher des observations
fournisseur explicitement activées, acquérir un candidat selon G2 et lancer une
analyse locale des échantillons déjà en quarantaine. L'acquisition et l'analyse
ne changent pas automatiquement la qualification `candidate`.

Un éditeur humain approuve, étend ou révoque l'enveloppe G2. Un analyste humain
confirme ou rejette l'appartenance G3. La composition de l'avis, son
approbation pour une version et sa publication V6 sont des étapes distinctes;
la publication crée un nouvel artefact sans modifier la publication de base ou
la composition d'édition.

`available_tools` publie `archive.extract_member` avec `schema_version: "1"`
seulement lorsque la famille 7-Zip du worker confiné est attestée. Son schéma
demande `association_id`, `member_path` et `reason`; `expected_sha256` et
`archive_preset` sont facultatifs. Exemple d'arguments :

```json
{
  "association_id": "fbc63a48-d06e-58ec-80fb-0fa318f70d57",
  "member_path": "bin/loader.dll",
  "reason": "Examiner statiquement le membre mentionné par l'observation.",
  "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Le résultat reste en quarantaine et candidat; `association_id` peut ensuite
être fourni à `sample.analyze` et aux outils locaux disponibles. Le worker
refuse les chemins ambigus, liens, tailles ou ratios hors limites, sans écrire
le membre sur le système de fichiers ni l'exécuter.

## Identité et déploiement

Les décisions G2/G3, l'approbation d'avis et les décisions sur les règles
héritent de l'identité fournie par `IdentityProvider`. Un déploiement exposé
doit installer un fournisseur authentifié ; l'identité locale de développement
ne constitue pas une authentification. L'acteur `HUMAN` n'est valide que
derrière ce fournisseur authentifié.

## Ce que G2 gouverne et ce qu'il ne gouverne pas

G2 gouverne l'accès à VirusTotal, Shodan, l'acquisition d'échantillons et les
budgets/volumes correspondants. Le Bridge suit la politique de source
`external_llm_allowed`, le TLP et `do_not_submit`, réévalués à chaque tour,
comme les autres appels modèle du produit. Une enveloppe G2 n'est pas requise
pour le Bridge ; elle ne gouverne pas cet appel.

## Runbook

1. Activez uniquement les routes nécessaires et configurez leurs ports,
   secrets et limites. Redémarrez les API et workers après changement de
   configuration.
2. Vérifiez le catalogue d'outils dans `GET /api/investigations/{id}` ou
   `GET /api/investigations/{id}/envelope`. La proposition sans décision doit
   afficher `approved: false`, version `0`, et les fournisseurs réellement
   disponibles.
3. Après vérification d'une synthèse, créez l'enquête. Les lectures locales
   fonctionnent immédiatement. VT, Shodan et les acquisitions restent soumis
   à une approbation G2 explicite ; le Bridge suit la politique de source, le
   TLP et `do_not_submit`, réévalués à chaque tour.
4. Approuvez seulement les fournisseurs et budgets requis. Pour permettre un
   téléchargement, activez aussi `acquire_hits_allowed` et fixez
   `max_acquire_volume`/`downloads`. Conservez la clé `Idempotency-Key` pour
   les reprises HTTP.
5. Qualifiez chaque candidat par G3 après examen des preuves. Pour retirer les
   droits externes en cours d'enquête, appelez `/envelope/revoke` avec la
   version courante; la décision crée une nouvelle version auditée.

## Dépannage

- **Un outil n'apparaît pas** : contrôlez le drapeau de capacité, la présence
  du port, l'archive/lecteur requis, puis la réponse `available_tools`.
- **`policy_denied` / `needs_review`** : vérifiez le fournisseur autorisé dans
  G2, le TLP, `do_not_submit`, le droit d'acquisition et les budgets restants.
  La réponse fournit les alternatives disponibles et indique si une permission
  humaine est requise.
- **Téléchargement indisponible** : vérifiez le drapeau VT download, la liste
  `VIRUSTOTAL_DOWNLOAD_ALLOWED_HOSTS`, le plafond d'acquisition et le stockage
  de quarantaine. Les octets ne sont acceptés que si le SHA-256 correspond.
- **Ghidra indisponible** : configurez l'hôte autorisé, les versions attendues
  et chaque chemin d'endpoint; un port configuré sans routes validées ne doit
  pas annoncer d'outil.
- **YARA absent** : activez `RULE_WORKBENCH_ENABLED` et vérifiez le runner
  isolé, le corpus de règles et l'accès aux octets de quarantaine.
- **Aucun tour Bridge n'arrive après une demande** : vérifiez le job durable,
  son état et la génération suivante. Les replays de `Idempotency-Key`
  renvoient la demande/job existants.
