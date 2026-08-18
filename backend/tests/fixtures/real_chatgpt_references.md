# REFERENCES

## SOURCE S1

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

## SOURCE S2

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

## SOURCE S3

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

## EVENT R1

date: 2026-07-01
sources: S1, S2, S3
text: Diffusion de MarkiRAT via de faux VPN.
