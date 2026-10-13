# Outils VirusTotal de l'enquête

Le module `cti_app.application.investigation_vt_tools` enregistre quatre
adaptateurs passifs derrière `ToolRegistry`. Ils appellent uniquement
`VirusTotalPort` et `VirusTotalObservationService` : aucune URL ni route HTTP
ne vient des arguments Bridge. Les pages brutes restent dans l'archive VT ;
les observations de preuve les référencent par blob et SHA-256.

## Capacités

`virustotal_tool_capabilities(capabilities)` projette les opt-ins de
`VirusTotalCapabilities` sur les clés de `ToolExecutionContext` :

| Capability D1 | Clé du registre |
|---|---|
| `file_report` | `vt.file_report` |
| `file_relationships` | `vt.relationships` |
| `intelligence_search` | `vt.intelligence_search` |
| `behaviour_reports` | `vt.behaviour` |

La composition fournit l'objet produit par
`virustotal_capabilities_from_settings(settings)` dans `workers/tasks.py`.
`ToolRegistry.list_available` n'annonce que les outils présents dans
`available_capabilities`. Une capability activée sans route ne fait pas
d'appel HTTP : l'adaptateur du port lève l'erreur locale de route, convertie en
`unavailable`.

`register_virustotal_tools` requiert le port VT, le service de persistance des
pages brutes, l'`EvidenceRecorder`, le dépôt d'observations d'enquête et le
dépôt de findings. Les deux dépôts vérifient que chaque `evidence_ref` de la
dork existe dans la même enquête. Les limites de pagination, de taille de page,
de résultats et de temps sont configurées par le backend (défauts : 3 pages,
40 éléments par page, 20 descripteurs et 120 secondes réservées).

## Outils et schémas

Tous les schémas sont JSON Schema Draft 2020-12, avec propriétés
supplémentaires refusées.

| Outil | Arguments | Résumé de sortie |
|---|---|---|
| `vt.file_report` | `file_hash` (MD5/SHA-1/SHA-256) | hash, nom et type assainis, taille, statistiques des moteurs, dates d'analyse/soumission/modification, tags bornés, route fournisseur et pages |
| `vt.relationships` | `object_type` (`file`, `ip_address`, `domain`, `url`), `object_id`, `relation` et `cursor` facultatif | objet normalisé, relation, compte, descripteurs bornés, route, pages et curseur de reprise si le plafond est atteint |
| `vt.intelligence_search` | dork `query`, `justification`, `evidence_refs` (1–16 observations/findings de la même enquête), `cursor` et `order` facultatifs | `effective_query`, descripteurs/identifiants, routes, pages et curseur éventuel |
| `vt.behaviour` | `file_hash` et `cursor` facultatif | sandbox, date, tags, indicateurs PCAP/rapport HTML, sections disponibles, route et pages |

Les relations sont contrôlées par la table `VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE`
de D1 avant l'appel au port. Une URL d'objet est validée puis ancrée par
`url-sha256:<digest>` dans les métadonnées de preuve ; les URL signées, les
identifiants avec credentials et les paramètres de signature sont refusés.

La dork reste une expression native VirusTotal : la validation normalise
notamment les variantes d'espacement de `content :`, vérifie UTF-8, caractères
de contrôle, taille maximale de 2 000 caractères et minimum de quatre octets
pour `content:`. Elle ne fige pas la liste des opérateurs VT. Les URL absolues,
les valeurs de credentials et les paramètres de clé sont refusés avant la
requête. Une référence inconnue ou d'une autre enquête donne
`policy_denied`, sans appel fournisseur. `content:` recherche une signature
dans l'index VT ; ce n'est pas un test YARA complet.

La recherche demande descripteurs uniquement. Ses hits sont représentés par
des observations `VT_RELATIONSHIP` liées au blob brut et ancrées au candidat
fichier/IP/domaine/URL. Le domaine C1 ne possède pas encore de kind distinct
pour Intelligence Search. Les pages sans candidat exploitable gardent une
observation de recherche ancrée par le SHA-256 de la dork.

## Observations et limites

Chaque page réussie produit une observation typée qui référence le blob brut
avec `record_existing_blob`, plus un résumé borné via `record_json`. Les
sorties d'outil ne contiennent ni corps brut, ni octets, ni clé, ni URL signée.
Les erreurs ne sont pas enregistrées comme pages brutes. Une page terminale
`data=[]` crée également une observation négative ; un 404 ne l'est que si le
code API identifie explicitement un objet absent.

Les rapports fichier exposent les dates VT disponibles et portent une
limitation sur leur ancienneté. Les relations sont des snapshots datés. Les
résultats de comportement dépendent des sandboxes et de leurs rapports
disponibles. Un plafond de pages ou de résultats rend le résultat `partial`.
Une recherche `content:` ne prouve ni la présence des octets d'un sample dans
un fichier, ni l'appartenance à une famille.

## Statuts et politique

| Situation | Statut | Sortie / preuve |
|---|---|---|
| Capability désactivée, route absente ou non autorisation VT | `unavailable` | `reason=capability_unavailable`, sans retry implicite |
| Quota VT | `unavailable` | `reason=quota`, `retry_after` si fourni |
| TLP `AMBER`/`RED` ou `do_not_submit` | `policy_denied` | refus local, aucun appel VT |
| Relation/dork/identifiant invalide | `failed` | validation avant l'appel concerné |
| 404 explicitement « not found » | `empty` | observation négative ancrée à l'objet |
| 404 sans code explicite, transport, 5xx ou réponse incompatible | `failed` | pas de résultat vide inventé |
| `data=[]` terminal | `empty` | observation VT typée et `record_negative` |
| Page ou résultat encore disponible au plafond configuré | `partial` | pages archivées et curseur de reprise assaini |
| Réponse exploitable complète | `succeeded` | résumé et références d'observations |

Les refus et résultats vides/partiels proposent les autres outils VT dont les
capabilities sont disponibles. Une piste vide ou indisponible laisse la boucle
d'enquête poursuivre avec une autre stratégie.

## Idempotence et budget

Le `ToolDispatcher` réserve les coûts maximaux du `ToolDefinition` avant
l'adaptateur : un `VT_UNITS` pour `vt.file_report`, jusqu'à `max_pages` pour
les outils paginés, et `TOOL_SECONDS` selon la borne configurée. Le résultat
rend les unités/pages et secondes réellement consommées. La boucle B calcule
`tool_operation_key` à partir des paramètres normalisés ; son dépôt relit une
invocation terminée sans réexécuter l'adaptateur. Le dispatcher garde aussi le
résultat d'une clé rejouée en mémoire et refuse de réutiliser cette clé avec
des arguments différents.

`vt.download_sample` reste hors de ce module ; l'acquisition dépend de
l'enveloppe C2/G2/G3.
