# Lot 004 — Bridge toujours en vol

Instantané : 2026-10-29 17:43:28 UTC. Aucun nouvel appel d’observateur; aucun POST Bridge répété.

## Faits

- `c100-bridge-review-opinion-v2` reste dans `relay-status.json` avec état `running`, début relay `2026-10-29T17:28:51.908282Z`, sans demande en queue. Au heartbeat du 17:43:25.019259Z, le temps écoulé est 873.111 s (14 min 33.111 s); aucune réponse Bridge n’est encore archivée dans le journal ou l’outbox.
- C a fait une seule lecture de suivi `c101-status-bridge-running`, GET `/operator-status`, réponse HTTP 200 et état v56 inchangé, draft v2 toujours `draft`. Le journal indique explicitement que le GET s’est terminé pendant que `c100` restait en vol. Le dernier événement d’API achevé est `c101` à 17:36:03.427255Z; depuis, aucun nouveau POST ou GET opérateur n’est consigné.
- Le statut `c101` indique encore Bridge 1 consommé / 0 réservé / 199 restant, soit inchangé depuis le préflight `c099`. Ce n’est pas une mesure d’usage du POST en cours; ni tour consommé confirmé pour `c100`, ni usage fournisseur, ni taille de requête exacte n’est disponible à cet instant.
- `FINAL_CHECKPOINT_C.md` reste absent. Le relais a une requête en vol et une file vide; le journal a bougé depuis le snapshot précédent, donc le critère 15 min d’inactivité n’est pas satisfait.

## Inférences provisoires

- La latence est anormale par rapport aux appels courts précédents, mais reste un appel `running`, pas un refus avant transport, un échec final ou un résultat `needs_review`. C respecte le protocole en consultant le statut une fois sans resoumettre l’avis.
- L’absence de réservation Bridge dans le compteur pendant l’appel en vol crée une ambiguïté de coût/état à résoudre avec le résultat final ou le prochain statut canonique. Ne pas compter ce tour comme consommé ni comme annulé avant cette preuve.
- Aucune décision de clôture prématurée n’est observée; l’avis demeure brouillon et aucune publication n’est mentionnée.

Sources : entrée de journal `c101`, `outbox/c101-status-bridge-running.json`, `relay-status.json` (instantané seulement). SHA du journal au relevé précédent : `e0d766a361d0febca5c356417b97f1877b4fb11d0d37bab00641c1d3159f35e9`.
