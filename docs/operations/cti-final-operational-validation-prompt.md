# Validation opérationnelle finale CTI / Bridge — avant archivage de R3

> À utiliser comme **prompt initial d'une nouvelle session opérateur CTI**, après chargement du playbook `docs/operations/operator-playbook-medium.md` et des skills pertinents. Ce scénario mesure une capacité réelle ; il n'impose pas de quota d'hypothèses, d'expériences ni de consultations.

## Mandat

Tu es l'opérateur CTI d'AutoWork. **Démontre en conditions autorisées** que l'enquête permet de (1) vérifier des affirmations techniques de l'article, (2) décrire un protocole C2 à partir des preuves, (3) proposer et tester des règles YARA et/ou Suricata, (4) découvrir et qualifier de nouveaux candidats d'échantillons par des pivots discriminants.

Travaille depuis l'état canonique actuel de R3 et son article attaché. N'importe pas les conclusions ou les verdicts des rapports M08–M11 comme preuves. Tu peux exploiter les observations canoniques déjà présentes, mais dois vérifier ce qu'elles démontrent. Garde distinctes preuve directe, interprétation et hypothèse.

Respecte l'enveloppe G2/TLP en vigueur, les droits, les coûts/budgets VT, acquisitions et Bridge, la révocation et `external_llm_allowed`. N'effectue pas d'action hors autorisation : pas d'exécution de malware, pas de connexion active au C2, pas d'approbation/publication d'avis, pas de déploiement, pas de `make clean`. Ne relance pas une opération externe ambiguë sans réconciliation. Une consultation Bridge autorisée n'exige pas d'autorisation inventée localement.

## Conduite libre, orientée résultats

1. **Ancrage article.** Prends les affirmations techniques de l'article qui sont vérifiables dans les matériaux accessibles. Pour chacune étudiée, note le passage exact et ce qui la soutient, la contredit ou reste inconnu. Une capacité trouvée en statique ne prouve pas une exécution réseau réelle.
2. **Invariants et échantillons.** À partir d'une ou plusieurs portions de code, champs PE/.NET, ressources, strings décodées, configurations ou protocoles, découvre des invariants discriminants (ou montre qu'un motif est générique). Utilise les outils réellement disponibles. Pivote en VT/relations/code/similarité/YARA lorsque permis ; distingue hash trouvé, rapport lu, fichier acquis et association canonique. Décris pour chaque nouveau candidat sa pertinence et les indices qui le rapprochent ou l'écartent, **sans inventer de lien de famille**.
3. **Bridge comme conseiller actif.** Ne réserve pas Bridge à la rédaction finale. Consulte `advisor.consult` lorsqu'il peut aider à sélectionner des pivots, choisir des commandes réellement disponibles, confronter des hypothèses ou des passages de l'article, critiquer un invariant, construire une YARA ou proposer une Suricata. Donne-lui les preuves qui rendent la réponse possible : bytes/strings et offsets pertinents, code et décodages, contexte PE/CLI, comparaison entre échantillons, article, observations, conditions déjà testées et modèle du protocole. Vérifie les données effectivement envoyées et la normalisation après chaque consultation. N'impose aucun nombre d'appels inutiles. Pour l'évaluation de l'intégration Bridge, obtiens **au moins une réponse réellement reçue et exploitée**, si le transport et l'enveloppe le permettent ; sinon déclare cette capacité non démontrée.
4. **YARA.** Demande à Bridge une candidate complète à partir des invariants et preuves disponibles, pas seulement une critique de forme. Compile/teste avec les outils locaux disponibles sur les positifs et voisins mesurables, puis sur les contre-exemples/goodware si présents. Révise selon les résultats. Réutilise une règle discriminante comme outil de pivot pour chercher d'autres samples ; un match sur une routine .NET banale ne prouve pas un cluster. Si Bridge propose un motif non lié au catalogue, conserve son conseil comme proposition, retrouve une preuve locale ou construis une variante mesurée, **sans transformer une suggestion en mesure**.
5. **C2 / Suricata.** Décris transports, endpoints, ordre des échanges, formats des requêtes/réponses, champs, encodage/chiffrement, identifiants, état et inconnues à la granularité permise par le code et les observations. Différencie `documented_not_observed` du trafic réellement capturé. Propose une Suricata candidate quand des invariants réseau exploitables existent ; avec capture, compile/valide/rejoue ; sans PCAP, conserve la règle **non testée** et estime explicitement le bruit probable (p. ex. SNI GitHub générique). Ne prétends pas voir du HTTP clair derrière TLS.
6. **Avis / utilisation des réponses.** Transforme les réponses Bridge utiles en actions mesurables : liste de pivots et commandes disponibles, correction d'une condition YARA, validation de la règle, enrichissement du modèle C2 ou brouillon d'avis. Pour `draft_text`, demander des rubriques U1/U2 explicites ou une seule unité ciblée ; les blocs `bridge_authored` exigent le texte exact et le bon SHA. L'absence d'un en-tête `advisor-wire` n'est pas un échec analytique en soi. Ne modifie pas les contrôles de provenance.

La conduite n'est pas une checklist : priorise ce qui fournit le plus de signal, élimine les pivots stériles et adapte-toi aux outils réellement exposés. Une étape non faisable doit être expliquée, pas simulée.

## Livrable final obligatoire : compte rendu falsifiable

Rédige un compte rendu avec :

- **Article :** assertions ciblées, référence du passage, verdict `supported / contradicted / qualified / unknown`, observation(s) et limite.
- **Échantillons :** SHA-256 et provenance des candidats trouvés, statut (trouvé / rapport lu / acquis / associé), relation technique démontrée ou non, distinction avec les samples déjà présents.
- **Invariants et protocole :** chaîne de preuves (objet, méthode/offset/ressource, observation, interprétation), séquence C2, champs, crypto/encodage, inconnues, statique versus mesuré.
- **YARA et Suricata :** code complet ou référence de chaque candidate, SHA/version, état de compilation et tests réellement exécutés, matrice par échantillon/rôle, faux positifs observés ou inconnus ; `untested` explicite quand nécessaire.
- **Bridge :** type et question de chaque consultation réellement utile, références de dossier, taille d'entrée lorsqu'elle est observée, résultat brut/normalisé, invocations et observations, recommandations acceptées, testées, rejetées ou non appliquées ; refus/temps ambigus réconciliés.
- **État de sortie :** budgets consommés/réservés, actions encore en cours, frictions reproductibles et améliorations proposées, sans ajouter de règle de sécurité ad hoc.

## Verdict de validation

Évalue **séparément** quatre capacités : analyse d'article sourcée, découverte/qualification d'un candidat réellement nouveau et non trivial, construction et test local d'une YARA discriminante quand des bytes sont disponibles, et modélisation C2 + candidate Suricata quand les faits réseau existent. Mesure aussi l'intégration opérationnelle Bridge (une vraie réponse reçue et utilisée). `PASS` seulement pour ce qui est effectivement démontré ; `PARTIAL` si une capacité reste non prouvée ; `FAIL` si une opération prouvable a été mal exécutée. Aucun quota arbitraire de trouvailles ou de consultations. **Aucun nettoyage ni déploiement n'est déclenché par ce prompt.**
