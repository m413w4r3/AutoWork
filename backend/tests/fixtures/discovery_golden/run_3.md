# SUJETS CANDIDATS

## SUBJECT S1

title: Cavern Manticore et framework C2 modulaire Cavern
presentation: Check Point Research décrit un acteur Iran-nexus ciblant notamment des organismes gouvernementaux et IT israéliens au moyen d’un framework C2 modulaire .NET et de composants natifs. La recherche documente une chaîne de déploiement via des outils légitimes et plusieurs modules spécialisés.
actor-campaign: Cavern Manticore; chevauchements rapportés avec MuddyWater et Lyceum, lien MOIS rapporté par Check Point Research
technical-potential: 4
technical-reason: La publication expose l’architecture C2, une chaîne de déploiement, plusieurs modules, des mécanismes de communication et de nombreux IOC.
artifacts: ioc
uncertainty: Le périmètre exact entre Cavern Manticore, MuddyWater et Lyceum dépend du modèle d’attribution du fournisseur.

### PUBLICATION P1

title: Cavern Manticore: Exposing Iran-Linked Modular C2 Framework
url: [https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/](https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/)
publisher: Check Point Research
published-at: 2026-07-06 ([Check Point Research][1])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, domain
visible-iocs: 37e123bd7998af4eae32718ce254776f36365a80ba56952593dab46f536d4066; 92cae0ad7f98f51a14bcc0ee05e372ebdc29ea96ea7bd161bd3f55198767603b; 5dc08bda6919a57a85e5f38b857985fa71529ca39c8299868d5a49a987e19b18; a4aa217def4c38f4ecacdf47b1cd687f60cc74c18ab75195be3c4357a790bf41; b630c96d3763182533d4fb9b614134382bd644cb02c6c1c3ade848b6ecc31e86; 8e9425c0b46eeb516610ae913d13f2b3f44a023043cb099277031d4ec38a6134; hospitalinstallation[.]com; auth[.]hospitalinstallation[.]com; adserviceupdate[.]com; hygienehistory[.]com ([Check Point Research][2])
publisher-ioc-count: unknown
ioc-note: Plusieurs autres valeurs sont visibles dans la publication; seules dix sont reproduites ici.

## SUBJECT S2

title: TAG-182 et diffusion du spyware Android MarkiRAT
presentation: Insikt Group décrit une infrastructure nouvellement identifiée associée à TAG-182 et utilisée pour diffuser MarkiRAT par de fausses applications VPN, multimédia ou apparentées. Le ciblage rapporté concerne principalement des personnes parlant persan en Iran et dans la diaspora.
actor-campaign: TAG-182; chevauchements rapportés avec Ferocious Kitten
technical-potential: 4
technical-reason: La recherche fournit infrastructure, échantillons hachés, mécanismes de diffusion et règles de détection, dont YARA.
artifacts: ioc, yara
uncertainty: Insikt Group relève des chevauchements avec Ferocious Kitten sans établir de lien organisationnel certain.

### PUBLICATION P1

title: Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool
url: [https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat](https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-01 ([Recorded Future][3])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, ipv4, domain
visible-iocs: 3b172281f65ceaee280ae810edb6fd39a1ecd25649f929f246c0405df94f4c89; 66dcd98c6b310f4429890821e609d48cc6395a6be15ffe5a121ec68b7a8f7402; 51a6686b8c5ec7c610637398f3de43589f4e9fcbe8bcc0245343c5454d3b91de; a4f1b79e96a7d016de1991a64506792018de99eac5df00f7cabe26ef41b2bd81; 212[.]83[.]61[.]198; yeplayer[.]store; yemplayer[.]site; comi-site[.]website; comesignt[.]website; comisignin[.]online ([Recorded Future][4])
publisher-ioc-count: unknown
ioc-note: La publication contient davantage d’indicateurs et expose également des règles de détection.

## SUBJECT S3

title: Extension du ciblage de PLC par des acteurs affiliés à l’Iran
presentation: La mise à jour de l’alerte conjointe américaine étend le périmètre de PLC ciblés au-delà de Rockwell Automation vers des équipements Schneider Electric et Siemens, dans plusieurs secteurs d’infrastructure critique. Les sources techniques décrivent l’usage d’outils d’ingénierie légitimes et la modification de projets ou de logique de contrôle.
actor-campaign: Iranian-affiliated actors; association publique avec CyberAv3ngers / Storm-0784 / CL-STA-1128 selon les sources industrielles
technical-potential: 4
technical-reason: L’ensemble apporte modèles de PLC, outils d’ingénierie, effets sur la logique de contrôle, mesures de défense et IOC diffusés avec l’avis officiel.
artifacts: ioc
uncertainty: L’avis officiel regroupe de l’activité affiliée à l’Iran sans nécessairement attribuer chaque intrusion au même opérateur. L’URL CISA principale correspond à une publication d’avril mise à jour le 22 juillet.

### PUBLICATION P1

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers Across US Critical Infrastructure
url: [https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a)
publisher: CISA
published-at: 2026-04-07 ([cisa.gov][5])
role: primary
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: L’avis a été substantiellement mis à jour le 2026-07-22 et annonce des IOC téléchargeables, mais leurs valeurs n’étaient pas visibles dans la page officielle consultée.

### PUBLICATION P2

title: CISA и ФБР предписали немедленно отключить PLC Rockwell, Schneider и Siemens от интернета из-за атак иранских хакеров
url: [https://1275.ru/ioc/cisa-i-fbr-predpisali-nemedlenno-otklyuchit-plc-rockwell-schneider-i-siemens-ot-interneta-iz-za-atak-iranskih-hakerov_32692](https://1275.ru/ioc/cisa-i-fbr-predpisali-nemedlenno-otklyuchit-plc-rockwell-schneider-i-siemens-ot-interneta-iz-za-atak-iranskih-hakerov_32692)
publisher: SEC-1275-1
published-at: 2026-07-22 ([SEC-1275-1][6])
role: relay
ioc-visibility: visible
visible-ioc-types: ipv4
visible-iocs: 135.136.1.133; 141.11.164.153; 175.110.121.107; 175.110.121.39; 175.110.121.41; 175.110.121.42; 185.225.17.225; 185.82.73.162; 185.82.73.164; 185.82.73.165 ([SEC-1275-1][6])
publisher-ioc-count: unknown
ioc-note: La page relaie des indicateurs associés à l’alerte; dix valeurs visibles sont reproduites.

### PUBLICATION P3

title: Iranian-Affiliated Actors Expand PLC Targeting to Siemens and Schneider Electric: What CISA’s Updated Advisory Means for CNI
url: [https://www.ioactive.com/iranian-affiliated-actors-expand-plc-targeting-to-siemens-and-schneider-electric-what-cisas-updated-advisory-means-for-cni/](https://www.ioactive.com/iranian-affiliated-actors-expand-plc-targeting-to-siemens-and-schneider-electric-what-cisas-updated-advisory-means-for-cni/)
publisher: IOActive
published-at: 2026-07-27 ([IOActive][7])
role: independent
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication renvoie à de nouveaux IOC sans valeurs directement visibles dans le contenu consulté.

### PUBLICATION P4

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLabs
published-at: 2026-07-21 ([SentinelOne][8])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse replace l’activité OT Iran-nexus dans son contexte et insiste sur les limites de l’attribution.

## SUBJECT S4

title: Attaques coordonnées contre plus de 30 réseaux d’eau du Minnesota et piste Iran
presentation: Des systèmes d’eau locaux du Minnesota ont subi fin juillet des actions coordonnées affectant notamment des PLC, avec changements de mots de passe ou d’adresses IP et bascule vers des opérations manuelles dans certains cas. Des responsables américains ont examiné une possible connexion iranienne, sans attribution publique ferme pendant la période.
actor-campaign: unknown; possible Iran-linked activity rapportée par des responsables américains non nommés
technical-potential: 3
technical-reason: Les effets sur les PLC et les mesures OT sont documentés, mais l’attribution et les détails forensiques publics restent limités.
artifacts: unknown
uncertainty: Le FBI n’avait pas publiquement attribué l’incident au 31 juillet. La similitude avec des opérations Iran-nexus antérieures ne suffit pas à établir une identité de campagne.

### PUBLICATION P1

title: CISA Urges Water and Wastewater Systems Sector to Protect OT Against Activity Targeting PLCs
url: [https://www.cisa.gov/news-events/alerts/2026/07/30/cisa-urges-water-and-wastewater-systems-sector-protect-ot-against-activity-targeting-plcs](https://www.cisa.gov/news-events/alerts/2026/07/30/cisa-urges-water-and-wastewater-systems-sector-protect-ot-against-activity-targeting-plcs)
publisher: CISA
published-at: 2026-07-30 ([cisa.gov][9])
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exploitable n’a été relevé dans le contenu visible consulté.

### PUBLICATION P2

title: Minnesota IT officials disclose 'coordinated cyberattack' at more than 30 local water systems
url: [https://www.reuters.com/legal/litigation/minnesota-it-officials-disclose-coordinated-cyberattack-more-than-30-local-water-2026-07-28/](https://www.reuters.com/legal/litigation/minnesota-it-officials-disclose-coordinated-cyberattack-more-than-30-local-water-2026-07-28/)
publisher: Reuters
published-at: 2026-07-28 ([Reuters][10])
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: L’article ne fournit pas d’IOC.

### PUBLICATION P3

title: Feds issue warning to local water systems over increased cyberattacks, following Minnesota incident
url: [https://abcnews.com/US/investigators-iran-connection-minnesota-water-system-hacks-us/story?id=135237777](https://abcnews.com/US/investigators-iran-connection-minnesota-water-system-hacks-us/story?id=135237777)
publisher: ABC News
published-at: 2026-07-31 ([ABC News][11])
role: independent
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: L’article rapporte l’examen d’une piste iranienne mais aucun IOC.

## SUBJECT S5

title: MuddyWater / Seedworm et Dindoor contre des réseaux américains
presentation: Une analyse de juillet remet en avant une opération Seedworm/MuddyWater ayant compromis des réseaux d’une banque, d’un aéroport et d’un éditeur de logiciels américains. La recherche primaire documente notamment Dindoor, Fakeset et l’usage de services légitimes pour l’exfiltration ou l’hébergement.
actor-campaign: MuddyWater / Seedworm
technical-potential: 4
technical-reason: Le rapport primaire fournit malware, modes opératoires, infrastructures et une table d’IOC directement exploitable.
artifacts: ioc
uncertainty: La publication de juillet est rétrospective; l’analyse primaire détaillée date de mars. Cette activité ne doit pas être fusionnée avec les autres campagnes MuddyWater uniquement sur la base de l’acteur.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: MuddyWater
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22 ([KELA Cyber Threat Intelligence][12])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication est une synthèse de plusieurs opérations MuddyWater; aucun IOC exact n’a été retenu depuis son contenu visible.

### PUBLICATION P2

title: Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company
url: [https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us](https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us)
publisher: Symantec and Carbon Black
published-at: 2026-03-05 ([Security.com][13])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, domain
visible-iocs: 0f9cf1cf8d641562053ce533aaa413754db88e60404cab6bbaa11f2b2491d542; 1d984d4b2b508b56a77c9a567fb7a50c858e672d56e8cf7677a1fca5c98c95d1; 2a00705cfd3c15cf8913e9eb4e23968efd06f1feceaef9987d26c5518887d043; 2a09bbb3d1ddb729ea7591f197b5955453aa3769c6fb98a5ef60c6e4b7df23a5; 42a5db2a020155b2adb77c00cbe6c6ad27c2285d8c6114679d9d34137e870b3f; 7467f326677a4a2c8576e71a832e297e794ea00e9b67c4fcbe78b5aec697cec4; 7c30c16e7a311dc0cdb1cdfd9ea6e502f44c027328dbe7d960b9bcd85ccf5eef; gitempire.s3.us-east-005.backblazeb2[.]com; uppdatefile[.]com; serialmenot[.]com ([Security.com][14])
publisher-ioc-count: unknown
ioc-note: La table visible contient d’autres hachages et domaines.

## SUBJECT S6

title: Operation Olalampo de MuddyWater et usage d’IA dans CHAR
presentation: Group-IB décrit une campagne MuddyWater observée début 2026 avec plusieurs familles ou composants de malware, tandis qu’une synthèse de juillet relève des traces d’assistance par IA dans le développement de CHAR. La campagne vise principalement le Moyen-Orient et combine plusieurs canaux et fonctions de contrôle.
actor-campaign: MuddyWater / Operation Olalampo
technical-potential: 4
technical-reason: La recherche primaire fournit une chaîne opérationnelle, plusieurs implants, infrastructure C2 et IOC, complétée en juillet par une analyse de l’usage d’IA.
artifacts: ioc
uncertainty: L’évaluation de l’assistance par IA porte sur des indices de développement et non sur une attribution indépendante de la campagne. La publication primaire est antérieure à juillet.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse traite de plusieurs acteurs et campagnes Iran-nexus; aucun IOC exact n’a été retenu depuis la page visible.

### PUBLICATION P2

title: Operation Olalampo: Inside MuddyWater’s Latest Campaign
url: [https://www.group-ib.com/blog/muddywater-operation-olalampo/](https://www.group-ib.com/blog/muddywater-operation-olalampo/)
publisher: Group-IB
published-at: 2026-02-20 ([Group-IB][16])
role: primary
ioc-visibility: visible
visible-ioc-types: domain, ipv4, sha1
visible-iocs: codefusiontech[.]org; Promoverse[.]org; miniquest[.]org; jerusalemsolutions[.]com; 162.0.230[.]185; 209.74.87[.]100; 143.198.5[.]41; 209.74.87[.]67; f4e0f4449dc50e33e912403082e093dd8e4bc55d; 3441306816018d08dd03a97ac306fac0200e9152 ([Group-IB][17])
publisher-ioc-count: unknown
ioc-note: D’autres valeurs sont visibles dans l’annexe de la publication.

## SUBJECT S7

title: Screening Serpens / UNC1549 et campagnes MiniUpdate–MiniJunk
presentation: Unit 42 suit plusieurs campagnes d’espionnage attribuées à Screening Serpens, avec leurres de recrutement, variantes de RAT et chaînes multistades visant notamment les États-Unis, Israël et les Émirats arabes unis. Une synthèse de juillet rattache cet ensemble aux priorités stratégiques iraniennes.
actor-campaign: Screening Serpens / UNC1549 / Smoke Sandstorm
technical-potential: 4
technical-reason: La recherche décrit plusieurs chaînes de livraison et implants, avec des infrastructures C2 explicitement observables.
artifacts: ioc
uncertainty: Screening Serpens et Nimbus Manticore sont mappés au même ensemble UNC1549 par certaines sources, mais les campagnes décrites sont conservées séparément. La date exacte du billet Unit 42 n’a pas pu être établie dans les pages consultées.

### PUBLICATION P1

title: Tracking Iranian APT Screening Serpens’ 2026 Espionage Campaigns
url: [https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/](https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/)
publisher: Unit 42
published-at: unknown
role: primary
ioc-visibility: visible
visible-ioc-types: url
visible-iocs: hxxps[:]//NanoMatrix.azurewebsites[.]net; hxxps[:]//QuantumWeave.azurewebsites[.]net; hxxps[:]//ElementShift.azurewebsites[.]net ([Unit 42][18])
publisher-ioc-count: unknown
ioc-note: Trois URL C2 explicitement visibles ont été retenues.

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLabs
published-at: 2026-07-21 ([SentinelOne][8])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication fournit surtout une mise en contexte et un crosswalk d’alias.

## SUBJECT S8

title: RedKitten et implant SloppyMIO
presentation: HarfangLab décrit RedKitten, une campagne visant des personnes liées aux manifestations iraniennes et utilisant l’implant C# SloppyMIO. Une synthèse de juillet la retient comme exemple d’accélération du développement offensif par l’IA dans l’écosystème Iran-nexus.
actor-campaign: RedKitten; attribution à un groupe iranien connu non établie
technical-potential: 4
technical-reason: Le rapport primaire comporte échantillons hachés, détails de développement, chaîne d’attaque et règle YARA.
artifacts: ioc, yara
uncertainty: HarfangLab ne rattache pas avec certitude RedKitten à un groupe iranien déjà nommé. Les ressemblances avec d’autres ensembles ne constituent pas une attribution ferme.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exact n’a été retenu depuis le contenu visible de cette synthèse.

### PUBLICATION P2

title: RedKitten: AI-accelerated campaign targeting Iranian protests
url: [https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/](https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/)
publisher: HarfangLab
published-at: 2026-01-29 ([HarfangLab][19])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256
visible-iocs: d3bb28307d11214867c570fe594f773ba90195ed22b834bad038b62bf75a4192; c40c94d787f6a35ac1cb4c5f031cf5777b77c79dc3929181badea33aaf177aa7; 59ee007fd17280470724eb8a11ab12a98e85fd2383af3065f5f09a7e1a73f88c; 90aebc9849b659515fd70dde6db717ad457ab2a90522a410d1fd531ca8640624; 96ee9d3ed80c59c4bf39ed630efbfa53591fbe51155db7919ef64535a6171044; 6d474cf5aeb58a60f2f7c4d47143cc5a11a5c7f17a6b43263723d337231c3d60; 16164c83ce4786ab85aa3fc9566a317519e866ff6cad3fbd647f3e955b8a8255; 36413af1a7c7dc9e49fdf465ebc5abc3b4bb6b33f1c5ccaa17ae5e0794b6faaa; 6e1bb2c41500ee18bd55a2de04bb3d74bd5c5e8c45eaeef030c7c6ea661cc2db; ac0e045b6f3683315ef420971f382e167385e39023d118d023fa6989e35fadf6 ([HarfangLab][19])
publisher-ioc-count: unknown
ioc-note: Une règle YARA nommée trr260101_sloppymio est également visible dans la publication.

## SUBJECT S9

title: Cyber Isnaad Front, GRAT et sabotage OT/IT
presentation: Profero décrit une opération destructive contre une installation israélienne de production alimentaire combinant manipulation de contrôleurs de réfrigération, actions sur des systèmes Windows et usage de GRAT. L’entreprise attribue avec forte confiance Cyber Isnaad Front à une façade opérée par ou avec une entité affiliée à l’IRGC.
actor-campaign: Cyber Isnaad Front; Aria Sepehr Ayandehsazan selon Profero
technical-potential: 4
technical-reason: La recherche fournit effets OT, mécanismes de sabotage IT, malware, infrastructure de commande et hachages.
artifacts: ioc
uncertainty: Le degré de contrôle direct de l’IRGC repose sur l’évaluation de Profero. La synthèse de juillet ne présente pas d’élément direct montrant un usage d’IA dans cette opération.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse de juillet replace la campagne dans le paysage Iran-nexus sans fournir d’IOC exact retenu ici.

### PUBLICATION P2

title: The War Between Wars: How an IRGC Cyber Front Runs Destructive OT and IT Attacks Under Cover of a Ceasefire
url: [https://profero.io/blog/war-between-wars/](https://profero.io/blog/war-between-wars/)
publisher: Profero
published-at: 2026-05-24 ([Profero | Rapid-IR][20])
role: primary
ioc-visibility: visible
visible-ioc-types: ipv4:port, sha256
visible-iocs: 84[.]201[.]6[.]131:7878; 84[.]201[.]6[.]131:9988; 6f5f427d96656ae51405e6a5e65253759db45ea0a17da2d70f881404a4ed717b; 0ad128e813314e4562489478e6def8c6dfcc251e006d7f55b24273e93d3bc7fb; c4909b2d7a7f813b5a3d729fe64535033e716ae89dc39c402a6cb8ccbccaadca; 86194eb5c5abcfe763899aaad7eb64894c71e816dd7d27427c8bac4ab280533d ([Profero | Rapid-IR][21])
publisher-ioc-count: unknown
ioc-note: Les valeurs reproduites correspondent aux indicateurs explicitement visibles dans la publication.

## SUBJECT S10

title: Dust Specter contre des responsables gouvernementaux irakiens
presentation: Zscaler ThreatLabz décrit une campagne début 2026 visant des responsables gouvernementaux irakiens au moyen de leurres diplomatiques, ClickFix et plusieurs implants. La recherche attribue l’activité à un acteur suspecté d’être Iran-nexus et relève des indices de développement assisté par IA.
actor-campaign: Dust Specter; suspected Iran-nexus
technical-potential: 4
technical-reason: La publication fournit deux chaînes d’infection, plusieurs familles de malware et un ensemble important d’IOC.
artifacts: ioc
uncertainty: L’attribution Iran-nexus est évaluative et non une attribution gouvernementale publique. La publication de juillet est une synthèse, tandis que le rapport primaire date de mars.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication de juillet synthétise notamment les indices d’usage d’IA attribués à Dust Specter.

### PUBLICATION P2

title: Dust Specter APT Targets Government Officials in Iraq
url: [https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq](https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq)
publisher: Zscaler ThreatLabz
published-at: 2026-03-02 ([Zscaler][22])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256, domain
visible-iocs: 903f7869a94d88d43b9140bb656f7bb86ef725efc78ef2ff9d12fd7c7c2aca74; 6bb0d45799076b3f2d7f602b978a0779868fc72a1188374f6919fbbfba23efce; 797325b3c8a9356dcace75d93cb5cfb7847d2049c66772d4cc2cee821618cb96; 293ee1fe8d36aa79cf1f64f5ddef402bc6939d229c6fca955c7b796119564779; ad26cd72a83b884a8bc5aaa87309683953e151ebb3fde42eda7bf9a4406e530d; f3f2dc31f70a105db161a5e7b463b2215d3cbd64ac0146fd68e39da1c279f7ef; lecturegenieltd[.]pro; meetingapp[.]site; afterworld[.]store; girlsbags[.]shop ([Zscaler][22])
publisher-ioc-count: unknown
ioc-note: D’autres domaines et hachages sont visibles dans la publication.

## SUBJECT S11

title: Nimbus Manticore et backdoor MiniFast
presentation: Check Point Research décrit des opérations rapides de Nimbus Manticore pendant le conflit iranien, avec leurres liés à l’aviation ou aux logiciels et un nouveau backdoor MiniFast. Les chaînes observées comprennent notamment des installateurs trojanisés, du détournement de chargement et des mécanismes de persistance.
actor-campaign: Nimbus Manticore / UNC1549; IRGC-affiliated selon Check Point Research
technical-potential: 4
technical-reason: La publication contient chaîne d’infection, composants MiniFast, infrastructure et nombreux hachages d’échantillons.
artifacts: ioc
uncertainty: Nimbus Manticore et Screening Serpens sont rapprochés d’UNC1549 dans certaines taxonomies, mais les campagnes sont conservées comme sujets distincts. La publication primaire est antérieure à juillet.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse de juillet discute notamment des indices d’assistance par IA dans le développement.

### PUBLICATION P2

title: Fast and Furious – Nimbus Manticore Operations During the Iranian Conflict
url: [https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/](https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/)
publisher: Check Point Research
published-at: 2026-05-22 ([Check Point Research][23])
role: primary
ioc-visibility: visible
visible-ioc-types: sha256
visible-iocs: 10fd541674adadfbba99b54280f7e59732746faf2b10ce68521866f737f1e46d; eee657ffdb2af8ed6412221e7d5fbf4f5742f2ac2c88f43f12db46af0697de71; 781605ce9d4a9869e846f6c9657d71437cb6240ab27ffbc4cd550c0e06996690; 2c214494fd0bad31473ca8adce78a4f50847876584571e66aadeae70827ec2dc; f08b17856616d66492a24dced27f788e235f35f42fa7cd10f315000d3a2f4c03; a57ffb819fe8d98ff925c5d7b239598fe302acf5a13193d7a535040a71298fdf; 63d0d3c4a7f71bdbca720903d6a99b832089cc093c64d2938e7e001e56c17ab4; 74882085db2088356ed7f72f01e0404a0a98cda88ef56fb15ce74c1f36b26d27; bc3b44154518c5794ce639108e7b9c5fecb0c189607a26de1aaed518d890c7ad; ecaf493c320d201d285ef5f61d75744216e47cf1115b4af528f9a78883cc446e ([Check Point Research][23])
publisher-ioc-count: unknown
ioc-note: Dix hachages visibles sont reproduits; la publication contient davantage de détails techniques.

## SUBJECT S12

title: APT42, SpearSpecter et compromission centrée sur l’identité et le cloud
presentation: KELA consacre une analyse de juillet à APT42, en mettant l’accent sur l’ingénierie sociale longue durée, le spearphishing, les compromissions de comptes cloud et la persistance au niveau des messageries. Une autre synthèse de juillet examine l’usage de modèles d’IA par l’acteur pour la préparation de ciblages et l’ingénierie sociale.
actor-campaign: APT42 / CALANQUE; IRGC-subordinated selon KELA
technical-potential: 3
technical-reason: Les publications exposent des TTP cloud et de phishing utiles à la détection, mais peu d’IOC exacts sont visibles dans les pages consultées.
artifacts: unknown
uncertainty: Les alias et sous-clusters associés à APT42 ne sont pas nécessairement équivalents entre fournisseurs. Les billets de juillet agrègent plusieurs opérations plutôt qu’une campagne unique nouvelle.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT42
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22 ([KELA Cyber Threat Intelligence][24])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exact n’a été retenu depuis le contenu visible consulté.

### PUBLICATION P2

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication synthétise notamment l’emploi de Gemini rapporté pour des activités liées à APT42.

## SUBJECT S13

title: Prince of Persia / Infy et évolution de Foudre, Tonnerre et Tornado
presentation: KELA publie en juillet un profil technique de Prince of Persia/Infy décrivant l’évolution de ses implants et mécanismes C2, notamment Telegram et un DGA utilisant la blockchain Bitcoin. Le billet souligne aussi les adaptations opérationnelles autour des coupures d’Internet en Iran.
actor-campaign: Prince of Persia / Infy
technical-potential: 3
technical-reason: La publication décrit plusieurs générations de malware, méthodes de livraison et mécanismes C2, mais aucun IOC exact n’a été retenu dans le contenu visible.
artifacts: unknown
uncertainty: L’article est un profil rétrospectif et non l’annonce d’une campagne de juillet. Aucun service étatique iranien précis n’y est publiquement attribué avec certitude.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: Prince of Persia (Infy)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22 ([KELA Cyber Threat Intelligence][25])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exact n’a été relevé dans le contenu visible consulté.

## SUBJECT S14

title: APT34 / OilRig et espionnage persistant des environnements Exchange
presentation: KELA publie en juillet un profil d’APT34/OilRig centré sur ses capacités d’espionnage, l’exploitation d’environnements Exchange, les web shells, la collecte d’identifiants et les techniques living-off-the-land. L’article rattache l’acteur au MOIS dans le cadre de sa synthèse.
actor-campaign: APT34 / OilRig; MOIS association rapportée par KELA
technical-potential: 3
technical-reason: Le profil rassemble des TTP de persistance et de collecte utiles à la chasse, mais les IOC exacts ne sont pas visibles dans le matériel consulté.
artifacts: unknown
uncertainty: Le billet agrège plusieurs années d’activité et ne constitue pas une campagne distincte observée en juillet. Les frontières d’alias peuvent varier entre fournisseurs.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT34 (OilRig)
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22 ([KELA Cyber Threat Intelligence][26])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exact n’a été retenu depuis le contenu visible consulté.

## SUBJECT S15

title: Ababil of Minab et activité disruptive associée à Vyncs
presentation: Insikt Group évoque en juillet une nouvelle persona nommée Ababil of Minab, présentée comme probablement liée au MOIS et associée à des activités disruptives et à l’emploi d’IA pour développer ou améliorer des scripts. Les détails disponibles publiquement dans la synthèse restent sensiblement moins complets que pour les campagnes disposant d’un rapport primaire.
actor-campaign: Ababil of Minab / Vyncs; probable MOIS linkage selon Insikt Group
technical-potential: 2
technical-reason: La publication apporte un signal d’acteur et quelques éléments d’outillage, mais pas suffisamment d’artefacts visibles pour une exploitation technique approfondie à ce stade.
artifacts: unknown
uncertainty: La source primaire sous-jacente n’a pas été établie avec suffisamment de certitude dans cette recherche. Le lien au MOIS est évaluatif.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Insikt Group / Recorded Future
published-at: 2026-07-16 ([Recorded Future][15])
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Aucun IOC exact lié à Ababil of Minab n’a été retenu depuis la page visible.

## SUBJECT S16

title: Earth Vetala / MuddyWater et backdoor loué sur une plateforme criminelle
presentation: Le rapport H1 de Trend décrit Earth Vetala/MuddyWater combinant son propre outillage avec un backdoor loué sur une plateforme criminelle. Le C2 est présenté comme résolu au moyen de consultations de blockchain publique.
actor-campaign: Earth Vetala / MuddyWater
technical-potential: 3
technical-reason: Le recours à un backdoor criminel et à la blockchain pour la résolution C2 constitue un élément de tradecraft significatif, mais la page publique résume les détails.
artifacts: unknown
uncertainty: Le HTML public ne fournit qu’un résumé de la campagne. Les indicateurs techniques annoncés pour le rapport complet n’ont pas été individuellement vérifiés dans cette phase.

### PUBLICATION P1

title: 2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI
url: [https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai](https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai)
publisher: TrendAI™ Research
published-at: 2026-07-29 ([www.trendmicro.com][27])
role: independent
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La page annonce des indicateurs techniques dans le rapport complet, sans valeurs propres à cette campagne explicitement visibles dans le HTML consulté.

## SUBJECT S17

title: CyberAv3ngers et plateforme OT/IoT IOCONTROL
presentation: La synthèse semestrielle de Trend relie CyberAv3ngers à IOCONTROL, présenté comme une plateforme de malware réutilisable destinée à des environnements OT, notamment des systèmes liés aux carburants. Cette activité est conservée séparément de la vague de ciblage de PLC décrite par CISA.
actor-campaign: CyberAv3ngers / IOCONTROL
technical-potential: 3
technical-reason: La plateforme malware OT est techniquement significative, mais les détails et indicateurs de la recherche primaire ne sont pas suffisamment visibles dans la publication de juillet.
artifacts: unknown
uncertainty: La publication de juillet est une synthèse et non le rapport primaire sur IOCONTROL. Elle ne permet pas à elle seule de déterminer si toutes les opérations CyberAv3ngers OT relèvent de la même campagne.

### PUBLICATION P1

title: 2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI
url: [https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai](https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai)
publisher: TrendAI™ Research
published-at: 2026-07-29 ([www.trendmicro.com][27])
role: independent
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Le rapport complet est annoncé comme comportant des indicateurs techniques; aucune valeur IOC propre à IOCONTROL n’a été reproduite depuis le HTML visible.

## SUBJECT S18

title: Vague suspectée Iran-nexus contre des jauges de réservoirs de carburant aux États-Unis
presentation: Trend mentionne une vague distincte visant des jauges de réservoirs de carburant exposées à Internet sur des sites américains, avec un lien iranien suspecté. La synthèse distingue explicitement cette activité d’IOCONTROL.
actor-campaign: suspected Iran-linked fuel-tank-gauge wave
technical-potential: 2
technical-reason: Le ciblage OT est pertinent, mais la page de synthèse publique fournit peu d’éléments techniques vérifiables et aucun IOC exact visible pour cette vague.
artifacts: unknown
uncertainty: L’attribution à l’Iran est seulement suspectée dans la source. Les détails techniques de la campagne sont trop réduits dans le HTML public pour une attribution ou un regroupement plus précis.

### PUBLICATION P1

title: 2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI
url: [https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai](https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai)
publisher: TrendAI™ Research
published-at: 2026-07-29 ([www.trendmicro.com][27])
role: independent
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Des indicateurs techniques sont annoncés pour le rapport complet, mais aucune valeur propre à cette vague n’était explicitement visible dans la page consultée.

## SUBJECT S19

title: Nouvelle taxonomie GTIG ION pour les acteurs Iran-nexus
presentation: Google Threat Intelligence Group introduit une taxonomie unifiée dans laquelle le second terme ION identifie les acteurs associés à l’Iran et publie plusieurs correspondances d’alias. Cette publication est éditorialement utile pour normaliser les noms d’acteurs rencontrés dans les autres recherches du mois.
actor-campaign: GTIG ION taxonomy for Iran-nexus actors
technical-potential: 1
technical-reason: La publication n’apporte pas de chaîne d’attaque ni d’IOC, mais fournit un crosswalk d’alias directement utile à la désambiguïsation CTI.
artifacts: none
uncertainty: La taxonomie est propre à Google et les équivalences d’alias ne doivent pas être supposées universelles. La page a été mise à jour le 30 juillet.

### PUBLICATION P1

title: Updated Cyber Threat Actor Naming System
url: [https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system](https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system)
publisher: Google Threat Intelligence Group
published-at: 2026-07-24 ([Google Cloud][28])
role: primary
ioc-visibility: none
visible-ioc-types: none
visible-iocs: none
publisher-ioc-count: unknown
ioc-note: Aucun IOC n’est fourni dans la publication visible; la table associe notamment APT33 à BLEAK ION, APT34 à SOLAR ION, APT35 à RICH ION, APT39 à CINDER ION, APT42/CALANQUE à CALANQUE ION et MuddyWater à MUDDY ION.

# LIMITES

La recherche a couvert la fenêtre 2026-07-01 au 2026-07-31 et a été élargie à des requêtes multilingues dans les principales langues indiquées. Les résultats non anglophones retrouvés étaient majoritairement des reprises ou relais d’éléments déjà publiés par des sources primaires; ils n’ont pas été écartés pour leur langue, mais les duplications sans apport technique ou éditorial propre n’ont pas été multipliées.

Plusieurs publications de juillet sont des synthèses semestrielles, des profils d’acteurs ou des analyses rétrospectives. Elles ont été reliées à des rapports primaires antérieurs lorsque cela permettait de conserver les IOC et éléments techniques originaux, sans fusionner des campagnes distinctes au seul motif qu’elles relèvent du même acteur.

L’accès direct à certaines pages CISA était partiellement restreint lors de la consultation; lorsque des IOC étaient annoncés mais non visibles dans la page accessible, ils sont indiqués comme `declared` et leurs valeurs restent `unknown`. Aucun IOC n’a été reconstruit à partir d’un texte tronqué, d’une URL normale de navigation ou d’une infrastructure d’éditeur.

Les pages de synthèse Trend annoncent des indicateurs techniques dans le rapport complet, mais les valeurs propres à certaines sous-campagnes n’étaient pas explicitement visibles dans le HTML consulté; elles ne sont donc pas reproduites. La date exacte du rapport Unit 42 sur Screening Serpens n’a pas pu être établie avec suffisamment de certitude et reste `unknown`.

L’attribution de la vague contre les systèmes d’eau du Minnesota et de la vague contre les jauges de carburant américaines demeure incertaine dans les publications de la période. Elles sont proposées comme sujets candidats en raison de la piste Iran-nexus explicitement rapportée, sans les présenter comme des campagnes iraniennes établies.

[1]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/?utm_source=chatgpt.com "Cavern Manticore: Exposing Iran-Linked Modular C2 ..."
[2]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/ "https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/"
[3]: https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat "https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat"
[4]: https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat?utm_source=chatgpt.com "Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance ..."
[5]: https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a?utm_source=chatgpt.com "Iranian-Affiliated Cyber Actors Exploit Programmable Logic ..."
[6]: https://1275.ru/ioc/cisa-i-fbr-predpisali-nemedlenno-otklyuchit-plc-rockwell-schneider-i-siemens-ot-interneta-iz-za-atak-iranskih-hakerov_32692 "https://1275.ru/ioc/cisa-i-fbr-predpisali-nemedlenno-otklyuchit-plc-rockwell-schneider-i-siemens-ot-interneta-iz-za-atak-iranskih-hakerov_32692"
[7]: https://www.ioactive.com/iranian-affiliated-actors-expand-plc-targeting-to-siemens-and-schneider-electric-what-cisas-updated-advisory-means-for-cni/ "https://www.ioactive.com/iranian-affiliated-actors-expand-plc-targeting-to-siemens-and-schneider-electric-what-cisas-updated-advisory-means-for-cni/"
[8]: https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/?utm_source=chatgpt.com "Iran War Cyber Threat Landscape | A Midyear Assessment ..."
[9]: https://www.cisa.gov/news-events/alerts/2026/07/30/cisa-urges-water-and-wastewater-systems-sector-protect-ot-against-activity-targeting-plcs?utm_source=chatgpt.com "CISA Urges Water and Wastewater Systems Sector to ..."
[10]: https://www.reuters.com/legal/litigation/minnesota-it-officials-disclose-coordinated-cyberattack-more-than-30-local-water-2026-07-28/?utm_source=chatgpt.com "Minnesota IT officials disclose 'coordinated cyberattack' at more than 30 local water systems"
[11]: https://abcnews.com/US/investigators-iran-connection-minnesota-water-system-hacks-us/story?id=135237777&utm_source=chatgpt.com "Feds issue warning to local water systems over increased ..."
[12]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/?utm_source=chatgpt.com "MuddyWater in 2026: Iran's APT Hits U.S. Targets"
[13]: https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us?utm_source=chatgpt.com "Seedworm: Iranian APT on Networks of U.S. Bank, Airport ..."
[14]: https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us "https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us"
[15]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook?utm_source=chatgpt.com "AI Has Enhanced Iran's Asymmetric Playbook During ..."
[16]: https://www.group-ib.com/blog/muddywater-operation-olalampo/?utm_source=chatgpt.com "Operation Olalampo: Inside MuddyWater's Latest Campaign"
[17]: https://www.group-ib.com/blog/muddywater-operation-olalampo/ "https://www.group-ib.com/blog/muddywater-operation-olalampo/"
[18]: https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/ "https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/"
[19]: https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/ "https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/"
[20]: https://profero.io/blog/war-between-wars/?utm_source=chatgpt.com "The War Between Wars: How an IRGC Cyber Front Runs ..."
[21]: https://profero.io/blog/war-between-wars/ "https://profero.io/blog/war-between-wars/"
[22]: https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq "https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq"
[23]: https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/ "https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/"
[24]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/?utm_source=chatgpt.com "APT42: Iran's Human-Centric Espionage in 2026"
[25]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/?utm_source=chatgpt.com "Prince of Persia (Infy): Iran's Persistent APT"
[26]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/?utm_source=chatgpt.com "APT34 (OilRig): Espionage on Your Infrastructure"
[27]: https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai?utm_source=chatgpt.com "How APTs Are Weaponizing Trust in the Age of AI"
[28]: https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system "https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system"

