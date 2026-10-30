# Rapport d’observation 002 — M07B

Relevé : 2026-10-29, 13:30:47–13:32:02 UTC  
Sources : journal, réponses API archivées, `operator_log.md`, `relay-status.json`, outbox/processed/inflight de M07B. Aucun appel API observateur; aucune écriture hors de `observer/`.

## Faits observés

- Au premier relevé du lot, `journal.jsonl` comptait `o6b-001` puis `a001`–`a033`. Les appels de l’opérateur s’étendent de 13:20:15 à 13:30:41 UTC; ils ont tous un ID unique et `ambiguous=false`.
- Trois erreurs de lecture ont été corrigées sans mutation : `a003-tools` → HTTP 403 `operator_client_scope_denied`; `a004-tools-section` → HTTP 422 `operator_context_offset_cursor_unsupported` après `cursor=0`; `a015-tools-page2` → HTTP 409 `operator_context_cursor_stale` après réutilisation sous CAS v2 d’un curseur de la page outil liée au contexte v1. Les lectures suivantes ont utilisé le chemin sectionné sans curseur initial, puis un curseur frais lié au CAS courant.
- L’opérateur a lu les 31 claims sélectionnés, puis les 71 pivots et le catalogue de 53 outils sur plusieurs pages. Les réponses aux lectures initiales de claims étaient tronquées; il a ensuite demandé les claims utiles séparément, lu les chunks retournés et relevé leurs limites.
- `a023-bridge-pivot` a échoué en précontrôle (`required_canonical_evidence_not_in_payload`, hypothèse canonique absente), `confirmed_non_execution=true`, zéro tour Bridge consommé. Le dossier rendu faisait 34 546 octets; le statut ultérieur montre une réservation libérée (1 tour, 3 600 s, 36 531 octets/9 133 tokens réservés, zéro consommé).
- `a029-bridge-pivot`, invocation `213728de-9367-4d21-805e-d7832feb2ca6`, a été soumis après déclaration des trois hypothèses. Statut `partial`, `usable=true`. Mesures du résultat : prompt 36 604 octets, dossier 34 540 octets, réponse Bridge 3 297 octets; soumission 110 950 ms, parsing 9 ms, persistance 28 ms; 112 secondes outil et 9 151 tokens/context-bytes 36 604 consommés; un tour Bridge consommé. `provider_usage=usage_unavailable`, donc coût fournisseur monétaire inconnu. Les 70 668 octets du corps HTTP journalisé ne sont pas la taille de la réponse Bridge seule.
- Le conseil Bridge bornait correctement un seul pivot : une résolution VT datée pourrait corroborer l’association DNS, sans prouver une transaction commerciale, une désactivation globale ETW ou une identité d’organisation. Il n’ajoutait aucune observation. L’opérateur a vérifié le schéma v1 de `vt.relationships`, relu G2/budgets et tenté un seul pivot passif (`a031-vt-relay-dns`, invocation `1c42606f-a088-4c53-a124-6bb9909d74bf`). Réponse `needs_review`, `adapter_output_archived=false`, `external_effect_unconfirmed`; l’observation `94b61d49-f455-5b2c-83c1-0bfbdf7f98af` ne porte aucun résultat DNS. Au dernier statut archivé, VT reste à 0 consommé/10 réservés et secondes outil à 112 consommées/120 réservées. Aucun rejeu du POST n’est observé.
- Le journal opérateur traite cette issue comme inconnue, non comme preuve négative, et indique un finding revendeur qualifié fondé sur le claim article et l’observation VT en `needs_review`. `a036-finding-reseller` est réussi (invocation `4cc9829b-a714-490d-941f-a9ee52ec8d19`, état v11); sa réponse référence le claim et l’observation VT. Le contenu/version finale du finding n’est pas inclus dans cette réponse d’outbox.
- Après `a033`, le relais a archivé deux lectures de schéma (`a034`, `a035`) et la proposition `a036`. À 13:32:02, `relay-status.json` montre `a037-finding-etw` reçu/en cours, file vide, dernier appel terminé `a036`; `FINAL_CHECKPOINT.md` est absent.

## Isolation et protocole

- Le journal opérateur référence `f7574d3f0a527a636adefaf59f1c2741db516275` comme « base du dépôt observée ». Cette valeur n’apparaît ni dans `OPERATOR_BRIEF.md`, ni dans les réponses de l’API/outbox de M07B, ni dans le journal du relais; elle est présente dans `operator_log.md`. C’est une référence hors des sources autorisées par la règle d’isolation, sauf si elle avait été fournie hors trace par l’orchestration. Cela ne prouve pas la lecture d’une enquête antérieure.
- Les références aux claims, noms d’acteurs, hypothèses, identifiants d’observations et leurs SHA utilisés dans les décisions sont présentes dans les réponses API de cette enquête et citées avec leurs observations. La recherche du journal opérateur ne montre pas de lecture déclarée de `.autowork-runs/m07/`, de `docs/mission07/`, d’autres archives ou de conclusions d’anciennes enquêtes.
- Aucune utilisation de `pgrep`/`ps` n’est mentionnée dans `operator_log.md`; seule leur interdiction apparaît dans le brief. Le journal de relais consigne les appels API, pas les commandes locales, donc cela ne suffit pas à prouver qu’aucune commande non journalisée n’a été exécutée.
- Aucun défaut de CAS sur un POST n’est visible : les mutations utilisent le `expected_version` correspondant; la consultation Bridge après le refus a un contenu reformulé et un CAS renouvelé. Aucun POST ambigu ni duplication aveugle n’est constaté. Les erreurs 403/422/409 sont de la friction d’accès/pagination récupérée, pas des mutations erronées.

## Inférences provisoires

- Les consultations de catalogues complets et l’exploration des 71 pivots ont consommé plusieurs lectures locales avant de sélectionner un seul pivot VT. C’est une exploration un peu large, mais elle a servi à trouver le schéma et l’objet exacts, sans coût fournisseur ni action externe. La reprise des outils v2 après curseur périmé était nécessaire.
- Le premier essai Bridge était prématuré : déclarer d’abord les hypothèses canoniques aurait évité un précontrôle rejeté et la préparation d’un dossier de 34,5 Ko. La récupération est propre : aucune transmission, aucune dépense Bridge et aucune répétition identique.
- Le tour Bridge réellement soumis a une utilité ciblée et proportionnée, mais sa latence de soumission dépasse 110 s et son coût fournisseur reste inconnu. L’opérateur a correctement contrôlé le conseil contre les sources et choisi le pivot conseillé sans le convertir en preuve.
- La prudence du journal sur l’effet VT inconnu est probatoirement correcte. La qualité exacte du finding H1 n’est pas encore contrôlable depuis l’outbox seule; le statut canonique final reste à vérifier.

## Suivi

Aucun verdict final. Le relais avait un POST en cours et le checkpoint final était absent au relevé. Continuer la surveillance, examiner le résultat `a037` et les findings/avis ultérieurs, puis mesurer les 15 minutes de stabilité seulement après apparition de `FINAL_CHECKPOINT.md`.
