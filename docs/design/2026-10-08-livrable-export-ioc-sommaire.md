# Export IOC et sommaire du bulletin

L’export est construit exclusivement depuis le blob `EditionDocumentV2` du
dernier release figé, après vérification de son SHA-256. Le téléchargement
`GET /api/editions/{edition_id}/release/iocs.zip` contient un
`article.csv` par publication, `legacy_iocs_<identifiant>.txt` et un
`manifest.json`. Il ne consulte pas les publications courantes.

Chaque CSV conserve l’en-tête métier `[Groupe|Titre|Éditeur|URL]`, puis
`isNew,type,value`. Le groupe et le titre viennent du préfixe `[Groupe]` du
titre; éditeur et URL viennent de la source de publication core. `indicators`
produit `isNew=0`, `original_indicators` `isNew=1`. Les types `ip`, `domain`
et `url` sont refangés (`hxxp`, `[.]`, `[:]`). `HASH` devient `file` pour les
empreintes hexadécimales MD5 (32 caractères), SHA-1 (40) ou SHA-256
(64), en minuscules. E-mail, hash invalide, CVE et types hors pipeline sont
exclus avec leur type, valeur et raison dans le manifeste. Les virgules,
barres et retours de ligne sont encodés en `%2C`, `%7C`, `%0D` et `%0A` afin
que le lecteur Typst et le convertisseur legacy ne découpent jamais une
valeur. L’ordre des publications, types, valeurs et exclusions est stable;
l’archive ZIP utilise des dates fixes.

Une publication est une brève si ses sections de synthèse n’ont aucun titre
et son corps contient au plus 120 mots; sinon c’est un article. Les articles
sont numérotés selon leur position, puis les brèves continuent la séquence,
en conservant leur ordre relatif. Le sommaire relie chaque entrée à son
ancre PDF. Le numéro de bulletin reprend `edition.version`; l’identifiant est
`country_code + AAMM` à partir de `period_start`. Le rendu indique qu’aucune
règle Suricata n’est projetée. STIX et Excel restent délégués en aval; le
tableau de garde décrit l’export IOC joint.

Les changements de modèle, manifeste et politique Typst invalident le rendu
du bulletin: reconstruire le rendu d’édition depuis le document figé, sans
appel modèle. Les publications par sujet restent valides. Les polices
Typst copiées localement sont ignorées par Git et ne sont pas livrées.
