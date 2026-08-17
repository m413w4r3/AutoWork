# Collecte HTTP des sources

## Frontière avec l'analyse

La collecte démarre uniquement à la demande de l'analyste avec
`POST /api/subjects/{subject_id}/collection`. Cette action crée le job canonique
`source.collect`; Dramatiq ne transporte que son identifiant.

**La collecte HTTP n'appelle aucun modèle.** Elle n'effectue ni parsing documentaire, ni
extraction d'IOC/TTP, ni création de claim ou d'artefact dérivé. Qwen, OpenAI et ChatGPT Bridge ne
font pas partie de ses dépendances. L'analyse sera une action explicite ultérieure, une fois les
archives contrôlées par l'analyste.

Le workflow métier préparé est : Sources → Analyse → Rédaction → Validation. Pour un article
principal, il deviendra Sources → Analyse → Synthèse → Pivots → Validation. Seule l'étape Sources
est opérationnelle dans cet incrément.

## Sécurité HTTP et états

Le collecteur accepte uniquement HTTP(S), interdit les credentials, localhost, les endpoints de
métadonnées et les adresses non globales. Il vérifie deux résolutions DNS, épingle l'adresse
approuvée, revalide chaque redirection et borne durée, taille encodée, taille décodée et ratio de
décompression. Le MIME est détecté depuis les octets pour HTML, PDF, texte et JSON. Un contenu non
pris en charge est un échec terminal ; aucun contenu actif n'est exécuté.

Les états de collecte sont :

- `pending` : connue et non traitée (`queued` reste lisible pour les données historiques) ;
- `fetching` : téléchargement sous bail ;
- `archived` : raw et decoded archivés, succès de collecte ;
- `unavailable` : réponse distante non disponible ;
- `blocked` : politique SSRF/domaine ;
- `failed_retryable` : timeout ou erreur réseau temporaire ;
- `failed_terminal` : taille, encodage ou type définitivement refusé.

`extracted` et `completed` sont des états historiques réservés à l'analyse. Le job de collecte ne
les produit plus, mais les reconnaît comme déjà archivés. Une source `archived`, `extracted` ou
`completed` n'est jamais retéléchargée.

Chaque source est persistée puis comptée dans la progression, quel que soit son résultat. Une
indisponibilité, un blocage ou un timeout ne coupe pas la boucle. Une panne de PostgreSQL, du blob
store ou un invariant canonique corrompu remonte en revanche comme panne systémique.

Le job réussit lorsque toute la boucle a été exécutée et qu'au moins une archive nouvelle ou
antérieure existe. Les autres résultats deviennent des avertissements visibles par source. Si
aucune archive n'existe, l'erreur contrôlée est `source_collection_no_success`. Le résumé structuré
est conservé dans un événement de provenance référencé par `output_reference`.

Les retries sont ciblés avec
`POST /api/subjects/{subject_id}/sources/{source_collection_id}/retry`. Une source bloquée ne peut
pas contourner la politique. Une nouvelle collecte globale ne retente pas silencieusement les
échecs antérieurs.

## Raw, decoded et document analyste

`source-raw` contient les octets après retrait du framing HTTP et avant décodage de
`Content-Encoding`; son hash est `encoded_sha256`. `source-decoded` contient les octets après
gzip/deflate ; son hash est `decoded_sha256`. Les deux blobs sont immuables et adressés par contenu.
`SourceDocument` conserve les deux références, tailles et hashes, ainsi que les URL demandée/finale,
MIME déclaré/détecté, titre, éditeur, date, TLP et identifiants de provenance.

Le fichier analyste et l'endpoint de téléchargement utilisent toujours le blob decoded et le MIME
détecté. Un document gzip HTML n'est donc jamais servi comme octets gzip sous une extension HTML.

Le nom logique suit exactement :

```text
{date-publication}_TLP {tlp}_{titre-article}_{publisher}.{extension}
```

Les caractères de contrôle et caractères de chemin sont neutralisés, Unicode est normalisé NFC,
la longueur UTF-8 est bornée et l'extension détectée est préservée. Une collision ajoute
`__{decoded_sha256_8}` avant l'extension. Les replis sont `date-inconnue`, `titre-inconnu` et
`publisher-inconnu`; le TLP vient toujours de la politique canonique.

## Workspace et téléchargement

Le workspace est une vue non canonique, supprimable et reconstructible. Les sources sont
matérialisées atomiquement sous `01_sources/original/{logical_filename}` depuis le blob decoded.
Son manifeste conserve les identifiants document/collection/candidat, chemins, deux blobs, deux
hashes/tailles, MIME, URL, métadonnées éditoriales, TLP, acquisition, méthode de matérialisation et
statut canonique. Les chemins, symlinks et collisions non identiques sont refusés.

`GET /api/subjects/{subject_id}/sources/{source_collection_id}/download` vérifie l'appartenance au
sujet et l'existence du blob decoded. La réponse utilise `Content-Disposition: attachment`, un
`filename` ASCII, `filename*` UTF-8, le MIME détecté et `X-Content-Type-Options: nosniff`. Aucun
chemin fourni par le client n'est accepté.

## Analyse ultérieure

Le parsing, les artefacts texte, claims, IOC, TTP, conversations et décisions humaines existants
restent conservés pour compatibilité, mais ne sont plus couplés à `source.collect`. Leur robustesse
et leur orchestration feront l'objet de l'incrément Analyse.
