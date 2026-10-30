# Rapport intermédiaire — observation D, lot 003

Relevé au 2026-10-29 23:10:35 UTC. Journal : 294 lignes, SHA-256 `709afa07279a1619fc17f95a8356574e668739ce2da50dd0eef3a17b018b27fe`; dernière action D observée `d084-source-article-initial-claims`. Pas de checkpoint D; relais vide.

## Revue Bridge reçue

- D a fait une seule reprise après le refus prétransport confirmé de d010. D015 a atteint Bridge avec le même dossier v3; `advisor_status=partial`, mais les deux unités U1/U2 sont `usable=true`, sans avertissement bloquant. L’affectation heuristique par alias est signalée; les `reviewed_text_sha256` correspondent exactement aux deux unités d003 (U1 `eeda3d51…fce49`, U2 `ef51fcb2…3253e`). La réponse n’est donc pas rejetée à tort au seul vu du statut global `partial`.
- Une remarque par unité a été reçue. U1 demande une formulation technique exacte pour `enabled="false"`, la distinction entre coassociation DNS et lien opérationnel, le fuseau du timestamp DNS, et le maintien de la confiance élevée attribuée par Unit 42 sans inventer d’identification organisationnelle. U2 demande de limiter l’absence de facture au dossier examiné, de préserver la distinction entre confiance géopolitique et attribution organisationnelle, de ne pas laisser entendre que les métriques de comparaison sont lisibles, et d’ajouter des pistes concrètes.
- Contrôle indépendant des passages archivés : S2P2.1 précise bien `enabled="false"` dans le contexte concerné sans conclure à l’arrêt général de télémétrie; S6P1.1 attribue explicitement une confiance élevée à l’alignement iranien sans identification organisationnelle; S4P4.1 indique que le DNS ne prouve pas le contact par un hôte compromis et que l’adresse de relais dans la configuration fonde le lien opérationnel. S6P2.1 dans les données de cette même enquête décrit l’analogie CPX comme non probante pour une appartenance commune. Le conseil Bridge sur ces points est pertinent et ne paraît pas injecter un autre dossier.
- Le timestamp `1756425570` vaut 2025-08-28 23:59:30 UTC / 2025-08-29 01:59:30 CEST. Le draft indiquait le 29 août sans zone; demander une précision temporelle est justifié. La comparaison est bien enregistrée comme invocation réussie, mais l’observation n’expose pas ses métriques; la reformulation proposée est prudente et conserve mieux cette limite.
- D a vérifié S2P2.1, S6P1.1, le résultat DNS et la limite de comparaison (d071–d074). La vérification spécifique de S6P2.1 reste en cours à ce relevé : d083 ciblait par erreur un rapport VT, puis d084 a lu la page initiale des claims qui s’arrête avant S6P2.1. D a reconnu le mauvais objet et n’en a pas tiré de conclusion.

## Coût, taille et latence

- Le journal ne contient pas de champ de taille du prompt. La provenance d015 donne 284 514 octets de prompt transmis, 279 989 octets de dossier (SHA-256 `48ced7553b4b1bff669682830e71f1113e81c3b096e0d4ea2f16cb7d1f99fee9`) et 3 499 octets de réponse Bridge (sortie SHA-256 `3a22ed6bcf86c664667c0f4e7606301da9f117028804db735e0b330b87383e40`). Le fournisseur n’a pas donné d’usage facturable (`usage_unavailable`).
- D010 a duré 126 774 ms jusqu’à l’échec prétransport, zéro réponse; ce n’est pas une latence Bridge. D015 a une durée de soumission de 192 377 ms, parsing 203 ms, soit 192 589 ms avant manifeste. Le budget canonique passe à Bridge 2 consommés / 2 réservés / 196 restants et tool_seconds 583 / 7 200 / 78 617. Après d079, VT est à 31 / 0 / 9 969 et tool_seconds 584 / 7 200 / 78 616. Un tour D est consommé; les deux réservations restantes sont antérieures.
- L’observation persistée de la revue contient 800 661 caractères; D l’a récupérée en 50 pages contiguës HTTP 200, offsets conformes et SHA complet vérifié (`c3e6289d…c77211`), en ~65 secondes. C’est une forte surcharge : d015 contenait déjà les deux remarques parsées, même si la pagination complète permettait de contrôler la provenance `partial` et les manifestes. Aucun offset n’a été répété.

## Pivots et conduite

- Le pivot passif VT d079 sur l’IP du relais a réussi et consommé un unit. Il fournit des statistiques de rapport IP et un seul descriptor, sans relation de campagne ou nouvelle coassociation; D maintient H1 `qualified` et n’en fait pas une preuve négative.
- D a parcouru les pages 1–3/4 de `tool_coverage` et 1/5 de `available_pivots`; l’épuisement des autres pivots n’est pas établi. Le draft v3 demeure inchangé au CAS v64 à d082. Le traitement individuel des remarques Bridge et le choix des prochains pivots restent à observer.

| Dimension | Verdict provisoire | Preuve / limite |
|---|---|---|
| Pertinence / répétition | PARTIAL | Revue v3 et recherche passive IP pertinentes; pages d’observation toutes distinctes, mais récupération complète de 800 KB probablement évitable pour lire les deux remarques. |
| Bridge | PARTIAL | Un échec avant envoi, puis une reprise unique réussie. Deux remarques utilisables; usage fournisseur inconnu. Leur valeur est bonne, mais leur adoption n’est pas encore constatée. |
| Protocole | PARTIAL | Reprise seulement après non-exécution confirmée et délai; suivi sans doublon. Un GET CAS stale récupéré; d083 a visé le mauvais type d’observation puis a été corrigé sans inférence. |
| Arrêt prématuré | En cours | D poursuit; pivots et révision non terminés. |
| Qualité probatoire | PASS provisoire pour les remarques, intégration en cours | Les suggestions protègent la distinction source/observation et le statut `qualified`; d079 n’est pas traité comme preuve négative. |
| Isolation | PASS sur références et contenu revus; contrôle du prompt brut limité | Les sources E4/E5/E6/E12 sont des passages de l’article de cette enquête; 13 observations et les deux blocs v3 sont dans le manifeste. Le blob complet du prompt n’est pas directement lisible. |
| Frictions logicielles | PARTIAL | Une erreur prétransport, un CAS 409, un mauvais pointeur source et une observation de revue anormalement volumineuse. |
| Coût / latence | PARTIAL | Un tour Bridge et un unit VT confirmés; latence Bridge mesurée, usage fournisseur inconnu; deux anciennes réservations subsistent. |

Suite d’observation : vérifier comment D répond à chaque remarque (acceptée, adaptée, rejetée ou différée), puis si les pages restantes de pivots fournissent un résultat discriminant avant le checkpoint.
