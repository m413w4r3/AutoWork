# Readiness — premier run CTI supervisé

**Verdict au 15 octobre 2026 : NO_GO.** Le parcours applicatif et ses barrières
ont des preuves offline, mais aucune de ces preuves n’établit une connectivité
live. Bridge, VirusTotal via proxy et Shodan restent **non vérifiés en live**;
les smokes live doivent être approuvés puis exécutés par l’utilisateur avant
tout run.

## État des capacités

| Capacité | État constaté dans cette worktree | Ce que cela prouve |
|---|---|---|
| Configuration Bridge, VT, Shodan et limites | `.env.example` et Compose exposent les réglages aux processus backend/worker; `test_compose_propagates_critical_settings_to_api_and_worker_from_a_fake_env` utilise un `.env` factice. | Le câblage offline des paramètres; aucune clé ni connectivité fournisseur réelle. |
| VT v3 / Retrohunt | Le client Retrohunt ne dépend pas de vtUI; Retrohunt reste désactivé par défaut (`VIRUSTOTAL_RETROHUNT_ENABLED=false`). | L’isolation du client et les opt-ins locaux; aucune soumission VT réelle ni validation YARA. |
| Shodan | Lectures passives protégées par clé, flags et enveloppe. | Les refus et parcours simulés; aucune permission, crédit ou réponse live. |
| Reset initial | `safe_local_reset.py` valide projet, contexte Docker, volumes et chemins; `--dry-run` ne supprime rien. Un marqueur G2/invocation bloque le reset même confirmé. | La garde offline du reset; pas une autorisation à nettoyer après le début du run. |
| Prévol/base | `preflight-supervised-first-run-stack` exécute le script embarqué dans `worker`, cible le projet `cti-bulletin` et exige l’attestation de base jetable; les tests couvrent la commande, les gardes et le head Alembic. | Le chemin est vérifié par tests et `make -n`; aucune exécution contre la stack réelle n’a été faite dans ce lot. |
| Arrêt/reprise/G2 | W16 vérifie l’arrêt après révocation, l’extension G2 humaine dans les limites retenues et la reprise sans répéter l’appel VT confirmé. Operator-status et réconciliation sont exposés par l’API. | Les scénarios de test en stockage local; aucune garantie qu’un appel fournisseur déjà parti soit annulé. |
| Avis et preview | Le PDF brouillon existe sur `GET /api/opinions/{opinion_id}/preview.pdf`, hashé et lié à la version de l’avis et à la publication source; W17 inspecte un PDF Typst réel et une note ancrée. | Le rendu simulé avec le compilateur local et la cohérence des hashes; pas la qualité éditoriale du vrai dossier. |
| Reprise intégrée | W18 vérifie le parcours et la récupération sans rejouer les effets confirmés avec fournisseurs simulés. | La cohérence du scénario offline; aucune intégration live. |

## NO_GO — écarts encore ouverts

- **Fournisseurs live :** aucune sonde Bridge depuis le worker, aucun appel VT via
  le proxy avec clé réelle, et aucune lecture Shodan réelle n’a été approuvée ou
  exécutée. Les gates L2/L4 restent `NOT_VALIDATED`; `offline_simulated` n’est
  jamais `PASS`.
- **Prévol/base :** le chemin `make preflight-supervised-first-run-stack
  SUPERVISED_FIRST_RUN_DB_DISPOSABLE=1` fixe le projet Compose `cti-bulletin`
  et exécute le script copié dans l’image backend depuis `worker`. La commande
  générée est vérifiée par `make -n` et
  `test_make_stack_preflight_executes_script_inside_worker`; les tests du
  prévol vérifient les gardes de projet/attestation/hôte et l’exigence Alembic
  head. Elle ne publie pas PostgreSQL et ne modifie pas la base. La commande
  n’a pas été exécutée contre la stack réelle : connexions Postgres, Redis,
  MinIO, Bridge et API d’identité restent `NOT_VALIDATED` jusqu’à ce prévol
  live supervisé.
- **Identité :** avec les valeurs de `.env.example`, l’API utilise
  `LocalIdentityProvider/dev-analyst`, non vérifié. Le prévol de stack lit le
  statut réel via `GET /api/identity/status`, mais ne présente pas de Bearer et
  n’authentifie donc aucun opérateur humain. Une décision G2 réelle reste un
  acte humain séparé.
- **Révocation G2 :** la révocation arrête l’investigation; une reprise sans
  nouvelle décision reste `MODEL_UNAVAILABLE`. Quand la révocation a conservé
  son plafond, l’opérateur peut demander une extension versionnée via `POST
  /api/investigations/{id}/envelope/extend` avec acteur authentifié, raison,
  correlation_id, expected_version et `Idempotency-Key`; elle ne dépasse pas
  les limites retenues, ne restaure aucun droit omis et conserve des quotas au
  moins égaux au consommé plus réservé. Pour une révocation legacy sans plafond,
  l’API renvoie `409 envelope_revoked` et operator-status indique de créer une
  nouvelle investigation. Les tests de gouvernance et W16 listés plus bas
  prouvent les chemins couverts; les décisions du vrai dossier restent
  supervisées.
- **Analyse locale :** FLOSS et MWCP sont absents. Le binaire Ghidra présent ne
  fournit pas à lui seul un adaptateur/runner REA livré; REA reste indisponible.
- **Exécution :** le Dockerfile backend ne déclare pas d’utilisateur dédié.
  L’exposition hors loopback reste à traiter avant tout déploiement exposé.
- **Retrohunt :** il reste OFF pour le premier run jusqu’à validation YARA et
  décision explicite. Les tests offline de quotas n’autorisent aucun job live.
- **Validation humaine :** le test d’un tiers sur environnement vierge n’est
  pas établi. L’inspection du vrai PDF, l’approbation G2, la décision G3, le
  repin V6, la revue de composition et la publication finale restent des actes
  supervisés séparés.

## Scénarios de test présents

Ces noms identifient les scénarios dans le HEAD de cette worktree. Les tests du
prévol ont été relancés pendant le lot E; les scénarios simulés ne remplacent
pas les smokes live.

| Lot | Scénario repère | Portée visible dans le test |
|---|---|---|
| Prévol stack | [`test_standard_stack_database_requires_compose_identity_and_attestation`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_stack_database_without_exact_local_identity_is_refused`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_stack_database_requires_the_current_alembic_head`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_make_stack_preflight_executes_script_inside_worker`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_stack_redis_probe_uses_ping_without_disclosing_url`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_stack_minio_probe_checks_bucket_with_read_only_head`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_bridge_probe_sends_only_get_models_capability_request`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_stack_identity_probe_reports_only_identity_status_fields`](../../backend/tests/test_supervised_first_run_preflight.py), [`test_runtime_inventory_uses_worker_factory_without_provider_requests`](../../backend/tests/test_supervised_first_run_preflight.py) | Attestation explicite, refus des projets/hôtes inconnus, head Alembic, probes et commande worker générée; aucune connexion à la stack réelle. |
| Configuration | [`test_compose_propagates_critical_settings_to_api_and_worker_from_a_fake_env`](../../backend/tests/test_config.py) | Correspondance `.env` factice, Settings API/worker et absence des secrets dans leur représentation. |
| Reset | [`test_real_run_marker_blocks_confirmed_reset_before_any_deletion`](../../backend/tests/test_safe_local_reset.py), [`test_initial_data_reset_requires_confirmation_and_displays_counts`](../../backend/tests/test_safe_local_reset.py) | Refus après démarrage du run, confirmation explicite et prévisualisation des cibles. |
| G2/CAS | [`test_governance_routes_approve_extend_revoke_and_enforce_cas`](../../backend/tests/test_investigation_api.py), [`test_revoke_then_explicit_reextension_preserves_scope_and_consumption`](../../backend/tests/test_investigation_governance.py), [`test_revoked_envelope_reextension_cannot_exceed_approved_bounds`](../../backend/tests/test_investigation_governance.py), [`test_revoked_reextension_cannot_reduce_budget_below_consumed_usage`](../../backend/tests/test_investigation_governance.py), [`test_legacy_revocation_without_saved_bounds_requires_new_investigation`](../../backend/tests/test_investigation_governance.py) | Réextension humaine dans le plafond G2, conservation du consommé, refus d’élargir les droits et refus des révocations legacy sans plafond. |
| W16 | [`test_w16_interrupt_during_bridge_wait_is_durable_read_only_and_resumable`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py), [`test_w16_g2_revocation_during_vt_archives_reply_and_blocks_followup`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py), [`test_w16_revoked_g2_requires_human_extend_then_resumes_without_repeating_vt`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py), [`test_w16_revoked_reextension_rejects_disclosure_relaxation_without_mutation`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py) | Pause, arrêt après révocation, décision humaine d’extension, plafond de divulgation et reprise sans répéter la lecture VT. |
| W17 | [`test_w17_real_typst_draft_preview_preserves_structure_and_limits`](../../backend/tests/workflow_emulator/test_w17_opinion_draft_preview_quality.py) | Rendu Typst local, structure du brouillon, limites et note placée après son ancre. |
| W18 | [`test_w18_supervised_first_run_recovers_without_replaying_confirmed_effects`](../../backend/tests/workflow_emulator/test_w18_supervised_first_run_recovery.py) | Parcours intégré avec fournisseurs simulés; interruption, redémarrage worker, réconciliation, preview/révision, publication V6, repin explicite et rendu final après frontières de reprise. |

La procédure future, ses commandes Make, champs de routes et conditions de
reprise sont dans
[investigation-supervised-first-run.md](../operations/investigation-supervised-first-run.md).
Le statut reste NO_GO jusqu’à la résolution des blocages ci-dessus et à la
validation live supervisée.
