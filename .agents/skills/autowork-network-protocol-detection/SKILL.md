---
name: autowork-network-protocol-detection
description: Reconstruire le protocole C2 depuis code statique et captures disponibles, puis produire et éprouver des signatures Suricata candidates sans exiger PCAP ou protocole complet.
---

# C2 et Suricata — produire au fil des preuves

**But :** obtenir un modèle partiel utile du C2, identifier les invariants de messages et, si possible, proposer une règle Suricata. Ce n'est pas une étape de validation imposée avant YARA ou les pivots. Aucune interaction live avec un C2 et aucune exécution de sample.

## Avant la rétro-ingénierie

Si l'article décrit mal les échanges et que le code est exploitable, demande tôt à Bridge (`plan_investigation`) où chercher les messages, leur sérialisation, les fonctions cryptographiques et les champs réseau utiles. Pose une question naturelle avec une phrase de contexte; le dossier est assemblé automatiquement et les références sont facultatives. Après l'analyse, fais proposer la section protocole avec `draft_text` et, si utile, un schéma avec `figure_plan`. Vérifie les faits, garde visibles les inconnues et intègre une prose naturelle. Les secrets embarqués utiles à l'analyse vont dans des annexes à accès contrôlé.

Après un modèle statique ou une capture mesurée, demande une critique des champs, séquences et incertitudes avec `suricata_draft` ou `plan_investigation`. Lis le texte brut avant son statut de parsing et vérifie chaque conseil utile sur les observations disponibles; renvoie les résultats à Bridge lorsqu’ils permettent une réévaluation. Il n’y a ni quota ni contrôle de forme entre échanges. Si Bridge est indisponible, ne lui attribue aucun texte, marque la section incomplète et reprends la collaboration lorsqu’il redevient disponible.

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
- Utiliser `advisor.consult` avec `suricata_draft` quand il existe un fait réseau ou un modèle C2 statique, sans attendre une PCAP. Dans une phrase, résumer les champs ou séquences observés et demander une règle candidate et sa limite; le dossier auto-assemblé conserve le contexte disponible. Une réponse en prose ou partiellement parsée reste exploitable.
- Si Bridge propose du code Suricata, l’importer immédiatement depuis l’observation de la consultation : `rule.import_suricata_bridge_candidate({"bridge_observation_id":"<UUID de l'observation advisor.consult>"})`. Ce seul champ est requis; l’import crée une candidate non validée et ne demande pas de références de mesure.
- **Si une capture associée et le moteur sont disponibles**, utiliser network.suricata_validate_rules puis network.suricata_replay ; vérifier le schéma exact, les alertes observées, erreurs et références. Ces outils acceptent une capture : ne pas inventer d'arguments pour lancer un test sans PCAP.
- **Sans PCAP**, conserver une règle candidate **non testée** avec ses fondements statiques ou documentaires ; ne pas fabriquer des hits. Le manque de capture ne bloque ni la rédaction ni la poursuite des autres axes CTI.
- Après la validation ou le replay, renvoyer les alertes, erreurs et limites à Bridge pour critique ou révision. Évite les consultations identiques si rien n’a changé.
- Les révisions sont motivées par de nouvelles observations ou faux positifs/faux négatifs mesurés, pas par une obligation de nombre de versions.

## Boucle vers la découverte

Utiliser les constantes, champs, séquences ou destinations distinctives pour choisir des pivots VT autorisés et comparer les nouveaux échantillons. Les nouvelles variantes alimentent le modèle C2, les règles Suricata et YARA.

## Sortie légère

Transport probable/observé ; messages/champs/transitions connus ; crypto et incertitudes ; observations exactes ; invariant réseau utilisable ; source et statut de la candidate Suricata (non testée, erreur, match mesuré) ; nouveau pivot pertinent.

**Oracle :** sans PCAP, l'opérateur décrit le protocole statique et peut conserver une candidate Suricata non testée. Avec une capture mesurable, il exécute la validation/replay autorisés et relie ses alertes aux observations.
