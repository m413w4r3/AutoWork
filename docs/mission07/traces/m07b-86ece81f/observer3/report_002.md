# Rapport intermédiaire — observation D, lot 002

Relevé au 2026-10-29 22:55:30 UTC. Dernier journal observé : 223 lignes, SHA-256 `69fdcd4d02ddc3e1fce1bdf25900516bf1243e776ba784487bb08fe5cb41eb3e`; dernière action D `d013-status-after-bridge-refusal`, à 22:53:28Z. Checkpoint D absent.

## Activité D depuis le lot 001

- 13 requêtes D : 12 GET et 1 POST. Tous les GET sauf `d011` répondent HTTP 200; `d011-tool-coverage` reçoit 409 stale CAS. Le relais est vide à 22:55:29Z.
- Les lectures `available_tools` couvrent les 53 capacités sur trois pages avec le curseur fourni. D a relu les trois findings `qualified` et la page 1/5 des 83 pivots, puis a préparé une revue Bridge du draft v3, U1 et U2 en prose. Le doublon apparent des GET de statut correspond au préflight, au suivi d’un POST long, puis à la réconciliation de son résultat.
- `d011` a utilisé v61 après l’acceptation du POST, quand l’état était passé à v62 : lecture échouée 409, sans effet. D a rechargé le statut à v62 avant toute suite. Pas de réémission identique.

## Bridge et validité du dossier

- L’appel `d010` (`advisor.consult`, invocation `38bd1c45-92f2-4c1f-8aa6-1969006bd779`) a échoué `bridge_server_error`, `failure_stage=pre_submission`, `confirmed_non_execution=true`. Aucune unité n’a été traitée, aucun identifiant Bridge ni réponse n’existe; `response_bytes=0`, `provider_usage=usage_unavailable`. Aucun avis Bridge ni remarque à traiter.
- Le journal seul ne fournit pas la taille du prompt. La sortie archivée de `d010` (SHA-256 `f31d16239339fba52e816368ee08d27ba93eb60e7927444d6704946a430061d8`) donne un préflight de 284 514 octets pour le prompt et 279 989 pour le dossier, SHA dossier `48ced7553b4b1bff669682830e71f1113e81c3b096e0d4ea2f16cb7d1f99fee9`. Ces octets sont préparés, non transmis; taille réellement envoyée : zéro.
- Le contrôle de complétude annonce 21 références requises, toutes présentes dans le dossier, 49 références canoniques présentes, zéro manquante et quatre omises intentionnellement. Deux grands fragments d’échantillons sont explicitement tronqués dans le manifeste; le résumé marque ces objets comme partiels. Les deux unités d’avis correspondent aux SHA de texte de d003. Cela rend le préflight structurel `READY`; le payload sémantique effectif n’a pas été transmis et n’est pas lisible dans les artefacts, donc sa vérification complète reste partielle.
- La durée archivée `submission=126 774 ms` mesure l’attente jusqu’à l’échec prétransport; ce n’est pas une latence Bridge. Latence fournisseur et coût fournisseur : inconnus. Aucune retry D n’est encore observée à 22:55:30Z.
- Le GET post-résultat d013 confirme Bridge 1 consommé / 2 réservés / 197 restants et tool_seconds 380 consommés / 7 200 réservés / 78 820 restants : la réservation de d010 a été libérée; les deux réservations antérieures subsistent. Aucun tour D n’est consommé.

## Évaluation provisoire

| Dimension | État provisoire | Preuve / limite |
|---|---|---|
| Pertinence / répétition | PARTIAL | Le préflight, les findings et les pivots consultés répondent au mandat. 83 pivots ne sont couverts que par une page sur cinq; les suivants restent à évaluer. Aucun POST dupliqué. |
| Bridge : résultat | ÉCHEC À CE STADE | La tentative d’avis n’a pas atteint Bridge; aucune remarque utile n’existe encore. La conduite de D après ce refus reste à observer. |
| Protocole | PARTIAL | Un CAS stale sur lecture, corrigé par rechargement; acceptation du POST suivie sans répétition. |
| Arrêt prématuré | En cours | Aucun checkpoint final; D n’a pas encore démontré l’épuisement des pivots ni une revue Bridge réussie. |
| Qualité probatoire | PASS provisoire pour le draft entrant; D non évalué sur révision | Le texte v3 de d003 maintient H1/H2/H3 qualifiés et sépare observations et inférences; aucune modification de D à ce stade. |
| Isolation | Conforme à ce stade | Les entrées D portent sur cette investigation; aucune autre enquête n’apparaît dans ses actions observées. |
| Frictions logicielles | PARTIAL | Erreur Bridge prétransport et 409 CAS; la première empêche l’objectif, la seconde est récupérée proprement. |
| Coût / latence | PARTIAL | Zéro tour D confirmé; 126,774 ms jusqu’à erreur prétransport. Utilisation fournisseur inconnue; les réservations préexistantes de deux tours persistent. |

Prochaine étape : observer si D effectue une tentative corrigée sans contourner la non-exécution confirmée, traite les pivots non consultés et confronte toute remarque reçue aux observations canoniques.
