# Rapport intermédiaire — observation D, lot 004

Relevé au 2026-10-29 23:15:01 UTC. `FINAL_CHECKPOINT_D.md` est présent (SHA-256 `96226337ac8a51348eb4548704813f772d59a00a96e438ccfe384fe9402380dd`). Journal : 302 lignes, SHA-256 `f404281f428f4ab6faea5db8df65a3cc610b638439e0299ee2e0429dc2b1ed55`, dernière entrée d092 à 23:12:42Z; stable depuis ~2 min 19 s seulement. Le relais est vide. Le rapport final reste en attente de 15 minutes de stabilité.

## Fin de lot D

- D totalise 92 entrées : 87 GET et 5 POST. Les POST sont d010 (échec confirmé avant Bridge), d015 (revue Bridge partielle mais deux unités utilisables), d079 (VT réussi), d086 (reparse local partiel), d089 (révision d’avis réussie). Aucun appel ambigu, approval, publication ou repin.
- D a listé chaque remarque U1/U2 comme acceptée ou adaptée dans `operator_log.md`, sans rejet/différé, et a créé l’avis v4/SHA `e970d9a9…a0fd8`, toujours `draft`. Les findings H1–H3 restent `qualified`. La plupart des précisions sont bien intégrées : paramètre ETW et portée, date UTC/Paris, nuance de confiance Unit 42, limites de comparaison, formulation bornée sur les factures, et pistes explicitement futures.
- La revue Bridge d015 est valide pour l’avis v3 exact; elle ne couvre pas l’avis v4. Le checkpoint le dit explicitement. Aucune nouvelle revue de v4 n’a été faite.

## Réserve probatoire à suivre

- La phrase ajoutée à v4 « le nom du fichier ne relie pas à lui seul l’échantillon aux activités de CPX » est prudente et concorde avec le passage S6P2.1 de l’article, présent dans le dossier Bridge (alias E12). Toutefois, la trace v4/H3 consultée référence S6P1.1, le rapport VT du nom de fichier et la comparaison; elle ne référence pas S6P2.1. D083 a lu par erreur un rapport VT à la place du passage source; d084 a lu la page initiale des claims, qui ne contient pas S6P2.1. Le contenu n’apparaît donc pas faux, mais la provenance de cette précision CPX n’est pas établie dans la chaîne de citation canonique de v4. À clarifier par un pointeur explicite vers S6P2.1 ou en retirant cette précision.

## Arrêt et pivots restants

- D a ajouté une recherche passive VT d079 sur l’IP, qui n’a fourni qu’un descriptor et ne change pas H1. La section `available_pivots` n’a été lue que page 1/5 (83 entrées au total), et `tool_coverage` seulement pages 1–3/4. Le checkpoint ne démontre pas que les autres pivots proportionnés ont été examinés ou épuisés. Comme Bridge a finalement répondu et VT a réussi, l’arrêt ne découle pas d’un blocage externe. Évaluation provisoire : objectif Bridge v3 atteint et traité, mais « pivots restants » incomplet ou insuffisamment justifié.
- Le `advisor.reparse` local a retourné `unsupported_legacy_format`, sans nouvel artefact ni tour; D s’est correctement appuyé sur les deux unités originales, leurs SHA exacts et leurs statuts `usable=true`, sans resoumission.
- Le rapport final devra noter que le rappel canonique `advisor_checkpoint_due` reste actif mais non bloquant et que l’usage fournisseur monétaire demeure inconnu.

| Dimension | Verdict provisoire | Preuve / limite |
|---|---|---|
| Pertinence / répétition | PARTIAL | Revue et pivot IP utiles; une seule des cinq pages de pivots et trois des quatre pages de couverture lues. Aucune requête identique répétée. |
| Bridge | PARTIAL | Revue utilisable de v3, commentaires traités; v4 non revue. Prompt 284 514 octets, réponse 3 499, latence 192,377 ms; usage fournisseur inconnu. |
| Protocole | PARTIAL | Reprise après non-exécution confirmée, suivi sans doublon, CAS stale récupéré. Un mauvais pointeur source et un reparse legacy échoué sont correctement signalés. |
| Arrêt prématuré / blocage | PARTIAL | Aucun abandon sur erreur Bridge; reprise réussie. L’épuisement raisonnable des pivots n’est pas prouvé. |
| Qualité probatoire | PARTIAL | H1–H3 restent qualifiés et les remarques sont majoritairement ancrées; la précision CPX de v4 manque d’un pointeur canonique direct vers S6P2.1. |
| Isolation | PASS sur les artefacts visibles | Les alias article et références Bridge citent cette enquête; la mention CPX existe dans son article S6P2.1. Le prompt blob lui-même n’est pas directement lisible. |
| Frictions logicielles | PARTIAL | Échec Bridge initial, 409 CAS, observation de 800 661 caractères sur 50 pages, reparse legacy non pris en charge. |
| Coût / latence | PARTIAL | Un tour Bridge et un unit VT confirmés; latence mesurée; facturation fournisseur inconnue et deux réservations historiques restantes. |

Prochaine observation : confirmer que le journal reste inchangé après d092 pendant au moins 15 minutes; si D reprend, évaluer les nouvelles pages de pivots et toute correction de provenance sans intervenir sur l’API.
