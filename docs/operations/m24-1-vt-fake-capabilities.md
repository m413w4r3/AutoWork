# M24.1 — Capacités VirusTotal vérifiées avec un fournisseur factice

Date : 2026-11-23. Base : `1f075e7e` (branche `m24-1/vt-fake-capabilities`).
Lot : **VT-FAKE**. Aucun appel VirusTotal réel, aucune acquisition réelle,
aucun réseau fournisseur. Toutes les preuves ci-dessous utilisent le port
factice en mémoire (`FakeVirusTotalPort`) ou, pour le parcours d'acceptation,
un adaptateur de téléchargement `httpx.MockTransport` ; aucun adaptateur HTTP
VT réel n'est instancié et aucun secret VT n'est lu.

## 1. Objet

Vérifier, sans fournisseur réel, les capacités de collecte VT de l'atelier
d'investigation M24 :

- inventaire de 5 hashes (3 disponibles / 2 indisponibles) plus 2 domaines et
  2 IP ;
- rapports partiels (pagination non exhaustive) ;
- refus de téléchargement ;
- quota ;
- replay / idempotence ;
- révocation G2 qui arrête les effets externes ;
- export workspace ;
- plafonds configurables et visibles, et comportement `route_unavailable`.

La règle appliquée a été : inventaire d'abord, puis uniquement les tests
manquants. Un défaut réel de vocabulaire a été démontré et corrigé
(§ 6). Le reste du code n'a pas été modifié.

## 2. Inventaire de l'existant (rejoué, non modifié)

| Oracle | Ce qu'il prouve | Niveau |
|---|---|---|
| `tests/test_investigation_bootstrap.py::test_b02…` | inventaire seeds ≠ découverts, 4 familles IOC | fake VT |
| `…::test_b05_approved_envelope_plans_and_runs…` | opérations autorisées sans clic par hash, download borné | fake VT |
| `…::test_b06_file_report_archived_normalized_once` | rapport fichier brut + normalisé, replay sans doublon | fake VT |
| `…::test_b06_unavailable_download_recorded…` | un download indisponible ne bloque pas le lot | fake VT |
| `…::test_b07_behaviour_report_records_route_limits` | comportement sandbox, PCAP non prétendu | fake VT |
| `…::test_b08_ip_domain_reports_keep_dates_relations_cursors` | IP/domaines datés, curseurs, `PARTIAL` | fake VT |
| `…::test_b09_download_is_bounded_hash_verified_and_quarantined` | octets bornés, SHA vérifié, quarantaine | fake VT |
| `…::test_b09_batch_stops_on_quota_and_revocation_without_retry` | quota et révocation arrêtent le lot, zéro retry | fake VT |
| `…::test_b10_replay_does_not_download_twice_or_consume_budget_again` | replay même clé et clé neuve : aucune 2e acquisition ni budget | fake VT |
| `…::test_b10_no_vt_never_blocks_workspace_or_snapshot` | VT absent n'empêche ni snapshot ni export | fake VT |
| `…::test_snapshot_export_builds_vt_layout_without_credentials` | export `vt/` complet, sans secret | fake VT |
| `tests/test_bootstrap_envelope.py::test_quota_exhaustion_mid_batch…` | quota par le vrai `InvestigationEnvelopeService` | fake VT |
| `…::test_b09_revocation_mid_batch_stops_without_resumption` | révocation G2 réelle en cours de lot, zéro effet ensuite | fake VT |
| `…::test_proposal_never_exceeds_settings_caps` | plafonds settings respectés par la proposition | fake VT |
| `tests/test_virustotal_investigation_ops.py::test_quota_and_authorization_errors…` | quota 429, 401/403, métadonnées journal | fake HTTP |
| `…::test_download_429_retry_uses_new_dispatch_occurrence` | 429 download, nouvelle occurrence, pas de double envoi | fake HTTP |
| `…::test_download_route_stays_absent_until_the_route_flag_is_enabled` | route non configurée → `virustotal_route_unavailable`, zéro requête | fake HTTP |
| `tests/test_investigation_vt_tools.py::test_explicit_not_found…` | 404 explicite = preuve négative, pas un succès | fake VT |
| `tests/workspace_agent/test_investigation_workspace.py::test_b_exports_vt_files_without_secrets` | export VT écrit par le vrai sidecar, hashes vérifiés | real sidecar |
| `tests/integration/test_acceptance_scenario_postgres.py` (étapes 03–05) | 5 hashes + 2 domaines + 2 IP, 5 rapports, 4 rapports d'objet, 3 downloads, 2 indisponibles, API + PG réels, VT factice | postgres |
| `tests/integration/test_bootstrap_envelope_postgres.py` | proposition → approbation humaine → lot → replay, stores PG réels | postgres |
| `tests/integration/test_investigation_bootstrap_postgres.py` | snapshot daté persisté une fois, replay sans 2e ligne | postgres |
| `tests/integration/test_virustotal_external_effects_postgres.py::test_revoked_osint_download_authority_has_zero_send_and_no_claim` | autorité révoquée ⇒ zéro envoi, zéro claim | postgres |

## 3. Tests ajoutés (manques réels)

| Test | Capacité | Ce qu'il pin précisément |
|---|---|---|
| `test_investigation_bootstrap.py::test_b11_five_hashes_three_available_two_unavailable_with_domains_and_ips` | scénario 5/3/2 + IP/domaines + partiel | 9 seeds ; 5 `vt.file_report` ; 4 `vt.object_report` ; 3 downloads quarantaine vérifiés ; 2 downloads route 404 `route_unavailable`, sans échantillon ni association liée ; 38 relations `PARTIAL` avec curseur ; export complet 5 fichiers + manifeste |
| `…::test_b12_download_route_404_is_route_unavailable_not_a_missing_file` | refus de téléchargement | 404 de route proxy ⇒ op FAILED, code `virustotal_download_route_not_found`, raison `route_unavailable` (jamais « fichier absent »), lot continue |
| `…::test_b12_download_provider_404_stays_not_found_and_is_distinguished` | refus de téléchargement | 404 d'origine fournisseur ⇒ `not_found`, code `virustotal_download_file_not_found` : la distinction route/fichier est conservée |
| `…::test_b13_local_route_failure_on_a_read_is_unavailable_never_success` | route indisponible sur lecture | `VirusTotalRouteUnavailableError` avant tout envoi ⇒ refus exact `UNAVAILABLE`/`capability_absent` (preuve pré-envoi E2E-ABC), aucun succès inventé, download laissé en attente |
| `…::test_b14_bootstrap_capabilities_from_settings_are_route_scoped` | plafonds configurables | parité avec `virustotal_capabilities_from_settings` ; flags sans route proxy, ou download sans hôte CDN, ne deviennent jamais des capabilities |
| `…::test_b14_planned_coverage_exposes_capability_and_route_limits` | visibilité | plan : download `route_status=UNVERIFIED` ; coverage `route_unverified`/`route_unavailable`/`capability_absent`/`deferred` selon l'état PCAP |
| `test_bootstrap_envelope.py::test_settings_caps_and_download_route_are_visible_in_the_proposal` | visibilité | `costs.caps` = plafonds settings ; limite effective plafonnée par l'inventaire avec sa source ; `route_status` + limitation `route_unavailable` visibles |

## 4. Matrice capacités → oracle → résultat observé

| Capacité | Oracle principal | Résultat |
|---|---|---|
| 5 hashes, 3 disponibles / 2 indisponibles | B11 (fake VT) ; acceptance étape 05 (PG) | PASS. 3 downloads `downloaded` avec `hash_verified_quarantine=true` ; 2 `failed`, aucun échantillon créé. Dans l'acceptation, les 2 indisponibles ressortent `failed`/`needs_review` (`virustotal_download_failed_before_bytes`) car le faux HTTP traverse le vrai adaptateur. |
| IP et domaines | B11 ; B08 ; acceptance 03/05 | PASS. 4 `vt.object_report` `succeeded`, dates/relations/curseurs conservés. |
| Rapports partiels | B11 ; B08 ; B07 | PASS. 38 opérations `PARTIAL` avec `next_cursor` et `pagination_complete=false` ; jamais `succeeded` complet. |
| Refus de téléchargement | B11/B12/B12b ; B06 ; envelope `…outside…` | PASS. Route 404 ⇒ `route_unavailable` ; fournisseur 404 ⇒ `not_found` ; autorisation absente ⇒ `acquisition_not_allowed` ; jamais de succès simulé. |
| Quota | B09 ; envelope quota ; client 429 | PASS. `stopped_reason=quota`, downloads suivants non émis, budget non rejoué. |
| Replay / idempotence | B10 ; PG bootstrap ; PG envelope | PASS. Même clé ⇒ même snapshot ; clé neuve ⇒ invocations réutilisées (`idempotent_replay=true`), aucun 2e download, budget inchangé. |
| Révocation G2 | B09 (service réel) ; envelope revocation ; PG OSINT | PASS. Une dispatch réussie puis `POLICY_DENIED/envelope_revoked`, zéro appel fournisseur après révocation, reprise non `READY`. |
| Export workspace | B11 ; export unitaire ; sidecar réel ; route API | PASS. `vt/manifest.json` + fichiers par IOC, SHA/taille concordants, aucun secret ; le sidecar réel écrit les fichiers. |
| Plafonds configurables/visibles | B14 ×2 ; proposal caps ; `from_settings` | PASS. Plafonds settings exposés (`costs.caps`, `limits` avec source), capabilities dérivées de settings avec préconditions de route, couverture visible. |

Résultat des tests ajoutés (avant correction, § 6) : B12 et B11 échouaient sur
`assert reason is ROUTE_UNAVAILABLE` (obtenu `not_found`) ; les 4 autres
passaient. Après correction : 6/6.

## 5. Comportement `route_unavailable` (documenté, testé)

Trois notions distinctes, volontairement non fusionnées :

1. **Planification** — une capability activée ne prouve pas la route.
   `vt.download_sample` planifié porte `route_status=UNVERIFIED`
   (`investigation_bootstrap.py:1436`) et la couverture émet
   `route_unverified` (`:1599-1609`). La proposition G2 expose le même état et
   la limitation « un échec devient route_unavailable, jamais un succès
   inventé » (`investigation_bootstrap_envelope.py:633-650`).
2. **Capability/route locales indisponibles** — `VirusTotalRouteUnavailableError`
   ou capability désactivée sont rendues par l'adaptateur en
   `UNAVAILABLE`/`capability_unavailable`, sans aucune requête
   (`investigation_vt_tools.py:656-669`). Depuis E2E-ABC, un refus prouvé
   AVANT tout envoi (compteur d'effets du dispatcher à zéro) est enregistré
   comme refus exact `UNAVAILABLE` ; seul un échec non prouvé pré-envoi reste
   `NEEDS_REVIEW` (`investigation_service.py:2347-2389`). B13 pin le refus
   exact. Le lot ne prétend donc jamais une lecture réussie.
3. **Échec de la route de téléchargement** — l'acquisition distingue
   l'origine 404 : proxy (`virustotal_download_route_not_found`), fournisseur
   (`virustotal_download_file_not_found`), CDN signé
   (`virustotal_download_404_origin_unknown`)
   (`sample_acquisition.py:540-575`). Le snapshot bootstrap mappe ces codes :
   route ⇒ `route_unavailable` ; fichier ⇒ `not_found` ; origine inconnue ⇒
   `failed` (`investigation_bootstrap.py:2088-2140`). PCAP : capability
   désactivée ⇒ `capability_absent` ; capability activée sans route ⇒
   `route_unavailable` ; route configurée ⇒ `deferred` (aucune opération
   concrète sans `sandbox_id`), `investigation_bootstrap.py:1610-1641`.

## 6. Défaut démontré et correction minimale

**Cause racine (VÉRIFIÉE)** : `_reason_from_result` classait
`virustotal_download_route_not_found` (404 de la route proxy) comme
`not_found`, alors que le plan et la proposition promettent
`route_unavailable`. Un 404 de route aurait donc été lu comme « l'échantillon
n'existe pas chez VT », ce qui fausse la provenance/la décision. B12 le
reproduisait (`assert route_unavailable` obtenu `not_found`).

**Correction** (`investigation_bootstrap.py:2130-2143`) : séparer les codes
route (`virustotal_download_route_not_found`,
`provider_download_route_not_found`) → `route_unavailable`, des codes fichier
(`virustotal_download_file_not_found`, `provider_file_not_found`) →
`not_found`. Aucun statut, budget, échantillon ou envoi n'est modifié ; seul
le vocabulaire du snapshot change. Commit séparé, test séparé.

## 7. Plafonds configurables et visibles

- Préconditions de route : `BootstrapVtCapabilities.from_settings`
  (`investigation_bootstrap.py:297-325`) n'active une capability que si la
  route proxy est plausible ; le download exige en plus l'hôte CDN requis et
  des ports. Parité vérifiée avec `virustotal_capabilities_from_settings`
  (`workers/tasks.py:130-157`) dans B14.
- Visibilité : `IOCInventoryLimitsV1`, `BootstrapPlanLimitsV1`,
  `BootstrapSnapshotLimitsV1` (`max_iocs`, `max_operations`,
  `operations_planned`, `stopped_reason`), `BootstrapBudgetViewV1`
  (unités/ downloads restants), `BootstrapSettingsCapsV1` (plafonds settings)
  et `BootstrapBudgetLimitV1` (limite effective + source
  `inventory`/`settings_cap`/`settings_default`).
- La limite effective downloads est le minimum entre le plafond settings et
  le nombre d'échantillons cités ; les deux valeurs et la source sont visibles
  (test `…settings_caps_and_download_route…`).

## 8. Couverture PostgreSQL rejouée (sous verrou)

| Commande ciblée | Résultat |
|---|---|
| `test_investigation_bootstrap_postgres.py` + `test_bootstrap_envelope_postgres.py` | 2 passed |
| `test_virustotal_external_effects_postgres.py::test_revoked_osint_download_authority_has_zero_send_and_no_claim` | 1 passed |
| `test_acceptance_scenario_postgres.py` (24 étapes, Typst réel) | 1 passed (23 s) |

La base de test est `127.0.0.1:55432` (DSN admin de `COMMON.md`), exécution
`flock /tmp/m24-pg.lock`, un seul pytest à la fois.

## 9. Non couvert / limites de cette vérification

- **Aucune preuve réelle fournisseur** : le fournisseur reste factice ; la
  connectivité, les credentials, les quotas et les routes réelles ne sont pas
  attestés (cf. `docs/operations/host-cli-spike-2026-11.md` pour la partie
  CLI, sans rapport).
- **Mapping route/fichier au niveau PG** : prouvé en test unitaire du service
  bootstrap (B12) ; le parcours d'acceptation PG ne l'exerce pas (son faux
  HTTP produit un échec ambigu avant octets, documenté § 4).
- **Quota au niveau PG** : prouvé au niveau unitaire/envelope (service réel,
  stores mémoire) ; aucun test PG ne persiste un arrêt quota.
- **Réplay multi-seed au niveau PG** : prouvé au niveau unitaire (clé neuve,
  vérifications durables) et PG pour un seul seed ; l'acceptation ne rejoue
  pas le lot.
- **Réponse fournisseur 401/403 reads** : couvert par les tests client
  (`test_virustotal_investigation_ops.py`), pas par un test bootstrap dédié.
- **Download 429** : couvert au niveau acquisition (`test_investigation_acquisition.py`),
  pas au niveau bootstrap.
- Le parcours navigateur temps réel (F7) et les fournisseurs réels restent
  hors périmètre de ce lot.

## 10. Commandes et résultats observés

```bash
cd backend
uv run --python 3.12 pytest tests/test_investigation_bootstrap.py tests/test_bootstrap_envelope.py -q --tb=short
# 41 passed
uv run --python 3.12 pytest tests/test_virustotal_investigation_ops.py \
  tests/test_investigation_vt_tools.py tests/test_investigation_acquisition_tools.py -q --tb=short
# 80 passed
uv run --python 3.12 pytest tests/test_investigation_service.py -q -k "download or route" --tb=short
# 9 passed
uv run --python 3.12 pytest tests/test_available_equals_executable.py -q -k "download or route" --tb=short
# 6 passed
uv run --python 3.12 pytest tests/workspace_agent/test_investigation_workspace.py \
  -q -k "exports_vt_files_without_secrets or exports_refuse_credential_material" --tb=short
# 2 passed
uv run --python 3.12 ruff check src/cti_app/application/investigation_bootstrap.py \
  tests/test_investigation_bootstrap.py tests/test_bootstrap_envelope.py
uv run --python 3.12 ruff format --check src/cti_app/application/investigation_bootstrap.py \
  tests/test_investigation_bootstrap.py tests/test_bootstrap_envelope.py
uv run --python 3.12 mypy src/cti_app/application/investigation_bootstrap.py \
  tests/test_investigation_bootstrap.py tests/test_bootstrap_envelope.py
# All checks passed / 3 files already formatted / Success: no issues found
```

Les commandes PG sont listées § 8.
