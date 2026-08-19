# SUJETS CANDIDATS

## SUBJECT S1

title: TAG-182 et MarkiRAT contre les Iraniens et la diaspora
presentation: Recorded Future décrit une infrastructure Iran-nexus diffusant MarkiRAT via de fausses applications VPN et multimédia afin de surveiller des cibles iraniennes. La publication expose directement chaîne d’attaque, infrastructure, échantillons, IOC et règles de détection.
actor-campaign: TAG-182
technical-potential: 4
technical-reason: Recherche primaire très technique avec échantillons, infrastructure C2, chaîne d’infection, IOC, YARA et Sigma.
artifacts: [ioc, samples, configurations, yara]
uncertainty: Aucun organisme iranien précis n’est attribué avec confiance ; le lien organisationnel avec Ferocious Kitten reste non établi.

### PUBLICATION P1

title: Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool
url: [https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat](https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-01
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256, ip, domain]
visible-iocs: [`3b172281f65ceaee280ae810edb6fd39a1ecd25649f929f246c0405df94f4c89`, `212[.]83[.]61[.]198`, `66dcd98c6b310f4429890821e609d48cc6395a6be15ffe5a121ec68b7a8f7402`, `51a6686b8c5ec7c610637398f3de43589f4e9fcbe8bcc0245343c5454d3b91de`, `a4f1b79e96a7d016de1991a64506792018de99eac5df00f7cabe26ef41b2bd81`, `400eb6a94810323a1fc5f8ab31c682fe765aaec2cc61b37c31d719c7e45c9a6c`, `8a7f5c8533df9e51b2da7cc2aeb52d8787418e4915577cc9288be1e46d1945c6`, `yeplayer[.]store`, `46[.]30[.]191[.]105`, `starvpn[.]pis2ray[.]online`]
publisher-ioc-count: unknown
ioc-note: L’annexe A contient des domaines, adresses IP et SHA-256 ; les annexes C et D fournissent respectivement une règle YARA et une règle Sigma. ([Recorded Future][1])

## SUBJECT S2

title: Cavern Manticore et le framework C2 modulaire Cavern
presentation: Check Point documente un nouvel acteur Iran-nexus ciblant notamment les secteurs gouvernemental et IT israéliens au moyen d’un framework C2 .NET modulaire. L’activité présente des recouvrements avec des acteurs liés au MOIS, dont MuddyWater et Lyceum.
actor-campaign: Cavern Manticore
technical-potential: 4
technical-reason: Analyse de code détaillée avec chaîne d’exécution, modules post-exploitation, configurations, infrastructure et IOC.
artifacts: [ioc, samples, configurations]
uncertainty: Le degré exact de rattachement organisationnel au MOIS reste dépendant de l’évaluation fournisseur ; la corroboration publique est encore limitée.

### PUBLICATION P1

title: Cavern Manticore: Exposing Iran-Linked Modular C2 Framework
url: [https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/](https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/)
publisher: Check Point Research
published-at: 2026-07-06
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256]
visible-iocs: [`37e123bd7998af4eae32718ce254776f36365a80ba56952593dab46f536d4066`, `92cae0ad7f98f51a14bcc0ee05e372ebdc29ea96ea7bd161bd3f55198767603b`, `5dc08bda6919a57a85e5f38b857985fa71529ca39c8299868d5a49a987e19b18`, `a4aa217def4c38f4ecacdf47b1cd687f60cc74c18ab75195be3c4357a790bf41`, `b630c96d3763182533d4fb9b614134382bd644cb02c6c1c3ade848b6ecc31e86`, `8e9425c0b46eeb516610ae913d13f2b3f44a023043cb099277031d4ec38a6134`, `0a3663648a46771a5a5423ad01e91a4e7ba825595e99fa934cb35cbb4848adc8`, `5394d3b220de4695f731647e3a70545f951a8912ceb0c6585efab8d6842e8b42`, `30cb4679c4b8599eeb3d63a551716475c6332bdc4d4b4e3de0964aadb3092a10`, `2cb1ad3b22db8e3666ea138fee88034a87a87cf43db3d3265a675ebf221379b0`]
publisher-ioc-count: unknown
ioc-note: La publication fournit également des domaines C2 et des artefacts hôte au-delà des dix valeurs reproduites ici. ([Check Point Research][2])

### PUBLICATION P2

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: SentinelLABS reprend Cavern Manticore comme activité d’espionnage par fournisseurs de services/RMM et qualifie le lien MOIS de confiance modérée et à source fournisseur unique. ([SentinelOne][3])

## SUBJECT S3

title: GigaWiper / BLUERABBIT et consolidation de capacités destructrices
presentation: Microsoft dissèque GigaWiper, un backdoor Golang combinant plusieurs familles destructrices antérieures dans un implant unique. Microsoft indique que GTIG et Binary Defense suivent le même ensemble sous le nom BLUERABBIT, pour lequel des analyses indépendantes rapportent un nexus iranien.
actor-campaign: BLUERABBIT / acteur derrière GigaWiper
technical-potential: 4
technical-reason: Analyse de code approfondie d’un backdoor destructeur avec hashes d’échantillons et infrastructure C2.
artifacts: [ioc, samples, configurations]
uncertainty: Microsoft ne formule pas lui-même dans cet article une attribution à l’Iran ; le nexus iranien repose sur les recoupements avec GTIG et Binary Defense.

### PUBLICATION P1

title: GigaWiper: Anatomy of a destructive backdoor assembled from multiple malware
url: [https://www.microsoft.com/en-us/security/blog/2026/07/09/gigawiper-anatomy-of-a-destructive-backdoor-assembled-from-multiple-malware/](https://www.microsoft.com/en-us/security/blog/2026/07/09/gigawiper-anatomy-of-a-destructive-backdoor-assembled-from-multiple-malware/)
publisher: Microsoft Threat Intelligence
published-at: 2026-07-09
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256, ip]
visible-iocs: [`633d4cbd496b1094495da89a64f5e6c31a0f6d4d1488411db5b0cba1cfe42001`, `ce9ad5f6c12019f4aae5b189bd8ddf5bb09e75b06a0a587b25a855c65948c913`, `f622ed85ef31ad4ab973f4e74524866fe1bb44f0965ad2b2ad796cd657a05bfd`, `9706a192e2c1a1faaf0a521daf31c2af60ff4590e3f47bbb4abc227f42af0683`, `3c30deb6556a94cfb84ae51798f4aecfae8c7358e55fdb321c5f2376579631cd`, `440b5385d3838e3f6bc21220caa83b65cd5f3618daea676f271c3671650ce9a3`, `12c39f052f030a77c0cd531df86ad3477f46d1287b8b98b625d1dcf89385d721`, `db41e0da7ab3305be8d9720769c6950b4dc1c1984ef857d3310eb873a0fc7674`, `185.182.193[.]21`, `212.8.248[.]104`]
publisher-ioc-count: unknown
ioc-note: Microsoft publie huit SHA-256 et deux adresses IP dans son tableau IOC. ([Microsoft][4])

### PUBLICATION P2

title: BLUERABBIT: A Golang-Based Backdoor with Ransomware and Destructive Capabilities
url: [https://binarydefense.com/resources/blog/bluerabbit-a-golang-based-backdoor-with-ransomware-and-destructive-capabilities](https://binarydefense.com/resources/blog/bluerabbit-a-golang-based-backdoor-with-ransomware-and-destructive-capabilities)
publisher: Binary Defense ARC Labs
published-at: unknown
role: independent
ioc-visibility: visible
visible-ioc-types: [sha256, ip, ja3, ja4, ja3s]
visible-iocs: [`633d4cbd496b1094495da89a64f5e6c31a0f6d4d1488411db5b0cba1cfe42001`, `9706a192e2c1a1faaf0a521daf31c2af60ff4590e3f47bbb4abc227f42af0683`, `ce9ad5f6c12019f4aae5b189bd8ddf5bb09e75b06a0a587b25a855c65948c913`, `f622ed85ef31ad4ab973f4e74524866fe1bb44f0965ad2b2ad796cd657a05bfd`, `185.182.193.21`, `212.8.248.104`, `806dab5164cf60d94026b88ab2d9851d`, `t13i131000_f57a46bbacb6_e5728521abd4`, `d80125b9429e9d5f06ace959f00de8d0`, `d75f9129bb5d05492a65ff78e081bcb2`]
publisher-ioc-count: unknown
ioc-note: Binary Defense relie BLUERABBIT à un ensemble de menaces probablement Iran-nexus et expose des empreintes réseau en plus des hashes et IP. ([Binary Defense][5])

## SUBJECT S4

title: Seedworm/MuddyWater et le backdoor Dindoor
presentation: La série KELA de juillet remet en avant la campagne Dindoor observée début 2026 contre notamment une banque et un aéroport américains. La recherche primaire de Symantec/Security.com documente les backdoors et l’exfiltration vers un stockage cloud commercial.
actor-campaign: Seedworm / MuddyWater
technical-potential: 4
technical-reason: Rapport primaire avec échantillons et IOC, complété par une synthèse de juillet replaçant la campagne dans l’activité 2026.
artifacts: [ioc, samples]
uncertainty: La publication de juillet est une synthèse ; l’investigation technique primaire date de mars.

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
ioc-note: KELA décrit Dindoor, son emploi de Deno et l’exfiltration via Rclone vers Wasabi, mais aucun tableau d’IOC propre à cette page n’a été retenu. ([KELA Cyber Threat Intelligence][6])

### PUBLICATION P2

title: Seedworm: Iranian APT on Networks of U.S. Bank, Airport, Software Company
url: [https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us](https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us)
publisher: Security.com / Symantec Threat Hunter Team
published-at: 2026-03-05
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256]
visible-iocs: [`0f9cf1cf8d641562053ce533aaa413754db88e60404cab6bbaa11f2b2491d542`, `1d984d4b2b508b56a77c9a567fb7a50c858e672d56e8cf7677a1fca5c98c95d1`, `2a00705cfd3c15cf8913e9eb4e23968efd06f1feceaef9987d26c5518887d043`, `2a09bbb3d1ddb729ea7591f197b5955453aa3769c6fb98a5ef60c6e4b7df23a5`, `42a5db2a020155b2adb77c00cbe6c6ad27c2285d8c6114679d9d34137e870b3f`, `7467f326677a4a2c8576e71a832e297e794ea00e9b67c4fcbe78b5aec697cec4`, `7c30c16e7a311dc0cdb1cdfd9ea6e502f44c027328dbe7d960b9bcd85ccf5eef`, `b0af82de672d81f3c2f153977923b3884a8a9e7045b182c2379b19a1996931a0`, `bd8203ab88983bc081545ff325f39e9c5cd5eb6a99d04ae2a6cf862535c9829a`, `c7cf1575336e78946f4fe4b0e7416b6ebe6813a1a040c54fb6ad82e72673478e`]
publisher-ioc-count: unknown
ioc-note: Dix SHA-256 explicitement visibles sont reproduits parmi les indicateurs publiés. ([Security.com][7])

## SUBJECT S5

title: Operation Olalampo de MuddyWater
presentation: Operation Olalampo constitue une campagne distincte de MuddyWater avec plusieurs familles de malware et un usage de Telegram pour le C2. La synthèse Recorded Future de juillet la replace dans l’évolution récente des capacités iraniennes.
actor-campaign: MuddyWater / Operation Olalampo
technical-potential: 4
technical-reason: Recherche primaire avec plusieurs familles de malware, infrastructure, échantillons et IOC.
artifacts: [ioc, samples, configurations]
uncertainty: La publication de juillet synthétise une investigation primaire antérieure.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La date de publication du 16 juillet est confirmée par l’index de recherche Recorded Future ; la page est une synthèse couvrant plusieurs opérations iraniennes. ([Recorded Future][8])

### PUBLICATION P2

title: Operation Olalampo: Inside MuddyWater's Latest Campaign
url: [https://www.group-ib.com/blog/muddywater-operation-olalampo/](https://www.group-ib.com/blog/muddywater-operation-olalampo/)
publisher: Group-IB Threat Intelligence
published-at: 2026-02-20
role: primary
ioc-visibility: visible
visible-ioc-types: [domain, ip, sha1]
visible-iocs: [`codefusiontech[.]org`, `Promoverse[.]org`, `miniquest[.]org`, `jerusalemsolutions[.]com`, `162.0.230[.]185`, `209.74.87[.]100`, `143.198.5[.]41`, `209.74.87[.]67`, `f4e0f4449dc50e33e912403082e093dd8e4bc55d`, `3441306816018d08dd03a97ac306fac0200e9152`]
publisher-ioc-count: unknown
ioc-note: Les valeurs reproduites sont explicitement présentées comme infrastructure ou hashes liés à l’opération. ([Group-IB][9])

## SUBJECT S6

title: Operation IconCat / UNG0801 et les implants RUSTRIC/PYTRIC
presentation: KELA rattache en juillet Operation IconCat à l’activité récente de MuddyWater, avec reconnaissance et capacités destructrices. La publication primaire de SEQRITE avait initialement suivi le cluster comme UNG0801 sans attribution définitive à MuddyWater.
actor-campaign: UNG0801 / Operation IconCat; rattachement à MuddyWater rapporté par KELA
technical-potential: 4
technical-reason: Deux chaînes d’infection, implants Rust/Python, infrastructure et IOC directement exploitables.
artifacts: [ioc, samples, configurations]
uncertainty: L’attribution et le regroupement diffèrent entre fournisseurs ; SEQRITE décrivait initialement un cluster d’origine probable d’Asie occidentale.

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
ioc-note: KELA décrit RUSTRIC, PYTRIC et des recouvrements de développement avec d’autres outils de MuddyWater. ([KELA Cyber Threat Intelligence][6])

### PUBLICATION P2

title: UNG0801: Tracking Threat Clusters obsessed with AV Icon Spoofing targeting Israel
url: [https://www.seqrite.com/blog/ung0801-tracking-threat-clusters-obsessed-with-av-icon-spoofing-targeting-israel/](https://www.seqrite.com/blog/ung0801-tracking-threat-clusters-obsessed-with-av-icon-spoofing-targeting-israel/)
publisher: SEQRITE Labs APT Team
published-at: 2025-12-22
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256, domain, url, ip]
visible-iocs: [`6df21646d13c5b68c14c70516dfc74ef2aef4a4246970d7f4fbd072053ba40e6`, `6f079c1e2655ed391fb8f0b6bfafa126acf905732b5554f38a9d32d0b9ca407d`, `77ceeb88a1fe4fb03af1acc589e02aeb156e3b22b110124ce1b25c940b0d9bbe`, `54ebdea80d30660f1d7be0b71bc3eb04189ef2036cdbba24d60f474547d3516a`, `2afcac3231235b5cea0fc702d705ec76afec424a9cec820749b83b6299d1fe1b`, `e422c2f25fbb4951f069c6ba24e9b917e95edb9019c10d34de4309f480c342df`, `stratioai[.]org`, `hxxps://www[.]dropbox[.]com/scl/fi/e2tctz6iy0s81dcxysbkf/help.pdf?rlkey=4b3uydquzd0h5xe7lk0gk95r9&st=c1qfydwi&dl=1`, `159[.]198[.]68[.]25`]
publisher-ioc-count: unknown
ioc-note: Les IOC sont explicitement visibles dans la recherche primaire SEQRITE. ([Seqrite][10])

## SUBJECT S7

title: APT42 / SpearSpecter
presentation: La synthèse KELA de juillet maintient SpearSpecter parmi les opérations récentes d’APT42 contre des individus à forte valeur. La recherche de l’Israel National Digital Agency documente une campagne IRGC-IO combinant ingénierie sociale personnalisée, TAMECAT et plusieurs canaux C2.
actor-campaign: APT42 / SpearSpecter
technical-potential: 3
technical-reason: Campagne techniquement documentée avec backdoor PowerShell, infrastructure cloud et C2 Telegram/Discord, mais les IOC n’ont pas été vérifiés dans la page consultée.
artifacts: [unknown]
uncertainty: La publication primaire est antérieure à juillet ; la publication de juillet est une synthèse d’acteur.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT42
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La page de juillet traite APT42 comme un acteur d’espionnage subordonné à l’IRGC et reprend ses campagnes récentes. ([KELA Cyber Threat Intelligence][11])

### PUBLICATION P2

title: SpearSpecter
url: [https://govextra.gov.il/national-digital-agency/cyber/research/spearspecter/](https://govextra.gov.il/national-digital-agency/cyber/research/spearspecter/)
publisher: Israel National Digital Agency
published-at: unknown
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La source indique une publication en novembre 2025 sans jour exact visible dans le résultat consulté ; elle décrit TAMECAT et des C2 HTTPS, Discord et Telegram. ([Minisite-New][12])

## SUBJECT S8

title: UNK_SmudgedSerpent et espionnage d’universitaires et experts
presentation: KELA rapproche en juillet UNK_SmudgedSerpent de l’écosystème APT42 tout en conservant une réserve d’attribution. Proofpoint avait documenté ce cluster Iran-nexus ciblant universitaires et spécialistes de politique étrangère au moyen d’usurpations, d’outils RMM et d’infrastructures dédiées.
actor-campaign: UNK_SmudgedSerpent
technical-potential: 4
technical-reason: Recherche primaire avec infrastructure de leurres, comptes d’usurpation, outils et indicateurs exploitables.
artifacts: [ioc, samples]
uncertainty: Proofpoint ne rattache pas le cluster avec haute confiance à APT42, TA455, TA453 ou TA450 ; le rapprochement KELA reste prudent.

### PUBLICATION P1

title: Iran's APTs and the U.S. Enterprise in 2026: APT42
url: [https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/](https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/)
publisher: KELA Cyber Intelligence Center
published-at: 2026-07-22
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: KELA traite UNK_SmudgedSerpent comme un cluster présentant des recouvrements avec l’activité iranienne suivie autour d’APT42, sans identité définitivement établie. ([KELA Cyber Threat Intelligence][11])

### PUBLICATION P2

title: Crossed wires: a case study of Iranian espionage and attribution
url: [https://www.proofpoint.com/us/blog/threat-insight/crossed-wires-case-study-iranian-espionage-and-attribution](https://www.proofpoint.com/us/blog/threat-insight/crossed-wires-case-study-iranian-espionage-and-attribution)
publisher: Proofpoint
published-at: 2025-11-05
role: primary
ioc-visibility: visible
visible-ioc-types: [email, url, domain, sha256]
visible-iocs: [`suzzanemaloney@gmail[.]com`, `suzannemaloney68@gmail[.]com`, `patrickclawson51@gmail[.]com`, `patrick.clawson51@outlook[.]com`, `hxxps://suzzanemaloney2506090953.onlyoffice[.]com/s.-k6vjflsdagdsfgh`, `thebesthomehealth[.]com`, `mosaichealthsolutions[.]com`, `healthcrescent[.]com`, `ebixcareers[.]com`, `6eb7df21d6f1e3546c252a112504eefbb19205167db89038f2861118bbc8871c`]
publisher-ioc-count: unknown
ioc-note: Les valeurs sont directement exposées dans l’étude de cas Proofpoint. ([Proofpoint][13])

## SUBJECT S9

title: Screening Serpens / Nimbus Manticore et Iranian Dream Job
presentation: SentinelLABS regroupe en juillet plusieurs labels fournisseurs autour d’une mission d’espionnage utilisant des leurres de recrutement. Unit 42 et Check Point avaient documenté en mai plusieurs nouveaux RAT, AppDomainManager hijacking, MiniUpdate/MiniJunk V2 et MiniFast.
actor-campaign: Screening Serpens / UNC1549 / Smoke Sandstorm / Nimbus Manticore; Iranian Dream Job
technical-potential: 4
technical-reason: Plusieurs recherches primaires complémentaires exposent familles de malware, chaînes d’exécution, infrastructure et IOC.
artifacts: [ioc, samples, configurations]
uncertainty: Les correspondances de nomenclature fournisseurs ne doivent pas être interprétées comme des alias strictement un-à-un.

### PUBLICATION P1

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: SentinelLABS cite six variantes de RAT observées entre février et avril et précise explicitement que son tableau de correspondance n’implique pas des alias un-à-un. ([SentinelOne][3])

### PUBLICATION P2

title: Tracking Iranian APT Screening Serpens’ 2026 Espionage Campaigns
url: [https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/](https://unit42.paloaltonetworks.com/tracking-iran-apt-screening-serpens/)
publisher: Unit 42 / Palo Alto Networks
published-at: 2026-05-22
role: primary
ioc-visibility: visible
visible-ioc-types: [domain, sha256]
visible-iocs: [`licencemanagers.azurewebsites[.]net`, `LicenceSupporting.azurewebsites[.]net`, `PeerDistSvcManagers.azurewebsites[.]net`, `ThemesManagers.azurewebsites[.]net`, `ThemesProviderManagers.azurewebsites[.]net`, `docspace-y4cumb.onlyoffice[.]com`, `NanoMatrix.azurewebsites[.]net`, `QuantumWeave.azurewebsites[.]net`, `ElementShift.azurewebsites[.]net`, `44f4f7aca7f1d9bfdaf7b3736934cbe19f851a707662f8f0b0c49b383e054250`]
publisher-ioc-count: unknown
ioc-note: La recherche primaire expose notamment infrastructure Azure/OnlyOffice et hashes d’échantillons. ([SOCRadar® Cyber Intelligence Inc.][14])

### PUBLICATION P3

title: Fast and Furious – Nimbus Manticore Operations During the Iranian Conflict
url: [https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/](https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/)
publisher: Check Point Research
published-at: 2026-05-22
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Check Point décrit notamment MiniFast et une activité d’espionnage contre les secteurs aviation et logiciel. ([Check Point Research][15])

## SUBJECT S10

title: Prince of Persia / Infy et renouvellement de l’outillage
presentation: KELA consacre en juillet un profil aux activités 2025-2026 de Prince of Persia/Infy. Les travaux primaires de SafeBreach décrivent notamment Foudre, Tonnerre et Tornado, avec une infrastructure renouvelée après plusieurs interruptions historiques.
actor-campaign: Prince of Persia / Infy
technical-potential: 4
technical-reason: Recherche primaire riche en familles de malware, infrastructure, C2 et IOC.
artifacts: [ioc, samples, configurations]
uncertainty: Le rattachement à l’État iranien est fortement évalué mais aucun service de renseignement précis n’est publiquement établi.

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
ioc-note: KELA publie ce quatrième volet de sa série Iran le 22 juillet 2026. ([KELA Cyber Threat Intelligence][16])

### PUBLICATION P2

title: Unmasking the Evolving Iranian Prince of Persia
url: [https://www.safebreach.com/blog/prince-of-persia-a-decade-of-an-iranian-nation-state-apt-campaign-activity/](https://www.safebreach.com/blog/prince-of-persia-a-decade-of-an-iranian-nation-state-apt-campaign-activity/)
publisher: SafeBreach Labs
published-at: 2025-12-18
role: primary
ioc-visibility: visible
visible-ioc-types: [ip, domain]
visible-iocs: [`45.80.148.35`, `45.80.151.166`, `45.80.151.24`, `45.80.151.179`, `45.80.148.128`, `179.43.190.13`, `45.80.151.71`, `dmxqdlcuiryu.site`, `xleeuzjdpqwm.ix.tc`, `xleeuzjdpqwm.hbmc.net`]
publisher-ioc-count: unknown
ioc-note: Les adresses IP et domaines reproduits figurent explicitement dans les indicateurs de la recherche SafeBreach. ([SafeBreach][17])

## SUBJECT S11

title: APT34 / OilRig et espionnage fondé sur identité, Exchange et IIS
presentation: Le dernier volet de la série KELA de juillet présente APT34/OilRig comme une capacité iranienne d’espionnage mature privilégiant le vol d’identités, Exchange, IIS et des outils discrets. Recorded Future signale parallèlement des activités 2026 liées à l’infrastructure MFA et à des leurres géopolitiques.
actor-campaign: APT34 / OilRig
technical-potential: 3
technical-reason: Les publications de juillet donnent un ensemble substantiel de TTP mais moins d’artefacts techniques directement visibles que les sujets à potentiel maximal.
artifacts: [unknown]
uncertainty: Les activités 2026 sont réparties entre plusieurs sources et ne constituent pas nécessairement une campagne unique.

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
ioc-note: KELA publie ce cinquième volet de sa série le 22 juillet et le décrit comme consacré à APT34/OilRig. ([KELA Cyber Threat Intelligence][18])

### PUBLICATION P2

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse décrit les opérations cyber iraniennes de 2026 et leurs usages croissants de l’IA sans présenter sur la page consultée un jeu d’IOC propre à APT34. ([Recorded Future][8])

## SUBJECT S12

title: Earth Vetala / MuddyWater et l’usage de ChainShell/CastleRAT
presentation: TrendAI signale en juillet qu’Earth Vetala, associé à MuddyWater, combine ses propres outils avec un backdoor loué sur une plateforme criminelle et utilise des blockchains publiques pour la résolution C2. Les investigations d’avril sur ChainShell/CastleRAT fournissent le contexte technique détaillé.
actor-campaign: Earth Vetala / MuddyWater
technical-potential: 4
technical-reason: Le sujet illustre la convergence APT-crimeware avec malware, configuration C2, infrastructure et jeu d’IOC explicite.
artifacts: [ioc, samples, configurations]
uncertainty: La correspondance exacte entre les labels Earth Vetala, MuddyWater et chaque sous-ensemble ChainShell/CastleRAT varie selon les fournisseurs.

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
ioc-note: La page nomme explicitement Earth Vetala/MuddyWater et annonce que le rapport complet contient des indicateurs techniques, sans exposer dans l’extrait Web consulté les valeurs Iran correspondantes. ([www.trendmicro.com][19])

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
ioc-note: La recherche relie techniquement MuddyWater à une infrastructure Malware-as-a-Service russe. ([JUMPSEC][20])

### PUBLICATION P3

title: MuddyWater Adopts Russian CastleRAT MaaS for Upgraded Cyber Espionage
url: [https://radar.certfa.com/en/threats/view/18c7ac20/](https://radar.certfa.com/en/threats/view/18c7ac20/)
publisher: CERTFA Radar
published-at: 2026-04-07
role: independent
ioc-visibility: visible
visible-ioc-types: [domain, sha256]
visible-iocs: [`mazafakaerindahouse[.]info`, `serialmenot[.]com`, `sharecodepro[.]com`, `ttrdomennew[.]com`, `3df9dcc45d2a3b1f639e40d47eceeafb229f6d9e7f0adcd8f1731af1563ffb90`, `49f17c061a72cadaf9e3f90cc380e994883a965b7a4ad8953d8e8089c65908e6`, `4aaf77c410f1f465d5e9063af60a07ad184e7a92ee87c973c2ea1542bfd66bff`, `7ab597ff0b1a5e6916cad1662b49f58231867a1d4fa91a4edf7ecb73c3ec7fe6`, `94f05495eb1b2ebe592481e01d3900615040aa02bd1807b705a50e45d7c53444`, `a8c380b57cb7c381ca6ba845bd7af7333f52ee4dc4e935e98b48bb81facad72b`]
publisher-ioc-count: 17
ioc-note: CERTFA annonce explicitement 17 IOC liés, répartis en trois IP, quatre domaines et dix hashes de fichiers. ([Certfa Radar][21])

## SUBJECT S13

title: CyberAv3ngers et le malware OT/IoT IOCONTROL
presentation: TrendAI cite en juillet CyberAv3ngers et IOCONTROL comme composante de l’activité iranienne visant les technologies opérationnelles. La recherche primaire Claroty fournit un échantillon IOCONTROL récupéré sur un système Gasboy/Orpak, sa configuration MQTT et son infrastructure.
actor-campaign: CyberAv3ngers / IOCONTROL
technical-potential: 4
technical-reason: Malware OT analysé en profondeur avec échantillon, configuration de C2 et indicateurs réseau/hôte.
artifacts: [ioc, samples, configurations]
uncertainty: Les périmètres de CyberAv3ngers et d’autres clusters IRGC-CEC associés aux PLC ne sont pas nécessairement identiques opération par opération.

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
ioc-note: TrendAI indique explicitement que CyberAv3ngers est lié à IOCONTROL, présenté comme une plateforme réutilisable destinée notamment aux systèmes de carburant. ([www.trendmicro.com][22])

### PUBLICATION P2

title: Inside a New OT/IoT Cyberweapon: IOCONTROL
url: [https://claroty.com/team82/research/inside-a-new-ot-iot-cyber-weapon-iocontrol](https://claroty.com/team82/research/inside-a-new-ot-iot-cyber-weapon-iocontrol)
publisher: Claroty Team82
published-at: 2024-12-10
role: primary
ioc-visibility: visible
visible-ioc-types: [ip, domain, sha256, path]
visible-iocs: [`159[.]100[.]6[.]69`, `uuokhhfsdlk[.]tylarion867mino[.]com`, `ocferda[.]com`, `1b39f9b2b96a6586c4a11ab2fdbff8fdf16ba5a0ac7603149023d73f33b84498`, `/usr/bin/iocontrol`, `/etc/rc3.d/S93InitSystemd.sh`, `/tmp/iocontrol`, `/var/run/iocontrol.pid`]
publisher-ioc-count: unknown
ioc-note: Les indicateurs correspondent explicitement à l’échantillon et à l’infrastructure IOCONTROL analysés par Team82. ([Claroty][23])

## SUBJECT S14

title: Campagne iranienne contre des PLC exposés aux États-Unis
presentation: La mise à jour de juillet de l’avis inter-agences AA26-097A élargit une campagne Iran-affiliated visant des PLC exposés à Internet à plusieurs fabricants et ajoute de nouveaux éléments de détection. Un plan de threat hunting publié le 24 juillet transforme ces renseignements en requêtes, signatures et un jeu explicite de 21 IP.
actor-campaign: Iranian-affiliated APT activity / HYDROKITTEN / CYBER AVENG3RS / BAUXITE
technical-potential: 4
technical-reason: Sujet OT très riche avec IOC STIX, manipulation de logique PLC, Dropbear, requêtes de chasse, YARA, Sigma, Suricata et procédures PCAP.
artifacts: [ioc, pcap, yara, suricata]
uncertainty: L’avis couvre un ensemble Iran-affiliated et ne démontre pas que chaque compromission relève du même opérateur ; les représentations mises en cache de l’avis CISA ont présenté des divergences de version.

### PUBLICATION P1

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers Across US Critical Infrastructure
url: [https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a)
publisher: CISA et agences partenaires
published-at: 2026-04-07
role: primary
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: L’avis initial du 7 avril a été révisé le 22 juillet 2026 ; la révision porte notamment le jeu machine-readable à 21 adresses IP et élargit le périmètre fabricant. Les valeurs ne sont pas recopiées depuis une représentation CISA mise en cache dont la version n’était pas cohérente. ([1898advisories.burnsmcd.com][24])

### PUBLICATION P2

title: July 2026 Update - Iran-Affiliated Threat Actors Targeting Critical Infrastructure Programmable Logic Controllers
url: [https://1898advisories.burnsmcd.com/july-2026-update-iran-affiliated-threat-actors-targeting-critical-infrastructure-programmable-logic-controllers](https://1898advisories.burnsmcd.com/july-2026-update-iran-affiliated-threat-actors-targeting-critical-infrastructure-programmable-logic-controllers)
publisher: 1898 & Co.
published-at: 2026-07-24
role: independent
ioc-visibility: declared
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: 21
ioc-note: La publication relaie l’élargissement du jeu d’indicateurs de l’avis AA26-097A à 21 IP. ([1898advisories.burnsmcd.com][25])

### PUBLICATION P3

title: Threat Hunt Plan: Iran-Affiliated Targeting of Critical Infrastructure PLCs — Rockwell, Schneider, and Siemens OT Exploitation
url: [https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation](https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation)
publisher: 1898 & Co.
published-at: 2026-07-24
role: independent
ioc-visibility: visible
visible-ioc-types: [ip]
visible-iocs: [`79.133.46.209`, `84.200.205.165`, `88.80.150.199`, `88.80.150.200`, `88.80.150.202`, `135.136.1.133`, `141.11.164.153`, `175.110.121.39`, `175.110.121.41`, `175.110.121.42`]
publisher-ioc-count: 21
ioc-note: La page identifie explicitement un jeu CISA de 21 IP et distingue séparément des IP adjacentes identifiées par la communauté, non officielles, qui ne sont pas reprises ici ; elle contient aussi des règles Sigma, Suricata et YARA. ([1898advisories.burnsmcd.com][24])

## SUBJECT S15

title: Vague de compromissions de PLC dans les réseaux d’eau américains fin juillet
presentation: Le FBI et l’EPA ont signalé le 30 juillet des compromissions de PLC Rockwell MicroLogix 1100/1400 dans au moins sept États depuis le 27 juillet, avec perte de pression et inondations parmi les effets rapportés. Cette vague est conservée séparément de la campagne Iran-affiliated AA26-097A car les autorités n’en ont pas publiquement attribué l’auteur.
actor-campaign: unknown
technical-potential: 3
technical-reason: Incident OT significatif avec modèles de PLC, changements de configuration et effets opérationnels, mais attribution et artefacts publics encore incomplets.
artifacts: [unknown]
uncertainty: Attribution fédérale en attente ; la proximité temporelle et technique avec les opérations iraniennes contre des PLC ne suffit pas à établir un lien.

### PUBLICATION P1

title: Malicious Cyber Actors Targeting Water and Wastewater Sector Internet- Facing Programmable Logic Controllers, Causing Operational Disruptions
url: [https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions](https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions)
publisher: FBI / EPA
published-at: 2026-07-30
role: primary
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Le PSA décrit les modèles visés, les changements d’IP et de mots de passe ainsi que les effets opérationnels, sans attribution à un acteur précis. ([FBI][26])

### PUBLICATION P2

title: Coordinated "cyberattack" on U.S. water utilities: What you need to know
url: [https://www.tenable.com/blog/coordinated-cyberattack-on-minnesota-water-utilities-what-you-need-to-know](https://www.tenable.com/blog/coordinated-cyberattack-on-minnesota-water-utilities-what-you-need-to-know)
publisher: Tenable Research Special Operations
published-at: 2026-07-28
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Tenable souligne que l’attribution fédérale reste en attente malgré des similitudes avec l’écosystème CyberAv3ngers ; la page observée comporte des mises à jour allant jusqu’au 10 août. ([Tenable®][27])

## SUBJECT S16

title: Vague suspectée Iran-linked contre des jauges de réservoirs de carburant américaines
presentation: TrendAI distingue explicitement une vague visant des jauges de réservoirs de carburant exposées à Internet aux États-Unis de l’activité CyberAv3ngers/IOCONTROL. L’attribution est formulée seulement comme suspectée Iran-linked.
actor-campaign: suspected Iran-linked actors
technical-potential: 3
technical-reason: Ciblage OT directement pertinent mais peu d’artefacts techniques visibles dans la publication Web de juillet.
artifacts: [unknown]
uncertainty: Aucun acteur nommé ; l’attribution à l’Iran demeure une suspicion dans la source.

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
ioc-note: TrendAI mentionne séparément cette vague « suspected Iran-linked » et annonce des indicateurs techniques dans le rapport complet sans valeurs propres à cet incident visibles sur la page consultée. ([www.trendmicro.com][22])

## SUBJECT S17

title: RedKitten et la campagne accélérée par l’IA contre les manifestations iraniennes
presentation: Recorded Future reprend en juillet RedKitten parmi les opérations liées à l’écosystème cyber iranien. HarfangLab avait documenté une campagne en langue persane alignée sur les intérêts de l’État iranien, avec développement accéléré par IA et ciblage des mouvements de protestation.
actor-campaign: RedKitten
technical-potential: 4
technical-reason: Analyse primaire de malware avec chaîne de compromission, nombreux échantillons et règles de détection.
artifacts: [ioc, samples, yara]
uncertainty: HarfangLab souligne que l’attribution précise à une entité étatique iranienne reste difficile.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La publication de juillet reprend RedKitten dans son analyse de l’usage de l’IA par l’écosystème iranien. ([Recorded Future][8])

### PUBLICATION P2

title: RedKitten: AI-accelerated campaign targeting Iranian protests
url: [https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/](https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/)
publisher: HarfangLab
published-at: 2026-01-29
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256]
visible-iocs: [`d3bb28307d11214867c570fe594f773ba90195ed22b834bad038b62bf75a4192`, `c40c94d787f6a35ac1cb4c5f031cf5777b77c79dc3929181badea33aaf177aa7`, `59ee007fd17280470724eb8a11ab12a98e85fd2383af3065f5f09a7e1a73f88c`, `90aebc9849b659515fd70dde6db717ad457ab2a90522a410d1fd531ca8640624`, `96ee9d3ed80c59c4bf39ed630efbfa53591fbe51155db7919ef64535a6171044`, `6d474cf5aeb58a60f2f7c4d47143cc5a11a5c7f17a6b43263723d337231c3d60`, `16164c83ce4786ab85aa3fc9566a317519e866ff6cad3fbd647f3e955b8a8255`, `36413af1a7c7dc9e49fdf465ebc5abc3b4bb6b33f1c5ccaa17ae5e0794b6faaa`, `6e1bb2c41500ee18bd55a2de04bb3d74bd5c5e8c45eaeef030c7c6ea661cc2db`, `ac0e045b6f3683315ef420971f382e167385e39023d118d023fa6989e35fadf6`]
publisher-ioc-count: unknown
ioc-note: Dix SHA-256 explicitement visibles sont reproduits ; la recherche fournit également des éléments de détection. ([HarfangLab][28])

## SUBJECT S18

title: Dust Specter contre des responsables gouvernementaux irakiens
presentation: Recorded Future reprend en juillet Dust Specter dans son panorama des opérations iraniennes de 2026. Zscaler avait attribué avec confiance moyenne à élevée un nexus iranien à ce cluster et documenté plusieurs familles de malware utilisées contre des responsables gouvernementaux irakiens.
actor-campaign: Dust Specter
technical-potential: 4
technical-reason: Deux chaînes d’infection et plusieurs implants personnalisés avec hashes d’échantillons et détails de fonctionnement.
artifacts: [ioc, samples, configurations]
uncertainty: Le nexus iranien est évalué avec confiance moyenne à élevée ; une correspondance définitive avec APT34 ou d’autres clusters historiques n’est pas établie.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: La synthèse de juillet inclut Dust Specter parmi les exemples d’opérations Iran-nexus récentes. ([Recorded Future][8])

### PUBLICATION P2

title: Dust Specter APT Targets Government Officials in Iraq
url: [https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq](https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq)
publisher: Zscaler ThreatLabz
published-at: 2026-03-02
role: primary
ioc-visibility: visible
visible-ioc-types: [sha256]
visible-iocs: [`903f7869a94d88d43b9140bb656f7bb86ef725efc78ef2ff9d12fd7c7c2aca74`, `6bb0d45799076b3f2d7f602b978a0779868fc72a1188374f6919fbbfba23efce`, `797325b3c8a9356dcace75d93cb5cfb7847d2049c66772d4cc2cee821618cb96`, `293ee1fe8d36aa79cf1f64f5ddef402bc6939d229c6fca955c7b796119564779`, `ad26cd72a83b884a8bc5aaa87309683953e151ebb3fde42eda7bf9a4406e530d`, `f3f2dc31f70a105db161a5e7b463b2215d3cbd64ac0146fd68e39da1c279f7ef`]
publisher-ioc-count: unknown
ioc-note: Les SHA-256 reproduits sont explicitement visibles dans les tableaux d’échantillons de ThreatLabz. ([Zscaler][29])

## SUBJECT S19

title: Void Manticore et les personas Handala, Homeland Justice et Karma
presentation: SentinelLABS consacre une partie importante de son évaluation de juillet aux opérations destructrices, hack-and-leak et coercitives liées à Void Manticore et à plusieurs personas publiques. L’analyse insiste sur l’écart fréquent entre les effets techniquement confirmés et les revendications des personas.
actor-campaign: Void Manticore / Handala Hack Team / Homeland Justice / Karma
technical-potential: 3
technical-reason: Forte valeur CTI sur les modes opératoires destructeurs et l’infrastructure de personas, mais peu d’IOC directement visibles dans la synthèse.
artifacts: [unknown]
uncertainty: Les personas ne constituent pas nécessairement des alias un-à-un ; les impacts revendiqués, notamment dans certains incidents, ne sont pas tous indépendamment validés.

### PUBLICATION P1

title: Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters
url: [https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/](https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/)
publisher: SentinelLABS / SentinelOne
published-at: 2026-07-21
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: SentinelLABS associe Void Manticore à Red Sandstorm, Storm-0842, Banished Kitten et TAG-145 et distingue les personas Handala, Homeland Justice et Karma/KarmaBelow80. ([SentinelOne][3])

## SUBJECT S20

title: Cyber Isnaad Front, GRAT et sabotage de réfrigération industrielle
presentation: Recorded Future signale en juillet une opération attribuée à Cyber Isnaad Front ayant saboté un système de réfrigération industriel israélien. Profero avait documenté le front comme opérant avec ou pour une structure liée à l’IRGC et décrit GRAT, ses capacités RAT/ransomware/wiper et la manipulation de paramètres OT.
actor-campaign: Cyber Isnaad Front / GRAT
technical-potential: 4
technical-reason: Recherche primaire reliant intrusion IT et effet OT avec malware multifonction, persistence, infrastructure et YARA.
artifacts: [ioc, samples, configurations, yara]
uncertainty: Les relations organisationnelles exactes entre le front, ASA et les structures IRGC reposent sur l’évaluation du fournisseur.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Recorded Future cite explicitement l’attaque de mai contre un système industriel de réfrigération et son attribution à Cyber Isnaad Front. ([Recorded Future][30])

### PUBLICATION P2

title: The War Between Wars: How an IRGC Cyber Front Runs Destructive OT and IT Attacks Under Cover of a Ceasefire
url: [https://profero.io/blog/war-between-wars/](https://profero.io/blog/war-between-wars/)
publisher: Profero Threat Intelligence
published-at: 2026-05-24
role: primary
ioc-visibility: visible
visible-ioc-types: [ip:port, sha256, path, scheduled-task, registry-key]
visible-iocs: [`84[.]201[.]6[.]131:7878`, `84[.]201[.]6[.]131:9988`, `6f5f427d96656ae51405e6a5e65253759db45ea0a17da2d70f881404a4ed717b`, `0ad128e813314e4562489478e6def8c6dfcc251e006d7f55b24273e93d3bc7fb`, `c4909b2d7a7f813b5a3d729fe64535033e716ae89dc39c402a6cb8ccbccaadca`, `86194eb5c5abcfe763899aaad7eb64894c71e816dd7d27427c8bac4ab280533d`, `C:\Users\<user>\AppData\Roaming\Microsoft\Spelling\SpellChecker.exe`, `C:\ProgramData\WindowsUpdater.exe`, `OneDrive Update`, `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule\TaskCache\Tree\OneDrive Update`]
publisher-ioc-count: unknown
ioc-note: Profero expose des IOC hôte et réseau ainsi qu’une règle YARA pour l’outillage analysé. ([Profero | Rapid-IR][31])

## SUBJECT S21

title: Ababil of Minab et opérations contre Vyncs et LA Metro
presentation: Recorded Future considère en juillet Ababil of Minab comme une nouvelle persona hacktiviste probablement reliée au MOIS et note son usage de ChatGPT. Des publications d’avril documentent ses revendications contre la plateforme GPS Vyncs et LA Metro, avec un niveau de validation variable.
actor-campaign: Ababil of Minab
technical-potential: 3
technical-reason: Sujet opérationnel intéressant pour le suivi des personas et de l’IA, mais les artefacts techniques publics accessibles sont limités.
artifacts: [unknown]
uncertainty: Le lien avec le MOIS est évaluatif ; certaines revendications de compromission restent partiellement ou non indépendamment vérifiées.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Recorded Future décrit Ababil of Minab comme une persona probablement liée au MOIS et évoque son utilisation de ChatGPT dans l’opération Vyncs. ([Recorded Future][8])

### PUBLICATION P2

title: Cyber Intel Brief: Ababil of Minab Claims Breach of Vyncs GPS Platform
url: [https://www.dataminr.com/resources/ababil-of-minab-breach-of-vyncs-gps-platform/](https://www.dataminr.com/resources/ababil-of-minab-breach-of-vyncs-gps-platform/)
publisher: Dataminr
published-at: 2026-04-16
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Publication de contexte sur la revendication Vyncs ; aucun IOC explicitement vérifié dans le contenu consulté. ([Dataminr][32])

### PUBLICATION P3

title: Cyber Intel Brief: Pro-Iranian Actor Ababil of Minab Claims Cyberattack on LA Metro (LACMTA)
url: [https://www.dataminr.com/resources/intel-brief/pro-iran-actor-ababil-of-minab-claims-cyberattack-on-la-metro/](https://www.dataminr.com/resources/intel-brief/pro-iran-actor-ababil-of-minab-claims-cyberattack-on-la-metro/)
publisher: Dataminr
published-at: 2026-04-13
role: independent
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Publication de contexte sur la revendication LA Metro ; aucun IOC explicitement vérifié dans le contenu consulté. ([Dataminr][33])

## SUBJECT S22

title: Fausse application Israeli Home Front Command Red Alert
presentation: Recorded Future cite en juillet une campagne Iran-linked diffusant une réplique malveillante de l’application d’alerte du Home Front Command israélien. Unit 42 avait documenté cette activité dans son suivi de l’escalade Iran 2026 et publié des indicateurs associés.
actor-campaign: Iran-linked actors / malicious Red Alert replica campaign
technical-potential: 3
technical-reason: Campagne mobile/social engineering avec infrastructure de distribution et IOC, mais acteur précis non nommé.
artifacts: [ioc, samples]
uncertainty: L’acteur exact n’est pas nommé ; le tableau IOC de la publication Unit 42 couvre plusieurs sous-activités et toutes les valeurs ne peuvent pas être attribuées individuellement à la fausse application.

### PUBLICATION P1

title: AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict
url: [https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook](https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook)
publisher: Recorded Future / Insikt Group
published-at: 2026-07-16
role: aggregator
ioc-visibility: unknown
visible-ioc-types: unknown
visible-iocs: unknown
publisher-ioc-count: unknown
ioc-note: Recorded Future mentionne explicitement une campagne Iran-linked utilisant une réplique malveillante de l’application Red Alert israélienne. ([Recorded Future][30])

### PUBLICATION P2

title: Threat Brief: Escalation of Cyber Risk Related to Iran (Updated April 17)
url: [https://unit42.paloaltonetworks.com/iranian-cyberattacks-2026/](https://unit42.paloaltonetworks.com/iranian-cyberattacks-2026/)
publisher: Unit 42 / Palo Alto Networks
published-at: 2026-04-17
role: primary
ioc-visibility: visible
visible-ioc-types: [url, domain]
visible-iocs: [`hxxps[:]www[.]shirideitch[.]com/wp-content/uploads/2022/06/RedAlert[.]apk`, `hxxps[:]//api[.]ra-backup[.]com/analytics/submit.php`, `hxxps[:]//bit[.]ly/4tWJhQh`, `media.megafilehost2[.]sbs`, `cache3.filehost36[.]sbs`, `alpha.filehost36[.]sbs`, `srv2.filehost37[.]sbs`, `arch2.megadatahost3[.]homes`, `media.hyperfilevault2[.]mom`, `hyperfilevault2[.]mom`]
publisher-ioc-count: unknown
ioc-note: Ces valeurs sont visibles dans les IOC de la publication globale ; leur appartenance individuelle à la seule sous-campagne Red Alert n’est pas affirmée ici. ([Unit 42][34])

## SUBJECT S23

title: Nouvelle nomenclature GTIG des acteurs iraniens en ION
presentation: Google Threat Intelligence Group a introduit le 24 juillet une nouvelle taxonomie d’acteurs, complétée le 30 juillet par un tableau de renommage. Pour l’Iran, le suffixe ION est adopté et plusieurs acteurs majeurs reçoivent de nouveaux noms, ce qui est pertinent pour la normalisation des futures éditions CTI.
actor-campaign: GTIG Iran threat-actor naming taxonomy
technical-potential: 0
technical-reason: Publication de nomenclature utile à la corrélation CTI mais sans campagne, chaîne d’infection ou artefact technique.
artifacts: [none]
uncertainty: Il s’agit de méta-CTI et non d’une opération ; les correspondances fournisseurs ne doivent pas être considérées comme universelles.

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
ioc-note: Mise à jour du 30 juillet : Iran utilise le suffixe ION ; le tableau mappe notamment APT33 vers BLEAK ION, APT34 vers SOLAR ION, APT35 vers RICH ION, APT39 vers CINDER ION, APT42/CALANQUE vers CALANQUE ION et TEMP.Zagros/MUDDYCOAST vers MUDDY ION. ([Google Cloud][35])

# LIMITES

* Recherche effectuée au 2026-08-17, avec sélection des publications datées ou mises à jour dans la fenêtre observable du 2026-07-01 au 2026-07-31. Les publications primaires antérieures n’ont été ajoutées que lorsqu’elles apportaient le matériau technique original nécessaire à un SUBJECT apparu dans une publication de juillet.
* Les recherches ont été menées au-delà des langues de travail, notamment en anglais, français, espagnol, portugais, allemand, italien, néerlandais, polonais, ukrainien, russe, turc, arabe, persan, chinois, japonais, coréen, vietnamien, indonésien, thaï et hindi. Les résultats non anglophones significatifs retrouvés dans la fenêtre étaient principalement des relais, traductions ou reprises de recherches déjà représentées ci-dessus ; aucun sujet primaire distinct n’a été écarté pour raison linguistique.
* L’indexation publique reste hétérogène : certains portails fournisseurs, rapports téléchargeables, plateformes CTI commerciales, contenus supprimés ou sources non indexées peuvent ne pas être intégralement accessibles.
* Les synthèses de KELA, SentinelLABS, TrendAI et Recorded Future mentionnent plusieurs campagnes distinctes. Elles ont été rattachées à plusieurs SUBJECT lorsque nécessaire plutôt que de fusionner artificiellement les opérations.
* Pour AA26-097A, différentes représentations mises en cache de la page/PDF CISA n’étaient pas cohérentes quant à la version affichée. Les dix IP reproduites dans S14 proviennent donc uniquement du plan de threat hunting 1898 & Co. qui les présente explicitement comme appartenant au jeu CISA de 21 IP du 22 juillet ; les IP « community-identified » adjacentes n’ont pas été reprises.
* La page Tenable relative aux réseaux d’eau a été publiée le 28 juillet mais constitue un document vivant mis à jour jusqu’au 10 août, date antérieure à la date de recherche. Certaines informations actuellement visibles sur cette page sont donc postérieures à la période observable.
* `publisher-ioc-count` reste `unknown` dès qu’aucun total explicite n’est annoncé, même lorsque plusieurs valeurs sont visibles. Les seuls totaux repris sont ceux explicitement annoncés dans les sources consultées, notamment 21 pour le jeu CISA repris par 1898 & Co. et 17 pour la publication CERTFA.
* Plusieurs attributions sont volontairement conservées comme incertaines : relation TAG-182/Ferocious Kitten, rattachement UNG0801/Operation IconCat à MuddyWater, sponsor précis de RedKitten, vague de jauges de carburant « suspected Iran-linked », et relation éventuelle entre les incidents WWS de fin juillet et les opérations Iran-affiliated antérieures.
* La sélection n’inclut pas comme sujets autonomes les briefs stratégiques généraux qui n’apportaient ni campagne distincte ni nouvelle preuve opérationnelle ; ils ne sont pas dupliqués simplement parce qu’ils citent les mêmes acteurs.
* Cette phase reste une sélection éditoriale : aucune tentative d’analyse exhaustive des chaînes d’infection, TTP, outils, infrastructures ou victimologies n’a été effectuée.

[1]: https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat "Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool"
[2]: https://research.checkpoint.com/2026/cavern-manticore-exposing-iran-linked-modular-c2-framework/ "Cavern Manticore: Exposing Iran-Linked Modular C2 Framework - Check Point Research"
[3]: https://www.sentinelone.com/labs/iran-war-cyber-threat-landscape-a-midyear-assessment-on-what-matters/ "Iran War Cyber Threat Landscape | A Midyear Assessment on What Matters | SentinelOne"
[4]: https://www.microsoft.com/en-us/security/blog/2026/07/09/gigawiper-anatomy-of-a-destructive-backdoor-assembled-from-multiple-malware/ "GigaWiper: Anatomy of a destructive backdoor assembled from multiple malware | Microsoft Security Blog"
[5]: https://binarydefense.com/resources/blog/bluerabbit-a-golang-based-backdoor-with-ransomware-and-destructive-capabilities?utm_source=chatgpt.com "BLUERABBIT: A Golang-Based Backdoor with ..."
[6]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-muddywater/ "MuddyWater in 2026: Iran's APT Hits U.S. Targets"
[7]: https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us "https://www.security.com/threat-intelligence/iran-cyber-threat-activity-us"
[8]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook?utm_source=chatgpt.com "AI Has Enhanced Iran's Asymmetric Playbook During ..."
[9]: https://www.group-ib.com/blog/muddywater-operation-olalampo/ "https://www.group-ib.com/blog/muddywater-operation-olalampo/"
[10]: https://www.seqrite.com/blog/ung0801-tracking-threat-clusters-obsessed-with-av-icon-spoofing-targeting-israel/ "UNG0801: AV Icon Spoofing Threats Target Israel | Seqrite"
[11]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt42/?utm_source=chatgpt.com "APT42: Iran's Human-Centric Espionage in 2026"
[12]: https://govextra.gov.il/national-digital-agency/cyber/research/spearspecter/?utm_source=chatgpt.com "SpearSpecter - govextra"
[13]: https://www.proofpoint.com/us/blog/threat-insight/crossed-wires-case-study-iranian-espionage-and-attribution "https://www.proofpoint.com/us/blog/threat-insight/crossed-wires-case-study-iranian-espionage-and-attribution"
[14]: https://socradar.io/free-tools/ioc-radar/reports/tracking-iranian-apt-screening-serpens-2026-espionage-campaigns-90380ebd03f4 "https://socradar.io/free-tools/ioc-radar/reports/tracking-iranian-apt-screening-serpens-2026-espionage-campaigns-90380ebd03f4"
[15]: https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/?utm_source=chatgpt.com "Fast and Furious - Nimbus Manticore Operations During ..."
[16]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-prince-of-persia-infy/?utm_source=chatgpt.com "Prince of Persia (Infy): Iran's Persistent APT"
[17]: https://www.safebreach.com/blog/prince-of-persia-a-decade-of-an-iranian-nation-state-apt-campaign-activity/ "https://www.safebreach.com/blog/prince-of-persia-a-decade-of-an-iranian-nation-state-apt-campaign-activity/"
[18]: https://www.kelacyber.com/blog/irans-apts-and-the-us-enterprise-in-2026-apt34-oilrig/?utm_source=chatgpt.com "APT34 (OilRig): Espionage on Your Infrastructure"
[19]: https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai?utm_source=chatgpt.com "How APTs Are Weaponizing Trust in the Age of AI"
[20]: https://www.jumpsec.com/guides/chainshell-muddywater-russian-criminal-infrastructure/?utm_source=chatgpt.com "ChainShell: MuddyWater's Russian MaaS Link"
[21]: https://radar.certfa.com/en/threats/view/18c7ac20/?utm_source=chatgpt.com "MuddyWater Adopts Russian CastleRAT MaaS for Upgraded ..."
[22]: https://www.trendmicro.com/vinfo/us/security/news/cybercrime-and-digital-threats/2026-h1-apt-report-how-apts-are-weaponizing-trust-in-the-age-of-ai "2026 H1 APT Report: How APTs Are Weaponizing Trust in the Age of AI | Trend Micro (US)"
[23]: https://claroty.com/team82/research/inside-a-new-ot-iot-cyber-weapon-iocontrol?utm_source=chatgpt.com "Inside a New OT/IoT Cyberweapon: IOCONTROL"
[24]: https://1898advisories.burnsmcd.com/threat-hunt-plan-iran-affiliated-targeting-of-critical-infrastructure-plcs-rockwell-schneider-and-siemens-ot-exploitation "Threat Hunt Plan: Iran-Affiliated Targeting of Critical Infrastructure PLCs — Rockwell, Schneider, and Siemens OT Exploitation"
[25]: https://1898advisories.burnsmcd.com/july-2026-update-iran-affiliated-threat-actors-targeting-critical-infrastructure-programmable-logic-controllers?utm_source=chatgpt.com "July 2026 Update - Iran-Affiliated Threat Actors Targeting Critical ..."
[26]: https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions "Malicious Cyber Actors Targeting Water and Wastewater Sector Internet- Facing Programmable Logic Controllers, Causing Operational Disruptions — FBI"
[27]: https://www.tenable.com/blog/coordinated-cyberattack-on-minnesota-water-utilities-what-you-need-to-know "Minnesota & other US Water Cyber Attacks, CISA AA26-097A | Tenable®"
[28]: https://harfanglab.io/insidethelab/redkitten-ai-accelerated-campaign-targeting-iranian-protests/?utm_source=chatgpt.com "RedKitten: AI-accelerated campaign targeting Iranian ..."
[29]: https://www.zscaler.com/blogs/security-research/dust-specter-apt-targets-government-officials-iraq?utm_source=chatgpt.com "Dust Specter APT Targets Government Officials in Iraq"
[30]: https://www.recordedfuture.com/research/iran-ai-asymmetric-playbook "AI Has Enhanced Iran’s Asymmetric Playbook During the 2026 Conflict"
[31]: https://profero.io/blog/war-between-wars/?utm_source=chatgpt.com "The War Between Wars: How an IRGC Cyber Front Runs ..."
[32]: https://www.dataminr.com/resources/ababil-of-minab-breach-of-vyncs-gps-platform/ "https://www.dataminr.com/resources/ababil-of-minab-breach-of-vyncs-gps-platform/"
[33]: https://www.dataminr.com/resources/intel-brief/pro-iran-actor-ababil-of-minab-claims-cyberattack-on-la-metro/ "https://www.dataminr.com/resources/intel-brief/pro-iran-actor-ababil-of-minab-claims-cyberattack-on-la-metro/"
[34]: https://unit42.paloaltonetworks.com/iranian-cyberattacks-2026/ "https://unit42.paloaltonetworks.com/iranian-cyberattacks-2026/"
[35]: https://cloud.google.com/blog/topics/threat-intelligence/updated-cyber-threat-actor-naming-system "Updated Cyber Threat Actor Naming System | Google Cloud Blog"

