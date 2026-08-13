SUJETS CANDIDATS

SUBJECT S1

title: Cyber Isnaad Front : sabotage conjoint IT/OT d’une installation frigorifique israélienne

presentation: Une synthèse Kaspersky ICS CERT publiée le 3 août reprend l’enquête de Profero sur une opération destructive attribuée à Cyber Isnaad Front, persona évaluée comme dirigée par l’État iranien. L’incident combine le RAT/wiper GRAT sur le réseau IT et une manipulation directe de contrôleurs frigorifiques ayant provoqué des dommages physiques.

actor_or_campaign: Cyber Isnaad Front / Aria Sepehr Ayandehsazan (ASA), ex-Emennet Pasargad

technical_potential: 4

technical_potential_reason: Le rapport original fournit des IOC, quatre hashes SHA-256, une infrastructure C2, des TTP ATT&CK Enterprise/ICS, une analyse du wiper GRAT et une règle YARA exploitable.

artifacts: [ioc, yara]

uncertainties: L’attribution d’ASA/IRGC repose sur l’évaluation de Profero ; la synthèse d’août ne constitue pas une nouvelle observation de campagne.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: The War Between Wars: How an IRGC Cyber Front Runs Destructive OT and IT Attacks Under Cover of a Ceasefire

url: https://profero.io/blog/war-between-wars/

publisher: Profero

published_at: 2026-05-24

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P3

title: Cyber attacker targets refrigeration plant

url: https://www.coolingpost.com/world-news/cyber-attacker-targets-refrigeration-plant/

publisher: Cooling Post

published_at: 2026-08-05

period_relation: in_period

source_role: relay

ioc_presence: none

ioc_declared_count: unknown

ioc_visible_count: 0

SUBJECT S2

title: Nimbus Manticore / UNC1549 : MiniFast, AppDomain hijacking et SEO poisoning

presentation: La synthèse Kaspersky du 3 août rattache à l’activité Iran-linked les trois vagues documentées par Check Point contre les secteurs aviation et logiciel en février-avril 2026. Check Point attribue l’opération à Nimbus Manticore, acteur affilié à l’IRGC, et documente MiniJunk/MiniFast, des installateurs Zoom trojanisés et une évolution vers le SEO poisoning.

actor_or_campaign: Nimbus Manticore / UNC1549

technical_potential: 4

technical_potential_reason: Le rapport original détaille plusieurs chaînes d’infection, la persistance par détournement de tâche Zoom, le protocole C2 et une section de 52 IOC visibles, dont 27 SHA-256 et 25 domaines.

artifacts: [ioc]

uncertainties: L’activité elle-même se situe principalement de février à avril 2026 ; la publication dans la période est une synthèse et non un nouveau rapport d’incident.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Fast and Furious – Nimbus Manticore Operations During the Iranian Conflict

url: https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/

publisher: Check Point Research

published_at: 2026-05-22

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: 52

PUBLICATION P3

title: Detecting Nimbus Manticore and their sideloading infection chains

url: https://www.nextron-systems.com/2026/06/01/detecting-nimbus-manticore-and-their-sideloading-infection-chains/

publisher: Nextron Systems

published_at: 2026-06-01

period_relation: outside_period

source_role: independent

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S3

title: MuddyWater : reconnaissance massive, compromission OWA et C2 multiprotocole

presentation: Kaspersky reprend le 3 août une enquête Oasis Security sur une campagne structurée p[https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: The War Between Wars: How an IRGC Cyber Front Runs Destructive OT and IT Attacks Under Cover of a Ceasefire

url: [https://profero.io/blog/war-between-wars/](https://profero.io/blog/war-between-wars/?utm_source=chatgpt.com)

publisher: Profero

published_at: 2026-05-24

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P3

title: Cyber attacker targets refrigeration plant

url: https://www.coolingpost.com/world-news/cyber-attacker-targets-refrigeration-plant/

publisher: Cooling Post

published_at: 2026-08-05

period_relation: in_period

source_role: relay

ioc_presence: none

ioc_declared_count: unknown

ioc_visible_count: 0

SUBJECT S2

title: Nimbus Manticore / UNC1549 : MiniFast, AppDomain hijacking et SEO poisoning

presentation: La synthèse Kaspersky du 3 août rattache à l’activité Iran-linked les trois vagues documentées par Check Point contre les secteurs aviation et logiciel en février-avril 2026. Check Point attribue l’opération à Nimbus Manticore, acteur affilié à l’IRGC, et documente MiniJunk/MiniFast, des installateurs Zoom trojanisés et une évolution vers le SEO poisoning.

actor_or_campaign: Nimbus Manticore / UNC1549

technical_potential: 4

technical_potential_reason: Le rapport original détaille plusieurs chaînes d’infection, la persistance par détournement de tâche Zoom, le protocole C2 et une section de 52 IOC visibles, dont 27 SHA-256 et 25 domaines.

artifacts: [ioc]

uncertainties: L’activité elle-même se situe principalement de février à avril 2026 ; la publication dans la période est une synthèse et non un nouveau rapport d’incident.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: [https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Fast and Furious – Nimbus Manticore Operations During the Iranian Conflict

url: [https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/](https://research.checkpoint.com/2026/fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict/?utm_source=chatgpt.com)

publisher: Check Point Research

published_at: 2026-05-22

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: 52

PUBLICATION P3

title: Detecting Nimbus Manticore and their sideloading infection chains

url: https://www.nextron-systems.com/2026/06/01/detecting-nimbus-manticore-and-their-sideloading-infection-chains/

publisher: Nextron Systems

published_at: 2026-06-01

period_relation: outside_period

source_role: independent

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S3

title: MuddyWater : reconnaissance massive, compromission OWA et C2 multiprotocole

presentation: Kaspersky reprend le 3 août une enquête Oasis Security sur une campagne structurée présentant des caractéristiques cohérentes avec MuddyWater, visant notamment aviation, énergie, infrastructu[https://www.nextron-systems.com/2026/06/01/detecting-nimbus-manticore-and-their-sideloading-infection-chains/](https://www.nextron-systems.com/2026/06/01/detecting-nimbus-manticore-and-their-sideloading-infection-chains/)

publisher: Nextron Systems

published_at: 2026-06-01

period_relation: outside_period

source_role: independent

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S3

title: MuddyWater : reconnaissance massive, compromission OWA et C2 multiprotocole

presentation: Kaspersky reprend le 3 août une enquête Oasis Security sur une campagne structurée présentant des caractéristiques cohérentes avec MuddyWater, visant notamment aviation, énergie, infrastructures et secteur public. L’opération passe d’un scan automatisé de systèmes exposés à des attaques ciblées sur les identifiants OWA, puis à l’exfiltration de données.

actor_or_campaign: MuddyWater

technical_potential: 4

technical_potenti_al_reason: Oasis documente la chaîne opérationnelle, cinq vulnérabilités exploitées pour la reconnaissance, les outils de brute force, l’architecture de contrôleurs C2 TCP/UDP/HTTP et des paramètres de communication chiffrée.

artifacts: [ioc]

uncertainties: Oasis qualifie le tradecraft de cohérent avec MuddyWater plutôt que de présenter une attribution catégorique ; le rapport original date d’avril.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation:_ in_period

source_role: relay

ioc_presence: dec_lared

ioc_declared_count:[https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Multi-Stage Cyber Campaign Targeting Middle Eastern Critical Sectors with Tradecraft Consistent with MuddyWater

url: https://oasis-security.io/blog/260414-Iran

publisher: Oasis Security

published_at: 2026-04-14

period_re_lation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_cou[https://oasis-security.io/blog/260414-Iran](https://oasis-security.io/blog/260414-Iran?utm_source=chatgpt.com)

publisher: Oasis Security

published_at: 2026-04-14

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S4

title: Seedworm / MuddyWater : campagne d’espionnage mondiale avec DLL sideloading et orchestration Node.js

presentation: La synthèse Kaspersky du 3 août reprend une campagne Symantec/Carbon Black attribuée à Seedworm, ayant touché au moins neuf organisations dans neuf pays au premier trimestre 2026. La chaîne étudiée combine Node.js, PowerShell, DLL sideloading via des exécutables signés Fortemedia et SentinelOne, vol d’identifiants, tunneling SOCKS5 et exfiltration via un service public.

actor_or_campaign: Seedworm / MuddyWater / Temp Zagros / Static Kitten

technical_potential: 4

technical_pot_ential_reason: Le rapport original reconstitue chronologiquement une intrusion, fournit les commandes utilisées, les outils et mécanismes de persistance ainsi qu’une section IOC comprenant 9 indicateurs fichiers et 13 entrées réseau visibles.

artifacts: [ioc]

uncertainties: Le vecteur d’accès initial de l’intrusion principale reste inconnu ; l’activité rapportée se situe au premier trimestre et n’est rappelée en août que par la synthèse Kaspersky.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/

publisher: Kaspersky ICS CERT

published_at: 202_6-08-03

period_relation: in_period

source_role: relay

ioc_presence: declar_ed

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION[https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Seedworm: Iran-Linked Hackers Breached Korean Electronics Maker in Global Spying Campaign

url: https://www.security.com/threat-intelligence/iran-seedworm-electronics

publisher: Symantec and Carbon Black Threat Hunter Team

published_at: 2026-05-12

period_relation: outs_ide_period

source_role: primary

ioc[https://www.security.com/threat-intelligence/iran-seedworm-electronics](https://www.security.com/threat-intelligence/iran-seedworm-electronics)

publisher: Symantec and Carbon Black Threat Hunter Team

published_at: 2026-05-12

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: 22

SUBJECT S5

title: Acteur affilié à l’Iran ciblant des PLC Rockwell Automation dans les infrastructures critiques américaines

presentation: Kaspersky ICS CERT inclut dans sa publication du 3 août l’alerte inter-agences américaine concernant une activité APT affiliée à l’Iran contre des PLC CompactLogix et Micro850 exposés à Internet. Les opérateurs ont notamment extrait des fichiers projet et manipulé des données affichées dans les interfaces HMI/SCADA.

actor_o_r_campaign: Iran-affiliated APT / unknown

technical_potential: 3

technical_potential_reason: L’avis gouvernemental documente les équipements ciblés, les méthodes d’accès, les TTP et des éléments de hunting réseau, mais l’acteur précis demeure non nommé dans la publication examinée.

artifacts: [ioc]

uncertainties: L’identité exacte du groupe n’est pas établie ; l’avis CISA est antérieur à août et sa dernière mise à jour retrouvée date du 22 juillet 2026.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_rela_tion: in_period

source_role: rela_y

ioc_presence: declared

ioc_declared_count: unknown[https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers in Multiple U.S. Critical Infrastructure Sectors

url: https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a

publisher: CISA et partenaires gouvernementaux américains

published_at: unknown

period_relation: outside_period

source_ro_[https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a?utm_source=chatgpt.com)

publisher: CISA et partenaires gouvernementaux américains

published_at: unknown

period_relation: outside_period

source_role: primary

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

LIMITES

La recherche n’est pas exhaustive. La période demandée, du 1er au 31 août 2026, est incomplète au moment de la recherche, effectuée le 13 août 2026 : aucune publication postérieure au 13 août ne peut donc être évaluée.

Les résultats significatifs retrouvés dans la partie observable de la période sont dominés par la synthèse Kaspersky ICS CERT du 3 août, qui republie ou résume des recherches techniques originales antérieures. Ces rapports hors période ont été rattachés comme contexte et classés `primary` lorsqu’ils constituent la recherche originale ; ils n’ont pas été traités comme de nouvelles campagnes d’août.

Les décomptes d’IOC ne sont fournis que lorsqu’ils pouvaient être établis directement à partir d’une section publique clairement visible. `unknown` est conservé lorsque la source déclare des IOC sans fournir un total vérifiable, lorsque des artefacts sont mélangés à des éléments de hunting non néces?utm_source=chatgpt.com)

publisher: Nextron Systems

published_at: 2026-06-01

period_relation: outside_period

source_role: independent

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S3

title: MuddyWater : reconnaissance massive, compromission OWA et C2 multiprotocole

presentation: Kaspersky reprend le 3 août une enquête Oasis Security sur une campagne structurée présentant des caractéristiques cohérentes avec MuddyWater, visant notamment aviation, énergie, infrastructures et secteur public. L’opération passe d’un scan automatisé de systèmes exposés à des attaques ciblées sur les identifiants OWA, puis à l’exfiltration de données.

actor_or_campaign: MuddyWater

technical_potential: 4

technical_potential_reason: Oasis documente la chaîne opérationnelle, cinq vulnérabilités exploitées pour la reconnaissance, les outils de brute force, l’architecture de contrôleurs C2 TCP/UDP/HTTP et des paramètres de communication chiffrée.

artifacts: [ioc]

uncertainties: Oasis qualifie le tradecraft de cohérent avec MuddyWater plutôt que de présenter une attribution catégorique ; le rapport original date d’avril.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: [https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Multi-Stage Cyber Campaign Targeting Middle Eastern Critical Sectors with Tradecraft Consistent with MuddyWater

url: [https://oasis-security.io/blog/260414-Iran](https://oasis-security.io/blog/260414-Iran?utm_source=chatgpt.com)

publisher: Oasis Security

published_at: 2026-04-14

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: unknown

SUBJECT S4

title: Seedworm / MuddyWater : campagne d’espionnage mondiale avec DLL sideloading et orchestration Node.js

presentation: La synthèse Kaspersky du 3 août reprend une campagne Symantec/Carbon Black attribuée à Seedworm, ayant touché au moins neuf organisations dans neuf pays au premier trimestre 2026. La chaîne étudiée combine Node.js, PowerShell, DLL sideloading via des exécutables signés Fortemedia et SentinelOne, vol d’identifiants, tunneling SOCKS5 et exfiltration via un service public.

actor_or_campaign: Seedworm / MuddyWater / Temp Zagros / Static Kitten

technical_potential: 4

technical_potential_reason: Le rapport original reconstitue chronologiquement une intrusion, fournit les commandes utilisées, les outils et mécanismes de persistance ainsi qu’une section IOC comprenant 9 indicateurs fichiers et 13 entrées réseau visibles.

artifacts: [ioc]

uncertainties: Le vecteur d’accès initial de l’intrusion principale reste inconnu ; l’activité rapportée se situe au premier trimestre et n’est rappelée en août que par la synthèse Kaspersky.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: [https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Seedworm: Iran-Linked Hackers Breached Korean Electronics Maker in Global Spying Campaign

url: [https://www.security.com/threat-intelligence/iran-seedworm-electronics](https://www.security.com/threat-intelligence/iran-seedworm-electronics?utm_source=chatgpt.com)

publisher: Symantec and Carbon Black Threat Hunter Team

published_at: 2026-05-12

period_relation: outside_period

source_role: primary

ioc_presence: visible

ioc_declared_count: unknown

ioc_visible_count: 22

SUBJECT S5

title: Acteur affilié à l’Iran ciblant des PLC Rockwell Automation dans les infrastructures critiques américaines

presentation: Kaspersky ICS CERT inclut dans sa publication du 3 août l’alerte inter-agences américaine concernant une activité APT affiliée à l’Iran contre des PLC CompactLogix et Micro850 exposés à Internet. Les opérateurs ont notamment extrait des fichiers projet et manipulé des données affichées dans les interfaces HMI/SCADA.

actor_or_campaign: Iran-affiliated APT / unknown

technical_potential: 3

technical_potential_reason: L’avis gouvernemental documente les équipements ciblés, les méthodes d’accès, les TTP et des éléments de hunting réseau, mais l’acteur précis demeure non nommé dans la publication examinée.

artifacts: [ioc]

uncertainties: L’identité exacte du groupe n’est pas établie ; l’avis CISA est antérieur à août et sa dernière mise à jour retrouvée date du 22 juillet 2026.

PUBLICATION P1

title: APT and financial attacks on industrial organizations in Q2 2026

url: [https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/](https://ics-cert.kaspersky.com/publications/reports/2026/08/03/apt-and-financial-attacks-on-industrial-organizations-in-q2-2026/)

publisher: Kaspersky ICS CERT

published_at: 2026-08-03

period_relation: in_period

source_role: relay

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

PUBLICATION P2

title: Iranian-Affiliated Cyber Actors Exploit Programmable Logic Controllers in Multiple U.S. Critical Infrastructure Sectors

url: [https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a](https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a?utm_source=chatgpt.com)

publisher: CISA et partenaires gouvernementaux américains

published_at: unknown

period_relation: outside_period

source_role: primary

ioc_presence: declared

ioc_declared_count: unknown

ioc_visible_count: unknown

LIMITES

La recherche n’est pas exhaustive. La période demandée, du 1er au 31 août 2026, est incomplète au moment de la recherche, effectuée le 13 août 2026 : aucune publication postérieure au 13 août ne peut donc être évaluée.

Les résultats significatifs retrouvés dans la partie observable de la période sont dominés par la synthèse Kaspersky ICS CERT du 3 août, qui republie ou résume des recherches techniques originales antérieures. Ces rapports hors période ont été rattachés comme contexte et classés `primary` lorsqu’ils constituent la recherche originale ; ils n’ont pas été traités comme de nouvelles campagnes d’août.

Les décomptes d’IOC ne sont fournis que lorsqu’ils pouvaient être établis directement à partir d’une section publique clairement visible. `unknown` est conservé lorsque la source déclare des IOC sans fournir un total vérifiable, lorsque des artefacts sont mélangés à des éléments de hunting non nécessairement malveillants, ou lorsque la page originale n’était pas entièrement accessible.