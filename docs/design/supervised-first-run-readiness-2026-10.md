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
| Arrêt/reprise | W16 vérifie l’interruption durable pendant des appels en attente et l’arrêt de la suite après révocation G2. Operator-status et réconciliation sont exposés par l’API. | Les scénarios de test en stockage local; aucune garantie qu’un appel fournisseur déjà parti soit annulé. |
| Avis et preview | Le PDF brouillon existe sur `GET /api/opinions/{opinion_id}/preview.pdf`, hashé et lié à la version de l’avis et à la publication source; W17 inspecte un PDF Typst réel et une note ancrée. | Le rendu simulé avec le compilateur local et la cohérence des hashes; pas la qualité éditoriale du vrai dossier. |
| Reprise intégrée | W18 vérifie le parcours et la récupération sans rejouer les effets confirmés avec fournisseurs simulés. | La cohérence du scénario offline; aucune intégration live. |

## NO_GO — écarts encore ouverts

- **Fournisseurs live :** aucune sonde Bridge depuis le worker, aucun appel VT via
  le proxy avec clé réelle, et aucune lecture Shodan réelle n’a été approuvée ou
  exécutée. Les gates L2/L4 restent `NOT_VALIDATED`; `offline_simulated` n’est
  jamais `PASS`.
- **Prévol/base :** le prévol lit `POSTGRES_DSN` dans l’environnement du processus
  et exige une cible loopback jetable dont le nom contient `pilot`, `first-run`
  ou `disposable`. Il ne charge pas `.env`. Or le service Postgres de la stack
  Compose standard n’a pas de port publié sur l’hôte et la cible Make lance le
  script sur l’hôte. Aucun chemin vérifié ne relie donc ce prévol à la base
  standard démarrée par `make up`; le gate base reste `NOT_VALIDATED` jusqu’à
  établissement d’une cible locale joignable.
- **Identité :** avec les valeurs de `.env.example`, l’API utilise
  `LocalIdentityProvider/dev-analyst`, déclaré non vérifié et loopback seulement.
  Le rapport du prévol code cette identité locale en dur. Une décision humaine
  réelle doit rester distincte d’un Bearer d’accès API.
- **Révocation G2 :** W16 prouve qu’une investigation révoquée ne reprend pas.
  `POST /api/investigations/{investigation_id}/envelope/extend` et
  `InvestigationEnvelopeService.extend` n’ont pas
  de garde explicite sur une révocation; les tests disponibles ne prouvent donc
  pas la règle « non ré-étendable ». Traitement opératoire : ne jamais appeler
  `extend` après révocation, ne pas reprendre, et garder le chantier NO_GO
  jusqu’à une garde/test explicites. La continuation dans une nouvelle
  investigation n’est pas démontrée ici.
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

Ces noms identifient les scénarios dans le HEAD de cette worktree; ils n’ont
pas été relancés pendant cette passe documentaire. Les scénarios simulés ne
remplacent pas les smokes live.

| Lot | Scénario repère | Portée visible dans le test |
|---|---|---|
| Configuration | [`test_compose_propagates_critical_settings_to_api_and_worker_from_a_fake_env`](../../backend/tests/test_config.py) | Correspondance `.env` factice, Settings API/worker et absence des secrets dans leur représentation. |
| Reset | [`test_real_run_marker_blocks_confirmed_reset_before_any_deletion`](../../backend/tests/test_safe_local_reset.py), [`test_initial_data_reset_requires_confirmation_and_displays_counts`](../../backend/tests/test_safe_local_reset.py) | Refus après démarrage du run, confirmation explicite et prévisualisation des cibles. |
| G2/CAS | [`test_governance_routes_approve_extend_revoke_and_enforce_cas`](../../backend/tests/test_investigation_api.py) | Approbation, extension, révocation et contrôle de version; ne vérifie pas l’extension après révocation. |
| W16 | [`test_w16_interrupt_during_bridge_wait_is_durable_read_only_and_resumable`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py), [`test_w16_g2_revocation_during_vt_archives_reply_and_blocks_followup`](../../backend/tests/workflow_emulator/test_w16_investigation_durable_stop_resume.py) | Pause pendant attente Bridge, résultat VT archivé et suite bloquée après révocation. |
| W17 | [`test_w17_real_typst_draft_preview_preserves_structure_and_limits`](../../backend/tests/workflow_emulator/test_w17_opinion_draft_preview_quality.py) | Rendu Typst local, structure du brouillon, limites et note placée après son ancre. |
| W18 | [`test_w18_supervised_first_run_recovers_without_replaying_confirmed_effects`](../../backend/tests/workflow_emulator/test_w18_supervised_first_run_recovery.py) | Parcours intégré avec fournisseurs simulés; interruption, redémarrage worker, réconciliation, preview/révision, publication V6, repin explicite et rendu final après frontières de reprise. |

La procédure future, ses commandes Make, champs de routes et conditions de
reprise sont dans
[investigation-supervised-first-run.md](../operations/investigation-supervised-first-run.md).
Le statut reste NO_GO jusqu’à la résolution des blocages ci-dessus et à la
validation live supervisée.
