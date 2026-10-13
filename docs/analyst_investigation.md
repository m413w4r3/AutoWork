# Enquête de l’analyste

Les statuts persistés restent `queued`, `running`, `awaiting_review`,
`completed`, `exhausted`, `failed` et `cancelled`. Les valeurs persistées de
`AnalystInvestigationStage` restent également inchangées (`seeds`, `features`,
`tooling`, `invariants`, `pivots`, `corpus`, `detection`, `note`). Le stage
courant est une information d’affichage ; il ne conditionne ni l’accès à un
outil ni l’ordre des recherches.

`AnalystInvestigation.record_turn(progress=...)` comptabilise un tour Bridge et
signale si le budget de tours configuré est atteint. Le plafond `max_cycles`
sert de budget de tours Bridge ; il ne termine pas lui-même le travail sur les
autres dimensions. `finish_cycle(validated_new_members=...)` reste disponible
pour les anciens appelants, mais zéro nouveau membre de corpus ne provoque plus
le statut `exhausted`. Un progrès peut être une chaîne d’infection précisée,
une observation réseau ou une affirmation de l’article vérifiée.

L’arrêt est une décision typée de l’agent ou une limite locale : l’agent peut
terminer, demander une permission, ou la boucle peut enregistrer un budget
épuisé, l’absence répétée de nouvelle information, une interruption ou une
indisponibilité du modèle. `AnalystInvestigation.conclude(StopReason)` projette
ces décisions sur les statuts persistés existants. Une permission manquante
met l’enquête en revue ; une panne technique seule utilise `failed`.

`LoopBudget` conserve ses champs persistés : tours (`max_cycles`), pivots,
acquisitions, nouveaux samples et unités VT. Le budget d’outils A1 ajoute des
dimensions séparées (tours Bridge, unités VT, crédits Shodan, téléchargements,
temps outil et taille du contexte) et réserve les coûts avant exécution.

La production documentaire garde son pipeline indépendant : `SOURCES` →
`REFERENCES` → `EXTRACTION` → `RELEVANCE_PROJECTION` → `SYNTHESIS` →
`EDITORIAL_ENRICHMENT` → `ASSEMBLY`. Une enquête est lancée explicitement à
partir d’une synthèse vérifiée et ne devient pas une étape de production.

`AnalystDecision` reste append-only et liée à l’enquête. Elle conserve le type
de décision, sa cible exacte, l’acteur, le motif, le `correlation_id` et la date ;
elle n’étend pas `HumanDecision`.
