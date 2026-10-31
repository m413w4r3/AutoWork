# M09 — Phase A : causes racines (HEAD `65c9c927`)

Sources : code au HEAD ; mesures live en lecture seule sur l'enquête `5e90caa2-208c-4433-ba78-bf53ff41b6eb`
(état v176, SHA `d9791af7…`) via le relais M08 (GET uniquement) et l'export d'audit
(`.autowork-runs/m09/audit/`, 498 événements, ignoré par Git). Aucun appel externe.

## Mesures de départ (live)

| Mesure | Valeur |
|---|---|
| `GET /operator-status` | 330 275 octets ; `invocations` seul = 231 Ko, répété (ambiguës + à réconcilier : 5 Ko ×2) |
| `next_idempotent_action` | `reconcile_tool_invocation` 53454898-… (jeton humain vérifié requis) |
| Pivots `available_pivots` | 68 : 64 sha256 + 4 md5 ; 39 `blocked` (`source_references_required`) ; 0 IP/domaine/URL ; ordre = (outil, hash) |
| Hypothèses dans l'état de travail | `[]` (outils `hypothesis.declare`, `experiment.record` annoncés mais non utilisés) |
| Invocations (audit) | 175 ; 5 `needs_review` ; 33+7+16 `partial` ; 13 `advisor.consult` en échec |

## Causes racines confirmées

**RC1 — Le tri précède la sélection, et il est arbitraire.**
`backend/src/cti_app/application/investigation_pivots.py:334-339` trie les candidats par
`(tool_name, schema_version, arguments JSON)` puis tronque à 128 pivots / 24 Kio (`:24-25`, `:339-351`).
Mécanisme : l'ordre revient à l'ordre alphabétique du hash ; disponibles et bloqués sont mêlés ; aucune
provenance ni couverture n'est exposée. Impact : la troncature (ou la pagination à 20) retire des pivots selon la valeur d'un hash.
En M08 la limite de 128 n'a pas mordu (68 pivots) mais `available_pivots_truncated=true`
(`research_context_builder.py:1396`, via les 512 observations sources). Correctif minimal : ordre explicite et
explicable (disponible avant bloqué, puis lien à une observation source, puis récence), troncature après
ordonnancement équitable par indicateur, et exposition de la provenance/couverture (faits, pas de score).

**RC2 — La priorité de la frontière n'a aucun lien avec une hypothèse, et n'alimente pas `available_pivots`.**
`investigation_pivot_frontier.py:933-959` : `50*nouveauté_article + 30*nouveauté_vérifiée + 5/profondeur − 5*coût`.
La frontière (`/pivots`) et `available_pivots` sont deux listes séparées ; seule la première porte un score,
la seconde (celle que l'opérateur pagine) n'en a pas. Aucune hypothèse n'est référencée : l'état de travail contient
`hypotheses: []` et le brief M08 n'a jamais demandé `hypothesis.declare`/`experiment.record`
(contrat existant : `investigation_experiments.py:1297-1420`, verdicts `supports/refutes/qualifies/inconclusive/blocked`).
Correctif minimal : ne pas changer la formule (pas de probabilité factice) ; utiliser les outils canoniques existants
comme enregistrement du cycle (brief M09), et exposer la hypothèse/le discriminant dans le journal d'opérateur.

**RC3 — Une invocation `needs_review` ancienne domine la prochaine action.**
`investigation_operator_view.py:784-813` : `unresolved[0]` (la plus ancienne, tri `updated_at`) devient
`next_idempotent_action` avant tout autre cas, y compris en `operator_led` (`:814`). En live, c'est une
réconciliation réservée à un humain. Impact : l'opérateur n'a aucune action analytique proposée, et la
réconciliation (sans effet sur les autres outils : le contrôle `operator_tool_invocation.py:1225-1304` ne bloque que
même outil + mêmes paramètres sémantiques) masque tout. En `bridge_led`, `investigation_service.py:614` et
`:2768-2784` bloquent en plus toute opération externe/reprise. Correctif minimal : exposer séparément
`next_analytic_action` et `reconciliation_tasks` ; en `operator_led`, une invocation `needs_review` sans effet sur
les autres pistes ne remplace plus l'action analytique. Aucun rejeu automatique, aucun contrôle retiré.

**RC4 — Le prompt `bridge_led` demande l'exhaustivité, pas le gain d'information.**
`bridge_investigator.py:1969-1972` : « Execute relevant listed pivots before finishing ».
Impact : pousse à exécuter la liste entière (breadth) et interdit implicitement l'arrêt documenté d'une piste.
Correctif minimal : remplacer par une sélection fondée sur la question ouverte, le discriminant et le coût,
avec arrêt autorisé si la raison est consignée.

**RC5 — Le statut est le coût dominant de l'opérateur.**
`/operator-status` fait 330 Ko (`invocations` complet à chaque lecture, 3 listes d'invocations redondantes,
`budget_reservations` 60 Ko, `evidence_checkpoint` 33 Ko) alors que le résumé de contexte ne porte ni hypothèses,
ni questions ouvertes, ni tâches de réconciliation, ni candidats. Impact : l'opérateur dépense son contexte en
statuts/CAS. Correctif minimal : vue compacte opt-in `operator-status?view=compact` (compteurs, N dernières
invocations, non résolues, `next_analytic_action`, `reconciliation_tasks`), vue complète inchangée par défaut.

## Hors cause (vérifié)
- Point 4 de la mission (« operator_led dépend d'un agent externe ») : propriété de conception, non corrigée.
- Le blocage d'une invocation ambiguë est déjà limité à l'action identique côté opérateur (preuve par test à ajouter).
