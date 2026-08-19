# SUJETS CANDIDATS

## SUBJECT S1

title: TAG-182 — diffusion de MarkiRAT via faux VPN et outils multimédias
presentation: Recorded Future documente une infrastructure Iran-nexus utilisée pour diffuser MarkiRAT auprès de populations iraniennes et persanophones. La recherche fournit des échantillons, une chaîne d’infection, des IOC et des règles de détection.
actor-campaign: TAG-182 / MarkiRAT
technical-potential: 4
technical-reason: La publication expose des échantillons, une infrastructure C2, des IOC détaillés, une chaîne de diffusion, une règle YARA et une règle Sigma.
artifacts: ioc, samples, yara
uncertainty: Aucun service de sécurité iranien précis n’est attribué publiquement avec confiance; le lien avec Ferocious Kitten est présenté comme un rapprochement opérationnel et non comme une équivalence. ([Recorded Future][1])

### PUBLICATION P1

title: Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool
url: [https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat](https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-01
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, ipv4, domain
visible-iocs: 3b172281f65ceaee280ae810edb6fd39a1ecd25649f929f246c0405df94f4c89; 66dcd98c6b310f4429890821e609d48cc6395a6be15ffe5a121ec68b7a8f7402; 51a6686b8c5ec7c610637398f3de43589f4e9fcbe8bcc0245343c5454d3b91de; a4f1b79e96a7d016de1991a64506792018de99eac5df00f7cabe26ef41b2bd81; 400eb6a94810323a1fc5f8ab31c682fe765aaec2cc61b37c31d719c7e45c9a6c; 8a7f5c8533df9e51b2da7cc2aeb52d8787418e4915577cc9288be1e46d1945c6; 45[.]86[.]162[.]197; 46[.]30[.]191[.]105; 46[.]30[.]191[.]123; 212[.]83[.]61[.]198
publisher-ioc-count: unknown
ioc-note: L’annexe A rend visibles des domaines, des adresses IP et des SHA-256; une règle YARA MarkiRAT figure en annexe C. ([Recorded Future][1])

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: SentinelLABS distingue TAG-182 de Ferocious Kitten et indique qu’aucun sponsor n’est publiquement attribué avec confiance. ([SentinelOne][2])

## SUBJECT S2

title: Cavern Manticore — framework C2 modulaire Iran-nexus
presentation: Check Point décrit un framework modulaire utilisé contre des organisations israéliennes, notamment via des relations de confiance avec des prestataires IT et des outils RMM. Les éléments techniques présentent des recoupements avec des activités associées au MOIS.
actor-campaign: Cavern Manticore
technical-potential: 4
technical-reason: La recherche détaille architecture et modules du framework, configurations, post-exploitation, infrastructure C2, échantillons et IOC hôte/réseau.
artifacts: ioc, samples, configurations
uncertainty: Le rapprochement avec le MOIS repose sur des recoupements techniques et opérationnels; SentinelLABS qualifie ce lien de confiance modérée et note une attribution publique essentiellement mono-fournisseur. ([Check Point Research][3])

### PUBLICATION P1

title: Cavern Manticore: Exposing Iran-Linked Modular C2 Framework
url: [https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/](https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/)
publisher: Check Point Research
published-at: 2026-07-06
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, domain
visible-iocs: 37e123bd7998af4eae32718ce254776f36365a80ba56952593dab46f536d4066; 92cae0ad7f98f51a14bcc0ee05e372ebdc29ea96ea7bd161bd3f55198767603b; 5dc08bda6919a57a85e5f38b857985fa71529ca39c8299868d5a49a987e19b18; a4aa217def4c38f4ecacdf47b1cd687f60cc74c18ab75195be3c4357a790bf41; b630c96d3763182533d4fb9b614134382bd644cb02c6c1c3ade848b6ecc31e86; 8e9425c0b46eeb516610ae913d13f2b3f44a023043cb099277031d4ec38a6134; 0a3663648a46771a5a5423ad01e91a4e7ba825595e99fa934cb35cbb4848adc8; hospitalinstallation[.]com; auth[.]hospitalinstallation[.]com; google[.]com[.]hospitalinstallation[.]com
publisher-ioc-count: unknown
ioc-note: La section IOC contient notamment des SHA-256, domaines C2 et artefacts hôte; aucun total global explicite n’a été identifié. ([Check Point Research][3])

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La synthèse reprend Cavern Manticore comme activité d’espionnage utilisant prestataires et RMM et conserve une attribution MOIS à confiance modérée. ([SentinelOne][2])

## SUBJECT S3

title: Acteurs affiliés à l’Iran — exploitation de PLC Rockwell, Schneider et Siemens
presentation: La mise à jour de juillet de l’avis inter-agences américain étend la campagne visant des PLC exposés à Internet et documente manipulation de logique, exfiltration de projets et perturbations opérationnelles. Un plan de chasse publié le 24 juillet transforme ces éléments en IOC et détections directement exploitables.
actor-campaign: Iranian-affiliated APT actors targeting internet-facing PLCs
technical-potential: 4
technical-reason: Le corpus fournit 21 adresses IP publiées, procédures de chasse OT, règles YARA et Suricata et détails sur les manipulations PLC/HMI/SCADA.
artifacts: ioc, yara, suricata
uncertainty: L’avis ne démontre pas qu’un unique acteur réalise l’ensemble des compromissions; les rapprochements historiques avec CyberAv3ngers ne prouvent pas que chaque incident lui est imputable. ([1898advisories.burnsmcd.com][4])

### PUBLICATION P1

title: Threat Hunt Plan: Iran-Affiliated Targeting of Critical Infrastructure PLCs — Rockwell, Schneider, and Siemens OT Exploitation
url: [https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation](https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation)
publisher: 1898 & Co.
published-at: 2026-07-24
role: independent
ioc-visibility: visible
visible-ioc-types: ipv4
visible-iocs: 79.133.46.209; 84.200.205.165; 88.80.150.199; 88.80.150.200; 88.80.150.202; 135.136.1.133; 141.11.164.153; 175.110.121.39; 175.110.121.41; 175.110.121.42
publisher-ioc-count: 21
ioc-note: Le document indique explicitement un ensemble STIX de 21 IP et reproduit les 21 valeurs; il contient également des procédures de chasse et des règles YARA/Suricata. ([1898advisories.burnsmcd.com][4])

### PUBLICATION P2

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers Across US Critical Infrastructure
url: [https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a)
publisher: CISA et partenaires inter-agences américains
published-at: 2026-04-07
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: L’avis original date du 7 avril 2026 et a été révisé le 22 juillet 2026; la mise à jour de juillet est explicitement signalée dans les sources consultées. ([CISA][5])

### PUBLICATION P3

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La synthèse considère la campagne PLC inter-agences comme le dossier public le plus solide d’activité Iran-nexus contre l’OT américain, tout en appelant à distinguer accès d’interface et effet physique démontré. ([SentinelOne][2])

## SUBJECT S4

title: Incidents PLC dans le secteur américain de l’eau à partir du 27 juillet — attribution ouverte
presentation: Le FBI et l’EPA signalent des attaques contre des PLC Rockwell MicroLogix dans des services d’eau et d’assainissement de plusieurs États, avec effets opérationnels observés. Le sujet est conservé séparément de la campagne Iran-affiliated PLC car l’avis ne lui attribue pas publiquement ces incidents.
actor-campaign: unknown
technical-potential: 3
technical-reason: L’avis décrit les équipements visés, modifications observées et conséquences opérationnelles, mais ne fournit pas d’IOC ni d’artefact de détection.
artifacts: none
uncertainty: Aucune attribution à l’Iran n’est formulée par le FBI/EPA; la proximité temporelle et technique avec le dossier PLC iranien ne suffit pas à établir un lien. ([FBI][6])

### PUBLICATION P1

title: Malicious Cyber Actors Targeting Water and Wastewater Sector Internet- Facing Programmable Logic Controllers, Causing Operational Disruptions
url: [https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions](https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions)
publisher: Federal Bureau of Investigation / Environmental Protection Agency
published-at: 2026-07-30
role: primary
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: L’avis indique que des incidents sont signalés depuis le 27 juillet dans au moins sept États mais ne publie aucune valeur IOC. ([FBI][6])

## SUBJECT S5

title: MuddyWater/Seedworm — campagne d’espionnage T1 2026 sur quatre continents
presentation: Une publication du 1er juillet relaie une campagne MuddyWater observée au premier trimestre 2026, comprenant notamment une intrusion prolongée chez un fabricant électronique sud-coréen. L’analyse technique originale de mai fournit une chaîne d’activité détaillée et des IOC.
actor-campaign: Seedworm / MuddyWater
technical-potential: 4
technical-reason: La recherche originale détaille DLL side-loading, Node.js, PowerShell, ChromElevator, vol d’identifiants, tunneling, exfiltration et IOC fichiers/réseau.
artifacts: ioc, samples
uncertainty: La publication de juillet est un relais d’une activité antérieure; le rapport technique original disponible publiquement est daté de mai 2026. ([IT Security Guru][7])

### PUBLICATION P1

title: Iran-linked MuddyWater espionage campaign targets organisations across four continents
url: [https://www.itsecurityguru.org/2026/07/01/iran-linked-muddywater-espionage-campaign-targets-organisations-across-four-continents/](https://www.itsecurityguru.org/2026/07/01/iran-linked-muddywater-espionage-campaign-targets-organisations-across-four-continents/)
publisher: IT Security Guru
published-at: 2026-07-01
role: relay
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: Le relais décrit notamment DLL side-loading, Node.js, ChromElevator et sendit.sh mais n’affiche pas de valeur IOC. ([IT Security Guru][7])

### PUBLICATION P2

title: Seedworm: Iran-Linked Hackers Breached Korean Electronics Maker in Global Spying Campaign
url: [https://www.security.com/threat-intelligence/iran-seedworm-electronics](https://www.security.com/threat-intelligence/iran-seedworm-electronics)
publisher: Symantec and Carbon Black
published-at: 2026-05-12
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, ipv4, domain
visible-iocs: e25892603c42e34bd7ba0d8ea73be600d898cadc290e3417a82c04d6281b743b; c6182fd01b14d84723e3c9d11bc0e16b34de6607ccb8334fc9bb97c1b44f0cde; 128b58a2a2f1df66c474094aacb7e50189025fbf45d7cd8e0834e93a8fbed667; 0c9b911935a3705b0ad569446804d80026feb6db3884aeb240b6c76e9b8cf139; 74ab3838ebed7054b2254bf7d334c80c8b2cfec4a97d1706723f8ea55f11061f; 3ee7dab4ae4f6d4f16dfabb6f38faef370411a9fc00ff035844e54703b99600a; bee79c3302b1a7afc0952842d14eff83a604ef00bfdae525176c16c80b2045f7; d587959841a763669279ad831b8f0379f6a7b037dffc19deab5d41f37f8b5ffc; 179.43.177[.]220; timetrakr[.]cloud
publisher-ioc-count: unknown
ioc-note: La section IOC publie des indicateurs fichiers et réseau; aucun total global explicite n’est annoncé. ([Security.com][8])

## SUBJECT S6

title: MuddyWater/Seedworm — Dindoor et Fakeset dans des réseaux américains
presentation: KELA remet en avant en juillet l’activité Seedworm de février-mars 2026 ayant touché notamment une banque et un aéroport américains ainsi que les opérations israéliennes d’un éditeur américain. Le rapport technique original de mars décrit Dindoor, Fakeset, les mécanismes de persistance et l’exfiltration.
actor-campaign: Seedworm / MuddyWater — Dindoor / Fakeset
technical-potential: 4
technical-reason: Le rapport original fournit deux familles de backdoor, infrastructure de téléchargement, certificats de signature, exfiltration Rclone/Wasabi et une liste de SHA-256.
artifacts: ioc, samples
uncertainty: La publication KELA de juillet est une synthèse d’acteur et non la découverte initiale de la campagne; elle couvre aussi d’autres opérations MuddyWater qui ne sont pas fusionnées ici. ([KELA Cyber Threat Intelligence][9])

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: MuddyWater
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication relie explicitement Dindoor aux compromissions américaines de début 2026 et traite séparément d’autres campagnes, notamment Operation Olalampo. ([KELA Cyber Threat Intelligence][9])

### PUBLICATION P2

title: Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company
url: [https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us](https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us)
publisher: Symantec and Carbon Black
published-at: 2026-03-05
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, domain
visible-iocs: 0f9cf1cf8d641562053ce533aaa413754db88e60404cab6bbaa11f2b2491d542; 1d984d4b2b508b56a77c9a567fb7a50c858e672d56e8cf7677a1fca5c98c95d1; 2a00705cfd3c15cf8913e9eb4e23968efd06f1feceaef9987d26c5518887d043; 2a09bbb3d1ddb729ea7591f197b5955453aa3769c6fb98a5ef60c6e4b7df23a5; 42a5db2a020155b2adb77c00cbe6c6ad27c2285d8c6114679d9d34137e870b3f; 7467f326677a4a2c8576e71a832e297e794ea00e9b67c4fcbe78b5aec697cec4; 7c30c16e7a311dc0cdb1cdfd9ea6e502f44c027328dbe7d960b9bcd85ccf5eef; b0af82de672d81f3c2f153977923b3884a8a9e7045b182c2379b19a1996931a0; gitempire.s3.us-east-005.backblazeb2.com; elvenforest.s3.us-east-005.backblazeb2.com
publisher-ioc-count: unknown
ioc-note: La publication comporte une section IOC avec de nombreux SHA-256 Dindoor/Fakeset; les deux domaines Backblaze sont explicitement présentés comme serveurs de téléchargement de Fakeset dans l’analyse. ([Security.com][10])

### PUBLICATION P3

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: SentinelLABS cite explicitement l’accès Seedworm/MuddyWater de février aux réseaux d’une banque, d’un aéroport, de nonprofits et d’un fournisseur logiciel américain. ([SentinelOne][2])

## SUBJECT S7

title: APT42 — SpearSpecter et modèle d’espionnage centré sur la relation de confiance
presentation: KELA publie en juillet une synthèse d’APT42 mettant notamment en avant l’opération SpearSpecter et les compromissions d’identités et de comptes cloud. Le sujet est retenu comme recherche d’acteur plutôt que comme nouvelle campagne découverte en juillet.
actor-campaign: APT42 / SpearSpecter
technical-potential: 2
technical-reason: La publication fournit un ensemble utile de TTP portant sur le rapport-building, le spear-phishing multicanal, TAMECAT, OAuth, les règles de boîte aux lettres et les C2 via services légitimes.
artifacts: none
uncertainty: L’article couvre plusieurs activités APT42 et SpearSpecter n’est pas présenté comme une campagne nouvellement apparue en juillet; certains alias mentionnés sont des clusters qui se chevauchent plutôt que des équivalences strictes. ([KELA Cyber Threat Intelligence][11])

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT42
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La page visible fournit des TTP et plusieurs campagnes nommées mais aucun jeu d’IOC explicite n’a été identifié. ([KELA Cyber Threat Intelligence][11])

## SUBJECT S8

title: Prince of Persia / Infy — résilience C2 et outillage 2025-2026
presentation: KELA synthétise l’évolution récente d’Infy, notamment la synchronisation observée entre son infrastructure C2 et la coupure Internet iranienne de janvier 2026. La recherche rassemble également les familles Foudre, Tonnerre, Tornado et ZZ Stealer.
actor-campaign: Prince of Persia / Infy / APT-C-07
technical-potential: 3
technical-reason: Le profil contient des éléments exploitables sur les chaînes SFX, familles de malware, Telegram C2, DGA blockchain, exploitation WinRAR et contre-mesures anti-recherche.
artifacts: none
uncertainty: Il s’agit principalement d’une synthèse de recherches antérieures; aucun service iranien précis n’est publiquement attribué à l’acteur dans cette source. ([KELA Cyber Threat Intelligence][12])

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: Prince of Persia (Infy)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La page visible décrit l’outillage et les TTP récents, sans jeu d’IOC explicitement publié dans le contenu consulté. ([KELA Cyber Threat Intelligence][12])

## SUBJECT S9

title: APT34 / OilRig — espionnage centré sur identité, Exchange et living-off-the-land
presentation: KELA publie en juillet un profil technique d’APT34 axé sur sa capacité à conduire des intrusions longues avec peu de malware et à exploiter l’infrastructure légitime de la victime. Une activité de fin 2024 autour d’une DLL de filtre de mots de passe est utilisée comme exemple récent.
actor-campaign: APT34 / OilRig
technical-potential: 2
technical-reason: La recherche consolide des TTP d’espionnage autour des identifiants, d’Exchange, du living-off-the-land et des mécanismes de persistance, mais sans IOC visible.
artifacts: none
uncertainty: La publication est une synthèse d’acteur et non l’annonce d’une nouvelle campagne de juillet; les recoupements avec d’autres acteurs iraniens ne constituent pas des alias univoques. ([KELA Cyber Threat Intelligence][13])

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT34 (OilRig)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: Aucun chapitre IOC ni SHA-256 n’est visible dans le contenu consulté; la valeur principale de la publication est la consolidation des TTP. ([KELA Cyber Threat Intelligence][13])

## SUBJECT S10

title: Usage de l’IA par les opérations cyber iraniennes — recherche transversale 2026
presentation: Recorded Future analyse l’emploi de l’IA comme accélérateur de capacités cyber iraniennes existantes plutôt que comme nouveau mode opératoire autonome. La publication recoupe plusieurs acteurs et campagnes sans les présenter comme une seule opération.
actor-campaign: multiple Iran-linked actors
technical-potential: 2
technical-reason: La recherche relie l’assistance IA à la génération de code, au phishing, au rapport-building et à plusieurs familles/campagnes iraniennes, mais ne fournit pas de jeu d’IOC.
artifacts: none
uncertainty: Une partie des indices d’assistance IA est indirecte ou issue de publications tierces; le rapport distingue explicitement accélération de capacités et création de capacités nouvelles. ([Recorded Future][14])

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La publication cite notamment MuddyWater/CHAR, RedKitten, Dust Specter/APT34, Nimbus Manticore et APT42 comme cas d’usage ou indices d’assistance IA, sans publier de jeu d’IOC. ([Recorded Future][15])

# LIMITES

La recherche est limitée aux publications Web publiquement indexées et accessibles jusqu’au 2026-08-17 inclus; aucun contenu publié après cette date n’a été recherché. Certaines pages officielles, notamment l’avis CISA AA26-097A, étaient moins directement exploitables que leurs reprises techniques, de sorte que les valeurs IOC n’ont pas été attribuées à CISA lorsqu’elles n’étaient pas explicitement visibles dans la représentation consultée. Les publications de synthèse de SentinelLABS, KELA et Recorded Future couvrent davantage de campagnes que celles retenues ici; les campagnes n’ont pas été fusionnées sur cette seule base. Les sujets KELA APT42, Prince of Persia et APT34 sont conservés comme recherches d’acteur publiées dans la période, même lorsque les activités techniques qu’elles récapitulent sont antérieures à juillet. Le sujet des incidents eau/assainissement du 27-30 juillet est volontairement séparé du dossier Iran-affiliated PLC, car aucune attribution iranienne n’est formulée dans l’avis FBI/EPA consulté.

[1]: https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat "Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool"
[2]: https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/ "Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters | SentinelOne"
[3]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/ "Cavern Manticore: Exposing Iran-Linked Modular C2 Framework - Check Point Research"
[4]: https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation "Threat Hunt Plan: Iran-Affiliated Targeting of Critical Infrastructure PLCs — Rockwell, Schneider, and Siemens OT Exploitation"
[5]: https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a?utm_source=chatgpt.com "Iranian-Affiliated Cyber Actors Exploit Programmable Logic ..."
[6]: https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions "Malicious Cyber Actors Targeting Water and Wastewater Sector Internet- Facing Programmable Logic Controllers, Causing Operational Disruptions — FBI"
[7]: https://www.itsecurityguru.org/2026/07/01/iran-linked-muddywater-espionage-campaign-targets-organisations-across-four-continents/ "WatchGuard Geopolitical Cyber Report - IT Security Guru"
[8]: https://www.security.com/threat-intelligence/iran-seedworm-electronics "Seedworm: Iran-Linked Hackers Breached Korean Electronics Maker in Global Spying Campaign | SECURITY.COM"
[9]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/ "MuddyWater in 2026: Iran's APT Hits U.S. Targets"
[10]: https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us "Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company | SECURITY.COM"
[11]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/ "APT42: Iran's Human-Centric Espionage in 2026"
[12]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/ "Prince of Persia (Infy): Iran's Persistent APT"
[13]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/ "APT34 (OilRig): Espionage on Your Infrastructure"
[14]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook "AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict"
[15]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook?utm_source=chatgpt.com "AI Has Enhanced Iran's Asymmetric Playbook During ..."

