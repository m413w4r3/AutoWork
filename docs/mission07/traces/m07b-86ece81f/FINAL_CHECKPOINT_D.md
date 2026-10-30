# Checkpoint final — opérateur D

Date : 2026-10-30. Enquête `86ece81f-33bf-4230-9c5b-be55ea67855e`, operator-led.

## État canonique

- Status final : `running`, working-state v66, SHA-256 `d350c79f3fea642203b6de342188e8b6e08f27f81312a013fa0756e2c706e9a4` (GET d090; final sections d091/d092 au même CAS). Aucune tâche active, invocation ambiguë ou réconciliation en attente.
- G2 v129 approuvé/non révoqué; TLP clear; `external_llm_allowed=true`; Bridge et VT permis.
- Avis `ef1838cb-000f-53a1-bc0e-82dde8eb1179`, v4, SHA `e970d9a984382cd2933d2caf4f339c8ef2d64f84e522dc7d7aea5d525e5a0fd8`, statut `draft`. U1 text SHA `c9b16b88b9e655811b65ae11b97ca515a65599b4b4776dd85a511739db1d8c1e`; U2 `975a90f4f6063fd64b6f5bc1de707d86caa5aa456557e86f4e97168e6a109864`.
- Aucun approval, publication, repin, acquisition ni exécution d’échantillon.

## Bridge — revue v3, décisions et portée

- Tentative d010 : échec avant soumission (`bridge_server_error`, `confirmed_non_execution=true`, aucun Bridge ID/réponse, environ 126.8 s); réservation libérée. Aucun avis n’en est tiré.
- Reprise unique d015 : soumission confirmée `succeeded`/`run_confirmed`, Bridge conversation turn `cdaec3b2-836f-4f2a-ade3-6c33c877999c`; une unité de tour, soumission 192.377 s (budget outil 203 s), prompt 284,514 octets, réponse 3,499 octets, output SHA `3a22ed6bcf86c664667c0f4e7606301da9f117028804db735e0b330b87383e40`; usage fournisseur `usage_unavailable`. Préflight `READY`: 49 pièces présentes, 21 requises, 0 manquante.
- La réponse v3 est complète et contient deux revues séparées, U1 et U2, chacune `usable=true`, sans blocage, et liée au text SHA exact envoyé. Statut global `partial`; affectation `alias` heuristique et avertissements `heuristic_unit_assignment`, `bridge_reported_partial`, `source_alias_in_prose`. Conformément au brief, le statut global ne rend pas inutilisables les deux unités vérifiées.
- Reparse local d086 : `unsupported_legacy_format`, aucun nouvel artefact, zéro tour Bridge; la réponse source conservée demeure vérifiable par unité. Aucun nouveau dossier n’a été soumis. La revue valide cible v3 uniquement; le draft v4 ci-dessus incorpore les remarques mais n’a pas été re-revu.

### Triage des remarques

| Unité | Remarque Bridge | Décision et preuve |
|---|---|---|
| U1 | Distinguer article, observation et conclusion | Acceptée; distinction déjà présente et conservée (`52bdd0ea`, `a6b68b75`, `17adef05`). |
| U1 | Formulation ETW | Adaptée à `etwEnable` configuré avec `enabled="false"` dans le contexte concerné; pas de généralisation à l’hôte (`52bdd0ea`). |
| U1 | Limites de RuntimeBroker.dll et de Client/Blackwood.dll.conf | Acceptée; analyse statique partielle, aucun effet ETW mesuré; fichier distinct et sortie `strings` sans preuve d’absence dans tous les octets (`69804f6b`, `6e6cd056`, `52bdd0ea`). |
| U1 | Date de l’association DNS | Adaptée à `2025-08-28 23:59:30 UTC` / `2025-08-29 01:59:30 CEST` à Paris; epoch 1756425570 (`17adef05`). |
| U1 | Lien opérationnel relais/Blackwood | Adaptée : l’article situe le lien dans l’adresse de relais inscrite dans la configuration Blackwood; les seules résolutions DNS ne prouvent pas de contact par un hôte compromis (`52bdd0ea`, `17adef05`). |
| U1 | Niveau de confiance Iran-aligned | Adaptée pour restituer la confiance élevée attribuée par Unit 42, sans identification indépendante d’une organisation (`a6b68b75`). |
| U1 | Analyse statique, similarité et nom Blackwood.dll | Acceptée; les limites sont conservées; ni le nom du fichier ni les traits ne prouvent appartenance/parenté (`69804f6b`, `0f016f02`, rapport VT `6c61974d`). |
| U2 | Facture « non disponible » | Adaptée à « non produite dans le dossier examiné », sans prétendre à une absence générale (`52bdd0ea`, finding H1). |
| U2 | Distinction des deux configs et absence de mesure hôte | Acceptée et conservée (`52bdd0ea`, `6e6cd056`). |
| U2 | Pas de groupe précis | Adaptée : confiance élevée sur l’alignement iranien selon Unit 42; pas d’identification indépendante de Blackwood APT ou d’un groupe précis (`a6b68b75`). |
| U2 | « Comparaison réussie » | Adaptée à « comparaison enregistrée et artefact produit »; métriques détaillées inaccessibles et aucune parenté déduite (`0f016f02`, artefact SHA `1ee0519c…`). |
| U2 | Ajouter des pistes concrètes | Acceptée comme vérifications futures, non comme résultats : config exacte et télémétrie ETW hôte; documents de transaction/contrôle; métriques comparatives lisibles. |

Aucune remarque rejetée ou différée. U1–U2 et leurs références sont consultables dans l’observation Bridge `37c67c07-20d3-5f12-a536-0ea05728f805` (SHA page `c3e6289d…`); le texte brut a été récupéré intégralement en 50 pages.

## Findings et pivot VT

- H1 `d5afc178-6357-59bd-a534-6d54af68162c` — `qualified`. Huit résolutions historiques pour `91.107.156[.]29`, dont `ns1.soroshpasargad.com`, corroborent la coassociation DNS (observations `17adef05`, `16839908`). Cela ne démontre ni achat, contrat, propriété/contrôle ni contact par un hôte compromis.
- H2 `2ccc6879-01c4-5504-baed-b4b6c2145594` — `qualified`. La source rapporte le réglage ETW; l’analyse statique partielle et le fichier de config distinct ne mesurent pas une télémétrie d’hôte (`52bdd0ea`, `69804f6b`, `6e6cd056`).
- H3 `4907bd4c-15b2-5aa5-b2ae-baac800068c5` — `qualified`. Unit 42 attribue une confiance élevée à l’alignement iranien; aucune identification indépendante ou appartenance du sample au cluster n’est établie (`a6b68b75`, `0f016f02`, `69804f6b`).
- Pivot d079 : recherche passive `vt.intelligence_search` sur l’IP déjà observée; succès, un descriptor IP seulement, statistiques 51 harmless / 3 malicious / 1 suspicious / 37 undetected (observation `493a62c6`, complète). Aucun nouveau nom DNS ni relation de campagne; H1 reste qualifié.
- Findings relus d092 : les trois statuts restent `qualified`, sans changement de portée. Les éléments directs soutiennent les affirmations étroites; les hypothèses plus larges restent ouvertes.

## Budgets et frictions

- Bridge : 2 consommés / 2 réservés / 196 restants. D015 consomme un tour; l’autre tour consommé est antérieur à D. Deux réservations historiques restent réservées; elles ne sont pas comptées comme consommées.
- VT : 31 consommés / 0 réservé / 9,969 restants. Téléchargements : 0; Shodan : 0. Temps outil : 584 s consommées / 7,200 réservées / 78,616 restantes. Tokens contexte : 80,280 consommés / 2,941,079 réservés / 12,978,641 restants; octets contexte : 321,118 / 11,764,313 / 55,023,433.
- Frictions : d010 a échoué avant envoi; la lecture d011 a reçu un CAS périmé après avancement du Bridge et a été rechargée; le handle local d’attente d015 a disparu au rafraîchissement de contexte, mais le relais a permis de suivre la même requête sans rejeu; l’observation Bridge de 800,661 caractères a nécessité 50 pages; reparse v4 a signalé un format legacy non pris en charge.
- Le status conserve un rappel non bloquant `advisor_checkpoint_due` (`plan_investigation`, cause `first_significant_decompilation`). Aucun conseil supplémentaire n’était nécessaire pour les décisions de cette mission; les limites et pistes futures sont consignées ci-dessus.

