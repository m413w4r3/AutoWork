---
name: autowork-network-protocol-detection
description: Reconstruire le protocole C2 depuis code statique et captures disponibles, puis produire et éprouver des signatures Suricata candidates sans exiger PCAP ou protocole complet.
---

# C2 et Suricata — produire au fil des preuves

**But :** obtenir un modèle partiel utile du C2, identifier les invariants de messages et, si possible, proposer une règle Suricata. Ce n'est pas une étape de validation imposée avant YARA ou les pivots. Aucune interaction live avec un C2 et aucune exécution de sample.

## Avant la rétro-ingénierie

Si l'article décrit mal les échanges et que le code est exploitable, c'est une branche prioritaire. Demander d'abord à Bridge (`plan_investigation`, avec les méthodes, données et octets déjà acquis) quelles méthodes cibler, où chercher les messages et leur sérialisation, quelles fonctions cryptographiques comprendre, quels champs serviraient une signature réseau et quels tests passifs départageraient les hypothèses. Après l'analyse, faire rédiger par Bridge (`draft_text`, `figure_plan`) la section protocole : le schéma ne contient que les étapes établies et rend les inconnues visibles, sans message ni transaction inventés. Les secrets embarqués utiles à l'analyse vont dans des annexes à accès contrôlé.

Après avoir reconstruit un modèle statique ou mesuré une capture, demander à Bridge (`plan_investigation` ou `suricata_draft`) une critique des champs, séquences et incertitudes ; l'opérateur vérifie les conseils sur les observations passives disponibles, puis Bridge réévalue les résultats utiles. Pour chaque section du livrable, demander un `draft_text` puis un `review_text` si une relecture apporte de la valeur. Ne pas transformer une seule consultation de cadrage en rédaction autonome de tout le rapport ; si Bridge est indisponible, signaler la section incomplète et reprendre après diagnostic.

## Sans capture : la voie statique est utile

À partir de code PE/.NET/ELF, strings décodées et ressources, inspecter sélectivement :
- Transport et appels réseau possibles ; client/serveur et destinations codées.
- Sérialisation, framing, longueur, délimiteurs, encodages, constantes et dispatch de commandes.
- Clés/IV/nonce et transformations observables, compression et conditions d'usage.
- Ordre de construction et parsing des messages ; états et séquences possibles.

Pour chaque propriété, distinguer **code/constante observés**, **chemin atteignable supposé** et **trafic capturé**. Un transport apparent dans un import ne confirme pas un protocole exécuté. Fournir le modèle partiel plutôt que déclarer tout « bloqué sans PCAP ».

## Avec capture ou rapport passif

Si le runtime expose une capture **déjà associée et autorisée** :
1. network.capture.inspect pour la couverture et les flows.
2. network.stream.reassemble et network.messages.segment pour vérifier frontières et octets observés.
3. network.protocol.hypothesize/compare/verify/document/render si le contrat et les données le justifient. Une représentation du protocole est facultative.
4. Confronter les champs capturés aux fonctions statiques ; chercher invariants stables, variations et contre-exemples.

Les rapports VT behaviour ou PCAP sont des indices/références de provenance ; ne pas inférer du trafic non observé.

## Construire une candidate Suricata

- Chercher un invariant **visible sur le réseau** : sens du flux, framing, contenu d'un champ, séquence d'octets observée, particularité de requête/réponse. Une valeur interne chiffrée mais jamais visible en clair sur le fil ne doit pas devenir un motif Suricata présumé.
- Choisir un contexte et une condition de règle proportionnés. Éviter de matcher seulement un protocole ou un header banal ; documenter la portée et le risque de faux positifs.
- Utiliser `advisor.consult` avec `suricata_draft` lorsque l'on dispose de faits réseau **ou d'un modèle C2 statique documenté** ; ne pas attendre une PCAP pour une candidate. Le dossier doit conserver endpoints et séquences, formats de message, chaînes de protocole et **octets/valeurs utiles à la signature s'ils existent**, ainsi que les références de preuves et la distinction `documented_not_observed` / `observed` ; demander une critique du bruit/faux positifs et une proposition de règle en prose et bloc de code si pertinent, jamais du JSON. Une réponse en prose peut être utilisable même sans en-tête de format rigide.
- **Si une capture associée et le moteur sont disponibles**, utiliser network.suricata_validate_rules puis network.suricata_replay ; vérifier le schéma exact, les alertes observées, erreurs et références. Ces outils acceptent une capture : ne pas inventer d'arguments pour lancer un test sans PCAP.
- **Sans PCAP**, conserver une règle candidate **non testée** avec ses fondements statiques ou documentaires ; ne pas fabriquer des hits. Le manque de capture ne bloque ni la rédaction ni la poursuite des autres axes CTI.
- Les révisions sont motivées par de nouvelles observations ou faux positifs/faux négatifs mesurés, pas par une obligation de nombre de versions.

## Boucle vers la découverte

Utiliser les constantes, champs, séquences ou destinations distinctives pour choisir des pivots VT autorisés et comparer les nouveaux échantillons. Les nouvelles variantes alimentent le modèle C2, les règles Suricata et YARA.

## Sortie légère

Transport probable/observé ; messages/champs/transitions connus ; crypto et incertitudes ; observations exactes ; invariant réseau utilisable ; source et statut de la candidate Suricata (non testée, erreur, match mesuré) ; nouveau pivot pertinent.

**Oracle :** sans PCAP, l'opérateur décrit le protocole statique et peut conserver une candidate Suricata non testée. Avec une capture mesurable, il exécute la validation/replay autorisés et relie ses alertes aux observations.
