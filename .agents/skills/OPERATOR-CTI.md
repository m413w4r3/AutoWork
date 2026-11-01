# AutoWork — skills de l'opérateur CTI (analyse et détection)

Ce registre est destiné à l'orchestrateur qui prépare une session CTI. Les skills ne sont **pas** des preuves, ne remplacent pas le playbook et ne donnent aucune permission. Le registre réel d'outils et l'enveloppe de CETTE enquête sont toujours prioritaires.

| Déclencheur précis | Skill |
| --- | --- |
| Nouvelle question / expérience / arrêt | autowork-cti-investigation-loop |
| Choisir un outil, budget, refus, CAS, besoin de bytes | autowork-tool-strategy |
| Nouvel artefact, type inconnu, archive, script | autowork-artifact-triage |
| Nouvelle relation/hash/IOC/similarité ; quel prochain test ? | autowork-cti-pivot-strategy |
| Observation informative → finding exact | autowork-evidence-to-finding |
| PE managé / metadata .NET / IL / AppDomainManager | autowork-dotnet-analysis |
| PE natif / loader / packing / Ghidra | autowork-pe-native-analysis |
| Proposition, test, comparaison, révision YARA avec Bridge | autowork-yara-bridge-workflow |
| Protocole C2, champs réseau, signature Suricata candidate | autowork-network-protocol-detection |

**Ne pas précharger les neuf SKILL.md intégralement.** L'opérateur lit uniquement les guides utiles. Une inspection simple n'exige pas de charger systématiquement la boucle, le guide d'outils et plusieurs spécialistes.

**Priorité métier : invariants → YARA/Suricata candidates → description C2 → pivots VT → nouveaux échantillons → révision des règles.** Ni nombre minimal d'hypothèses/tests/pivots, ni goodware/holdout obligatoire pour la première candidate, ni PCAP indispensable au modèle statique du protocole. Les garde-fous du backend et les autorisations courantes demeurent l'autorité.

Les compétences YARA/Bridge et les analyses PE ne sont pas des motifs de collecte externes implicites. Pour la sûreté et la preuve, utiliser aussi les skills existants autowork-cti-evidence-gate et autowork-bridge-dossier-review lors de la revue indépendante, sans leur confier de mutation.

## Isolation de l'enquête

Le code AutoWork et les consignes des **agents de développement Paseo** ne sont pas automatiquement accessibles à l'opérateur CTI isolé. L'orchestrateur peut copier uniquement les neuf guides génériques dans le répertoire autorisé de la nouvelle session via scripts/ops/stage_operator_cti_skills.py, avant le démarrage. Aucun journal, mémoire, hash d'enquête, finding ou compte rendu antérieur n'est copié. Dans le brief de session, autoriser explicitement la lecture du répertoire de skills déposé ; ne pas autoriser l'accès au dépôt général, aux secrets ou aux autres runs.

Pour Claude Code, les nouveaux dossiers sont également référencés par symlinks de dépôt dans .claude/skills ; cela ne prouve PAS que Paseo ou le runtime CTI les ont chargés. Vérifier réellement l'inventaire et la lecture dans une session propre. Voir docs/operations/operator-skills.md.

## Non-objectifs

Pas de nouveau moteur agentique, de modifications de contrat G2, de contournement de CAS, d'accès live non autorisé ni d'extraction/exécution de malware. Les guides spécialisés ELF, Office/document et archives profondes restent à traiter ultérieurement ; le guide C2/Suricata est ajouté dès maintenant pour répondre aux objectifs métier.