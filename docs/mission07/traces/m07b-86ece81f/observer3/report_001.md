# Rapport intermédiaire — observation D, lot 001

Relevé au 2026-10-29 22:44:38 UTC. Périmètre : artefacts de l’enquête `86ece81f-33bf-4230-9c5b-be55ea67855e` et rapports d’observateurs désignés. Aucun appel API/provider effectué par l’observateur.

## État observé

- `journal.jsonl` : 215 lignes, SHA-256 `818ef40805dc90601929bee5d100add89c1888b5a1f7f75dbdf748eff00de0de`. D a 5 requêtes (`d001`–`d005`), toutes GET/HTTP 200, 145 075 octets de réponses cumulées; aucun POST.
- D a relu le statut v61/SHA `ec20349e…5be2d`, le résumé, l’avis courant et deux pages de `available_tools` en suivant le curseur serveur. Avis `ef1838cb…1179`, v3/SHA `4af95801…d033f0e`, brouillon, deux unités et 13 références par unité.
- Le contexte résumé mesure 247 916 octets; ce chiffre n’est pas une taille de prompt Bridge. Aucun prompt Bridge de D n’est à ce stade observable : ni taille ni latence applicables.
- G2 v119 est approuvé/non révoqué, TLP clear, `external_llm_allowed=true`; le statut canonique signale zéro job actif, zéro invocation ambiguë et zéro réconciliation. Budget initial observé : Bridge 1 consommé / 2 réservés / 197 restants. Ces valeurs précèdent les actions futures de D.
- `relay-status.json` à 22:44:38Z : file et appels en vol vides, dernier résultat `d005`. `FINAL_CHECKPOINT_D.md` absent. Le journal de travail contient les entrées D jusqu’à d005.

## Évaluation provisoire

| Dimension | État provisoire | Preuve / limite |
|---|---|---|
| Pertinence / répétition | En cours | Les cinq lectures préparent la revue et le choix de pivots; aucune répétition observable. |
| Bridge | Non évaluable | Aucun POST de D; tours, prompt effectif, latence et utilité inconnus. |
| Protocole | Conforme à ce stade | CAS v61 respecté et page suivante lue avec le curseur retourné; aucun échec ni retry. |
| Arrêt prématuré | En cours | D vient de commencer; aucune conclusion d’arrêt. |
| Qualité probatoire | Non évaluable pour D | Aucun finding ni avis modifié par D. |
| Isolation | Conforme à ce stade | Les cinq requêtes journalisées ciblent l’investigation assignée; échantillon trop court pour verdict final. |
| Frictions logicielles | Aucune observée à ce stade | Cinq GET réussis; aucune action Bridge encore testée. |
| Coût / latence | Incomplet | 145 075 octets de réponses GET, sans mesure de coût fournisseur; aucune latence Bridge. |

Prochaine étape d’observation : attendre le prochain lot D, puis rapprocher le journal, le relais, les sorties et le journal opérateur sans effectuer d’action opérationnelle.
