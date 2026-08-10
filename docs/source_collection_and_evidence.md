# Collecte sûre des sources et extraction de preuves

## Déclenchement et frontières

La collecte ne démarre jamais lors de la sélection éditoriale. L'analyste ouvre le Workbench d'un
`Subject` sélectionné puis appelle explicitement `POST /api/subjects/{subject_id}/collection`.
Cette action crée le job canonique `source.collect`; Dramatiq ne transporte que son identifiant.
Le handler reprend les sources `failed_retryable`, `archived` ou `extracted` et ignore celles déjà
`completed`. Une annulation reste coopérative avant DNS, redirection, archivage, parsing et entre les
segments Qwen.

`fetching` est une prise de bail persistée sous `SELECT FOR UPDATE`. Un bail valide interdit un second
téléchargement. Après expiration, un nouveau job ajoute une tentative `interrupted` append-only puis
réclame la source. Les états `archived` et `extracted` repartent sans réseau. Une annulation avant
archivage libère immédiatement la source en `failed_retryable`; après archivage, elle reste
reconstructible depuis les blobs.

Le collecteur est un adaptateur typé. Les tests remplacent DNS et transport HTTP par des doubles et
n'ouvrent aucune connexion. Le collecteur de production :

- accepte uniquement HTTP et HTTPS, sans credentials dans l'URL ;
- bloque localhost, metadata cloud et toute adresse IPv4/IPv6 privée, loopback, link-local,
  multicast, réservée ou non globale ;
- contrôle deux réponses DNS puis connecte l'adresse approuvée, avec validation TLS du nom d'hôte ;
- recommence ces contrôles après chaque redirection ;
- borne la durée totale, les redirections, les octets réseau, les octets après décompression et le
  ratio de décompression ;
- détecte HTML ou PDF depuis les octets au lieu de faire confiance à `Content-Type` ;
- n'exécute ni JavaScript, macro, script, binaire ni échantillon.

Les domaines peuvent en plus être bornés par `COLLECTION_ALLOWED_DOMAINS` et
`COLLECTION_BLOCKED_DOMAINS` (listes séparées par des virgules).

## Modèle canonique et stockage

| Table | Contenu | Mutabilité |
| --- | --- | --- |
| `source_collections` | Source candidate rattachée au Subject, état et relation proposée | projection mutable |
| `collection_attempts` | URL, redirections, statut, MIME, deux tailles/hashes, job, politique et erreur | append-only |
| `collection_policy_snapshots` | Limites exactes, UA, domaines, versions collecteur/parseur/segments | append-only |
| `source_documents` | Observation sémantique d'une URL acquise | métadonnées canoniques |
| `derived_artifacts` | Texte dérivé, parseur/version et métadonnées de publication | append-only |
| `claims` | Valeur extraite, méthode, source et offsets | append-only |
| `indicators` | Valeurs originale/normalisée, type, source et offsets | append-only |
| `rejected_model_proposals` | Proposition, segment, type demandé, motif, hash et ModelRun | append-only |
| `human_decisions` | Validation, correction ou rejet par un acteur | append-only |

`source-raw` contient exactement les octets reçus après décodage du transfert HTTP, mais avant le
décodage de `Content-Encoding`. Son hash est `encoded_sha256`. `source-decoded` contient les octets
après gzip/deflate et porte `decoded_sha256`; c'est exclusivement cette représentation qui alimente
HTML/PDF. Le texte UTF-8 dérivé va dans `source-text`. Tous ces blobs sont adressés par contenu et ne
sont jamais stockés dans PostgreSQL.

Les états sont :

```text
queued -> fetching -> archived -> extracted -> completed
                   \-> unavailable
                   \-> blocked
                   \-> failed_retryable
                   \-> failed_terminal
```

Une reprise ne modifie ni ne supprime une tentative passée. Les migrations additives `0007` et
`0008` installent des triggers PostgreSQL refusant `UPDATE` et `DELETE` sur tentatives, snapshots,
artefacts et propositions rejetées.

## Extraction et validation

Le parseur HTML ignore les zones `script`, `style`, `noscript`, `template` et `svg`. Le parseur PDF
tourne dans un processus isolé interruptible. Il borne taille, pages, temps, texte et métadonnées,
refuse les PDF chiffrés ou malformés et n'exécute ni action, lien ni pièce jointe. Chaque texte est un
artefact dérivé versionné.

L'extracteur déterministe reconnaît hash, domaine, IP, URL, CVE, identifiant ATT&CK et email,
y compris les formes defangées. La valeur originale et la normalisation sont toutes deux gardées.
Qwen reçoit des segments déterministes chevauchants, explicitement marqués comme données distantes
non fiables. Chaque claim conserve son segment, son span local, son span global et son ModelRun. Les
doublons de chevauchement fusionnent leur provenance. Les catégories imposent les types suivants :
acteurs/campagnes/malwares/outils → `name`; chaîne d'infection → `infection_chain`; TTP → `ttp`;
victimologie → `victimology`; évaluations → `assessment`; incertitudes → `uncertainty`; faits →
`fact`, `date`, `ioc` ou `cve`. Une proposition invalide est journalisée individuellement et ne fait
pas perdre les propositions valides.

Une correction crée une `HumanDecision` avec valeur originale et valeur corrigée. Le `Claim` ou
l'`Indicator` initial n'est jamais écrasé. De même, une relation de source proposée par un modèle
reste `provisional`; `verified` exige une preuve déterministe `deterministic:*` ou une décision
humaine `human:*`.
