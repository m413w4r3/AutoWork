# SUJETS CANDIDATS

## SUBJECT S1

title: TAG-182 diffuse MarkiRAT pour la surveillance d’utilisateurs iraniens
presentation: Insikt Group décrit une infrastructure nouvellement identifiée de TAG-182 distribuant MarkiRAT via de faux VPN, lecteurs multimédias et autres leurres à destination d’utilisateurs persanophones. SentinelLABS reprend cette activité dans son bilan de juillet comme un cluster de surveillance iranien sans sponsor organisationnel attribué avec confiance. ([Recorded Future][1])
actor-campaign: TAG-182 / MarkiRAT
technical-potential: 4
technical-reason: La publication fournit une chaîne de distribution, des échantillons identifiés par hash, de l’infrastructure C2, des TTP et une règle YARA.
artifacts: [ioc, yara]
uncertainty: Lien avec Ferocious Kitten jugé plausible mais non démontré comme identité organisationnelle ; aucun service de sécurité iranien précis n’est attribué.

### PUBLICATION P1

title: Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool
url: [https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat](https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-01
role: primary
ioc-visibility: visible
visible-ioc-types: [SHA-256, IPv4, domain]
visible-iocs: [3b172281f65ceaee280ae810edb6fd39a1ecd25649f929f246c0405df94f4c89, 66dcd98c6b310f4429890821e609d48cc6395a6be15ffe5a121ec68b7a8f7402, 51a6686b8c5ec7c610637398f3de43589f4e9fcbe8bcc0245343c5454d3b91de, a4f1b79e96a7d016de1991a64506792018de99eac5df00f7cabe26ef41b2bd81, 400eb6a94810323a1fc5f8ab31c682fe765aaec2cc61b37c31d719c7e45c9a6c, 8a7f5c8533df9e51b2da7cc2aeb52d8787418e4915577cc9288be1e46d1945c6, 212[.]83[.]61[.]198, yeplayer[.]store, yemplayer[.]site, comi-site[.]website]
publisher-ioc-count: unknown
ioc-note: Des IOC sont visibles dans le corps et l’Appendix A ; l’Appendix C contient une règle YARA et l’Appendix D une règle Sigma. ([Recorded Future][1])

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La synthèse classe TAG-182 comme cluster de surveillance de dissidents et de diaspora et distingue explicitement ses labels des groupes apparentés ; aucune liste d’IOC n’est fournie dans le contenu visible. ([SentinelOne][2])

## SUBJECT S2

title: Cavern Manticore et le framework C2 modulaire Cavern
presentation: Check Point Research décrit un framework post-exploitation .NET modulaire observé chez Cavern Manticore contre des organisations israéliennes, notamment via des fournisseurs IT et des accès RMM existants. SentinelLABS considère le nexus iranien pertinent mais qualifie le lien MOIS de confiance modérée et dépendant d’un reporting fournisseur unique. ([Check Point Research][3])
actor-campaign: Cavern Manticore / Cavern
technical-potential: 4
technical-reason: Le rapport documente la chaîne d’exécution, plusieurs modules C2, leurs formats de compilation, configurations, fonctions post-exploitation et IOC.
artifacts: [ioc, configurations]
uncertainty: Attribution organisationnelle MOIS moins robuste que le nexus iranien ; l’usage de SysAid intervient après compromission et ne résulte pas d’une vulnérabilité SysAid.

### PUBLICATION P1

title: Cavern Manticore: Exposing Iran-Linked Modular C2 Framework
url: [https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/](https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/)
publisher: Check Point Research
published-at: 2026-07-06
role: primary
ioc-visibility: visible
visible-ioc-types: [SHA-256, domain]
visible-iocs: [37e123bd7998af4eae32718ce254776f36365a80ba56952593dab46f536d4066, 92cae0ad7f98f51a14bcc0ee05e372ebdc29ea96ea7bd161bd3f55198767603b, 5dc08bda6919a57a85e5f38b857985fa71529ca39c8299868d5a49a987e19b18, a4aa217def4c38f4ecacdf47b1cd687f60cc74c18ab75195be3c4357a790bf41, b630c96d3763182533d4fb9b614134382bd644cb02c6c1c3ade848b6ecc31e86, 8e9425c0b46eeb516610ae913d13f2b3f44a023043cb099277031d4ec38a6134, 0a3663648a46771a5a5423ad01e91a4e7ba825595e99fa934cb35cbb4848adc8, 5394d3b220de4695f731647e3a70545f951a8912ceb0c6585efab8d6842e8b42, hospitalinstallation[.]com, auth[.]hospitalinstallation[.]com]
publisher-ioc-count: unknown
ioc-note: La section IOCs publie des hashes, domaines, mutex, chemins et artefacts de configuration, dont config.txt et Cvn.cfg.A/Cvn.cfg.U. ([Check Point Research][4])

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: SentinelLABS retient Cavern Manticore comme espionnage via fournisseurs de services et RMM, avec lien MOIS à confiance modérée ; aucune liste d’IOC n’est visible. ([SentinelOne][2])

## SUBJECT S3

title: Acteurs affiliés à l’Iran ciblant des PLC d’infrastructures critiques américaines
presentation: L’avis conjoint AA26-097A a reçu le 22 juillet une mise à jour substantielle ajoutant des IOC, élargissant le périmètre des PLC observés et ajoutant des conseils de détection des modifications malveillantes de logique réutilisable. Les agences décrivent des acteurs APT affiliés à l’Iran sans établir que toute l’activité observée relève nécessairement de CyberAv3ngers. ([CISA][5])
actor-campaign: Iranian-affiliated APT actors; CyberAv3ngers / Shahid Kaveh Group comme activité historique apparentée
technical-potential: 4
technical-reason: L’avis fournit IOC réseau, protocoles et ports OT, familles de PLC ciblées, logiciels d’ingénierie utilisés et comportements de modification de logique.
artifacts: [ioc]
uncertainty: Avis initialement publié en avril puis substantiellement révisé en juillet ; l’identité précise des opérateurs de chaque intrusion reste non établie publiquement.

### PUBLICATION P1

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers Across US Critical Infrastructure
url: [https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a)
publisher: CISA
published-at: 2026-04-07
role: primary
ioc-visibility: visible
visible-ioc-types: [IPv4]
visible-iocs: [185.82.73[.]175, 141.11.164[.]153, 175.110.121[.]42, 175.110.121[.]39, 175.110.121[.]41, 175.110.121[.]107, 192.142.54[.]79, 84.200.205[.]165, 185.225.17[.]225, 79.133.46[.]209]
publisher-ioc-count: unknown
ioc-note: Le document révisé contient un tableau explicitement intitulé Indicators of Compromise (New, July 22, 2026) ; les valeurs ci-dessus sont reproduites de ce tableau. ([U.S. Department of War][6])

### PUBLICATION P2

title: CISA, FBI, EPA and U.S. Government Partners Update Warning of Iran-Affiliated Threat Actors Targeting Critical Infrastructure Programmable Logic Controllers
url: [https://www.cisa.gov/news-events/news/cisa-fbi-epa-and-us-government-partners-update-warning-iran-affiliated-threat-actors-targeting](https://www.cisa.gov/news-events/news/cisa-fbi-epa-and-us-government-partners-update-warning-iran-affiliated-threat-actors-targeting)
publisher: CISA
published-at: 2026-07-22
role: relay
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Cette publication annonce la mise à jour de l’avis conjoint ; la page n’a pas pu être inspectée directement en raison d’un refus HTTP 403. ([CISA][7])

### PUBLICATION P3

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: SentinelLABS présente l’avis conjoint d’avril comme la preuve publique la plus solide de l’activité OT iranienne observée durant le conflit et appelle à ne pas généraliser l’attribution CyberAv3ngers. ([SentinelOne][2])

## SUBJECT S4

title: APT42 SpearSpecter et évolution de TAMECAT avec assistance par IA
presentation: Dark Atlas décrit SpearSpecter comme une combinaison de social engineering prolongé, d’abus de search-ms/WebDAV et d’une version enrichie de TAMECAT, tout en rapportant un usage de l’IA générative dans plusieurs phases opérationnelles. KELA reprend SpearSpecter dans son profil de juillet consacré à APT42. ([darkatlas.io][8])
actor-campaign: APT42 / SpearSpecter / TAMECAT
technical-potential: 4
technical-reason: La recherche expose une chaîne d’infection Windows, une porte dérobée évoluée et des usages opérationnels de l’IA, avec suffisamment de détails pour une analyse technique ultérieure.
artifacts: [unknown]
uncertainty: La page Dark Atlas complète n’a pas pu être chargée par l’outil en raison de sa taille ; la visibilité exacte sur d’éventuels IOC ou artefacts téléchargeables reste inconnue.

### PUBLICATION P1

title: APT42: AI-Assisted Rapport Phishing and a More Resilient TAMECAT
url: [https://darkatlas.io/blog/apt42-ai-assisted-phishing-tamecat-analysis](https://darkatlas.io/blog/apt42-ai-assisted-phishing-tamecat-analysis)
publisher: Dark Atlas
published-at: 2026-07-19
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: L’index de l’éditeur expose le titre, la date et le résumé technique, mais le corps complet n’a pas été récupéré de façon exploitable pour vérifier une éventuelle section IOC. ([darkatlas.io][9])

### PUBLICATION P2

title: Iran's APTs and the U.S. Enterprise in 2026: APT42
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: KELA décrit SpearSpecter, la prise de contact prolongée et TAMECAT, mais aucune liste d’IOC spécifique à SpearSpecter n’a été établie dans le contenu consulté. ([KELA Cyber Threat Intelligence][10])

## SUBJECT S5

title: MuddyWater / Seedworm déploie la backdoor Dindoor sur des réseaux américains
presentation: Le profil MuddyWater publié par KELA en juillet remet en avant les intrusions de février-mars 2026 contre notamment une banque et un aéroport américains ainsi que l’opération israélienne d’un éditeur américain. Le rapport technique original de Symantec et Carbon Black documente Dindoor, Fakeset et les tentatives d’exfiltration via Rclone vers Wasabi. ([KELA Cyber Threat Intelligence][11])
actor-campaign: MuddyWater / Seedworm / Dindoor
technical-potential: 4
technical-reason: Le rapport original fournit plusieurs familles de malware, mécanismes d’exécution et d’exfiltration ainsi qu’une longue liste d’IOC.
artifacts: [ioc]
uncertainty: La publication de juillet est rétrospective ; l’activité technique originale documentée se concentre surtout sur février et mars 2026.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: MuddyWater
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse décrit Dindoor, son exécution via Deno et l’usage de Rclone/Wasabi, mais aucune liste d’IOC dédiée n’a été établie dans la page consultée. ([KELA Cyber Threat Intelligence][11])

### PUBLICATION P2

title: Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company
url: [https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us](https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us)
publisher: Symantec and Carbon Black Threat Hunter Team
published-at: 2026-03-05
role: primary
ioc-visibility: visible
visible-ioc-types: [SHA-256, domain]
visible-iocs: [0f9cf1cf8d641562053ce533aaa413754db88e60404cab6bbaa11f2b2491d542, 1d984d4b2b508b56a77c9a567fb7a50c858e672d56e8cf7677a1fca5c98c95d1, 2a00705cfd3c15cf8913e9eb4e23968efd06f1feceaef9987d26c5518887d043, 2a09bbb3d1ddb729ea7591f197b5955453aa3769c6fb98a5ef60c6e4b7df23a5, 42a5db2a020155b2adb77c00cbe6c6ad27c2285d8c6114679d9d34137e870b3f, 7467f326677a4a2c8576e71a832e297e794ea00e9b67c4fcbe78b5aec697cec4, 7c30c16e7a311dc0cdb1cdfd9ea6e502f44c027328dbe7d960b9bcd85ccf5eef, b0af82de672d81f3c2f153977923b3884a8a9e7045b182c2379b19a1996931a0, gitempire.s3.us-east-005.backblazeb2[.]com, elvenforest.s3.us-east-005.backblazeb2[.]com]
publisher-ioc-count: unknown
ioc-note: La section Indicators of Compromise fournit des hashes pour Dindoor, Fakeset, Stagecomp et Darkcomp ainsi que plusieurs indicateurs réseau. ([Security.com][12])

### PUBLICATION P3

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: SentinelLABS reprend cette activité comme exemple d’accès persistant de Seedworm/MuddyWater acquis avant l’escalade ; aucune liste d’IOC n’est visible. ([SentinelOne][2])

## SUBJECT S6

title: MuddyWater Operation IconCat et implants RUSTRIC/PYTRIC
presentation: Le profil MuddyWater de KELA distingue Operation IconCat comme une campagne ayant employé RUSTRIC pour la reconnaissance et PYTRIC pour des fonctions destructrices, notamment contre des MSP et des organisations liées à la défense. Cette campagne reste distincte d’Operation Olalampo malgré certains chevauchements de développement signalés. ([KELA Cyber Threat Intelligence][13])
actor-campaign: MuddyWater / Operation IconCat
technical-potential: 3
technical-reason: La synthèse décrit leurres, persistance, implants Rust, reconnaissance des produits de sécurité et fonctions destructrices, mais les artefacts exploitables n’ont pas été vérifiés directement.
artifacts: [unknown]
uncertainty: Activité principalement antérieure à juillet ; absence de rapport primaire spécifique inspecté dans cette phase.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: MuddyWater
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La page distingue Operation IconCat des autres opérations MuddyWater et décrit RUSTRIC/RustyWater et PYTRIC ; aucune liste d’IOC propre à IconCat n’a été vérifiée. ([KELA Cyber Threat Intelligence][13])

## SUBJECT S7

title: MuddyWater Operation Olalampo et écosystème GhostFetch/GhostBackDoor/HTTP_VIP/CHAR
presentation: KELA reprend Operation Olalampo comme une campagne MuddyWater multi-outils, avec plusieurs implants spécialisés et du C2 notamment via Telegram. Les recherches antérieures de Ctrl-Alt-Intel exposent de l’infrastructure, des C2, du code et des IOC MuddyWater présentant des chevauchements avec les travaux sur Olalampo. ([KELA Cyber Threat Intelligence][13])
actor-campaign: MuddyWater / Operation Olalampo
technical-potential: 4
technical-reason: Les sources combinées fournissent familles de malware, fonctions C2, post-exploitation, code et binaires exposés ainsi que des IOC.
artifacts: [ioc, samples, configurations]
uncertainty: La publication de juillet est rétrospective ; les IOC Ctrl-Alt-Intel couvrent une infrastructure MuddyWater plus large et ne doivent pas tous être attribués exclusivement à Olalampo.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: MuddyWater
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: KELA décrit GhostFetch, GhostBackDoor, HTTP_VIP et CHAR comme composants de l’opération ; aucune liste d’IOC spécifique à Olalampo n’a été vérifiée dans la page. ([KELA Cyber Threat Intelligence][13])

### PUBLICATION P2

title: MuddyWater Exposed: Inside an Iranian APT operation
url: [https://ctrlaltintel.com/research/MuddyWater/](https://ctrlaltintel.com/research/MuddyWater/)
publisher: Ctrl-Alt-Intel
published-at: 2026-03-04
role: primary
ioc-visibility: visible
visible-ioc-types: [IPv4, domain, smart-contract-address, SHA-256]
visible-iocs: [185.236.25[.]119, 193.17.183[.]126, 162.0.230[.]185, 157.20.182[.]49, 209.74.87[.]100, 18.223.24[.]218, 194.11.246[.]101, [www.xt24[.]com](http://www.xt24[.]com), 0x2B77671cfEE4907776a95abbb9681eee598c102E, 7ab597ff0b1a5e6916cad1662b49f58231867a1d4fa91a4edf7ecb73c3ec7fe6]
publisher-ioc-count: unknown
ioc-note: L’éditeur publie une table IOC et indique avoir mis en ligne sur GitHub plusieurs binaires ou sources C2 ; la liste couvre l’opération MuddyWater exposée au sens large. ([Ctrl-Alt-Intel][14])

## SUBJECT S8

title: MuddyWater ChainShell et lien avec une infrastructure criminelle russe
presentation: Le rapport semestriel de TrendAI reprend l’emploi par MuddyWater d’une capacité ChainShell associée à une infrastructure criminelle russophone, tandis que JUMPSEC avait auparavant documenté le lien opérationnel. Les recherches de Ctrl-Alt-Intel apportent un contexte primaire sur l’infrastructure MuddyWater exposée et ses mécanismes d’accès et de C2. ([www.trendmicro.com][15])
actor-campaign: MuddyWater / ChainShell
technical-potential: 4
technical-reason: Le corpus documente acquisition ou réutilisation de capacités, exploitation de systèmes exposés, C2 et infrastructure avec IOC et artefacts opérateur.
artifacts: [ioc, samples, configurations]
uncertainty: La page JUMPSEC n’a pas été inspectée en profondeur dans cette phase ; tous les artefacts Ctrl-Alt-Intel ne sont pas spécifiques à ChainShell.

### PUBLICATION P1

title: 2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI
url: [https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai](https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai)
publisher: TrendAI Research / Trend Micro
published-at: 2026-07-29
role: aggregator
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La page annonce que le rapport complet contient des technical indicators, sans fournir sur la page consultée un total ni une liste spécifique à ChainShell. ([www.trendmicro.com][15])

### PUBLICATION P2

title: ChainShell: MuddyWater's Russian MaaS Link
url: [https://www.jumpsec.com/guides/chainshell-muddywater-russian-criminal-infrastructure/](https://www.jumpsec.com/guides/chainshell-muddywater-russian-criminal-infrastructure/)
publisher: JUMPSEC
published-at: 2026-04-07
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Les métadonnées accessibles décrivent un lien opérationnel direct entre MuddyWater et une infrastructure/capacité criminelle russe ; l’accès détaillé aux éventuels IOC n’a pas été établi. ([JUMPSEC][16])

### PUBLICATION P3

title: MuddyWater Exposed: Inside an Iranian APT operation
url: [https://ctrlaltintel.com/research/MuddyWater/](https://ctrlaltintel.com/research/MuddyWater/)
publisher: Ctrl-Alt-Intel
published-at: 2026-03-04
role: primary
ioc-visibility: visible
visible-ioc-types: [IPv4, domain, smart-contract-address, SHA-256]
visible-iocs: [185.236.25[.]119, 193.17.183[.]126, 162.0.230[.]185, 157.20.182[.]49, 209.74.87[.]100, 18.223.24[.]218, 194.11.246[.]101, [www.xt24[.]com](http://www.xt24[.]com), 0x2B77671cfEE4907776a95abbb9681eee598c102E, 7ab597ff0b1a5e6916cad1662b49f58231867a1d4fa91a4edf7ecb73c3ec7fe6]
publisher-ioc-count: unknown
ioc-note: Les IOC concernent l’infrastructure MuddyWater exposée qui sert de contexte aux recherches ultérieures ; ils ne sont pas tous spécifiques à ChainShell. ([Ctrl-Alt-Intel][17])

## SUBJECT S9

title: Vague de ciblage de jauges automatiques de réservoirs attribuée de façon suspectée à un nexus iranien
presentation: Le rapport H1 de TrendAI décrit une vague de ciblage d’équipements de jaugeage de réservoirs de carburant exposés à Internet et évoque un lien iranien suspecté. Les références historiques à CyberAv3ngers et IOCONTROL constituent du contexte et ne suffisent pas à attribuer cette vague précise au même opérateur.
actor-campaign: suspected Iran-linked actors targeting automatic tank gauges
technical-potential: 2
technical-reason: Le sujet comporte des éléments OT, des équipements exposés et des vulnérabilités pertinentes, mais peu d’artefacts techniques spécifiques à la vague ont été rendus visibles.
artifacts: [unknown]
uncertainty: Attribution seulement suspectée ; pas de preuve publique examinée permettant d’assimiler la vague actuelle à CyberAv3ngers.

### PUBLICATION P1

title: 2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI
url: [https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai](https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai)
publisher: TrendAI Research / Trend Micro
published-at: 2026-07-29
role: aggregator
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Le rapport complet est annoncé comme contenant des indicateurs techniques, mais aucune valeur spécifiquement rattachable à la vague ATG n’a été retenue comme visible sur la page consultée. ([www.trendmicro.com][15])

### PUBLICATION P2

title: CISA Urges Stronger Security for Automatic Tank Gauge Systems
url: [https://www.cisa.gov/news-events/news/cisa-urges-stronger-security-automatic-tank-gauge-systems](https://www.cisa.gov/news-events/news/cisa-urges-stronger-security-automatic-tank-gauge-systems)
publisher: CISA
published-at: 2026-06-02
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Cette publication constitue le contexte officiel antérieur sur le ciblage d’ATG américains ; aucune valeur d’IOC n’a été établie comme explicitement visible lors de cette phase. ([CISA][18])

## SUBJECT S10

title: Prince of Persia / Infy adapte Tornado et mène une contre-opération contre les chercheurs
presentation: Le profil KELA de juillet remet en avant les adaptations 2025-2026 de Prince of Persia, notamment Tornado v51, le C2 Telegram et le recours à une DGA. SafeBreach documente en détail le renouvellement de l’infrastructure, la chaîne de livraison et une tentative de strike-back utilisant ZZ Stealer contre les chercheurs. ([KELA Cyber Threat Intelligence][19])
actor-campaign: Prince of Persia / Infy / APT-C-07
technical-potential: 4
technical-reason: SafeBreach fournit analyse de malware, infrastructure C2, chaîne d’infection, script de déchiffrement et IOC détaillés.
artifacts: [ioc]
uncertainty: La publication de juillet est rétrospective ; l’attribution à l’État iranien est une évaluation du fournisseur sans service gouvernemental spécifique publiquement identifié.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: Prince of Persia (Infy)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: KELA synthétise notamment Foudre, Tonnerre, Tornado, le C2 Telegram et l’activité de contre-ciblage des chercheurs ; aucune liste d’IOC propre à cette page n’a été établie. ([KELA Cyber Threat Intelligence][19])

### PUBLICATION P2

title: Prince of Persia, Part II: Covering Tracks, Striking Back & a Revealing Link to the Iranian Regime Amid the Country's Internet Blackout
url: [https://www.safebreach.com/blog/prince-of-persia-part-ii/](https://www.safebreach.com/blog/prince-of-persia-part-ii/)
publisher: SafeBreach Labs
published-at: 2026-02-04
role: primary
ioc-visibility: visible
visible-ioc-types: [SHA-256, IPv4, domain]
visible-iocs: [44fc9e306763774b50b61fc7487aa1d219aa288aefa201119c7bc278e17600a8, 5db4ed7d07ab028ab6ceba8efec5f667d86a419020d2a8c86e90a3125aa31bb9, 8DB20544F280955ED3EF3C42DC8423E3000E244FC7C8F0E3A7567FA48F7A15D9, B937024B7484B26D09BA8130CC4AB04600DC18C976BB0C7724A063F1FC6F0D77, 45.80.148.249, szzqwggurg.hbmc.net, szzqwggurg.conningstone.net, kbbpissmqs.conningstone.net, kbbpissmqs.hbmc.net, vssmqppaup.conningstone.net]
publisher-ioc-count: unknown
ioc-note: L’Appendix B publie explicitement des hashes et C2 ; l’Appendix A fournit un script de déchiffrement ZZ Stealer. ([SafeBreach][20])

## SUBJECT S11

title: Screening Serpens déploie MiniUpdate et MiniJunk V2
presentation: SentinelLABS cite Screening Serpens parmi les clusters d’espionnage iraniens actifs, tandis que Unit 42 fournit l’analyse technique primaire de deux familles de RAT et de six variantes déployées entre février et avril. La recherche documente notamment DLL sideloading, AppDomainManager hijacking et des infrastructures C2 dédiées par cible. ([SentinelOne][2])
actor-campaign: Screening Serpens / UNC1549 / Smoke Sandstorm / Iranian Dream Job
technical-potential: 4
technical-reason: Unit 42 fournit chaînes d’exécution, familles de RAT, variantes, C2 et une section IOC détaillée.
artifacts: [ioc]
uncertainty: L’activité primaire observée précède juillet ; le lien à des objectifs de renseignement iraniens est plus robuste que l’identification publique d’un service sponsor précis.

### PUBLICATION P1

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS
published-at: 2026-07-21
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La synthèse associe le cluster à l’espionnage par social engineering de type recrutement et à des priorités stratégiques iraniennes ; aucune liste d’IOC n’est visible. ([SentinelOne][2])

### PUBLICATION P2

title: Tracking Iranian APT Screening Serpens’ 2026 Espionage Campaigns
url: [https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/](https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/)
publisher: Unit 42 / Palo Alto Networks
published-at: 2026-05-22
role: primary
ioc-visibility: visible
visible-ioc-types: [domain, SHA-256]
visible-iocs: [licencemanagers.azurewebsites[.]net, LicenceSupporting.azurewebsites[.]net, PeerDistSvcManagers.azurewebsites[.]net, ThemesManagers.azurewebsites[.]net, ThemesProviderManagers.azurewebsites[.]net, docspace-y4cumb.onlyoffice[.]com, 44f4f7aca7f1d9bfdaf7b3736934cbe19f851a707662f8f0b0c49b383e054250, 332ba2f0297dfb1599adecc3e9067893e7cf243aa23aedce4906a4c480574c17, 0db36a04d304ad96f9e6f97b531934594cd95a5cea9ff2c9af249201089dc864, 38bd137c672bd58d08c4f0502f993a6561e2c3411773d1ae57ee0151a0a9d11d]
publisher-ioc-count: unknown
ioc-note: La section Indicators of Compromise contient des domaines, URL et hashes SHA-256 pour les campagnes MiniUpdate et MiniJunk V2 ; aucun total global n’est explicitement annoncé. ([Unit 42][21])

## SUBJECT S12

title: Profil technique 2026 d’APT34 / OilRig
presentation: KELA publie en juillet un profil d’APT34 centré sur l’abus des identités, d’Exchange, des contrôleurs de domaine et des techniques de living-off-the-land. L’exemple technique détaillé mis en avant dans la publication remonte toutefois à fin 2024 plutôt qu’à une opération nouvelle de juillet 2026. ([KELA Cyber Threat Intelligence][22])
actor-campaign: APT34 / OilRig
technical-potential: 2
technical-reason: La publication synthétise des TTP exploitables pour le renseignement et la détection mais n’apporte pas, dans le contenu examiné, une nouvelle campagne technique de juillet.
artifacts: [unknown]
uncertainty: Pas de nouvelle opération distincte observée en juillet dans la publication ; attribution MOIS présentée comme un lien fort par KELA.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT34 (OilRig)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La page décrit notamment C2 via Exchange/EWS, tunneling DNS, web shells IIS et password-filter DLLs ; aucune liste d’IOC publiquement visible n’a été établie dans cette phase. ([KELA Cyber Threat Intelligence][22])

## SUBJECT S13

title: Nouvelle nomenclature Google pour les acteurs iraniens suivis en CTI
presentation: Google Threat Intelligence Group a adopté en juillet une nouvelle taxonomie et a ajouté le 30 juillet une table de renommage comprenant plusieurs acteurs iraniens majeurs. Ce sujet est surtout pertinent pour la normalisation des alias et le rapprochement des publications, et non comme campagne opérationnelle. ([Google Cloud][23])
actor-campaign: APT33 / APT34 / APT35 / APT39 / APT42 / CALANQUE / TEMP.Zagros / MUDDYCOAST
technical-potential: 1
technical-reason: La publication modifie la nomenclature CTI utile au pivot et à la corrélation mais ne fournit ni chaîne d’infection ni artefacts opérationnels.
artifacts: [none]
uncertainty: Les correspondances de noms reflètent la taxonomie GTIG et ne garantissent pas une équivalence parfaite avec les clusters de tous les autres fournisseurs.

### PUBLICATION P1

title: Updated Cyber Threat Actor Naming System
url: [https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system](https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system)
publisher: Google Threat Intelligence Group
published-at: 2026-07-24
role: primary
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: La mise à jour du 30 juillet donne notamment APT33 → BLEAK ION, APT34 → SOLAR ION, APT35 → RICH ION, APT39 → CINDER ION, APT42/CALANQUE → CALANQUE ION et TEMP.Zagros/MUDDYCOAST → MUDDY ION ; aucun IOC n’est fourni. ([Google Cloud][23])

## SUBJECT S14

title: Évaluation de l’usage de l’IA dans les capacités asymétriques iraniennes en 2026
presentation: Insikt Group évalue que l’IA a surtout accéléré des capacités iraniennes préexistantes en matière cyber, influence, renseignement et répression plutôt que créé de nouvelles capacités autonomes. La publication est stratégique et transversale plutôt qu’une analyse de campagne ou de malware. ([Recorded Future][24])
actor-campaign: Iranian state-sponsored and state-aligned cyber actors
technical-potential: 1
technical-reason: Le rapport apporte du contexte sur l’usage de l’IA pour la reconnaissance, le développement et le social engineering mais peu d’artefacts directement exploitables.
artifacts: [none]
uncertainty: Évaluation agrégée et probabiliste ; ne permet pas d’attribuer chaque usage de l’IA à un acteur ou une campagne précis.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: Le contenu visible est une évaluation stratégique multisectorielle et ne présente pas de section d’IOC ni de valeurs d’IOC. ([Recorded Future][24])

# LIMITES

La recherche couvre les sources publiques accessibles en français et en anglais et a été arrêtée à la date de recherche du 17 août 2026 ; aucune publication postérieure à cette date n’a été utilisée. La collecte significative retrouvée pour juillet est très majoritairement anglophone ; aucune publication francophone de niveau technique comparable n’a été identifiée dans les résultats consultés.

Plusieurs publications de juillet sont des synthèses rétrospectives : elles ont été rattachées à des SUBJECT distincts lorsque chacune faisait clairement référence à une campagne ou une recherche identifiable, puis complétées par le rapport primaire antérieur lorsqu’il a été retrouvé. Toutes les campagnes historiques simplement mentionnées dans les profils d’acteurs n’ont pas été promues automatiquement en SUBJECT lorsqu’aucun apport éditorial ou technique suffisamment distinct n’a été établi.

Certaines pages ont présenté des restrictions d’accès. La page canonique CISA a notamment opposé un HTTP 403 lors de l’ouverture directe ; pour AA26-097A, le contenu technique du PDF officiel et les résultats indexés ont permis de vérifier la révision du 22 juillet et ses IOC. La page Dark Atlas consacrée à APT42 était indexable mais trop volumineuse pour être chargée intégralement par l’outil ; sa visibilité IOC est donc laissée à unknown plutôt que déduite.

Les champs IOC ne comptabilisent que les valeurs explicitement visibles dans les sources consultées. Aucun total n’a été calculé à partir des listes : publisher-ioc-count reste unknown lorsqu’aucun total n’est explicitement annoncé par l’éditeur. Les différences d’alias et d’attribution entre fournisseurs ont été conservées avec leurs réserves plutôt que normalisées de force.

[1]: https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat?utm_source=chatgpt.com "Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance ..."
[2]: https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/?utm_source=chatgpt.com "Iran War Cyber Threat Landscape | A Midyear Assessment ..."
[3]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/?utm_source=chatgpt.com "Cavern Manticore: Exposing Iran-Linked Modular C2 ..."
[4]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/ "Cavern Manticore: Exposing Iran-Linked Modular C2 Framework - Check Point Research"
[5]: https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a?utm_source=chatgpt.com "Iranian-Affiliated Cyber Actors Exploit Programmable Logic ..."
[6]: https://media.defense.gov/2026/Apr/07/2003907538/-1/-1/0/AA26-097A-IRANIAN-AFFILIATED-CYBER-ACTORS-EXPLOIT-PROGRAMMABLE-LOGIC-CONTROLLERS-ACROSS-US-CRITICAL-INFRASTRUCTURE_508C.PDF?utm_source=chatgpt.com "Iranian-Affiliated Cyber Actors Exploit Programmable Logic ..."
[7]: https://www.cisa.gov/news-events/news/cisa-fbi-epa-and-us-government-partners-update-warning-iran-affiliated-threat-actors-targeting?utm_source=chatgpt.com "CISA, FBI, EPA and U.S. Government Partners Update ..."
[8]: https://darkatlas.io/blog/apt42-ai-assisted-phishing-tamecat-analysis?utm_source=chatgpt.com "APT42: AI-Assisted Phishing and the Resilient TAMECAT ..."
[9]: https://darkatlas.io/blog?utm_source=chatgpt.com "Blog | Dark Atlas | Dark Web Monitoring & Threat Intelligence"
[10]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/?utm_source=chatgpt.com "APT42: Iran's Human-Centric Espionage in 2026"
[11]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/?utm_source=chatgpt.com "MuddyWater in 2026: Iran's APT Hits U.S. Targets"
[12]: https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us "Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company | SECURITY.COM"
[13]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/ "https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/"
[14]: https://ctrlaltintel.com/research/MuddyWater/?utm_source=chatgpt.com "MuddyWater Exposed: Inside an Iranian APT operation"
[15]: https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai?utm_source=chatgpt.com "How APTs Are Weaponizing Trust in the Age of AI"
[16]: https://www.jumpsec.com/guides/chainshell-muddywater-russian-criminal-infrastructure/?utm_source=chatgpt.com "ChainShell: MuddyWater's Russian MaaS Link"
[17]: https://ctrlaltintel.com/research/MuddyWater/ "MuddyWater Exposed: Inside an Iranian APT operation - Ctrl-Alt-Intel"
[18]: https://www.cisa.gov/news-events/news/cisa-urges-stronger-security-automatic-tank-gauge-systems "https://www.cisa.gov/news-events/news/cisa-urges-stronger-security-automatic-tank-gauge-systems"
[19]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/?utm_source=chatgpt.com "Prince of Persia (Infy): Iran's Persistent APT"
[20]: https://www.safebreach.com/blog/prince-of-persia-part-ii/ "An Update on the Prince of Persia Threat Actor | SafeBreach"
[21]: https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/ "Tracking Iranian APT Screening Serpens’ 2026 Espionage Campaigns"
[22]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/?utm_source=chatgpt.com "APT34 (OilRig): Espionage on Your Infrastructure"
[23]: https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system?utm_source=chatgpt.com "Updated Cyber Threat Actor Naming System"
[24]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook?utm_source=chatgpt.com "AI Has Enhanced Iran's Asymmetric Playbook During ..."

