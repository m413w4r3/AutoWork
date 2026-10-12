# Audit des surfaces de contrôle agentique — Phase 3.1–3.4

Date : 2026-10-12  
Référence du worktree : `806d214`  
Périmètre : audit en lecture seule. Ce livrable ne propose ni nouveau pipeline ni
état canonique parallèle.

## Résumé

Les commandes nécessaires existent déjà : production explicite sur les sujets
sélectionnés, suivi des lots et runs, réparations, révisions bornées,
reconstruction et rendu distincts, Review, release et export IOC. Les fences
par identifiant, génération et hash sont généralement solides.

Le manque principal est une vue de contrôle bornée qui relie ces ressources.
Pour une édition de neuf sujets, l’agent doit faire au moins 97 lectures pour
reconstituer le minimum décrit en Phase 3.2, avant les détails de blocages,
lineages et rendus d’article. Des informations de run historique et les ajouts
de sources en attente ne sont pas composables depuis toutes les routes
actuelles. Je recommande une projection agrégée strictement en lecture seule.

Deux défauts signalés dans le journal du chantier sont encore présents :
`BridgeTransportError` échappe au typage des routes de révision, et un rejet de
validation peut recommander un recalcul de SYNTHESIS ou EXTRACTION sans que ce
stage explique réellement le refus. La garde contre une diminution implicite
des blocs d’enrichissement est, elle, présente dans le chemin ciblé actuel.

## Méthode et limites de lecture

Le brief commun a été lu en premier, puis les consignes racine et
`backend/AGENTS.md`. `.claude/CLAUDE.md` est absent du worktree. Le locator
`ctx.py` n’a pas pu démarrer : cache uv initial en lecture seule, puis dépendance
`numpy` absente avec le réseau désactivé. Après ces échecs, les recherches
`rg` ont été limitées aux fichiers API, services et domaines demandés; les
plages de code citées ci-dessous ont été lues. Aucun test, PostgreSQL, service
externe ou modèle n’a été lancé.

## 1. Matrice des capacités — Phase 3.1

| Besoin | Endpoints existants (méthode, chemin, source:ligne) | Identifiants requis | Données retournées | Lecture ou mutation | Gardes-fous existants | Limites pour un agent |
|---|---|---|---|---|---|---|
| État canonique de l’édition | `GET /api/editions/{edition_id}` (`api/editions.py:119`); journal `GET /api/editions/{edition_id}/audit` (`:161`). Écriture : `PUT /api/editions/{edition_id}` (`:128`), `POST .../archive` (`:144`). | `edition_id`; les commandes portent `version`. | État `OPEN/ARCHIVED`, période, TLP, version; audit avec acteur et avant/après. | GET lecture; PUT/archive mutation. | UUID canonique, version attendue, acteur via `IdentityProvider`; une édition archivée bloque les commandes de production. Les champs de TLP sont dans le DTO (`api/editions.py:32-63`). | Donne l’identité éditoriale, pas les runs, jobs ni artefacts. L’identité de développement est `dev-analyst`, pas une authentification de production (`backend/README.md:5-7`). |
| Sujets sélectionnés | `GET /api/editions/{edition_id}/selection` (`api/selection.py:30,104`); décision `POST .../selection/decisions` (`:114`). La production relit les sujets matérialisés via `GET /api/editions/{edition_id}/production` (`api/production.py:3563`). | `edition_id`; décision : `snapshot_version`, `discovery_subject_id`, action, `Idempotency-Key`, décision attendue. | Snapshot/version, état selected/ignored/undecided, `subject_id`, candidat canonique, décision effective; board Production expose les sujets et leur TLP. | GET lecture; décision mutation. | Décision liée à un snapshot versionné, clé d’idempotence, identifiants Discovery/Subject. La sélection matérialise un Subject; la production reçoit ensuite des `subject_ids` explicites (`docs/workflow.md:14-24,32-42`). | Le board Selection décrit aussi les candidats non sélectionnés; le board Production est le bon accès à la liste matérialisée pour produire. L’agent ne doit ni déduire un sujet d’un titre ni re-décider Selection. |
| Batch actif, progression, pause | Lecture `GET /api/editions/{edition_id}/production` (`api/production.py:3563-3655`); création `POST .../production/batches` (`:3525`); pause/reprise/annulation `POST .../{batch_id}/pause|resume|cancel` (`:3658`, `:3719`, `:3785`). | Édition; création : `subject_ids` ordonnés et `Idempotency-Key`; contrôles : `edition_id`, `batch_id`. | Batch actif/récents, items ordonnés, compteurs, état/phase, échéance, progression d’extraction, génération et activité par item. | GET lecture; création et contrôles mutation. | Édition ouverte; sujets explicites et sans run actif incompatible; idempotence du batch; pause n’interrompt pas l’étape courante et empêche la suivante; annulation garde les artefacts historiques (`production.py:3532-3556,3658-3847`; `docs/production.md:420-478`). | Le board donne les derniers runs et une activité synthétique, pas tous les jobs échoués d’un run ni ses artefacts détaillés. Il ne lit pas l’API Selection. |
| Runs, génération, checkpoints | `GET /api/subjects/{subject_id}/production` (`api/production.py:1999-2085`); `GET /api/subjects/{subject_id}/production/runs` (`:2132`); `GET /api/production/runs/{run_id}` (`:2121`). Retry/reprise/annulation : `POST .../retry`, `/resume`, `/cancel` (`:2621`, `:2810`, `:2819`, `:2847`). | Sujet pour le run courant/historique; `run_id` pour l’identité exacte; commande de retry porte `stage`. | Run courant : `run_id`, génération, étape, état, erreurs/détails, avertissements, activité d’extraction, réconciliation et statut de chaque stage. Historique : run number, état, stage, génération et erreur. | GET lecture; retry/reprise/annulation mutation. | Retry incrémente la génération, stalee les artefacts concernés, passe par un job idempotent; un run en réconciliation ne doit pas être resoumis aveuglément (`production.py:1600-1669,2641-2698`). | Le résumé par `run_id` ne fournit pas ses checkpoints. Les routes d’artefacts par `subject_id` pointent au run courant; seul PUBLICATION a aussi une lecture par `run_id` (`production.py:3506-3513`). On ne peut donc pas composer la vue complète d’un run historique. |
| Jobs, erreurs, activité et blocages | `GET /api/jobs?aggregate_type=subject&aggregate_id={subject_id}` (`api/jobs.py:122`); détail `GET /api/jobs/{job_id}` (`:136`); historique `GET .../{job_id}/history` (`:155`); événements SSE `GET .../{job_id}/events` (`:199`); métriques `GET /api/jobs/metrics/operational` (`:142`). Retry/annulation : `POST .../{job_id}/retry|cancel` (`:164`, `:185`). | `subject_id` comme agrégat; `job_id` pour détail/historique. Les jobs de production contiennent aussi `run_id` dans leurs paramètres (`application/production_jobs.py:71-82,1631-1639`). | État, tentative, retry prévu, progression, erreur structurée, paramètres et événements d’audit. Le board dérive les activités `model_call`, `reconciliation_probe`, `retry_scheduled`, `waiting_batch`, `deterministic_stage`, `idle` (`application/production_read_model.py:69-122`). | GET lecture; retry/annulation mutation. | Retry contrôlé par transitions d’état; pause du batch; audit d’acteur; les sondages de réconciliation sont différenciés d’un appel modèle. | La liste est par sujet, non par run, et n’a pas de curseur. Elle peut contenir toutes les générations; l’agent doit filtrer localement `input_parameters.run_id`. Le board synthétique n’expose pas toute l’erreur du job. |
| Artefacts, versions, sources et preuves | Artefacts courants : `GET /api/subjects/{subject_id}/production/artifacts/{references|extraction|relevance_projection|synthesis|editorial_enrichment|publication}` (`api/production.py:2951-2977,3506-3508`). Version enrichissement : `GET .../editorial_enrichment/{artifact_id}` (`:2980`). Publications : `GET /api/subjects/{id}/content` (`api/subject_content.py:145`) et indicateurs `GET .../indicators` (`:498`). Métadonnées de sources/assets : `GET .../assets` (`:504`). | Sujet courant, ou artifact ID pour la version d’enrichissement. | Chaque artifact expose ID, run, stage, version, `input_hash`, SHA-256 canonique calculé, état, réutilisation, métadonnées et `revision_targets`; les routes courantes retournent aussi le JSON canonique (`production.py:2878-2948`). Assets : TLP, hash, origine et permissions d’envoi externe (`subject_content.py:73-92,504-516`). | GET; le PDF d’article ci-dessous peut déclencher un rendu dérivé. | Hash calculé depuis les bytes canoniques, MinIO pour les blobs, versions et références exactes. Les sources d’Extraction se résolvent par `source_document_id` et `content_sha256` (`docs/production.md:130-188`). | Les réponses d’artefact incluent le JSON complet et `metadata`, sans limite de taille (`production.py:2913-2947`). `assets` ne donne pas le corps source; `/content` est la publication, pas le texte source. Le front doit demander le détail seulement si nécessaire. |
| Réparations et décisions | Sujet : `GET /api/subjects/{id}/production/repairs` (`api/production.py:2185-2255`), détail `GET .../repairs/{repair_key}` (`:2258`), vérification `POST .../verify-replacement` (`:2375`), décision append-only `POST .../{repair_key}/decision` (`:2422`), application `POST .../repairs/apply` (`:2544`), reconstruction de REFERENCES `POST .../repairs/rebuild-references` (`:2309`). Édition : `GET /api/editions/{id}/review/repairs` (`api/publication.py:469-524`), détail `GET .../repairs/{repair_key}` (`:665-805`), décision et rebuild item (`:846-1114`). | Sujet/édition, repair key; décision : run/artifact observés, génération, décision effective attendue, action, raison; rebuild peut porter run/génération/artifact. | Diagnostic, criticité et motif, disponibilité du payload, décision effective/historique, état d’application, réparations en attente, étape et plan d’exécution (impact, étapes fournisseur/déterministes, appels Q2 prévus/réutilisés). | GET/verify lecture; décision, apply et rebuild mutation. | Motifs typés, décision append-only, fences sur artifact et génération, justification, vérification des valeurs; refus 409 si la décision ou l’artifact a changé. Décision humaine requise pour lever certaines pertes critiques (`publication.py:188-250,254-291`; `production.py:2422-2473,2544-2600`). | Les listes sont paginées (100 par sujet, 200 par édition); chaque détail se demande séparément. Une décision de réparation ne signifie pas encore que le nouvel artefact l’a matérialisée (`publication.py:643-651`). |
| Preview/apply de révision ciblée | `POST /api/subjects/{id}/production/artifacts/synthesis/revisions/preview` (`api/production.py:3356-3416`); apply `POST .../synthesis/revisions` (`:3419-3480`); lineage `GET /api/production/artifacts/{artifact_id}/revision-lineage` (`:3483-3503`). Révision enrichissement héritée `POST .../editorial_enrichment/revisions` (`:3017-3086`). | Sujet, artifact/version/input hash/SHA-256 canonique, target stable, opération, instruction, mode; `before_hash`; extension : request ID et `source_additions_hash`. | Preview : target path, stages préservés/invalidés/rebasés, appels modèle attendus, blockers, refus typé. Apply : artifact/version/hashes, publication obtenue, lineage, acteur, preuves et refus. | Preview HTTP POST mais lecture sans écriture/modèle; apply mutation (ou refus sans artifact); lineage lecture. La route enrichissement héritée n’a pas de preview distinct. | CAS exact sur ID/version/input hash/SHA-256 et `before_hash`; cibles/opérations typées; validation de preuves, TLP/politique d’accès; idempotence de requête; `delete_*` explicite. La doc agentique décrit ce contrat (`2026-10-08-targeted-revision-agent-interface.md:1-36`). | Le preview porte déjà un bon plan local à cette révision, mais ne fournit pas un catalogue global des actions ni le coût complet d’un retry générique. Le POST apply est synchrone; aucun job de révision à sonder avant la réponse. |
| Extension de sources | `POST .../source-additions/candidates`, `/select`, `/acquire` (`api/production.py:3240-3353`); puis preview/apply commun ci-dessus. | Sujet + `production_run_id` courant, `request_id`, URL; sélection porte critère/acteur; acquisition peut porter archive manuelle encodée. | État de l’addition, archive/document/checkpoint, extraction et preuves; acquisition donne appels modèles, réutilisations et refus/blockers; preview donne manifest, hash et appels prévus par stage. | Les trois appels mutent le journal/acquisition; preview commun est lecture seule; apply promeut REFERENCES/EXTRACTION/PROJECTION et SYNTHESIS. | Vérifie que le run d’entrée est encore courant (`production.py:3228-3237`); URL et archive contrôlées; politiques de collecte/TLP/modèle; fences et promotion atomique; bloqueurs critiques non ignorés (`:3300-3353`; `docs/design/2026-10-08-chantier-correction-journal.md:100-125`). | Il n’existe pas de GET pour lister les additions en attente d’un run. Les réponses des POST et le preview par request ID sont les seules lectures API de cet état; le client agent doit garder ce request ID. |
| Restore d’enrichissement stale | Preview `POST /api/subjects/{id}/production/artifacts/editorial_enrichment/restore/preview` (`api/production.py:3089-3129`); apply `POST .../restore` (`:3132-3183`). | Sujet et fences complètes de l’artefact d’enrichissement courant. | Blocs restaurables/non restaurables avec provenance; apply retourne nouvel artifact/version/hashes et publication reconstruite. | Preview lecture sans modèle/écriture; apply mutation déterministe. | Base artifact exacte; validation actuelle des preuves/placements; les suppressions `delete_*` empêchent la restauration; append-only (`docs/design/2026-10-08-targeted-editorial-revision.md:180-200`). | Pas de statut asynchrone requis pour le restore lui-même; le statut courant se relit ensuite par run/artifact. |
| Preview et rendu article/édition | Article : `GET /api/subjects/{id}/publication/preview?artifact_id=...` et `GET .../preview/{artifact_id}/pdf?render_identity=...` (`api/subject_content.py:373-442`); PDF courant `GET /api/subjects/{id}/publication/pdf` (`:153-209`). Édition : `GET /api/editions/{id}/preview?preview_input_hash=...` (`api/publication.py:1476-1505`); preview PDF `GET .../preview/pdf?preview_input_hash=...` (`:1591-1608`); release PDF `GET .../release/pdf` (`:1543-1553`); rendu final/retry `POST .../release/render` (`:1460-1473`). | Sujet + artifact/render identity pour preview article; édition + preview hash pour édition. | Disposition ready/stale/in-progress, IDs/hash d’artifact et de rendu; preview édition donne hash d’entrée, refs d’artefacts et document complet. Les PDF sont des blobs rendus. | GET preview; l’accès au PDF article appelle le renderer si nécessaire; preview PDF édition compile; POST release/render relance le rendu. | PDF article seulement sur publication vérifiée et hash d’identité du rendu; preview PDF édition fence son input hash. Les rendus sont séparés des stages de production (`docs/production.md:31-67`). | Le GET de preview édition peut renvoyer le document entier. Son `stale` compare au hash précédemment observé par le client/service, pas à une seconde projection canonique persistée (`application/edition_preview.py:205-279`). |
| Review, acceptation, release | `GET /api/editions/{id}/review` (`api/publication.py:461`); include/exclude `POST .../review/items/{subject_id}/include|exclude` (`:1387-1422`); accept `POST /api/editions/{id}/publication/accept` (`:1425-1448`); release status `GET /api/editions/{id}/release` (`:1451-1457`). Release projection materialization `POST .../release/materialize` (`:1508-1540`). | Édition, sujet. Review decision : run/génération/artifact/version/input hash; acceptation crée un manifeste et un job. | `can_accept`, items bloquants, runs et générations, décisions courantes, artefact/document exact; acceptation retourne manifeste + SHA-256, version et job; release retourne release/render IDs et disponibilité PDF. | GET lecture; décisions/acceptation/materialization mutation. | Review exige fences du document; exclude a une raison obligatoire; acteur audité; acceptation append-only sur les artefacts retenus (`publication.py:106-187,320-360`; `docs/workflow.md:114-141`). | `can_accept` agrège les préconditions, mais l’acceptation reste une frontière éditoriale explicite. Release status est distinct du job d’assemblage/rendu; il faut sonder les jobs ou relire le statut. |
| Export IOC | `GET /api/editions/{id}/release/iocs.zip` (`api/publication.py:1556-1571`). | Édition avec release figée. | ZIP des CSV par publication, export texte historique et manifeste; compteurs IOC/articles dans les headers. | Lecture du release gelé. | Lecture depuis la release immuable; pas depuis le draft courant (`api/publication.py:1556-1569`). | Le contenu ZIP n’est pas à incorporer dans une projection de contrôle; celle-ci doit fournir le lien et le statut de release. |

### Invariants de contrôle à préserver

- Les mutations ciblent des UUID canoniques, versions, générations et hashes; aucun
  titre, ordre visuel ou index de carte ne constitue une identité.
- Les preuves et corps source restent adressés par document/hash; le résumé de
  contrôle ne copie ni blobs MinIO ni sorties brutes de modèle.
- L’identité d’acteur fournie par `IdentityProvider` est enregistrée sur les
  décisions et mutations; le backend README précise que `dev-analyst` est une
  identité de développement, pas l’authentification de production.
- Le TLP et `external_llm_allowed`/`do_not_submit` restent appliqués aux appels
  modèle et aux additions de sources. Une révision ne devient pas valide en
  changeant de canal.
- Pas d’endpoint d’exécution SQL/Python générique ni d’écriture arbitraire
  d’artefacts. Les commandes restent typées et passent leurs validateurs.
- Les frontières de décision humaines de Selection, des réparations critiques,
  de Review et de l’acceptation sont conservées. Rien ici n’anticipe le pivot
  « avis de l’analyste ».

## 2. Projection consolidée en lecture seule — Phase 3.2

### Composition requise aujourd’hui

Pour un run courant, l’agent doit composer au minimum **10 lectures** pour
obtenir l’identité/génération, checkpoints, identités d’artefacts, jobs,
réparations et contexte TLP des sources :

1. `GET /subjects/{subject_id}/production` : état, génération, étape,
   checkpoints, erreurs et activité.
2. Six lectures d’artefacts : REFERENCES, EXTRACTION, RELEVANCE_PROJECTION,
   SYNTHESIS, EDITORIAL_ENRICHMENT, PUBLICATION.
3. `GET /api/jobs?aggregate_type=subject&aggregate_id=...` : jobs du sujet, à
   filtrer localement par `run_id`.
4. `GET /subjects/{subject_id}/production/repairs` : dettes et blockers du run
   courant.
5. `GET /subjects/{subject_id}/assets` : TLP et politiques de soumission des
   sources.

Il faut ensuite **un appel par détail de réparation** dont on veut connaître le
payload, l’historique ou la décision effective; un appel par lineage ciblé à
vérifier; et des appels séparés pour les aperçus/PDF. Ces dix réponses ne sont
pas toutes bornées : les artefacts renvoient leur JSON complet et la liste de
jobs n’est pas paginée.

Pour une édition de `N` sujets, le minimum complet est **`10N + 7` lectures** :
10 par sujet courant, puis état édition, Selection, ProductionBoard, Review, une
page Repair Desk, aperçu édition et statut release. Pour neuf sujets cela fait
97 lectures, avant détails de réparations, pages supplémentaires ou ZIP IOC.
Le Selection board peut être omis si l’agent ne doit que produire depuis les
Subjects matérialisés du ProductionBoard; il est requis pour expliquer la
frontière snapshot/décision de sélection.

La limite n’est pas seulement le nombre de requêtes : un run historique n’a pas
de lecture de ses stages/artifacts par run ID (sauf publication), et les
additions de sources en attente ne peuvent pas être relues sans leur
`request_id`. Le corps source archivé n’est pas une ressource renvoyée par
`/assets` ou `/content`; les artefacts d’extraction exposent les références et
preuves extraites. Enfin, il n’existe pas de catalogue API global des actions
permises pour un état donné.

### Recommandation tranchée : une famille de lectures agrégées

Je recommande (a), une projection de lecture dédiée plutôt qu’un simple guide
de composition. Les dix appels par run sont un coût récurrent; l’absence de
lecture des runs historiques et des additions en attente ne se corrige pas par
un client documenté. La famille de routes est :

```text
GET /api/editions/{edition_id}/production/control-view
GET /api/production/runs/{run_id}/control-view
```

Les deux routes appellent le même projecteur de requête et acceptent `cursor`,
`limit` (1–25), et `observed_preview_input_hash` facultatif. La première pagine
les sujets; la seconde renvoie un seul run sans changer de sémantique. La
projection se reconstruit à chaque requête depuis PostgreSQL, les références
de blobs, les services de lecture existants et leurs hashes. Elle ne crée ni
table, ni cache canonique, ni job, ni état mutable.

#### Schéma de réponse proposé

Les champs et limites suivants sont le contrat public exact; aucune propriété
ne contient les corps JSON, PDF, texte source complet ou réponse modèle.

```json
{
  "schema_version": "agent-control.v1",
  "as_of": "RFC3339 UTC",
  "scope": {"kind": "edition|run", "edition_id": "UUID", "run_id": "UUID|null"},
  "edition": {
    "id": "UUID", "state": "open|archived", "version": 1,
    "period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD",
    "country_code": "XX", "tlp": "TLP value"
  },
  "selection": {
    "snapshot_id": "UUID|null", "snapshot_version": 1,
    "selected_count": 0, "href": "/api/editions/{id}/selection"
  },
  "batch": {
    "active": null,
    "recent": [{"batch_id": "UUID", "status": "queued|running|paused|...",
      "phase": "string", "items": 0, "completed": 0, "failed": 0,
      "paused_at": "RFC3339|null", "href": "/api/editions/{id}/production"}],
    "href": "/api/editions/{id}/production"
  },
  "subjects": {
    "items": [{
      "subject_id": "UUID", "title": "bounded string", "tlp": "TLP value",
      "run": null,
      "activity": {"kind": "model_call|reconciliation_probe|retry_scheduled|waiting_batch|deterministic_stage|idle",
        "stage": "ProductionStage|null", "started_at": "RFC3339|null", "attempt": 0},
      "stages": [{"stage": "ProductionStage", "status": "pending|running|succeeded|needs_review|failed",
        "artifact": null}],
      "jobs": {"counts": {"active": 0, "failed": 0, "waiting": 0},
        "items": [], "next_cursor": "opaque|null", "href": "/api/jobs?..."},
      "sources": {"counts": {"total": 0, "archived": 0, "eligible": 0,
        "critical_unresolved": 0}, "critical_unresolved": [], "next_cursor": "opaque|null",
        "href": "/api/subjects/{id}/production/artifacts/references"},
      "repair_debt": {"unresolved": 0, "blocking": 0, "pending_rebuild": 0,
        "items": [], "next_cursor": "opaque|null",
        "href": "/api/subjects/{id}/production/repairs"},
      "preview": {"state": "available|stale|unavailable|unknown", "input_hash": "sha256|null",
        "artifact_id": "UUID|null", "render_identity": "sha256|null", "href": "path|null"},
      "actions": [{"id": "typed action", "method": "GET|POST", "href": "existing path",
        "requires_human_decision": true, "preconditions": ["bounded code"]}],
      "resources": {"run": "path|null", "artifacts": {"stage": "path"},
        "jobs": "path", "repairs": "path", "revision_lineage": ["path"]}
    }],
    "next_cursor": "opaque|null"
  },
  "review": {"can_accept": false, "blocking_subjects": 0,
    "unresolved_repairs": 0, "href": "/api/editions/{id}/review"},
  "release": {"manifest_id": "UUID|null", "manifest_sha256": "sha256|null",
    "release_id": "UUID|null", "render_status": "enum", "pdf_available": false,
    "ioc_export_href": "/api/editions/{id}/release/iocs.zip"}
}
```

Pour `run` non nul, le contrat de cet objet est :

```json
{
  "run_id": "UUID", "run_number": 1, "pipeline_generation": 0,
  "status": "queued|running|ready|needs_review|failed|cancelled",
  "current_stage": "ProductionStage", "error": {"code": "string|null",
    "message": "string|null", "details_ref": "path|null"},
  "stages": [{"stage": "ProductionStage", "status": "enum",
    "artifact": {"id": "UUID", "version": 1, "input_hash": "sha256",
      "canonical_sha256": "sha256", "status": "enum", "href": "existing artifact path"}}],
  "revision_debt": {"stale_enrichment_blocks": 0,
    "latest_revision_artifact_id": "UUID|null", "lineage_href": "path|null"}
}
```

`stages` contient toujours les sept stages ordonnés; `artifact` est nul pour
SOURCES et avant production de l’artefact. `jobs.items`, `critical_unresolved`
et `repair_debt.items` sont bornés à 20, ne contiennent que IDs, état, codes,
étape, génération et horodatages; leurs `href` pointent vers les lectures
détaillées existantes. Les artefacts exposent seulement identité et hash. Les
actions sont des opérations de domaine autorisées après application des mêmes
préconditions que les services; elles ne contiennent jamais de code ni de
payload arbitraire. Les compteurs couvrent les pages suivantes.

#### Lectures réutilisées et coût

- Réutiliser le chargement du ProductionBoard : `editions.get`, batch actif et
  récents, origines Subject, runs d’édition et
  `batch_status_read_model.list_activity_for_runs` (`api/production.py:3568-3655,1212-1242`).
- Pour chaque run, réutiliser `production_runs.get/list`, `production_artifacts.list_for_run`,
  snapshot, `source_collections.list_for_subject`, le builder de statuts et les
  projecteurs de réparations déjà appelés par les routes existantes
  (`api/production.py:2008-2047,2878-2948`).
- Réutiliser `JobService.list_for_aggregate("subject", subject_id)` et filtrer
  sur le `run_id` déjà présent dans les paramètres job; ne pas créer un second
  index d’activité.
- Réutiliser `EditionReviewService.get`, `EditionRepairReadService.list` et le
  calcul de fingerprint de `EditionPreviewService`; le JSON d’aperçu est
  construit puis écarté, jamais inclus dans cette réponse.
- Ajouter au repository d’additions une lecture `list_latest_for_run` paginée
  uniquement si la vue doit afficher les intentions d’extension sans
  `request_id`. Aujourd’hui il ne sait lire que par `(run_id, request_id)`
  (`infrastructure/database/repositories/production_source_additions.py:40-65`).

Le coût DB est proportionnel à la page de sujets, leurs runs courants et les
résumés jobs/sources/réparations. Le chargement doit se faire dans une seule
lecture cohérente autant que possible et éviter une requête par champ. Les
identités canoniques restent vérifiables via leurs routes détaillées; aucun
corps de blob n’est transféré dans l’agrégat. La pagination borne le coût et la
taille de réponse; les liens détaillés existants restent l’accès aux valeurs
complètes.

## 3. Prévisibilité des actions et économie — Phases 3.3–3.4

### Hiérarchie des coûts actuelle

| Niveau | Action et coût attendu | Exposé par l’API ? | Effet / limite actuelle |
|---|---|---|---|
| 1. Rerender seul si le JSON canonique est correct | Typst depuis PUBLICATION/EditionRelease, zéro appel modèle. | Oui : PDF article `GET /api/subjects/{id}/publication/pdf`; retry rendu édition `POST /api/editions/{id}/release/render`. | Le rendu lit la publication vérifiée ou le release déjà gelé; il ne relance pas REFERENCES, EXTRACTION ni un modèle. Ce n’est pas la même chose que recomposer PUBLICATION. Le GET article peut créer/réutiliser un rendu dérivé. |
| 2. ASSEMBLY seul | Reconstruction déterministe de PUBLICATION + QA, zéro appel modèle. | Oui par retry générique `POST /api/subjects/{id}/production/retry` avec `stage=assembly`, ou rebuild de Review. | Aucun endpoint dédié nommé « rebuild publication/assembly ». ASSEMBLY est le dernier stage et produit PUBLICATION (`domain/production_pipeline.py:16-30,114-145`); le retry stalee l’artefact puis soumet le job de stage (`api/production.py:1600-1669`). Le worker avance directement à la fin après ASSEMBLY (`application/production_jobs.py:828-832`). |
| 3. Révision ciblée d’EDITORIAL_ENRICHMENT | Création/réécriture : un appel modèle; delete et restore : zéro. | Oui : preview/apply ciblés; restore preview/apply. | Seul ENRICHMENT change; PUBLICATION est reconstruit sans modèle. Le preview commun annonce l’appel attendu et les stages invalidés. |
| 4. Révision ciblée de SYNTHESIS | Un appel modèle, sauf reprise idempotente d’un ModelRun vérifié. | Oui : preview/apply ciblés et lineage. | REFERENCES/EXTRACTION/RELEVANCE restent valides; PUBLICATION est reconstruite; ENRICHMENT se rebase si ses ancres/preuves tiennent, sinon devient stale (`targeted-editorial-revision.md:65-78,172-178`). Le preview annonce les appels attendus. |
| 5. Extension de source / réparation de provenance | Acquisition et extraction de la seule nouvelle source; checkpoints réutilisables, nombre d’appels renvoyé. Une réparation de décisions d’extraction est déterministe jusqu’au retry narratif éventuel. | Oui : candidate/select/acquire, preview/promotion ciblés; réparations `verify`, `decision`, `apply`, `rebuild-references`. | `acquire` retourne `model_calls` et `reused_checkpoints` (`api/production.py:3335-3352`); le preview ciblé expose les appels par stage (`:3391-3415`). Les plans de réparation donnent les appels Q2 connus. Le coût total d’un parcours de réparation n’est pas uniformément estimé avant commande. |
| 6. Retry d’un stage amont | Stage demandé + tous ses descendants; coût modèle dépend des checkpoints/hashes/réutilisations. | Oui : retry par sujet ou run, puis suivi via run/jobs. | Graphe statique et invalidation disponibles, mais pas de preview coût/invalidation attaché à un retry générique. Le plan `model_calls_expected` n’est actuellement fourni qu’à la reprise d’un run annulé (`api/production.py:1692-1714`). |

**Conclusion sur le rerender :** un rendu seul ne relance pas
REFERENCES/EXTRACTION/modèles. La pipeline écrit `PublicationDocumentV5`
avant le renderer (`docs/production.md:31-41`); le PDF article appelle
`render_pdf(publication.artifact.id)` (`api/subject_content.py:153-184`), et le
retry de release repasse par `EditionPublicationService.retry_render`
(`api/publication.py:1460-1473`).

### Informations encore manquantes ou partielles

- **Diagnostics de refus : partiels.** Les refus ciblés ont code, raison,
  détails et parfois `recommended_restart_stage`; les contrôles de hashes
  répondent aussi avec des conflits typés. Mais transport Bridge non classé,
  validation modèle, blocker de preuve, prérequis de stage et panne transitoire
  n’ont pas un même contrat expliquant cause, portée, prochaine action sûre et
  décision humaine requise.
- **Stage minimal à recalculer :** l’API a des noms de stage et quelques
  messages, pas une justification machine stable liant l’input manquant aux
  artefacts invalidés/conservés. `recommended_restart_stage` seul ne suffit pas
  à établir que ce stage doit vraiment être relancé.
- **Nombre d’appels modèle : partiel.** Révisions ciblées, ajouts de source et
  plans de réparation exposent des estimations/compteurs. Un retry arbitraire
  depuis REFERENCES/EXTRACTION/SYNTHESIS n’expose pas de preview avec calls
  attendus, réutilisations, incertitude ou coût de downstream.
- **Réparation sémantique vs rerender pur :** les étapes et artefacts séparés
  permettent de les distinguer, mais aucune vue unifiée n’exprime le niveau de
  coût et les checkpoints impactés avant l’action.
- **État des révisions/reconstructions :** le résultat réussi d’une révision
  est versionné et consultable en lineage; un retry produit un nouveau job et
  une génération. Le POST de révision ne donne cependant pas de ressource
  d’opération interrogeable avant de finir. Le rendu édition a un statut de
  release distinct; le statut d’une révision en cours n’est pas agrégé.
- **Décision humaine : déjà visible localement, incomplet globalement.** Repair
  Desk expose criticité, blocage, action recommandée, historique et plan
  d’exécution; Review expose `can_accept`. Il faut plusieurs détails pour
  expliquer les blockers, et le ProductionBoard ne les rassemble pas.

### Défauts signalés dans le journal : vérification actuelle

| Défaut historique | État au code actuel | Preuve |
|---|---|---|
| `BridgeTransportError` de révision devient HTTP 500 non typé | **Encore présent.** Le service de synthèse appelle `draft_synthesis_proposal` sans traiter `BridgeTransportError` (`application/production_synthesis_revision.py:2601-2618`). La route ne mappe que les conflits, erreurs typées de validation, `SourceAdditionError`/`ValueError` (`api/production.py:3369-3390,3432-3453`). Le service enrichissement appelle également le gateway à `:1602-1605`; sa route ne mappe que conflit/validation/ValueError (`api/production.py:3026-3062`). Le journal rapporte explicitement les 500 de transport (`2026-10-08-chantier-correction-journal.md:196-197,213-214`). |
| `recommended_restart_stage` trompeur lorsqu’une validation rejette la sortie modèle | **Encore présent dans le parcours allow-source-extension.** L’exception de validation classe le restart en EXTRACTION si le code contient « extraction »/« lineage », SYNTHESIS sinon (`application/production_synthesis_revision.py:2179-2220`). C’est une heuristique de code d’erreur, pas une preuve qu’un recalcul de ce stage corrige le patch rejeté. Une proposition enrichissement invalide retourne aussi un refus recommandé EXTRACTION à `:1644-1651`; un besoin de nouvelles preuves recommande REFERENCES à `:1652-1655`. Le journal avait déjà noté ce cas (`2026-10-08-chantier-correction-journal.md:196-197`). |
| Une révision réduit les blocs d’enrichissement sans `delete_*` explicite | **Absent du chemin ciblé courant; garde présente.** Le contrat commun a des opérations `delete_table|diagram|chart|source_figure` (`domain/production_editorial_revision.py:21-40`). Une proposition de rewrite doit porter exactement un bloc de la famille visée et garder sa clé (`application/production_enrichment_revision.py:2425-2456`); elle remplace le bloc ciblé sans changer le nombre (`:1717-1809`). La suppression retire seulement la clé demandée, par opération delete/remove explicite (`:1688-1715,2620-2663`). Le journal décrivait l’incident historique et la garde recommandée (`2026-10-08-chantier-correction-journal.md:216-229`). Ajouter une régression test reste utile. |

## 4. Propositions bornées

Le lot D demandé est **analyse uniquement** : aucune de ces modifications n’est
faite dans ce commit. Pour le chantier d’implémentation agentique, les deux
premières propositions sont à inclure; les autres sont recommandées en suivi.

| Rang / statut | Problème prouvé | Changement minimal (fichiers/symboles) | Tests à ajouter | Risque |
|---|---|---|---|---|
| 1 — À implémenter dans le chantier agentique; valeur très haute, coût moyen | Il faut `10N + 7` appels pour la vue édition, sans vue complète de run historique ni lecture d’additions sans request ID. | Ajouter les deux lectures `control-view` et un projecteur application en lecture seule; réutiliser `get_edition_production`, `build_stage_statuses`, `EditionReviewService`, `EditionRepairReadService`, le read model d’activité et `list_for_run`. Ajouter seulement le query repository `list_latest_for_run` pour le journal d’additions. Aucun stockage/cache canonique. | API : édition/run courant et historique; pagination stable; 25 sujets/20 sous-listes max; liens vers détails; hashes/générations cohérents; sources absentes/critique; aucune écriture SQL, job, appel modèle ou mutation du journal; test limitant les requêtes et excluant les blobs. | Cohérence temporelle entre repositories et blobs; coût de lecture/hash des artifacts. Faire une vue point-in-time, borner/paginer, signaler `as_of`; ne pas prétendre à une transaction distribuée MinIO/PG. |
| 2 — À implémenter dans le chantier agentique; valeur haute, coût faible à moyen | Un échec de transport est un 500 brut; une validation sémantique peut donner un restart stage trompeur. | Dans `production_synthesis_revision.py` et `production_enrichment_revision.py`, classifier seulement les erreurs Bridge dont l’état de soumission est prouvé `not_submitted` en erreur retryable typée. Dans `api/production.py`, mapper au code HTTP stable avec `request_identity`/`model_run_id` lorsque connus. Séparer `refusal_reason` de `recommended_restart_stage`; le restart reste nul si le contenu proposé est rejeté sans défaut amont établi. Aligner les types `frontend/src/api/production.ts`. | Transport prouvé non soumis => code retryable et replay idempotent sans double soumission; transport ambigu => reste en réconciliation; refus de claims non étayés => aucune recommandation EXTRACTION par défaut; vraie panne d’extraction => EXTRACTION. | Ne jamais appeler retryable un transport potentiellement accepté; cela pourrait doubler une soumission modèle. Réutiliser la réconciliation déjà présente. |
| 3 — Recommandation différée; valeur moyenne, coût moyen | Retry générique n’annonce pas à l’avance ses stages invalidés ni le nombre d’appels; les coûts détaillés n’existent que pour certaines actions. | Ajouter une lecture/preview typée de plan de retry à `SubjectProductionService.retry_from_stage`/`plan_production_resume` et `api/production.py`; retourner stage minimal prouvé, invalidations, reuses, calls attendus par stage et `unknown_count`. Pas de commande implicite. | Pour chaque stage demander puis comparer le plan au retry réel; hashes/fences périmés donnent 409; ASSEMBLY donne seulement PUBLICATION et 0 modèle; source checkpoint réutilisé compte 0. | Le nombre d’appels modèle peut être une estimation jusqu’à résolution des checkpoints et politiques d’accès; l’API doit exposer `unknown`, pas promettre un coût exact. |
| 4 — Recommandation différée; valeur moyenne, coût faible | Garde de cardinalité actuelle vérifiée, mais le défaut historique justifie un test de non-régression explicite sur chaque famille d’enrichissement. | Ajouter des tests ciblés dans les tests existants de `ProductionEditorialEnrichmentRevisionService`: rewrite ne retire pas d’élément frère; `delete_*` ne retire qu’une clé; restore ne réintroduit pas une suppression explicite. Aucun changement de comportement nécessaire. | Table, diagramme, chart, figure; refus typed si payload omet/corrompt le bloc visé; artifact courant ancien reste immuable. | Faible; risque limité à un faux test si l’élément ciblé est recompilé ou rebasé, donc fixture de vrais contrats canoniques requise. |

À différer : endpoint de lecture des blobs source complets, opérations asynchrones
de révision avec nouvelle table de statut, changement d’authentification
production, nouveaux modèles de décision, et tout pivot « avis de l’analyste ».
Les détails de source restent accessibles via les systèmes de preuve dédiés;
la présente projection ne doit ni copier leurs bytes ni créer de deuxième
historique.
