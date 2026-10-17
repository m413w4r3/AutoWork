# Plan de pivots IOC — Blinder Tunnel (préparé avant G2, aucune requête fournisseur envoyée)

Source : artefact `extraction` 495fc542-74f8-4065-8499-094c6d594fd5 (indicateurs `confirmed_ioc`, valeurs normalisées/défangées). Aucun IOC contextuel n’est inclus.

## Hashes (17)
- `0354862d83a61c8e69adc3e65f6e5c921523eff829ef1b169e4f0f143b04091f`
- `03cffb8c4aa5113de6034fb82af529e6`
- `0e25efeb4e3304815f9e51c1d9bd3a2e2a23ece3a32f0b47f829536f71ead17a`
- `3fd810a3aa0039993393741b32287c367a9a5037a41e826906440887cdd3ed13`
- `472e685e7994f51bbb259be9c61f01b8b8f35d20030f03215ce205993dbad7f5`
- `53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402`
- `5c384109d3e578a0107e8518bcb91cd63f6926f0c0d0e01525d34a734445685c`
- `6e7d9b33f1e72ea1ede71373a604ecdb060dab7d42055179c1eede9ecd1fd239`
- `76273382e4252c1f60a2251141e108942494409c759358320735891762c0682e`
- `7cc571aca6d8715d9aaad3d83e1bcd30467565d583db1dfe73697c5d00a1f875`
- `8a062a2d0741b57fab9635920b17275b`
- `d3561bd4aad003dc3e08157b0891860bb496b80cd6e44901692e08ab1d4e8260`
- `e51c6f0fbc5a7e0b03a0d6e1e1d26ab566d606b551c785bf882e9a02f04c862b`
- `f5b12772db6817f7a765a6fe7565fd3d4f87edc28e42fe3ec0244a372a410fc9`
- `f5ba1645694c62f527ed6ceda8c68a5c3dd92b4032439167e8e937e72803b4bd`
- `fb8d4c24bcfd853edb15c5c4096723b239f03255f17cec42f2d881f5f31b6025`
- `feb5d225fa38efe2a627ddfbe9654bf59c171ac0742cd565b7a5f22b45a4cc3a`

## IPv4 (9)
- `172.86.68.55`
- `195.16.74.138`
- `2.56.126.151`
- `2.56.126.157`
- `2.56.126.188`
- `38.180.136.127`
- `65.109.214.145`
- `87.248.129.239`
- `91.107.156.29`

## Domaines (8)
- `arthurshelby.click`
- `asdfafadafg.online`
- `cloud.g-drive.cam`
- `drivegoogel.cam`
- `googeldrive.cam`
- `googelmeet.online`
- `meetonline.cam`
- `speed-test.click`

## URL (5)
- `github.com/arturshellby`
- `github.com/johnshelllby`
- `https://cloud.g-drive.cam/signin/v2/identifier?from=landing`
- `https://github.com/greenberet0`
- `https://github.com/peakyblinders-tm`

## Ordre de pivot proposé
1. `vt.file_report` sur chaque hash (lecture seule, pas de soumission) ; relations `contacted_ips`/`contacted_domains`/`dropped_files` limitées.
2. `shodan.host` sur 91.107.156.29 (relais Blackwood), 65.109.214.145, 38.180.136.127, puis les autres IP ; `shodan.count`/`search` bornés pour l’infrastructure partagée.
3. `vt.relationships` des domaines `.cam`/`.online` (hypothèse infrastructure mutualisée Blackwood), `vt.intelligence_search` borné.
4. Pas de téléchargement, pas de Retrohunt, `do_not_submit=true`.

Budgets prévus : VT 500 unités, Shodan 100 crédits. Un résultat absent n’est jamais une réfutation.
